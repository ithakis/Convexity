"""Unit tests for portfolio_tracker.ml_features — the shared featurizer whose
train/serve parity the deployed model depends on."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_tracker import ml_features as mf

pytest.importorskip("sklearn", reason="featurizer text side needs sklearn")
import numpy as np  # noqa: E402


def test_dense_vector_matches_schema_length():
    assert len(mf.dense_vector({})) == len(mf.DENSE_COLUMNS)


def test_event_flags_fire():
    ev = mf.event_flags("Company beats earnings, raises guidance, announces buyback")
    assert ev["earnings"] and ev["guidance"] and ev["buyback"]
    assert not ev["fda"] and not ev["bankruptcy"]


def test_publisher_tier_in_sync_with_news_sentiment():
    """publisher_tier is a copy of news_sentiment._source_weight tiering (kept
    separate so ml_features never imports the LLM stack) — this test IS the
    sync mechanism."""
    from portfolio_tracker.news_sentiment import _source_weight

    for src in ("Reuters", "Bloomberg", "CNBC", "Motley Fool", "Benzinga",
                "Random Blog", "GlobeNewswire", "MarketWatch", None, ""):
        assert mf.publisher_tier(src) == _source_weight(src or ""), src


def test_feature_schema_stable_across_calls():
    assert mf.feature_schema() == mf.feature_schema()
    assert mf.feature_schema()["hash_dim"] == mf.HASH_DIM


def test_hashing_truncates_summary():
    long_summary = "unique_tail_token " * 200
    a = mf.text_for_hashing("title", long_summary)
    assert len(a) <= len("title ") + mf.HASH_SUMMARY_MAX_CHARS


def test_tfidf_rows_are_unit_norm():
    X = mf.hash_counts(["apple beats earnings", "microsoft cuts guidance"])
    idf = mf.fit_idf([X])
    Xt = mf.apply_idf(X, idf)
    norms = np.sqrt(Xt.multiply(Xt).sum(axis=1)).A1
    assert np.allclose(norms, 1.0)


def test_idf_downweights_common_terms():
    X = mf.hash_counts(["apple rises", "apple falls", "apple flat", "zebra jumps"])
    idf = mf.fit_idf([X])
    apple_idx = mf.hash_counts(["apple"]).indices[0]
    zebra_idx = mf.hash_counts(["zebra"]).indices[0]
    assert idf[apple_idx] < idf[zebra_idx]


def test_dense_vector_market_context_passthrough():
    row = mf.dense_vector({"spy_ret_1d": 0.01, "tkr_ret_5d": -0.02})
    cols = dict(zip(mf.DENSE_COLUMNS, row))
    assert cols["spy_ret_1d"] == pytest.approx(0.01)
    assert cols["tkr_ret_5d"] == pytest.approx(-0.02)
