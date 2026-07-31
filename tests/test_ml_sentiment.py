"""Tests for portfolio_tracker.ml_sentiment — production inference.

Covers the two contracts that matter in production:
1. Graceful degradation: no artifact -> available() False, every call None,
   nothing raises.
2. Train/serve featurizer parity: an article scored through the production
   path (score_article) yields the same prediction as the training-path
   featurization (ml_features called directly) — the skew test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# These were bare importorskip calls, which meant that in exactly the broken
# environment — the one shipped for weeks with no lightgbm — this entire module
# silently skipped instead of failing. A skip is only legitimate when someone
# has deliberately opted into an ML-less env; anywhere else (CI, a synced `pt`)
# a missing dependency is a bug the suite must report.
_ALLOW_MISSING = os.environ.get("PT_ALLOW_MISSING_ML") == "1"
for _mod in ("lightgbm", "sklearn"):
    if importlib.util.find_spec(_mod) is None:
        if _ALLOW_MISSING:
            pytest.skip(f"{_mod} not installed (PT_ALLOW_MISSING_ML=1)",
                        allow_module_level=True)
        pytest.fail(
            f"{_mod} is not installed — ML sentiment would be dead at runtime. "
            f"Run ./update.sh to sync the conda env. Set PT_ALLOW_MISSING_ML=1 "
            f"to skip these tests deliberately instead.",
            pytrace=False)

import numpy as np  # noqa: E402
import scipy.sparse as sp  # noqa: E402

from portfolio_tracker import ml_features as mf  # noqa: E402
from portfolio_tracker import ml_sentiment as ms  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    ms.reset_for_tests()
    yield
    ms.reset_for_tests()


@pytest.fixture()
def tiny_artifact(tmp_path, monkeypatch):
    """A real (tiny) LightGBM artifact with the production schema."""
    import lightgbm as lgb

    rng = np.random.default_rng(0)
    n = 400
    texts = [f"company {'beats' if i % 2 else 'misses'} earnings row{i}" for i in range(n)]
    counts = mf.hash_counts(texts)
    idf = mf.fit_idf([counts])
    # df-pruning mask, same construction as 06_build_features.build_mask
    dfreq = np.asarray((counts > 0).sum(axis=0)).ravel()
    mask = np.sort(np.argsort(dfreq)[::-1][:4096]).astype("int32")
    dense = np.asarray([mf.dense_vector({"title": t, "session_class": "dateonly_cc"})
                        for t in texts], dtype="float32")
    X = sp.hstack([mf.apply_idf(counts, idf)[:, mask], sp.csr_matrix(dense)],
                  format="csr")
    y = np.where(np.arange(n) % 2, 0.5, -0.5) + rng.normal(0, 0.1, n)
    bst = lgb.train({"objective": "regression", "verbosity": -1, "min_data_in_leaf": 5},
                    lgb.Dataset(X, y), num_boost_round=20)

    d = tmp_path / "mlsent-v1"
    d.mkdir()
    bst.save_model(str(d / "model.lgbm.txt"))
    np.save(d / "idf.npy", idf)
    np.save(d / "col_mask.npy", mask)
    (d / "feature_schema.json").write_text(json.dumps(mf.feature_schema()))
    (d / "tier_cuts.json").write_text(json.dumps({
        "tiers": ["very_bearish", "bearish", "neutral", "bullish", "very_bullish"],
        "cuts": [-0.4, -0.15, 0.15, 0.4],
    }))
    (d / "meta.json").write_text(json.dumps({"version": "test"}))
    monkeypatch.setenv("MLSENT_MODEL_DIR", str(d))
    return d, bst, idf, mask


def test_unavailable_without_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("MLSENT_MODEL_DIR", str(tmp_path / "nothing_here"))
    assert ms.available() is False
    assert ms.score_article({"headline": "x"}, "AAPL") is None
    assert ms.aggregate([{"sar_pred": 1.0, "relevance": 1.0}]) is None


def test_schema_mismatch_degrades(tiny_artifact):
    d, _, _, _ = tiny_artifact
    schema = json.loads((d / "feature_schema.json").read_text())
    schema["hash_dim"] = 123
    (d / "feature_schema.json").write_text(json.dumps(schema))
    assert ms.available() is False


def test_score_article_and_parity(tiny_artifact):
    d, bst, idf, mask = tiny_artifact
    assert ms.available() is True

    article = {"headline": "company beats earnings row1", "summary": "",
               "source": "Reuters", "datetime": 0, "n_duplicates": 0}
    out = ms.score_article(article, "AAPL")
    assert out is not None and "sar_pred" in out and "relevance" in out

    # training-path featurization of the same article must give the same pred
    counts = mf.hash_counts([mf.text_for_hashing(article["headline"], "")])
    dense = np.asarray([mf.dense_vector({
        "title": article["headline"], "summary": "",
        "publisher_tier": mf.publisher_tier("Reuters"),
        "relevance": out["relevance"], "n_duplicates": 0, "co_mention_count": 1,
        "session_class": "dateonly_cc",
        # datetime=0 -> production stamps day_of_week/month from 'now'
        "day_of_week": __import__("datetime").datetime.now(ms._ET).isoweekday(),
        "month": __import__("datetime").datetime.now(ms._ET).month,
    })], dtype="float32")
    X = sp.hstack([mf.apply_idf(counts, idf)[:, mask], sp.csr_matrix(dense)],
                  format="csr")
    expected = float(bst.predict(X)[0])
    assert out["sar_pred"] == pytest.approx(expected, abs=1e-3)


def test_aggregate_weighting_and_tiers(tiny_artifact):
    import time as _t

    now = _t.time()
    scored = [
        {"sar_pred": 1.0, "relevance": 1.0, "datetime": now, "source": "Reuters"},
        {"sar_pred": -1.0, "relevance": 0.05, "datetime": now - 20 * 86400,
         "source": "openpr"},
    ]
    out = ms.aggregate(scored)
    assert out is not None
    # high-relevance fresh tier-1 article must dominate the stale irrelevant one
    assert out["ml_sar"] > 0.8
    assert out["ml_tier"] == "very_bullish"
    assert 0 < out["ml_confidence"] <= 1
    assert out["ml_n"] == 2


def test_aggregate_empty_and_none_entries(tiny_artifact):
    assert ms.aggregate([]) is None
    assert ms.aggregate([None, {"sar_pred": None}]) is None


# --------------------------- publish-last race ------------------------------


def test_load_never_publishes_a_half_initialized_state(tiny_artifact, monkeypatch):
    """No caller may ever observe {loaded: True, ok: False, reason: ""}.

    _load() used to set `loaded` BEFORE the multi-second lightgbm/scipy/sklearn
    import while the fast path read it without the lock, so a thread arriving
    mid-load saw a published-but-empty state, reported the model unavailable
    with no reason, and silently fell back to the LLM — one ticker per refresh,
    which is what "ML coverage 14/15" was.
    """
    import threading
    import time

    gate = threading.Event()
    real_schema = mf.feature_schema

    # Stall the loader from inside its own critical section. feature_schema() is
    # the ideal hook: _load() calls it while holding _LOCK, and nothing else in
    # the process does.
    def slow_schema():
        gate.set()          # loader is inside the critical section now...
        time.sleep(0.4)     # ...and stays there
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
    # Every observation must be a real answer: either loaded (ok) or a genuine
    # failure that says why. Never the empty-reason limbo state.
    for ok, reason in observations:
        assert ok or reason, "observed loaded=True, ok=False, reason='' — the race is back"
    assert all(ok for ok, _ in observations)


# --------------------------- display_score ----------------------------------


_CUTS = [-0.02026843903586268, -0.0007415462750941515,
         0.00506285997107625, 0.015028036199510098]   # the shipped mlsent-v1 cuts


def _client_tier(score):
    """Mirror of app.js nsTierFromScore — the client's fixed thresholds."""
    if score <= -0.5:
        return "very_bearish"
    if score <= -0.15:
        return "bearish"
    if score < 0.15:
        return "neutral"
    if score < 0.5:
        return "bullish"
    return "very_bullish"


def test_display_score_pins_cuts_to_anchors():
    for cut, anchor in zip(_CUTS, ms._DISP_ANCHORS):
        assert ms.display_score(cut, _CUTS) == pytest.approx(anchor, abs=1e-9)


def test_display_score_is_monotone_and_bounded():
    xs = [-5.0, -1.0, -0.1, -0.05] + \
         [(-0.03 + i * 0.0005) for i in range(140)] + [0.1, 1.0, 5.0]
    ys = [ms.display_score(x, _CUTS) for x in xs]
    assert all(y is not None for y in ys)
    assert all(-1.0 <= y <= 1.0 for y in ys)
    # Strictly increasing across everything the model actually produces. Past
    # about |SAR| > 0.29 the tanh tail saturates to +/-1.0 in float64 — roughly
    # 20x the 95th-percentile cut, so unreachable in practice.
    lys = [ms.display_score(x, _CUTS) for x in xs if abs(x) <= 0.1]
    assert all(b > a for a, b in zip(lys, lys[1:])), "must be strictly increasing"
    assert all(b >= a for a, b in zip(ys, ys[1:])), "must never invert"


def test_display_score_agrees_with_server_tier():
    """The whole point of anchoring on the client's thresholds: bucketing the
    displayed number must reproduce the tier the server computed from raw SAR.

    Holds for every value strictly between cuts. Exactly ON a positive cut the
    two disagree by one band, because _tier buckets left-open/right-closed
    (matching np.searchsorted in training) while app.js's nsTierFromScore is
    symmetric about zero. That is unobservable in the app — nothing ever
    buckets a value client-side that also carries a server ml_tier — and
    nsTierFromScore is deliberately left alone because it also serves LLM
    scores, where the symmetric convention is the intended one.
    """
    tiers = ["very_bearish", "bearish", "neutral", "bullish", "very_bullish"]
    for i in range(400):
        sar = -0.06 + i * 0.0004
        assert sar not in _CUTS
        server = ms._tier(sar, _CUTS, tiers)
        client = _client_tier(ms.display_score(sar, _CUTS))
        assert client == server, f"sar={sar}: server={server} client={client}"


def test_display_score_spreads_where_tanh_collapsed():
    """The regression this exists to prevent.

    tanh(sar/2) squeezes the model's entire 5th-to-95th-percentile range into
    under 0.02 of display width — two ticks at 2 decimal places — and the whole
    neutral band (cuts[1]..cuts[2], 45% of the mass by construction) renders as
    a literal zero. display_score spends the full [-0.5, +0.5] on the same range.
    """
    import math

    tanh_span = math.tanh(_CUTS[3] / 2.0) - math.tanh(_CUTS[0] / 2.0)
    assert tanh_span < 0.02
    for sar in (_CUTS[1], 0.0, _CUTS[2]):
        assert f"{math.tanh(sar / 2.0):.2f}" in ("-0.00", "0.00")

    disp_span = ms.display_score(_CUTS[3], _CUTS) - ms.display_score(_CUTS[0], _CUTS)
    assert disp_span == pytest.approx(1.0, abs=1e-9)
    assert len({f"{ms.display_score(c, _CUTS):.2f}" for c in _CUTS}) == 4


def test_display_score_rejects_bad_input():
    assert ms.display_score(None, _CUTS) is None
    assert ms.display_score(float("nan"), _CUTS) is None
    assert ms.display_score("x", _CUTS) is None
    assert ms.display_score(0.0, [0.1, 0.0, 0.2, 0.3]) is None   # non-monotone cuts
    assert ms.display_score(0.0, [0.1, 0.2]) is None             # wrong arity


def test_aggregate_exposes_display_score(tiny_artifact):
    import time as _t

    out = ms.aggregate([{"sar_pred": 0.9, "relevance": 1.0,
                         "datetime": _t.time(), "source": "Reuters"}])
    assert out is not None
    # tiny_artifact's cuts are [-0.4, -0.15, 0.15, 0.4]
    assert out["ml_score_disp"] == pytest.approx(
        ms.display_score(out["ml_sar"], [-0.4, -0.15, 0.15, 0.4]), abs=1e-9)
    # raw fields must be untouched — the sentiment history and compute_diagnostics
    # are built on them
    import math
    assert out["ml_score"] == pytest.approx(math.tanh(out["ml_sar"] / 2.0), abs=1e-4)
