"""In-memory ring buffer of everything the backend prints.

Why this exists: the whole backend logs via bare `print()` — 30+ call sites
across news_sentiment, ml_sentiment, fetcher and server. In browser mode those
land in the terminal that launched `convexity`. In DESKTOP mode they land
nowhere at all: the `.app` bundle has no terminal, and desktop.py's
`_setup_logging()` redirects the `logging` module, not `sys.stdout`. So when the
ML model failed to load in the shipped app, the one line that said so was
written to a file descriptor no human would ever read.

Rather than rewrite every call site, this module tees `sys.stdout`/`sys.stderr`
into a bounded deque. Every existing `print()` is captured for free, and the
Settings -> Logs console reads the ring through `GET /api/logs`.

Design notes:
  * Bounded (`_MAXLEN`) — this runs for the whole process lifetime and must
    never grow without limit.
  * Monotonic `seq` per line, never reset. The client polls with the last seq
    it saw; if that seq has already fallen out of the ring the response says
    so, so the UI can show a "lines dropped" marker instead of silently
    skipping output.
  * `install()` is idempotent and re-entrant-safe. Double-installing would
    double-record every line (a tee wrapping a tee).
  * Writes still go to the real stream, so the terminal / launcher log file
    behave exactly as before. This is additive.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import deque
from typing import Any

# ~4000 lines is a few hundred KB and covers a full news refresh plus a build
# with room to spare.
_MAXLEN = 4000

_LOCK = threading.Lock()
_BUF: deque[dict] = deque(maxlen=_MAXLEN)
_SEQ = 0
_INSTALLED = False


def record(text: str, stream: str = "stdout") -> None:
    """Append one line. Blank lines are dropped (print() emits a bare '\\n' as
    a separate write, which would otherwise double every entry)."""
    global _SEQ
    text = text.rstrip("\n")
    if not text.strip():
        return
    with _LOCK:
        _SEQ += 1
        _BUF.append({
            "seq": _SEQ,
            "ts": time.time(),
            "stream": stream,
            "text": text[:4000],   # one pathological line can't eat the ring
            "http": _is_http_line(text),
        })


def _is_http_line(text: str) -> bool:
    """Server request log lines (server.py's log_message override) are tagged
    so the UI can hide them by default — one per request drowns everything
    else in the console."""
    return ' "GET ' in text or ' "POST ' in text or ' "DELETE ' in text


class _Tee:
    """Minimal write-through file wrapper. Deliberately not a full TextIO: it
    forwards the handful of attributes real code touches and delegates the rest
    to the wrapped stream, so anything expecting a file (tracebacks, warnings,
    yfinance's own logging) keeps working."""

    def __init__(self, wrapped: Any, name: str) -> None:
        self._wrapped = wrapped
        self._name = name
        self._partial = ""

    def write(self, s: str) -> int:
        n = self._wrapped.write(s)
        try:
            # print() issues the text and the trailing newline as separate
            # writes, and long output can arrive in fragments — buffer until a
            # newline so the ring holds whole lines.
            self._partial += s
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                record(line, self._name)
            if len(self._partial) > 8000:      # never buffer unboundedly
                record(self._partial, self._name)
                self._partial = ""
        except Exception:
            pass          # logging must never break the thing being logged
        return n

    def flush(self) -> None:
        self._wrapped.flush()

    def isatty(self) -> bool:
        try:
            return self._wrapped.isatty()
        except Exception:
            return False

    def __getattr__(self, item: str) -> Any:
        return getattr(self._wrapped, item)


def install() -> None:
    """Tee stdout/stderr into the ring. Safe to call more than once."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        _INSTALLED = True
    sys.stdout = _Tee(sys.stdout, "stdout")
    sys.stderr = _Tee(sys.stderr, "stderr")


def read(since: int = 0, limit: int = 1000) -> dict:
    """Lines with seq > `since`, oldest first.

    `dropped` is True when `since` predates the oldest line still in the ring,
    i.e. output was produced and evicted between polls.
    """
    with _LOCK:
        oldest = _BUF[0]["seq"] if _BUF else 0
        lines = [ln for ln in _BUF if ln["seq"] > since]
        last_seq = _BUF[-1]["seq"] if _BUF else since
    dropped = bool(since and oldest and since + 1 < oldest)
    if limit and len(lines) > limit:
        lines = lines[-limit:]
        dropped = True
    return {"lines": lines, "last_seq": last_seq, "dropped": dropped,
            "capacity": _MAXLEN}


def reset_for_tests() -> None:
    global _SEQ
    with _LOCK:
        _BUF.clear()
        _SEQ = 0
