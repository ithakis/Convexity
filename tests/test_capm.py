"""The app's one expected-return model (capm.py): beta, mu, model price.

No network: closes are synthetic, the rate and the bulk download are stubbed.
"""

import datetime as dt
import json
import math

import numpy as np
import pandas as pd
import pytest

from convexity import analyst_targets, capm, paths, persistence

# ---------------------------------------------------------------- estimator


def test_bswa_matches_a_hand_computed_case():
    """x = (1%, -2%, 3%), y = (5%, -1%, 2%), decay ln 2 per day (weights 1/4,
    1/2, 1 -> 1/7, 2/7, 4/7). Day 1's 5% is clipped to 4 x 1% = 4%; the
    weighted OLS slope is then exactly 67/117 (by fractions)."""
    x = np.array([0.01, -0.02, 0.03])
    y = np.array([0.05, -0.01, 0.02])
    assert capm.bswa(y, x, rho=math.log(2)) == pytest.approx(67 / 117, rel=1e-12)


def test_bswa_clips_to_minus_two_and_four_market_units():
    # A down-market day: the band is [4 r_m, -2 r_m] = [-8%, +4%].
    x = np.array([0.01, -0.02, 0.03, -0.01])
    y_in = np.array([0.02, -0.08, 0.06, -0.01])
    y_out = y_in.copy()
    y_out[1] = -0.5  # far below -8%: must count exactly as -8%
    assert capm.bswa(y_out, x) == pytest.approx(capm.bswa(y_in, x), rel=1e-12)


def test_recent_days_weigh_more():
    """Beta 0.5 for two years, then 1.5 for the last year: the age weight
    (half-life ~87 days) puts the estimate well above the plain average."""
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.01, 756)
    b = np.r_[np.full(504, 0.5), np.full(252, 1.5)]
    y = b * x
    assert capm.bswa(y, x) > 1.35
    assert capm.ols(y, x) < 1.0


def test_shrink_toward_one():
    assert capm.shrink(2.0) == pytest.approx(1.9)
    assert capm.shrink(0.2) == pytest.approx(0.28)
    assert capm.shrink(1.0) == 1.0
    assert capm.shrink(0.4, 0.5) == pytest.approx(0.7)


def _series(n, beta, seed=1, start="2023-01-02", noise=0.01):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    m = rng.normal(0.0003, 0.01, n)
    s = beta * m + rng.normal(0, noise, n)
    return (
        pd.Series(100 * np.exp(np.cumsum(s)), index=idx),
        pd.Series(4000 * np.exp(np.cumsum(m)), index=idx),
    )


def test_estimate_methods_by_history_length():
    s, m = _series(800, 1.3)
    full = capm.estimate(s, m)
    assert full["method"] == "bswa" and full["n_days"] == 757
    assert full["raw"] == pytest.approx(1.3, abs=0.15)
    young = capm.estimate(s.iloc[-200:], m)
    assert young["method"] == "ols1y" and young["n_days"] == 200
    ipo = capm.estimate(s.iloc[-100:], m)
    assert ipo == {"raw": None, "method": "prior", "n_days": 100, "asof": ipo["asof"]}
    assert capm.finish(ipo, capm.home_proxy("ABNB"), capm.settings({}))["beta"] == 1.0


def test_estimate_uses_joint_days_and_the_window():
    s, m = _series(800, 0.8)
    m = m.drop(m.index[::7])  # the market's holidays: joint days only
    assert capm.estimate(s, m)["n_days"] <= 757
    assert capm.estimate(s, m, years=1)["n_days"] == 253


def test_glitch_returns_are_ignored():
    s, m = _series(800, 1.0)
    bad = s.copy()
    bad.iloc[500] *= 52  # Yahoo's x52 bar, then back
    assert capm.estimate(bad, m)["raw"] == pytest.approx(capm.estimate(s, m)["raw"], abs=0.02)


# ---------------------------------------------------------------- proxy map


@pytest.mark.parametrize(
    "sym,proxy,kind",
    [
        ("AAPL", "^GSPC", "us"),
        ("SPY", "^GSPC", "us"),
        ("BNP.PA", "^FCHI", "home"),
        ("DBK.DE", "^GDAXI", "home"),
        ("NOV.DE", "^GDAXI", "home"),
        ("SAN.MC", "^IBEX", "home"),
        ("ISP.MI", "FTSEMIB.MI", "home"),
        ("INGA.AS", "^AEX", "home"),
        ("KBC.BR", "^BFX", "home"),
        ("NDA-FI.HE", "^OMXH25", "home"),
        ("EBS.VI", "^ATX", "home"),
        ("NESN.SW", "^SSMI", "home"),
        ("VOLV-B.ST", "^OMX", "home"),
        ("NOVO-B.CO", "^OMXC25", "home"),
        ("HSBA.L", "^FTAS", "home"),
        ("7203.T", "1306.T", "home"),
        ("EDP.LS", "^STOXX", "regional"),
        ("EQNR.OL", "^STOXX", "regional"),
        ("XYZ.ZZ", "^GSPC", "fallback"),
    ],
)
def test_home_index_map(sym, proxy, kind):
    p = capm.home_proxy(sym)
    assert (p["proxy"], p["kind"]) == (proxy, kind)


def test_fallback_chain():
    assert [p["proxy"] for p in capm.fallbacks("BNP.PA")] == ["^FCHI", "^STOXX", "^GSPC"]
    assert [p["proxy"] for p in capm.fallbacks("7203.T")] == ["1306.T", "^GSPC"]
    assert [p["proxy"] for p in capm.fallbacks("AAPL")] == ["^GSPC"]


# ------------------------------------------------------------ batch + cache


@pytest.fixture
def closes(monkeypatch):
    """A bulk download that knows a few names; counts its calls."""
    s, m = _series(800, 1.4, seed=3)
    fr, _ = _series(800, 1.0, seed=4)
    frame = pd.DataFrame({"BNP.PA": s, "^FCHI": m, "^STOXX": fr, "^GSPC": fr, "AAPL": s})
    calls = []

    def bulk(symbols, period, **_):
        calls.append(list(symbols))
        return frame[[c for c in symbols if c in frame.columns]]

    from convexity import analytics

    monkeypatch.setattr(analytics, "_bulk_close", bulk)
    capm._MEM.clear()
    return calls


def test_ensure_computes_once_a_day_and_persists(closes):
    out = capm.ensure(["BNP.PA"], {}, {"BNP.PA": "EUR"})["BNP.PA"]
    assert out["proxy"] == "^FCHI" and out["method"] == "bswa"
    assert out["beta"] == pytest.approx(1 + 0.9 * (out["raw"] - 1), abs=1e-4)
    assert set(out) >= {"beta", "raw", "proxy", "proxy_name", "n_days", "method", "asof"}
    capm._MEM.clear()
    assert capm.cached("BNP.PA") == out  # from state/betas.json, no download
    capm.ensure(["BNP.PA"], {})
    assert len(closes) == 1
    store = json.loads(paths.state_file("betas").read_text(encoding="utf-8"))
    assert set(store["items"]) == {"BNP.PA|3"}  # market data only: no weights, no book


def test_a_changed_shrink_needs_no_recompute(closes):
    raw = capm.ensure(["BNP.PA"], {})["BNP.PA"]["raw"]
    half = capm.cached("BNP.PA", {"model_beta_shrink": 0.5})
    assert half["beta"] == pytest.approx(1 + 0.5 * (raw - 1), abs=1e-4)
    assert len(closes) == 1


def test_missing_history_is_the_prior_and_never_cached(closes):
    out = capm.ensure(["NEWCO"], {})["NEWCO"]
    assert out["beta"] == 1.0 and out["method"] == "prior" and out["raw"] is None
    assert capm.cached("NEWCO") is None


def test_yesterdays_beta_is_recomputed(closes):
    capm.ensure(["AAPL"], {})
    k = "AAPL|3"
    capm._MEM[k] = {**capm._MEM[k], "day": (dt.date.today() - dt.timedelta(days=1)).isoformat()}
    store = json.loads(paths.state_file("betas").read_text(encoding="utf-8"))
    store["items"][k]["day"] = "2000-01-01"
    paths.state_file("betas").write_text(json.dumps(store), encoding="utf-8")
    assert capm.cached("AAPL") is None


# ---------------------------------------------------------------- the model


@pytest.fixture
def model_env(monkeypatch):
    from convexity import options_density

    monkeypatch.setattr(options_density, "_local_rate", lambda c: (0.03, "test rate"))
    monkeypatch.setattr(analyst_targets, "view_ic", lambda: (0.02, "prior"))
    bm = capm.finish({"raw": 1.2, "method": "bswa", "n_days": 757}, capm.home_proxy("X"),
                     capm.settings({}))  # fmt: skip
    return bm


def test_mu_on_and_off(model_env):
    m = capm.model("X", 1.0, s0=100.0, ccy="EUR", target=140.0, div_yield=0.0,
                   beta_meta=model_env)  # fmt: skip
    beta = 1 + 0.9 * 0.2
    assert m["mu_capm"] == pytest.approx(0.03 + beta * 0.05)
    u = math.log1p(0.40)
    ref = (u + analyst_targets.SHRINK_N0 * analyst_targets.UPSIDE_MEAN) / (
        1 + analyst_targets.SHRINK_N0
    )
    z = (u - ref) / analyst_targets.UPSIDE_SD
    assert m["alpha_meta"]["z"] == pytest.approx(z, abs=1e-4)
    assert m["alpha"] == pytest.approx(0.02 * 0.42 * z, abs=1e-6)
    assert m["mu"] == pytest.approx(m["mu_capm"] + m["alpha"], abs=1e-6)
    assert m["model_price"] == pytest.approx(100 * math.exp(m["mu"]), rel=1e-6)
    assert m["capm_price"] == pytest.approx(100 * math.exp(m["mu_capm"]), rel=1e-6)
    assert set(m["alpha_meta"]) >= {"ic", "ic_source", "z", "upside", "ref_upside"}
    assert m["rf_source"] == "test rate"


def test_no_targets_no_alpha(model_env):
    m = capm.model("X", 1.0, s0=100.0, ccy="USD", target=None, beta_meta=model_env)
    assert m["alpha"] == 0.0 and m["mu"] == m["mu_capm"]
    assert m["model_price"] == m["capm_price"]
    assert m["alpha_meta"]["reason"] == "no_targets"


def test_alpha_is_capped_and_settable(model_env):
    hi = capm.model("X", 1.0, s0=100.0, ccy="USD", target=900.0, beta_meta=model_env)
    assert hi["alpha_meta"]["z"] == analyst_targets.Z_CAP
    assert hi["alpha"] == pytest.approx(0.02 * 0.42 * 3.0)
    p = {"model_ic": 0.05, "model_z_cap": 1.0}
    own = capm.model("X", 1.0, s0=100.0, ccy="USD", target=900.0, beta_meta=model_env, prefs=p)
    assert own["alpha"] == pytest.approx(0.05 * 0.42 * 1.0)
    assert own["alpha_meta"]["ic_source"] == "set"


def test_horizon_scales_the_price(model_env):
    m3 = capm.model("X", 0.25, s0=50.0, ccy="USD", target=60.0, beta_meta=model_env)
    m12 = capm.model("X", 1.0, s0=50.0, ccy="USD", target=60.0, beta_meta=model_env)
    assert m3["mu"] == m12["mu"]
    assert m3["model_price"] == pytest.approx(50 * math.exp(m12["mu"] * 0.25), rel=1e-6)


def test_one_model_price_on_every_path(model_env, monkeypatch):
    """The grid row (for_rows), the export (_with_model) and the density's
    model block (_model_curves) give the same 12-month model price."""
    from convexity import options_density as od
    from convexity import xlsx_export

    monkeypatch.setattr(capm, "ensure", lambda syms, prefs=None, ccy=None: {
        s.upper(): model_env for s in syms})  # fmt: skip
    monkeypatch.setattr(capm, "beta_for", lambda *a, **k: model_env)
    row = {"symbol": "XYZ", "price": 80.0, "currency": "USD", "target_mean_price": 96.0,
           "dividend_yield": 0.02}  # fmt: skip
    grid = capm.for_rows([row], {})["XYZ"]
    sheet = xlsx_export._with_model({"rows": [row]}, lambda rows: capm.for_rows(rows, {}))
    rn = {"T": 1.0, "S0": 80.0, "F": 82.0, "rf": 0.03}
    monkeypatch.setattr(od, "mean_curve", lambda rn, mu, k: {"p": None, "method": "B"})
    _, dens = od._model_curves("XYZ", rn, "USD", 1.0, {"mean": 96.0}, {"prefs": {}}, 0.92, 0.02)
    assert grid["model_price"] == dens["model_price"] == sheet["rows"][0]["model_price"]
    assert grid["mu"] == dens["mu"] == sheet["rows"][0]["model_mu"]
    assert sheet["rows"][0]["beta"] == model_env["beta"]


# ------------------------------------------------- what 1.29 took out, gone


def test_yahoo_beta_is_never_read():
    from convexity import fetcher

    row = {"symbol": "AZN.L"}
    fetcher._row_fields({"beta": 0.03, "currency": "GBp"}, 100.0, row)
    assert row["beta"] is None and row["beta_meta"] is None  # no app beta cached yet


def test_saved_rows_lose_their_yahoo_beta():
    persistence.save_view("Demo", "AAPL", [{"symbol": "AAPL", "price": 1.0, "beta": 0.03}])
    assert "beta" not in persistence.load_view("Demo")["rows"][0]
    mine = {"symbol": "AAPL", "price": 1.0, "beta": 1.1, "beta_meta": {"beta": 1.1}}
    persistence.save_view("Demo", "AAPL", [mine])
    assert persistence.load_view("Demo")["rows"][0]["beta"] == 1.1


def test_removed_fields_are_gone():
    from convexity import options_density, search, xlsx_export

    assert "beta" not in search.FIELDS
    assert not hasattr(options_density, "analysts_mixture")
    assert not hasattr(options_density, "TRUSTS")
    assert "beta_info" not in {k for k, _ in xlsx_export.ANALYST_COLS}
    src = open(options_density.__file__, encoding="utf-8").read()
    for gone in ('"analysts_t50"', '"at_expiry"', "dist_trust", 'q.get("beta")'):
        assert gone not in src
