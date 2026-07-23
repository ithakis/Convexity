"""Mean-CVaR frontier orchestrator — Black-Litterman returns + CVaR risk.

Builds the JSON the Optimize tab consumes. The pipeline is:
  prices → daily FX-adjusted scenarios → annualized covariance (estimator of
  choice) → Black-Litterman posterior μ (market-cap equilibrium prior + analyst
  target views, tempered by the trust haircut) → mean-CVaR efficient frontier
  (custom numba solver in mpt.py) → anchors + a light (CVaR, return) cloud.

Nothing mean-variance survives here: no historical-mean μ, no CLA, no tangency,
no vol/return cloud. See mpt.py for the math and the module docstring there for
why the LP solver is a bespoke interior-point method rather than scipy.
"""

from __future__ import annotations

import concurrent.futures as _fut
import time

import numpy as np

from portfolio_tracker import mpt
from portfolio_tracker.analytics import _analyst_for, _bulk_close
from portfolio_tracker.cache import _cache_get, _cache_put
from portfolio_tracker.fx import (
    _apply_fx_to_closes, _fx_latest_close, _norm_ccy_for_fx, fx_rates,
)

_MPT_LOOKBACK_YF = {"1Y": "1y", "3Y": "3y", "5Y": "5y", "10Y": "10y"}
_COV_MODELS = ("sample", "ledoit", "ewma")

# Compute budgets are a **wall-clock target**, not a fixed work count. The cloud
# (visual decoration) is drawn to a fixed, modest density first — it's cheap and
# saturates visually well before it bloats the payload — then the 8-core (prange)
# bootstrap band, which is the valuable part, runs until the wall-clock target
# elapses. This adapts to portfolio size automatically (a small/fast portfolio
# gets more bootstrap replicas ⇒ a tighter band; a large one gets fewer) and makes
# the streamed pct/ETA trivially honest (pct = elapsed/target). `nf` = base-frontier
# sample points (cheap); `cloud` = shipped cloud points.
_BUDGETS = {
    "light":    {"nf": 24, "secs": 5.0,  "cloud": 20000},
    "standard": {"nf": 40, "secs": 15.0, "cloud": 40000},
    "dense":    {"nf": 60, "secs": 60.0, "cloud": 80000},
}
_DEF_BUDGET = "standard"
_BOOT_MIN, _BOOT_CAP = 24, 8000          # bootstrap replicas: floor / ceiling

_RF_YF: dict[str, str] = {
    "USD": "^IRX", "EUR": "^IRX", "GBP": "^IRX", "JPY": "^IRX",
    "CHF": "^IRX", "AUD": "^IRX", "CAD": "^IRX",
}


def _risk_free_history(ccy: str, lookback: str) -> dict:
    """13-week T-bill yield history for the rf sparkline (unchanged from MV era)."""
    cc = (ccy or "USD").upper()
    lb = (lookback or "3Y").upper()
    if lb not in _MPT_LOOKBACK_YF:
        lb = "3Y"
    cache_key = f"rfhist|{cc}|{lb}"
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit

    ticker = _RF_YF.get(cc, "^IRX")
    period_yf = _MPT_LOOKBACK_YF[lb]
    try:
        df = _bulk_close([ticker], period_yf)
    except Exception:
        df = None
    series: list[tuple[str, float]] = []
    mean_pct = None
    current_pct = None
    if df is not None and not df.empty and ticker in df.columns:
        col = df[ticker].dropna()
        for ts, val in col.items():
            try:
                series.append((ts.strftime("%Y-%m-%d"), float(val)))
            except Exception:
                continue
        if series:
            vals = [v for _, v in series]
            mean_pct = sum(vals) / len(vals)
            current_pct = vals[-1]

    note = (
        f"{ticker} (US 13-week T-bill yield)" if cc == "USD"
        else f"{ticker} proxy — no native yf series for {cc}; using US 13-week T-bill"
    )
    out = {
        "ccy": cc, "lookback": lb, "ticker": ticker,
        "series": series, "mean_pct": mean_pct, "current_pct": current_pct,
        "source_note": note,
    }
    _cache_put(cache_key, out, ttl=14400.0)
    return out


def _usd_per_unit(ccy: str, rates: dict) -> float | None:
    """USD value of one unit of ``ccy`` (for FX-normalising market caps).

    ``rates`` is fx_rates("USD")["rates"] = {ccy: units-of-ccy per 1 USD}. So the
    USD value of 1 unit of ccy is 1/rates[ccy]. Falls back to a direct
    ccyUSD=X spot for currencies outside the majors basket.
    """
    ccy = _norm_ccy_for_fx(ccy)
    if ccy == "USD":
        return 1.0
    r = rates.get(ccy)
    if r and r > 0:
        return 1.0 / float(r)
    return _fx_latest_close(ccy, "USD")


def _market_cap_weights(active: list[str], by_sym: dict) -> dict[str, float]:
    """Market-cap weights, FX-normalised to a common currency (USD).

    yfinance reports marketCap in each security's native listing currency, so raw
    caps are numerically incomparable across currencies (a ¥ cap is ~150× a $ cap
    for the same value). Convert every cap to USD before weighting, else the BL
    equilibrium prior is dominated by whichever assets happen to quote in a
    small-unit currency.
    """
    ccys = {
        _norm_ccy_for_fx(by_sym.get(s, {}).get("currency") or "USD")
        for s in active
    }
    rates: dict = {}
    if ccys - {"USD"}:
        try:
            rates = (fx_rates("USD") or {}).get("rates", {})
        except Exception:
            rates = {}
    fx_cache: dict[str, float | None] = {"USD": 1.0}
    caps: dict[str, float] = {}
    for s in active:
        cap = by_sym.get(s, {}).get("market_cap")
        try:
            cap = float(cap or 0.0)
        except (TypeError, ValueError):
            cap = 0.0
        if cap <= 0:
            caps[s] = 0.0
            continue
        ccy = _norm_ccy_for_fx(by_sym.get(s, {}).get("currency") or "USD")
        if ccy not in fx_cache:
            fx_cache[ccy] = _usd_per_unit(ccy, rates)
        rate = fx_cache[ccy]
        caps[s] = cap * float(rate) if rate else cap  # last-resort: raw cap
    return caps


def _analyst_views(active: list[str], by_sym: dict, rf: float) -> dict[str, dict]:
    """Per-asset BL views from 12-month analyst targets (currency-neutral ratios).

    view q_i (excess) = target_mean_i / price_i − 1 − rf. Confidence inputs: analyst
    count and the low/high dispersion. Assets without a usable target get no view.
    """
    views: dict[str, dict] = {}

    def _one(s: str) -> tuple[str, dict | None]:
        try:
            blk = _analyst_for(s, by_sym.get(s, {}))
        except Exception:
            return s, None
        price = blk.get("price") or by_sym.get(s, {}).get("price")
        tgt = blk.get("target_mean")
        try:
            price = float(price); tgt = float(tgt)
        except (TypeError, ValueError):
            return s, None
        if price <= 0 or tgt <= 0:
            return s, None
        q = tgt / price - 1.0 - float(rf)
        lo, hi = blk.get("target_low"), blk.get("target_high")
        disp = None
        try:
            if lo is not None and hi is not None and float(hi) >= float(lo) and tgt > 0:
                disp = (float(hi) - float(lo)) / tgt
        except (TypeError, ValueError):
            disp = None
        n = blk.get("n_analysts")
        return s, {"q": q, "n": (float(n) if n else 0.0), "disp": disp}

    workers = min(8, max(1, len(active)))
    with _fut.ThreadPoolExecutor(max_workers=workers) as pool:
        for s, v in pool.map(_one, active):
            if v is not None:
                views[s] = v
    return views


def _build_bounds(active: list[str], bounds: dict | None, w_min: float, w_max: float):
    """Per-position [min, max] weight vectors aligned to ``active``.

    ``bounds`` is the UI's ``{symbol: {"min": frac, "max": frac}}`` map. Symbols
    without an entry fall back to the global ``w_min``/``w_max``. Returns scalar
    globals when no per-position map is given (mpt broadcasts them).
    """
    if not bounds:
        return float(max(0.0, w_min)), (float(min(1.0, w_max)) if w_max else 1.0)
    lo_v, hi_v = [], []
    for s in active:
        b = bounds.get(s) or {}
        lo = b.get("min")
        hi = b.get("max")
        lo_v.append(float(lo) if lo is not None else float(max(0.0, w_min)))
        hi_v.append(float(hi) if hi is not None else (float(w_max) if w_max else 1.0))
    return lo_v, hi_v


def compute_efficient_frontier_stream(
    rows: list[dict],
    *,
    lookback: str = "3Y",
    display_ccy: str = "USD",
    rf: float = 0.04,
    alpha: float = 0.95,
    fully_invested: bool = True,
    bounds: dict | None = None,
    w_min: float = 0.0,
    w_max: float = 1.0,
    cov_model: str = "ledoit",
    haircut: float = 0.25,
    budget: str = _DEF_BUDGET,
    current_weights: dict | None = None,
    max_seconds: float | None = None,
):
    """Streaming mean-CVaR frontier. Yields ``{"type": ...}`` messages.

    Message shapes:
      ``{"type": "progress", "pct": 0-100, "eta": secs|None, "label": str}``
      ``{"type": "done", "result": {...}}``   — the full payload (same schema the
                                                 blocking wrapper returns)
      ``{"type": "error", "error": str}``

    The heavy stages (bootstrap band, cloud) are chunked so the server can report
    honest progress + ETA and stop promptly when the client disconnects/cancels
    (it simply stops pulling the generator). See §12 in CLAUDE.md.
    """
    t_total = time.perf_counter()
    budget = (budget or _DEF_BUDGET).lower()
    bud = _BUDGETS.get(budget, _BUDGETS[_DEF_BUDGET])
    # ``max_seconds`` overrides the preset's wall-clock target (tests pass 0 so
    # only the minimum bootstrap/cloud run — fast, still exercises the full path).
    nf, cloud_n = bud["nf"], bud["cloud"]
    target_s = max(0.001, float(max_seconds) if max_seconds is not None else bud["secs"])

    def _prog(label: str) -> dict:
        # pct/ETA are w.r.t. the wall-clock target, so both are literally true:
        # pct = elapsed/target, ETA = target − elapsed (never negative).
        el = time.perf_counter() - t_total
        pct = max(0.01, min(0.99, el / target_s))
        return {"type": "progress", "pct": round(pct * 100.0, 1),
                "eta": round(max(0.0, target_s - el), 1), "label": label}

    lookback_u = (lookback or "3Y").upper()
    if lookback_u not in _MPT_LOOKBACK_YF:
        lookback_u = "3Y"
    period_yf = _MPT_LOOKBACK_YF[lookback_u]
    alpha = float(min(max(alpha, 0.80), 0.995))
    cov_model = (cov_model or "ledoit").lower()
    if cov_model not in _COV_MODELS:
        cov_model = "ledoit"
    haircut = float(min(max(haircut, 0.0), 1.0))
    display_ccy = _norm_ccy_for_fx(display_ccy or "USD")

    rows = [r for r in (rows or []) if r and r.get("symbol")]
    symbols = [str(r["symbol"]) for r in rows]
    if len(symbols) < 2:
        yield {"type": "error", "error": "need at least 2 symbols with price history"}
        return
    by_sym = {r["symbol"]: r for r in rows}

    # ---- data fetch ----
    yield _prog("Fetching price history…")
    t_fetch = time.perf_counter()
    closes = _bulk_close(symbols, period_yf)
    if display_ccy != "USD" and closes is not None and not closes.empty:
        closes = _apply_fx_to_closes(closes, by_sym, display_ccy, period_yf)
    fetch_ms = int((time.perf_counter() - t_fetch) * 1000)
    if closes is None or closes.empty:
        yield {"type": "error", "error": "no price history available"}
        return
    returns = mpt.compute_returns(closes, "daily")  # DAILY scenarios
    if returns.shape[0] < 60 or returns.shape[1] < 2:
        yield {"type": "error", "error": f"insufficient overlapping data "
               f"({returns.shape[0]} daily obs, {returns.shape[1]} assets)"}
        return
    active = list(returns.columns)
    missing = [s for s in symbols if s not in active]

    # ---- Black-Litterman posterior returns ----
    yield _prog("Black-Litterman posterior returns…")
    t_bl = time.perf_counter()
    cov = mpt.annualized_cov(returns, "daily", cov_model)
    mkt_w = _market_cap_weights(active, by_sym)
    views = _analyst_views(active, by_sym, rf)
    bl = mpt.black_litterman(active, cov, mkt_w, views, rf=rf, haircut=haircut)
    mu = bl["mu"]
    bl_ms = int((time.perf_counter() - t_bl) * 1000)

    # ---- base frontier (exact weights) ----
    yield _prog("Solving mean-CVaR frontier…")
    t_opt = time.perf_counter()
    lo_b, hi_b = _build_bounds(active, bounds, w_min, w_max)
    fr = mpt.mean_cvar_frontier(
        returns, mu, alpha=alpha, w_min=lo_b, w_max=hi_b,
        fully_invested=fully_invested, rf=rf, n_points=nf, cov=cov,
    )
    if not fr.get("ok"):
        yield {"type": "error", "error": fr.get("error", "frontier failed")}
        return
    optimize_ms = int((time.perf_counter() - t_opt) * 1000)
    ctx = fr.pop("_ctx")
    frontier = fr["frontier"]
    K = len(frontier)

    # ---- (CVaR, return) cloud — fixed modest density, cheap, drawn first ----
    t_cloud = time.perf_counter()
    cloud: list = []
    g = 0
    while len(cloud) < cloud_n:
        take = min(5000, cloud_n - len(cloud))
        cloud.extend(mpt.cvar_return_cloud(returns, mu, alpha=alpha, n=take, seed=1000 + g))
        g += 1
        yield _prog(f"Sampling portfolio cloud… {len(cloud)}/{cloud_n}")
    cloud_ms = int((time.perf_counter() - t_cloud) * 1000)

    # ---- bootstrap stability band — fills the rest of the wall-clock budget ----
    # Runs until t_total + target_s (adapts to portfolio size); a minimum # of
    # replicas always runs so the band exists even if fetch ate the whole budget.
    boot_deadline = t_total + target_s
    Rv = returns.values
    boot: list = []
    per_chunk = max(4, min(400, int(650 / max(1, K))))  # ≈1 s of solves per chunk
    n_boot = 0
    while n_boot < _BOOT_CAP and (n_boot < _BOOT_MIN or time.perf_counter() < boot_deadline):
        arr = mpt.bootstrap_cvar(Rv, ctx, np.arange(n_boot + 1, n_boot + 1 + per_chunk, dtype=np.int64))
        boot.append(arr)
        n_boot += per_chunk
        yield _prog(f"Bootstrapping frontier stability… {n_boot} replicas")
    if boot:
        allb = np.vstack(boot)  # [n_boot, K]
        lo = np.percentile(allb, 10, axis=0)
        med = np.percentile(allb, 50, axis=0)
        hi = np.percentile(allb, 90, axis=0)
        for i, p in enumerate(frontier):
            p["cvar_lo"], p["cvar_med"], p["cvar_hi"] = float(lo[i]), float(med[i]), float(hi[i])

    for p in frontier:
        p.pop("_t", None)

    # ---- anchors (equal / cap / current) ----
    eq_w = {s: 1.0 / len(active) for s in active}
    cap_total = sum(mkt_w.values())
    cap_w = ({s: mkt_w[s] / cap_total for s in active} if cap_total > 0 else dict(eq_w))
    cur_in = current_weights or {}
    cur_total = sum(max(0.0, float(cur_in.get(s, 0.0))) for s in active)
    cur_w = ({s: max(0.0, float(cur_in.get(s, 0.0))) / cur_total for s in active}
             if cur_total > 0 else dict(eq_w))

    def _anchor(w: dict) -> dict:
        return mpt.portfolio_risk_metrics(w, mu, cov, returns, alpha=alpha, rf=rf,
                                          fully_invested=fully_invested)

    warnings: list[str] = []
    if returns.shape[0] < 200:
        warnings.append(f"thin history ({returns.shape[0]} daily obs) — CVaR tail is noisy")
    n_tail = int((1.0 - alpha) * returns.shape[0])
    if n_tail < 5:
        warnings.append(f"only {n_tail} scenarios in the α={alpha:.0%} tail")
    if fr.get("n_nonconv"):
        warnings.append(f"{fr['n_nonconv']} frontier point(s) hit the solver iteration cap")

    result = {
        "symbols": active, "missing": missing,
        "frontier": frontier,
        "min_cvar": fr["min_cvar"], "max_ret": fr["max_ret"],
        "cloud": cloud,
        "anchors": {"equal": _anchor(eq_w), "cap": _anchor(cap_w), "current": _anchor(cur_w)},
        "bl": {
            "mu": bl["mu"], "prior": bl["prior"], "q": bl["q"],
            "delta": bl["delta"], "tau": bl["tau"], "haircut": bl["haircut"],
            "viewed": bl["viewed"], "no_view": bl["no_view"],
        },
        "params": {
            "lookback": lookback_u, "frequency": "daily", "display_ccy": display_ccy,
            "rf": rf, "alpha": alpha, "fully_invested": bool(fully_invested),
            "cov_model": cov_model, "haircut": haircut,
            "budget": budget, "bounds": bounds or {},
        },
        "meta": {
            "n_obs": int(returns.shape[0]), "n_assets": int(len(active)),
            "n_frontier": int(K), "n_cloud": int(len(cloud)),
            "n_boot": int(n_boot), "n_viewed": int(len(bl["viewed"])),
            "fetch_ms": fetch_ms, "bl_ms": bl_ms, "optimize_ms": optimize_ms,
            "cloud_ms": cloud_ms,
            "total_ms": int((time.perf_counter() - t_total) * 1000),
            "warnings": warnings,
        },
    }
    yield {"type": "done", "result": result}


def compute_efficient_frontier(rows: list[dict], **kwargs) -> dict:
    """Blocking wrapper — drains the stream and returns the final payload.

    Kept for the test-suite and any non-streaming caller. Accepts the same kwargs
    as :func:`compute_efficient_frontier_stream` (plus the legacy ``cloud_budget``
    alias for ``budget``).
    """
    if "cloud_budget" in kwargs and "budget" not in kwargs:
        kwargs["budget"] = kwargs.pop("cloud_budget")
    kwargs.pop("cloud_budget", None)
    result: dict | None = None
    for msg in compute_efficient_frontier_stream(rows, **kwargs):
        if msg["type"] == "done":
            result = msg["result"]
        elif msg["type"] == "error":
            return {"error": msg["error"]}
    return result if result is not None else {"error": "no result produced"}
