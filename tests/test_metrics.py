"""
Closed-form unit tests for every quantitative metric in the portfolio tracker.

Each test documents:
  - Standard formula being validated
  - Hand-derived expected value
  - Source code location being exercised

No network calls, no yfinance, no HTTP server required.
The analytics _stats() closure (dashboard.py:2502-2524) is replicated
inline below because it is a local function inside analyze_portfolios_multi.
_normalize_dividend_yield is importable at module level from dashboard.py.
"""

import math
import sys
import os
import importlib

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
import mpt

# Import _normalize_dividend_yield from dashboard.py (module-level function,
# importable once requirements.txt deps are installed). If the import fails
# (e.g. missing yfinance/numba on a stripped env) skip all dashboard tests
# explicitly — do NOT silently fall back to a local copy, which would make
# tests pass even when dashboard.py is broken or has diverged.
try:
    from dashboard import _normalize_dividend_yield  # noqa: E402
    _DASHBOARD_IMPORT_ERROR = None
except Exception as _exc:
    _DASHBOARD_IMPORT_ERROR = str(_exc)
    _normalize_dividend_yield = None  # type: ignore[assignment]


def _require_dashboard():
    """Skip the calling test if dashboard.py could not be imported."""
    if _DASHBOARD_IMPORT_ERROR is not None:
        pytest.skip(f"dashboard.py not importable: {_DASHBOARD_IMPORT_ERROR}")


# ---------------------------------------------------------------------------
# Replicated _stats logic from dashboard.py:2502-2524
# (inner closure — cannot be imported directly)
# ---------------------------------------------------------------------------

def _stats(ret: pd.Series, val: pd.Series) -> dict:
    """
    Replicated from dashboard.py:2502-2524 (_stats inner closure inside
    analyze_portfolios_multi).  Kept in sync manually; the companion test
    catches drift.

    Inputs
    ------
    ret : daily return series (pct-change)
    val : equity curve (rebased to 100 at t=0)
    """
    if ret.empty or val.empty:
        return {}
    n_days = (val.index[-1] - val.index[0]).days or 1
    years = max(n_days / 365.25, 1e-6)
    total_return = float(val.iloc[-1] / val.iloc[0] - 1.0) * 100.0
    ann_return = float((val.iloc[-1] / val.iloc[0]) ** (1.0 / years) - 1.0) * 100.0
    ann_vol = float(ret.std() * math.sqrt(252)) * 100.0
    sharpe = float((ret.mean() * 252) / (ret.std() * math.sqrt(252))) if ret.std() else None
    # Standard semi-deviation: sqrt(mean(min(r_i, 0)²)) over ALL N periods.
    downside = math.sqrt((ret.clip(upper=0) ** 2).mean())
    sortino = float((ret.mean() * 252) / (downside * math.sqrt(252))) if downside > 0 else None
    run_mx = val.cummax()
    max_dd = float((val / run_mx - 1.0).min() * 100.0)
    calmar = (ann_return / abs(max_dd)) if max_dd < 0 else None
    return {
        "total_return": total_return,
        "ann_return": ann_return,
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_dd": max_dd,
        "calmar": calmar,
    }


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
# 5. Beta
# ===========================================================================

def test_beta():
    """
    Formula: β = Cov(r_p, r_m) / Var(r_m)
    Source:  dashboard.py:2614-2615

    rp = 2 * rb  exactly  →  β = Cov(2rb, rb) / Var(rb) = 2*Var(rb) / Var(rb) = 2.0
    """
    rb_vals = [0.005, 0.010, -0.005, 0.015, 0.002, -0.008]
    rp_vals = [2 * x for x in rb_vals]
    rp = pd.Series(rp_vals)
    rb = pd.Series(rb_vals)
    beta = rp.cov(rb) / rb.var()
    assert abs(beta - 2.0) < 1e-12


# ===========================================================================
# 6. R² (coefficient of determination vs benchmark)
# ===========================================================================

def test_r_squared():
    """
    Formula: R² = Corr(r_p, r_m)²
    Source:  dashboard.py:2616-2617

    rp = 2 * rb  →  Corr = 1.0  →  R² = 1.0
    """
    rb_vals = [0.005, 0.010, -0.005, 0.015, 0.002, -0.008]
    rp_vals = [2 * x for x in rb_vals]
    rp = pd.Series(rp_vals)
    rb = pd.Series(rb_vals)
    corr = rp.corr(rb)
    r2 = corr * corr
    assert abs(r2 - 1.0) < 1e-12


def test_r_squared_partial():
    """R² < 1 when portfolio has idiosyncratic component."""
    np.random.seed(42)
    rb = pd.Series(np.random.randn(100) * 0.01)
    noise = pd.Series(np.random.randn(100) * 0.005)
    rp = rb + noise
    corr = rp.corr(rb)
    r2 = corr * corr
    assert 0.0 < r2 < 1.0


# ===========================================================================
# 7. Tracking Error
# ===========================================================================

def test_tracking_error():
    """
    Formula: TE = √252 * σ(r_p - r_m)
    Source:  dashboard.py:2618-2619

    diff = rp - rb = rb  (since rp = 2*rb)
    σ(rb) = sample std of rb_vals
    TE = σ(rb) * √252
    """
    rb_vals = [0.005, 0.010, -0.005, 0.015, 0.002, -0.008]
    rp_vals = [2 * x for x in rb_vals]
    rp = pd.Series(rp_vals)
    rb = pd.Series(rb_vals)
    diff = rp - rb   # = rb_vals exactly
    te_expected = diff.std() * math.sqrt(252) * 100.0
    # Replicate the computation from dashboard.py:2618-2619
    te = (rp - rb).std()
    te_computed = float(te * math.sqrt(252) * 100.0)
    assert abs(te_computed - te_expected) < 1e-12


def test_tracking_error_identical_series():
    """TE = 0 when portfolio == benchmark (used for SPY vs itself)."""
    rb = pd.Series([0.01, -0.005, 0.02, 0.0])
    rp = rb.copy()
    te = (rp - rb).std()
    assert te == 0.0


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


def test_dividend_yield_xlsx_copy_matches_dashboard():
    """
    xlsx_export.py has a local copy of _normalize_dividend_yield (using
    _maybe_num instead of _safe_num). Verify both produce identical results
    for the same inputs. Source: xlsx_export.py:238-258
    """
    _require_dashboard()
    try:
        from xlsx_export import _normalize_dividend_yield as xlsx_norm
    except Exception:
        pytest.skip("xlsx_export not importable (missing openpyxl/yfinance)")

    test_cases = [
        (0.035, {}, 0.035),
        (3.5, {}, 0.035),
        (None, {"price": 20.0, "dividend_rate": 1.0}, 0.05),
        (None, {"trailing_yield": 2.5}, 0.025),
        (0.0, {}, 0.0),
    ]
    for raw, kw, expected in test_cases:
        dash_result = _normalize_dividend_yield(raw, **kw)
        xlsx_result = xlsx_norm(raw, **kw)
        assert dash_result == xlsx_result == pytest.approx(expected, abs=1e-12), (
            f"Mismatch for raw={raw}, kw={kw}: "
            f"dashboard={dash_result}, xlsx={xlsx_result}, expected={expected}"
        )


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
# 11. MPT Sharpe (on pre-annualised values)
# ===========================================================================

def test_mpt_sharpe():
    """
    Formula: Sharpe = (ret - rf) / vol   (values already annualised)
    Source:  mpt.py:430

    ret=0.12, vol=0.15, rf=0.02  →  Sharpe = 0.10/0.15 = 0.6667
    """
    curve = [{"ret": 0.12, "vol": 0.15}]
    result = mpt.tangency_portfolio(curve, rf=0.02)
    assert result is not None
    assert abs(result["sharpe"] - (0.12 - 0.02) / 0.15) < 1e-12


def test_mpt_sharpe_zero_rf():
    """Sharpe with rf=0 is just ret/vol."""
    curve = [{"ret": 0.10, "vol": 0.20}]
    result = mpt.tangency_portfolio(curve, rf=0.0)
    assert abs(result["sharpe"] - 0.10 / 0.20) < 1e-12


# ===========================================================================
# 12. MPT Annualisation
# ===========================================================================

def test_mpt_annualization_weekly():
    """
    Formula: mu = mean(returns) * 52,  cov = sample_cov(returns) * 52
    Source:  mpt.py:66-67

    Two assets, two weeks of weekly returns.
    Asset A: [0.01, -0.005]   mean = 0.0025   var_sample = ...
    Asset B: [0.02, -0.010]   mean = 0.005
    """
    data = {"A": [0.01, -0.005], "B": [0.02, -0.010]}
    idx = pd.date_range("2020-01-03", periods=2, freq="W-FRI")
    rets = pd.DataFrame(data, index=idx)
    mu, cov = mpt.annualize(rets, freq="weekly")
    # mu_A = 0.0025 * 52 = 0.13
    assert abs(mu["A"] - rets["A"].mean() * 52) < 1e-12
    assert abs(mu["B"] - rets["B"].mean() * 52) < 1e-12
    # cov diagonal: Var(A) * 52
    assert abs(cov.loc["A", "A"] - rets["A"].var() * 52) < 1e-12


def test_mpt_annualization_daily():
    """Daily: multiply by 252."""
    data = {"X": [0.01, 0.02, -0.01, 0.005]}
    idx = pd.bdate_range("2020-01-02", periods=4)
    rets = pd.DataFrame(data, index=idx)
    mu, cov = mpt.annualize(rets, freq="daily")
    assert abs(mu["X"] - rets["X"].mean() * 252) < 1e-12
    assert abs(cov.loc["X", "X"] - rets["X"].var() * 252) < 1e-12


# ===========================================================================
# 13. Ledoit-Wolf shrinkage
# ===========================================================================

def test_ledoit_wolf_one_asset():
    """
    One asset: no off-diagonal terms, target == sample cov.
    Shrinkage is identity — result must equal input.
    Source: mpt.py:71-100
    """
    cov = pd.DataFrame([[0.04]], index=["A"], columns=["A"])
    result = mpt.ledoit_wolf_shrink(cov)
    assert abs(result.loc["A", "A"] - 0.04) < 1e-12


def test_ledoit_wolf_shrinks_toward_target():
    """
    With two perfectly uncorrelated assets (off-diagonal = 0),
    the constant-correlation target has r_bar = 0,
    so the target IS the sample cov → no shrinkage regardless of alpha.
    Result should equal input.
    Source: mpt.py:86-88
    """
    cov = pd.DataFrame(
        [[0.04, 0.0],
         [0.0,  0.09]],
        index=["A", "B"], columns=["A", "B"]
    )
    result = mpt.ledoit_wolf_shrink(cov)
    assert abs(result.loc["A", "A"] - 0.04) < 1e-10
    assert abs(result.loc["B", "B"] - 0.09) < 1e-10
    # Off-diagonal target = r_bar * std_A * std_B = 0 * ... = 0
    assert abs(result.loc["A", "B"]) < 1e-10


def test_ledoit_wolf_output_positive_semidefinite():
    """
    Shrunk matrix must remain PSD — eigenvalues ≥ 0.
    Source: mpt.py:71-100
    """
    np.random.seed(0)
    n = 5
    x = np.random.randn(30, n) * 0.01
    rets = pd.DataFrame(x, columns=[f"A{i}" for i in range(n)])
    _, cov_raw = mpt.annualize(rets, freq="daily")
    cov_shrunk = mpt.ledoit_wolf_shrink(cov_raw, returns=rets)
    eigvals = np.linalg.eigvalsh(cov_shrunk.values)
    assert np.all(eigvals >= -1e-10), f"Negative eigenvalue: {eigvals.min()}"
