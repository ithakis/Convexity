"""
Closed-form unit tests for every quantitative metric in Convexity.

Each test documents:
  - Standard formula being validated
  - Hand-derived expected value
  - Source code location being exercised

No network calls, no yfinance, no HTTP server required.
The analytics _stats() and _relative() helpers are imported and tested directly.
_normalize_dividend_yield is importable at module level from dashboard.py.
"""

import math
import sys
import os

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Import helpers from the app modules
# ---------------------------------------------------------------------------

# dashboard.py does heavy top-level work only when __name__ == '__main__' or
# when the HTTP handler runs, but importing it executes module-level code
# including yfinance imports. We use importlib with a path insertion and rely
# on the fact that dashboard.py is importable (CI smoke-test confirms this).
# Only _normalize_dividend_yield is imported directly from dashboard.py.

_REPO = os.path.dirname(os.path.dirname(__file__))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# Import mpt directly (pure numpy/pandas, no server deps).
from convexity import mpt

# Import _normalize_dividend_yield from the package (module-level function,
# importable once requirements.txt deps are installed). If the import fails
# (e.g. missing yfinance/numba on a stripped env) skip all dashboard tests
# explicitly — do NOT silently fall back to a local copy, which would make
# tests pass even when the package is broken or has diverged.
try:
    from convexity.helpers import _normalize_dividend_yield  # noqa: E402
    _DASHBOARD_IMPORT_ERROR = None
except Exception as _exc:
    _DASHBOARD_IMPORT_ERROR = str(_exc)
    _normalize_dividend_yield = None  # type: ignore[assignment]


def _require_dashboard():
    """Skip the calling test if dashboard.py could not be imported."""
    if _DASHBOARD_IMPORT_ERROR is not None:
        pytest.skip(f"dashboard.py not importable: {_DASHBOARD_IMPORT_ERROR}")


# The real analytics stats helpers (ret = daily returns, val = equity curve).
from convexity.analytics import _relative, _stats  # noqa: E402


# ---------------------------------------------------------------------------
# Helper — build a dated index of N business days
# ---------------------------------------------------------------------------

def _bday_index(n: int, start="2020-01-02"):
    return pd.bdate_range(start=start, periods=n)


# ===========================================================================
# 1. Sharpe Ratio
# ===========================================================================

def test_sharpe():
    """
    Formula: S = (E[R] * 252) / (σ * √252) = E[R] * √252 / σ
    Source:  dashboard.py:2510

    Daily returns: [0.01, -0.005, 0.02, 0.0, -0.01]
    mean  = (0.01 - 0.005 + 0.02 + 0.0 - 0.01) / 5 = 0.015 / 5 = 0.003
    var   = [(0.01-0.003)² + (-0.005-0.003)² + (0.02-0.003)² + (0.0-0.003)² + (-0.01-0.003)²] / 4
          = [49e-6 + 64e-6 + 289e-6 + 9e-6 + 169e-6] / 4
          = 580e-6 / 4 = 145e-6
    std   = sqrt(145e-6) ≈ 0.012042
    Sharpe = 0.003 * √252 / 0.012042 ≈ 3.9686
    """
    rets = [0.01, -0.005, 0.02, 0.0, -0.01]
    r = pd.Series(rets, index=_bday_index(5))
    mean = r.mean()
    std = r.std()
    expected = mean * math.sqrt(252) / std
    val = pd.Series([100.0] + [100.0 * np.prod([1 + x for x in rets[:i+1]]) for i in range(5)],
                    index=pd.bdate_range(start="2020-01-01", periods=6))
    s = _stats(r, val)
    assert s["sharpe"] is not None
    assert abs(s["sharpe"] - expected) < 1e-10


# ===========================================================================
# 2. Sortino Ratio (standard semi-deviation)
# ===========================================================================

def test_sortino_standard_formula():
    """
    Formula: S_o = (E[R] * 252) / (σ_down * √252)
    where σ_down = sqrt( mean( min(r_i, 0)² ) )  — all N periods in denominator
    Source:  dashboard.py:2511-2512 (post-fix)

    Daily returns: [0.01, -0.005, 0.02, 0.0, -0.01], MAR = 0
    min(r, 0) → [0, -0.005, 0, 0, -0.01]
    squared   → [0, 25e-6,  0, 0, 100e-6]
    mean      = 125e-6 / 5 = 25e-6
    σ_down_daily = sqrt(25e-6) = 0.005
    mean daily   = 0.003
    Sortino = (0.003 * 252) / (0.005 * √252) = 0.756 / 0.07937 ≈ 9.525
    """
    rets = [0.01, -0.005, 0.02, 0.0, -0.01]
    r = pd.Series(rets, index=_bday_index(5))
    # Hand derivation
    neg_sq = [min(x, 0) ** 2 for x in rets]
    sigma_down = math.sqrt(sum(neg_sq) / len(neg_sq))   # = 0.005
    expected = (r.mean() * 252) / (sigma_down * math.sqrt(252))

    val = pd.Series([100.0] + [100.0 * np.prod([1 + x for x in rets[:i+1]]) for i in range(5)],
                    index=pd.bdate_range(start="2020-01-01", periods=6))
    s = _stats(r, val)
    assert s["sortino"] is not None
    assert abs(s["sortino"] - expected) < 1e-10


def test_sortino_differs_from_buggy_formula():
    """
    Confirms the old buggy formula (ret[ret<0].std()) gives a DIFFERENT
    (larger) value than the standard semi-deviation formula.
    With returns [0.01, -0.005, 0.02, 0.0, -0.01]:

    Buggy:  neg_rets = [-0.005, -0.01], mean_neg = -0.0075
            std_neg = sqrt(((−0.005+0.0075)²+(−0.01+0.0075)²)/1) = sqrt(3.125e-6/1) = 0.002500
            Sortino_buggy = (0.003*252)/(0.002500*√252) ≈ 19.05  <-- inflated

    Standard: σ_down = 0.005, Sortino ≈ 9.53                     <-- correct
    """
    rets = [0.01, -0.005, 0.02, 0.0, -0.01]
    r = pd.Series(rets)

    # Standard formula (what the code should use)
    downside_standard = math.sqrt((r.clip(upper=0) ** 2).mean())
    sortino_standard = (r.mean() * 252) / (downside_standard * math.sqrt(252))

    # Old buggy formula
    neg = r[r < 0]
    downside_buggy = neg.std()
    sortino_buggy = (r.mean() * 252) / (downside_buggy * math.sqrt(252))

    # Buggy formula produces a larger (overestimated) Sortino (typically 30–60%)
    assert sortino_buggy > sortino_standard * 1.3, (
        f"Expected buggy ({sortino_buggy:.2f}) >> standard ({sortino_standard:.2f})"
    )


# ===========================================================================
# 3. Max Drawdown
# ===========================================================================

def test_max_drawdown():
    """
    Formula: DD_max = min_t( V_t / max_{s≤t}(V_s) - 1 ) * 100
    Source:  dashboard.py:2513-2514

    Equity curve: [100, 110, 95, 105, 80, 90]
    Running max:  [100, 110, 110, 110, 110, 110]
    Ratios:       [1.0, 1.0, 0.8636, 0.9545, 0.7273, 0.8182]
    Min ratio:    0.7273  →  DD = (0.7273 - 1) * 100 = -27.27%  (80/110 - 1)
    """
    prices = [100.0, 110.0, 95.0, 105.0, 80.0, 90.0]
    idx = pd.bdate_range(start="2020-01-01", periods=6)
    val = pd.Series(prices, index=idx)
    rets = val.pct_change().dropna()
    s = _stats(rets, val)
    expected_dd = (80.0 / 110.0 - 1.0) * 100.0
    assert abs(s["max_dd"] - expected_dd) < 1e-9


# ===========================================================================
# 4. Calmar Ratio
# ===========================================================================

def test_calmar():
    """
    Formula: C = R_ann / |DD_max|   (both in %)
    Source:  dashboard.py:2515

    ann_return = 15%, max_dd = -20%  →  Calmar = 15 / 20 = 0.75
    """
    ann_return = 15.0
    max_dd = -20.0
    calmar = ann_return / abs(max_dd)
    assert abs(calmar - 0.75) < 1e-12


def test_calmar_from_stats():
    """Calmar from _stats() matches hand calculation for a known equity curve."""
    # Build a curve where we know ann_return ≈ 10% and max_dd = -10%
    # 1 year of daily data, steady growth except one dip
    n = 252
    idx = pd.bdate_range(start="2020-01-02", periods=n)
    # linear growth 0→10% with a -10% dip at midpoint
    base = np.linspace(100, 110, n)
    curve = base.copy()
    curve[n // 2] = 99.0   # dip to create a clear max drawdown
    val = pd.Series(curve, index=idx)
    rets = val.pct_change().dropna()
    s = _stats(rets, val)
    assert s["calmar"] is not None
    # Calmar should be positive (positive ann_return, negative max_dd)
    assert s["calmar"] > 0
    # ann_return / |max_dd| should match
    assert abs(s["calmar"] - s["ann_return"] / abs(s["max_dd"])) < 1e-9


# ===========================================================================
# 5. Beta, R², tracking error (analytics._relative)
# ===========================================================================

def test_relative_exact_leverage():
    """
    analytics._relative: β = Cov/Var, R² = Corr², TE = √252·σ(rp − rb)·100.
    rp = 2·rb exactly  →  β = 2, R² = 1, rp − rb = rb  →  TE = √252·σ(rb)·100.
    """
    rb = pd.Series(np.random.default_rng(0).normal(0, 0.01, 60), index=_bday_index(60))
    rel = _relative(2 * rb, rb)
    assert abs(rel["beta"] - 2.0) < 1e-12
    assert abs(rel["r2"] - 1.0) < 1e-12
    assert abs(rel["te"] - rb.std() * math.sqrt(252) * 100.0) < 1e-9


def test_relative_partial_and_identity():
    """R² < 1 with idiosyncratic noise; TE = 0 against itself; <30 days → {}."""
    rng = np.random.default_rng(42)
    rb = pd.Series(rng.normal(0, 0.01, 100), index=_bday_index(100))
    rp = rb + rng.normal(0, 0.005, 100)
    assert 0.0 < _relative(rp, rb)["r2"] < 1.0
    assert _relative(rb, rb)["te"] == 0.0
    assert _relative(rb[:20], rb[:20]) == {}


# ===========================================================================
# 8. Dividend Yield normalization
# ===========================================================================

def test_dividend_yield_fraction_input():
    """
    Yahoo returns yield as fraction 0.035 → pass through unchanged (<=1 branch).
    Source: dashboard.py:1774
    """
    _require_dashboard()
    result = _normalize_dividend_yield(0.035)
    assert abs(result - 0.035) < 1e-12


def test_dividend_yield_percent_input():
    """
    Yahoo returns yield as 3.5 (i.e. 3.5%) → divide by 100.
    Source: dashboard.py:1774  (val > 1.0 branch)
    """
    _require_dashboard()
    result = _normalize_dividend_yield(3.5)
    assert abs(result - 0.035) < 1e-12


def test_dividend_yield_none():
    """None input → None output."""
    _require_dashboard()
    assert _normalize_dividend_yield(None) is None


def test_dividend_yield_zero():
    """Yield of 0 is valid (no dividend) → 0.0."""
    _require_dashboard()
    result = _normalize_dividend_yield(0.0)
    assert result == 0.0


def test_dividend_yield_rate_over_price():
    """
    Prefer dividend_rate / price over raw yield field.
    dividend_rate=1.0, price=20.0  →  yield = 1.0/20.0 = 0.05
    Source: dashboard.py:1764-1768
    """
    _require_dashboard()
    result = _normalize_dividend_yield(
        raw_yield=0.99,          # should be ignored
        price=20.0,
        dividend_rate=1.0,
    )
    assert abs(result - 0.05) < 1e-12


def test_dividend_yield_high_percent():
    """
    Yahoo occasionally returns yields like 150 (meaning 150% — synthetic/error).
    divide by 100: 150 → 1.5 (kept as-is for caller to filter).
    Source: dashboard.py:1774
    """
    _require_dashboard()
    result = _normalize_dividend_yield(150.0)
    assert abs(result - 1.5) < 1e-12


def test_dividend_yield_trailing_rate_fallback():
    """
    When dividend_rate is not provided, trailing_rate is tried next.
    trailing_rate=2.0, price=40.0 → yield = 2.0/40.0 = 0.05
    Source: dashboard.py:1765-1768
    """
    _require_dashboard()
    result = _normalize_dividend_yield(
        raw_yield=0.99,   # should be ignored (rate/price takes priority)
        price=40.0,
        trailing_rate=2.0,
    )
    assert abs(result - 0.05) < 1e-12


def test_dividend_yield_trailing_yield_fallback():
    """
    When price/rate are unavailable, fall through to trailing_yield.
    trailing_yield=0.035 (already a fraction) → 0.035.
    Source: dashboard.py:1770-1774
    """
    _require_dashboard()
    result = _normalize_dividend_yield(
        raw_yield=None,
        trailing_yield=0.035,
    )
    assert abs(result - 0.035) < 1e-12


def test_dividend_yield_trailing_yield_percent():
    """
    trailing_yield=3.5 (percent representation) → 0.035.
    Source: dashboard.py:1774 (val > 1.0 branch)
    """
    _require_dashboard()
    result = _normalize_dividend_yield(
        raw_yield=None,
        trailing_yield=3.5,
    )
    assert abs(result - 0.035) < 1e-12


def test_dividend_yield_negative_raw_skipped():
    """
    Negative raw_yield is skipped; trailing_yield is used.
    Source: dashboard.py:1772 (val < 0 → continue)
    """
    _require_dashboard()
    result = _normalize_dividend_yield(
        raw_yield=-0.5,
        trailing_yield=0.02,
    )
    assert abs(result - 0.02) < 1e-12


def test_dividend_yield_price_zero_falls_to_raw():
    """
    price=0 means rate/price path is skipped; falls back to raw_yield.
    dividend_rate=1.0, price=0 → can't divide; raw_yield=0.04 → 0.04
    Source: dashboard.py:1764 (px > 0 guard)
    """
    _require_dashboard()
    result = _normalize_dividend_yield(
        raw_yield=0.04,
        price=0.0,
        dividend_rate=1.0,
    )
    assert abs(result - 0.04) < 1e-12


def test_dividend_yield_both_none_returns_none():
    """All inputs unavailable → None."""
    _require_dashboard()
    result = _normalize_dividend_yield(None, trailing_yield=None)
    assert result is None


# ===========================================================================
# 9. Annualised Return (CAGR)
# ===========================================================================

def test_ann_return_two_years():
    """
    Formula: (V_T/V_0)^(365.25/days) - 1
    Source:  dashboard.py:2508

    V_0=100, V_T=121, days=730 (2 years)
    CAGR = 1.21^(365.25/730) - 1 = 1.21^0.5 - 1 = 0.10 = 10%
    """
    start = pd.Timestamp("2020-01-02")
    end = pd.Timestamp("2022-01-02")
    idx = pd.bdate_range(start=start, end=end)
    n = len(idx)
    # Equity curve from 100 to 121 linearly
    curve = np.linspace(100.0, 121.0, n)
    val = pd.Series(curve, index=idx)
    rets = val.pct_change().dropna()
    s = _stats(rets, val)
    # Expected uses the actual index span that _stats sees
    days = (val.index[-1] - val.index[0]).days
    expected = ((val.iloc[-1] / val.iloc[0]) ** (365.25 / days) - 1.0) * 100.0
    assert abs(s["ann_return"] - expected) < 1e-9


# ===========================================================================
# 10. Annualised Volatility
# ===========================================================================

def test_ann_vol():
    """
    Formula: σ_ann = σ_daily * √252 * 100  (in %)
    Source:  dashboard.py:2509

    Constant daily return of 0.01 → std = 0 → ann_vol = 0%.
    Non-trivial: [0.01, -0.01, 0.01, -0.01, 0.01, -0.01]
    σ_daily = sample std = 0.010050...
    ann_vol = 0.010050 * √252 * 100 ≈ 15.95%
    """
    rets_vals = [0.01, -0.01, 0.01, -0.01, 0.01, -0.01]
    r = pd.Series(rets_vals, index=_bday_index(6))
    expected_ann_vol = r.std() * math.sqrt(252) * 100.0
    val = pd.Series([100.0] + [100.0 * np.prod([1 + x for x in rets_vals[:i+1]]) for i in range(6)],
                    index=pd.bdate_range(start="2020-01-01", periods=7))
    s = _stats(r, val)
    assert abs(s["ann_vol"] - expected_ann_vol) < 1e-10



# ===========================================================================
# 11. Covariance estimators (annualized_cov: sample / ledoit / ewma)
# ===========================================================================

def _toy_returns(seed=0, T=200, n=4):
    """Reproducible daily returns with distinct per-asset mean/vol."""
    rng = np.random.default_rng(seed)
    means = np.array([0.001, 0.0008, 0.0012, 0.0005])[:n]
    vols = np.array([0.02, 0.015, 0.03, 0.01])[:n]
    x = rng.standard_normal((T, n)) * vols + means
    return pd.DataFrame(x, columns=[f"A{i}" for i in range(n)])


def test_annualized_cov_sample_scales_by_periods():
    """Sample estimator = returns.cov() × periods-per-year. Source: mpt.annualized_cov."""
    rets = _toy_returns()
    cov = mpt.annualized_cov(rets, "daily", "sample")
    expected = rets.cov() * 252
    assert np.allclose(cov.values, expected.values, atol=1e-12)


def test_annualized_cov_ledoit_is_psd_and_unit_consistent():
    """Ledoit-Wolf default must stay PSD and actually shrink (intensity computed
    in per-period units, then annualized). Source: mpt.annualized_cov."""
    rets = _toy_returns(seed=2, T=120, n=4)
    cov = mpt.annualized_cov(rets, "daily", "ledoit")
    eig = np.linalg.eigvalsh(cov.values)
    assert np.all(eig >= -1e-10), f"not PSD: {eig.min()}"
    # Diagonal (variance) is barely shrunk; off-diagonals move toward the
    # constant-correlation target, so ledoit != sample in general.
    sample = mpt.annualized_cov(rets, "daily", "sample")
    assert not np.allclose(cov.values, sample.values)


def test_annualized_cov_ewma_psd():
    """EWMA estimator is symmetric PSD and annualized. Source: mpt.annualized_cov."""
    rets = _toy_returns(seed=5, T=250, n=4)
    cov = mpt.annualized_cov(rets, "daily", "ewma")
    assert np.allclose(cov.values, cov.values.T, atol=1e-12)
    assert np.all(np.linalg.eigvalsh(cov.values) >= -1e-10)


def test_annualized_cov_unknown_model_falls_back_to_ledoit():
    rets = _toy_returns()
    a = mpt.annualized_cov(rets, "daily", "nonsense")
    b = mpt.annualized_cov(rets, "daily", "ledoit")
    assert np.allclose(a.values, b.values)


# ===========================================================================
# 12. Ledoit-Wolf shrinkage primitive
# ===========================================================================

def test_ledoit_wolf_one_asset():
    """One asset: target == sample cov, shrinkage is identity."""
    cov = pd.DataFrame([[0.04]], index=["A"], columns=["A"])
    result = mpt.ledoit_wolf_shrink(cov)
    assert abs(result.loc["A", "A"] - 0.04) < 1e-12


def test_ledoit_wolf_output_positive_semidefinite():
    """Shrunk matrix stays PSD. Source: mpt.ledoit_wolf_shrink."""
    rng = np.random.default_rng(0)
    rets = pd.DataFrame(rng.standard_normal((60, 5)) * 0.01,
                        columns=[f"A{i}" for i in range(5)])
    cov_shrunk = mpt.ledoit_wolf_shrink(rets.cov(), returns=rets)
    eig = np.linalg.eigvalsh(cov_shrunk.values)
    assert np.all(eig >= -1e-10), f"Negative eigenvalue: {eig.min()}"


# ===========================================================================
# 13. Black-Litterman posterior returns
# ===========================================================================

def _bl_setup(seed=0):
    rng = np.random.default_rng(seed)
    syms = ["A", "B", "C", "D"]
    rets = pd.DataFrame(rng.standard_normal((300, 4)) * 0.02, columns=syms)
    cov = mpt.annualized_cov(rets, "daily", "ledoit")
    mktw = {"A": 100, "B": 50, "C": 30, "D": 20}
    views = {"A": {"q": 0.25, "n": 20, "disp": 0.1},
             "C": {"q": -0.05, "n": 15, "disp": 0.15}}
    return syms, cov, mktw, views


def test_bl_collapses_to_prior_when_haircut_zero():
    """H → 0 ⇒ Ω → ∞ ⇒ posterior == prior. Source: mpt.black_litterman."""
    syms, cov, mktw, views = _bl_setup()
    prior = mpt.black_litterman(syms, cov, mktw, views, haircut=0.0)["mu"]
    post = mpt.black_litterman(syms, cov, mktw, views, haircut=1e-5)["mu"]
    for s in syms:
        assert abs(post[s] - prior[s]) < 1e-3


def test_bl_approaches_views_when_haircut_one():
    """H → 1 ⇒ Ω → 0 ⇒ posterior == views (total = q + rf) on viewed assets."""
    syms, cov, mktw, views = _bl_setup()
    rf = 0.04
    post = mpt.black_litterman(syms, cov, mktw, views, rf=rf, haircut=1.0)["mu"]
    for s, v in views.items():
        assert abs(post[s] - (v["q"] + rf)) < 1e-6


def test_bl_no_views_equals_prior():
    """No views ⇒ posterior == prior exactly, for any haircut."""
    syms, cov, mktw, _ = _bl_setup()
    prior = mpt.black_litterman(syms, cov, mktw, {}, haircut=0.0)["mu"]
    post = mpt.black_litterman(syms, cov, mktw, {}, haircut=0.9)["mu"]
    for s in syms:
        assert abs(post[s] - prior[s]) < 1e-12


def test_bl_delta_finite_positive_and_breakdown_complete():
    syms, cov, mktw, views = _bl_setup()
    out = mpt.black_litterman(syms, cov, mktw, views, haircut=0.5)
    assert 0 < out["delta"] < 50
    assert set(out) >= {"mu", "prior", "q", "delta", "tau", "haircut", "viewed", "no_view"}
    assert set(out["viewed"]) == set(views)
    assert set(out["no_view"]) == set(syms) - set(views)


# ===========================================================================
# 14. CVaR / CDaR / max-drawdown vs brute-force definitions
# ===========================================================================

def test_cvar_of_matches_tail_mean():
    """cvar_of == negated mean of the worst ceil((1-α)T) returns."""
    rng = np.random.default_rng(1)
    r = rng.standard_normal(500) * 0.02
    alpha = 0.95
    k = mpt._tail_count(len(r), alpha)
    brute = -np.sort(r)[:k].mean()
    assert abs(mpt.cvar_of(r, alpha) - brute) < 1e-12


def test_max_drawdown_hand_computed():
    """A path up to +25% then down to a trough 20% below the peak."""
    r = np.array([0.25, -0.20])  # 1.0 -> 1.25 -> 1.0 ; peak 1.25, trough 1.0
    assert abs(mpt.max_drawdown(r) - (1 - 1.0 / 1.25)) < 1e-12


def test_cdar_is_mean_of_worst_drawdowns():
    rng = np.random.default_rng(3)
    r = rng.standard_normal(300) * 0.02
    curve = np.cumprod(1 + r)
    dd = 1 - curve / np.maximum.accumulate(curve)
    k = mpt._tail_count(len(dd), 0.95)
    brute = np.sort(dd)[::-1][:k].mean()
    assert abs(mpt.cdar(r, 0.95) - brute) < 1e-12


# ===========================================================================
# 15. Mean-CVaR solver — certify _cvar_pdip against the HiGHS reference
# ===========================================================================

def _rand_problem(seed, N, T):
    rng = np.random.default_rng(seed)
    vols = rng.uniform(0.008, 0.03, N)
    means = rng.uniform(-5e-4, 15e-4, N)
    F = rng.standard_normal((T, 1)) * 0.01
    R = np.ascontiguousarray(rng.standard_normal((T, N)) * vols + means
                             + F * rng.uniform(0.3, 1.0, N))
    mu = means * 252
    return R, mu


def test_cvar_pdip_matches_highs_reference():
    """Custom interior-point solver == HiGHS reference on the objective (CVaR)
    to 1e-6 across many random problems / constraint modes."""
    from tests._cvar_reference import cvar_lp_reference
    worst = 0.0
    n_cases = 0
    for seed in range(25):
        N = int(np.random.default_rng(seed).integers(3, 18))
        T = int(np.random.default_rng(seed + 99).integers(120, 500))
        R, mu = _rand_problem(seed, N, T)
        for alpha in (0.90, 0.95, 0.99):
            for fully in (True, False):
                l = np.zeros(N); h = np.ones(N)
                a_ret = mu if fully else (mu - 0.04)
                kappa = 1.0 / ((1 - alpha) * T)
                w, _, _, _ = mpt._cvar_pdip(R, a_ret, -1e18, l, h, kappa, 1 if fully else 0, 80)
                ref = cvar_lp_reference(R, a_ret, -1e18, l, h, alpha, fully)
                assert ref is not None
                # Compare empirical CVaR of each solution (objective proxy).
                d = abs(mpt.cvar_of(R @ w, alpha) - mpt.cvar_of(R @ ref[0], alpha))
                worst = max(worst, d)
                n_cases += 1
    assert worst < 1e-6, f"worst |ΔCVaR| = {worst:.2e} over {n_cases} cases"


def test_cvar_pdip_respects_constraints():
    """Returned weights satisfy sum=1 / box / return floor."""
    R, mu = _rand_problem(7, 8, 300)
    l = np.full(8, 0.05); h = np.full(8, 0.40)
    kappa = 1.0 / ((1 - 0.95) * 300)
    # min-CVaR endpoint (floor disabled)
    w, _, _, conv = mpt._cvar_pdip(R, mu, -1e18, l, h, kappa, 1, 60)
    assert conv == 1
    assert abs(w.sum() - 1.0) < 1e-5
    assert (w >= l - 1e-6).all() and (w <= h + 1e-6).all()
    # with an active return floor at the midpoint
    w_mr = mpt._max_return_weights(mu, l, h, True)
    r_lo, r_hi = float(mu @ w), float(mu @ w_mr)
    if r_hi > r_lo + 1e-6:
        floor = r_lo + 0.5 * (r_hi - r_lo)
        w2, _, _, _ = mpt._cvar_pdip(R, mu, floor, l, h, kappa, 1, 80)
        assert (mu @ w2) >= floor - 1e-4


def test_cvar_pdip_cash_mode_allows_underinvestment():
    """fully_invested=0 permits Σw ≤ 1 (cash), still long-only in box."""
    R, mu = _rand_problem(11, 6, 250)
    l = np.zeros(6); h = np.ones(6)
    kappa = 1.0 / ((1 - 0.95) * 250)
    w, _, _, _ = mpt._cvar_pdip(R, mu - 0.04, -1e18, l, h, kappa, 0, 80)
    assert w.sum() <= 1.0 + 1e-5
    assert (w >= -1e-6).all()


# ===========================================================================
# 16. Mean-CVaR frontier + risk metrics
# ===========================================================================

def test_mean_cvar_frontier_monotone_and_shaped():
    """Frontier is min-CVaR → max-return, monotone (ret & cvar both nondecreasing),
    every point carries the full metric set. Source: mpt.mean_cvar_frontier."""
    R, mu_v = _rand_problem(4, 8, 400)
    df = pd.DataFrame(R, columns=[f"A{i}" for i in range(8)])
    mu = {f"A{i}": float(mu_v[i]) for i in range(8)}
    cov = mpt.annualized_cov(df, "daily", "ledoit")
    out = mpt.mean_cvar_frontier(df, mu, alpha=0.95, n_points=20, cov=cov)
    assert out["ok"]
    fr = out["frontier"]
    assert len(fr) >= 2
    for i in range(len(fr) - 1):
        assert fr[i]["ret"] <= fr[i + 1]["ret"] + 1e-9
        assert fr[i]["cvar"] <= fr[i + 1]["cvar"] + 1e-9
    for p in fr:
        assert set(p) >= {"ret", "cvar", "vol", "mdd", "cdar", "weights"}
        assert abs(sum(p["weights"].values()) - 1.0) < 1e-4
    assert out["min_cvar"] is fr[0] and out["max_ret"] is fr[-1]


def test_mean_cvar_frontier_single_asset():
    """One asset degenerates to a single 100% point, no crash."""
    df = pd.DataFrame({"A": np.random.default_rng(0).standard_normal(100) * 0.01})
    out = mpt.mean_cvar_frontier(df, {"A": 0.1}, alpha=0.95)
    assert out["ok"] and len(out["frontier"]) == 1
    assert abs(out["frontier"][0]["weights"]["A"] - 1.0) < 1e-9


def test_mean_cvar_frontier_infeasible_box():
    """w_max·N < 1 for a fully-invested portfolio is infeasible → ok:False, no crash."""
    R, mu_v = _rand_problem(2, 5, 200)
    df = pd.DataFrame(R, columns=[f"A{i}" for i in range(5)])
    mu = {f"A{i}": float(mu_v[i]) for i in range(5)}
    out = mpt.mean_cvar_frontier(df, mu, alpha=0.95, w_max=0.1, fully_invested=True)
    assert out["ok"] is False and "error" in out


def test_portfolio_risk_metrics_shape():
    R, mu_v = _rand_problem(6, 5, 250)
    df = pd.DataFrame(R, columns=[f"A{i}" for i in range(5)])
    mu = {f"A{i}": float(mu_v[i]) for i in range(5)}
    cov = mpt.annualized_cov(df, "daily", "ledoit")
    w = {f"A{i}": 0.2 for i in range(5)}
    m = mpt.portfolio_risk_metrics(w, mu, cov, df, alpha=0.95)
    assert set(m) >= {"ret", "cvar", "vol", "mdd", "cdar", "weights"}
    assert m["cvar"] >= 0 and m["vol"] >= 0


def test_cvar_return_cloud_shape():
    """Light cloud returns [cvar, ret] pairs; empty for <2 assets."""
    R, mu_v = _rand_problem(8, 6, 300)
    df = pd.DataFrame(R, columns=[f"A{i}" for i in range(6)])
    mu = {f"A{i}": float(mu_v[i]) for i in range(6)}
    cloud = mpt.cvar_return_cloud(df, mu, alpha=0.95, n=800)
    assert len(cloud) >= 500
    assert all(len(p) == 2 for p in cloud[:10])
    assert mpt.cvar_return_cloud(df.iloc[:, :1], mu, alpha=0.95) == []


# ===========================================================================
# 17. Per-position bounds, bootstrap band, streaming orchestrator (v2 rework)
# ===========================================================================

def test_as_bound_vec_scalar_and_vector():
    """Scalar broadcasts; per-asset vector passes through; bad length falls back."""
    assert np.allclose(mpt._as_bound_vec(0.1, 4, 0.0), [0.1] * 4)
    assert np.allclose(mpt._as_bound_vec([0.0, 0.2, 0.5, 0.3], 4, 0.0), [0.0, 0.2, 0.5, 0.3])
    assert np.allclose(mpt._as_bound_vec([0.1, 0.2], 4, 0.9), [0.9] * 4)   # wrong length → default
    assert np.allclose(mpt._as_bound_vec(None, 3, 0.7), [0.7] * 3)
    assert (mpt._as_bound_vec([2.0, -1.0, 0.5], 3, 0.0) == [1.0, 0.0, 0.5]).all()  # clipped to [0,1]


def test_mean_cvar_frontier_per_position_bounds():
    """Per-asset min/max vectors are honoured at every frontier point."""
    R, mu_v = _rand_problem(5, 6, 350)
    df = pd.DataFrame(R, columns=[f"A{i}" for i in range(6)])
    mu = {f"A{i}": float(mu_v[i]) for i in range(6)}
    lo = [0.05, 0.0, 0.0, 0.10, 0.0, 0.0]
    hi = [0.30, 0.30, 0.30, 0.30, 0.30, 0.30]
    out = mpt.mean_cvar_frontier(df, mu, alpha=0.95, w_min=lo, w_max=hi, n_points=16)
    assert out["ok"]
    for p in out["frontier"]:
        w = p["weights"]
        assert w["A0"] >= 0.05 - 1e-6 and w["A3"] >= 0.10 - 1e-6
        assert all(v <= 0.30 + 1e-6 for v in w.values())


def test_mean_cvar_frontier_infeasible_per_position():
    """Σmin > 1 (either mode) and Σmax < 1 (fully invested) are rejected cleanly."""
    R, mu_v = _rand_problem(3, 4, 200)
    df = pd.DataFrame(R, columns=[f"A{i}" for i in range(4)])
    mu = {f"A{i}": float(mu_v[i]) for i in range(4)}
    out = mpt.mean_cvar_frontier(df, mu, w_min=[0.4, 0.4, 0.4, 0.4], fully_invested=True)
    assert out["ok"] is False and "per-position" in out["error"]
    # Σmin > 1 must also fail in cash mode (w ≥ l ⇒ Σw ≥ Σl > 1 contradicts Σw ≤ 1).
    out_cash = mpt.mean_cvar_frontier(df, mu, w_min=[0.4, 0.4, 0.4, 0.4], fully_invested=False)
    assert out_cash["ok"] is False and "per-position" in out_cash["error"]


def test_mean_cvar_frontier_pin_and_exclude_respected():
    """Regression: an equal box (pin min==max) and a zero max (exclude) must be
    honored exactly — the old `h <= l` repair silently widened both. Uses a pin
    value below 1/n and an exclude, the two cases that triggered the bug."""
    R, mu_v = _rand_problem(4, 6, 400)   # 1/n = 16.7%
    df = pd.DataFrame(R, columns=[f"A{i}" for i in range(6)])
    mu = {f"A{i}": float(mu_v[i]) for i in range(6)}
    lo = [0.04, 0.0, 0.0, 0.0, 0.0, 0.0]   # pin A0 = 4%
    hi = [0.04, 0.0, 1.0, 1.0, 1.0, 1.0]   # A0 pinned, A1 excluded (max 0)
    out = mpt.mean_cvar_frontier(df, mu, alpha=0.95, w_min=lo, w_max=hi, n_points=12)
    assert out["ok"]
    for p in out["frontier"]:
        assert abs(p["weights"]["A0"] - 0.04) < 1e-4, p["weights"]["A0"]  # pinned, not widened
        assert p["weights"]["A1"] < 1e-4, p["weights"]["A1"]              # excluded, not funded


def test_max_return_weights_funds_floor_in_cash_mode():
    """Regression: the closed-form max-return endpoint must fund a mandatory floor
    even on a negative-μ asset in cash mode (was seeded from zeros → floor ignored)."""
    mu_ex = np.array([-0.05, 0.10, 0.08])   # A0 return-unattractive but has a floor
    l = np.array([0.08, 0.0, 0.0])
    h = np.ones(3)
    w = mpt._max_return_weights(mu_ex, l, h, fully_invested=False)
    assert w[0] >= 0.08 - 1e-9, w          # floor funded despite negative μ
    assert w.sum() <= 1.0 + 1e-9
    # And end-to-end: the frontier's max_ret endpoint honors it too.
    R, mu_v = _rand_problem(5, 3, 300)
    df = pd.DataFrame(R, columns=["A0", "A1", "A2"])
    mu = {"A0": -0.05, "A1": 0.10, "A2": 0.08}
    out = mpt.mean_cvar_frontier(df, mu, alpha=0.95, w_min=[0.08, 0.0, 0.0],
                                 fully_invested=False, n_points=10)
    assert out["ok"] and out["max_ret"]["weights"]["A0"] >= 0.08 - 1e-4


def test_bootstrap_cvar_band_brackets_base():
    """bootstrap_cvar returns finite cvar_ann[B,K]; the base CVaR sits inside the
    10th–90th bootstrap band at most frontier points (sampling-uncertainty check)."""
    R, mu_v = _rand_problem(3, 7, 500)
    df = pd.DataFrame(R, columns=[f"A{i}" for i in range(7)])
    mu = {f"A{i}": float(mu_v[i]) for i in range(7)}
    out = mpt.mean_cvar_frontier(df, mu, alpha=0.95, n_points=14)
    ctx = out["_ctx"]
    arr = mpt.bootstrap_cvar(df.values, ctx, np.arange(1, 121, dtype=np.int64))
    K = len(out["frontier"])
    assert arr.shape == (120, K)
    assert np.isfinite(arr).all() and (arr >= 0).all()
    lo = np.percentile(arr, 10, axis=0)
    hi = np.percentile(arr, 90, axis=0)
    base = np.array([p["cvar"] for p in out["frontier"]])
    inside = ((base >= lo - 1e-9) & (base <= hi + 1e-9)).mean()
    assert inside >= 0.6, f"base CVaR inside band at only {inside:.0%} of points"


def test_cloud_kernel_parallel_matches_serial():
    """The prange cloud kernel is deterministic and order-independent (parallel-safe):
    a fixed weight matrix yields the same (cvar, ret) rows as a plain numpy compute."""
    rng = np.random.default_rng(0)
    N, T, Kp = 6, 400, 50
    R = np.ascontiguousarray(rng.standard_normal((T, N)) * 0.01)
    W = np.ascontiguousarray(rng.dirichlet(np.ones(N), size=Kp))
    mu = rng.uniform(0.0, 0.2, N)
    h = mpt.LIQ_HORIZON
    out = mpt._cloud_kernel(W, R, mu, 0.95, h)
    port = W @ R.T                      # [Kp, T]
    # reference: empirical CVaR of each portfolio's overlapping h-day returns
    M = T - h + 1
    ref_rh = np.vstack([mpt.overlapping_h_returns(port[j], h) for j in range(Kp)])
    k = mpt._tail_count(M, 0.95)
    ref_cvar = -np.sort(ref_rh, axis=1)[:, :k].mean(axis=1)
    assert np.allclose(out[:, 0], ref_cvar, atol=1e-8)
    assert np.allclose(out[:, 1], W @ mu, atol=1e-9)


def test_frontier_stream_progress_then_done():
    """compute_efficient_frontier_stream yields progress msgs then one done payload
    with the bootstrap band attached; max_seconds keeps it fast."""
    from convexity import frontier
    rng = np.random.default_rng(1)
    rows = []
    for i in range(6):
        rows.append({"symbol": f"A{i}", "price": 100.0, "market_cap": 1e11 * (i + 1),
                     "currency": "USD"})
    # Patch the data-fetch + views so the test never touches yfinance.
    idx = pd.date_range("2022-01-01", periods=400, freq="B")
    closes = pd.DataFrame(np.cumprod(1 + rng.standard_normal((400, 6)) * 0.01, axis=0) * 100,
                          index=idx, columns=[f"A{i}" for i in range(6)])
    orig_close, orig_views = frontier._bulk_close, frontier._analyst_views
    frontier._bulk_close = lambda syms, period: closes[[s for s in syms if s in closes.columns]]
    frontier._analyst_views = lambda active, by_sym, rf: ({}, {})
    try:
        msgs = list(frontier.compute_efficient_frontier_stream(
            rows, budget="light", max_seconds=0.0, haircut=0.25,
            bounds={"A0": {"min": 0.05, "max": 0.25}}))
    finally:
        frontier._bulk_close, frontier._analyst_views = orig_close, orig_views
    kinds = [m["type"] for m in msgs]
    assert kinds.count("done") == 1 and kinds[-1] == "done"
    assert "progress" in kinds
    prog = [m for m in msgs if m["type"] == "progress"]
    assert all(0.0 <= m["pct"] <= 100.0 and (m["eta"] is None or m["eta"] >= 0) for m in prog)
    res = [m for m in msgs if m["type"] == "done"][0]["result"]
    assert res["meta"]["n_boot"] >= 1 and res["params"]["bounds"] == {"A0": {"min": 0.05, "max": 0.25}}
    p0 = res["frontier"][0]
    assert {"cvar_lo", "cvar_med", "cvar_hi"} <= set(p0)
    assert p0["weights"]["A0"] >= 0.05 - 1e-6 and p0["weights"]["A0"] <= 0.25 + 1e-6


def test_frontier_stream_error_too_few_symbols():
    """A single-symbol request yields exactly one error message, no crash."""
    from convexity import frontier
    msgs = list(frontier.compute_efficient_frontier_stream([{"symbol": "AAA"}], budget="light"))
    assert msgs and msgs[-1]["type"] == "error"


def test_var_cvar_horizon_estimator():
    """10d overlapping VaR/CVaR: correct window count, √3 scaling, cvar ≥ var."""
    rng = np.random.default_rng(7)
    r = rng.standard_normal(750) * 0.012
    rh = mpt.overlapping_h_returns(r, 10)
    assert rh.size == r.size - 9                      # T − h + 1
    # unscaled reference at 10 days
    k = mpt._tail_count(rh.size, 0.95)
    worst = np.sort(rh)[:k]
    ref_cvar10 = -worst.mean()
    ref_var10 = -worst[-1]
    var30, cvar30 = mpt.var_cvar_horizon(r, 0.95)
    assert cvar30 >= var30                            # shortfall ≥ threshold
    assert abs(cvar30 - ref_cvar10 * np.sqrt(3.0)) < 1e-12   # √(30/10) scaling
    assert abs(var30 - ref_var10 * np.sqrt(3.0)) < 1e-12
    # degenerate: fewer obs than the horizon → single whole-period window (finite)
    assert mpt.overlapping_h_returns(r[:4], 10).size == 1


def test_asset_risk_stats_fields_and_finiteness():
    """Per-asset stats carry the expected keys and stay finite on normal data."""
    rng = np.random.default_rng(8)
    R = pd.DataFrame(rng.standard_normal((400, 3)) * 0.011 + 0.0003,
                     columns=["A", "B", "C"])
    st = mpt.asset_risk_stats(R, 0.95)
    assert set(st["A"]) == {"ret_ann", "ret_total", "var30", "cvar30"}
    for s in R.columns:
        assert all(np.isfinite(v) for v in st[s].values())
        assert st[s]["cvar30"] >= st[s]["var30"]


def test_frontier_stream_cloud_frontier_messages_and_payload():
    """The stream emits a `frontier` msg then `cloud` chunks; `done` omits the bulk
    cloud but the blocking wrapper reassembles it; 30-day risk fields are present
    and the displayed frontier is monotone in cvar30."""
    from convexity import frontier
    rng = np.random.default_rng(2)
    rows = [{"symbol": f"A{i}", "price": 100.0, "market_cap": 1e11 * (i + 1),
             "currency": "USD"} for i in range(6)]
    idx = pd.date_range("2022-01-01", periods=400, freq="B")
    closes = pd.DataFrame(np.cumprod(1 + rng.standard_normal((400, 6)) * 0.01, axis=0) * 100,
                          index=idx, columns=[f"A{i}" for i in range(6)])
    orig_close, orig_views = frontier._bulk_close, frontier._analyst_views
    frontier._bulk_close = lambda syms, period: closes[[s for s in syms if s in closes.columns]]
    frontier._analyst_views = lambda active, by_sym, rf: (
        {}, {"A0": {"price": 100.0, "target_mean": 120.0, "target_low": 100.0,
                    "target_high": 150.0, "upside_pct": 20.0, "n_analysts": 8, "disp": 0.42}})
    try:
        msgs = list(frontier.compute_efficient_frontier_stream(
            rows, budget="light", max_seconds=0.0))
        res = frontier.compute_efficient_frontier(rows, budget="light", max_seconds=0.0)
    finally:
        frontier._bulk_close, frontier._analyst_views = orig_close, orig_views
    kinds = [m["type"] for m in msgs]
    # a `frontier` message precedes the first `cloud` chunk, `done` is last
    assert "frontier" in kinds and "cloud" in kinds
    assert kinds.index("frontier") < kinds.index("cloud") < kinds.index("done")
    done = [m for m in msgs if m["type"] == "done"][0]["result"]
    assert done["cloud"] == []                        # bulk cloud not re-shipped
    assert "asset_stats" in done and "analyst_detail" in done
    assert done["analyst_detail"]["A0"]["upside_pct"] == 20.0
    p0 = done["frontier"][0]
    assert {"var30", "cvar30", "cvar30_lo", "cvar30_med", "cvar30_hi"} <= set(p0)
    xs = [p["cvar30"] for p in done["frontier"]]
    assert all(b >= a - 1e-9 for a, b in zip(xs, xs[1:]))   # monotone on display axis
    # blocking wrapper reassembles the streamed cloud
    assert len(res["cloud"]) >= 500 and all(len(pt) == 2 for pt in res["cloud"][:5])


def test_mpt_run_history_last_three(tmp_path, monkeypatch):
    """save_mpt_run keeps the last 3 runs newest-first, dedupes identical params,
    caps at 3; get_last_mpt_run/get_mpt_runs read them; legacy formats tolerated."""
    from convexity import persistence as P
    monkeypatch.setattr(P, "_MPT_FILE", tmp_path / "mpt.json")
    for a in (0.90, 0.95, 0.975, 0.99):                       # 4 distinct params
        P.save_mpt_run("View1", {"params": {"alpha": a}, "symbols": ["A", "B"]})
    runs = P.get_mpt_runs("View1")
    assert [r["params"]["alpha"] for r in runs] == [0.99, 0.975, 0.95]  # newest-first, cap 3
    assert P.get_last_mpt_run("View1")["params"]["alpha"] == 0.99
    # identical params → replace newest (dedupe), not append
    P.save_mpt_run("View1", {"params": {"alpha": 0.99}, "symbols": ["A", "B", "C"]})
    runs = P.get_mpt_runs("View1")
    assert len(runs) == 3 and runs[0]["symbols"] == ["A", "B", "C"]
    assert [r["params"]["alpha"] for r in runs] == [0.99, 0.975, 0.95]
    assert P.get_mpt_runs("Missing") == [] and P.get_last_mpt_run("Missing") is None
    # legacy single-dict format → tolerated as a one-element history
    P._write_mpt_raw({"runs": {"Old": {"id": "r1", "params": {"alpha": 0.95}}}})
    assert P.get_last_mpt_run("Old")["id"] == "r1"
    assert len(P.get_mpt_runs("Old")) == 1
    # legacy multi-run list format → newest entry is index 0
    P._write_mpt_raw({"runs": {"Leg": [{"id": "r2"}, {"id": "r1"}]}})
    assert P.get_last_mpt_run("Leg")["id"] == "r2"
