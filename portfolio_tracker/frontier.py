"""Efficient frontier orchestrator — MPT computation using the shared price pipeline."""

from __future__ import annotations

import time

import numpy as np

from portfolio_tracker import mpt
from portfolio_tracker.analytics import _bulk_close
from portfolio_tracker.cache import _cache_get, _cache_put
from portfolio_tracker.fx import _apply_fx_to_closes, _norm_ccy_for_fx

_MPT_LOOKBACK_YF = {"1Y": "1y", "3Y": "3y", "5Y": "5y", "10Y": "10y"}
_MPT_BUDGETS = {
    "fast":       {"cloud":    200_000, "frontier":  80, "label": "Fast — 200k configs (~1s)"},
    "standard":   {"cloud":  1_000_000, "frontier": 200, "label": "Standard — 1M configs (~4s)"},
    "thorough":   {"cloud":  3_500_000, "frontier": 400, "label": "Thorough — 3.5M configs (~14s)"},
    "exhaustive": {"cloud": 15_000_000, "frontier": 800, "label": "Exhaustive — 15M configs (~60s)"},
}

_RF_YF: dict[str, str] = {
    "USD": "^IRX", "EUR": "^IRX", "GBP": "^IRX", "JPY": "^IRX",
    "CHF": "^IRX", "AUD": "^IRX", "CAD": "^IRX",
}


def _risk_free_history(ccy: str, lookback: str) -> dict:
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


def compute_efficient_frontier(
    rows: list[dict],
    *,
    lookback: str = "3Y",
    frequency: str = "weekly",
    display_ccy: str = "USD",
    rf: float = 0.04,
    budget: str = "standard",
    current_weights: dict | None = None,
    diversified: bool = False,
) -> dict:
    t_total = time.perf_counter()
    lookback_u = (lookback or "3Y").upper()
    if lookback_u not in _MPT_LOOKBACK_YF:
        lookback_u = "3Y"
    period_yf = _MPT_LOOKBACK_YF[lookback_u]
    freq = (frequency or "weekly").lower()
    if freq not in mpt.FREQ_PER_YEAR:
        freq = "weekly"
    cfg = _MPT_BUDGETS.get((budget or "standard").lower(), _MPT_BUDGETS["standard"])
    display_ccy = _norm_ccy_for_fx(display_ccy or "USD")

    rows = [r for r in (rows or []) if r and r.get("symbol")]
    symbols = [str(r["symbol"]) for r in rows]
    if len(symbols) < 2:
        return {"error": "need at least 2 symbols with price history"}
    by_sym = {r["symbol"]: r for r in rows}

    t_fetch = time.perf_counter()
    closes = _bulk_close(symbols, period_yf)
    if display_ccy != "USD":
        closes = _apply_fx_to_closes(closes, by_sym, display_ccy, period_yf)
    fetch_ms = int((time.perf_counter() - t_fetch) * 1000)

    if closes is None or closes.empty:
        return {"error": "no price history available", "fetch_ms": fetch_ms}

    returns = mpt.compute_returns(closes, freq)
    if returns.shape[0] < 12 or returns.shape[1] < 2:
        return {"error": f"insufficient overlapping data ({returns.shape[0]} obs, "
                          f"{returns.shape[1]} assets)", "fetch_ms": fetch_ms}

    active = list(returns.columns)
    missing = [s for s in symbols if s not in active]

    mu, cov = mpt.annualize(returns, freq)
    cov_s = mpt.ledoit_wolf_shrink(cov, returns)

    t_opt = time.perf_counter()
    n_active = len(active)
    if diversified and n_active >= 2:
        floor = min(1.0 / n_active, max(0.005, 0.5 / n_active))
    else:
        floor = 0.0
    if floor > 0:
        turning = mpt.critical_line_with_floor(mu, cov_s, floor)
    else:
        turning = mpt.critical_line(mu, cov_s)
    curve = mpt.frontier_curve(turning, mu, cov_s, n_samples=cfg["frontier"])
    tangency = mpt.tangency_portfolio(curve, rf=rf)
    min_vol = curve[0] if curve else None
    max_ret = curve[-1] if curve else None
    anchor_W = mpt.anchor_samples(turning, n_per_segment=4)
    pert_basis = mpt.anchor_samples(turning, n_per_segment=12)
    cloud_arr = mpt.monte_carlo_cloud(
        mu, cov_s, n_samples=cfg["cloud"], rf=rf, anchor_weights=anchor_W,
        floor=floor, frontier_weights=pert_basis, perturbed_fraction=0.6,
    )
    n_samples_actual = int(cloud_arr.shape[0])
    optimize_ms = int((time.perf_counter() - t_opt) * 1000)

    t_cvar = time.perf_counter()
    cvar_alphas = [a / 100.0 for a in range(99, 49, -1)]
    cvar_frontier = mpt.cvar_curve(returns, cvar_alphas, mu, cov_s,
                                   rf=rf, floor=floor)
    cvar_ms = int((time.perf_counter() - t_cvar) * 1000)

    warnings: list[str] = []
    if curve:
        sample_idx = np.linspace(0, len(curve) - 1, 5, dtype=int)
        max_delta = 0.0
        for idx in sample_idx:
            p = curve[int(idx)]
            chk = mpt.portfolio_stats(p["weights"], mu, cov_s, rf=rf)
            d = max(abs(chk["vol"] - p["vol"]), abs(chk["ret"] - p["ret"]))
            if d > max_delta:
                max_delta = d
        if max_delta > 1e-9:
            warnings.append(f"frontier self-check delta {max_delta:.2e} > 1e-9")

    CLOUD_TRANSIT_MAX = 200_000
    if n_samples_actual > CLOUD_TRANSIT_MAX:
        rng = np.random.default_rng(42)
        keep = rng.choice(n_samples_actual, size=CLOUD_TRANSIT_MAX, replace=False)
        n_anchor = int(anchor_W.shape[0])
        if n_anchor > 0:
            anchor_idx = np.arange(n_samples_actual - n_anchor, n_samples_actual)
            keep = np.unique(np.concatenate([keep, anchor_idx]))
        cloud_arr = cloud_arr[keep]
    cloud = cloud_arr.tolist()

    eq_w = {s: 1.0 / len(active) for s in active}
    caps = {s: max(0.0, float(by_sym.get(s, {}).get("market_cap") or 0.0)) for s in active}
    cap_total = sum(caps.values())
    if cap_total > 0:
        cap_w = {s: caps[s] / cap_total for s in active}
    else:
        cap_w = dict(eq_w)
    cur_w_in = current_weights or {}
    cur_total = sum(max(0.0, float(cur_w_in.get(s, 0.0))) for s in active)
    cur_w = ({s: max(0.0, float(cur_w_in.get(s, 0.0))) / cur_total for s in active}
             if cur_total > 0 else dict(eq_w))

    def _anchor(w: dict) -> dict:
        stats = mpt.portfolio_stats(w, mu, cov_s, rf=rf)
        return {"vol": stats["vol"], "ret": stats["ret"], "sharpe": stats["sharpe"],
                "weights": w}

    return {
        "symbols": active, "missing": missing,
        "frontier": curve, "cvar_frontier": cvar_frontier,
        "tangency": tangency, "min_vol": min_vol, "max_ret": max_ret,
        "cloud": cloud,
        "anchors": {
            "equal": _anchor(eq_w), "cap": _anchor(cap_w), "current": _anchor(cur_w),
        },
        "mu":  {s: float(mu[s]) for s in active},
        "vol": {s: float(cov_s.iloc[i, i] ** 0.5) for i, s in enumerate(active)},
        "params": {
            "lookback": lookback_u, "frequency": freq, "display_ccy": display_ccy,
            "rf": rf, "budget": (budget or "standard").lower(),
            "diversified": bool(diversified), "floor": float(floor),
        },
        "meta": {
            "n_obs": int(returns.shape[0]), "n_assets": int(len(active)),
            "n_samples_target": int(cfg["cloud"]),
            "n_samples_actual": n_samples_actual,
            "cloud_transit_rows": int(len(cloud)),
            "n_anchor": int(anchor_W.shape[0]),
            "sampler_mix": "25% sparse-k · 30% Dir(0.05) · 25% Dir(0.3) · 20% Dir(1.0)",
            "fetch_ms": fetch_ms, "optimize_ms": optimize_ms,
            "cvar_ms": cvar_ms, "n_cvar": int(len(cvar_frontier)),
            "total_ms": int((time.perf_counter() - t_total) * 1000),
            "freq_label": freq, "budget_label": cfg["label"],
            "warnings": warnings,
        },
    }
