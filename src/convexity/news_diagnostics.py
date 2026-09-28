"""Track record — has either engine actually predicted anything?

Reads the append-only sentiment history (one record per ticker per refresh
day) and scores both engines against what the stocks then did, measured as
the forward idiosyncratic return r_i - beta_i * r_SPY.

Every statistic is DATE-CLUSTERED. The previous panel pooled every
ticker-day into one Spearman correlation and reported a t-stat that assumed
independent observations — dozens of same-day names are not independent
draws, so that t was overstated by roughly sqrt(names per day), and its tier
chart showed "Very Bearish" earning the best return on n=1. Here one DATE is
one observation: the daily cross-sectional IC, its mean, and a t from the
daily series (Newey-West for overlapping 5-day returns).

A verdict is only given once there are enough dates to mean something
(VERDICT_MIN_DAYS); before that the answer is "too early", with the count.
"""

from __future__ import annotations

import math
from typing import Any

VERDICT_MIN_DAYS = 40
VERDICT_TARGET_DAYS = 60
_BULL = ("bullish", "very_bullish")
_BEAR = ("bearish", "very_bearish")


# ----------------------------------------------------------------- history
def load_records(raw: list[dict]) -> list[dict]:
    """Normalize history records to the two-engine field names.

    COMPATIBILITY SHIM — delete after 2026-12-31, when the pre-v1.12 records
    (written before 2026-09-24) have aged out of the history. It maps exactly
    two fields and nothing else: the old LLM `s_idio` -> `news_score` and the
    old v1 `ml_sar` -> `market_sar`. Old tiers are NOT mapped: the LLM ones
    were percentile-forced and the ML ones were 93% "bearish" by a calibration
    bug, so tier-based statistics start with the new records."""
    out = []
    for r in raw:
        if not isinstance(r, dict) or not r.get("symbol") or r["symbol"] == "__market__":
            continue
        rec = dict(r)
        if "news_score" not in rec and isinstance(r.get("s_idio"), (int, float)):
            rec["news_score"] = r["s_idio"]
        if "market_sar" not in rec and isinstance(r.get("ml_sar"), (int, float)):
            rec["market_sar"] = r["ml_sar"]
        out.append(rec)
    return out


# ----------------------------------------------------------------- statistics
def _rank(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    rx, ry = _rank(x), _rank(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def clustered_mean_t(series: list[float], horizon: int = 1) -> dict:
    """Mean of a daily series and its t-statistic. With horizon h > 1 the
    daily observations overlap (consecutive 5-day returns share 4 days), so
    the variance is Newey-West with Bartlett lag h-1; h=1 is the plain t.
    ml/scripts/train_utils.daily_ic uses this same function."""
    n = len(series)
    if n < 3:
        return {"mean": (sum(series) / n if n else None), "t": None, "n_days": n}
    mu = sum(series) / n
    e = [x - mu for x in series]
    if horizon <= 1:
        var = sum(v * v for v in e) / (n - 1)  # the textbook daily-series t
    else:
        var = sum(v * v for v in e) / n
        for lag in range(1, horizon):
            var += 2.0 * (1.0 - lag / horizon) * sum(e[i] * e[i - lag] for i in range(lag, n)) / n
    t = mu / math.sqrt(var / n) if var > 0 else None
    return {"mean": mu, "t": t, "n_days": n}


def wilson(k: int, n: int, z: float = 1.96) -> dict:
    """Hit rate with its Wilson 95% interval (well-behaved at small n, unlike
    the normal approximation that goes below 0 on 2 hits out of 3)."""
    if n <= 0:
        return {"k": 0, "n": 0, "rate": None, "lo": None, "hi": None}
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return {"k": k, "n": n, "rate": p, "lo": max(0.0, centre - half), "hi": min(1.0, centre + half)}


def verdict(ic: dict) -> dict:
    """Plain-language verdict from the date-clustered IC (plan §7.1)."""
    n = ic.get("n_days") or 0
    mean, t = ic.get("mean"), ic.get("t")
    if n < VERDICT_MIN_DAYS:
        return {
            "key": "too_early",
            "text": f"Too early — {n} of ~{VERDICT_TARGET_DAYS} trading days",
        }
    if t is not None and t >= 2 and (mean or 0) > 0:
        return {"key": "edge", "text": "Evidence of an edge"}
    if t is not None and 1 <= t < 2:
        return {"key": "weak", "text": "Weak evidence"}
    return {"key": "none", "text": "No evidence of an edge"}


def daily_ics(rows: list[dict], key: str, horizon: int, min_names: int = 5) -> list[tuple]:
    """[(date, ic, n_names)] — one cross-sectional Spearman per date."""
    by_date: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        s, f = r.get(key), r.get(f"fwd_{horizon}d")
        if isinstance(s, (int, float)) and isinstance(f, (int, float)):
            by_date.setdefault(r["date"], []).append((float(s), float(f)))
    out = []
    for d in sorted(by_date):
        pairs = by_date[d]
        if len(pairs) < min_names:
            continue
        ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if ic is not None:
            out.append((d, ic, len(pairs)))
    return out


def long_short(rows: list[dict], tier_key: str, horizon: int = 1) -> list[dict]:
    """Cumulative equal-weight (bullish+ minus bearish-) forward idio return,
    one point per date where both legs are populated."""
    by_date: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        t, f = r.get(tier_key), r.get(f"fwd_{horizon}d")
        if not isinstance(f, (int, float)) or t not in (*_BULL, *_BEAR):
            continue
        leg = "long" if t in _BULL else "short"
        by_date.setdefault(r["date"], {"long": [], "short": []})[leg].append(float(f))
    cum, out = 0.0, []
    for d in sorted(by_date):
        legs = by_date[d]
        if not legs["long"] or not legs["short"]:
            continue
        spread = sum(legs["long"]) / len(legs["long"]) - sum(legs["short"]) / len(legs["short"])
        cum += spread
        out.append(
            {
                "date": d,
                "spread_pct": round(spread * 100, 3),
                "cum_pct": round(cum * 100, 3),
                "n_long": len(legs["long"]),
                "n_short": len(legs["short"]),
            }
        )
    return out


def hit_rates(rows: list[dict], tier_of) -> dict:
    """Bullish calls followed by a positive 5d idio return, bearish calls by a
    negative one; each with a Wilson interval."""
    bk = bn = sk = sn = 0
    for r in rows:
        f = r.get("fwd_5d")
        t = tier_of(r)
        if not isinstance(f, (int, float)) or t is None:
            continue
        if t in _BULL:
            bn += 1
            bk += f > 0
        elif t in _BEAR:
            sn += 1
            sk += f < 0
    return {"bullish": wilson(bk, bn), "bearish": wilson(sk, sn), "all": wilson(bk + sk, bn + sn)}


def _news_tier(score: float | None) -> str | None:
    # Local import keeps this module free of the network-facing engine at
    # import time (the engine loads caches from disk on import).
    from convexity.news_sentiment import tier_for

    return tier_for(score)


# ----------------------------------------------------------------- main entry
def compute(raw_records: list[dict], closes=None, market_horizon: int = 5) -> dict[str, Any]:
    """The Track record payload. `closes` is a daily close frame (columns =
    symbols + SPY); None means "fetch it" (the route) — tests pass a frame."""
    records = load_records(raw_records)
    out: dict[str, Any] = {"n_records": len(records), "market_horizon_days": market_horizon}
    dates = sorted({r["date"] for r in records if r.get("date")})
    out["date_range"] = [dates[0], dates[-1]] if dates else None
    if not records:
        return _empty(out)
    if closes is None:
        from convexity.analytics import _bulk_close

        try:
            closes = _bulk_close(sorted({r["symbol"] for r in records}) + ["SPY"], "1Y")
        except Exception:
            closes = None
    if closes is None or getattr(closes, "empty", True) or "SPY" not in closes.columns:
        out["note"] = "price history unavailable"
        return _empty(out)

    import pandas as pd

    spy = closes["SPY"].dropna()
    series = {s: closes[s].dropna() for s in closes.columns}

    def fwd_idio(rec: dict, h: int) -> float | None:
        s = series.get(rec["symbol"])
        if s is None or len(s) < h + 2:
            return None
        ts = pd.Timestamp(rec["date"])
        pos = s.index.searchsorted(ts, side="right") - 1  # last close <= date
        spos = spy.index.searchsorted(ts, side="right") - 1
        if pos < 0 or pos + h >= len(s) or spos < 0 or spos + h >= len(spy):
            return None
        beta = rec.get("beta")
        beta = float(beta) if isinstance(beta, (int, float)) else 1.0
        return float(s.iloc[pos + h] / s.iloc[pos] - 1.0) - beta * float(
            spy.iloc[spos + h] / spy.iloc[spos] - 1.0
        )

    rows = []
    for rec in records:
        row = dict(rec, fwd_1d=fwd_idio(rec, 1), fwd_5d=fwd_idio(rec, 5))
        # Rank on the raw model score: z is re-anchored when the percentile
        # switches from the training knots to live history, the score never
        # is. Pre-v1.12 records only carry the raw v1 SAR.
        row["market_rank"] = next(
            (
                rec[k]
                for k in ("market_score", "market_sar")
                if isinstance(rec.get(k), (int, float))
            ),
            None,
        )
        rows.append(row)

    engines = {
        "news": ("news_score", "news_tier", 1),
        "market": ("market_rank", "market_tier", market_horizon),
    }
    quants: dict[str, Any] = {"daily_ic": {}, "tier_table": {}}
    for eng, (key, tier_key, h) in engines.items():
        ic_by_h = {}
        for hh in (1, 5):
            ics = daily_ics(rows, key, hh)
            st = clustered_mean_t([ic for _, ic, _ in ics], hh)
            ic_by_h[f"{hh}d"] = {
                **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in st.items()}
            }
            quants["daily_ic"].setdefault(eng, {})[f"{hh}d"] = [
                {"date": d, "ic": round(ic, 4), "n": n} for d, ic, n in ics
            ]
        scored = [r for r in rows if isinstance(r.get(key), (int, float))]
        latest = max((r["date"] for r in scored), default=None)
        out[eng] = {
            "horizon_days": h,
            "ic": ic_by_h,
            "verdict": verdict(ic_by_h[f"{h}d"]),
            "hit_rate": hit_rates(rows, lambda r, tk=tier_key: r.get(tk)),
            "long_short": long_short(rows, tier_key, 1),
            "coverage": {
                "n_records": len(scored),
                "pct_records": round(100.0 * len(scored) / max(1, len(rows)), 1),
                "latest_date": latest,
            },
        }
        quants["tier_table"][eng] = _tier_table(rows, tier_key)

    # News read per-lens hit rates (a lens call = its score's tier)
    out["news"]["lens_hit_rate"] = {
        lens: hit_rates(rows, lambda r, ln=lens: _news_tier(((r.get("lens") or {}).get(ln))))
        for lens in ("financials", "outlook", "competition", "regulation", "street")
    }
    agreements = [
        float(r["agreement"]) for r in rows if isinstance(r.get("agreement"), (int, float))
    ]
    bins = [0] * 10
    for a in agreements:
        bins[min(9, int(a * 10))] += 1
    out["news"]["consistency"] = {
        "mean": round(sum(agreements) / len(agreements), 3) if agreements else None,
        "n": len(agreements),
        "hist": {"edges": [i / 10 for i in range(11)], "counts": bins},
    }
    quants["market_calibration"] = _calibration(rows, market_horizon)
    out["quants"] = quants
    return out


def _tier_table(rows: list[dict], tier_key: str) -> dict:
    out = {}
    for t in ("very_bearish", "bearish", "no_edge", "neutral", "bullish", "very_bullish"):
        grp = [r for r in rows if r.get(tier_key) == t]
        if not grp:
            continue
        f1 = [r["fwd_1d"] for r in grp if isinstance(r.get("fwd_1d"), (int, float))]
        f5 = [r["fwd_5d"] for r in grp if isinstance(r.get("fwd_5d"), (int, float))]
        out[t] = {
            "n": len(grp),
            "fwd_1d_pct": round(100 * sum(f1) / len(f1), 3) if f1 else None,
            "fwd_5d_pct": round(100 * sum(f5) / len(f5), 3) if f5 else None,
            "n_days": len({r["date"] for r in grp}),
        }
    return out


def _calibration(rows: list[dict], h: int) -> list[dict]:
    """Market read z (quintile bins) vs the realized forward idio return."""
    pts = sorted(
        (float(r["market_z"]), float(r[f"fwd_{h}d"]))
        for r in rows
        if isinstance(r.get("market_z"), (int, float))
        and isinstance(r.get(f"fwd_{h}d"), (int, float))
    )
    if len(pts) < 25:
        return []
    k = 5
    out = []
    for b in range(k):
        grp = pts[b * len(pts) // k : (b + 1) * len(pts) // k]
        if grp:
            out.append(
                {
                    "z_mean": round(sum(p[0] for p in grp) / len(grp), 2),
                    "realized_pct": round(100 * sum(p[1] for p in grp) / len(grp), 3),
                    "n": len(grp),
                }
            )
    return out


def _empty(out: dict) -> dict:
    for eng in ("news", "market"):
        out[eng] = {
            "horizon_days": 1 if eng == "news" else out["market_horizon_days"],
            "ic": {},
            "verdict": verdict({"n_days": 0}),
            "hit_rate": hit_rates([], lambda r: None),
            "long_short": [],
            "coverage": {"n_records": 0, "pct_records": 0.0, "latest_date": None},
        }
    out["news"]["lens_hit_rate"] = {}
    out["news"]["consistency"] = {"mean": None, "n": 0, "hist": {"edges": [], "counts": []}}
    out["quants"] = {"daily_ic": {}, "tier_table": {}, "market_calibration": []}
    return out
