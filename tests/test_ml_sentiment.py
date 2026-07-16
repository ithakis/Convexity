"""Tests for portfolio_tracker.ml_sentiment — production inference.

Covers the two contracts that matter in production:
1. Graceful degradation: no artifact -> available() False, every call None,
   nothing raises.
2. Train/serve featurizer parity: an article scored through the production
   path (score_article) yields the same prediction as the training-path
   featurization (ml_features called directly) — the skew test.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("lightgbm")
pytest.importorskip("sklearn")

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
