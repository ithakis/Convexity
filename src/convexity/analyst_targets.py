"""The analysts' 12-month price target: one number, computed once, read everywhere.

Before this module the app carried two different "mean targets" for the same
stock: the grid, the per-holding consensus table, the optimizer and the export
read Yahoo's summary (``targetMeanPrice``), while the stock window and σ/μ read
the mean of each firm's latest target (1.28.2). On CRWD (2026-10-22) they were
$246.65 and $285.02: the firm list had never been split-adjusted, and CRWD
split 4:1 on 2026-07-02, so the pre-split targets ($400–$720) still counted at
face value.

Now every surface reads :func:`consensus` (cached per symbol), and it is the
firms' list done properly:

1. Each firm's LATEST action (``upgrades_downgrades``). A latest action with
   no target is a firm that dropped its target: it is left out, unless the
   action only maintained/reiterated an unchanged rating, in which case the
   firm's previous target still stands. (The old code filtered blank targets
   BEFORE picking each firm's latest row, so a firm that moved to Neutral
   without a target kept counting its old Buy target.)
2. Split-adjusted: a target published before a split is divided by every
   split ratio since (Yahoo never restates them).
3. At most ``TARGET_MAX_AGE`` days old (a 12-month target past its horizon is
   no forecast), and within 4x of the price either way after adjustment (the
   per-firm feed can carry another line's figure: AZN.L had one at 198
   against 12,446p).
4. Mean, median, low, high and n are all from that one list, so the coverage
   card's range and its mean can never disagree again.

No firm list (most non-US listings) falls back to Yahoo's summary, from the
``.info`` the caller already has. ``source`` says which one it is.

Network: one ``upgrades_downgrades`` request (~0.1 s) per symbol per ``TTL``;
`fetch_one` passes the split history from its own price history
(``actions=True``) and its ``.info``, so nothing else is asked twice.
"""

from __future__ import annotations

import datetime as dt
import math

from convexity.cache import _cache_get, _cache_put

TARGET_MAX_AGE = 365  # days: a firm's target older than this is dropped
TARGET_STALE = 182  # days: older than this is flagged (drawn faded in σ/μ)
BAND = 4.0  # a target further than 4x from the price either way is another line's
TTL = 6 * 3600.0
MISS_TTL = 1800.0  # Yahoo answered nothing: ask again sooner (§9: never cache a failure long)
_KEEP_ACTIONS = ("main", "reit")  # actions that leave the firm's previous target standing


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _naive(ts) -> dt.datetime:
    """A pandas/py timestamp as a naive datetime (UTC wall time when aware)."""
    d = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if d.tzinfo is not None:
        d = d.astimezone(dt.UTC).replace(tzinfo=None)
    return d


def _split_list(splits) -> list[tuple[dt.datetime, float]]:
    """[(date, ratio)] from a pandas Series of split ratios (yfinance's
    ``Stock Splits``: 4.0 for 4-for-1, 0 on days without one)."""
    out: list[tuple[dt.datetime, float]] = []
    if splits is None:
        return out
    try:
        items = list(splits.items())
    except AttributeError:
        return out
    for d, r in items:
        r = _num(r)
        if r and r != 1.0:
            out.append((_naive(d), r))
    return out


def split_factor(splits: list[tuple[dt.datetime, float]], when: dt.datetime) -> float:
    """Product of the split ratios after ``when``: divide a price from then by it."""
    f = 1.0
    for d, r in splits:
        if d > when:
            f *= r
    return f


def firm_targets(ud, splits=None, price: float | None = None, today: dt.datetime | None = None):
    """Each firm's current target from an ``upgrades_downgrades`` frame (pure:
    tests feed it a synthetic frame). Returns a list sorted by target."""
    if ud is None or getattr(ud, "empty", True) or "currentPriceTarget" not in ud.columns:
        return []
    today = today or dt.datetime.now()
    spl = _split_list(splits)
    df = ud.reset_index()
    datecol = "GradeDate" if "GradeDate" in df.columns else df.columns[0]
    if "Firm" not in df.columns:
        return []
    lo, hi = (price / BAND, price * BAND) if price else (0.0, math.inf)
    firms = []
    for firm, g in df.sort_values(datecol).groupby("Firm"):
        last = g.iloc[-1]
        use = last
        if _num(last.get("currentPriceTarget")) is None:
            from_g = str(last.get("FromGrade") or "").strip()
            to_g = str(last.get("ToGrade") or "").strip()
            unchanged = not from_g or from_g == to_g
            if str(last.get("Action") or "") not in _KEEP_ACTIONS or not unchanged:
                continue  # the firm moved without a target: it has none now
            valid = g[g["currentPriceTarget"].map(_num).notna()]
            if valid.empty:
                continue
            use = valid.iloc[-1]
        when = _naive(use[datecol])
        age = (today - when).days
        if age > TARGET_MAX_AGE:
            continue
        k = split_factor(spl, when)
        target = _num(use["currentPriceTarget"]) / k
        if not lo <= target <= hi:
            continue
        prior = _num(use.get("priorPriceTarget"))
        prior = prior / k if prior else None
        firms.append(
            {
                "firm": str(firm),
                "rating": str(last.get("ToGrade") or ""),
                "target": round(target, 4),
                "prior": round(prior, 4) if prior else None,
                "date": when.date().isoformat(),
                "age_days": age,
                "stale": age > TARGET_STALE,
                "split_adjusted": k != 1.0,
                "raised_90d": bool(age <= 90 and prior and target > prior),
            }
        )
    return sorted(firms, key=lambda f: f["target"])


def summarise(firms: list[dict], summary: dict | None = None) -> dict | None:
    """The consensus block from a firm list, Yahoo's summary when it is empty.

    Keys: firms, mean, median (also as ``consensus``, σ/μ's name for it),
    low, high, n (firms; None for the summary), n_stale, n_opinions, source."""
    summary = summary or {}
    if firms:
        t = sorted(f["target"] for f in firms)
        n = len(t)
        med = t[n // 2] if n % 2 else (t[n // 2 - 1] + t[n // 2]) / 2
        return {
            "firms": firms,
            "mean": sum(t) / n,
            "median": med,
            "consensus": med,
            "low": t[0],
            "high": t[-1],
            "n": n,
            "n_stale": sum(1 for f in firms if f["stale"]),
            "n_opinions": summary.get("n_opinions"),
            "source": "firms",
        }
    mean = _num(summary.get("mean"))
    med = _num(summary.get("median"))
    if not (mean or med):
        return None
    return {
        "firms": [],
        "mean": mean or med,
        "median": med,
        "consensus": med or mean,
        "low": _num(summary.get("low")),
        "high": _num(summary.get("high")),
        "n": None,
        "n_stale": 0,
        "n_opinions": summary.get("n_opinions"),
        "source": "summary",
    }


def summary_from_info(info: dict | None) -> dict:
    info = info or {}
    return {
        "mean": info.get("targetMeanPrice"),
        "median": info.get("targetMedianPrice"),
        "low": info.get("targetLowPrice"),
        "high": info.get("targetHighPrice"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "n_opinions": _num(info.get("numberOfAnalystOpinions")),
    }


def consensus(symbol: str, tk=None, info: dict | None = None, splits=None, price=None):
    """The consensus block for ``symbol`` (see the module docstring), cached
    for ``TTL``. ``tk``/``info``/``splits``/``price`` are optional: pass what
    the caller already fetched and nothing is asked twice. None when Yahoo
    has no target at all (a fund)."""
    key = f"analyst_targets|{(symbol or '').upper()}" if symbol else None
    hit = _cache_get(key) if key else None
    if hit is not None:
        return hit.get("v")
    import yfinance as yf

    tk = tk or yf.Ticker(symbol)
    if info is None:
        try:
            s = tk.analyst_price_targets or {}
        except Exception:
            s = {}
        summary = {
            "mean": s.get("mean"),
            "median": s.get("median"),
            "low": s.get("low"),
            "high": s.get("high"),
            "price": s.get("current"),
            "n_opinions": None,
        }
    else:
        summary = summary_from_info(info)
    px = _num(price) or _num(summary.get("price"))
    try:
        ud = tk.upgrades_downgrades
    except Exception:
        ud = None
    if splits is None:
        try:
            splits = tk.splits
        except Exception:
            splits = None
    out = summarise(firm_targets(ud, splits, px), summary)
    if key:
        # Only a firm list is kept for the full TTL. Yahoo answers a burst of
        # requests with EMPTY frames rather than errors (a throttle, see
        # analytics._analyst_for), so a summary fallback or nothing may be a
        # throttled answer for a name that does have firms: asked again soon.
        firms_ok = bool(out and out["source"] == "firms")
        _cache_put(key, {"v": out}, TTL if firms_ok else MISS_TTL)
    return out


def scaled(block: dict | None, k: float | None, via: str | None = None) -> dict | None:
    """``block`` with every price multiplied by ``k`` (a research line's targets
    read in this listing's currency and units)."""
    if not block or not (k and math.isfinite(k) and k > 0):
        return None
    sc = lambda v: round(v * k, 4) if v is not None else None  # noqa: E731
    out = dict(block)
    out["firms"] = [
        {**f, "target": sc(f["target"]), "prior": sc(f.get("prior"))} for f in block["firms"]
    ]
    for f in ("mean", "median", "consensus", "low", "high"):
        out[f] = sc(block.get(f))
    if via:
        out["via"] = via
    return out


def apply_to_row(out: dict, block: dict | None) -> None:
    """Write a consensus block onto a streaming row (the grid, the
    per-holding table, the optimizer and the export read these keys)."""
    if not block:
        return
    out["target_mean_price"] = block.get("mean")
    out["target_median"] = block.get("median")
    out["target_low"] = block.get("low")
    out["target_high"] = block.get("high")
    out["target_n_firms"] = block.get("n")
    out["target_source"] = block.get("source")
