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

import concurrent.futures as _fut
import math
import time

import numpy as np

from convexity import analyst_targets, mpt
from convexity.analytics import (
    _analyst_for,
    _bulk_close,
    _drift,
    _market_cap_usd,
    _rebalance_days,
    _when,
    extend_history,
)
from convexity.persistence import clean_trading
from convexity.cache import _cache_get, _cache_put
from convexity.fx import _apply_fx_to_closes
from convexity.helpers import _dedupe_rows_by_symbol, major_ccy

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
    "light": {"nf": 24, "secs": 5.0, "cloud": 20000},
    "standard": {"nf": 40, "secs": 15.0, "cloud": 40000},
    "dense": {"nf": 60, "secs": 60.0, "cloud": 80000},
}
_DEF_BUDGET = "standard"
_BOOT_MIN, _BOOT_CAP = 24, 8000  # bootstrap replicas: floor / ceiling

_RF_YF: dict[str, str] = {
    "USD": "^IRX",
    "EUR": "^IRX",
    "GBP": "^IRX",
    "JPY": "^IRX",
    "CHF": "^IRX",
    "AUD": "^IRX",
    "CAD": "^IRX",
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
        f"{ticker} (US 13-week T-bill yield)"
        if cc == "USD"
        else f"{ticker} proxy — no native yf series for {cc}; using US 13-week T-bill"
    )
    out = {
        "ccy": cc,
        "lookback": lb,
        "ticker": ticker,
        "series": series,
        "mean_pct": mean_pct,
        "current_pct": current_pct,
        "source_note": note,
    }
    _cache_put(cache_key, out, ttl=14400.0)
    return out


def _market_cap_weights(active: list[str], by_sym: dict) -> dict[str, float]:
    """Market caps in USD, the BL equilibrium weights (renormalised there).

    yfinance reports marketCap in each security's own currency, so raw caps
    are incomparable (a yen cap is ~150x its dollar value) and the prior would
    be dominated by whichever assets quote in a small-unit currency. The
    conversion is analytics._market_cap_usd — the same one cap-weighting,
    size buckets and the Excel export use. A cap with no FX rate counts as 0
    (the asset keeps a prior through its covariance with the others) rather
    than entering at the wrong scale.
    """
    caps: dict[str, float] = {}
    for s in active:
        v = _market_cap_usd(by_sym.get(s, {}))
        caps[s] = float(v) if v and v > 0 else 0.0
    return caps


def _analyst_views(
    active: list[str], by_sym: dict, rf: float
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Per-asset BL views + a richer analyst-detail block, in ONE fetch pass.

    Returns ``(views, detail, ref)``:
      * ``views[s]`` = ``{alpha, n, disp, age}`` — the BL inputs. A view is
        RELATIVE: the equilibrium return Π_i plus a tilt for how much more (or
        less) optimistic the analysts are about this holding than usual,
        alpha_i = IC x sigma_xs x z_i (analyst_targets.relative_alpha). Until
        2026-10-23 a view was the raw upside, q_i = T/P - 1 + dy - rf, which
        took a +20% target at face value although targets have not ranked
        12-month returns (IC ~0.01-0.03 in two point-in-time studies) and
        average ~11% of upside a year. Confidence comes from the analyst
        count, the low/high dispersion and the targets' median age.
        Assets without a usable target get no view.
      * ``ref`` = the IC used (and whether it is the prior or measured) and
        the reference upside the tilts are relative to.
      * ``detail[s]`` = ``{price, target_mean, target_low, target_high,
        upside_pct, n_analysts, disp}`` — surfaced to the per-company hover in
        the Optimize tab. Same fetch (one ``_analyst_for`` call per symbol), so
        no second network pass.
    """
    views: dict[str, dict] = {}
    detail: dict[str, dict] = {}

    def _one(s: str) -> tuple[str, dict | None, dict | None]:
        try:
            blk = _analyst_for(s, by_sym.get(s, {}))
        except Exception:
            return s, None, None
        price = blk.get("price") or by_sym.get(s, {}).get("price")
        tgt = blk.get("target_mean")
        try:
            price = float(price)
            tgt = float(tgt)
        except (TypeError, ValueError):
            return s, None, None
        if price <= 0 or tgt <= 0:
            return s, None, None
        try:
            dy = float(by_sym.get(s, {}).get("dividend_yield") or 0.0)
        except (TypeError, ValueError):
            dy = 0.0
        dy = dy if (dy == dy and 0.0 <= dy < 0.5) else 0.0  # NaN / absurd -> 0
        lo, hi = blk.get("target_low"), blk.get("target_high")
        disp = None
        try:
            if lo is not None and hi is not None and float(hi) >= float(lo) and tgt > 0:
                disp = (float(hi) - float(lo)) / tgt
        except (TypeError, ValueError):
            disp = None
        n = blk.get("n_analysts")
        det = {
            "price": price,
            "currency": blk.get("currency"),
            "target_mean": tgt,
            "target_low": (float(lo) if lo is not None else None),
            "target_high": (float(hi) if hi is not None else None),
            "upside_pct": (tgt / price - 1.0) * 100.0,
            "div_yield": dy,
            "n_analysts": (int(n) if n else 0),
            "disp": disp,
            "age_days": blk.get("target_age_days"),
        }
        view = {"n": (float(n) if n else 0.0), "disp": disp, "age": blk.get("target_age_days")}
        return s, view, det

    workers = min(8, max(1, len(active)))
    with _fut.ThreadPoolExecutor(max_workers=workers) as pool:
        for s, v, det in pool.map(_one, active):
            if v is not None:
                views[s] = v
            if det is not None:
                detail[s] = det
    # The model's settings (capm.py, Settings -> Model price): the owner may
    # fix the IC and the z cap; the window's alpha uses the same two.
    from convexity import capm
    from convexity.persistence import load_prefs

    mst = capm.settings(load_prefs())
    ic, ic_src = (mst["ic"], "set") if mst["ic"] is not None else analyst_targets.view_ic()
    # Total upside: a price target is a price, and a high-yield name's target
    # already prices in its slower growth, so the yield is added before the
    # holdings are compared.
    alphas, ref = analyst_targets.relative_alpha(
        {s: detail[s]["upside_pct"] / 100.0 + detail[s]["div_yield"] for s in views},
        ic,
        z_cap=mst["z_cap"],
    )
    for s in list(views):
        if s not in alphas:
            views.pop(s)
            continue
        views[s]["alpha"] = alphas[s]["alpha"]
        detail[s].update(view_alpha=alphas[s]["alpha"], view_z=alphas[s]["z"])
    ref["ic_source"] = ic_src
    return views, detail, ref


def _build_bounds(active: list[str], bounds: dict | None, w_min: float, w_max: float):
    """Per-position [min, max] weight vectors aligned to ``active``.

    ``bounds`` is the UI's ``{symbol: {"min": frac, "max": frac}}`` map. Symbols
    without an entry fall back to the global ``w_min``/``w_max``. Returns scalar
    globals when no per-position map is given (mpt broadcasts them).
    """
    if not bounds:
        return float(max(0.0, w_min)), (float(min(1.0, w_max)) if w_max else 1.0)

    def frac(v, default: float) -> float:
        # A blank, non-numeric or non-finite field is no bound, not a crash
        # ("could not convert string to float" reached the page once).
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        return min(max(f, 0.0), 1.0) if math.isfinite(f) else default

    lo_v, hi_v = [], []
    for s in active:
        b = bounds.get(s) if isinstance(bounds.get(s), dict) else {}
        lo = frac(b.get("min"), float(max(0.0, w_min)))
        hi = frac(b.get("max"), float(w_max) if w_max else 1.0)
        if lo > hi + 1e-12:  # the LP would just report "infeasible"
            raise ValueError(f"{s}: minimum {lo:.0%} is above its maximum {hi:.0%}")
        lo_v.append(lo)
        hi_v.append(hi)
    return lo_v, hi_v


# Rebalances a year and trading days per interval for each calendar
# (persistence.REBALANCE); "N" (buy and hold) never trades after the start.
_PER_YEAR = {"D": 252, "W": 52, "M": 12, "Q": 4, "S": 2, "Y": 1, "N": 0}
_HORIZON = {"D": 1, "W": 5, "M": 21, "Q": 63, "S": 126, "Y": 252, "N": 1}


def _drift_cost(returns, ref_w: np.ndarray, rebalance: str, tc: float) -> np.ndarray:
    """Each asset's yearly cost of keeping its weight, per unit of weight.

    Between rebalances a weight drifts by w_i (R_i - R_p) / (1 + R_p), so
    resetting it trades w_i |R_i - R_p| / (1 + R_p): linear in w_i once R_p
    is taken as the reference basket's (the BL market weights). The cost is
    tc x rebalances a year x that drift's mean over overlapping windows of one
    interval. Being linear it comes straight off mu, so the CVaR LP stays exact
    (proportional costs: Krokhmal, Palmquist & Uryasev 2002; a turnover penalty
    as regularisation: Olivares-Nadal & DeMiguel 2018); the exact drift
    backtest (_turnover) reports what each portfolio actually trades."""
    n = returns.shape[1]
    f = _PER_YEAR.get(rebalance, 0)
    if not f or tc <= 0:
        return np.zeros(n)
    g = np.log1p(returns.to_numpy(dtype=float))
    # A lookback too short for three windows of one interval (yearly on 1Y)
    # uses shorter windows, scaled up by sqrt(time): drift grows like a
    # random walk's spread, so plain short windows would under-charge.
    h = min(_HORIZON[rebalance], max(1, len(g) // 3))
    c = np.vstack([np.zeros(n), np.cumsum(g, axis=0)])
    rh = np.expm1(c[h:] - c[:-h])
    rref = rh @ ref_w
    drift = np.mean(np.abs(rh - rref[:, None]) / (1.0 + rref[:, None]), axis=0)
    return tc * f * drift * np.sqrt(_HORIZON[rebalance] / h)


def _turnover(returns, w: dict, rebalance: str, tc: float, rf: float) -> dict:
    """What a portfolio held at weights ``w`` and rebalanced on the calendar
    trades over the lookback (one-way, a year) and what that costs a year:
    the same drift and reset as the portfolio chart (analytics._drift), the
    same calendar-year annualisation. Cash left over by "allow cash" earns rf
    and drifts like any holding; moving money in or out of it costs nothing
    (the asset side of the trade pays)."""
    r = returns.to_numpy(dtype=float)
    n = r.shape[1]
    wv = np.array([float(w.get(s, 0.0)) for s in returns.columns])
    if wv.sum() < 1.0 - 1e-9:
        r = np.hstack([r, np.full((len(r), 1), rf / 252.0)])
        wv = np.append(wv, 1.0 - wv.sum())
    reset = _rebalance_days(returns.index, rebalance)
    reset[0] = True
    _, traded = _drift(r, np.broadcast_to(wv, r.shape), reset)
    traded[0] = 0.0
    years = max((returns.index[-1] - returns.index[0]).days / 365.25, 1e-9)
    cost = tc * traded[:, :n].sum(axis=1)
    return {
        "turnover_ann": float(traded.sum() / 2.0 / years),
        "cost_ann": float(1.0 - np.prod(1.0 - cost) ** (1.0 / years)),
    }


# ---- Phase 4: saved sets on the chart, the plug's point, its weight set ----

_MAX_SETS = 40  # marks per run: 0.3 + 1.5 ms each on 27 names over 3Y


def _clean_weights(w_in) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(w_in, dict):
        return out
    for s, v in w_in.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if isinstance(s, str) and math.isfinite(f) and f > 0:
            out[s.strip().upper()] = out.get(s.strip().upper(), 0.0) + f
    return out


def _mark_sets(weight_sets, active: list[str], anchor) -> dict[str, dict]:
    """Each saved set measured like an anchor (``anchor`` = this run's
    portfolio_risk_metrics). Names outside the run's ``active`` are dropped
    and the rest renormalised; such a mark is ``partial``, with the share of
    its weight it kept (``coverage``) and what it dropped."""
    out: dict[str, dict] = {}
    if not isinstance(weight_sets, dict):
        return out
    for name, w_in in list(weight_sets.items())[:_MAX_SETS]:
        if not isinstance(name, str):
            continue
        w = _clean_weights(w_in)
        total = sum(w.values())
        inside = sum(w.get(s, 0.0) for s in active)
        dropped = sorted((s for s in w if s not in active), key=lambda s: -w[s])
        if total <= 0 or inside <= 0:
            out[name] = {"error": "no weight inside this run", "partial": True,
                         "coverage": 0.0, "dropped": dropped}  # fmt: skip
            continue
        m = anchor({s: w.get(s, 0.0) / inside for s in active})
        m.update(partial=bool(dropped), coverage=inside / total, dropped=dropped)
        out[name] = m
    return out


def _held_out(current_weights, symbols: list[str], active: list[str]) -> list[dict]:
    """The portfolio's names this run leaves out (no usable history even with
    a stand-in) and their current weight: a set built from the run keeps them
    there (``set_from_point``) instead of selling them."""
    cur = _clean_weights(current_weights)
    total = sum(cur.get(s, 0.0) for s in symbols)
    if total <= 0:
        return []
    return [{"symbol": s, "weight": cur[s] / total}
            for s in symbols if s not in active and cur.get(s, 0.0) > 0]  # fmt: skip


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _cvar30(p: dict) -> float | None:
    """The displayed 30-day CVaR (the legacy annualised one for old runs)."""
    return _num(p.get("cvar30") if p.get("cvar30") is not None else p.get("cvar"))


SAME_RISK_NOTE = ("Your current weights sit above this frontier point: the run has only a "
                  "few points, and this is the nearest one at or below your current risk, "
                  "with a little less expected return.")  # fmt: skip
SAME_RISK_FALLBACK_NOTE = ("No point of the frontier is as safe as your current weights: "
                           "this is the lowest-risk point.")  # fmt: skip


def pick_points(frontier: list[dict], current: dict | None) -> dict:
    """The optimizer plug's two points, one rule here rather than in the page.

    Both are chosen by ``cvar``, the daily CVaR (annualised) the frontier is
    optimal for, never the displayed ``cvar30``: the 30-day figure is another
    estimator, and by it the current weights could sit above the drawn
    frontier, so a "same risk" pick by cvar30 had less return than now (1Y on
    a real 27-name book: 5.7% against 6.5%; by cvar: 6.7%).

    ``same_risk``: the highest-return point whose ``cvar`` is no worse than the
    current weights'. When the current set is inside the feasible set that
    point returns at least as much, up to the frontier's grid: if the grid
    still leaves it below the current return, it is the nearest point at or
    below the current risk and carries a ``note``. None qualifying: the
    lowest-risk point (``fallback``, with a note). ``min_cvar``: the
    lowest-``cvar`` point. Max return per CVaR is not offered: on a real book
    it was a 100% single-stock corner, and the ratio is flat over most of the
    frontier (ill-conditioned). Indexes into ``frontier``."""
    pts = [(i, _num(p.get("cvar")), p.get("ret")) for i, p in enumerate(frontier or [])]
    pts = [(i, c, r) for i, c, r in pts if c is not None and isinstance(r, int | float)]
    if not pts:
        return {"same_risk": None, "min_cvar": None}
    lo = min(pts, key=lambda t: (t[1], -t[2]))[0]
    cur = current or {}
    risk, ret = _num(cur.get("cvar")), _num(cur.get("ret"))
    ok = [t for t in pts if risk is not None and t[1] <= risk + 1e-12]
    same = max(ok, key=lambda t: (t[2], -t[1]))[0] if ok else lo
    pick = {"index": same, "fallback": not ok, "current_cvar30": _cvar30(cur)}
    if not ok:
        pick["note"] = SAME_RISK_FALLBACK_NOTE
    elif ret is not None and frontier[same]["ret"] < ret - 1e-12:
        pick["note"] = SAME_RISK_NOTE
    return {"same_risk": pick, "min_cvar": {"index": lo}}


_PICK_LABEL = {"same_risk": "same risk", "min_cvar": "lowest risk", "selected": "selected"}


def set_from_point(point_weights, held_out: list[dict] | None) -> dict[str, float]:
    """A frontier point as a weight set: under 0.1% to 0, renormalised, the
    run's left-out names kept at their current weight (the optimised part
    scaled to 1 − kept), rounded to 0.01% with the rounding residue on the
    largest weight so the set sums to exactly 1."""
    w = {s: v for s, v in _clean_weights(point_weights).items() if v >= 0.001}
    tot = sum(w.values())
    if tot <= 0:
        raise ValueError("the point has no weights")
    kept = {x["symbol"]: float(x["weight"]) for x in held_out or []
            if isinstance(x, dict) and isinstance(x.get("symbol"), str) and x["symbol"] not in w
            and isinstance(x.get("weight"), int | float) and 0 < x["weight"] < 1}  # fmt: skip
    k = sum(kept.values())
    if k >= 1:
        kept, k = {}, 0.0
    out = {s: v / tot * (1.0 - k) for s, v in w.items()} | kept
    out = {s: round(v, 4) for s, v in out.items() if round(v, 4) > 0}
    big = max(out, key=out.get)
    out[big] = round(out[big] + 1.0 - sum(out.values()), 4)
    return out


def set_name(rule: str, taken, today=None) -> str:
    """ "Optimized · same risk · 13 Nov", then " (2)", " (3)"… if taken."""
    from datetime import date

    d = today or date.today()
    base = f"Optimized · {_PICK_LABEL.get(rule, 'selected')} · {d.day} {d:%b}"
    taken = set(taken)
    name, n = base, 2
    while name in taken:
        name, n = f"{base} ({n})", n + 1
    return name


PLUG_DEFAULTS = {"lookback": "3Y", "alpha": 0.95, "rf": 0.045, "fully_invested": True,
                 "cov_model": "ledoit", "haircut": 0.25, "bounds": {}}  # fmt: skip


def plug_params(runs: list[dict] | None) -> dict:
    """The optimizer plug's settings: the portfolio's newest saved run's,
    cleaned (old runs carry ``frequency``, ``diversified`` and budgets that no
    longer exist), else the app's defaults; always the light budget. The
    rebalancing calendar and cost come from the page's saved trading setting."""
    out = {**PLUG_DEFAULTS, "bounds": {}}
    src = runs[0].get("params") if runs and isinstance(runs[0], dict) else None
    src = src if isinstance(src, dict) else {}

    def num(k, lo, hi):
        try:
            f = float(src.get(k))
        except (TypeError, ValueError):
            return
        if math.isfinite(f) and lo <= f <= hi:
            out[k] = f

    if str(src.get("lookback") or "").upper() in _MPT_LOOKBACK_YF:
        out["lookback"] = str(src["lookback"]).upper()
    num("alpha", 0.80, 0.995)
    num("rf", 0.0, 0.2)
    num("haircut", 0.0, 1.0)
    if isinstance(src.get("fully_invested"), bool):
        out["fully_invested"] = src["fully_invested"]
    if str(src.get("cov_model") or "").lower() in _COV_MODELS:
        out["cov_model"] = str(src["cov_model"]).lower()
    if isinstance(src.get("bounds"), dict):
        out["bounds"] = src["bounds"]
    out["budget"] = "light"
    return out


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
    rebalance: str = "D",
    tc_bps: float = 0.0,
    max_seconds: float | None = None,
    weight_sets: dict | None = None,
):
    """Streaming mean-CVaR frontier. Yields ``{"type": ...}`` messages.

    ``weight_sets`` ({name: {symbol: weight}}, the portfolio's saved sets) are
    marked on the chart: each gets the anchors' figures (``sets``), measured
    with this run's μ, Σ, scenarios, α, rf and cash rule (Phase 4).

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
        return {
            "type": "progress",
            "pct": round(pct * 100.0, 1),
            "eta": round(max(0.0, target_s - el), 1),
            "label": label,
        }

    lookback_u = (lookback or "3Y").upper()
    if lookback_u not in _MPT_LOOKBACK_YF:
        lookback_u = "3Y"
    period_yf = _MPT_LOOKBACK_YF[lookback_u]
    alpha = float(min(max(alpha, 0.80), 0.995))
    cov_model = (cov_model or "ledoit").lower()
    if cov_model not in _COV_MODELS:
        cov_model = "ledoit"
    haircut = float(min(max(haircut, 0.0), 1.0))
    display_ccy = major_ccy(display_ccy or "USD")
    rebalance, tc = clean_trading(rebalance, tc_bps)

    # Deduped before the 2-symbol gate: two rows for one ticker would pass it
    # and then fail later with a misleading "1 assets" data error (see
    # helpers._dedupe_rows_by_symbol for why repeats reach us at all).
    rows = _dedupe_rows_by_symbol([r for r in (rows or []) if r and r.get("symbol")])
    symbols = [str(r["symbol"]) for r in rows]
    if len(symbols) < 2:
        yield {"type": "error", "error": "need at least 2 symbols with price history"}
        return
    by_sym = {r["symbol"]: r for r in rows}

    # ---- data fetch ----
    yield _prog("Fetching price history…")
    t_fetch = time.perf_counter()
    closes = _bulk_close(symbols, period_yf)
    # A short listing borrows the same company's longer line before its own
    # first close, exactly as the portfolio chart does (analytics.extend_history):
    # without it a young or gapped secondary line (a Swiss company's Xetra line
    # had no closes for five years) failed compute_returns' coverage rule and was
    # silently left out of the run, so a weight set from it sold all of it.
    stand_ins: dict[str, dict] = {}
    if closes is not None and not closes.empty:

        def _fetch(syms, period, retry=True):  # through this module's name: tests stub it
            return _bulk_close(syms, period) if retry else _bulk_close(syms, period, retry=False)

        try:
            closes, proxied = extend_history(
                closes, symbols, {s: by_sym[s].get("currency") for s in symbols},
                {s: by_sym[s].get("name") for s in symbols}, period_yf, fetch=_fetch,
            )  # fmt: skip
            stand_ins = {s: {"proxy": p, "since": since} for s, (p, since) in proxied.items()}
        except Exception as e:  # a stand-in is extra: never lose the run over one
            print(f"[frontier] stand-ins skipped: {type(e).__name__}")
    if display_ccy != "USD" and closes is not None and not closes.empty:
        closes = _apply_fx_to_closes(closes, by_sym, display_ccy, period_yf)
    fetch_ms = int((time.perf_counter() - t_fetch) * 1000)
    if closes is None or closes.empty:
        yield {"type": "error", "error": "no price history available"}
        return
    returns = mpt.compute_returns(closes, "daily")  # DAILY scenarios
    if returns.shape[0] < 60 or returns.shape[1] < 2:
        yield {
            "type": "error",
            "error": f"insufficient overlapping data "
            f"({returns.shape[0]} daily obs, {returns.shape[1]} assets)",
        }
        return
    active = list(returns.columns)
    missing = [s for s in symbols if s not in active]
    # A stand-in that still left its line short of the coverage rule (a company
    # listed 20 months ago, on a 3Y lookback) didn't make it in: not a stand-in.
    stand_ins = {s: x for s, x in stand_ins.items() if s in active}

    # ---- Black-Litterman posterior returns ----
    yield _prog("Black-Litterman posterior returns…")
    t_bl = time.perf_counter()
    cov = mpt.annualized_cov(returns, "daily", cov_model)
    mkt_w = _market_cap_weights(active, by_sym)
    views, analyst_detail, view_ref = _analyst_views(active, by_sym, rf)
    bl = mpt.black_litterman(active, cov, mkt_w, views, rf=rf, haircut=haircut)
    # Everything below (frontier, cloud, anchors) optimizes and reports the
    # return net of the rebalancing cost (_drift_cost); bl.mu stays gross.
    mw = np.array([mkt_w.get(s, 0.0) for s in active])
    ref_w = mw / mw.sum() if mw.sum() > 0 else np.full(len(active), 1.0 / len(active))
    cost_v = _drift_cost(returns, ref_w, rebalance, tc)
    mu = {s: bl["mu"][s] - float(cost_v[i]) for i, s in enumerate(active)}
    bl_ms = int((time.perf_counter() - t_bl) * 1000)

    # ---- base frontier (exact weights) ----
    yield _prog("Solving mean-CVaR frontier…")
    t_opt = time.perf_counter()
    try:
        lo_b, hi_b = _build_bounds(active, bounds, w_min, w_max)
    except ValueError as e:
        yield {"type": "error", "error": str(e)}
        return
    fr = mpt.mean_cvar_frontier(
        returns,
        mu,
        alpha=alpha,
        w_min=lo_b,
        w_max=hi_b,
        fully_invested=fully_invested,
        rf=rf,
        n_points=nf,
        cov=cov,
    )
    if not fr.get("ok"):
        yield {"type": "error", "error": fr.get("error", "frontier failed")}
        return
    optimize_ms = int((time.perf_counter() - t_opt) * 1000)
    ctx = fr.pop("_ctx")
    frontier = fr["frontier"]
    K = len(frontier)
    # _t (internal target-return level) is not shipped — drop it before the point
    # dicts leave this function (they go out in both the frontier and done messages).
    for p in frontier:
        p.pop("_t", None)

    # ---- anchors (equal / cap / current) — computed up front so the early
    # frontier message can carry them (they don't depend on cloud/bootstrap) ----
    eq_w = {s: 1.0 / len(active) for s in active}
    cap_total = sum(mkt_w.values())
    cap_w = {s: mkt_w[s] / cap_total for s in active} if cap_total > 0 else dict(eq_w)
    cur_in = current_weights or {}
    cur_total = sum(max(0.0, float(cur_in.get(s, 0.0))) for s in active)
    cur_w = (
        {s: max(0.0, float(cur_in.get(s, 0.0))) / cur_total for s in active}
        if cur_total > 0
        else dict(eq_w)
    )

    def _anchor(w: dict) -> dict:
        return mpt.portfolio_risk_metrics(
            w, mu, cov, returns, alpha=alpha, rf=rf, fully_invested=fully_invested
        )

    anchors = {"equal": _anchor(eq_w), "cap": _anchor(cap_w), "current": _anchor(cur_w)}
    sets = _mark_sets(weight_sets, active, _anchor)
    held_out = _held_out(cur_in, symbols, active)
    # What each portfolio would trade, and moving to it from the current
    # weights. The optimizer ranks on a cost linearised around the market
    # basket (_drift_cost); "ret_net" swaps that estimate for this exact
    # backtest, so a concentrated portfolio that hardly drifts isn't shown
    # paying for trades it never makes. (min_cvar / max_ret are usually the
    # frontier's end points: the same dicts, done once.)
    seen: set[int] = set()
    marked = [m for m in sets.values() if "weights" in m]
    for p in [*frontier, *anchors.values(), fr["min_cvar"], fr["max_ret"], *marked]:
        if id(p) in seen:
            continue
        seen.add(id(p))
        p.update(_turnover(returns, p["weights"], rebalance, tc, rf))
        est = sum(float(cost_v[i]) * p["weights"].get(s, 0.0) for i, s in enumerate(active))
        p["ret_net"] = p["ret"] + est - p["cost_ann"]
        p["switch_cost"] = tc * sum(abs(p["weights"].get(s, 0.0) - cur_w[s]) for s in active)
    bl_payload = {
        "mu": bl["mu"],
        "prior": bl["prior"],
        "q": bl["q"],
        "delta": bl["delta"],
        "tau": bl["tau"],
        "haircut": bl["haircut"],
        "viewed": bl["viewed"],
        "no_view": bl["no_view"],
        "view_ref": view_ref,
        "cost": {s: float(cost_v[i]) for i, s in enumerate(active)},
    }
    params_payload = {
        "lookback": lookback_u,
        "frequency": "daily",
        "display_ccy": display_ccy,
        "rf": rf,
        "alpha": alpha,
        "fully_invested": bool(fully_invested),
        "cov_model": cov_model,
        "haircut": haircut,
        "budget": budget,
        "bounds": bounds or {},
        "rebalance": rebalance,
        "tc_bps": round(tc * 1e4, 6),
    }
    picks = pick_points(frontier, anchors["current"])
    p4 = {"sets": sets, "picks": picks, "held_out": held_out, "stand_ins": stand_ins}

    # Ship the frontier + anchors NOW so the client can fix the axis domain and
    # draw axes + the frontier line while the cloud streams in beneath it. The
    # bootstrap band (cvar30_lo/med/hi) is added later and re-shipped in `done`.
    yield {
        "type": "frontier",
        "symbols": active,
        "missing": missing,
        "frontier": frontier,
        "min_cvar": fr["min_cvar"],
        "max_ret": fr["max_ret"],
        "anchors": anchors,
        **p4,
        "bl": bl_payload,
        "params": params_payload,
        "meta": {"n_frontier": int(K), "n_cloud": int(cloud_n), "n_viewed": int(len(bl["viewed"]))},
    }

    # ---- (CVaR30, return) cloud — streamed in chunks so it fills the scatter
    # visibly during compute (client draws each chunk as it arrives) ----
    t_cloud = time.perf_counter()
    cloud: list = []
    g = 0
    while len(cloud) < cloud_n:
        take = min(5000, cloud_n - len(cloud))
        # Same box / cash rule / rf the frontier was solved under — the cloud is
        # the achievable set the frontier has to be the upper-left envelope of,
        # so sampling a wider one puts most of the scatter outside the feasible
        # region and leaves the frontier floating in the middle of it.
        pts = mpt.cvar_return_cloud(
            returns,
            mu,
            alpha=alpha,
            n=take,
            seed=1000 + g,
            w_min=lo_b,
            w_max=hi_b,
            fully_invested=fully_invested,
            rf=rf,
        )
        cloud.extend(pts)
        g += 1
        yield {"type": "cloud", "points": pts}
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
        arr = mpt.bootstrap_cvar(
            Rv, ctx, np.arange(n_boot + 1, n_boot + 1 + per_chunk, dtype=np.int64)
        )
        boot.append(arr)
        n_boot += per_chunk
        yield _prog(f"Bootstrapping frontier stability… {n_boot} replicas")
    if boot:
        allb = np.vstack(boot)  # [n_boot, K] — daily-annualized CVaR per replica
        lo = np.percentile(allb, 10, axis=0)
        med = np.percentile(allb, 50, axis=0)
        hi = np.percentile(allb, 90, axis=0)
        for i, p in enumerate(frontier):
            p["cvar_lo"], p["cvar_med"], p["cvar_hi"] = float(lo[i]), float(med[i]), float(hi[i])
            # The band is computed on DAILY CVaR (i.i.d. bootstrap resampling
            # destroys the time-ordering the overlapping-10-day estimator needs),
            # so we transfer only its *relative* width onto the point's own 30-day
            # CVaR: cvar30_{lo,hi} = cvar30 · (band_{lo,hi} / band_med). This is the
            # one deliberate approximation in the risk display.
            c30 = float(p.get("cvar30") or 0.0)
            m_i = float(med[i])
            if m_i > 0 and c30 == c30:  # med>0 and cvar30 not NaN
                p["cvar30_lo"] = c30 * float(lo[i]) / m_i
                p["cvar30_med"] = c30
                p["cvar30_hi"] = c30 * float(hi[i]) / m_i
            else:
                p["cvar30_lo"] = p["cvar30_med"] = p["cvar30_hi"] = c30

    warnings: list[str] = []
    if returns.shape[0] < 200:
        warnings.append(f"thin history ({returns.shape[0]} daily obs) — CVaR tail is noisy")
    n_tail = int((1.0 - alpha) * returns.shape[0])
    if n_tail < 5:
        warnings.append(f"only {n_tail} scenarios in the α={alpha:.0%} tail")
    if fr.get("n_nonconv"):
        warnings.append(f"{fr['n_nonconv']} frontier point(s) hit the solver iteration cap")
    for s, x in stand_ins.items():
        name = by_sym.get(s, {}).get("name") or s
        warnings.append(f"{name} ({s}): before {_when(x['since'])} the prices are {x['proxy']}'s, "
                        "the same company on another exchange.")  # fmt: skip

    result = {
        "symbols": active,
        "missing": missing,
        "frontier": frontier,
        "min_cvar": fr["min_cvar"],
        "max_ret": fr["max_ret"],
        # Cloud was already streamed as `cloud` chunk messages — don't re-ship the
        # 20-80k points here. The blocking wrapper reassembles them from the chunks.
        "cloud": [],
        "anchors": anchors,
        **p4,
        "asset_stats": mpt.asset_risk_stats(returns, alpha),
        "analyst_detail": analyst_detail,
        "bl": bl_payload,
        "params": params_payload,
        "meta": {
            "n_nonconv": int(fr.get("n_nonconv") or 0),
            "n_obs": int(returns.shape[0]),
            "n_assets": int(len(active)),
            "n_frontier": int(K),
            "n_cloud": int(len(cloud)),
            "n_boot": int(n_boot),
            "n_viewed": int(len(bl["viewed"])),
            "fetch_ms": fetch_ms,
            "bl_ms": bl_ms,
            "optimize_ms": optimize_ms,
            "cloud_ms": cloud_ms,
            "total_ms": int((time.perf_counter() - t_total) * 1000),
            "warnings": warnings,
        },
    }
    yield {"type": "done", "result": result}


def compute_efficient_frontier(rows: list[dict], **kwargs) -> dict:
    """Blocking wrapper — drains the stream and returns the final payload.

    Kept for the test-suite and any non-streaming caller. Accepts the same kwargs
    as :func:`compute_efficient_frontier_stream`.
    """
    result: dict | None = None
    cloud_acc: list = []
    for msg in compute_efficient_frontier_stream(rows, **kwargs):
        t = msg["type"]
        if t == "cloud":  # reassemble the streamed chunks
            cloud_acc.extend(msg.get("points") or [])
        elif t == "done":
            result = msg["result"]
        elif t == "error":
            return {"error": msg["error"]}
    if result is not None and not result.get("cloud"):
        result["cloud"] = cloud_acc  # so non-streaming callers/tests see it
    return result if result is not None else {"error": "no result produced"}
