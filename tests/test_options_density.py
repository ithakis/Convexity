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


def test_haircut_halves_the_excess_log_return(rn):
    beta, erp = 1.0, 0.05
    lc = math.log(od.capm_ratio(rn, beta, erp))
    mx = od.analysts_mixture(rn, [140.0], beta, erp, 0.92, haircut=True)
    expect = math.exp(lc + 0.5 * (math.log(140.0 / S0) - lc))
    assert od.curve_stats(rn, mx["p"])["mean_ratio"] == pytest.approx(expect, rel=1e-3)


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
    st = od._settings({"dist_erp": 0.04, "dist_measure": "Q", "dist_haircut": True})
    assert st["erp"] == 0.04 and st["measure"] == "Q" and st["haircut"] is True
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
