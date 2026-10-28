"""options_density: the real-world price distribution behind the stock
window's σ/μ, checked on a synthetic Black-Scholes chain (no network).

A flat-vol chain has a known answer: the risk-neutral density is lognormal
with that vol, so every tilt can be checked against its targets."""

import datetime as dt
import math
import time

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
    mu, kappa = 0.04 + 1.2 * 0.05, 0.9
    mk = od.mean_curve(rn, mu, kappa)
    st = od.curve_stats(rn, mk["p"])
    assert mk["method"] == "B"
    assert st["mean_ratio"] == pytest.approx(math.exp(mu * T), rel=1e-4)
    assert st["var_x"] == pytest.approx((kappa * rn["sigma_q"]) ** 2 * T, rel=1e-3)


def test_a_fallback_still_hits_its_mean(rn):
    # A variance no exponential-quadratic tilt can reach forces Approach A.
    target = math.exp((rn["rf"] + 0.05) * T)
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


def _model_stub(monkeypatch, alpha=0.012):
    from convexity import capm

    def fake(symbol, T, *, s0, ccy, target=None, div_yield=None, prefs=None, **_):
        a = alpha if target else 0.0
        return {"mu": 0.04 + 0.05 + a, "mu_capm": 0.09, "alpha": a, "T": T, "s0": s0,
                "model_price": capm.model_price(s0, 0.09 + a, T),
                "capm_price": capm.model_price(s0, 0.09, T)}  # fmt: skip

    monkeypatch.setattr(capm, "model", fake)


def test_model_curves_hit_mu_on_and_mu_off(rn, monkeypatch):
    """One payload, two curves: "model" (mu on, with alpha) and "market"
    (mu off, pure CAPM); each means S0 exp(mu T), the model block's price."""
    _model_stub(monkeypatch)
    st = {**od._settings({}), "prefs": {}}
    cv, mdl = od._model_curves("X", rn, "USD", 1.0, {"mean": 130.0}, st, 0.92, 0.0)
    assert set(cv) == {"model", "market"}
    on, off = (od.curve_stats(rn, cv[k][0])["mean_ratio"] for k in ("model", "market"))
    assert on * S0 == pytest.approx(mdl["model_price"], rel=1e-4)
    assert off * S0 == pytest.approx(mdl["capm_price"], rel=1e-4)
    assert on > off


def test_no_targets_means_one_curve(rn, monkeypatch):
    _model_stub(monkeypatch)
    st = {**od._settings({}), "prefs": {}}
    cv, mdl = od._model_curves("X", rn, "USD", 1.0, None, st, 0.92, None)
    assert mdl["alpha"] == 0.0 and cv["model"] is cv["market"]


def test_the_listing_target_is_used_in_its_own_units(rn, monkeypatch):
    """A chain on another line (ratio != 1): the block is in chain units, the
    model reads the listing's price and target (x ratio), like its grid row."""
    seen = {}
    from convexity import capm

    def fake(symbol, T, *, s0, ccy, target=None, div_yield=None, prefs=None, **_):
        seen.update(s0=s0, target=target, ccy=ccy)
        return {"mu": 0.09, "mu_capm": 0.09, "alpha": 0.0}

    monkeypatch.setattr(capm, "model", fake)
    od._model_curves("NOV.DE", rn, "EUR", 0.9, {"mean": 120.0}, {"prefs": {}}, 0.92, 0.01)
    assert seen == {"s0": pytest.approx(S0 * 0.9), "target": pytest.approx(108.0), "ccy": "EUR"}


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
        out = od.density("ZZZZ.XX", 365, {})
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
    assert od.density("ZZZZ.YY", 91, {})["reason"] == "error"

    good = {"ok": True, "chain_symbol": "OKAY"}

    def ok(*a):
        calls["n"] += 1
        return good

    monkeypatch.setattr(od, "_compute", ok)
    calls["n"] = 0
    assert od.density("OKAY", 365, {}) is good
    assert od.density("OKAY", 365, {}) is good
    assert calls["n"] == 1


def test_settings_and_horizon_snap():
    st = od._settings({"dist_erp": 0.04, "dist_measure": "Q", "dist_trust": 1.0})
    assert st["erp"] == 0.04 and st["measure"] == "Q"
    assert "haircut" not in st and "trust" not in st  # 1.29: no trust control at all
    assert od._settings({"dist_measure": "X"})["measure"] == "P"
    assert od._settings(None)["kappa_stock"] == od.KAPPA_DEFAULT["stock"]


def test_a_nan_fit_fails_the_gate(rn):
    # NaN passes every comparison: the gate must say no, not let it through.
    for k in ("martingale_err", "svi_rmse_volpts"):
        ok, why = od.quality({**rn, k: float("nan")})
        assert not ok and why == "fit failed"


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
    out = od.density("OPRX", 365, {})
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
    out = od.density("ASML.AS", 365, {"dist_erp": 0.051})
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


def test_a_borrowed_chain_block_sets_no_alpha(rn, monkeypatch):
    """No targets on the listing or its research line: the chain line's block
    is still the Street reference, but alpha stays 0, as on the grid row."""
    _model_stub(monkeypatch)
    st = {**od._settings({}), "prefs": {}}
    _, mdl = od._model_curves("X.DE", rn, "EUR", 0.9, {"mean": 130.0}, st, 0.92, 0.0, own=False)
    assert mdl["alpha"] == 0.0


def test_window_dividend_yield_repairs_pence():
    """HSBA.L: a pence price and a pound dividend rate (the row's rule)."""
    info = {"currency": "GBp", "dividendRate": 0.56, "dividendYield": 4.07}
    assert od._dividend_yield(info, 1347.8, "GBp") == pytest.approx(0.0415, abs=0.002)


def test_local_rate_compares_the_futures_on_the_same_days(monkeypatch):
    """The far contract trades thinly: its last close can be a day older than
    the near one's. Mixing dates turned a 0.3% EUR/USD move into a one-point
    rate error; the spread is read on joint days only."""
    import pandas as pd

    days = pd.bdate_range("2026-10-12", periods=10)
    near = pd.Series(1.1200, index=days)
    far = pd.Series(1.1200 * math.exp((0.04 - 0.024) * 91 / 365), index=days)
    near.iloc[-1] = 1.1200 * 0.997  # today's move, far has no print today
    far = far.iloc[:-1]
    monkeypatch.setattr(od, "_rate", lambda: 0.04)
    monkeypatch.setattr(
        od,
        "_next_quarters",
        lambda n=2: [("Z26", dt.date(2026, 12, 16)), ("H27", dt.date(2027, 3, 17))],
    )
    monkeypatch.setattr(od, "_close", lambda sym, period="5d": near if "Z26" in sym else far)
    od._RATE.pop("EUR", None)
    r, src = od._local_rate("EUR")
    assert src == "CME futures" and r == pytest.approx(0.024, abs=0.002)
    od._RATE.pop("EUR", None)


# ------------------------------------------- local rates without a CME future

_BIS_CSV = """FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE
D,BR,2026-10-20,13.75
D,NO,2026-10-13,4.5
D,IN,2026-10-19,5.5
D,PH,2026-10-18,NaN
D,AR,2025-07-10,29
"""


@pytest.fixture
def bis(monkeypatch):
    """BIS answers from a stub; counts the calls."""
    import io
    import urllib.request

    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)
        if isinstance(fake.body, Exception):
            raise fake.body
        return io.BytesIO(fake.body.encode())

    fake.body = _BIS_CSV
    monkeypatch.setenv("CONVEXITY_POLICY_RATES", "1")
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(od, "_POLICY", {"t": 0.0, "failed_at": 0.0, "rates": None})
    monkeypatch.setattr(od, "_rate", lambda: 0.04)
    monkeypatch.setattr(od, "_spawn", lambda fn: fn())  # the background refresh, inline
    fake.calls = calls
    return fake


def test_bis_rows_are_parsed_and_stale_or_missing_ones_dropped():
    got = od._parse_bis(_BIS_CSV, today=dt.date(2026, 10, 26))
    assert got["BR"] == {"rate": 0.1375, "asof": "2026-10-20"}
    assert "PH" not in got  # NaN
    assert "AR" not in got  # a discontinued series


def test_a_currency_without_a_future_gets_its_central_banks_rate(bis):
    assert od._local_rate("BRL") == (pytest.approx(0.1375), "Selic policy rate")
    assert od._local_rate("NOK") == (pytest.approx(0.045), "Norges Bank policy rate")
    assert od._local_rate("INR")[0] == pytest.approx(0.055)
    assert len(bis.calls) == 1  # one request for every currency, cached


def test_no_bis_series_falls_back_as_before(bis, monkeypatch):
    # TWD has no BIS series: the US bill, said so
    assert od._local_rate("TWD") == (0.04, "US 3-month bill (no local rate)")
    # a European currency BIS does not answer for: the euro's rate
    monkeypatch.setattr(od, "_POLICY", {"t": time.time(), "failed_at": 0.0, "rates": {}})
    od._RATE["EUR"] = (time.time(), 0.024, "CME futures")
    try:
        assert od._local_rate("SEK") == (0.024, "euro rate (no local quote)")
    finally:
        od._RATE.pop("EUR", None)


def test_a_failed_fetch_keeps_the_last_good_rates_and_waits(bis, monkeypatch):
    od._local_rate("BRL")
    od._POLICY["t"] = 0.0  # a day later: refreshed in the background, the old table answers
    bis.body = OSError("BIS down")
    assert od._local_rate("BRL")[0] == pytest.approx(0.1375)  # last good, not the US bill
    n = len(bis.calls)
    od._local_rate("BRL")
    assert len(bis.calls) == n  # no retry storm inside POLICY_RETRY


def test_policy_rates_survive_a_restart_on_disk(bis, monkeypatch):
    from convexity import paths

    od._local_rate("BRL")
    assert paths.state_file("policy_rates").exists()
    monkeypatch.setattr(od, "_POLICY", {"t": 0.0, "failed_at": 0.0, "rates": None})
    bis.body = OSError("offline")
    assert od._local_rate("BRL")[0] == pytest.approx(0.1375)  # read back from disk


def test_majors_keep_the_cme_rate(bis, monkeypatch):
    od._RATE["GBP"] = (time.time(), 0.037, "CME futures")
    try:
        assert od._local_rate("GBP") == (0.037, "CME futures")
        assert not bis.calls  # a major never asks BIS
    finally:
        od._RATE.pop("GBP", None)


def test_a_stale_table_answers_while_the_refresh_runs(bis, monkeypatch):
    od._local_rate("BRL")
    od._POLICY["t"] = 0.0
    spawned = []
    monkeypatch.setattr(od, "_spawn", spawned.append)  # the refresh never finishes
    bis.body = _BIS_CSV.replace("13.75", "14.25")
    assert od._local_rate("BRL")[0] == pytest.approx(0.1375)  # no wait on BIS
    assert len(spawned) == 1
    od._local_rate("BRL")
    assert len(spawned) == 1  # one refresh in flight, not one per request
    spawned[0]()
    assert od._local_rate("BRL")[0] == pytest.approx(0.1425)
