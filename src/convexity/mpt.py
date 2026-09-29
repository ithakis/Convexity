"""
mpt.py — Portfolio-optimization primitives: Black-Litterman expected returns
+ a mean-CVaR efficient frontier.

This module replaces the old Markowitz mean-variance engine (Critical Line
Algorithm, tangency portfolio, vol/return Monte-Carlo cloud) with two pieces:

1. **Return engine — Black-Litterman.** The optimizer's expected-return vector is
   the BL posterior, NOT a historical mean. The prior is the market-implied
   equilibrium (reverse optimization, Π = δ·Σ·w_mkt) and the views are absolute
   per-asset returns implied by 12-month analyst price targets, tempered by a
   global analyst-trust haircut. `black_litterman()` returns the posterior μ and
   a breakdown for the UI.

2. **Risk engine — CVaR on daily scenarios.** Risk is Conditional Value-at-Risk
   estimated directly on the empirical daily return scenarios (~750 over 3Y),
   NOT a parametric variance. The mean-CVaR efficient frontier

       maximise  μ_BLᵀw   subject to   CVaR_α(w) ≤ c ,  w ∈ box ,  Σw = 1

   is traced by sweeping the return floor and, at each point, minimising CVaR via
   the Rockafellar-Uryasev linear program. That LP is solved by a **custom
   numba-JIT primal-dual interior-point method** (`_cvar_pdip`) that exploits the
   LP's structure: the T scenario-slack variables form a diagonal block that is
   eliminated analytically each Newton step, collapsing the linear system to an
   (N+1)×(N+1) dense solve (N = #assets ≤ ~60). No scipy on the hot path.

   A scipy/HiGHS reference solver for the same LP lives in the test-suite only
   (`tests/_cvar_reference.py`) and is used to certify `_cvar_pdip` to 1e-6.

Public surface (used by `frontier.py`):
    compute_returns(closes_df, freq)                 -> returns_df
    annualized_cov(returns_df, freq, model)          -> cov_df   (sample/ledoit/ewma)
    ledoit_wolf_shrink(cov, returns=None)            -> cov_df
    black_litterman(...)                             -> dict (posterior μ + breakdown)
    mean_cvar_frontier(returns_df, mu, alpha, ...)   -> dict (frontier + endpoints)
    portfolio_risk_metrics(weights, mu, cov, returns, alpha=0.95, rf=0.04,
                           fully_invested=True)      -> dict {ret, cvar, vol, mdd, cdar}
    cvar_return_cloud(returns_df, mu, alpha, n)      -> list of [cvar_30day, ret_ann]

Designed for ~2–60 assets and a 10–20 s wall budget per call (data fetch
dominates; the optimization math here is well under 100 ms).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from numba import njit, prange

FREQ_PER_YEAR = {"daily": 252, "weekly": 52, "monthly": 12}
_FREQ_RESAMPLE = {"daily": None, "weekly": "W-FRI", "monthly": "M"}
TRADING_DAYS = 252

# FRTB-style tail-risk display horizons (trading days). Displayed VaR/CVaR are
# computed empirically on *overlapping* LIQ_HORIZON-day returns (the FRTB
# liquidity-horizon convention — captures autocorrelation/fat tails that a
# √-time rescale of the daily number would miss), then scaled to DISP_HORIZON
# by square-root-of-time (√(DISP/LIQ) = √3). These affect *display only* — the
# frontier is still optimized on the daily CVaR LP. Retune here in one place.
LIQ_HORIZON = 10
DISP_HORIZON = 30


# ---------------------------------------------------------------------------
# Returns + covariance
# ---------------------------------------------------------------------------


def compute_returns(closes: pd.DataFrame, freq: str = "daily") -> pd.DataFrame:
    """Resample close prices to the requested frequency and return pct-change.

    Drops the first row (NaN) and any column with insufficient overlap. The
    mean-CVaR path uses ``freq="daily"`` so the scenario set is the raw daily
    returns; the BL covariance can be estimated at any supported frequency.
    """
    freq = freq.lower()
    if freq not in FREQ_PER_YEAR:
        raise ValueError(f"unknown freq {freq!r}; expected daily/weekly/monthly")
    rule = _FREQ_RESAMPLE[freq]
    px = closes.sort_index()
    if rule is not None:
        px = px.resample(rule).last()
    rets = px.pct_change().dropna(how="all")
    # Require at least 60% coverage per column before we drop remaining NaN rows.
    min_obs = max(8, int(0.6 * len(rets)))
    keep = [c for c in rets.columns if rets[c].notna().sum() >= min_obs]
    rets = rets[keep].dropna()
    return rets


def _sample_cov(returns: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Annualized sample covariance."""
    n = FREQ_PER_YEAR[freq.lower()]
    return returns.cov() * n


def _ewma_cov(returns: pd.DataFrame, freq: str, halflife: float = 60.0) -> pd.DataFrame:
    """Annualized exponentially-weighted covariance (RiskMetrics-style).

    Recent observations weigh more (``halflife`` in periods). Uses a mean-centred
    weighted cross-product; annualized by periods-per-year so it is directly
    comparable to the sample estimator and usable as the BL prior covariance.
    """
    n = FREQ_PER_YEAR[freq.lower()]
    x = returns.values.astype(float)
    t = x.shape[0]
    if t < 2:
        return returns.cov() * n
    lam = 0.5 ** (1.0 / max(1e-9, float(halflife)))
    # Weights newest→oldest, normalised to sum 1.
    w = lam ** np.arange(t - 1, -1, -1)
    w = w / w.sum()
    mean = (w[:, None] * x).sum(axis=0)
    xc = x - mean
    cov = (xc * w[:, None]).T @ xc
    # Symmetrize (guards against tiny fp asymmetry) and annualize.
    cov = 0.5 * (cov + cov.T) * n
    return pd.DataFrame(cov, index=returns.columns, columns=returns.columns)


def ledoit_wolf_shrink(cov: pd.DataFrame, returns: pd.DataFrame | None = None) -> pd.DataFrame:
    """Ledoit-Wolf (2004) shrinkage toward the constant-correlation target.

    Target F: every pair gets the average sample correlation r̄, each asset
    keeps its own variance. Shrunk Σ = δ·F + (1−δ)·S with the optimal
    intensity δ = max(0, min(1, κ/T)), κ = (π − ρ)/γ, estimated as in
    Ledoit & Wolf, "Honey, I Shrunk the Sample Covariance Matrix" (J. Portfolio
    Management, 2004) and their reference code (covCor.m):

      π = Σ_ij π_ij,  π_ij = (1/T) Σ_t (x_it x_jt − s_ij)²   (noise in S)
      ρ = Σ_i π_ii + r̄ Σ_{i≠j} √(s_jj/s_ii) θ_ij,
          θ_ij = (1/T) Σ_t x_it³ x_jt − s_ii s_ij               (F's own noise)
      γ = ‖F − S‖²_F                                           (misspecification)

    The ρ term matters: F is itself estimated from the same data, and leaving
    ρ out (as this function did before 2026-09-29) overstates δ. The moments
    use the 1/T sample covariance of the paper; the returned matrix shrinks
    the ``cov`` passed in. Without ``returns`` a fixed δ = 0.2 is used.
    """
    s = cov.values.astype(float)
    n = s.shape[0]
    if n < 2:
        return cov.copy()
    var = np.diag(s)
    std = np.sqrt(np.clip(var, 1e-18, None))
    corr = s / np.outer(std, std)
    mask = ~np.eye(n, dtype=bool)
    r_bar = float(corr[mask].mean()) if mask.any() else 0.0
    target = r_bar * np.outer(std, std)
    np.fill_diagonal(target, var)
    if returns is None or len(returns) < 4:
        alpha = 0.2
    else:
        x = returns.values.astype(float)
        x = x - x.mean(axis=0, keepdims=True)
        t = x.shape[0]
        sample = x.T @ x / t
        v = np.diag(sample)
        sd = np.sqrt(np.clip(v, 1e-30, None))
        c = sample / np.outer(sd, sd)
        rb = float(c[mask].mean())
        prior = rb * np.outer(sd, sd)
        np.fill_diagonal(prior, v)
        y = x * x
        phi_mat = y.T @ y / t - sample**2
        phi = float(phi_mat.sum())
        theta = (x**3).T @ x / t - v[:, None] * sample
        np.fill_diagonal(theta, 0.0)
        rho = float(np.trace(phi_mat) + rb * ((sd[None, :] / sd[:, None]) * theta).sum())
        gamma = float(((sample - prior) ** 2).sum())
        alpha = float(np.clip((phi - rho) / gamma / t, 0.0, 1.0)) if gamma > 0 else 1.0
    shrunk = alpha * target + (1.0 - alpha) * s
    return pd.DataFrame(shrunk, index=cov.index, columns=cov.columns)


def annualized_cov(returns: pd.DataFrame, freq: str, model: str = "ledoit") -> pd.DataFrame:
    """Annualized covariance via the selected estimator.

    ``model`` ∈ {"sample", "ledoit" (default), "ewma"}. Ledoit-Wolf is the
    default because with 15–60 assets the raw sample covariance is noisy and the
    BL prior Π = δ·Σ·w_mkt inherits that noise directly.
    """
    m = (model or "ledoit").lower()
    if m == "sample":
        return _sample_cov(returns, freq)
    if m == "ewma":
        return _ewma_cov(returns, freq)
    # default: Ledoit-Wolf. Shrink in PER-PERIOD units so the data-driven
    # intensity (which compares the sample-cov variance against ‖S−target‖ using
    # the raw `returns`) is unit-consistent, THEN annualize. Shrinking an already
    # ×252 covariance while estimating intensity from raw daily returns mixes
    # units and collapses the shrinkage to ≈0 (defeats the estimator).
    n = FREQ_PER_YEAR[freq.lower()]
    return ledoit_wolf_shrink(returns.cov(), returns) * n


# ---------------------------------------------------------------------------
# Black-Litterman
# ---------------------------------------------------------------------------
#
# All quantities are in *excess-return* space (over rf) until the final step,
# where rf is added back so callers see total expected returns. Working in
# excess space is the standard BL convention and keeps δ, Π and the views on the
# same footing.


def black_litterman(
    symbols: list[str],
    cov: pd.DataFrame,
    mkt_weights: dict[str, float],
    views: dict[str, dict],
    *,
    rf: float = 0.04,
    tau: float = 0.05,
    haircut: float = 0.5,
    risk_premium: float = 0.05,
    k0: float = 5.0,
    d0: float = 0.25,
) -> dict:
    """Black-Litterman posterior expected returns.

    Parameters
    ----------
    symbols       : asset order (aligns cov / weights / views).
    cov           : annualized covariance Σ (aligned to ``symbols``).
    mkt_weights   : market-cap weights w_mkt (any positive scaling; renormalized).
    views         : ``{sym: {"q": excess_view_return, "n": n_analysts,
                    "disp": (hi-lo)/tgt}}`` — one absolute view per covered asset.
                    Assets absent here get no view (fall back to the prior via Σ).
    rf            : annual risk-free rate (added back at the end).
    tau           : prior-uncertainty scalar (τΣ).
    haircut       : analyst-trust H ∈ [0,1]. 0 ⇒ ignore analysts (posterior→prior),
                    1 ⇒ full trust (posterior→views). Scales Ω by (1-H)/H.
    risk_premium  : target market risk premium used to calibrate δ.
    k0, d0        : view-confidence knobs (count saturation / dispersion scale).

    Returns
    -------
    dict with ``mu`` (posterior *total* annual return per symbol), ``prior``
    (equilibrium total return rf+Π), ``q`` (per-symbol view or None), ``delta``,
    ``tau``, ``haircut``, ``viewed``, ``no_view``.
    """
    n = len(symbols)
    Sig = cov.reindex(index=symbols, columns=symbols).values.astype(float)

    # Market-cap weights, renormalized over the active set.
    w = np.array([max(0.0, float(mkt_weights.get(s, 0.0))) for s in symbols], dtype=float)
    tot = w.sum()
    w = w / tot if tot > 0 else np.full(n, 1.0 / n)

    # δ from a target market risk premium: premium = δ·σ²_mkt ⇒ δ = premium/σ²_mkt.
    var_mkt = float(w @ Sig @ w)
    delta = (risk_premium / var_mkt) if var_mkt > 1e-12 else 2.5
    delta = float(min(max(delta, 0.5), 50.0))  # keep sane
    pi = delta * (Sig @ w)  # equilibrium excess returns (prior mean)

    # Assemble the views: P = identity rows for covered assets.
    H = float(min(max(haircut, 0.0), 1.0))
    q_map: dict[str, float | None] = {s: None for s in symbols}
    viewed: list[str] = []
    if H > 1e-6 and views:
        rows, qs, omegas = [], [], []
        for i, s in enumerate(symbols):
            v = views.get(s)
            if not v:
                continue
            q = v.get("q")
            if q is None or not math.isfinite(float(q)):
                continue
            nn = float(v.get("n") or 0.0)
            disp = v.get("disp")
            disp = float(disp) if (disp is not None and math.isfinite(float(disp))) else 0.5
            # Per-asset confidence: more analysts + tighter dispersion ⇒ tighter view.
            n_conf = nn / (nn + k0) if nn > 0 else 0.15
            disp_conf = 1.0 / (1.0 + max(0.0, disp) / max(1e-9, d0))
            c_i = max(1e-3, n_conf * disp_conf)
            base = tau * Sig[i, i]
            # Ω tightens as H→1 and as confidence rises; (1-H)/H → 0 at full trust.
            omega_i = base / c_i * ((1.0 - H) / H)
            rows.append(i)
            qs.append(float(q))
            omegas.append(max(omega_i, 1e-12))
            q_map[s] = float(q)
            viewed.append(s)
        if rows:
            idx = np.array(rows, dtype=np.int64)
            Q = np.array(qs, dtype=float)
            Omega = np.diag(np.array(omegas, dtype=float))
            tS = tau * Sig
            tSP = tS[:, idx]  # τΣ Pᵀ  (n×k)
            PtSP = tS[np.ix_(idx, idx)]  # P τΣ Pᵀ (k×k)
            k = idx.size
            A = PtSP + Omega + 1e-12 * np.eye(k)
            adj = tSP @ np.linalg.solve(A, Q - pi[idx])
            post = pi + adj
        else:
            post = pi
    else:
        post = pi

    mu_total = {s: float(rf + post[i]) for i, s in enumerate(symbols)}
    prior_total = {s: float(rf + pi[i]) for i, s in enumerate(symbols)}
    return {
        "mu": mu_total,
        "prior": prior_total,
        "q": {s: (float(rf + q_map[s]) if q_map[s] is not None else None) for s in symbols},
        "delta": float(delta),
        "tau": float(tau),
        "haircut": float(H),
        "viewed": viewed,
        "no_view": [s for s in symbols if s not in viewed],
    }


# ---------------------------------------------------------------------------
# CVaR / drawdown helpers (empirical, on a portfolio's scenario returns)
# ---------------------------------------------------------------------------


def _tail_count(t: int, level: float) -> int:
    """Number of observations in the worst (1-level) tail = ceil((1-level)·t).

    The ``- 1e-9`` guards against floating-point dust: e.g. (1-0.95)·300 evaluates
    to 15.000000000000013, whose naive ceil is 16 rather than the intended 15.
    """
    return max(1, int(math.ceil((1.0 - level) * t - 1e-9)))


def cvar_of(port_ret: np.ndarray, alpha: float) -> float:
    """Empirical CVaR_α of a portfolio return series (positive = expected loss).

    CVaR = mean of the worst (1-α) fraction of losses. Loss = -return.
    """
    r = np.asarray(port_ret, dtype=float)
    r = r[np.isfinite(r)]
    t = r.size
    if t == 0:
        return float("nan")
    k = _tail_count(t, alpha)
    worst = np.sort(r)[:k]  # k smallest returns (largest losses)
    return float(-worst.mean())


def max_drawdown(port_ret: np.ndarray) -> float:
    """Maximum drawdown (positive fraction) of the compounded return path."""
    r = np.asarray(port_ret, dtype=float)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return float("nan")
    curve = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(curve)
    dd = 1.0 - curve / peak
    return float(dd.max()) if dd.size else 0.0


def cdar(port_ret: np.ndarray, beta: float = 0.95) -> float:
    """Conditional Drawdown-at-Risk: mean of the worst (1-β) drawdowns."""
    r = np.asarray(port_ret, dtype=float)
    r = r[np.isfinite(r)]
    if r.size == 0:
        return float("nan")
    curve = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(curve)
    dd = 1.0 - curve / peak
    k = _tail_count(dd.size, beta)
    worst = np.sort(dd)[::-1][:k]  # k largest drawdowns
    return float(worst.mean())


def overlapping_h_returns(port_ret: np.ndarray, h: int) -> np.ndarray:
    """Overlapping ``h``-day **compounded** returns from a time-ordered series.

    window_i = ∏_{t=i}^{i+h-1}(1+r_t) − 1, for i = 0 … T−h (so T−h+1 windows).
    Computed via a cumulative-product ratio (O(T), not O(T·h)). Requires the
    input to be time-ordered — do NOT feed a bootstrap-resampled series here.
    """
    r = np.asarray(port_ret, dtype=float)
    r = r[np.isfinite(r)]
    t = r.size
    if h < 1 or t == 0:
        return np.empty(0, dtype=float)
    if t < h:
        # Degenerate (fewer obs than the horizon): fall back to the single
        # whole-period compounded return. Matches the numba cloud kernel's
        # degenerate branch exactly so cloud and frontier CVaR never disagree.
        # Unreachable in the real pipeline (≥60-obs floor upstream).
        return np.array([float(np.prod(1.0 + r) - 1.0)], dtype=float)
    g = np.cumprod(1.0 + r)  # g[i] = ∏_{0..i}(1+r)
    num = g[h - 1 :]  # ∏_{0..i+h-1}, length T−h+1
    prev = np.empty(t - h + 1, dtype=float)
    prev[0] = 1.0  # first window has no prior product
    prev[1:] = g[: t - h]  # g[i-1] for i ≥ 1
    return num / prev - 1.0


def var_cvar_horizon(
    port_ret: np.ndarray, alpha: float, h_base: int = LIQ_HORIZON, h_target: int = DISP_HORIZON
) -> tuple[float, float]:
    """Empirical (VaR, CVaR) on overlapping ``h_base``-day returns, √-scaled to ``h_target``.

    Both are positive expected-loss fractions at confidence ``alpha``. VaR is the
    tail boundary (the α-quantile loss); CVaR is the mean of the worst (1−α) tail
    — exactly the ``cvar_of`` convention, via the shared ``_tail_count``. Scaled
    by √(h_target/h_base). Returns ``(nan, nan)`` when there is too little data.
    """
    rh = overlapping_h_returns(port_ret, h_base)
    if rh.size == 0:
        return float("nan"), float("nan")
    k = _tail_count(rh.size, alpha)
    worst = np.sort(rh)[:k]  # k smallest returns = k largest losses
    scale = math.sqrt(float(h_target) / float(h_base))
    cvar = float(-worst.mean() * scale)
    var = float(-worst[-1] * scale)  # least-extreme of the tail = the quantile
    return var, cvar


def asset_risk_stats(returns: pd.DataFrame, alpha: float) -> dict[str, dict]:
    """Per-asset realized return + 30-day VaR/CVaR from the daily returns matrix.

    Cheap (one pass per column, no refetch — the caller already has ``returns``).
    ``ret_total`` = cumulative return over the window; ``ret_ann`` = its annualized
    equivalent; ``var30``/``cvar30`` via :func:`var_cvar_horizon`. Feeds the
    per-company hover tooltip in the Optimize tab.
    """
    out: dict[str, dict] = {}
    for s in returns.columns:
        col = np.asarray(returns[s].values, dtype=float)
        col = col[np.isfinite(col)]
        if col.size == 0:
            continue
        total = float(np.prod(1.0 + col) - 1.0)
        # 1+total = ∏(1+r) ≥ 0 (a daily return can't be < −1), so the fractional
        # power is real; a wiped-out asset (total ≤ −1) annualizes to −100%.
        ann = float((1.0 + total) ** (TRADING_DAYS / col.size) - 1.0) if total > -1.0 else -1.0
        # Guard against a huge gain on a very short series blowing up the exponent
        # (e.g. 1.5**252 → inf); surface NaN rather than a garbage tooltip number.
        if not math.isfinite(ann):
            ann = float("nan")
        v30, c30 = var_cvar_horizon(col, alpha)
        out[s] = {"ret_ann": ann, "ret_total": total, "var30": v30, "cvar30": c30}
    return out


# ---------------------------------------------------------------------------
# Mean-CVaR solver — numba-JIT primal-dual interior-point method (Mehrotra)
# ---------------------------------------------------------------------------
#
# LP (min-CVaR at confidence α, with an optional return floor):
#
#   variables : w ∈ R^N (weights), ζ ∈ R (VaR), u ∈ R^T_+ (scenario slack)
#   minimise  : ζ + κ·Σ_t u_t                         κ = 1/((1-α)T)
#   s.t.      : u_t + R_t·w + ζ ≥ 0     (t=1..T)       scenario
#               a_ret·w ≥ b_ret                        return floor (inactive if b_ret=-inf)
#               l ≤ w ≤ h                              box
#               Σw = 1        (fully-invested)   OR    Σw ≤ 1 (cash allowed)
#
# At the optimum ζ*=VaR_α and the objective equals CVaR_α (daily units).
#
# We solve with an infeasible-start Mehrotra predictor-corrector. The Newton
# system's u-block is diagonal, so u is eliminated by a Schur complement and each
# iteration reduces to a dense (N+1)×(N+1) [+1 border row for the Σw=1 equality]
# solve — the T-sized work stays as two matmuls (R and Rᵀ). See the module
# docstring for why this beats calling scipy.optimize.linprog per frontier point.
#
# Inequality groups (slack s = q - Gz ≥ 0, dual λ ≥ 0):
#   L : w ≥ l           s_L = w - l          D1 = λL/sL
#   U : w ≤ h           s_U = h - w          D2 = λU/sU
#   Nn: u ≥ 0           s_N = u              D3 = λN/sN
#   Rt: a_ret·w ≥ b_ret s_R = a_ret·w-b_ret  D4 = λR/sR
#   S : Rw+ζ+u ≥ 0      s_S = Rw+ζ+u         D5 = λS/sS
#   C : Σw ≤ 1  (cash)  s_C = 1 - Σw         D6 = λC/sC   (only if not fully_invested)


@njit(cache=True, fastmath=True)
def _cvar_pdip(R, a_ret, b_ret, l, h, kappa, fully_invested, max_iter):
    """Solve one mean-CVaR LP. Returns (w, zeta, cvar, converged).

    All inputs are float64 arrays / scalars. ``fully_invested`` is 1 (Σw=1) or 0
    (Σw≤1). ``b_ret = -1e18`` disables the return floor.
    """
    T = R.shape[0]
    N = R.shape[1]
    nx = N + 1  # x = (w, zeta)

    # ---- starting point (interior, infeasible allowed) ----
    w = np.empty(N)
    for i in range(N):
        w[i] = (
            min(max(1.0 / N, l[i] + 1e-3), h[i] - 1e-3)
            if h[i] - l[i] > 2e-3
            else 0.5 * (l[i] + h[i])
        )
    zeta = 0.0
    # portfolio scenario returns at start
    u = np.empty(T)
    for t in range(T):
        rt = 0.0
        for i in range(N):
            rt += R[t, i] * w[i]
        u[t] = abs(rt) + 1.0  # ensures s_S = rt+zeta+u > 0 and u > 0

    # slacks
    sL = np.empty(N)
    sU = np.empty(N)
    for i in range(N):
        sL[i] = max(w[i] - l[i], 1e-6)
        sU[i] = max(h[i] - w[i], 1e-6)
    sN = np.empty(T)
    sS = np.empty(T)
    for t in range(T):
        sN[t] = max(u[t], 1e-6)
        rt = 0.0
        for i in range(N):
            rt += R[t, i] * w[i]
        sS[t] = max(rt + zeta + u[t], 1e-6)
    aw = 0.0
    for i in range(N):
        aw += a_ret[i] * w[i]
    sR = max(aw - b_ret, 1e-6)
    sumw = 0.0
    for i in range(N):
        sumw += w[i]
    sC = max(1.0 - sumw, 1e-6)

    # duals: balance the initial complementarity products (λ_i·s_i ≈ 1) instead
    # of λ=1. This also neutralises a disabled return floor (s_R≈1e18 ⇒ λ_R≈1e-18
    # ⇒ product ≈ 1, so it never pollutes the duality measure) and typically
    # halves iterations vs a flat λ=1 start.
    lL = np.empty(N)
    lU = np.empty(N)
    for i in range(N):
        lL[i] = 1.0 / sL[i]
        lU[i] = 1.0 / sU[i]
    lN = np.empty(T)
    lS = np.empty(T)
    for t in range(T):
        lN[t] = 1.0 / sN[t]
        lS[t] = 1.0 / sS[t]
    lR = 1.0 / sR
    lC = 1.0 / sC
    y = 0.0  # equality dual (budget)

    m = 2 * N + 2 * T + 1 + (0 if fully_invested == 1 else 1)  # #inequalities

    conv = 0
    for _ in range(max_iter):
        # ---- duality measure ----
        comp = 0.0
        for i in range(N):
            comp += sL[i] * lL[i] + sU[i] * lU[i]
        for t in range(T):
            comp += sN[t] * lN[t] + sS[t] * lS[t]
        comp += sR * lR
        if fully_invested == 0:
            comp += sC * lC
        mu = comp / m

        # 1e-9 duality gap ⇒ objective accurate to well past the 1e-6 bar the
        # HiGHS cross-check enforces. Tighter (1e-10) makes degenerate near-cash
        # vertices spuriously hit the iteration cap without improving the answer.
        if mu < 1e-9:
            conv = 1
            break

        # ---- diagonal D = λ/s per group ----
        D1 = lL / sL
        D2 = lU / sU
        D3 = lN / sN
        D5 = lS / sS
        D4 = lR / sR
        D6 = lC / sC

        # ---- primal residuals r_p = Gz + s - q (per group) ----
        # For s defined independently we track r_p to drive feasibility.
        # r_p_L = (-w + sL) - (-l) = sL - (w - l)
        rpL = np.empty(N)
        rpU = np.empty(N)
        for i in range(N):
            rpL[i] = sL[i] - (w[i] - l[i])
            rpU[i] = sU[i] - (h[i] - w[i])
        rpN = np.empty(T)
        rpS = np.empty(T)
        for t in range(T):
            rt = 0.0
            for i in range(N):
                rt += R[t, i] * w[i]
            rpN[t] = sN[t] - u[t]
            rpS[t] = sS[t] - (rt + zeta + u[t])
        aw = 0.0
        sumw = 0.0
        for i in range(N):
            aw += a_ret[i] * w[i]
            sumw += w[i]
        rpR = sR - (aw - b_ret)
        rpC = sC - (1.0 - sumw)
        rb = sumw - 1.0  # equality residual (budget), only used if fully_invested

        # ---- dual residual r_d = c + Gᵀλ + Bᵀy ----
        # c: c_w=0, c_zeta=1, c_u=kappa
        rSsum = 0.0
        for t in range(T):
            rSsum += lS[t]
        rdw = np.empty(N)
        for i in range(N):
            v = 0.0
            # groups touching w: L(-I), U(+I), R(-a_ret), S(-R), C(+1 if cash)
            v += -lL[i] + lU[i] - lR * a_ret[i]
            for t in range(T):
                v += -R[t, i] * lS[t]
            if fully_invested == 0:
                v += lC
            else:
                v += y  # Bᵀy, budget column = 1
            rdw[i] = v
        rdz = 1.0 - rSsum
        rdu = np.empty(T)
        for t in range(T):
            rdu[t] = kappa - lN[t] - lS[t]

        # ================= Mehrotra: affine predictor =================
        # r_c = λ∘s (σ=0). rhs uses (r_c - λ∘r_p)/s and r_d.
        # Build reduced (x=(w,zeta)) system twice (affine, then corrector);
        # H is identical, only rhs differs.

        # g_t after u-elimination: g = D3*D5/(D3+D5)
        g = np.empty(T)
        for t in range(T):
            g[t] = D3[t] * D5[t] / (D3[t] + D5[t])

        # ---- build S_xx (nx×nx) once ----
        Sxx = np.zeros((nx, nx))
        for i in range(N):
            Sxx[i, i] += D1[i] + D2[i]
        # return rank-1: D4 * a_ret a_retᵀ
        for i in range(N):
            di = D4 * a_ret[i]
            for j in range(N):
                Sxx[i, j] += di * a_ret[j]
        # cash rank-1: D6 * 1 1ᵀ
        if fully_invested == 0:
            for i in range(N):
                for j in range(N):
                    Sxx[i, j] += D6
        # scenario contribution g_t (R_t;1)(R_t;1)ᵀ  via matmuls
        Rg = np.empty((T, N))
        for t in range(T):
            for i in range(N):
                Rg[t, i] = g[t] * R[t, i]
        RtGR = R.T @ Rg  # N×N
        RtG = np.empty(N)  # Σ_t g_t R_t
        gsum = 0.0
        for t in range(T):
            gsum += g[t]
        for i in range(N):
            acc = 0.0
            for t in range(T):
                acc += Rg[t, i]
            RtG[i] = acc
        for i in range(N):
            for j in range(N):
                Sxx[i, j] += RtGR[i, j]
            Sxx[i, N] += RtG[i]
            Sxx[N, i] += RtG[i]
        Sxx[N, N] += gsum

        # augmented KKT matrix M (with budget border if fully invested)
        if fully_invested == 1:
            nk = nx + 1
        else:
            nk = nx
        M = np.zeros((nk, nk))
        for i in range(nx):
            for j in range(nx):
                M[i, j] = Sxx[i, j]
        if fully_invested == 1:
            for i in range(N):
                M[i, nx] = 1.0
                M[nx, i] = 1.0

        # helper values reused by both solves
        # e_t coefficient for reducing rhs_u: E = D5/(D3+D5)
        Ecoef = np.empty(T)
        for t in range(T):
            Ecoef[t] = D5[t] / (D3[t] + D5[t])

        # ================= affine predictor (sigma=0) =================
        # tv_* = (r_c - lam*rp)/s  with r_c = lam*s  ->  tv = lam - lam*rp/s
        tvL = np.empty(N)
        tvU = np.empty(N)
        for i in range(N):
            tvL[i] = lL[i] - lL[i] * rpL[i] / sL[i]
            tvU[i] = lU[i] - lU[i] * rpU[i] / sU[i]
        tvN = np.empty(T)
        tvS = np.empty(T)
        for t in range(T):
            tvN[t] = lN[t] - lN[t] * rpN[t] / sN[t]
            tvS[t] = lS[t] - lS[t] * rpS[t] / sS[t]
        tvR = lR - lR * rpR / sR
        tvC = lC - lC * rpC / sC

        dw_a = np.empty(N)
        dz_a = np.empty(1)
        du_a = np.empty(T)
        dy_a = np.empty(1)
        _solve_reduced(
            M,
            nx,
            nk,
            N,
            T,
            fully_invested,
            rdw,
            rdz,
            rdu,
            rb,
            tvL,
            tvU,
            tvN,
            tvS,
            tvR,
            tvC,
            a_ret,
            R,
            D5,
            Ecoef,
            dw_a,
            dz_a,
            du_a,
            dy_a,
        )

        # recover affine Ds, Dlam per group. Ds = -rp - G*Dz ; complementarity
        # (sigma=0): Lam*Ds + S*Dlam = -(lam*s)  ->  Dlam = -lam - lam*Ds/s.
        dsL_a = np.empty(N)
        dsU_a = np.empty(N)
        dlL_a = np.empty(N)
        dlU_a = np.empty(N)
        dlN_a = np.empty(T)
        dlS_a = np.empty(T)
        du_af = du_a
        dz_af = dz_a[0]
        for i in range(N):
            dsL = -rpL[i] - (-dw_a[i])
            dsU = -rpU[i] - dw_a[i]
            dsL_a[i] = dsL
            dsU_a[i] = dsU
            dlL_a[i] = -lL[i] - lL[i] * dsL / sL[i]
            dlU_a[i] = -lU[i] - lU[i] * dsU / sU[i]
        dsN_a = np.empty(T)
        dsS_a = np.empty(T)
        for t in range(T):
            rdw_t = 0.0
            for i in range(N):
                rdw_t += R[t, i] * dw_a[i]
            gzN = -du_af[t]
            gzS = -(rdw_t + dz_af + du_af[t])
            dsN = -rpN[t] - gzN
            dsS = -rpS[t] - gzS
            dsN_a[t] = dsN
            dsS_a[t] = dsS
            dlN_a[t] = -lN[t] - lN[t] * dsN / sN[t]
            dlS_a[t] = -lS[t] - lS[t] * dsS / sS[t]
        adw_r = 0.0
        sumdw = 0.0
        for i in range(N):
            adw_r += a_ret[i] * dw_a[i]
            sumdw += dw_a[i]
        gzR = -adw_r
        dsR_a = -rpR - gzR
        dlR_a = -lR - lR * dsR_a / sR
        gzC = sumdw
        dsC_a = -rpC - gzC
        dlC_a = -lC - lC * dsC_a / sC

        # affine step length (fraction to boundary on s,λ ≥ 0)
        a_aff = 1.0
        a_aff = _ratio(a_aff, sL, dsL_a)
        a_aff = _ratio(a_aff, sU, dsU_a)
        a_aff = _ratio(a_aff, sN, dsN_a)
        a_aff = _ratio(a_aff, sS, dsS_a)
        a_aff = _ratio(a_aff, lL, dlL_a)
        a_aff = _ratio(a_aff, lU, dlU_a)
        a_aff = _ratio(a_aff, lN, dlN_a)
        a_aff = _ratio(a_aff, lS, dlS_a)
        a_aff = _ratio1(a_aff, sR, dsR_a)
        a_aff = _ratio1(a_aff, lR, dlR_a)
        if fully_invested == 0:
            a_aff = _ratio1(a_aff, sC, dsC_a)
            a_aff = _ratio1(a_aff, lC, dlC_a)

        # mu_aff
        comp_aff = 0.0
        for i in range(N):
            comp_aff += (sL[i] + a_aff * dsL_a[i]) * (lL[i] + a_aff * dlL_a[i])
            comp_aff += (sU[i] + a_aff * dsU_a[i]) * (lU[i] + a_aff * dlU_a[i])
        for t in range(T):
            comp_aff += (sN[t] + a_aff * dsN_a[t]) * (lN[t] + a_aff * dlN_a[t])
            comp_aff += (sS[t] + a_aff * dsS_a[t]) * (lS[t] + a_aff * dlS_a[t])
        comp_aff += (sR + a_aff * dsR_a) * (lR + a_aff * dlR_a)
        if fully_invested == 0:
            comp_aff += (sC + a_aff * dsC_a) * (lC + a_aff * dlC_a)
        mu_aff = comp_aff / m
        sigma = (mu_aff / mu) ** 3
        if sigma > 1.0:
            sigma = 1.0
        sig_mu = sigma * mu

        # ================= corrector solve =================
        # r_c = λ s - sig_mu + Δs_aff∘Δλ_aff ; tv = (r_c - λ rp)/s
        for i in range(N):
            rcL = lL[i] * sL[i] - sig_mu + dsL_a[i] * dlL_a[i]
            rcU = lU[i] * sU[i] - sig_mu + dsU_a[i] * dlU_a[i]
            tvL[i] = (rcL - lL[i] * rpL[i]) / sL[i]
            tvU[i] = (rcU - lU[i] * rpU[i]) / sU[i]
        for t in range(T):
            rcN = lN[t] * sN[t] - sig_mu + dsN_a[t] * dlN_a[t]
            rcS = lS[t] * sS[t] - sig_mu + dsS_a[t] * dlS_a[t]
            tvN[t] = (rcN - lN[t] * rpN[t]) / sN[t]
            tvS[t] = (rcS - lS[t] * rpS[t]) / sS[t]
        rcR = lR * sR - sig_mu + dsR_a * dlR_a
        tvR = (rcR - lR * rpR) / sR
        rcC = lC * sC - sig_mu + dsC_a * dlC_a
        tvC = (rcC - lC * rpC) / sC

        dw = np.empty(N)
        dz = np.empty(1)
        du = np.empty(T)
        dyv = np.empty(1)
        _solve_reduced(
            M,
            nx,
            nk,
            N,
            T,
            fully_invested,
            rdw,
            rdz,
            rdu,
            rb,
            tvL,
            tvU,
            tvN,
            tvS,
            tvR,
            tvC,
            a_ret,
            R,
            D5,
            Ecoef,
            dw,
            dz,
            du,
            dyv,
        )

        # recover ds, dl for corrector
        dsL = np.empty(N)
        dsU = np.empty(N)
        dlL = np.empty(N)
        dlU = np.empty(N)
        for i in range(N):
            gzl = -dw[i]
            gzu = dw[i]
            dsL[i] = -rpL[i] - gzl
            dsU[i] = -rpU[i] - gzu
            rcL = lL[i] * sL[i] - sig_mu + dsL_a[i] * dlL_a[i]
            rcU = lU[i] * sU[i] - sig_mu + dsU_a[i] * dlU_a[i]
            dlL[i] = (-rcL - lL[i] * dsL[i]) / sL[i]
            dlU[i] = (-rcU - lU[i] * dsU[i]) / sU[i]
        dsN = np.empty(T)
        dsS = np.empty(T)
        dlN = np.empty(T)
        dlS = np.empty(T)
        dz0 = dz[0]
        for t in range(T):
            rdw_t = 0.0
            for i in range(N):
                rdw_t += R[t, i] * dw[i]
            gzN = -du[t]
            gzS = -(rdw_t + dz0 + du[t])
            dsN[t] = -rpN[t] - gzN
            dsS[t] = -rpS[t] - gzS
            rcN = lN[t] * sN[t] - sig_mu + dsN_a[t] * dlN_a[t]
            rcS = lS[t] * sS[t] - sig_mu + dsS_a[t] * dlS_a[t]
            dlN[t] = (-rcN - lN[t] * dsN[t]) / sN[t]
            dlS[t] = (-rcS - lS[t] * dsS[t]) / sS[t]
        adw_r = 0.0
        sumdw = 0.0
        for i in range(N):
            adw_r += a_ret[i] * dw[i]
            sumdw += dw[i]
        dsR = -rpR - (-adw_r)
        rcR = lR * sR - sig_mu + dsR_a * dlR_a
        dlR = (-rcR - lR * dsR) / sR
        dsC = -rpC - sumdw
        rcC = lC * sC - sig_mu + dsC_a * dlC_a
        dlC = (-rcC - lC * dsC) / sC

        # step length (fraction to boundary, η=0.95)
        eta = 0.95
        a_p = 1.0
        a_p = _ratio(a_p, sL, dsL)
        a_p = _ratio(a_p, sU, dsU)
        a_p = _ratio(a_p, sN, dsN)
        a_p = _ratio(a_p, sS, dsS)
        a_p = _ratio(a_p, lL, dlL)
        a_p = _ratio(a_p, lU, dlU)
        a_p = _ratio(a_p, lN, dlN)
        a_p = _ratio(a_p, lS, dlS)
        a_p = _ratio1(a_p, sR, dsR)
        a_p = _ratio1(a_p, lR, dlR)
        if fully_invested == 0:
            a_p = _ratio1(a_p, sC, dsC)
            a_p = _ratio1(a_p, lC, dlC)
        step = eta * a_p
        if step > 1.0:
            step = 1.0

        # ---- update ----
        for i in range(N):
            w[i] += step * dw[i]
            sL[i] += step * dsL[i]
            sU[i] += step * dsU[i]
            lL[i] += step * dlL[i]
            lU[i] += step * dlU[i]
        zeta += step * dz0
        for t in range(T):
            u[t] += step * du[t]
            sN[t] += step * dsN[t]
            sS[t] += step * dsS[t]
            lN[t] += step * dlN[t]
            lS[t] += step * dlS[t]
        sR += step * dsR
        lR += step * dlR
        if fully_invested == 0:
            sC += step * dsC
            lC += step * dlC
        else:
            y += step * dyv[0]

    # objective = zeta + kappa * sum(max(0, -(R w + zeta))) — recompute cleanly
    cvar = zeta
    for t in range(T):
        rt = 0.0
        for i in range(N):
            rt += R[t, i] * w[i]
        loss = -(rt + zeta)
        if loss > 0.0:
            cvar += kappa * loss
    # clip tiny negatives on weights and (if fully invested) renormalize gently
    for i in range(N):
        if w[i] < l[i]:
            w[i] = l[i]
        elif w[i] > h[i]:
            w[i] = h[i]
    return w, zeta, cvar, conv


@njit(cache=True, fastmath=True)
def _ratio(alpha, s, ds):
    """Fraction-to-boundary over an array: max α s.t. s+α ds ≥ 0."""
    n = s.shape[0]
    for i in range(n):
        if ds[i] < 0.0:
            r = -s[i] / ds[i]
            if r < alpha:
                alpha = r
    return alpha


@njit(cache=True, fastmath=True)
def _ratio1(alpha, s, ds):
    """Fraction-to-boundary for a scalar."""
    if ds < 0.0:
        r = -s / ds
        if r < alpha:
            alpha = r
    return alpha


@njit(cache=True, fastmath=True)
def _solve_reduced(
    M,
    nx,
    nk,
    N,
    T,
    fully_invested,
    rdw,
    rdz,
    rdu,
    rb,
    tvL,
    tvU,
    tvN,
    tvS,
    tvR,
    tvC,
    a_ret,
    R,
    D5,
    Ecoef,
    dw_out,
    dz_out,
    du_out,
    dy_out,
):
    """Solve the reduced KKT system for (Δw, Δζ, Δu, Δy).

    rhs_z = -r_d + Gᵀ((r_c - λ∘r_p)/s) = -r_d + Gᵀ(tv). Then u eliminated:
    rhs_x -= Σ_t Ecoef_t * tvU-part... (see module math). ``M`` is the prebuilt
    augmented matrix (Schur S_xx plus budget border when fully invested).
    """
    # Gᵀ(tv): w-part = -tvL + tvU - tvR a_ret - Rᵀ tvS (+ tvC if cash)
    # zeta-part = -Σ tvS ; u-part = -tvN - tvS
    gtw = np.empty(N)
    tvSsum = 0.0
    for t in range(T):
        tvSsum += tvS[t]
    for i in range(N):
        v = -tvL[i] + tvU[i] - tvR * a_ret[i]
        for t in range(T):
            v += -R[t, i] * tvS[t]
        if fully_invested == 0:
            v += tvC
        gtw[i] = v
    gtz = -tvSsum
    gtu = np.empty(T)
    for t in range(T):
        gtu[t] = -tvN[t] - tvS[t]

    # rhs_z (full): -r_d + Gᵀtv
    rzw = np.empty(N)
    for i in range(N):
        rzw[i] = -rdw[i] + gtw[i]
    rzz = -rdz + gtz
    rzu = np.empty(T)
    for t in range(T):
        rzu[t] = -rdu[t] + gtu[t]

    # eliminate u: rhs_x -= H_xu H_uu^{-1} rhs_u, with column t = D5_t (R_t;1),
    # H_uu^{-1}_t = 1/(D3+D5). e_t = Ecoef_t * rzu_t  (Ecoef=D5/(D3+D5))
    ew = np.zeros(N)
    ez = 0.0
    for t in range(T):
        e = Ecoef[t] * rzu[t]
        for i in range(N):
            ew[i] += e * R[t, i]
        ez += e
    rxw = np.empty(N)
    for i in range(N):
        rxw[i] = rzw[i] - ew[i]
    rxz = rzz - ez

    # assemble and solve augmented system
    rhs = np.empty(nk)
    for i in range(N):
        rhs[i] = rxw[i]
    rhs[N] = rxz
    if fully_invested == 1:
        rhs[nx] = -rb
    sol = np.linalg.solve(M, rhs)
    for i in range(N):
        dw_out[i] = sol[i]
    dz_out[0] = sol[N]
    if fully_invested == 1:
        dy_out[0] = sol[nx]
    else:
        dy_out[0] = 0.0

    # recover Δu_t = (1/(D3+D5)) (rzu_t - D5_t (R_t·Δw + Δζ))
    dz0 = dz_out[0]
    for t in range(T):
        rdw_t = 0.0
        for i in range(N):
            rdw_t += R[t, i] * dw_out[i]
        inv = Ecoef[t] / D5[t]  # = 1/(D3+D5)
        du_out[t] = inv * (rzu[t] - D5[t] * (rdw_t + dz0))


# ---------------------------------------------------------------------------
# Bootstrap frontier-stability band — parallel across all cores (prange)
# ---------------------------------------------------------------------------
#
# The optimization math is cheap, so the compute budget the user dials in
# (light/standard/dense) is spent here, where it buys something real: the
# sampling uncertainty of the frontier. Each of B replicas resamples the T daily
# scenarios with replacement and re-solves the min-CVaR LP at every frontier
# return level; the cross-replica spread of the resulting CVaR is the band.


@njit(parallel=True, fastmath=True, cache=True)
def _bootstrap_cvar(R, a_ret, targets, l, h, kappa, fully_invested, seeds):
    """Annualized CVaR at each target on B bootstrap-resampled scenario sets.

    ``prange`` over the B replicas (one per seed) → all cores. Returns
    cvar_ann[B, K]; row spread = frontier sampling uncertainty. Each inner solve
    is an independent :func:`_cvar_pdip` call, safe to run concurrently.
    """
    B = seeds.shape[0]
    K = targets.shape[0]
    T = R.shape[0]
    N = R.shape[1]
    ann = math.sqrt(252.0)
    out = np.empty((B, K))
    for b in prange(B):
        np.random.seed(seeds[b])  # per-replica determinism (thread-local RNG)
        Rb = np.empty((T, N))
        for t in range(T):
            src = np.random.randint(0, T)  # resample a scenario row with replacement
            for i in range(N):
                Rb[t, i] = R[src, i]
        for k in range(K):
            w, zeta, cvar, conv = _cvar_pdip(Rb, a_ret, targets[k], l, h, kappa, fully_invested, 60)
            out[b, k] = cvar * ann
    return out


def bootstrap_cvar(R: np.ndarray, ctx: dict, seeds: np.ndarray) -> np.ndarray:
    """Run one bootstrap chunk (thin py wrapper around the prange kernel).

    ``ctx`` is the ``_ctx`` dict returned by :func:`mean_cvar_frontier`. Called
    once per streamed chunk so the orchestrator can report progress and honour
    cancellation between chunks. Returns cvar_ann[len(seeds), K].
    """
    return _bootstrap_cvar(
        np.ascontiguousarray(R, dtype=np.float64),
        ctx["a_ret"],
        ctx["targets"],
        ctx["l"],
        ctx["h"],
        float(ctx["kappa"]),
        int(ctx["fi"]),
        np.ascontiguousarray(seeds, dtype=np.int64),
    )


# ---------------------------------------------------------------------------
# Frontier orchestration (pure-python wrappers around the JIT core)
# ---------------------------------------------------------------------------


def _max_return_weights(
    mu_ex: np.ndarray, l: np.ndarray, h: np.ndarray, fully_invested: bool
) -> np.ndarray:
    """Closed-form max-(excess-)return long-only box portfolio.

    Every asset starts at its floor ``l`` (a hard constraint in both modes), then
    the remaining budget greedily fills the highest-μ_ex assets up to their cap.
    Fully invested spends the whole budget to Σw=1; cash-allowed only funds
    *positive*-μ_ex assets beyond their floors (idle cash beats a negative bet).
    """
    # Fund every mandatory floor first in BOTH modes (a per-asset lower bound is a
    # hard constraint regardless of the Σw=1 vs Σw≤1 budget); then greedily fill.
    w = l.copy()
    budget = 1.0 - w.sum()
    order = np.argsort(-mu_ex)
    for i in order:
        if budget <= 1e-12:
            break
        if (not fully_invested) and mu_ex[i] <= 0.0:
            continue
        room = h[i] - w[i]
        add = min(room, budget)
        if add > 0:
            w[i] += add
            budget -= add
    return w


def _as_bound_vec(x, n: int, default: float) -> np.ndarray:
    """Coerce a scalar OR per-asset array bound into a length-n float vector.

    ``w_min``/``w_max`` may now be a single number (applied to every asset) or a
    per-asset sequence (the per-position min/max box the UI sends). Missing/NaN
    entries fall back to ``default``; every value is clamped into [0, 1].
    """
    if x is None:
        arr = np.full(n, float(default))
    elif np.isscalar(x):
        arr = np.full(n, float(x))
    else:
        arr = np.asarray(x, dtype=float).ravel()
        if arr.size != n:
            arr = np.full(n, float(default))
        arr = np.where(np.isfinite(arr), arr, float(default))
    return np.clip(arr, 0.0, 1.0)


def mean_cvar_frontier(
    returns: pd.DataFrame,
    mu: dict[str, float],
    *,
    alpha: float = 0.95,
    w_min=0.0,
    w_max=1.0,
    fully_invested: bool = True,
    rf: float = 0.04,
    n_points: int = 24,
    cov: pd.DataFrame | None = None,
    cdar_beta: float = 0.95,
) -> dict:
    """Trace the mean-CVaR efficient frontier.

    ``returns`` = daily FX-adjusted scenario returns (columns aligned to ``mu``).
    ``w_min``/``w_max`` are each either a scalar (uniform box) or a per-asset
    sequence aligned to ``returns.columns`` (the per-position limits the UI
    sends). Returns a dict with ``frontier`` (min-CVaR→max-return list of point
    dicts), ``min_cvar``, ``max_ret``, ``ok`` and an internal ``_ctx`` the
    orchestrator reuses for the bootstrap stability band. Each point carries
    ``{ret, cvar, vol, mdd, cdar, weights}`` with CVaR annualized (×√252) and
    return/vol annualized. CVaR is optimized in daily units internally.
    """
    symbols = list(returns.columns)
    n = len(symbols)
    if n < 1:
        return {"ok": False, "error": "no assets"}
    R = np.ascontiguousarray(returns.values, dtype=np.float64)
    T = R.shape[0]
    mu_ann = np.array([float(mu.get(s, 0.0)) for s in symbols], dtype=float)
    # per-asset box (scalar broadcast or the UI's per-position vectors)
    l = _as_bound_vec(w_min, n, 0.0)
    h = _as_bound_vec(w_max, n, 1.0)
    # Repair only genuinely inverted boxes (max < min). An *equal* box is a
    # legitimate user constraint — a pin (min==max==x ⇒ hold exactly x) or an
    # exclude (max==0) — and must survive intact; widening it (the old `<=`)
    # silently let pinned/excluded positions take extra weight.
    h = np.where(h < l, np.maximum(l, 1.0 / n if fully_invested else 1.0), h)
    # Σmin > 1 is infeasible in either mode (w ≥ l ⇒ Σw ≥ Σl); Σmax < 1 only bites
    # when fully invested (weights must reach Σw = 1).
    if float(l.sum()) > 1.0 + 1e-9:
        return {
            "ok": False,
            "error": f"infeasible per-position minimums (Σmin={l.sum():.2f} exceeds 100%)",
        }
    if fully_invested and float(h.sum()) < 1.0 - 1e-9:
        return {
            "ok": False,
            "error": "infeasible per-position maximums for a fully-invested "
            f"portfolio (Σmax={h.sum():.2f} below 100%)",
        }
    kappa = 1.0 / ((1.0 - float(alpha)) * T)
    fi = 1 if fully_invested else 0
    ann = math.sqrt(TRADING_DAYS)

    cov_v = None
    if cov is not None:
        cov_v = cov.reindex(index=symbols, columns=symbols).values.astype(float)

    disabled = -1.0e18
    a_ret = mu_ann if fully_invested else (mu_ann - rf)

    def _point(w: np.ndarray) -> dict:
        port = R @ w
        cv_daily = cvar_of(port, alpha)  # daily — drives the efficient envelope
        var30, cvar30 = var_cvar_horizon(port, alpha)  # 10d→30d — display only
        ret_ann = float(mu_ann @ w) if fully_invested else float(rf + (mu_ann - rf) @ w)
        vol = None
        if cov_v is not None:
            vol = float(math.sqrt(max(0.0, w @ cov_v @ w)))
        return {
            "ret": ret_ann,
            "cvar": float(cv_daily * ann),
            "var30": var30,
            "cvar30": cvar30,
            "vol": vol,
            "mdd": max_drawdown(port),
            "cdar": cdar(port, cdar_beta),
            "weights": {symbols[i]: float(w[i]) for i in range(n)},
            "_t": float(a_ret @ w),  # target-return level (for bootstrap band alignment)
        }

    def _ctx(targets: np.ndarray) -> dict:
        # Everything the bootstrap band needs to re-solve at the frontier's own
        # return levels on resampled scenarios (see _bootstrap_cvar / frontier.py).
        return {
            "a_ret": a_ret,
            "l": l,
            "h": h,
            "kappa": float(kappa),
            "fi": int(fi),
            "targets": np.ascontiguousarray(targets, dtype=np.float64),
        }

    if n == 1:
        w = np.array([1.0])
        p = _point(w)
        return {
            "ok": True,
            "frontier": [p],
            "min_cvar": p,
            "max_ret": p,
            "n_nonconv": 0,
            "_ctx": _ctx(np.array([p["_t"]])),
        }

    # endpoints. Track non-convergence so the orchestrator can warn rather than
    # silently trust an iterate that hit the iteration cap.
    n_nonconv = 0
    w_min_cvar, _, _, conv = _cvar_pdip(R, a_ret, disabled, l, h, kappa, fi, 60)
    n_nonconv += 0 if conv else 1
    p_min = _point(w_min_cvar)
    w_maxret = _max_return_weights(mu_ann - (0.0 if fully_invested else rf), l, h, fully_invested)
    p_max = _point(w_maxret)

    r_lo = float(a_ret @ w_min_cvar)
    r_hi = float(a_ret @ w_maxret)
    pts: list[dict] = [p_min]
    if r_hi > r_lo + 1e-9:
        targets = np.linspace(r_lo, r_hi, max(3, int(n_points)))
        for b in targets[1:-1]:
            w, _, _, conv = _cvar_pdip(R, a_ret, float(b), l, h, kappa, fi, 60)
            n_nonconv += 0 if conv else 1
            pts.append(_point(w))
    pts.append(p_max)

    # Sort + enforce the efficient upper-envelope on the *displayed* risk axis
    # (30-day CVaR), so the plotted frontier is monotone (ret nondecreasing in
    # cvar30) and never plots to the left of an earlier point. Points optimized on
    # daily CVaR that turn out dominated on the 30-day axis are dropped from the
    # display. Fall back to daily cvar if cvar30 is NaN (unreachable ≥60-obs data).
    def _xkey(p: dict) -> float:
        c = p.get("cvar30")
        return float(c) if (c is not None and c == c) else float(p["cvar"])

    pts.sort(key=lambda p: (_xkey(p), -p["ret"]))
    cleaned: list[dict] = []
    best = -1e18
    for p in pts:
        if p["ret"] > best + 1e-9:
            cleaned.append(p)
            best = p["ret"]
    if not cleaned:
        cleaned = [p_min]
    targets = np.array([p["_t"] for p in cleaned], dtype=np.float64)
    return {
        "ok": True,
        "frontier": cleaned,
        "min_cvar": cleaned[0],
        "max_ret": cleaned[-1],
        "n_nonconv": int(n_nonconv),
        "_ctx": _ctx(targets),
    }


def portfolio_risk_metrics(
    weights: dict[str, float],
    mu: dict[str, float],
    cov: pd.DataFrame | None,
    returns: pd.DataFrame,
    alpha: float = 0.95,
    rf: float = 0.04,
    fully_invested: bool = True,
) -> dict:
    """Metrics for an arbitrary weight vector (anchors: equal/cap/current)."""
    symbols = list(returns.columns)
    n = len(symbols)
    w = np.array([float(weights.get(s, 0.0)) for s in symbols], dtype=float)
    R = returns.values.astype(float)
    port = R @ w
    mu_ann = np.array([float(mu.get(s, 0.0)) for s in symbols], dtype=float)
    sw = w.sum()
    ret_ann = float(mu_ann @ w) if fully_invested else float(rf * (1.0 - sw) + (mu_ann @ w))
    vol = None
    if cov is not None:
        cov_v = cov.reindex(index=symbols, columns=symbols).values.astype(float)
        vol = float(math.sqrt(max(0.0, w @ cov_v @ w)))
    var30, cvar30 = var_cvar_horizon(port, alpha)
    return {
        "ret": ret_ann,
        "cvar": float(cvar_of(port, alpha) * math.sqrt(TRADING_DAYS)),
        "var30": var30,
        "cvar30": cvar30,
        "vol": vol,
        "mdd": max_drawdown(port),
        "cdar": cdar(port),
        "weights": {symbols[i]: float(w[i]) for i in range(n)},
    }


# ---------------------------------------------------------------------------
# Light (CVaR, return) cloud — visual decoration
# ---------------------------------------------------------------------------


@njit(parallel=True, cache=True, fastmath=True)
def _cloud_kernel(W, R, mu_ann, alpha, h):
    """(cvar_hday, ret_ann) for each row of W. Parallel over portfolios (prange).

    ``cvar_hday`` is the empirical CVaR of the portfolio's *overlapping* ``h``-day
    compounded returns (NOT yet scaled to the display horizon — the caller applies
    the √-time factor). Matches ``var_cvar_horizon``'s estimator so the cloud and
    the frontier points share one CVaR definition.
    """
    K = W.shape[0]
    N = W.shape[1]
    T = R.shape[0]
    M = T - h + 1  # number of overlapping h-day windows
    if M < 1:
        M = 1
    k = int(math.ceil((1.0 - alpha) * M - 1e-9))  # matches _tail_count on M
    if k < 1:
        k = 1
    out = np.empty((K, 2))
    for j in prange(K):
        port = np.empty(T)  # thread-local scratch (must be inside the parallel loop)
        cp = np.empty(T)  # thread-local cumulative product of (1+port)
        rh = np.empty(M)  # thread-local overlapping h-day returns
        r = 0.0
        for i in range(N):
            r += W[j, i] * mu_ann[i]
        acc = 1.0
        for t in range(T):
            pv = 0.0
            for i in range(N):
                pv += W[j, i] * R[t, i]
            port[t] = pv
            acc *= 1.0 + pv
            cp[t] = acc
        if T >= h:
            for m in range(M):
                prev = cp[m - 1] if m >= 1 else 1.0
                rh[m] = cp[m + h - 1] / prev - 1.0
        else:  # degenerate: fewer obs than the horizon
            rh[0] = cp[T - 1] - 1.0
        srt = np.sort(rh)
        tail = 0.0
        for t in range(k):
            tail += srt[t]
        out[j, 0] = -tail / k
        out[j, 1] = r
    return out


def _project_into_box(W: np.ndarray, lo: np.ndarray, hi: np.ndarray, iters: int = 32) -> np.ndarray:
    """Water-fill rows of ``W`` back under ``hi`` without changing their sums.

    ``W`` arrives already at or above ``lo`` and with the right row sums; only the
    upper bounds can still be violated. Clip the overflow and redistribute it into
    whatever headroom the row's other assets still have, proportionally, until
    nothing pokes out. Each pass strictly reduces the excess, so a couple of
    passes normally suffice and ``iters`` is only a safety stop.

    Rejection sampling would be the obvious alternative and is unusable here: with
    a tight box (the reported case pinned 80% of the book across four names) the
    acceptance rate collapses towards zero and the cloud would simply never fill.
    """
    for _ in range(iters):
        over = W - hi
        np.maximum(over, 0.0, out=over)
        excess = over.sum(axis=1)
        np.minimum(W, hi, out=W)
        room = hi - W
        tot = room.sum(axis=1)
        live = (excess > 1e-12) & (tot > 1e-12)
        if not live.any():
            break
        W[live] += (excess[live] / tot[live])[:, None] * room[live]
    return W


def cvar_return_cloud(
    returns: pd.DataFrame,
    mu: dict[str, float],
    alpha: float = 0.95,
    n: int = 4000,
    seed: int = 42,
    w_min=0.0,
    w_max=1.0,
    fully_invested: bool = True,
    rf: float = 0.0,
) -> list:
    """A light long-only cloud in (CVaR_30day, return_annual) space.

    Dirichlet mixture (concentrated + uniform + a few sparse-k) so the cloud
    spans corners without the old 15M-config machinery. The CVaR coordinate is
    the 10d→30d display CVaR (same estimator as the frontier points), so the
    scatter shares the chart's x-axis. Returns ``[[cvar30, ret_ann],…]``.

    ``w_min``/``w_max``/``fully_invested``/``rf`` MUST be the same constraints the
    frontier was solved under. The cloud is read as "the achievable set", so the
    efficient frontier has to be its upper-left envelope; sampling the bare
    simplex while the solver worked inside a per-position box put most of the
    scatter outside the feasible set and left the frontier floating mid-cloud
    (measured: with 80% of the book pinned, 52.7% of points landed at a lower
    CVaR than the frontier's own min-CVaR portfolio, and the axis domain
    stretched to a risk level nothing feasible could reach).

    With no box (``lo=0``, ``hi=1``, fully invested) the free budget is 1 and the
    mixture passes through untouched, so the unconstrained cloud is bit-identical
    to the pre-box behaviour.
    """
    symbols = list(returns.columns)
    n_assets = len(symbols)
    if n_assets < 2 or returns.shape[0] < 2:
        return []
    R = np.ascontiguousarray(returns.values, dtype=np.float64)
    mu_ann = np.array([float(mu.get(s, 0.0)) for s in symbols], dtype=float)
    lo = _as_bound_vec(w_min, n_assets, 0.0)
    hi = np.maximum(_as_bound_vec(w_max, n_assets, 1.0), lo)
    lo_sum = float(lo.sum())
    # An infeasible floor (mins summing past the budget) would make the frontier
    # solve fail long before we get here; scale it back rather than emit NaNs.
    if lo_sum > 1.0:
        lo = lo / lo_sum
        hi = np.maximum(hi, lo)
        lo_sum = 1.0
    rng = np.random.default_rng(seed)
    n = max(500, int(n))
    n_lo = n // 3
    n_hi = n // 3
    n_sp = n - n_lo - n_hi
    # D holds the *direction* the free budget is spread in (rows sum to 1); the
    # box floor is added underneath it below.
    D = np.empty((n, n_assets), dtype=np.float64)
    D[:n_lo] = rng.dirichlet(np.full(n_assets, 0.15), size=n_lo)
    D[n_lo : n_lo + n_hi] = rng.dirichlet(np.full(n_assets, 1.0), size=n_hi)
    # sparse-k rows
    base = n_lo + n_hi
    ks = np.clip(rng.choice(np.array([1, 2, 3, 5, 8]), size=n_sp), 1, n_assets)
    D[base:] = 0.0
    for r in range(n_sp):
        kk = int(ks[r])
        cols = rng.choice(n_assets, size=kk, replace=False)
        D[base + r, cols] = rng.dirichlet(np.ones(kk))
    # Fully invested => every row spends the whole free budget. With cash allowed
    # the solver may hold any amount back, so sample the spend to match the set it
    # actually optimizes over.
    free = 1.0 - lo_sum
    spend = np.full(n, free) if fully_invested else rng.uniform(0.0, free, size=n)
    W = np.ascontiguousarray(lo + spend[:, None] * D)
    if (hi < 1.0).any():
        W = _project_into_box(W, lo, hi)
    arr = _cloud_kernel(W, R, mu_ann, float(alpha), int(LIQ_HORIZON))
    arr[:, 0] *= math.sqrt(float(DISP_HORIZON) / float(LIQ_HORIZON))  # 10d → 30d
    # Cash earns rf. The kernel only sums w·mu, so a row holding cash would
    # otherwise be reported below the return the same portfolio shows on the
    # frontier, re-opening the mismatch this function exists to close.
    if not fully_invested:
        arr[:, 1] += (1.0 - W.sum(axis=1)) * float(rf)
    return arr.tolist()


# ---------------------------------------------------------------------------
# JIT warm-up (first user call shouldn't pay the compile cost)
# ---------------------------------------------------------------------------


def _warm_jit() -> None:
    rng = np.random.default_rng(0)
    R = np.ascontiguousarray(rng.standard_normal((40, 3)) * 0.01)
    mu = np.array([0.08, 0.10, 0.06])
    l = np.zeros(3)
    h = np.ones(3)
    _cvar_pdip(R, mu, -1e18, l, h, 1.0 / (0.05 * 40), 1, 40)
    _cloud_kernel(np.ascontiguousarray(rng.dirichlet(np.ones(3), size=8)), R, mu, 0.95, 10)
    _bootstrap_cvar(
        R, mu, np.array([0.05, 0.10]), l, h, 1.0 / (0.05 * 40), 1, np.array([1, 2], dtype=np.int64)
    )


try:
    _warm_jit()
except Exception:
    # Warm-up failure must not break import; the first real call JITs lazily.
    pass
