"""The background news read: when it runs, what it reads, what it remembers."""

import threading
import time
from datetime import UTC, datetime

import pytest

from convexity import investments, jobs, news_sentiment, news_watch

NOW = 2_000_000_000.0
DAY = 86400.0


def _at(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat()


@pytest.fixture
def book(monkeypatch):
    reads = {}
    monkeypatch.setattr(news_watch, "_keys_set", lambda: True)
    monkeypatch.setattr(investments, "held_symbols", lambda: ["AAPL", "MSFT", "SPY"])
    monkeypatch.setattr(
        investments, "_pack_kind", lambda s, name=None: {"sector": None, "fund": s == "SPY"}
    )
    monkeypatch.setattr(news_sentiment, "get_cached_sentiment", lambda s, days=None: reads.get(s))
    return reads


def test_only_old_or_missing_reads_of_shares_are_due(book):
    book["AAPL"] = {"news": {"assessed_at": _at(NOW - 2 * DAY)}}
    assert news_watch.due(NOW) == ["MSFT"]  # AAPL is fresh, SPY is a fund, MSFT has no read
    book["AAPL"] = {"news": {"assessed_at": _at(NOW - 4 * DAY)}}
    assert news_watch.due(NOW) == ["AAPL", "MSFT"]


def test_no_keys_no_pass(book, monkeypatch):
    monkeypatch.setattr(news_watch, "_keys_set", lambda: False)
    assert news_watch.due(NOW) == []


def test_a_share_tried_lately_waits(book):
    news_watch._mark("MSFT", NOW - DAY)
    assert news_watch.due(NOW) == ["AAPL"]
    assert sorted(news_watch.due(NOW + 3 * DAY)) == ["AAPL", "MSFT"]


def test_a_damaged_file_only_means_asking_again(book):
    news_watch._mark("AAPL", NOW)
    news_watch._path().write_text("{nope", encoding="utf-8")
    assert news_watch._tried() == {}
    assert news_watch.due(NOW) == ["AAPL", "MSFT"]


def test_the_pass_reads_small_and_remembers_each_outcome(book, monkeypatch):
    seen = {}

    def refresh(
        symbols, context=None, progress_cb=None, *, cancel=None, refresh_market=True, workers=2
    ):
        seen.update(symbols=symbols, market=refresh_market, workers=workers)
        progress_cb("symbol", {"symbol": "AAPL", "outcome": "ok"})
        progress_cb("symbol", {"symbol": "MSFT", "outcome": "failed"})
        return {"status": {"scored": 1, "failed": 1, "empty": 0}, "cancelled": False}

    forgot = []
    monkeypatch.setattr(news_sentiment, "refresh_sentiment", refresh)
    monkeypatch.setattr(news_watch, "_context", lambda job, s: {})
    monkeypatch.setattr(investments, "forget_page", lambda: forgot.append(1))

    class Job:
        cancel = threading.Event()

    assert news_watch.run_pass(Job()) == "done"
    assert seen == {"symbols": ["AAPL", "MSFT"], "market": False, "workers": 1}
    assert forgot
    now = time.time()
    tried = news_watch._tried()
    assert now - tried["AAPL"] < 60
    # A failed read comes due again after hours, not days.
    assert news_watch.due(now + news_watch.RETRY_S + 60) == ["MSFT"]


def test_a_cancelled_read_isnt_remembered(book, monkeypatch):
    def refresh(symbols, context=None, progress_cb=None, **kw):
        progress_cb("symbol", {"symbol": "AAPL", "outcome": "cancelled"})
        return {"status": {}, "cancelled": True}

    monkeypatch.setattr(news_sentiment, "refresh_sentiment", refresh)
    monkeypatch.setattr(news_watch, "_context", lambda job, s: {})
    monkeypatch.setattr(investments, "forget_page", lambda: None)

    class Job:
        cancel = threading.Event()

    assert news_watch.run_pass(Job()) == "cancelled"
    assert "AAPL" not in news_watch._tried()


def test_it_waits_for_anything_running_and_gives_way_to_the_owner(book, monkeypatch):
    class Running:
        @staticmethod
        def is_terminal():
            return False

    monkeypatch.setattr(jobs, "current", lambda: Running())
    assert news_watch.tick(NOW, submit=lambda: "started") is None
    monkeypatch.setattr(jobs, "current", lambda: None)
    assert news_watch.tick(NOW, submit=lambda: "started") == "started"


def test_nothing_due_starts_nothing(book, monkeypatch):
    monkeypatch.setattr(jobs, "current", lambda: None)
    monkeypatch.setattr(news_watch, "due", lambda now=None: [])
    assert news_watch.tick(NOW, submit=lambda: "started") is None


def test_the_pass_is_a_hidden_background_job(book, monkeypatch):
    seen = {}

    def submit(**kw):
        seen.update(kw)
        return "job", "created"

    monkeypatch.setattr(jobs, "submit", submit)
    news_watch._submit()
    assert seen["phases"] == ["newsread"]
    assert seen["context"] == {"background": True}
