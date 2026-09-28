"""Tests for convexity.ml_sentiment — the Market read.

The contracts that matter in production:
1. Graceful degradation: no artifact / a schema mismatch -> available() False,
   every call returns nothing, nothing raises.
2. Train/serve parity: the live path (score_articles -> weighted_sar) must
   produce exactly the score the calibration panel holds for the same
   articles (ml/scripts/06 builds its items the way _training_item does
   here), and the price context must follow the as-of-D-1 convention of
   ml/scripts/05 stage D.
3. Calibration: z, percentile and tier come from the artifact's knots until
   the app has MIN_LIVE_HISTORY live scores, then from those; the middle 70%
   is labelled "no edge".
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A bare importorskip would let this module skip silently in exactly the
# broken environment (the desktop env once shipped without lightgbm). Skipping
# is only legitimate when someone deliberately opts into an ML-less env.
_ALLOW_MISSING = os.environ.get("PT_ALLOW_MISSING_ML") == "1"
for _mod in ("lightgbm", "sklearn"):
    if importlib.util.find_spec(_mod) is None:
        if _ALLOW_MISSING:
            pytest.skip(f"{_mod} not installed (PT_ALLOW_MISSING_ML=1)", allow_module_level=True)
        pytest.fail(
            f"{_mod} is not installed — the Market read would be dead at runtime. "
            f"Run `uv sync --extra dev`. Set PT_ALLOW_MISSING_ML=1 "
            f"to skip these tests deliberately instead.",
            pytrace=False,
        )

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import scipy.sparse as sp  # noqa: E402

from convexity import ml_features as mf  # noqa: E402
from convexity import ml_sentiment as ms  # noqa: E402
from convexity.relevance import is_boilerplate, relevance_score  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    ms.reset_for_tests()
    yield
    ms.reset_for_tests()


def _cal(horizon=1):
    # z knots: an evenly spread standard-normal-ish grid from -3 to +3
    knots = [round(-3 + 6 * i / 100, 4) for i in range(101)]
    return {
        "version": "test",
        "score": "enc_wmean",
        "horizon_days": horizon,
        "mu": 0.1,
        "sigma": 0.5,
        "pct_knots": knots,
        "tier_pct": [5.0, 15.0, 85.0, 95.0],
        "tiers": ["very_bearish", "bearish", "no_edge", "bullish", "very_bullish"],
        "exp_sar": {
            "pct_edges": list(range(0, 101, 5)),
            "sar": [round(-0.2 + 0.02 * i, 3) for i in range(20)],
        },
    }


@pytest.fixture()
def tiny_artifact(tmp_path, monkeypatch):
    """A real (tiny) encoder artifact with the production schema."""
    built = build_tiny_artifact(tmp_path / "mlsent-v1.1")
    monkeypatch.setenv("MLSENT_MODEL_DIR", str(built[0]))
    return built


def build_tiny_artifact(d: Path):
    """Write the six artifact files into ``d``; also used by test_model_fetch."""
    import lightgbm as lgb

    rng = np.random.default_rng(0)
    n = 400
    texts = [f"company {'beats' if i % 2 else 'misses'} earnings row{i}" for i in range(n)]
    counts = mf.hash_counts(texts)
    idf = mf.fit_idf([counts])
    dfreq = np.asarray((counts > 0).sum(axis=0)).ravel()
    mask = np.sort(np.argsort(dfreq)[::-1][:4096]).astype("int32")
    dense = np.asarray(
        [mf.dense_vector({"title": t, "session_class": "dateonly_cc"}) for t in texts],
        dtype="float32",
    )
    X = sp.hstack([mf.apply_idf(counts, idf)[:, mask], sp.csr_matrix(dense)], format="csr")
    y = np.where(np.arange(n) % 2, 0.5, -0.5) + rng.normal(0, 0.1, n)
    enc = lgb.train(
        {"objective": "regression", "verbosity": -1, "min_data_in_leaf": 5},
        lgb.Dataset(X, y),
        num_boost_round=20,
    )

    d.mkdir(parents=True)
    enc.save_model(str(d / "model.lgbm.txt"))
    np.save(d / "idf.npy", idf)
    np.save(d / "col_mask.npy", mask)
    (d / "feature_schema.json").write_text(json.dumps(mf.feature_schema()))
    (d / "tier_cuts.json").write_text(json.dumps(_cal()))
    (d / "meta.json").write_text(json.dumps({"version": "test"}))
    return d, enc, idf, mask


# ----------------------------------------------------------------- degradation
def test_unavailable_without_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("MLSENT_MODEL_DIR", str(tmp_path / "nothing_here"))
    assert ms.available() is False
    assert ms.score_articles([{"headline": "x"}], "AAPL") == []
    assert ms.market_read("AAPL", [{"headline": "x", "datetime": time.time()}]) == (None, None)
    st = ms.runtime_status()
    assert st["available"] is False and st["reason"] and st["model_dir_exists"] is False


def test_schema_mismatch_degrades(tiny_artifact):
    d = tiny_artifact[0]
    schema = json.loads((d / "feature_schema.json").read_text())
    schema["hash_dim"] = 123
    (d / "feature_schema.json").write_text(json.dumps(schema))
    assert ms.available() is False
    assert "schema" in ms.runtime_status()["reason"]


def test_bad_knots_degrade(tiny_artifact):
    d = tiny_artifact[0]
    cal = _cal()
    cal["pct_knots"] = cal["pct_knots"][::-1]
    (d / "tier_cuts.json").write_text(json.dumps(cal))
    assert ms.available() is False


# ----------------------------------------------------------------- parity
def _article(i=1, age_h=2.0, **kw):
    a = {
        "headline": f"company beats earnings row{i}",
        "summary": "strong quarter",
        "source": "Reuters",
        "datetime": time.time() - age_h * 3600,
        "n_duplicates": 0,
        "related": "AAPL",
    }
    a.update(kw)
    return a


def _training_item(a, symbol, sar_pred):
    """How ml/scripts/06 (_articles_worker + _panel_worker) builds a window item."""
    tier = mf.publisher_tier(a["source"])
    rel = relevance_score(a["headline"], a["summary"], symbol, None, 1, tier)
    row = mf.dense_vector({"title": a["headline"], "summary": a["summary"]})
    lm = (
        None
        if row[mf.DENSE_COLUMNS.index("lm_missing")]
        else row[mf.DENSE_COLUMNS.index("lm_score")]
    )
    return {
        "sar_pred": sar_pred,
        "lm": lm,
        "unc": row[mf.DENSE_COLUMNS.index("uncertainty_ratio")],
        "publisher_tier": tier,
        "n_duplicates": a["n_duplicates"],
        "relevance": rel,
        "boiler": is_boilerplate(a["headline"]),
        "datetime": a["datetime"],
    }


def test_encoder_matches_the_training_featurization(tiny_artifact, monkeypatch):
    d, enc, idf, mask = tiny_artifact
    monkeypatch.setattr("convexity.relevance.load_company_names", lambda: {})
    a = _article()
    items = ms.score_articles([a], "AAPL")
    assert len(items) == 1
    dt = datetime.fromtimestamp(a["datetime"], tz=timezone.utc).astimezone(ms._ET)
    counts = mf.hash_counts([mf.text_for_hashing(a["headline"], a["summary"])])
    dense = np.asarray(
        [
            mf.dense_vector(
                {
                    "title": a["headline"],
                    "summary": a["summary"],
                    "publisher_tier": mf.publisher_tier("Reuters"),
                    "relevance": items[0]["relevance"],
                    "n_duplicates": 0,
                    "co_mention_count": 1,
                    "session_class": "dateonly_cc",
                    "day_of_week": dt.isoweekday(),
                    "month": dt.month,
                }
            )
        ],
        dtype="float32",
    )
    X = sp.hstack([mf.apply_idf(counts, idf)[:, mask], sp.csr_matrix(dense)], format="csr")
    assert items[0]["sar_pred"] == pytest.approx(float(enc.predict(X)[0]), abs=1e-6)


def test_served_score_matches_the_calibration_panel(tiny_artifact, monkeypatch):
    """The Market read score (weighted_sar over live items) equals the panel's
    enc_wmean over training-built items for the same articles — the number
    the tier cuts were fitted on."""
    monkeypatch.setattr("convexity.relevance.load_company_names", lambda: {})
    arts = [
        _article(1, 1.0),
        _article(
            2,
            30.0,
            source="PR Newswire",
            n_duplicates=3,
            headline="Top 5 stocks to watch: company row2",
        ),
        _article(3, 80.0, source="Some Blog", summary=""),
    ]
    live = ms.score_articles(arts, "AAPL")
    train = [_training_item(a, "AAPL", it["sar_pred"]) for a, it in zip(arts, live)]
    now = time.time()
    s_live, w_live = mf.weighted_sar(live, now)
    s_train, w_train = mf.weighted_sar(train, now)
    assert s_live == pytest.approx(s_train, abs=1e-12)
    assert w_live == pytest.approx(w_train, abs=1e-12)


def test_price_features_are_as_of_the_previous_session():
    days = pd.bdate_range(end=pd.Timestamp("2026-09-25"), periods=40)
    c = 100 * np.cumprod(1 + np.linspace(-0.01, 0.02, len(days)))
    spy = 400 * np.cumprod(1 + np.linspace(0.005, -0.005, len(days)))
    closes = pd.DataFrame({"AAPL": c, "SPY": spy}, index=days)
    today = date(2026, 9, 25)  # the last row is today's (partial) bar
    f = ms.price_features(closes, "AAPL", today)
    h = c[:-1]  # the SQL stage uses closes < D only
    r = np.diff(h[-21:]) / h[-21:-1]
    assert f["tkr_ret_1d"] == pytest.approx(h[-1] / h[-2] - 1)
    assert f["tkr_ret_5d"] == pytest.approx(h[-1] / h[-6] - 1)
    assert f["tkr_ret_20d"] == pytest.approx(h[-1] / h[-21] - 1)
    assert f["tkr_vol_20d"] == pytest.approx(np.std(r[-20:], ddof=1))
    s = spy[:-1]
    m = np.diff(s[-21:]) / s[-21:-1]
    assert f["spy_ret_5d"] == pytest.approx(np.prod(1 + m[-5:]) - 1)
    assert f["spy_vol_20d"] == pytest.approx(np.std(m[-20:], ddof=1))
    empty = ms.price_features(None, "AAPL", today)
    assert all(math.isnan(v) for v in empty.values())


# ----------------------------------------------------------------- calibration
def test_calibrate_places_scores_on_their_history():
    cal = _cal()
    # knots are linear in pct: z = -3 + 6 * pct / 100  =>  pct = (z + 3) / 6 * 100
    for z, tier in [
        (-3.0, "very_bearish"),
        (-2.7, "very_bearish"),
        (-2.6, "bearish"),
        (-2.1, "bearish"),
        (-2.0, "no_edge"),
        (0.0, "no_edge"),
        (2.1, "bullish"),
        (2.7, "very_bullish"),
        (3.5, "very_bullish"),
    ]:
        out = ms.calibrate(0.1 + 0.5 * z, cal)
        assert out["z"] == pytest.approx(z, abs=0.01)
        assert out["tier"] == tier, (z, out)
    mid = ms.calibrate(0.1, cal)
    assert mid["pct"] == pytest.approx(50.0, abs=0.01)
    assert mid["sar"] == cal["exp_sar"]["sar"][10]  # the 50-55 band


def test_calibrate_anchors_to_live_history_once_there_is_enough():
    cal = _cal()
    hist = [0.001 * i for i in range(ms.MIN_LIVE_HISTORY)]
    few = ms.calibrate(0.19, cal, hist[:-1])
    assert few["anchor"] == "training" and few["n_history"] == ms.MIN_LIVE_HISTORY - 1
    out = ms.calibrate(0.19, cal, hist)  # 190 of 200 below
    assert out["anchor"] == "live" and out["n_history"] == ms.MIN_LIVE_HISTORY
    assert out["pct"] == pytest.approx(100 * 190.5 / ms.MIN_LIVE_HISTORY, abs=0.05)
    assert out["tier"] == "very_bullish"
    arr = np.asarray(hist)
    assert out["z"] == pytest.approx((0.19 - arr.mean()) / arr.std(), abs=0.01)
    assert ms.calibrate(0.1, cal, hist)["tier"] == "no_edge"
    assert ms.calibrate(-1.0, cal, hist)["pct"] == 0.0


def test_calibrate_three_way_anchor():
    """live (own reads) > reference (the S&P 500 pack) > training knots."""
    cal = _cal()
    ref = [0.001 * i for i in range(ms.MIN_LIVE_HISTORY + 50)]
    own = [0.5 + 0.001 * i for i in range(ms.MIN_LIVE_HISTORY - 1)]
    out = ms.calibrate(0.19, cal, own, ref, "2026-09-26")
    assert out["anchor"] == "reference" and out["reference_date"] == "2026-09-26"
    assert out["n_reference"] == len(ref) and out["n_history"] == len(own)
    assert out["pct"] == pytest.approx(100 * 190.5 / len(ref), abs=0.05)
    # Enough reads of its own: the reference is ignored.
    live = ms.calibrate(0.19, cal, own + [0.9], ref)
    assert live["anchor"] == "live" and "reference_date" not in live
    # A reference that is too small counts for nothing.
    assert ms.calibrate(0.19, cal, [], ref[:10])["anchor"] == "training"


def test_calibrate_ignores_non_finite_history():
    hist = [float("nan")] * 50 + [0.001 * i for i in range(ms.MIN_LIVE_HISTORY - 1)]
    assert ms.calibrate(0.0, _cal(), hist)["anchor"] == "training"


def test_calibrate_handles_flat_knots():
    cal = _cal()
    cal["pct_knots"] = [0.0] * 40 + [round(0.05 * i, 3) for i in range(61)]
    out = ms.calibrate(0.1, cal)  # z = 0, a tied knot
    assert 0 <= out["pct"] <= 100


# ----------------------------------------------------------------- market_read
def test_market_read_is_the_weighted_encoder_score_over_7_days(tiny_artifact, monkeypatch):
    monkeypatch.setattr("convexity.relevance.load_company_names", lambda: {})
    now = time.time()
    fresh = [_article(i, age_h=6 * i) for i in range(1, 6)]
    old = [_article(9, age_h=24 * 12)]
    market, vol = ms.market_read("AAPL", fresh + old, None, now=now)
    assert market["n_articles"] == 5  # the 12-day-old one is out
    assert market["model_version"] == ms.ARTIFACT_VERSION
    assert market["horizon_days"] == 1
    assert market["tier"] in ms.TIERS and 0 <= market["confidence"] <= 1
    expected, _ = mf.weighted_sar(ms.score_articles(fresh, "AAPL"), now)
    assert market["score"] == pytest.approx(expected, abs=1e-6)
    assert market["anchor"] == "training" and market["n_history"] == 0
    assert vol is None  # no closes, no vol


def test_market_read_ranks_against_the_history_it_is_given(tiny_artifact, monkeypatch):
    monkeypatch.setattr("convexity.relevance.load_company_names", lambda: {})
    arts = [_article(i, age_h=6 * i) for i in range(1, 4)]
    base, _ = ms.market_read("AAPL", arts)
    below = [base["score"] - 1.0] * ms.MIN_LIVE_HISTORY
    market, _ = ms.market_read("AAPL", arts, history=below)
    assert market["anchor"] == "live" and market["pct"] == 100.0
    assert market["tier"] == "very_bullish"


def test_market_read_caps_the_window_like_the_training_panel(tiny_artifact, monkeypatch):
    monkeypatch.setattr("convexity.relevance.load_company_names", lambda: {})
    arts = [_article(i, age_h=0.5 + i * 0.9) for i in range(150)]  # ~5.6 days
    market, _ = ms.market_read("AAPL", arts)
    assert market["n_articles"] <= mf.WINDOW_CAP


def test_market_read_without_articles_is_none(tiny_artifact):
    assert ms.market_read("AAPL", []) == (None, None)


# ----------------------------------------------------------------- publish-last race
def test_load_never_publishes_a_half_initialized_state(tiny_artifact, monkeypatch):
    """No caller may ever observe {loaded: True, ok: False, reason: ""}.

    _load() used to set `loaded` BEFORE the multi-second lightgbm/scipy/sklearn
    import while the fast path read it without the lock, so a thread arriving
    mid-load saw a published-but-empty state and silently skipped the read.
    """
    import threading

    gate = threading.Event()
    real_schema = mf.feature_schema

    def slow_schema():
        gate.set()
        time.sleep(0.4)
        return real_schema()

    monkeypatch.setattr(mf, "feature_schema", slow_schema)
    observations: list[tuple[bool, str]] = []

    def observer():
        gate.wait(5.0)
        st = ms._load()
        observations.append((bool(st["ok"]), st.get("reason") or ""))

    watchers = [threading.Thread(target=observer) for _ in range(8)]
    loader = threading.Thread(target=ms.available)
    loader.start()
    for t in watchers:
        t.start()
    loader.join(30)
    for t in watchers:
        t.join(30)
    assert len(observations) == 8, "watchers never ran"
    for ok, reason in observations:
        assert ok or reason, "observed loaded=True, ok=False, reason='' — the race is back"
    assert all(ok for ok, _ in observations)


def test_article_context_uses_the_article_day():
    days = pd.bdate_range(end=pd.Timestamp("2026-09-25"), periods=30)
    closes = pd.DataFrame(
        {"AAPL": np.linspace(100, 130, 30), "SPY": np.linspace(400, 460, 30)}, index=days
    )
    ctx = ms._article_context(closes, "AAPL", date(2026, 9, 20))  # a Sunday
    spy = closes["SPY"][closes.index <= "2026-09-20"].to_numpy()
    assert ctx["spy_ret_1d"] == pytest.approx(spy[-1] / spy[-2] - 1)  # Friday's move
    a = closes["AAPL"][closes.index <= "2026-09-20"].to_numpy()
    assert ctx["tkr_ret_5d"] == pytest.approx(a[-1] / a[-6] - 1)
    assert ms._article_context(None, "AAPL", date(2026, 9, 20))["spy_ret_1d"] == 0.0
