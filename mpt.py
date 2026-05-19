"""
mpt.py — Modern Portfolio Theory primitives for the Portfolio Tracker.

Long-only, fully-invested (sum=1) mean-variance optimization. Pure NumPy / Pandas
so the dashboard stays dependency-light. Designed for ~5–60 assets and a
10–20 s wall budget per call (data fetch dominates; the math here is <100 ms).

Public surface (kept minimal — see HTTP handler in dashboard.py):
    compute_returns(closes_df, freq)        -> returns_df
    annualize(returns_df, freq)             -> (mu, sigma)        (annualized mean + cov)
    ledoit_wolf_shrink(cov)                 -> shrunken cov
    critical_line(mu, cov)                  -> [turning points]   (Markowitz CLA, long-only)
    frontier_curve(turning_points, n)       -> list[{ret, vol, weights}]
    tangency_portfolio(curve, rf)           -> {ret, vol, weights, sharpe}
    monte_carlo_cloud(mu, cov, n)           -> list[(vol, ret, sharpe)]
    portfolio_stats(w, mu, cov, rf=0)       -> {ret, vol, sharpe}

The Critical Line Algorithm (Markowitz 1959; Bailey & López de Prado 2013) is
used instead of a per-target QP loop because it returns the *entire* exact
piecewise-linear frontier in a single pass — orders of magnitude faster than
calling scipy.optimize.minimize for each target return.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd
from numba import njit, prange
from scipy.optimize import linprog
from scipy.sparse import csr_matrix, eye as speye, hstack as sphstack, vstack as spvstack


FREQ_PER_YEAR = {"daily": 252, "weekly": 52, "monthly": 12}
_FREQ_RESAMPLE = {"daily": None, "weekly": "W-FRI", "monthly": "M"}


# ---------------------------------------------------------------------------
# Returns + covariance
# ---------------------------------------------------------------------------

def compute_returns(closes: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Resample close prices to the requested frequency and return pct-change.

    Drops the first row (NaN) and any column / row with insufficient overlap.
    """
    freq = freq.lower()
    if freq not in FREQ_PER_YEAR:
        raise ValueError(f"unknown freq {freq!r}; expected daily/weekly/monthly")
    rule = _FREQ_RESAMPLE[freq]
    px = closes.sort_index()
    if rule is not None:
        px = px.resample(rule).last()
    rets = px.pct_change().dropna(how="all")
    # Drop columns that are mostly NaN; require at least 60% coverage
    min_obs = max(8, int(0.6 * len(rets)))
    keep = [c for c in rets.columns if rets[c].notna().sum() >= min_obs]
    rets = rets[keep].dropna()
    return rets


def annualize(returns: pd.DataFrame, freq: str) -> tuple[pd.Series, pd.DataFrame]:
    """Annualized mean (vector) and covariance (matrix) from periodic returns."""
    n = FREQ_PER_YEAR[freq.lower()]
    mu = returns.mean() * n
    cov = returns.cov() * n
    return mu, cov


def ledoit_wolf_shrink(cov: pd.DataFrame, returns: pd.DataFrame | None = None) -> pd.DataFrame:
    """Ledoit-Wolf shrinkage toward the constant-correlation target.

    If ``returns`` is provided we estimate the optimal shrinkage intensity from
    it; otherwise we use a fixed 0.2 prior. Pure NumPy, ~20 lines.
    """
    s = cov.values.astype(float)
    n = s.shape[0]
    if n < 2:
        return cov.copy()
    var = np.diag(s)
    std = np.sqrt(np.clip(var, 1e-18, None))
    corr = s / np.outer(std, std)
    # average off-diagonal correlation
    mask = ~np.eye(n, dtype=bool)
    r_bar = float(corr[mask].mean()) if mask.any() else 0.0
    target = r_bar * np.outer(std, std)
    np.fill_diagonal(target, var)
    if returns is None or len(returns) < 4:
        alpha = 0.2
    else:
        # Light-touch Ledoit-Wolf intensity: based on variance of sample cov
        x = returns.values - returns.values.mean(axis=0, keepdims=True)
        t = x.shape[0]
        phi_mat = ((x[:, :, None] * x[:, None, :]) - s[None, :, :]) ** 2
        phi = phi_mat.sum() / t
        gamma = float(((s - target) ** 2).sum())
        alpha = float(np.clip(phi / (gamma * t + 1e-18), 0.0, 1.0)) if gamma > 0 else 0.0
    shrunk = alpha * target + (1.0 - alpha) * s
    return pd.DataFrame(shrunk, index=cov.index, columns=cov.columns)


# ---------------------------------------------------------------------------
# Critical Line Algorithm (long-only, sum=1, no upper bound)
# ---------------------------------------------------------------------------

@dataclass
class _TurningPoint:
    w: np.ndarray         # weights vector
    lam: float            # lambda (risk-aversion parameter at this corner)
    ret: float            # annualised return
    vol: float            # annualised vol


def _stats(w: np.ndarray, mu: np.ndarray, cov: np.ndarray) -> tuple[float, float]:
    r = float(w @ mu)
    v = float(math.sqrt(max(w @ cov @ w, 0.0)))
    return r, v


def critical_line(mu_in: pd.Series, cov_in: pd.DataFrame,
                  lower: float = 0.0) -> list[_TurningPoint]:
    """Markowitz CLA for long-only, sum=1, with a per-asset lower bound.

    Returns a list of turning points ordered from max-return corner (highest
    lambda) down to min-variance corner (lambda → 0). The efficient frontier
    is the piecewise-linear path between consecutive turning points.

    Algorithm: Bailey & López de Prado (2013), simplified for the box
    ``[lower, 1]`` with the sum-to-one equality constraint. ``lower = 0`` is
    the standard long-only case; ``lower > 0`` enforces a per-asset minimum
    weight (Diversified mode). At ``N·lower → 1`` the only feasible
    portfolio is equal-weight, and we return that single corner.
    """
    symbols = list(mu_in.index)
    mu = mu_in.values.astype(float)
    cov = cov_in.loc[symbols, symbols].values.astype(float)
    n = len(mu)
    if n == 0:
        return []
    if n == 1:
        w = np.array([1.0])
        r, v = _stats(w, mu, cov)
        return [_TurningPoint(w, 0.0, r, v)]
    if lower < 0:
        lower = 0.0
    # Degenerate: floor saturates the budget — only feasible portfolio is
    # equal-weight (w_i = 1/N everywhere).
    if lower * n >= 1.0 - 1e-12:
        w = np.full(n, 1.0 / n)
        r, v = _stats(w, mu, cov)
        return [_TurningPoint(w, 0.0, r, v)]

    # ---- Step 1: starting corner = max-return feasible portfolio. With a
    # floor, that puts every asset at `lower` and gives the residual
    # (1 - (n-1)*lower) to the highest-mean asset.
    free: list[int] = []
    w = np.full(n, lower)
    order = np.argsort(-mu)
    w[order[0]] = 1.0 - (n - 1) * lower
    free = [int(order[0])]
    turning: list[_TurningPoint] = []
    r, v = _stats(w, mu, cov)
    turning.append(_TurningPoint(w.copy(), float("inf"), r, v))

    def _solve_free(free_idx: list[int]) -> tuple[np.ndarray, np.ndarray, float, np.ndarray]:
        """Solve for free-asset weights as affine functions of lambda.

        Returns (alpha, beta, c, w_F) where w_F(λ) = alpha + λ * beta is the
        optimal free-asset allocation, and c sums beta. The KKT system for
        the constrained QP (min ½ w'Σw - λ μ'w s.t. Σ w = 1, w_B fixed) gives
        a linear system in [w_F; ν] that we split into a constant and a
        λ-scaled component.
        """
        F = free_idx
        kF = len(F)
        if kF == 0:
            raise RuntimeError("CLA: empty free set")
        cov_FF = cov[np.ix_(F, F)]
        # Construct KKT matrix:
        # [ Σ_FF   1 ] [ w_F ] = [  λ μ_F - Σ_FB w_B ]
        # [ 1ᵀ     0 ] [  ν  ]   [  1 - 1ᵀ w_B       ]
        B = [i for i in range(n) if i not in F]
        w_B = w[B] if B else np.zeros(0)
        cov_FB = cov[np.ix_(F, B)] if B else np.zeros((kF, 0))
        rhs0 = np.empty(kF + 1)
        rhs0[:kF] = -cov_FB @ w_B if B else 0.0
        rhs0[kF] = 1.0 - (w_B.sum() if B else 0.0)
        rhs1 = np.empty(kF + 1)
        rhs1[:kF] = mu[F]
        rhs1[kF] = 0.0
        K = np.zeros((kF + 1, kF + 1))
        K[:kF, :kF] = cov_FF
        K[:kF, kF] = 1.0
        K[kF, :kF] = 1.0
        # Pseudo-inverse handles singular cov on degenerate corners
        try:
            sol0 = np.linalg.solve(K, rhs0)
            sol1 = np.linalg.solve(K, rhs1)
        except np.linalg.LinAlgError:
            sol0 = np.linalg.lstsq(K, rhs0, rcond=None)[0]
            sol1 = np.linalg.lstsq(K, rhs1, rcond=None)[0]
        alpha = sol0[:kF]
        beta = sol1[:kF]
        return alpha, beta, float(beta.sum()), w_B

    max_iter = 4 * n + 20
    lam_prev = float("inf")
    for _ in range(max_iter):
        # Decide candidate transitions: an asset enters or leaves the free set
        # at the λ where its weight hits 0 or where lagrangian sign flips.
        alpha, beta, _, _ = _solve_free(free)

        # Candidate λ where a free asset's weight hits 0 (it would leave free)
        best_lam = -math.inf
        best_action: tuple[str, int] | None = None
        for k, i in enumerate(free):
            b = beta[k]
            a = alpha[k]
            if abs(b) < 1e-14:
                continue
            lam_i = (lower - a) / b   # w_i(λ) = a + λ b = lower
            if lam_i < lam_prev - 1e-12 and lam_i > best_lam:
                best_lam = lam_i
                best_action = ("remove", i)

        # Candidate λ where a bound asset would want to become free (gradient
        # of Lagrangian on its bound becomes favorable).
        # For an asset i at bound 0, it enters when
        #   λ μ_i  - Σ_iF w_F(λ) - ν(λ) = 0
        # which is linear in λ; solve for λ.
        # We compute ν(λ) implicitly via KKT — easier: refit including i and
        # check whether its weight would become positive.
        for i in range(n):
            if i in free:
                continue
            trial = sorted(free + [i])
            try:
                a_t, b_t, _, _ = _solve_free(trial)
            except Exception:
                continue
            j = trial.index(i)
            a_i, b_i = a_t[j], b_t[j]
            if abs(b_i) < 1e-14:
                continue
            lam_i = (lower - a_i) / b_i   # weight of i hits lower from above at this λ
            if lam_i < lam_prev - 1e-12 and lam_i > best_lam:
                best_lam = lam_i
                best_action = ("add", i)

        if best_action is None or best_lam <= 0:
            # Compute global minimum-variance portfolio (λ = 0). Bound assets
            # stay at `lower`; free assets take the KKT-solved α₀ value.
            alpha0, _, _, _ = _solve_free(free)
            w_new = np.full(n, lower)
            for k, i in enumerate(free):
                w_new[i] = max(lower, alpha0[k])
            s = w_new.sum()
            # Renormalise only via the free assets — bound assets must stay
            # at exactly `lower`. Compute the excess and redistribute on the
            # free set proportionally to their already-clipped values.
            excess = s - 1.0
            if abs(excess) > 1e-12 and free:
                free_sum = sum(w_new[i] - lower for i in free)
                if free_sum > 1e-12:
                    scale = (free_sum - excess) / free_sum
                    for i in free:
                        w_new[i] = lower + (w_new[i] - lower) * scale
            r, v = _stats(w_new, mu, cov)
            turning.append(_TurningPoint(w_new, 0.0, r, v))
            break

        # Apply transition
        lam_prev = best_lam
        w_new = np.full(n, lower)
        alpha_t, beta_t, _, _ = _solve_free(free if best_action[0] == "remove"
                                            else sorted(free + [best_action[1]]))
        idx_set = free if best_action[0] == "remove" else sorted(free + [best_action[1]])
        for k, i in enumerate(idx_set):
            w_new[i] = max(lower, alpha_t[k] + best_lam * beta_t[k])
        s = w_new.sum()
        excess = s - 1.0
        if abs(excess) > 1e-12 and idx_set:
            free_sum = sum(w_new[i] - lower for i in idx_set)
            if free_sum > 1e-12:
                scale = (free_sum - excess) / free_sum
                for i in idx_set:
                    w_new[i] = lower + (w_new[i] - lower) * scale
        r, v = _stats(w_new, mu, cov)
        turning.append(_TurningPoint(w_new, best_lam, r, v))
        # Update free set
        if best_action[0] == "remove":
            free = [i for i in free if i != best_action[1]]
            if not free:
                # All free assets have hit `lower` — we've reached the
                # most-diversified extreme. Bail out with this as the min-vol
                # corner instead of re-seeding (which would only loop).
                break
        else:
            free = sorted(free + [best_action[1]])

    return turning


def critical_line_with_floor(mu_in: pd.Series, cov_in: pd.DataFrame,
                             floor: float) -> list[_TurningPoint]:
    """Convenience: long-only CLA with a per-asset minimum weight.

    Equivalent to ``critical_line(mu, cov, lower=floor)`` — kept as a named
    entry point so callers reading the dashboard side can see at a glance
    that Diversified mode goes through here.
    """
    return critical_line(mu_in, cov_in, lower=max(0.0, float(floor)))


# ---------------------------------------------------------------------------
# Frontier sampling + tangency
# ---------------------------------------------------------------------------

def _segment_arc_len(a: _TurningPoint, b: _TurningPoint,
                     mu_v: np.ndarray, cov_v: np.ndarray, k: int = 8) -> float:
    """Approximate arc length of the (vol, ret) curve as weights walk a→b.

    Both ``ret`` and ``vol²`` are quadratic in the segment parameter ``t``
    (ret is linear; vol² = wᵀΣw becomes A + 2Bt + Ct²), so a small Simpson
    rule on ``k+1`` sample points is more than enough — empirically <1e-4
    relative error vs k=64 even on pathological segments.
    """
    if k < 2:
        k = 2
    ts = np.linspace(0.0, 1.0, k + 1)
    rs = np.empty(k + 1)
    vs = np.empty(k + 1)
    for i, t in enumerate(ts):
        w = (1.0 - t) * a.w + t * b.w
        rs[i] = float(w @ mu_v)
        vs[i] = math.sqrt(max(float(w @ cov_v @ w), 0.0))
    return float(np.sum(np.hypot(np.diff(vs), np.diff(rs))))


def frontier_curve(turning: list[_TurningPoint], mu: pd.Series, cov: pd.DataFrame,
                   n_samples: int = 200) -> list[dict]:
    """Sample n points along the piecewise-linear efficient frontier.

    Improvements over the simple Euclidean-chord allocation:

    * Per-segment sample count is proportional to the **arc length** of the
      curve in (vol, ret) space — not the straight-line chord. Without this,
      curved segments (where weight-space linearity ≠ (vol,ret) linearity)
      get under-sampled and the rendered polyline cuts the arc, producing
      visible "spikes" between dense and sparse segments.
    * **Every turning point is included exactly once and in order** so the
      true min-variance corner (``turning[-1]``) and max-return corner
      (``turning[0]``) are the first and last entries of the output. That
      lets the UI slider land precisely on them at its endpoints.
    * An **upper-envelope filter** drops any sample whose ``ret`` is ≤ the
      running max ``ret`` at a smaller ``vol``. Mathematically the efficient
      frontier is the upper envelope of the feasible set, so we enforce
      monotone-nondecreasing ret(vol). Kills residual numerical jitter that
      would otherwise show as zigzags.

    Returns dicts ``{ret, vol, weights}`` ordered from min-vol to max-ret.
    """
    if not turning:
        return []
    # Order from min-vol (last turning point, λ≈0) to max-ret (first, λ→∞)
    tp = list(reversed(turning))
    symbols = list(mu.index)
    mu_v = mu.values.astype(float)
    cov_v = cov.loc[symbols, symbols].values.astype(float)

    def _pt(w: np.ndarray) -> dict:
        r, v = _stats(w, mu_v, cov_v)
        return {"ret": r, "vol": v, "weights": dict(zip(symbols, w.tolist()))}

    if len(tp) == 1:
        return [_pt(tp[0].w)]

    # Arc length per segment for sample-budget allocation
    seg_lens = [max(1e-12, _segment_arc_len(a, b, mu_v, cov_v))
                for a, b in zip(tp[:-1], tp[1:])]
    total = sum(seg_lens) or 1e-12
    n_samples = max(len(tp), int(n_samples))
    # Floor of 3 interior samples per segment so even tiny corners get drawn
    interior_budget = max(0, n_samples - len(tp))
    per_seg_extra = [max(0, int(round(interior_budget * L / total))) for L in seg_lens]

    pts: list[dict] = [_pt(tp[0].w)]  # exact min-vol corner
    for (a, b), extra in zip(zip(tp[:-1], tp[1:]), per_seg_extra):
        k = max(0, extra)
        if k > 0:
            # Interior samples strictly between t=0 and t=1 (corners are added
            # by neighbouring segments / the explicit append below).
            for t in np.linspace(0.0, 1.0, k + 2)[1:-1]:
                w = (1.0 - t) * a.w + t * b.w
                # CLA weights are non-negative by construction, but clip+renorm
                # is cheap insurance against floating-point drift.
                w = np.clip(w, 0.0, None)
                s = w.sum()
                if s > 0:
                    w = w / s
                pts.append(_pt(w))
        # Append the right-hand corner exactly once
        pts.append(_pt(b.w))

    # Sort by vol and apply the upper-envelope filter. This is the *definition*
    # of the efficient frontier in (vol, ret) space, so any non-monotone
    # samples are by construction dominated — safe to drop.
    pts.sort(key=lambda p: (p["vol"], -p["ret"]))
    cleaned: list[dict] = []
    best_ret = -math.inf
    for p in pts:
        if p["ret"] > best_ret + 1e-12:
            cleaned.append(p)
            best_ret = p["ret"]
    # Ensure the exact endpoints are present (they should be after sort, but
    # the filter can drop a numerically-identical duplicate of the corner).
    if cleaned and cleaned[0]["vol"] > tp[0].vol + 1e-12:
        cleaned.insert(0, _pt(tp[0].w))
    if cleaned and cleaned[-1]["ret"] < tp[-1].ret - 1e-12:
        cleaned.append(_pt(tp[-1].w))
    return cleaned


def tangency_portfolio(curve: list[dict], rf: float = 0.0) -> dict | None:
    """Max-Sharpe point on the supplied frontier polyline."""
    if not curve:
        return None
    best = max(curve, key=lambda p: (p["ret"] - rf) / p["vol"] if p["vol"] > 1e-12 else -math.inf)
    sharpe = (best["ret"] - rf) / best["vol"] if best["vol"] > 1e-12 else float("nan")
    return {**best, "sharpe": sharpe}


# ---------------------------------------------------------------------------
# Monte-Carlo cloud (visual decoration)
# ---------------------------------------------------------------------------
#
# Design notes
# ------------
# The cloud serves two purposes for the user:
#   (1) visually convey the feasible set of long-only portfolios, and
#   (2) make the efficient-frontier curve look honest by ensuring some
#       random samples actually sit at or near the frontier.
#
# A pure Dirichlet(α≈0.3-1.0) sampler on N≈55 assets concentrates around
# equal-weight portfolios (mass is roughly Beta-distributed with mean 1/N
# and tiny variance), so the resulting cloud never reaches the high-return
# corner (≈100% on the best-mu asset) nor the low-vol corner (concentrated
# on the lowest-vol asset). The frontier ends up "floating" above an
# unrelated cluster — visually suspect, even when correct.
#
# The mixture sampler below explicitly explores:
#   * sparse-k subsets — pick k ∈ {1,2,3,5,8,13,21}, Dirichlet(1) within
#     the subset. Reaches single-asset and small-coalition corners.
#   * low-α Dirichlet (α=0.05) — heavily skewed, often near-corner.
#   * medium-α Dirichlet (α=0.3) — the legacy "concentrated" tier.
#   * high-α Dirichlet (α=1.0) — uniform on the simplex.
#
# All evaluation goes through a numba-JIT, parallel kernel (`_mc_kernel`)
# so 30M portfolios on a modern multi-core box finish in ~30-60s with
# bounded memory. The outer Python loop streams batches and discards W
# after each batch — only the (vol, ret, sharpe) triples persist.
# Result is returned as a contiguous (N, 3) float32 array.

_SPARSE_K_CHOICES = np.array([1, 2, 3, 5, 8, 13, 21], dtype=np.int64)


@njit(parallel=True, fastmath=True, cache=True)
def _mc_kernel(W, mu_v, cov_v, rf):
    """Compute (vol, ret, sharpe) for each row of W. JIT, multi-core, no allocations of W."""
    n = W.shape[0]
    m = W.shape[1]
    out = np.empty((n, 3), dtype=np.float32)
    for i in prange(n):
        r = 0.0
        for j in range(m):
            r += W[i, j] * mu_v[j]
        v2 = 0.0
        for j in range(m):
            s = 0.0
            for k in range(m):
                s += W[i, k] * cov_v[k, j]
            v2 += W[i, j] * s
        if v2 < 0.0:
            v2 = 0.0
        v = math.sqrt(v2)
        out[i, 0] = np.float32(v)
        out[i, 1] = np.float32(r)
        if v > 1e-12:
            out[i, 2] = np.float32((r - rf) / v)
        else:
            out[i, 2] = np.float32(np.nan)
    return out


def _sample_batch(rng: np.random.Generator, n_assets: int, batch_size: int,
                  floor: float = 0.0) -> np.ndarray:
    """Mixture sampler: returns (batch_size, n_assets) float64 weights summing to 1 per row.

    When ``floor > 0`` (Diversified mode) sparse-k samples violate the
    per-asset minimum by construction, so we skip them entirely and reweight
    the Dirichlet mixture across α=0.3/1.0 (low-α concentrates mass on few
    assets and is also a poor fit when every asset must carry weight).
    Each row is shifted via ``w = floor + B * ũ`` with ``B = 1 - N*floor``
    so the per-asset floor is exactly enforced.
    """
    W = np.zeros((batch_size, n_assets), dtype=np.float64)
    if floor > 0.0 and n_assets >= 1 and floor * n_assets < 1.0:
        # Diversified mixture: medium / high Dirichlet only (skip sparse-k
        # because at least one entry would be 0 < floor).
        n_med  = int(0.50 * batch_size)
        n_high = batch_size - n_med
        row = 0
        if n_med > 0:
            W[row:row + n_med, :] = rng.dirichlet(np.full(n_assets, 0.3), size=n_med)
            row += n_med
        if n_high > 0:
            W[row:row + n_high, :] = rng.dirichlet(np.full(n_assets, 1.0), size=n_high)
        # Apply floor: w = floor + B * ũ
        B = 1.0 - n_assets * floor
        W = floor + B * W
        return W
    # Sparse (default) — original mixture
    # Split: 25% sparse-k, 30% low-α, 25% medium-α, 20% high-α
    n_sparse = int(0.25 * batch_size)
    n_low    = int(0.30 * batch_size)
    n_med    = int(0.25 * batch_size)
    n_high   = batch_size - n_sparse - n_low - n_med
    row = 0
    # Sparse-k: pick k assets uniformly, then Dirichlet(1) inside that subset
    if n_sparse > 0 and n_assets >= 1:
        ks = rng.choice(_SPARSE_K_CHOICES, size=n_sparse)
        # Cap k at n_assets so we don't ask for more than we have
        ks = np.clip(ks, 1, n_assets)
        for r in range(n_sparse):
            k = int(ks[r])
            cols = rng.choice(n_assets, size=k, replace=False)
            w_sub = rng.dirichlet(np.ones(k))
            W[row, cols] = w_sub
            row += 1
    # Low-alpha Dirichlet across all assets
    if n_low > 0:
        W[row:row + n_low, :] = rng.dirichlet(np.full(n_assets, 0.05), size=n_low)
        row += n_low
    # Medium-alpha Dirichlet
    if n_med > 0:
        W[row:row + n_med, :] = rng.dirichlet(np.full(n_assets, 0.3), size=n_med)
        row += n_med
    # High-alpha Dirichlet (~uniform on simplex)
    if n_high > 0:
        W[row:row + n_high, :] = rng.dirichlet(np.full(n_assets, 1.0), size=n_high)
    return W


def _sample_perturbed(rng: np.random.Generator, frontier_weights: np.ndarray,
                      n_samples: int, jitter: float = 0.40,
                      floor: float = 0.0) -> np.ndarray:
    """Sample portfolios as perturbations of CLA frontier weights.

    The CLA frontier path covers the *upper edge* of the feasible set
    (max-return-for-vol). A pure-Dirichlet cloud concentrates at the simplex
    centroid and leaves the rendered frontier curve floating above the dots.
    Mixing perturbed-frontier samples fills the region *adjacent* to the
    frontier, so the cloud visibly hugs the curve.

    Each row is ``(1 - j) * w_base + j * dirichlet(alpha=2.0)`` where
    ``w_base`` is a uniformly-chosen row of ``frontier_weights`` and ``j``
    itself is uniform in ``[jitter/2, jitter]`` for spread variety. The
    ``floor`` constraint is enforced post-mix.
    """
    if frontier_weights is None or frontier_weights.size == 0:
        return np.zeros((0, 0), dtype=np.float64)
    K, n_assets = frontier_weights.shape
    if n_samples <= 0 or n_assets == 0:
        return np.zeros((0, n_assets), dtype=np.float64)
    base_idx = rng.integers(0, K, size=n_samples)
    base = np.ascontiguousarray(frontier_weights[base_idx], dtype=np.float64)
    noise = rng.dirichlet(np.full(n_assets, 2.0), size=n_samples)
    j = rng.uniform(jitter * 0.5, jitter, size=n_samples).reshape(-1, 1)
    W = (1.0 - j) * base + j * noise
    if floor > 0.0 and floor * n_assets < 1.0:
        # Clamp to floor and renormalise on the residual budget.
        W = np.maximum(W, floor)
        B = 1.0 - n_assets * floor
        # Re-distribute excess back so each row sums to 1.
        s = W.sum(axis=1, keepdims=True)
        W = floor + B * (W - floor) / np.maximum(s - n_assets * floor, 1e-12)
    else:
        # Renormalise to sum=1 (Dirichlet noise already sums to 1, base too,
        # but the convex combination is exact only in expectation).
        s = W.sum(axis=1, keepdims=True)
        W = W / np.maximum(s, 1e-12)
    return W


def monte_carlo_cloud(mu: pd.Series, cov: pd.DataFrame, n_samples: int = 25_000,
                      rf: float = 0.0, seed: int = 42, batch_size: int = 50_000,
                      anchor_weights: np.ndarray | None = None,
                      floor: float = 0.0,
                      frontier_weights: np.ndarray | None = None,
                      perturbed_fraction: float = 0.35) -> np.ndarray:
    """Hybrid sampler: perturbed-frontier + mixture-Dirichlet, long-only.

    Returns an ``(N, 3) float32`` ndarray of ``(vol, ret, sharpe)`` rows. ``N``
    is ``n_samples`` plus the number of anchor rows (if any). Weights are
    streamed batch-by-batch so peak RAM stays at ``O(batch_size * n_assets)``
    regardless of total sample count.

    Sampling strategy:
      - ``perturbed_fraction`` of ``n_samples`` come from
        ``_sample_perturbed(frontier_weights)`` — these hug the frontier curve.
      - The remaining samples come from the original Dirichlet mixture
        (``_sample_batch``) — these fill the diffuse feasible region away
        from the frontier. Default 35/65 split is biased toward the wide
        cloud so the lower-right (high-vol) region renders densely.

    If ``frontier_weights`` is None (or empty), the sampler degrades to the
    pure-Dirichlet behaviour — preserves caller back-compat.

    ``anchor_weights`` (optional ``(K, n_assets) float64``) lets the caller
    inject specific portfolios. Appended at the end of the output, untouched.
    """
    symbols = list(mu.index)
    n_assets = len(symbols)
    if n_assets == 0:
        return np.empty((0, 3), dtype=np.float32)
    n_samples = max(0, int(n_samples))
    n_anchor = 0 if anchor_weights is None else int(anchor_weights.shape[0])
    total = n_samples + n_anchor
    if total <= 0:
        return np.empty((0, 3), dtype=np.float32)

    rng = np.random.default_rng(seed)
    mu_v = np.ascontiguousarray(mu.values, dtype=np.float64)
    cov_v = np.ascontiguousarray(cov.loc[symbols, symbols].values, dtype=np.float64)
    out = np.empty((total, 3), dtype=np.float32)

    # Split between perturbed-frontier and pure-Dirichlet samples.
    have_frontier = (frontier_weights is not None
                     and getattr(frontier_weights, "size", 0) > 0
                     and frontier_weights.shape[1] == n_assets)
    pf = float(perturbed_fraction) if have_frontier else 0.0
    pf = max(0.0, min(1.0, pf))
    n_pert = int(round(n_samples * pf))
    n_diri = n_samples - n_pert

    cursor = 0
    bs = max(1024, int(batch_size))

    # Perturbed-frontier batches — hug the curve.
    remaining = n_pert
    while remaining > 0:
        b = min(bs, remaining)
        W = _sample_perturbed(rng, frontier_weights, b, floor=floor)
        out[cursor:cursor + b] = _mc_kernel(W, mu_v, cov_v, float(rf))
        cursor += b
        remaining -= b
        del W

    # Dirichlet-mixture batches — preserve wide coverage.
    remaining = n_diri
    while remaining > 0:
        b = min(bs, remaining)
        W = _sample_batch(rng, n_assets, b, floor=floor)
        out[cursor:cursor + b] = _mc_kernel(W, mu_v, cov_v, float(rf))
        cursor += b
        remaining -= b
        del W

    # Anchors (small, evaluated as one batch)
    if n_anchor > 0:
        Wa = np.ascontiguousarray(anchor_weights, dtype=np.float64)
        out[cursor:cursor + n_anchor] = _mc_kernel(Wa, mu_v, cov_v, float(rf))

    return out


def anchor_samples(turning: list[_TurningPoint], n_per_segment: int = 4) -> np.ndarray:
    """Sample weight vectors along the CLA piecewise-linear frontier path.

    Used by ``compute_efficient_frontier`` to feed ``monte_carlo_cloud`` so
    the cloud explicitly contains portfolios that lie on the frontier — gives
    the user visual proof that the rendered curve really is achievable.
    Returns ``(K, n_assets) float64``.
    """
    if not turning:
        return np.zeros((0, 0), dtype=np.float64)
    tp = list(reversed(turning))  # min-vol -> max-ret
    n_assets = len(tp[0].w)
    if len(tp) == 1:
        return tp[0].w.reshape(1, n_assets).astype(np.float64)
    rows: list[np.ndarray] = []
    for a, b in zip(tp[:-1], tp[1:]):
        for t in np.linspace(0.0, 1.0, max(2, n_per_segment), endpoint=False):
            w = (1.0 - t) * a.w + t * b.w
            w = np.clip(w, 0.0, None)
            s = w.sum()
            if s > 0:
                w = w / s
            rows.append(w)
    rows.append(tp[-1].w)
    return np.asarray(rows, dtype=np.float64)


# Warm the JIT cache at import so the first user request doesn't pay the
# ~1-2 s compile cost. Uses a tiny dummy problem.
def _warm_jit() -> None:
    _W = np.array([[0.5, 0.5], [1.0, 0.0]], dtype=np.float64)
    _mu = np.array([0.1, 0.05], dtype=np.float64)
    _cov = np.array([[0.04, 0.0], [0.0, 0.04]], dtype=np.float64)
    _mc_kernel(_W, _mu, _cov, 0.0)


try:
    _warm_jit()
except Exception:
    # Warm-up failure should not break import; the first real call will JIT lazily.
    pass


# ---------------------------------------------------------------------------
# Single-portfolio stats
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Minimum-CVaR optimization (Rockafellar–Uryasev LP)
# ---------------------------------------------------------------------------
#
# For confidence level α ∈ (0, 1), the min-CVaR portfolio at α minimises the
# expected loss conditional on the loss being in the worst (1-α) quantile.
# Rockafellar & Uryasev (2000) showed this can be cast as a linear program
# over the joint variables (w, ζ, u):
#
#     variables : w ∈ R^N (weights),
#                 ζ ∈ R   (the VaR — sign convention: loss = -portfolio return),
#                 u ∈ R^T_+ (slack per scenario)
#     minimise  : ζ + 1/((1-α) T) · Σ_t u_t
#     s.t.      : u_t ≥ -R_t · w - ζ          (scenario inequality)
#                 u_t ≥ 0                      (non-negative slack)
#                 Σ w = 1                      (fully invested)
#                 lower ≤ w_i ≤ 1              (long-only with optional floor)
#
# At the optimum, ζ* = VaR_α and the objective equals CVaR_α. T scenarios
# are the rows of ``returns`` (the same per-period returns used elsewhere
# in this module). Solved with scipy's HiGHS backend.

def _cvar_lp(returns_arr: np.ndarray, alpha: float, lower: float = 0.0
             ) -> tuple[np.ndarray, float, float] | None:
    """Solve the Rockafellar-Uryasev LP for one confidence level α.

    Returns ``(w, var, cvar)`` where ``var`` is ζ* (the LP's VaR variable in
    return-space; positive means a loss) and ``cvar`` is the optimum
    objective. Returns ``None`` on solver failure.
    """
    R = np.asarray(returns_arr, dtype=float)
    T, N = R.shape
    if T < 2 or N < 1:
        return None
    if not (0.0 < alpha < 1.0):
        return None
    tail = (1.0 - alpha) * T
    if tail <= 0:
        return None

    # Decision variable order: [w (N), ζ (1), u (T)]
    n_vars = N + 1 + T
    c = np.zeros(n_vars)
    c[N] = 1.0
    c[N + 1:] = 1.0 / tail

    # Inequality A_ub x ≤ b_ub for u_t ≥ -R_t·w - ζ  ⇔  -R_t·w - ζ - u_t ≤ 0
    # Use sparse blocks; dense versions blow up for T > 500.
    neg_R = csr_matrix(-R)
    neg_one_col = csr_matrix(-np.ones((T, 1)))
    neg_eye_T = -speye(T, format="csr")
    A_ub = sphstack([neg_R, neg_one_col, neg_eye_T], format="csr")
    b_ub = np.zeros(T)

    # Equality Σ w = 1
    A_eq = csr_matrix(
        (np.ones(N), (np.zeros(N, dtype=int), np.arange(N))),
        shape=(1, n_vars),
    )
    b_eq = np.array([1.0])

    # Bounds: w_i ∈ [lower, 1]; ζ free; u_t ≥ 0.
    bounds = ([(lower, 1.0)] * N
              + [(None, None)]
              + [(0.0, None)] * T)

    res = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                  bounds=bounds, method="highs")
    if not res.success:
        return None
    x = res.x
    w = np.clip(x[:N], lower, 1.0)
    # Renormalise tiny floating drift so Σ w = 1 to machine precision.
    s = float(w.sum())
    if s > 1e-12:
        w = w / s
    var = float(x[N])
    cvar = float(res.fun)
    return w, var, cvar


def min_cvar_portfolio(returns: pd.DataFrame, alpha: float,
                       floor: float = 0.0) -> dict | None:
    """Long-only min-CVaR portfolio at confidence level α.

    Returns ``{weights: {sym: w}, cvar, var}`` (CVaR and VaR are in
    per-period return units, sign convention: positive = loss). Returns
    ``None`` on solver failure.
    """
    if returns is None or returns.empty:
        return None
    symbols = list(returns.columns)
    res = _cvar_lp(returns.values, float(alpha), lower=max(0.0, float(floor)))
    if res is None:
        return None
    w, var, cvar = res
    return {"weights": {s: float(w[i]) for i, s in enumerate(symbols)},
            "cvar": cvar, "var": var}


def cvar_curve(returns: pd.DataFrame, alphas: Iterable[float],
               mu: pd.Series, cov: pd.DataFrame, rf: float = 0.0,
               floor: float = 0.0) -> list[dict]:
    """Build a curve of min-CVaR portfolios across confidence levels.

    For each α in ``alphas`` we solve the Rockafellar-Uryasev LP and project
    the resulting weight vector into the same (vol, ret) coordinate system
    used by the MV frontier — that lets the dashboard render the two
    curves on a shared chart. Output is one dict per α with keys
    ``conf, ret, vol, sharpe, cvar, var, weights``.

    Order follows the input ``alphas``; the dashboard passes them in
    descending order (99 → 50) so slider index 0 = strict tail.
    """
    if returns is None or returns.empty:
        return []
    symbols = list(returns.columns)
    # Align mu/cov to the returns' column order so the (vol, ret) projection
    # below uses consistent indexing with the LP solution.
    mu_aln = mu.reindex(symbols).values.astype(float)
    cov_aln = cov.reindex(index=symbols, columns=symbols).values.astype(float)
    R = returns.values.astype(float)
    lower = max(0.0, float(floor))

    out: list[dict] = []
    for a in alphas:
        af = float(a)
        if not (0.0 < af < 1.0):
            continue
        res = _cvar_lp(R, af, lower=lower)
        if res is None:
            continue
        w, var, cvar = res
        r, v = _stats(w, mu_aln, cov_aln)
        sharpe = (r - rf) / v if v > 1e-12 else float("nan")
        out.append({
            "conf": af,
            "ret": float(r),
            "vol": float(v),
            "sharpe": float(sharpe) if math.isfinite(sharpe) else None,
            "cvar": float(cvar),
            "var": float(var),
            "weights": {s: float(w[i]) for i, s in enumerate(symbols)},
        })
    return out


def portfolio_stats(weights: dict[str, float], mu: pd.Series, cov: pd.DataFrame,
                    rf: float = 0.0) -> dict:
    symbols = list(mu.index)
    w = np.array([weights.get(s, 0.0) for s in symbols], dtype=float)
    s = w.sum()
    if s > 0:
        w = w / s
    r, v = _stats(w, mu.values.astype(float), cov.loc[symbols, symbols].values.astype(float))
    sharpe = (r - rf) / v if v > 1e-12 else float("nan")
    return {"ret": r, "vol": v, "sharpe": sharpe}
