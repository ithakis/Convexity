import os
import sys
import time

import pytest


_REPO = os.path.dirname(os.path.dirname(__file__))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from portfolio_tracker import news_sentiment as ns


@pytest.fixture(autouse=True)
def _clean_news_state(monkeypatch, tmp_path):
    ns._NEWS_CACHE.clear()
    ns._SENTIMENT_CACHE.clear()
    with ns._FH_LIMITER._lock:
        ns._FH_LIMITER._calls.clear()
    with ns._NV_LIMITER._lock:
        ns._NV_LIMITER._calls.clear()
    monkeypatch.setattr(ns, "_PERSIST_FILE", tmp_path / ".portfolio_tracker_news.json")
    monkeypatch.setattr(ns, "_schedule_persist", lambda: None)
    monkeypatch.setattr(ns, "_fh_rate_limit_until", 0.0)
    monkeypatch.setattr(ns, "_nv_rate_limit_until", 0.0)
    yield
    ns._NEWS_CACHE.clear()
    ns._SENTIMENT_CACHE.clear()


def test_refresh_symbol_restores_previous_cache_on_transient_failure(monkeypatch):
    prev_news = [{"headline": "Old headline"}]
    prev_sentiment = {
        "tier": "bullish",
        "score": 0.45,
        "summary": "Previous cached sentiment.",
        "assessed_at": "2026-05-23T09:00:00+00:00",
    }
    now = time.time()
    ns._NEWS_CACHE["news|MSFT"] = (now, ns._NEWS_TTL, prev_news)
    ns._SENTIMENT_CACHE["sentiment|MSFT"] = (now, ns._SENTIMENT_TTL, prev_sentiment)

    monkeypatch.setattr(ns, "get_news_sentiment", lambda symbol: None)

    result = ns._refresh_symbol_sentiment("MSFT")

    assert result == prev_sentiment
    assert ns._cache_get(ns._NEWS_CACHE, "news|MSFT") == prev_news
    assert ns._cache_get(ns._SENTIMENT_CACHE, "sentiment|MSFT") == prev_sentiment


def test_refresh_sentiment_returns_consistent_status_shape(monkeypatch):
    market = {
        "tier": "neutral",
        "score": 0.0,
        "summary": "Market unchanged.",
        "assessed_at": "2026-05-23T10:00:00+00:00",
    }
    sentiments = {
        "MSFT": {
            "tier": "bullish",
            "score": 0.62,
            "summary": "Positive incremental signal.",
            "assessed_at": "2026-05-23T10:01:00+00:00",
        },
        "TSLA": None,
    }

    monkeypatch.setattr(ns, "FINNHUB_API_KEY", "fh-test")
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "nv-test")
    monkeypatch.setattr(ns, "_refresh_market_sentiment", lambda: market)
    monkeypatch.setattr(ns, "_refresh_symbol_sentiment", lambda symbol: sentiments[symbol])

    result = ns.refresh_sentiment(["MSFT", "TSLA"])
    status = result["status"]

    assert result["market"] == market
    assert result["portfolio"] == sentiments
    assert status["finnhub_key"] is True
    assert status["finnhub_key_set"] is True
    assert status["nvidia_key"] is True
    assert status["nvidia_key_set"] is True
    assert status["market_ok"] is True
    assert status["fetched"] == 1
    assert status["total"] == 2
    assert "finnhub_calls_used" in status
    assert "finnhub_calls_limit" in status
    assert "nvidia_calls_used" in status
    assert "nvidia_calls_limit" in status


def test_status_reports_backoff_windows(monkeypatch):
    monkeypatch.setattr(ns, "FINNHUB_API_KEY", "fh-test")
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "nv-test")
    monkeypatch.setattr(ns, "_fh_rate_limit_until", time.time() + 9.2)
    monkeypatch.setattr(ns, "_nv_rate_limit_until", time.time() + 4.4)

    status = ns.status()

    assert status["finnhub_backoff_s"] >= 9
    assert status["nvidia_backoff_s"] >= 4
    assert status["finnhub_calls_limit"] == ns._FH_LIMITER.max
    assert status["nvidia_calls_limit"] == ns._NV_LIMITER.max


def test_nvidia_call_retries_after_non_json(monkeypatch):
    class _Message:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Message(content)

    class _Response:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    class _Completions:
        def __init__(self, payloads):
            self.payloads = list(payloads)

        def create(self, **_kwargs):
            return _Response(self.payloads.pop(0))

    class _Chat:
        def __init__(self, payloads):
            self.completions = _Completions(payloads)

    class _Client:
        def __init__(self, payloads):
            self.chat = _Chat(payloads)

    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "nv-test")
    monkeypatch.setattr(ns, "_get_client", lambda: _Client([
        "not-json",
        '{"tier":"bullish","score":0.5,"summary":"Recovered on retry."}',
    ]))
    monkeypatch.setattr(ns.time, "sleep", lambda *_args, **_kwargs: None)

    result = ns._nvidia_call("system", "user")

    assert result == {
        "tier": "bullish",
        "score": 0.5,
        "summary": "Recovered on retry.",
    }