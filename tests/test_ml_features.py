"""Unit tests for convexity.ml_features — the shared featurizer whose
train/serve parity the deployed model depends on."""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from convexity import ml_features as mf

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
    from convexity.news_sentiment import _source_weight

    for src in (
        "Reuters",
        "Bloomberg",
        "CNBC",
        "Motley Fool",
        "Benzinga",
        "Random Blog",
        "GlobeNewswire",
        "MarketWatch",
        None,
        "",
    ):
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


# ------------------------------------------------------------ ticker-day window
def _win_item(sar, age_d=0.0, tier=1.0, dup=0, rel=1.0, lm=0.5, unc=0.01, boiler=False):
    return {
        "sar_pred": sar,
        "lm": lm,
        "unc": unc,
        "publisher_tier": tier,
        "n_duplicates": dup,
        "relevance": rel,
        "boiler": boiler,
        "datetime": 1_000_000.0 - age_d * 86400,
    }


def test_window_vector_names_every_column():
    row = mf.window_vector([_win_item(0.01)], 1_000_000.0, 1.0, 1.0, {})
    assert len(row) == len(mf.WINDOW_COLUMNS)


def test_window_vector_statistics():
    now = 1_000_000.0
    items = [
        _win_item(0.02, 0.0, tier=1.0),
        _win_item(-0.01, 2.0, tier=0.5, dup=3, lm=None, boiler=True),
    ]
    cols = dict(
        zip(mf.WINDOW_COLUMNS, mf.window_vector(items, now, 2 / 7, 0.1, {"tkr_ret_1d": 0.03}))
    )
    assert cols["enc_mean"] == pytest.approx(0.005)
    assert cols["enc_max"] == 0.02 and cols["enc_min"] == -0.01
    wm, wsum = mf.weighted_sar(items, now)
    assert cols["enc_wmean"] == pytest.approx(wm) and wm > 0.005  # fresher tier-1 dominates
    assert cols["lm_mean"] == pytest.approx(0.5)  # the None is skipped
    assert cols["share_tier1"] == 0.5 and cols["share_boiler"] == 0.5
    assert cols["fresh_days"] == 0.0
    assert cols["attn_shock"] == pytest.approx(mf.attention_shock(2 / 7, 0.1))
    assert cols["tkr_ret_1d"] == 0.03 and math.isnan(cols["spy_vol_20d"])


def test_window_vector_empty_is_nan_not_zero():
    cols = dict(zip(mf.WINDOW_COLUMNS, mf.window_vector([], 1.0, 0.0, 0.0, None)))
    assert math.isnan(cols["enc_mean"]) and math.isnan(cols["share_tier1"])
    assert cols["attn_shock"] == 0.0


def test_attention_shock_is_scale_free_and_finite():
    assert mf.attention_shock(1.0, 1.0) == 0.0
    assert mf.attention_shock(5.0, 0.0) > 3  # a first-ever burst is large, finite
    assert mf.attention_shock(0.0, 5.0) < 0


def test_article_weight_reads_either_tier_or_source():
    a = {"datetime": 100.0, "n_duplicates": 0, "relevance": 1.0}
    assert mf.article_weight(dict(a, publisher_tier=1.0), 100.0) == pytest.approx(
        mf.article_weight(dict(a, source="Reuters"), 100.0)
    )
    assert mf.article_weight(dict(a, source="Reuters"), 100.0 + 3 * 86400) == pytest.approx(
        math.exp(-1.0)
    )
