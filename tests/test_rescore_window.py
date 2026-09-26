"""Tests for news_sentiment.rescore_window — the instant News-window change.

Built entirely on hand-made _NEWS_CACHE fixtures: the point of this function is
that it touches nothing but the cache, so a test that needed the network would
be testing the wrong thing.

The property that matters most is the honest one — widening the window cannot
conjure headlines that were never fetched or read, and the result has to SAY so
rather than presenting a re-weighting of 7 days of evidence as a 30-day read.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from convexity import news_sentiment as ns  # noqa: E402

DAY = 86400


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    ns._NEWS_CACHE.clear()
    ns._SENTIMENT_CACHE.clear()
    # A rescore must never write history — it would double-count the day in
    # the Track record.
    writes = []
    monkeypatch.setattr(ns, "_history_append", lambda rec: writes.append(rec))
    monkeypatch.setattr(ns, "_schedule_persist", lambda: None)
    yield writes
    ns._NEWS_CACHE.clear()
    ns._SENTIMENT_CACHE.clear()


def _article(age_days, score, *, url=None, headline="Co beats estimates", lens="financials"):
    a = {
        "headline": headline, "summary": "", "source": "Reuters",
        "datetime": int(time.time() - age_days * DAY),
        "url": url or f"http://x/{headline}/{age_days}", "n_duplicates": 0,
    }
    if score is not None:
        a.update({"lens": lens, "llm_score": score, "fact": f"{headline} fact"})
    a["aid"] = ns._article_id(a)
    return a


def _cache_news(sym, days, articles):
    ns._cache_put(ns._NEWS_CACHE, f"news|{sym}|{days}", articles, ns._NEWS_TTL)


# ------------------------------ core behaviour ------------------------------


def test_rescores_from_cache_without_network(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("rescore_window must not fetch or score")
    monkeypatch.setattr(ns, "_fh_call", _boom)
    monkeypatch.setattr(ns, "_fetch_yf_news", _boom)
    monkeypatch.setattr(ns, "_nvidia_call", _boom)
    monkeypatch.setattr(ns, "_market_read", _boom)

    _cache_news("AAPL", 7, [_article(1, 2), _article(2, 1)])
    out = ns.rescore_window(["AAPL"], 7)
    s = out["sentiment"]["AAPL"]
    assert s is not None
    assert s["news"]["rescored"] is True
    assert s["news"]["score"] > 0
    assert s["news"]["lenses"]["financials"]["n"] == 2
    assert out["coverage"]["AAPL"]["n_scored"] == 2


def test_narrowing_drops_older_articles():
    _cache_news("AAPL", 30, [_article(1, 2), _article(20, -2)])
    wide = ns.rescore_window(["AAPL"], 30)["sentiment"]["AAPL"]
    ns._SENTIMENT_CACHE.clear()
    narrow = ns.rescore_window(["AAPL"], 3)["sentiment"]["AAPL"]
    # The 20-day-old bearish headline is outside a 3-day window entirely.
    assert narrow["news"]["score"] > wide["news"]["score"]
    assert narrow["article_count"] == 1


def test_lenses_are_re_aggregated_per_window():
    _cache_news("AAPL", 30, [_article(1, 2, lens="financials"),
                             _article(10, -2, lens="regulation", headline="Fine")])
    narrow = ns.rescore_window(["AAPL"], 7)["sentiment"]["AAPL"]["news"]["lenses"]
    assert narrow["financials"]["score"] == 2.0
    assert narrow["regulation"] is None           # out of window: no news, not 0
    ns._SENTIMENT_CACHE.clear()
    wide = ns.rescore_window(["AAPL"], 30)["sentiment"]["AAPL"]["news"]["lenses"]
    assert wide["regulation"]["score"] == -2.0


def test_widening_adds_nothing_and_says_so():
    """The honest limitation. News is cached per window, so a 7D fetch only ever
    pulled 7 days — widening to 30D re-weights the SAME evidence."""
    arts = [_article(1, 1), _article(2, 1)]
    _cache_news("AAPL", 7, arts)
    seven = ns.rescore_window(["AAPL"], 7)
    ns._SENTIMENT_CACHE.clear()
    thirty = ns.rescore_window(["AAPL"], 30)
    assert thirty["coverage"]["AAPL"]["n_scored"] == seven["coverage"]["AAPL"]["n_scored"]
    assert thirty["coverage"]["AAPL"]["truncated_by_fetch"] is True
    assert seven["coverage"]["AAPL"]["truncated_by_fetch"] is False
    assert seven["coverage"]["AAPL"]["from_windows"] == [7]


def test_truncation_is_about_the_fetch_not_the_news_flow():
    """A quiet ticker with no week-old headlines is NOT a truncated fetch."""
    _cache_news("AAPL", 30, [_article(1, 1)])
    out = ns.rescore_window(["AAPL"], 30)
    assert out["coverage"]["AAPL"]["truncated_by_fetch"] is False


def test_union_spans_every_cached_window():
    """No single cache key holds everything ever fetched for a ticker."""
    _cache_news("AAPL", 7, [_article(1, 1, url="u1")])
    _cache_news("AAPL", 30, [_article(9, -1, url="u2")])
    out = ns.rescore_window(["AAPL"], 30)
    assert out["coverage"]["AAPL"]["n_scored"] == 2


def test_dedup_prefers_the_read_copy():
    """Only _SCORE_BATCH headlines of each window are read, so the same
    headline is often cached read in one window and unread in another."""
    _cache_news("AAPL", 30, [_article(1, None, url="same")])
    _cache_news("AAPL", 7, [_article(1, 2, url="same")])
    out = ns.rescore_window(["AAPL"], 30)
    assert out["coverage"]["AAPL"]["n_scored"] == 1
    assert out["sentiment"]["AAPL"]["news"]["score"] > 0


def test_unread_articles_leave_the_ticker_alone():
    _cache_news("AAPL", 7, [_article(1, None)])
    out = ns.rescore_window(["AAPL"], 7)
    assert out["sentiment"]["AAPL"] is None
    assert out["coverage"]["AAPL"]["n_scored"] == 0


def test_never_writes_history(_clean_caches):
    _cache_news("AAPL", 7, [_article(1, 1)])
    ns.rescore_window(["AAPL"], 7)
    assert _clean_caches == [], "a rescore must not append to the sentiment history"


def test_brief_and_market_are_carried_forward():
    _cache_news("AAPL", 7, [_article(1, 2)])
    market = {"sar": -0.2, "z": -2.1, "pct": 2.0, "tier": "very_bearish", "score": -0.01,
              "horizon_days": 1, "confidence": 0.7, "model_version": "mlsent-v1.1"}
    ns._cache_put(ns._SENTIMENT_CACHE, "sentiment|AAPL|7",
                  {"news": {"brief": "Old brief.", "lookback_days": 7, "agreement": 0.9},
                   "market": market}, ns._SENTIMENT_TTL)
    s = ns.rescore_window(["AAPL"], 7)["sentiment"]["AAPL"]
    # The LLM wrote the brief for a different headline set and it can't be
    # regenerated without a NIM call — carry it, but don't pass it off as current.
    assert s["news"]["brief"] == "Old brief."
    assert s["news"]["brief_stale"] is True
    assert s["news"]["agreement"] == 0.9
    # The Market read is defined on its 7-day training window: carried as is.
    assert s["market"] == market
    assert s["divergence"]["kind"] == "good_news_weak_reaction"


def test_market_read_is_served_from_the_nearest_cached_window():
    _cache_news("AAPL", 7, [_article(1, 1)])
    ns._cache_put(ns._SENTIMENT_CACHE, "sentiment|__market__|7",
                  {"news": {"score": -0.8, "tier": "bearish"}}, ns._SENTIMENT_TTL)
    out = ns.rescore_window(["AAPL"], 30)     # no 30D market entry exists
    assert out["market"]["news"]["score"] == -0.8


def test_unknown_symbol_is_reported_not_crashed():
    out = ns.rescore_window(["NOSUCH"], 7)
    assert out["sentiment"]["NOSUCH"] is None
    assert out["coverage"]["NOSUCH"]["n_scored"] == 0


def test_days_are_clamped():
    _cache_news("AAPL", 7, [_article(1, 1)])
    assert ns.rescore_window(["AAPL"], 9999)["days"] in ns._LOOKBACK_CHOICES
