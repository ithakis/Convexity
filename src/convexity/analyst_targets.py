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
import json
import math
import threading

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
                "lowered_90d": bool(age <= 90 and prior and target < prior),
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
        ages = sorted(f.get("age_days", 0) for f in firms)
        return {
            "firms": firms,
            "mean": sum(t) / n,
            "median": med,
            "consensus": med,
            "low": t[0],
            "high": t[-1],
            "n": n,
            "n_stale": sum(1 for f in firms if f["stale"]),
            "age_days": ages[n // 2],
            "n_raised_90d": sum(1 for f in firms if f.get("raised_90d")),
            "n_lowered_90d": sum(1 for f in firms if f.get("lowered_90d")),
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
        "age_days": None,
        "n_raised_90d": None,
        "n_lowered_90d": None,
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
    if key and out and px:
        record(symbol, px, out)
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
    out["target_age_days"] = block.get("age_days")
    out["target_n_stale"] = block.get("n_stale")
    out["target_raised_90d"] = block.get("n_raised_90d")
    out["target_lowered_90d"] = block.get("n_lowered_90d")


# ---------------------------------------------------------------------------
# Targets as an expected return (the optimizer's Black-Litterman views)
# ---------------------------------------------------------------------------
# Two studies (2026-10-22; docs/architecture/backend.md "What analyst targets
# predict") found that a target's raw upside is not a return forecast: on 80
# large caps (2021-25) the plain mean ranked 12-month returns with IC 0.023
# (t 0.5), and on the S&P 500 (2015-25) the best analyst signal fell to IC
# 0.013 (t 0.9) once its volatility tilt was removed; the average upside
# (~11% a year in log terms) also runs above what is realised. So a view is
# the equilibrium return plus a small tilt for RELATIVE optimism, sized by
# Grinold's rule alpha = IC x sigma_xs x z (Grinold & Kahn, Active Portfolio
# Management, ch. 10): about +/-0.8% a year per standard deviation, never the
# raw +20% a target can imply.
IC_PRIOR = 0.02  # between the volatility-neutral (0.013) and raw (0.033) results
SIGMA_XS = 0.42  # cross-sectional sd of 12-month returns, S&P 500 2015-25
UPSIDE_MEAN = 0.11  # long-run mean ln(target / price), US large caps 2021-25
UPSIDE_SD = 0.125  # its cross-sectional sd (stable month to month: 0.12-0.13)
SHRINK_N0 = 10  # the holdings' own mean counts as this many universe names
Z_CAP = 3.0


def relative_alpha(upsides: dict[str, float], ic: float | None = None) -> tuple[dict, dict]:
    """Per-symbol alpha (annual, excess over equilibrium) from each holding's
    target upside (T / P - 1). The reference mean blends the holdings' own
    mean with the long-run universe mean: the universe's average upside swings
    from 2% to 31% with the market, which a fixed constant would misread as
    every holding being relatively bullish or bearish."""
    ic = IC_PRIOR if ic is None else ic
    u = {s: math.log1p(v) for s, v in upsides.items() if v is not None and v > -0.95}
    if not u:
        return {}, {"ic": ic, "ref_upside": None}
    n = len(u)
    ref = (sum(u.values()) + SHRINK_N0 * UPSIDE_MEAN) / (n + SHRINK_N0)
    out = {}
    for s, v in u.items():
        z = max(-Z_CAP, min(Z_CAP, (v - ref) / UPSIDE_SD))
        out[s] = {"alpha": ic * SIGMA_XS * z, "z": z}
    return out, {"ic": ic, "ref_upside": math.expm1(ref), "sigma_xs": SIGMA_XS}


# ---------------------------------------------------------------------------
# Our own point-in-time record (state/target_history.jsonl) and its track record
# ---------------------------------------------------------------------------
# Yahoo's per-firm history is thin before 2020 and every backtest on today's
# index is survivorship-biased. The app therefore keeps its own dated record:
# one line per (day, symbol) the first time a block is computed that day,
# scored once the price 12 months on is known. Market data only (no holdings
# sizes), so nothing about the owner's book is in it.
_HIST_LOCK = threading.Lock()
_SEEN: set[tuple[str, str]] = set()
HIST_MAX_DAYS = 3 * 365
HORIZON_DAYS = 365
MIN_NAMES = 8  # a day's cross-section needs this many names to rank
MIN_WEEKS = 52  # weeks with an outcome before a verdict


def _hist_path():
    from convexity import paths

    return paths.state_dir() / "target_history.jsonl"


def record(symbol: str, price: float, block: dict) -> None:
    """Append today's block for ``symbol`` (once per day per symbol)."""
    day = dt.date.today().isoformat()
    key = (day, (symbol or "").upper())
    if key in _SEEN or not block.get("mean"):
        return
    line = {
        "d": day,
        "s": key[1],
        "p": round(float(price), 6),
        "mean": round(float(block["mean"]), 6),
        "median": round(float(block["median"]), 6) if block.get("median") else None,
        "n": block.get("n"),
        "src": block.get("source"),
        "age": block.get("age_days"),
    }
    try:
        with _HIST_LOCK:
            if key in _SEEN:
                return
            path = _hist_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
            _SEEN.add(key)
    except OSError as e:
        print(f"[targets] history: {type(e).__name__}")


def load_history() -> list[dict]:
    """The record, one line per (day, symbol) (the last wins), malformed
    lines skipped, older than HIST_MAX_DAYS dropped."""
    cutoff = (dt.date.today() - dt.timedelta(days=HIST_MAX_DAYS)).isoformat()
    out: dict[tuple, dict] = {}
    try:
        text = _hist_path().read_text(encoding="utf-8")
    except OSError:
        return []
    for ln in text.splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if (
            isinstance(r, dict)
            and r.get("d", "") >= cutoff
            and _num(r.get("p"))
            and _num(r.get("mean"))
        ):
            out[(r["d"], r["s"])] = r
    return list(out.values())


def _nw_t(x: list[float], lag: int) -> float | None:
    n = len(x)
    if n < 5:
        return None
    m = sum(x) / n
    e = [v - m for v in x]
    var = sum(v * v for v in e) / n
    for k in range(1, min(lag, n - 1) + 1):
        var += 2 * (1 - k / (lag + 1)) * sum(e[i] * e[i - k] for i in range(k, n)) / n
    return m / math.sqrt(var / n) if var > 0 else None


def _spearman(a: list[float], b: list[float]) -> float | None:
    import pandas as pd

    if len(a) < 3:
        return None
    r = pd.Series(a).corr(pd.Series(b), method="spearman")
    return float(r) if r == r else None


def track_record(closes=None) -> dict:
    """How well relative target upside has ranked the next 12 months, on the
    app's own record. Weekly cross-sections (the last recorded day of each
    week with >= MIN_NAMES names), Spearman IC of ln(T/P) minus that day's
    mean against the realised 12-month log return, Newey-West t with lag 51
    (52 overlapping weeks). ``closes`` ({symbol: pandas Series}) is injected
    by tests; otherwise fetched once for the symbols that need an outcome."""
    hist = load_history()
    today = dt.date.today()
    matured = [r for r in hist if (today - dt.date.fromisoformat(r["d"])).days >= HORIZON_DAYS]
    first = min((r["d"] for r in hist), default=None)
    base = {
        "n_records": len(hist),
        "n_symbols": len({r["s"] for r in hist}),
        "since": first,
        "first_outcome": (
            dt.date.fromisoformat(first) + dt.timedelta(days=HORIZON_DAYS)
        ).isoformat()
        if first
        else None,
        "ic_prior": IC_PRIOR,
    }
    weeks: dict[tuple, dict[str, dict]] = {}
    for r in matured:
        d = dt.date.fromisoformat(r["d"])
        wk = d.isocalendar()[:2]
        cur = weeks.setdefault(wk, {})
        if r["s"] not in cur or cur[r["s"]]["d"] < r["d"]:
            cur[r["s"]] = r
    if matured and closes is None:
        closes = _fetch_closes({r["s"] for r in matured}, min(r["d"] for r in matured))
    ics: list[float] = []
    for wk in sorted(weeks):
        rows = list(weeks[wk].values())
        pts = []
        for r in rows:
            ser = (closes or {}).get(r["s"])
            if ser is None or len(ser) == 0:
                continue
            end = dt.date.fromisoformat(r["d"]) + dt.timedelta(days=HORIZON_DAYS)
            later = ser[ser.index.date <= end]
            # The close at the horizon, not the last one Yahoo had: a listing
            # whose prices stop early is skipped, never scored on a stale price.
            if not len(later) or (end - later.index[-1].date()).days > 10:
                continue
            p1 = float(later.iloc[-1])
            if p1 > 0:
                pts.append((math.log(r["mean"] / r["p"]), math.log(p1 / r["p"])))
        if len(pts) >= MIN_NAMES:
            mu = sum(p[0] for p in pts) / len(pts)
            ic = _spearman([p[0] - mu for p in pts], [p[1] for p in pts])
            if ic is not None:
                ics.append(ic)
    n = len(ics)
    mean = sum(ics) / n if n else None
    t = _nw_t(ics, 51) if n >= 5 else None
    if n < MIN_WEEKS:
        verdict = f"Too early — {n} of {MIN_WEEKS} weeks with a 12-month outcome"
    elif t is not None and t >= 2 and mean > 0:
        verdict = "Evidence of an edge"
    elif t is not None and 1 <= t < 2 and mean > 0:
        verdict = "Weak evidence"
    else:
        verdict = "No evidence"
    out = {**base, "n_weeks": n, "ic": mean, "t": t, "ic_pos_share": (sum(1 for v in ics if v > 0) / n) if n else None,
           "verdict": verdict, "ic_series": [round(v, 4) for v in ics]}  # fmt: skip
    _cache_put("analyst_targets|track_record", {"v": out}, 6 * 3600.0)
    return out


def _fetch_closes(symbols: set[str], since: str) -> dict:
    import yfinance as yf

    out = {}
    try:
        df = yf.download(sorted(symbols), start=since, auto_adjust=False, progress=False)["Close"]
    except Exception as e:
        print(f"[targets] track record closes: {type(e).__name__}")
        return out
    if hasattr(df, "columns"):
        for s in df.columns:
            out[str(s)] = df[s].dropna()
    return out


def view_ic() -> tuple[float, str]:
    """The IC behind the optimizer's analyst tilts: the app's own measured IC
    once its track record shows evidence (>= MIN_WEEKS weeks, t >= 2), else
    the studies' prior. Reads the last computed track record only (no network
    on the optimizer's path)."""
    hit = _cache_get("analyst_targets|track_record")
    tr = hit.get("v") if hit else None
    if (
        tr
        and tr.get("n_weeks", 0) >= MIN_WEEKS
        and (tr.get("t") or 0) >= 2
        and (tr.get("ic") or 0) > 0
    ):
        return float(tr["ic"]), "measured"
    return IC_PRIOR, "prior"
