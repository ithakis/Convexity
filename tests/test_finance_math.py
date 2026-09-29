"""Finance-math corrections from the 2026-09-29 audit (docs/METRICS_AUDIT.md).

Each test pins a convention the app now follows, against a hand calculation
or an independent reference implementation — never against the code's own
output. All offline: the network seams are monkeypatched.
"""

import math

import numpy as np
import pandas as pd
import pytest

from convexity import analytics as A
from convexity import fetcher, fx, mpt
from convexity import news_diagnostics as nd
from convexity.helpers import _rsi, _ytd_change, major_ccy, price_in_major


# ----------------------------------------------------------------- returns
def test_ytd_is_measured_from_last_years_close():
    idx = pd.to_datetime(["2025-12-30", "2025-12-31", "2026-01-02", "2026-01-05"])
    s = pd.Series([100.0, 100.0, 110.0, 121.0], index=idx)
    # From the 2025-12-31 close (100): +21%, not +10% from the first 2026 close.
    assert abs(_ytd_change(s) - 21.0) < 1e-12


def test_ytd_for_a_listing_from_this_year():
    idx = pd.to_datetime(["2026-03-02", "2026-03-03"])
    s = pd.Series([50.0, 55.0], index=idx)
    assert abs(_ytd_change(s) - 10.0) < 1e-12
    assert _ytd_change(s.iloc[:1]) is None


def test_period_slice_starts_at_the_last_close_on_or_before_start():
    idx = pd.to_datetime(["2025-12-30", "2025-12-31", "2026-01-02", "2026-01-05"])
    f = pd.DataFrame({"X": [1.0, 2.0, 3.0, 4.0]}, index=idx)
    out = A._period_slice(f, pd.Timestamp("2026-01-01"))
    assert list(out.index) == list(idx[1:])  # base = 2025-12-31
    exact = A._period_slice(f, pd.Timestamp("2026-01-02"))
    assert exact.index[0] == pd.Timestamp("2026-01-02")
    assert A._period_slice(f, pd.Timestamp("2020-01-01")).index[0] == idx[0]


def test_rsi_of_a_flat_series_is_neutral():
    assert _rsi(pd.Series([10.0] * 40), 14) == 50.0
    assert _rsi(pd.Series(np.arange(40, dtype=float)), 14) == 100.0


# ----------------------------------------------------------------- risk stats
def _curve(rets, start="2025-01-01"):
    idx = pd.bdate_range(start, periods=len(rets) + 1)
    val = pd.Series(np.cumprod([1.0, *(1.0 + np.asarray(rets))]), index=idx)
    return val.pct_change().dropna(), val


def test_sharpe_and_sortino_subtract_the_risk_free_rate():
    rng = np.random.default_rng(3)
    r = rng.normal(0.0006, 0.01, 500)
    ret, val = _curve(r)
    rf = pd.Series(0.0002, index=ret.index)
    s = A._stats(ret, val, rf)
    ex = r - 0.0002
    assert abs(s["sharpe"] - ex.mean() * 252 / (ex.std(ddof=1) * math.sqrt(252))) < 1e-9
    dd = math.sqrt(np.mean(np.minimum(ex, 0.0) ** 2))
    assert abs(s["sortino"] - ex.mean() * 252 / (dd * math.sqrt(252))) < 1e-9
    assert (
        abs(
            s["rf_ann"]
            - ((1.0002**500) ** (1 / ((val.index[-1] - val.index[0]).days / 365.25)) - 1) * 100
        )
        < 1e-6
    )
    s0 = A._stats(ret, val)
    assert s0["sharpe"] > s["sharpe"] and s0["rf_ann"] == 0.0


def test_daily_rf_from_irx_yield():
    idx = pd.bdate_range("2026-01-01", periods=5)
    wide = pd.DataFrame({"^IRX": [5.0, np.nan, 5.0, 5.0, 5.0]}, index=idx)
    rf = A._daily_rf(wide, idx, "USD")
    assert np.allclose(rf.values, 1.05 ** (1 / 252) - 1)
    assert A._daily_rf(wide, idx, "EUR") is None


def test_information_ratio():
    rng = np.random.default_rng(5)
    idx = pd.bdate_range("2025-01-01", periods=300)
    rb = pd.Series(rng.normal(0.0004, 0.01, 300), index=idx)
    rp = rb + pd.Series(rng.normal(0.0002, 0.004, 300), index=idx)
    out = A._relative(rp, rb)
    a = (rp - rb).to_numpy()
    assert abs(out["ir"] - a.mean() * 252 / (a.std(ddof=1) * math.sqrt(252))) < 1e-9
    assert abs(out["te"] - a.std(ddof=1) * math.sqrt(252) * 100) < 1e-9


# ----------------------------------------------------------------- aggregation
def test_harmonic_mean_is_the_look_through_pe():
    # Two holdings, 50/50 by value: P/E 10 and 40. Per $1 invested the book
    # earns 0.5/10 + 0.5/40 = 0.0625 → look-through P/E 16, not the mean 25.
    w = {"A": 0.5, "B": 0.5}
    assert abs(A._w_harmonic({"A": 10.0, "B": 40.0}, w) - 16.0) < 1e-12
    # Loss-makers (and missing data) drop out of both sums.
    w3 = {"A": 0.25, "B": 0.25, "C": 0.5}
    assert abs(A._w_harmonic({"A": 10.0, "B": 40.0, "C": -5.0}, w3) - 16.0) < 1e-12
    assert A._w_harmonic({"A": None}, {"A": 1.0}) is None
    assert abs(A._coverage({"A": 10.0, "B": 40.0, "C": -5.0}, w3) - 0.5) < 1e-12


def test_linked_contribution_adds_up_to_the_period_return():
    rng = np.random.default_rng(11)
    idx = pd.bdate_range("2025-01-01", periods=260)
    win = pd.DataFrame(rng.normal(0.001, 0.02, (260, 3)), index=idx, columns=list("XYZ"))
    w = pd.Series({"X": 0.5, "Y": 0.3, "Z": 0.2})
    c = A._linked_contribution(win, w)
    total = (np.prod(1.0 + (win * w).sum(axis=1)) - 1.0) * 100.0
    assert abs(sum(c.values()) - total) < 1e-9
    # A zero-weight holding contributes nothing.
    w0 = pd.Series({"X": 0.6, "Y": 0.4, "Z": 0.0})
    assert A._linked_contribution(win, w0)["Z"] == 0.0


def test_market_cap_usd_converts_and_uses_major_units(monkeypatch):
    monkeypatch.setattr(
        fx,
        "usd_per_unit",
        lambda c, **k: {"USD": 1.0, "JPY": 1 / 150, "GBP": 1.35}.get(fx._norm_ccy_for_fx(c)),
    )
    assert A._market_cap_usd({"market_cap_usd": 5e9}) == 5e9
    assert abs(A._market_cap_usd({"market_cap": 34e12, "currency": "JPY"}) - 34e12 / 150) < 1
    # LSE: price in GBp, cap already in GBP.
    assert abs(A._market_cap_usd({"market_cap": 208e9, "currency": "GBp"}) - 208e9 * 1.35) < 1
    assert A._market_cap_usd({"market_cap": 1e9, "currency": "XYZ"}) is None
    assert A._mcap_bucket(34e12 / 150) == "Mega ($200B+)"
    assert A._mcap_bucket(2.5e9) == "Mid ($2B–10B)"


def _analytics(monkeypatch, rows, weights, period="1Y", extra=None):
    tickers = [r["symbol"] for r in rows] + [t for t, _, _ in A._BENCHMARKS.values()]
    rng = np.random.default_rng(1)
    idx = pd.bdate_range(end="2026-09-25", periods=520)
    wide = pd.DataFrame(
        {s: 100 * np.cumprod(1 + rng.normal(0.0005, 0.012, len(idx))) for s in tickers}, index=idx
    )
    for k, v in (extra or {}).items():
        wide[k] = v
    monkeypatch.setattr(A, "_bulk_close", lambda syms, period: wide[[s for s in syms if s in wide]])
    monkeypatch.setattr(A, "_apply_fx_to_closes", lambda df, *a, **k: df)
    # Results are cached per (symbols, weights, period): isolate each test.
    monkeypatch.setattr(A, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(A, "_cache_put", lambda *a, **k: None)
    monkeypatch.setattr(
        A,
        "_analyst_for",
        lambda s, row=None: {
            "ev_ebitda": (row or {}).get("ev_ebitda"),
            "dist": (row or {}).get("rating_dist"),
        },
    )
    return A.analyze_portfolios_multi(rows, {"w": weights}, period)["w"]


def test_analytics_contribution_sums_and_multiples(monkeypatch):
    rows = [
        {
            "symbol": "AAA",
            "currency": "USD",
            "market_cap": 100e9,
            "pe_ratio": 10.0,
            "ps_ratio": 2.0,
        },
        {
            "symbol": "BBB",
            "currency": "USD",
            "market_cap": 300e9,
            "pe_ratio": 40.0,
            "ps_ratio": 8.0,
        },
    ]
    a = _analytics(monkeypatch, rows, {"AAA": 1, "BBB": 1})
    assert (
        abs(sum(c["contribution"] for c in a["contribution"]) - a["stats"]["total_return"]) < 1e-9
    )
    assert abs(a["weighted"]["pe"] - 16.0) < 1e-12
    assert abs(a["weighted"]["ps"] - 1 / (0.5 / 2 + 0.5 / 8)) < 1e-12
    assert abs(a["weighted"]["market_cap"] - 200e9) < 1
    assert a["weighted"]["market_cap_ccy"] == "USD"
    assert a["stats"]["rf_source"].startswith("0")  # no ^IRX in the frame


def test_analytics_uses_irx_in_usd(monkeypatch):
    rows = [{"symbol": "AAA", "currency": "USD"}, {"symbol": "BBB", "currency": "USD"}]
    a = _analytics(monkeypatch, rows, {"AAA": 1, "BBB": 1}, extra={"^IRX": 4.0})
    assert "IRX" in a["stats"]["rf_source"]
    # Daily rate (1.04)^(1/252) compounded over the window's trading days,
    # annualised by calendar time. The synthetic calendar trades all ~261
    # weekdays a year, so it lands slightly above 4% (a real one: ~4.0%).
    pts = a["series"]["portfolio"]
    n = len(pts) - 1
    years = (pts[-1][0] - pts[0][0]) / 86_400_000 / 365.25
    expected = (1.04 ** (n / 252)) ** (1 / years) - 1
    assert abs(a["stats"]["rf_ann"] - expected * 100) < 1e-6


def test_rating_distribution_weights_each_holdings_share(monkeypatch):
    rows = [
        {
            "symbol": "AAA",
            "currency": "USD",
            "rating_dist": {"strongBuy": 50, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0},
        },
        {
            "symbol": "BBB",
            "currency": "USD",
            "rating_dist": {"strongBuy": 0, "buy": 0, "hold": 0, "sell": 5, "strongSell": 0},
        },
    ]
    a = _analytics(monkeypatch, rows, {"AAA": 1, "BBB": 1})
    d = a["analyst"]["distribution_pct"]
    # Equal weights → 50/50, not 50 votes against 5 (91/9).
    assert abs(d["strongBuy"] - 50.0) < 1e-9 and abs(d["sell"] - 50.0) < 1e-9


def test_ytd_analytics_includes_the_first_session(monkeypatch):
    idx = pd.bdate_range("2025-12-01", "2026-03-31")
    px = pd.Series(100.0, index=idx)
    px[idx >= pd.Timestamp("2026-01-02")] = 110.0  # the whole move is on day one
    rows = [{"symbol": "AAA", "currency": "USD"}, {"symbol": "BBB", "currency": "USD"}]
    monkeypatch.setattr(
        A,
        "_bulk_close",
        lambda syms, period: pd.DataFrame({s: px for s in syms if s in ("AAA", "BBB", "SPY")}),
    )
    monkeypatch.setattr(A, "_apply_fx_to_closes", lambda df, *a, **k: df)
    monkeypatch.setattr(A, "_analyst_for", lambda s, row=None: {})
    monkeypatch.setattr(A, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(A, "_cache_put", lambda *a, **k: None)
    a = A.analyze_portfolios_multi(rows, {"w": {"AAA": 1, "BBB": 1}}, "YTD")["w"]
    assert abs(a["stats"]["total_return"] - 10.0) < 1e-9


# ----------------------------------------------------------------- fundamentals
def test_minor_units():
    assert major_ccy("GBp") == "GBP" and major_ccy("ZAc") == "ZAR" and major_ccy("ILA") == "ILS"
    assert major_ccy("usd") == "USD"
    assert price_in_major(3654.5, "GBp") == 36.545 and price_in_major(10.0, "USD") == 10.0


def test_adr_multiples_are_rebuilt_in_the_statement_currency(monkeypatch):
    # TM, live on 2026-09-29: USD listing, JPY statements. Yahoo's own
    # EV/EBITDA 6.27 and P/B 15.3 against 11.9 / 0.91 on the Tokyo line.
    monkeypatch.setattr(fetcher, "usd_per_unit", lambda c, **k: {"USD": 1.0, "JPY": 1 / 150}.get(c))
    monkeypatch.setattr(
        fx, "usd_per_unit", lambda c, **k: {"USD": 1.0, "JPY": 1 / 150}.get(fx._norm_ccy_for_fx(c))
    )
    info = {
        "financialCurrency": "JPY",
        "marketCap": 222981734400,
        "totalRevenue": 51957024686080,
        "ebitda": 5600540884992,
        "totalDebt": 43953168580608,
        "totalCash": 13350560858112,
        "debtToEquity": 114.974,
        "freeCashflow": -3599962472448,
    }
    out = {"ev_ebitda": 6.266, "price_book": 15.3, "ps_ratio": 0.0}
    fetcher._currency_consistent(info, out, "USD")
    mc = 222981734400 * 150
    ev = mc + 43953168580608 - 13350560858112
    assert abs(out["ev_ebitda"] - ev / 5600540884992) < 1e-9
    assert 10.5 < out["ev_ebitda"] < 12.5
    equity = 43953168580608 / 1.14974
    assert abs(out["price_book"] - mc / equity) < 1e-9 and 0.7 < out["price_book"] < 1.1
    assert abs(out["fcf_yield"] - (-3599962472448 / mc)) < 1e-12
    assert out["market_cap_usd"] == 222981734400
    assert out["financial_currency"] == "JPY"


def test_same_currency_keeps_yahoo_multiples(monkeypatch):
    monkeypatch.setattr(fetcher, "usd_per_unit", lambda c, **k: 1.0)
    out = {"ev_ebitda": 29.5, "price_book": 46.0}
    fetcher._currency_consistent(
        {"financialCurrency": "USD", "marketCap": 4.9e12, "freeCashflow": 1.08e11}, out, "USD"
    )
    assert out["ev_ebitda"] == 29.5 and out["price_book"] == 46.0
    assert abs(out["fcf_yield"] - 1.08e11 / 4.9e12) < 1e-15


def test_adr_without_an_fx_rate_shows_nothing_rather_than_wrong(monkeypatch):
    monkeypatch.setattr(fetcher, "usd_per_unit", lambda c, **k: 1.0 if c == "USD" else None)
    monkeypatch.setattr(
        fx, "usd_per_unit", lambda c, **k: 1.0 if fx._norm_ccy_for_fx(c) == "USD" else None
    )
    out = {"ev_ebitda": 5.17, "price_book": 93.0}
    fetcher._currency_consistent(
        {
            "financialCurrency": "TWD",
            "marketCap": 2.3e12,
            "ebitda": 3.1e12,
            "totalDebt": 1e12,
            "totalCash": 3.5e12,
            "debtToEquity": 16.5,
            "freeCashflow": 7e11,
        },
        out,
        "USD",
    )
    assert out["ev_ebitda"] is None and out["price_book"] is None and out["fcf_yield"] is None


def test_negative_multiples_are_not_meaningful():
    assert fetcher._positive(-12.0) is None and fetcher._positive(0) is None
    assert fetcher._positive("15.2") == 15.2


def test_ttm_needs_four_quarters():
    df = pd.DataFrame({"q1": [1.0], "q2": [2.0], "q3": [3.0]}, index=["Net Income"])
    assert fetcher._ttm_statement_value(df, ["Net Income"]) is None
    df["q4"] = 4.0
    assert fetcher._ttm_statement_value(df, ["Net Income"]) == 10.0


# ----------------------------------------------------------------- covariance
def _covcor_reference(x: np.ndarray) -> float:
    """Ledoit & Wolf's covCor.m, transcribed line by line (term1..term4 kept
    unsimplified) — the independent reference for the shrinkage intensity."""
    t, n = x.shape
    x = x - x.mean(axis=0)
    sample = x.T @ x / t
    var = np.diag(sample)
    sqrtvar = np.sqrt(var)
    rbar = (np.sum(sample / np.outer(sqrtvar, sqrtvar)) - n) / (n * (n - 1))
    prior = rbar * np.outer(sqrtvar, sqrtvar)
    np.fill_diagonal(prior, var)
    y = x**2
    phimat = y.T @ y / t - 2 * (x.T @ x) * sample / t + sample**2
    phi = phimat.sum()
    term1 = (x**3).T @ x / t
    help_ = x.T @ x / t
    helpdiag = np.diag(help_)
    term2 = helpdiag[:, None] * sample
    term3 = help_ * var[:, None]
    term4 = var[:, None] * sample
    thetamat = term1 - term2 - term3 + term4
    np.fill_diagonal(thetamat, 0.0)
    rho = np.trace(phimat) + rbar * np.sum(np.outer(1 / sqrtvar, sqrtvar) * thetamat)
    gamma = np.linalg.norm(sample - prior, "fro") ** 2
    return float(max(0.0, min(1.0, (phi - rho) / gamma / t)))


@pytest.mark.parametrize("t,n,seed", [(60, 8, 0), (250, 12, 1), (750, 20, 2)])
def test_ledoit_wolf_intensity_matches_the_reference(t, n, seed):
    rng = np.random.default_rng(seed)
    f = rng.normal(0, 0.01, (t, 1))
    x = f @ rng.uniform(0.5, 1.5, (1, n)) + rng.normal(0, 0.012, (t, n))
    rets = pd.DataFrame(x)
    cov = rets.cov()
    out = mpt.ledoit_wolf_shrink(cov, rets).values
    s = cov.values
    sd = np.sqrt(np.diag(s))
    c = s / np.outer(sd, sd)
    rbar = c[~np.eye(n, dtype=bool)].mean()
    target = rbar * np.outer(sd, sd)
    np.fill_diagonal(target, np.diag(s))
    d = _covcor_reference(x)
    assert np.allclose(out, d * target + (1 - d) * s, atol=1e-15, rtol=1e-10)
    assert 0.0 < d < 1.0


# ----------------------------------------------------------------- Track record
def test_clustered_hit_rate_widens_for_same_day_calls():
    # 40 calls on 2 days, perfectly correlated within the day: 2 real draws.
    calls = [("d1", True)] * 20 + [("d2", False)] * 20
    c = nd.wilson_clustered(calls)
    iid = nd.wilson(20, 40)
    assert c["rate"] == iid["rate"] == 0.5
    assert c["n_eff"] < 3 and (c["hi"] - c["lo"]) > (iid["hi"] - iid["lo"]) * 2


def test_clustered_hit_rate_matches_wilson_for_independent_days():
    rng = np.random.default_rng(7)
    hits = rng.random(400) < 0.6
    calls = [(f"d{i}", bool(h)) for i, h in enumerate(hits)]  # one call per day
    c = nd.wilson_clustered(calls)
    iid = nd.wilson(int(hits.sum()), 400)
    assert abs(c["lo"] - iid["lo"]) < 0.01 and abs(c["hi"] - iid["hi"]) < 0.01


def test_clustered_hit_rate_single_day_is_one_observation():
    c = nd.wilson_clustered([("d1", True), ("d1", False), ("d1", True)])
    assert c["n_eff"] == 1.0 and c["n"] == 3


# ----------------------------------------------------------------- optimizer
def test_black_litterman_view_adds_the_dividend_yield(monkeypatch):
    from convexity import frontier

    monkeypatch.setattr(
        frontier,
        "_analyst_for",
        lambda s, row=None: {"price": 100.0, "target_mean": 108.0, "n_analysts": 10},
    )
    by_sym = {"A": {"dividend_yield": 0.03}, "B": {}}
    views, detail = frontier._analyst_views(["A", "B"], by_sym, rf=0.04)
    assert abs(views["A"]["q"] - (0.08 + 0.03 - 0.04)) < 1e-12
    assert abs(views["B"]["q"] - (0.08 - 0.04)) < 1e-12
    assert abs(detail["A"]["upside_pct"] - 8.0) < 1e-9  # the price upside stays price-only
