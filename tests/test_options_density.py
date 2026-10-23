"""options_density: the real-world price distribution behind the stock
window's σ/μ, checked on a synthetic Black-Scholes chain (no network).

A flat-vol chain has a known answer: the risk-neutral density is lognormal
with that vol, so every tilt can be checked against its targets."""

import datetime as dt
import math

import numpy as np
import pytest

from convexity import options_density as od

S0, R, VOL, T = 100.0, 0.04, 0.30, 1.0


def bs_chain(strikes, vol=VOL, s0=S0, r=R, t=T, spread=0.02):
    """Calls and puts priced by Black-Scholes, quoted ±1% around mid."""
    F, DF = s0 * math.exp(r * t), math.exp(-r * t)
    calls, puts = [], []
    for K in strikes:
        c = float(od.black_call(F, K, vol * vol * t, DF))
        p = c - DF * (F - K)
        for out, px in ((calls, c), (puts, p)):
            if px > 0.01:
                out.append(
                    {"strike": K, "bid": px * (1 - spread / 2), "ask": px * (1 + spread / 2)}
                )
    return calls, puts


@pytest.fixture(scope="module")
def rn():
    calls, puts = bs_chain(np.arange(40.0, 251.0, 2.5))
    return od.fit_density(S0, T, R, calls, puts)


def test_flat_vol_chain_recovers_the_lognormal(rn):
    assert rn["F"] == pytest.approx(S0 * math.exp(R * T), rel=1e-3)
    assert rn["sigma_q"] == pytest.approx(VOL, abs=0.005)
    assert od.quality(rn) == (True, "")


def test_b_with_kappa_one_and_no_premium_returns_q(rn):
    # beta 0 (no premium) and kappa 1: the targets are q's own moments.
    mean_ratio = float(np.trapezoid(rn["q"] * rn["F"] * np.exp(rn["x"]), rn["x"]) / S0)
    p, method, theta = od.tilt(rn, mean_ratio, rn["sigma_q"] ** 2 * T)
    assert method == "B"
    assert np.max(np.abs(p - rn["q"])) < 1e-3 * np.max(rn["q"])
    assert abs(theta[0]) < 1e-3 and abs(theta[1]) < 1e-3


def test_b_hits_its_mean_and_variance(rn):
    beta, erp, kappa = 1.2, 0.05, 0.9
    mk = od.market_curve(rn, beta, erp, kappa)
    st = od.curve_stats(rn, mk["p"])
    assert mk["method"] == "B"
    assert st["mean_ratio"] == pytest.approx(od.capm_ratio(rn, beta, erp), rel=1e-4)
    assert st["var_x"] == pytest.approx((kappa * rn["sigma_q"]) ** 2 * T, rel=1e-3)


def test_a_fallback_still_hits_its_mean(rn):
    # A variance no exponential-quadratic tilt can reach forces Approach A.
    target = od.capm_ratio(rn, 1.0, 0.05)
    p, method, theta = od.tilt(rn, target, 1e-8)
    assert method == "A" and theta[1] == 0.0
    assert od.curve_stats(rn, p)["mean_ratio"] == pytest.approx(target, rel=1e-4)


def test_the_gate_trips_on_a_truncated_chain():
    calls, puts = bs_chain(np.arange(85.0, 116.0, 2.5))
    rn = od.fit_density(S0, T, R, calls, puts)
    ok, why = od.quality(rn)
    assert not ok and "quotes" in why


def test_too_few_quotes_to_fit_is_thin():
    calls, puts = bs_chain([95.0, 100.0, 105.0])
    with pytest.raises(od.DensityError) as e:
        od.fit_density(S0, T, R, calls, puts)
    assert e.value.reason == "thin"


def test_svi_wings_respect_lees_bound():
    k = np.array([20.0, 40.0])
    for params in (
        [0.1, 1.0, 0.999, 0.0, 0.1],
        [0.1, 1.0, -0.999, 0.0, 0.1],
        [0.0, 0.5, 0.0, 0.0, 1.0],
    ):
        for sign in (1, -1):
            w = od.svi(params, sign * k)
            assert abs(w[1] - w[0]) / 20.0 <= 2.0 + 1e-9


def test_analysts_mixture_keeps_total_variance_and_averages_the_means(rn):
    kappa = 0.92
    targets = [100.0, 108.0, 115.0, 125.0]
    mx = od.analysts_mixture(rn, targets, 1.0, 0.05, kappa)
    st = od.curve_stats(rn, mx["p"])
    assert st["mean_ratio"] == pytest.approx(np.mean(targets) / S0, rel=1e-3)
    assert st["var_x"] == pytest.approx((kappa * rn["sigma_q"]) ** 2 * T, rel=0.02)


@pytest.mark.parametrize("T_", [0.5, 1.0, 1.4])
def test_target_at_expiry_is_the_full_trust_mixture_mean(rn, T_):
    """The chart's solid diamond: one target at the expiry, pro-rated before a
    year and carried at CAPM after, equal to the mixture's mean at full trust."""
    r = {**rn, "T": T_}
    targets = [100.0, 115.0, 140.0]
    at = od.target_at_expiry(r, targets, 1.0, 0.05)
    lr = np.log(np.array(targets) / S0) * min(T_, 1.0) + max(T_ - 1.0, 0.0) * (r["rf"] + 0.05)
    assert at == pytest.approx(S0 * np.mean(np.exp(lr)), rel=1e-12)
    if T_ == 1.0:
        assert at == pytest.approx(np.mean(targets))
    if T_ < 1.0:
        assert S0 < at < np.mean(targets)
    assert od.target_at_expiry(r, [float("nan")], 1.0, 0.05) is None


@pytest.mark.parametrize("w", [0.0, 0.5, 1.0])
def test_trust_keeps_that_share_of_the_excess_log_return(rn, w):
    beta, erp = 1.0, 0.05
    lc = math.log(od.capm_ratio(rn, beta, erp))
    mx = od.analysts_mixture(rn, [140.0], beta, erp, 0.92, w)
    expect = math.exp(lc + w * (math.log(140.0 / S0) - lc))
    assert od.curve_stats(rn, mx["p"])["mean_ratio"] == pytest.approx(expect, rel=1e-3)


def test_zero_trust_is_the_markets_curve(rn):
    """At trust 0 every firm sits on CAPM, the firms agree, and the blend is
    one tilt: the market's curve, which the page draws for 0%."""
    beta, erp, kappa = 1.2, 0.05, 0.92
    a = od.curve_stats(
        rn, od.analysts_mixture(rn, [90.0, 140.0, 175.0], beta, erp, kappa, 0.0)["p"]
    )
    m = od.curve_stats(rn, od.market_curve(rn, beta, erp, kappa)["p"])
    assert a["mean_ratio"] == pytest.approx(m["mean_ratio"], rel=1e-6)
    assert a["var_x"] == pytest.approx(m["var_x"], rel=1e-6)


def test_both_trust_curves_ship_together(rn):
    st = od._settings({})
    an = {"firms": [{"target": 120.0}, {"target": 150.0}]}
    cv, out = od._analysts_curves(rn, an, 365, 1.0, st, 0.92)
    assert set(cv) == {"analysts", "analysts_t50"}
    full, half = (od.curve_stats(rn, cv[k][0])["mean_ratio"] for k in ("analysts", "analysts_t50"))
    assert full == pytest.approx(out["at_expiry"] / S0, rel=1e-3)
    assert od.capm_ratio(rn, 1.0, st["erp"]) < half < full
    assert od._analysts_curves(rn, None, 365, 1.0, st, 0.92)[0] == {
        "analysts": (None, None), "analysts_t50": (None, None)}  # fmt: skip


def test_each_horizon_gets_its_own_at_expiry(rn):
    """analyst_targets.consensus hands out one cached dict to both horizons:
    the 3M computation must not move the 12M payload's diamond."""
    st, shared = od._settings({}), {"firms": [{"target": 140.0}]}
    _, a12 = od._analysts_curves(rn, shared, 365, 1.0, st, 0.92)
    _, a3 = od._analysts_curves({**rn, "T": 0.25}, shared, 91, 1.0, st, 0.92)
    assert "at_expiry" not in shared
    assert a12["at_expiry"] == pytest.approx(140.0)
    assert a3["at_expiry"] == pytest.approx(S0 * (140.0 / S0) ** 0.25)


def test_downsample_is_a_normalised_curve(rn):
    d = od.downsample(rn["x"], rn["q"])
    x, p = np.array(d["x"]), np.array(d["p"])
    assert len(x) == od.OUT_POINTS
    assert np.trapezoid(p, x) == pytest.approx(1.0, abs=2e-3)


def test_density_never_raises_and_caches_successes_only(monkeypatch):
    calls = {"n": 0}

    def fail(*a):
        calls["n"] += 1
        raise od.DensityError("no_options")

    def no_history(*a):
        raise od.DensityError("no_history")

    monkeypatch.setattr(od, "_compute", fail)
    monkeypatch.setattr(od, "_history", no_history)
    for _ in range(2):
        out = od.density("ZZZZ.XX", 365, 1.0, {})
        assert out == {
            "ok": False,
            "reason": "no_options",
            "detail": "",
            "symbol": "ZZZZ.XX",
            "horizon": 365,
        }
    assert calls["n"] == 2  # a failure is asked again, never cached

    def boom(*a):
        raise RuntimeError("yahoo")

    monkeypatch.setattr(od, "_compute", boom)
    assert od.density("ZZZZ.YY", 91, None, {})["reason"] == "error"

    good = {"ok": True, "chain_symbol": "OKAY"}

    def ok(*a):
        calls["n"] += 1
        return good

    monkeypatch.setattr(od, "_compute", ok)
    calls["n"] = 0
    assert od.density("OKAY", 365, 1.0, {}) is good
    assert od.density("OKAY", 365, 1.0, {}) is good
    assert calls["n"] == 1


def test_settings_and_horizon_snap():
    st = od._settings({"dist_erp": 0.04, "dist_measure": "Q", "dist_trust": 1.0})
    assert st["erp"] == 0.04 and st["measure"] == "Q"
    assert "haircut" not in st and "trust" not in st  # trust is the page's: both curves ship
    assert od._settings({"dist_measure": "X"})["measure"] == "P"
    assert od._settings(None)["kappa_stock"] == od.KAPPA_DEFAULT["stock"]


def test_a_nan_fit_fails_the_gate(rn):
    # NaN passes every comparison: the gate must say no, not let it through.
    for k in ("martingale_err", "svi_rmse_volpts"):
        ok, why = od.quality({**rn, k: float("nan")})
        assert not ok and why == "fit failed"


def test_targets_scale_to_the_expiry():
    # A 12-month target sets a 6-month mean at half its log return, and past a
    # year the market's return carries it on (AAPL's "12M" is 335 days, ARKK's 461).
    for t, expect in ((0.5, lambda lr, cp: 0.5 * lr), (1.5, lambda lr, cp: lr + 0.5 * cp)):
        calls, puts = bs_chain(np.arange(40.0, 251.0, 2.5), t=t)
        r = od.fit_density(S0, t, R, calls, puts)
        mx = od.analysts_mixture(r, [130.0, float("nan"), float("inf")], 1.0, 0.05, 0.92)
        got = math.log(od.curve_stats(r, mx["p"])["mean_ratio"])
        assert got == pytest.approx(expect(math.log(1.3), R + 0.05), rel=2e-3)


def mixture_chain(strikes, comps, s0=S0, r=R, t=T):
    """Prices from a mix of lognormals [(weight, mean/F, vol)]: arbitrage-free,
    skewed like an index, and E[S_T] = F exactly when the means average to 1."""
    F, DF = s0 * math.exp(r * t), math.exp(-r * t)
    calls, puts = [], []
    for K in strikes:
        c = sum(wt * float(od.black_call(F * m, K, v * v * t, DF)) for wt, m, v in comps)
        p = c - DF * (F - K)
        for out, px in ((calls, c), (puts, p)):
            if px > 0.01:
                out.append({"strike": K, "bid": px * 0.99, "ask": px * 1.01})
    return calls, puts


# 90% calm (15% vol), 10% crash regime (80% vol, centred 10% lower). Fitted to
# strikes within +-15% only, the SVI bends up past them, as SPY's 12M wing did.
STRESS = [(0.9, (1 - 0.1 * 0.9) / 0.9, 0.15), (0.1, 0.9, 0.80)]


@pytest.fixture(scope="module")
def truncated():
    return mixture_chain(np.arange(85.0, 115.01, 0.5), STRESS)


def test_gev_tails_rescue_a_chain_truncated_at_15pct(truncated, monkeypatch):
    rn = od.fit_density(S0, T, R, *truncated)
    assert od.quality(rn) == (True, "")
    assert abs(rn["martingale_err"]) < od.MARTINGALE_GATE
    t = rn["tails"]
    assert 85.0 <= t["left"]["strike"] < t["right"]["strike"] <= 115.0
    # The same chain with the SVI's own wings, as before the tails: the gate trips.
    monkeypatch.setattr(od, "_tails", lambda Kg, f, *a: (f, None))
    raw = od.fit_density(S0, T, R, *truncated)
    ok, why = od.quality(raw)
    assert not ok and why.startswith("tails off")
    assert raw["quotes"] == rn["quotes"] and raw["svi_rmse_volpts"] == rn["svi_rmse_volpts"]


def test_the_tails_moments_are_finite(truncated, rn):
    for r in (od.fit_density(S0, T, R, *truncated), rn):
        x, q = r["x"], r["q"]
        assert r["tails"]["right"]["xi"] < 0.5  # E[S] and Var[S] finite
        for n in (1, 2):
            assert math.isfinite(float(np.trapezoid(q * np.exp(n * x), x)))
        assert np.all(np.isfinite(q)) and np.all(q >= 0)
        assert max(q[0], q[-1]) < 1e-4 * q.max()  # no mass piled at the grid's ends
        st = od.curve_stats(r, q)
        assert math.isfinite(st["var_x"]) and st["var_x"] > 0


def test_tails_keep_a_flat_vol_chain_lognormal(rn):
    # Strikes 40..250 reach the 5% / 95% points: the tails start there and
    # must continue the lognormal, not reshape it.
    t = rn["tails"]
    assert t["left"]["cdf"] == pytest.approx(0.05, abs=0.005)
    assert t["right"]["cdf"] == pytest.approx(0.95, abs=0.005)
    assert abs(rn["martingale_err"]) < 0.002


def test_a_stale_quote_breaking_monotonicity_is_dropped():
    # DELL 12M: one stale far put (bid above the next strike's ask) bent the
    # smile into a Lee-slope kink and the mean 9% below F.
    calls, puts = bs_chain(np.arange(40.0, 251.0, 2.5))
    clean = od.fit_density(S0, T, R, calls, puts)
    bad = [dict(p) for p in puts]
    i = next(j for j, p in enumerate(bad) if p["strike"] == 55.0)
    bad[i].update(bid=bad[i + 1]["ask"] * 3, ask=bad[i + 1]["ask"] * 4)
    rn = od.fit_density(S0, T, R, calls, bad)
    assert rn["quotes"] == clean["quotes"] - 1
    assert rn["svi_rmse_volpts"] < 0.1 and od.quality(rn) == (True, "")


def test_no_expiry_near_the_horizon(monkeypatch):
    # A small cap listing nothing past 153 days has no 12M, and says why.
    exps = [("2026-12-18", 62), ("2027-03-19", 153)]
    monkeypatch.setattr(od, "_resolve_chain", lambda s: ("OPRX", None, exps))
    monkeypatch.setattr(od, "_history", lambda *a: (_ for _ in ()).throw(od.DensityError("x")))
    out = od.density("OPRX", 365, 1.0, {})
    assert out["reason"] == "no_expiry" and "153" in out["detail"]


# ------------------------- Sparse chains: the SSVI surface -------------------------


def ssvi_chain(t, strikes, rho=-0.4, eta=0.8, gamma=0.4, theta_per_year=0.06):
    """Quotes priced off a known SSVI surface: the fit must recover them."""
    F, DF = S0 * math.exp(R * t), math.exp(-R * t)
    calls, puts = [], []
    for K in strikes:
        w = float(od.ssvi(theta_per_year * t, math.log(K / F), rho, eta, gamma))
        c = float(od.black_call(F, K, w, DF))
        p = c - DF * (F - K)
        for out, px in ((calls, c), (puts, p)):
            if px > 0.01:
                out.append({"strike": K, "bid": px * 0.99, "ask": px * 1.01})
    return calls, puts


@pytest.fixture(scope="module")
def sparse_surface():
    # ~20 strikes per expiry, $5-10 apart, as PG's and PEP's chains list them.
    out = []
    for days in (62, 90, 153, 243, 335, 461, 790):
        t = days / 365
        sd = 0.25 * math.sqrt(t)
        ks = np.unique(np.round(S0 * np.exp(np.linspace(-2.2 * sd, 1.8 * sd, 22)) / 2.5) * 2.5)
        out.append(od._slice(S0, t, R, *ssvi_chain(t, ks)))
    return out


def test_a_sparse_slice_fails_alone_and_passes_on_the_surface(sparse_surface):
    tgt = next(s for s in sparse_surface if abs(s["T"] - 335 / 365) < 1e-9)
    alone = od._svi_density(tgt)
    ok, why = od.quality(alone)
    assert not ok and why.endswith("quotes")  # fewer than MIN_QUOTES
    rn = od.fit_surface(sparse_surface, tgt)
    assert rn["smile"] == "ssvi" and od.quality(rn) == (True, "")
    assert rn["surface_rmse_volpts"] < 0.2 and abs(rn["martingale_err"]) < 0.002
    assert rn["ssvi"]["rho"] == pytest.approx(-0.4, abs=0.05)
    # No butterfly arbitrage by construction: eta (1 + |rho|) <= 2.
    assert rn["ssvi"]["eta"] * (1 + abs(rn["ssvi"]["rho"])) <= 2 + 1e-9


def test_the_surface_gate_wants_coverage_and_enough_quotes(sparse_surface):
    tgt = sparse_surface[4]
    rn = od.fit_surface(sparse_surface, tgt)
    narrow = {**rn, "tails": {**rn["tails"], "left": {**rn["tails"]["left"], "cdf": 0.25}}}
    ok, why = od.quality(narrow)
    assert not ok and why.startswith("quotes cover only")
    few = od.fit_surface(sparse_surface[3:5], tgt)
    ok, why = od.quality(few)
    assert not ok and "expiries" in why


def test_the_surface_reads_a_maturity_no_expiry_has(sparse_surface):
    # An ADR listing to 7 months: 12M comes off the surface, flat vol
    # past the last expiry. The surface's ATM vol is sqrt(0.06) at every T.
    short = sparse_surface[:4]  # 62..243 days
    rn = od.surface_at(short, 1.0)
    assert rn["extrapolated"] == {"from_days": 243, "beyond": True}
    assert rn["atm_iv"] == pytest.approx(math.sqrt(0.06), abs=0.005)
    assert rn["F"] == pytest.approx(S0 * math.exp(R * 1.0), rel=2e-3)
    assert abs(rn["martingale_err"]) < 0.003 and od.quality(rn) == (True, "")
    # Between expiries, theta is interpolated: same vol, not extrapolated.
    mid = od.surface_at(sparse_surface[:3] + sparse_surface[4:], 243 / 365)
    assert mid["extrapolated"]["beyond"] is False
    assert mid["atm_iv"] == pytest.approx(math.sqrt(0.06), abs=0.005)


def test_extrapolation_needs_half_the_horizon():
    assert od._can_extrapolate([("a", 62), ("b", 216)], 365)
    assert not od._can_extrapolate([("a", 62), ("b", 153)], 365)
    assert not od._can_extrapolate([], 365)


def test_the_chain_with_the_most_open_interest_wins(monkeypatch):
    # A European listing has no options; its ADR does, its pink sheet barely.
    import yfinance

    from convexity import symbol_db

    class Tk:
        def __init__(self, sym):
            self.sym = sym

    exps = {"ZZADR": [("2027-01-15", 90)], "ZZPINK": [("2027-01-15", 90)], "ZZ.AS": []}
    monkeypatch.setattr(yfinance, "Ticker", Tk)
    monkeypatch.setattr(od, "_expiries", lambda tk: exps.get(tk.sym, []))
    monkeypatch.setattr(
        od, "_open_interest", lambda tk, e: {"ZZADR": 9071.0, "ZZPINK": 3.0}[tk.sym]
    )
    monkeypatch.setattr(symbol_db, "us_lines", lambda s: ["ZZPINK", "ZZADR"])
    monkeypatch.setattr(symbol_db, "discover_us_line", lambda s: None)
    sym, tk, got = od._resolve_chain("ZZ.AS")
    assert sym == "ZZADR" and got == exps["ZZADR"]
    monkeypatch.setattr(symbol_db, "us_lines", lambda s: [])
    with pytest.raises(od.DensityError) as e:
        od._resolve_chain("ZZ2.AS")
    assert e.value.reason == "no_options"


def test_hist_vol_is_annualised_like_an_implied_vol(monkeypatch):
    import pandas as pd

    rng = np.random.default_rng(7)
    r = rng.normal(0, 0.02, 252)
    monkeypatch.setattr(od, "_close", lambda sym, period: pd.Series(100 * np.exp(np.cumsum(r))))
    assert od._hist_vol("X") == pytest.approx(np.std(r[1:], ddof=1) * math.sqrt(252), rel=1e-6)
    monkeypatch.setattr(od, "_close", lambda sym, period: pd.Series(np.ones(50)))
    assert od._hist_vol("X") is None  # too short to say


# ------------------------------ FX and the history path ------------------------------


def test_affine_mix_adds_variance_and_keeps_the_mean():
    x = np.linspace(-2, 2, 4001)
    s = 0.2
    p = np.exp(-0.5 * ((x + s * s / 2) / s) ** 2)
    p /= np.trapezoid(p, x)
    y = od.affine_mix(x, p, x, 1.1, 0.05**2)
    mean = lambda q: float(np.trapezoid(q * np.exp(x), x))  # noqa: E731
    var = lambda q: float(np.trapezoid(q * x * x, x) - np.trapezoid(q * x, x) ** 2)  # noqa: E731
    assert mean(y) == pytest.approx(mean(p), rel=1e-4)
    assert var(y) == pytest.approx(1.1**2 * s * s + 0.05**2, rel=1e-3)


def test_quarterly_futures_codes():
    q = od._next_quarters()
    assert len(q) == 2 and q[0][1] < q[1][1]
    for code, wed in q:
        assert code[0] in "HMUZ" and wed.weekday() == 2 and 15 <= wed.day <= 21
        assert (wed - dt.date.today()).days > 7


def test_variance_forecast_reverts_to_the_long_run():
    import pandas as pd

    calm, wild = np.full(143, 0.01), np.full(13, 0.04)
    r = pd.Series(np.concatenate([calm, wild]))
    lr, st = float(np.mean(r**2)) * 52, 0.04**2 * 52
    assert od._forecast_var(r, 1e-6) == pytest.approx(st, rel=1e-3)
    assert lr < od._forecast_var(r, 1.0) < st
    assert od._forecast_var(r, 200.0) == pytest.approx(lr, rel=0.02)


def test_winsor_clips_a_glitch_week_only():
    import pandas as pd

    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0, 0.02, 156))
    r.iloc[50] = 0.19  # a secondary listing's phantom +19% week
    w = od._winsor(r)
    assert w.iloc[50] < 0.15 and (w.drop(50) == r.drop(50)).all()


def test_history_stands_in_when_options_fail(monkeypatch):
    thin = {
        "ok": False,
        "reason": "thin",
        "chain_symbol": "ASML",
        "quality": {"why": "smile fit 1.7"},
    }
    monkeypatch.setattr(od, "_compute", lambda *a: dict(thin))
    monkeypatch.setattr(
        od, "_history", lambda *a: {"ok": True, "source": "history", "chain_symbol": "ASML.AS"}
    )
    out = od.density("ASML.AS", 365, 0.5, {"dist_erp": 0.051})
    assert out["source"] == "history"
    assert out["fallback"] == {
        "reason": "thin",
        "detail": "",
        "chain_symbol": "ASML",
        "why": "smile fit 1.7",
    }


# ---------------- Robust chains: session clock, hygiene, anchors, snapshots ----------------


def _ny_ts(day: str, hh: int = 15) -> float:
    from zoneinfo import ZoneInfo

    return (
        dt.datetime.fromisoformat(f"{day}T{hh:02d}:30").replace(tzinfo=ZoneInfo(od._ET)).timestamp()
    )


def test_maturities_count_from_the_quotes_session_not_the_calendar():
    # Friday 16 Oct 2026's quotes read on Sunday: the session is Friday, so a
    # weekend read prices the same T as Friday's close did.
    q = {"strike": 100.0, "bid": 1.0, "ask": 1.1, "ts": _ny_ts("2026-10-16"), "oi": 5.0}
    old = {**q, "ts": _ny_ts("2026-10-09")}
    assert od._session([("2027-10-15", 0, [q, old], [])]) == dt.date(2026, 10, 16)
    # 23:30 New York is still Friday's session, though it is Saturday in UTC.
    late = {**q, "ts": _ny_ts("2026-10-16", 23)}
    assert od._session([("2027-10-15", 0, [late], [])]) == dt.date(2026, 10, 16)
    assert od._session([("2027-10-15", 0, [{**q, "ts": None}], [])]) is None


def test_min_tick_wings_and_dead_strikes_are_not_a_market():
    # NVO's January call at K=90 on a $39 stock, 0.03/0.04: its implied vol is
    # tick noise (a 14.7 vol-pt outlier that failed the whole surface).
    asof = _ny_ts("2026-10-16")
    live = {"strike": 40.0, "bid": 2.41, "ask": 2.5, "ts": asof, "oi": 15258.0}
    tick = {"strike": 90.0, "bid": 0.03, "ask": 0.04, "ts": asof, "oi": 12593.0}
    dead = {"strike": 44.0, "bid": 1.0, "ask": 1.2, "ts": asof - 30 * 86400, "oi": 0.0}
    held = {**dead, "strike": 46.0, "oi": 300.0}  # old trade, but open interest: kept
    got = od._live([live, tick, dead, held], 38.8, asof)
    assert [q["strike"] for q in got] == [40.0, 46.0]
    # Quotes without trade times (tests, snapshots) only meet the floor.
    assert len(od._live([{"strike": 40.0, "bid": 2.0, "ask": 2.2}], 38.8, asof)) == 1


def test_a_far_wing_past_the_sd_cut_is_dropped():
    calls, puts = bs_chain(np.arange(40.0, 251.0, 2.5), t=0.25)
    calls.append({"strike": 400.0, "bid": 0.5, "ask": 0.55})  # 0.3 vol, 3M: ~9 sd out
    sl = od._slice(S0, 0.25, R, calls, puts)
    assert sl["K"].max() < 400.0
    assert np.abs(sl["k"]).max() <= od.MAX_K_SD * 0.3 * math.sqrt(0.25) + 1e-9


def _surface_chains(days_quotes, session="2026-10-16"):
    """(expiry, days, calls, puts) off one SSVI surface; oi per expiry."""
    out = []
    base = dt.date.fromisoformat(session)
    for days, n, oi in days_quotes:
        t = days / 365
        sd = 0.25 * math.sqrt(t)
        ks = np.unique(np.round(S0 * np.exp(np.linspace(-2.2 * sd, 1.8 * sd, n)) / 2.5) * 2.5)
        calls, puts = ssvi_chain(t, ks)
        for q in (*calls, *puts):
            q.update(ts=_ny_ts(session), oi=oi)
        out.append(((base + dt.timedelta(days=days)).isoformat(), days, calls, puts))
    return out


def test_12m_reads_the_surface_between_the_liquid_anchors(monkeypatch):
    # The nearest expiry to a year is thin (NVO's September: 9 quotes);
    # June and the January LEAP either side are deep. 12M comes off the
    # surface at exactly 365 days, anchored on those two.
    chains = _surface_chains([(62, 22, 50), (90, 22, 400), (153, 22, 90), (243, 22, 130),
                              (335, 9, 22), (461, 22, 178), (790, 22, 40)])  # fmt: skip
    res = {"chains": chains, "session": dt.date(2026, 10, 16), "s0": None, "snapshot": None}
    monkeypatch.setattr(od, "_chains", lambda tk, sym: res)
    monkeypatch.setattr(od, "SLICE_MIN_QUOTES", 12)  # the thin September can't pass on its own
    exps = [(e, d) for e, d, *_ in chains]
    rn, expiry, days, ok, why, meta = od._options_rn(None, "ZZANCH", exps, 365, S0, R)
    assert ok, why
    assert days == 365 and expiry == "2027-10-16" and rn["T"] == pytest.approx(1.0)
    assert rn["anchors"] == [243, 461]
    assert rn["atm_iv"] == pytest.approx(math.sqrt(0.06), abs=0.005)
    assert meta == {"session": "2026-10-16", "snapshot": False}


def test_a_liquid_expiry_at_the_horizon_keeps_its_own_fit(monkeypatch):
    chains = _surface_chains([(62, 40, 50), (90, 40, 400), (153, 40, 90), (243, 40, 130),
                              (350, 40, 220), (461, 40, 178)])  # fmt: skip
    res = {"chains": chains, "session": dt.date(2026, 10, 16), "s0": None, "snapshot": None}
    monkeypatch.setattr(od, "_chains", lambda tk, sym: res)
    rn, expiry, days, ok, why, _ = od._options_rn(None, "ZZAT", [], 365, S0, R)
    assert ok and days == 350 and "anchors" not in rn


class _Tk:
    """A yfinance Ticker stand-in serving one quote table for every expiry."""

    def __init__(self, exps, bid_scale=1.0):
        import pandas as pd

        self.options = exps
        self._pd, self._scale = pd, bid_scale

    def option_chain(self, e):
        from types import SimpleNamespace

        t = (dt.date.fromisoformat(e) - dt.date(2026, 10, 16)).days / 365
        calls, puts = bs_chain(np.arange(60.0, 160.0, 5.0), t=t)
        stamp = self._pd.Timestamp(_ny_ts("2026-10-16"), unit="s", tz="UTC")

        def df(rows):
            return self._pd.DataFrame([
                {"strike": r["strike"], "bid": r["bid"] * self._scale, "ask": r["ask"] * self._scale,
                 "lastTradeDate": stamp, "openInterest": 100.0} for r in rows
            ])  # fmt: skip

        return SimpleNamespace(calls=df(calls), puts=df(puts))


def test_a_zeroed_chain_stands_in_with_the_last_good_snapshot(monkeypatch):
    exps = ["2027-01-15", "2027-06-17", "2028-01-21"]
    monkeypatch.setattr(od.dt, "date", _FrozenDate)
    good = od._chains(_Tk(exps), "ZZSNAP")
    assert good["session"] == dt.date(2026, 10, 16) and not good["snapshot"]
    assert good["chains"][0][1] == 91  # days from Friday's session
    od._snap_save("ZZSNAP", good, 100.0)
    od.cache._CACHE.pop("density-chains|ZZSNAP", None)
    zeroed = od._chains(_Tk(exps, bid_scale=0.0), "ZZSNAP")
    assert zeroed["snapshot"] == dt.date(2026, 10, 16) and zeroed["s0"] == 100.0
    assert len(zeroed["chains"]) == 3 and zeroed["chains"][0][2][0]["bid"] > 0
    # Older than SNAP_MAX_AGE days: no stand-in.
    od.cache._CACHE.pop("density-chains|ZZSNAP", None)
    monkeypatch.setattr(_FrozenDate, "_today", dt.date(2026, 10, 23))
    assert not od._chains(_Tk(exps, bid_scale=0.0), "ZZSNAP").get("snapshot")


class _FrozenDate(dt.date):
    _today = dt.date(2026, 10, 18)  # a Sunday

    @classmethod
    def today(cls):
        return cls._today


def test_analysts_drop_a_firm_target_in_another_lines_units():
    """AZN.L: the per-firm feed held one target of 198 (a US-line figure)
    against a 12,446p price; Yahoo's summary was in pence. The odd firm goes
    and the summary stands in, so the marks stay on the chart."""
    import pandas as pd

    when = pd.Timestamp(dt.datetime.now() - dt.timedelta(days=10))
    ud = pd.DataFrame({"Firm": ["X"], "ToGrade": ["Buy"], "currentPriceTarget": [198.0],
                       "priorPriceTarget": [190.0]}, index=pd.Index([when], name="GradeDate"))  # fmt: skip

    class Tk:
        upgrades_downgrades = ud
        analyst_price_targets = {
            "current": 12446.0,
            "low": 11617.0,
            "high": 19731.0,
            "median": 15995.5,
        }

    a = od._analysts(Tk())
    assert a["firms"] == [] and a["consensus"] == 15995.5 and a["low"] == 11617.0
    assert a["mean"] == 15995.5  # no summary mean: the median stands in
    Tk.analyst_price_targets = {**Tk.analyst_price_targets, "current": 200.0}
    assert od._analysts(Tk())["firms"][0]["target"] == 198.0


def test_analysts_carry_the_firms_mean():
    """One analyst number in the stock window: the firms' mean (the coverage
    card, the readout and the chart's diamonds all read it)."""
    import pandas as pd

    now = dt.datetime.now()
    idx = pd.Index([pd.Timestamp(now - dt.timedelta(days=d)) for d in (5, 6, 7)], name="GradeDate")
    ud = pd.DataFrame({"Firm": ["A", "B", "C"], "ToGrade": ["Buy"] * 3,
                       "currentPriceTarget": [100.0, 110.0, 150.0],
                       "priorPriceTarget": [None] * 3}, index=idx)  # fmt: skip

    class Tk:
        upgrades_downgrades = ud
        analyst_price_targets = {"current": 100.0, "mean": 999.0}

    a = od._analysts(Tk())
    assert a["n"] == 3 and a["consensus"] == 110.0
    assert a["mean"] == pytest.approx(120.0)
