"""Tests for news_sentiment.rescore_window — the instant News-window change.

Built entirely on hand-made _NEWS_CACHE fixtures: the point of this function is
that it touches nothing but the cache, so a test that needed the network would
be testing the wrong thing.

The property that matters most is the honest one — widening the window cannot
conjure articles that were never fetched, and the result has to SAY so rather
than presenting a re-weighting of 7 days of evidence as a 30-day read.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_tracker import news_sentiment as ns  # noqa: E402

DAY = 86400


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    ns._NEWS_CACHE.clear()
    ns._SENTIMENT_CACHE.clear()
    # A rescore must never write history — it would double-count the day and
    # skew the rolling quantiles calibrate_tier reads back.
    writes = []
    monkeypatch.setattr(ns, "_history_append", lambda rec: writes.append(rec))
    monkeypatch.setattr(ns, "_calibration_scores", lambda: [])
    yield writes
    ns._NEWS_CACHE.clear()
    ns._SENTIMENT_CACHE.clear()


def _article(age_days, score, *, url=None, headline="Co beats estimates", ml=None):
    a = {
        "headline": headline, "summary": "", "source": "Reuters",
        "datetime": int(time.time() - age_days * DAY),
        "url": url or f"http://x/{headline}/{age_days}",
        "score": score, "relevance": "high", "n_duplicates": 0,
    }
    if ml is not None:
        a["ml_sar"] = ml
        a["ml_relevance"] = 0.9
    return a


def _cache_news(sym, days, articles):
    ns._cache_put(ns._NEWS_CACHE, f"news|{sym}|{days}", articles, ns._NEWS_TTL)


# ------------------------------ core behaviour ------------------------------


def test_rescores_from_cache_without_network(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("rescore_window must not fetch")
    monkeypatch.setattr(ns, "_fh_call", _boom)
    monkeypatch.setattr(ns, "_fetch_yf_news", _boom)
    monkeypatch.setattr(ns, "_nvidia_call", _boom)

    _cache_news("AAPL", 7, [_article(1, 0.8), _article(2, 0.6)])
    out = ns.rescore_window(["AAPL"], 7)
    s = out["sentiment"]["AAPL"]
    assert s is not None
    assert s["rescored"] is True
    assert s["s_idio"] > 0
    assert out["coverage"]["AAPL"]["n_scored"] == 2


def test_narrowing_drops_older_articles():
    _cache_news("AAPL", 30, [_article(1, 1.0), _article(20, -1.0)])
    wide = ns.rescore_window(["AAPL"], 30)["sentiment"]["AAPL"]
    ns._SENTIMENT_CACHE.clear()
    narrow = ns.rescore_window(["AAPL"], 3)["sentiment"]["AAPL"]
    # The 20-day-old bearish article is outside a 3-day window entirely.
    assert narrow["s_idio"] > wide["s_idio"]
    assert narrow["article_count"] == 1


def test_widening_adds_nothing_and_says_so():
    """The honest limitation. News is cached per window, so a 7D fetch only ever
    pulled 7 days — widening to 30D re-weights the SAME evidence."""
    arts = [_article(1, 0.5), _article(2, 0.5)]
    _cache_news("AAPL", 7, arts)
    seven = ns.rescore_window(["AAPL"], 7)
    ns._SENTIMENT_CACHE.clear()
    thirty = ns.rescore_window(["AAPL"], 30)
    assert thirty["coverage"]["AAPL"]["n_scored"] == seven["coverage"]["AAPL"]["n_scored"]
    # ...and the caller is told, so the UI can stop it looking like a real
    # 30-day read.
    assert thirty["coverage"]["AAPL"]["truncated_by_fetch"] is True
    assert seven["coverage"]["AAPL"]["truncated_by_fetch"] is False
    assert seven["coverage"]["AAPL"]["from_windows"] == [7]


def test_truncation_is_about_the_fetch_not_the_news_flow():
    """A quiet ticker with no week-old headlines is NOT a truncated fetch. An
    earlier timestamp-based heuristic flagged both, which would have put the
    "narrower cache" warning on perfectly complete reads."""
    _cache_news("AAPL", 30, [_article(1, 0.5)])      # one fresh article, 30D fetched
    out = ns.rescore_window(["AAPL"], 30)
    assert out["coverage"]["AAPL"]["truncated_by_fetch"] is False


def test_union_spans_every_cached_window():
    """No single cache key holds everything ever fetched for a ticker."""
    _cache_news("AAPL", 7, [_article(1, 0.5, url="u1")])
    _cache_news("AAPL", 30, [_article(9, -0.5, url="u2")])
    out = ns.rescore_window(["AAPL"], 30)
    assert out["coverage"]["AAPL"]["n_scored"] == 2


def test_dedup_prefers_the_scored_copy():
    """Only articles[:_SCORE_BATCH] of each window get scored, so the same
    headline is often cached scored in one window and unscored in another."""
    unscored = _article(1, None, url="same")
    unscored.pop("score")
    scored = _article(1, 0.9, url="same")
    _cache_news("AAPL", 30, [unscored])
    _cache_news("AAPL", 7, [scored])
    out = ns.rescore_window(["AAPL"], 30)
    assert out["coverage"]["AAPL"]["n_scored"] == 1
    assert out["sentiment"]["AAPL"]["s_idio"] > 0


def test_unscored_articles_leave_the_ticker_alone():
    a = _article(1, None)
    a.pop("score")
    _cache_news("AAPL", 7, [a])
    out = ns.rescore_window(["AAPL"], 7)
    assert out["sentiment"]["AAPL"] is None
    assert out["coverage"]["AAPL"]["n_scored"] == 0


def test_never_writes_history(_clean_caches):
    _cache_news("AAPL", 7, [_article(1, 0.8)])
    ns.rescore_window(["AAPL"], 7)
    assert _clean_caches == [], "a rescore must not append to the sentiment history"


def test_brief_is_carried_forward_and_flagged():
    _cache_news("AAPL", 7, [_article(1, 0.8)])
    ns._cache_put(ns._SENTIMENT_CACHE, "sentiment|AAPL|7",
                  {"summary": "Old brief.", "beta_used": 1.3, "lookback_days": 7},
                  ns._SENTIMENT_TTL)
    s = ns.rescore_window(["AAPL"], 7)["sentiment"]["AAPL"]
    # The LLM wrote it for a different article set and it can't be regenerated
    # without a NIM call — carry it, but don't pass it off as current.
    assert s["summary"] == "Old brief."
    assert s["brief_stale"] is True
    assert s["beta_used"] == 1.3      # beta reused rather than refetched


def test_market_falls_back_to_the_nearest_cached_window():
    _cache_news("AAPL", 7, [_article(1, 0.8)])
    ns._cache_put(ns._SENTIMENT_CACHE, "sentiment|__market__|7",
                  {"score": -0.4, "tier": "bearish"}, ns._SENTIMENT_TTL)
    out = ns.rescore_window(["AAPL"], 30)     # no 30D market entry exists
    assert out["market"]["score"] == -0.4
    assert out["sentiment"]["AAPL"]["s_mkt_window"] == 7


def test_ml_reaggregates_without_inference(monkeypatch):
    """ml_sentiment.aggregate is a weighted mean over cached SARs — no model
    forward pass. score_article must never be called."""
    import portfolio_tracker.ml_sentiment as mls

    monkeypatch.setattr(mls, "score_article",
                        lambda *a, **k: pytest.fail("no inference during a rescore"))
    called = {}

    def _fake_aggregate(scored, now=None):
        called["scored"] = scored
        return {"ml_sar": 0.02, "ml_score": 0.01, "ml_score_disp": 0.62,
                "ml_tier": "very_bullish", "ml_confidence": 0.7, "ml_n": len(scored)}

    monkeypatch.setattr(mls, "aggregate", _fake_aggregate)
    _cache_news("AAPL", 7, [_article(1, 0.4, ml=0.02), _article(2, 0.2, ml=0.01)])
    s = ns.rescore_window(["AAPL"], 7)["sentiment"]["AAPL"]
    assert s["disp_source"] == "ml"
    assert s["tier"] == "very_bullish"
    # ml_score_disp, not the unreadable raw tanh value
    assert s["score"] == 0.62
    assert s["llm_score"] is not None       # LLM preserved as the challenger
    # the numeric ml_relevance, not the LLM's categorical "high"
    assert all(isinstance(x["relevance"], float) for x in called["scored"])


def test_unknown_symbol_is_reported_not_crashed():
    out = ns.rescore_window(["NOSUCH"], 7)
    assert out["sentiment"]["NOSUCH"] is None
    assert out["coverage"]["NOSUCH"]["n_scored"] == 0


def test_days_are_clamped():
    _cache_news("AAPL", 7, [_article(1, 0.8)])
    assert ns.rescore_window(["AAPL"], 9999)["days"] in ns._LOOKBACK_CHOICES
