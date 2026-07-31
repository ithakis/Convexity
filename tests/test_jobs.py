"""Tests for the background refresh job registry.

No network: fetch_one is monkeypatched. The properties under test are the ones
that are invisible in review and painful in production — replay gaplessness on
reconnect, the single-flight guard, cancellation actually de-queuing work, and
the guard that stops a background job overwriting good holdings with a batch
Yahoo mostly failed to answer.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_tracker import fetcher, jobs, persistence  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    jobs._JOBS.clear()
    jobs.__dict__["_CURRENT"] = None
    yield
    for j in list(jobs._JOBS.values()):
        j.cancel.set()
    jobs._JOBS.clear()
    jobs.__dict__["_CURRENT"] = None


@pytest.fixture()
def fake_quotes(monkeypatch):
    """fetch_one that sleeps, so cancellation has something to interrupt."""
    state = {"calls": [], "delay": 0.0, "error_symbols": set()}

    def _fetch_one(sym):
        state["calls"].append(sym)
        if state["delay"]:
            time.sleep(state["delay"])
        if sym in state["error_symbols"]:
            return {"symbol": sym, "error": "no data"}
        return {"symbol": sym, "price": 1.0, "beta": 1.0, "market_cap": 10}

    monkeypatch.setattr(fetcher, "fetch_one", _fetch_one)
    monkeypatch.setattr(fetcher, "_ordered_resolve", lambda entries: [e.upper() for e in entries])
    return state


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point persistence at a temp dir so tests never touch real holdings."""
    monkeypatch.setattr(persistence, "_VIEWS_FILE", tmp_path / "views.json")
    monkeypatch.setattr(persistence, "_WATCHLISTS_FILE", tmp_path / "watch.json")
    monkeypatch.setattr(persistence, "_MPT_FILE", tmp_path / "mpt.json")
    monkeypatch.setattr(persistence, "_LEGACY_SESSION_FILE", tmp_path / "legacy.json")
    return tmp_path


def _run_to_completion(job, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.is_terminal():
            return True
        time.sleep(0.02)
    return False


# ------------------------------ event stream --------------------------------


def test_seqs_are_monotonic_and_replay_is_gapless():
    job = jobs.Job(scope="current", phases=("quotes",), views=["v"],
                   entries={"v": "AAPL"}, days=7, context=None)
    for i in range(10):
        job.emit("item", n=i)
    frames, dropped = job.events_since(0)
    assert [f["seq"] for f in frames] == list(range(1, 11))
    assert dropped is False
    # A reconnect at seq 4 must resume at exactly 5 — no repeats, no holes.
    frames, dropped = job.events_since(4)
    assert [f["seq"] for f in frames] == [5, 6, 7, 8, 9, 10]
    assert dropped is False


def test_dropped_is_reported_when_the_ring_overflows(monkeypatch):
    monkeypatch.setattr(jobs, "_EVENT_MAXLEN", 8)
    job = jobs.Job(scope="current", phases=("quotes",), views=["v"],
                   entries={"v": "AAPL"}, days=7, context=None)
    job._events = type(job._events)(maxlen=8)
    for i in range(20):
        job.emit("item", n=i)
    # seq 1 is long gone, so a client resuming from it cannot be replayed
    # faithfully and must be told to cold-re-read instead.
    _, dropped = job.events_since(1)
    assert dropped is True
    frames, dropped_recent = job.events_since(19)
    assert [f["seq"] for f in frames] == [20]
    assert dropped_recent is False


def test_counts_ride_on_every_counted_frame():
    """The client assigns counts, never accumulates — see Job._COUNTED. A
    `phase` total is per-view, so adding it client-side double-counted."""
    job = jobs.Job(scope="current", phases=("quotes",), views=["v"],
                   entries={"v": "AAPL"}, days=7, context=None)
    job.bump("quotes_total", 4)
    for kind in jobs.Job._COUNTED:
        assert "counts" in job.emit(kind)
    assert "counts" not in job.emit("item_stage", stage="fetch")


def test_wait_blocks_until_a_new_frame_arrives():
    job = jobs.Job(scope="current", phases=("quotes",), views=["v"],
                   entries={"v": "AAPL"}, days=7, context=None)
    got = []

    def waiter():
        got.extend(job.wait(0, timeout=5.0))

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    assert got == []
    job.emit("item", n=1)
    t.join(5)
    assert [f["seq"] for f in got] == [1]


# ------------------------------ single flight -------------------------------


def test_second_submit_is_rejected_and_returns_the_running_job(fake_quotes, isolated_state):
    fake_quotes["delay"] = 0.3
    first, outcome = jobs.submit(scope="current", phases=["quotes"], days=7,
                                 entries_by_view={"v": "A,B,C,D"})
    assert outcome == "created"
    second, outcome2 = jobs.submit(scope="current", phases=["quotes"], days=7,
                                   entries_by_view={"v": "E"})
    # Not politeness: the rate limiters are process-global and the sentiment
    # cache take/restore races destructively between two concurrent jobs.
    assert outcome2 == "rejected"
    assert second is first
    _run_to_completion(first)


def test_concurrent_submits_cannot_both_win(fake_quotes, isolated_state):
    """The single-flight check must be atomic with the _CURRENT assignment.

    Reading current() before taking _REG_LOCK was a TOCTOU: two simultaneous
    POSTs (a double-click, or a second tab — the server is a
    ThreadingHTTPServer) both saw "nothing running" and both started, 10 times
    in 80 trials. _CURRENT then named only one of them, so the other was
    invisible to current(): the UI could never cancel it, and it kept spending
    the same process-global Finnhub/NIM budget.
    """
    fake_quotes["delay"] = 0.05
    for _ in range(40):
        jobs._JOBS.clear()
        jobs.__dict__["_CURRENT"] = None
        outcomes, start = [], threading.Barrier(2)

        def _submit(name):
            start.wait()
            outcomes.append(jobs.submit(scope="current", phases=["quotes"], days=7,
                                        entries_by_view={name: "A,B"}))

        threads = [threading.Thread(target=_submit, args=(f"v{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        created = [j for j, outcome in outcomes if outcome == "created"]
        assert len({j.id for j in created}) == 1, "two jobs started concurrently"
        for j, _o in outcomes:
            j.cancel.set()
        for j in created:
            _run_to_completion(j)


def test_supersede_cancels_the_running_job(fake_quotes, isolated_state):
    fake_quotes["delay"] = 0.5
    first, _ = jobs.submit(scope="current", phases=["quotes"], days=7,
                           entries_by_view={"v": "A,B,C,D,E,F"})
    second, outcome = jobs.submit(scope="current", phases=["quotes"], days=7,
                                  entries_by_view={"v": "G"},
                                  on_conflict="supersede")
    assert outcome == "created" and second is not first
    assert first.cancel.is_set()
    _run_to_completion(first)
    _run_to_completion(second)


def test_current_clears_once_the_job_finishes(fake_quotes, isolated_state):
    job, _ = jobs.submit(scope="current", phases=["quotes"], days=7,
                         entries_by_view={"v": "A,B"})
    assert _run_to_completion(job)
    assert jobs.current() is None


# ------------------------------ cancellation --------------------------------


def test_cancel_dequeues_pending_work(fake_quotes, isolated_state):
    """Cancel must actually stop work, not merely stop reporting it.

    With 5 workers and 40 slow symbols, a cancel shortly after start should
    leave the vast majority never fetched at all.
    """
    fake_quotes["delay"] = 0.2
    symbols = ",".join(f"S{i}" for i in range(40))
    job, _ = jobs.submit(scope="current", phases=["quotes"], days=7,
                         entries_by_view={"v": symbols})
    time.sleep(0.35)
    assert job.request_cancel() is True
    assert _run_to_completion(job)
    assert job.state == "cancelled"
    assert len(fake_quotes["calls"]) < 20, (
        f"cancel de-queued nothing: {len(fake_quotes['calls'])}/40 still fetched")


def test_cancel_frame_is_emitted_synchronously(fake_quotes, isolated_state):
    """The UI must reflect the cancel immediately, not whenever a worker
    happens to notice."""
    fake_quotes["delay"] = 0.3
    job, _ = jobs.submit(scope="current", phases=["quotes"], days=7,
                         entries_by_view={"v": "A,B,C,D,E,F,G,H"})
    time.sleep(0.1)
    before = job.snapshot()["last_seq"]
    job.request_cancel("user")
    frames, _ = job.events_since(before)
    cancelled = [f for f in frames if f["type"] == "cancelled"]
    assert cancelled and cancelled[0]["drained"] is False
    assert _run_to_completion(job)
    # ...and a later frame confirms the workers actually unwound.
    frames, _ = job.events_since(0)
    assert any(f["type"] == "job" and f.get("drained") is True for f in frames)


def test_a_cancelled_job_does_not_save(fake_quotes, isolated_state):
    fake_quotes["delay"] = 0.2
    job, _ = jobs.submit(scope="current", phases=["quotes"], days=7,
                         entries_by_view={"Tech": ",".join(f"S{i}" for i in range(30))})
    time.sleep(0.3)
    job.request_cancel()
    assert _run_to_completion(job)
    assert persistence.load_view("Tech") == {}


# ------------------------------ save guards ---------------------------------


def test_degraded_batch_is_not_saved(fake_quotes, isolated_state):
    """save_view overwrites rows wholesale. An unattended job must never
    replace good holdings with a batch Yahoo mostly failed to answer."""
    persistence.save_view("Tech", "A,B,C,D,E", [{"symbol": "A", "price": 1.0}])
    fake_quotes["error_symbols"] = {"A", "B", "C", "D"}     # 4/5 broken
    job, _ = jobs.submit(scope="current", phases=["quotes"], days=7,
                         entries_by_view={"Tech": "A,B,C,D,E"})
    assert _run_to_completion(job)
    frames, _ = job.events_since(0)
    end = [f for f in frames if f["type"] == "phase" and f.get("state") == "end"][0]
    assert end["saved"] is False and end["reason"] == "degraded"
    assert persistence.load_view("Tech")["rows"] == [{"symbol": "A", "price": 1.0}]


def test_healthy_batch_saves_without_touching_last_view(fake_quotes, isolated_state):
    persistence.save_view("Other", "Z", [{"symbol": "Z"}])   # sets last_view
    job, _ = jobs.submit(scope="current", phases=["quotes"], days=7,
                         entries_by_view={"Tech": "A,B,C"})
    assert _run_to_completion(job)
    assert len(persistence.load_view("Tech")["rows"]) == 3
    # set_last=False: a background job must never repoint the restore-on-launch
    # target at whatever portfolio it happened to touch.
    assert persistence.list_views()["last_view"] == "Other"


def test_views_run_sequentially(fake_quotes, isolated_state):
    """Concurrent portfolio work makes yfinance return empty frames, so the
    all-scope phase must do one portfolio at a time."""
    order = []
    fake_quotes["delay"] = 0.05
    job, _ = jobs.submit(scope="all", phases=["quotes"], days=7,
                         entries_by_view={"A": "AA,AB", "B": "BA,BB"})
    assert _run_to_completion(job)
    for f in job.events_since(0)[0]:
        if f["type"] == "phase" and f.get("state") == "start":
            order.append(f["view"])
        if f["type"] == "item":
            order.append(f["view"])
    # every A item lands before B's phase even starts
    assert order.index("B") > max(i for i, v in enumerate(order) if v == "A")


# ------------------------------ stream_quotes -------------------------------


def test_stream_quotes_emits_the_legacy_message_shape(fake_quotes):
    msgs = list(fetcher.stream_quotes(["a", "b"]))
    assert msgs[0] == {"type": "start", "total": 2, "symbols": ["A", "B"]}
    assert {m["type"] for m in msgs[1:-1]} == {"row"}
    assert msgs[-1] == {"type": "done", "total": 2}
    assert [m["done"] for m in msgs[1:-1]] == [1, 2]


def test_stream_quotes_cancel_discards_and_dequeues(fake_quotes):
    fake_quotes["delay"] = 0.15
    cancel = threading.Event()
    gen = fetcher.stream_quotes([f"s{i}" for i in range(40)], cancel=cancel)
    rows = 0
    for msg in gen:
        if msg["type"] == "row":
            rows += 1
            if rows == 2:
                cancel.set()
    assert rows < 10
    assert len(fake_quotes["calls"]) < 25


def test_closing_the_generator_dequeues(fake_quotes):
    """The disconnect path: gen.close() raises GeneratorExit, whose finally
    de-queues. The old inline handler's `return` left a `with` block that
    JOINED every future, so an abandoned build kept hitting Yahoo for ~30 s."""
    fake_quotes["delay"] = 0.15
    gen = fetcher.stream_quotes([f"s{i}" for i in range(40)])
    next(gen)              # start
    next(gen)              # first row
    gen.close()
    time.sleep(0.4)
    assert len(fake_quotes["calls"]) < 25
