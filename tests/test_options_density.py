"""options_density: the real-world price distribution behind the stock
window's σ/μ, checked on a synthetic Black-Scholes chain (no network).

A flat-vol chain has a known answer: the risk-neutral density is lognormal
with that vol, so every tilt can be checked against its targets."""

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

    monkeypatch.setattr(od, "_compute", fail)
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


def test_no_expiry_near_the_horizon(monkeypatch):
    # A small cap listing nothing past 153 days has no 12M, and says why.
    exps = [("2026-12-18", 62), ("2027-03-19", 153)]
    monkeypatch.setattr(od, "_resolve_chain", lambda s: ("OPRX", None, exps))
    out = od.density("OPRX", 365, 1.0, {})
    assert out["reason"] == "no_expiry" and "153" in out["detail"]
