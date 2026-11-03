"""Reads the news for the held shares by itself once their reads have gone old
(the owner's call, 2026-11-03: the book is for someone who doesn't look every
day, and My Investments' Refresh only updates prices).

A hidden background job, like ``long_run``'s: it waits until nothing else is
running, and a refresh the owner asks for supersedes it. It spends the owner's
NVIDIA and Finnhub quota, so it is deliberately slow and small: one share at a
time, only those whose read is over ``STALE_S`` old, funds left out (they have
no company news) and no market-wide read (nothing on the book's page shows it).
No keys, no pass. ``state/news_watch.json`` keeps when each share was last tried:
a share with no news has no read to date, and without this it would be asked
about on every launch. A failed read tries again in ``RETRY_S``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

from convexity import paths
from convexity.persistence import _atomic_write

STALE_S = 3 * 86400  # a read this old is worth replacing
RETRY_S = 6 * 3600  # after a failed read
FIRST_TICK_S = 300  # after launch: the page, its warm-ups and the owner go first
TICK_S = 300

_STATE: dict = {"thread": None, "stop": threading.Event()}


def _path():
    return paths.state_file("news_watch")


def _tried() -> dict:
    """{symbol: when it was last tried}; a damaged file only means asking again."""
    try:
        body = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(body, dict):
        return {}
    return {k: v for k, v in body.items() if isinstance(v, int | float)}


def _mark(symbol: str, when: float) -> None:
    _atomic_write(_path(), json.dumps({**_tried(), symbol: when}, separators=(",", ":")) + "\n")


def _read_at(read: dict | None) -> float:
    """When a cached read was made, 0 if it has no date."""
    stamp = ((read or {}).get("news") or {}).get("assessed_at") or (read or {}).get("assessed_at")
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _keys_set() -> bool:
    from convexity import news_sentiment as ns

    st = ns.status()
    return bool(st.get("finnhub_key_set") and st.get("nvidia_key_set"))


def due(now: float | None = None) -> list[str]:
    """The held shares whose read is old and that weren't tried lately."""
    from convexity import investments
    from convexity import news_sentiment as ns

    now = time.time() if now is None else now
    if not _keys_set():
        return []
    tried = _tried()
    out = []
    for s in investments.held_symbols():
        if investments._pack_kind(s)["fund"]:
            continue
        if now - max(_read_at(ns.get_cached_sentiment(s)), tried.get(s, 0.0)) >= STALE_S:
            out.append(s)
    return out


def _context(job, symbols: list[str]) -> dict:
    """What a refresh would send: the saved rows' figures and betas for the brief
    and the Track record, the book's weights so the biggest holdings go first."""
    from convexity import investments, jobs, persistence

    saved = {r.get("symbol"): r for r in (persistence.load_view("__book__") or {}).get("rows") or []
             if isinstance(r, dict)}  # fmt: skip
    ctx = jobs._build_news_context(job, {s: saved.get(s) or {} for s in symbols})
    page = investments.computed_page() or {}
    return {
        **ctx,
        "weights": {p["symbol"]: p.get("weight") or 0.0 for p in page.get("positions") or []},
    }


def run_pass(job) -> str:
    """One background pass over what is due. Called only from the job."""
    from convexity import investments
    from convexity import news_sentiment as ns

    symbols = due()
    if not symbols:
        return "nothing"

    def progress(kind: str, body: dict) -> None:
        # Kept as each one lands: a superseded pass doesn't ask again for what it read.
        if kind != "symbol" or body.get("outcome") == "cancelled":
            return
        now = time.time()
        _mark(body["symbol"], now - STALE_S + RETRY_S if body.get("outcome") == "failed" else now)

    try:
        result = ns.refresh_sentiment(symbols, context=_context(job, symbols), progress_cb=progress,
                                      cancel=job.cancel, refresh_market=False, workers=1)  # fmt: skip
    finally:
        investments.forget_page()  # the book's page carries these reads
    st = result.get("status") or {}
    print(
        f"[news_watch] read {st.get('scored')}, failed {st.get('failed')}, no news {st.get('empty')}"
    )
    return "cancelled" if result.get("cancelled") else "done"


# ----------------------------------------------------------------- scheduler


def tick(now: float | None = None, *, submit=None):
    """Start a pass if something is due and nothing else is running."""
    from convexity import jobs

    running = jobs.current()
    if running is not None and not running.is_terminal():
        return None
    if not due(now):
        return None
    return (submit or _submit)()


def _submit():
    from convexity import jobs

    return jobs.submit(scope="current", phases=["newsread"], days=7,
                       entries_by_view={jobs.BOOK_VIEW: ""}, context={"background": True})  # fmt: skip


def _loop() -> None:
    stop = _STATE["stop"]
    if stop.wait(FIRST_TICK_S):
        return
    while True:
        try:
            tick()
        except Exception as exc:  # the scheduler outlives any one bad tick
            print(f"[news_watch] tick failed: {type(exc).__name__}: {exc}")
        if stop.wait(TICK_S):
            return


def start() -> None:
    """Start the scheduler thread once (server.start_server, both modes)."""
    if os.environ.get("CONVEXITY_LONGRUN") == "0":  # the test suite's servers
        return
    t = _STATE["thread"]
    if t is not None and t.is_alive():
        return
    _STATE["stop"].clear()
    _STATE["thread"] = threading.Thread(target=_loop, name="news-watch", daemon=True)
    _STATE["thread"].start()


def stop() -> None:
    _STATE["stop"].set()
