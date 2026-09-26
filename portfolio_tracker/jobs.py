"""In-process registry for background refresh jobs.

Why this exists: refreshing quotes and news used to be two disconnected,
blocking buttons. Nothing survived a tab switch, nothing was cancellable, and
there was no way to refresh the whole account. A refresh legitimately outlives
a single HTTP request, so the work moves onto a daemon thread and the client
attaches to a replayable NDJSON event stream.

Design notes that are load-bearing:

* **Single-flight.** ``submit`` rejects a second concurrent job unless the
  caller explicitly asks to supersede (the long-press "all portfolios" path,
  behind its own confirmation). This is a correctness requirement, not
  politeness: the Finnhub/NIM limiters are process-global, so two jobs don't go
  twice as fast — they spend each other's budget and both stall. Worse,
  ``news_sentiment._refresh_symbol_sentiment`` takes the cache entry out, then
  restores it in a ``finally`` if the refresh failed; with two jobs on the same
  ``sym|days`` key, A takes it, B takes nothing, A fails and restores the STALE
  value on top of B's fresh one. Single-flight is also what makes the single
  global ``helpers._RATE_OBSERVER`` unambiguous.

* **A dead client is not a cancel.** The stream handler returns on BrokenPipe
  and never touches ``job.cancel``; the job keeps running and the client
  reattaches at its last seq. (The old standalone news-refresh route did the
  opposite: it silenced writes while the work — and the API quota — carried on
  with nobody watching. It was deleted in v1.12.0.)

* **Cancel actually stops work.** Queued items are de-queued via
  ``pool.shutdown(cancel_futures=True)``; in-flight ones check a token at every
  coarse checkpoint, including the two sleep gates (the rolling rate limiter
  and the 65 s circuit breaker) that would otherwise make "cancelled" a lie for
  a full minute. The honest bound: the ``cancelled`` frame is emitted
  synchronously by the cancel route, no NEW upstream call is issued after that
  instant, and already-open sockets detach within ~30 s worst case
  (``_NV_TIMEOUT_S``).

* **Totals are planned up front, then reconciled.** ``_run`` seeds
  ``quotes_total`` and ``news_total`` from the snapshotted entries strings
  BEFORE the first item runs, and each phase corrects its own total to the real
  number once it knows it. Without this the client's denominator only existed
  for the phase in flight, so the progress bar filled to 100% during quotes and
  then rewound when news started (and again per portfolio on an all-scope run).
  The estimate is allowed to be slightly wrong; what matters is that it is
  never zero and never grows by a whole phase mid-run.

* Worker threads are daemons — ``server.shutdown_server`` ends in
  ``os._exit(0)``, so nothing here may ever be able to block process exit.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque

from portfolio_tracker import helpers, persistence
from portfolio_tracker.fetcher import stream_quotes

try:
    from portfolio_tracker import news_sentiment as _ns
except Exception:  # pragma: no cover - optional at runtime, like everywhere else
    _ns = None

# A 150-symbol all-scope job emits roughly 1100 frames; the ring is generous so
# `dropped` is effectively unreachable for realistic portfolios. Memory is
# dominated by `row` frames (a fetch_one dict carries a 252-point sparkline,
# ~6 KB), so ~1 MB per large job and <= 4 MB resident with the retention below.
_EVENT_MAXLEN = 6000
_RETAIN_S = 900          # a finished job stays queryable for 15 minutes
_MAX_FINISHED = 3

# Refuse to save a portfolio whose rows came back mostly broken. Yahoo answers
# an overloaded batch with error rows rather than an exception, and save_view
# overwrites `rows` wholesale — so an unattended background job could quietly
# replace good data with "no data" while the user was on another tab.
_MAX_ERROR_FRACTION = 0.20

_JOBS: "OrderedDict[str, Job]" = OrderedDict()
_REG_LOCK = threading.Lock()
_CURRENT: "Job | None" = None


def _parse_entries(entries: str) -> list[str]:
    """Split a stored entries string into individual constituent entries.

    Shared by the up-front planner and the quotes phase itself. They MUST agree:
    if the planner counted differently from what actually gets fetched, the
    progress denominator would visibly correct itself on the first item — which
    is the exact behaviour the planning exists to remove.

    Literal repeats are dropped (order kept) — an entries string pasted from two
    overlapping lists names some tickers twice, and stream_quotes would fetch
    each only once anyway. Only exact repeats: "abc" and "ABC" can resolve to
    different tickers, so collapsing those is left to resolution.
    """
    parts = (e.strip() for e in (entries or "").replace("\n", ",").split(","))
    return list(dict.fromkeys(e for e in parts if e))


class Job:
    """One refresh run. All mutable state is guarded by ``_cv``."""

    def __init__(self, *, scope, phases, views, entries, days, context):
        self.id = "rj_" + uuid.uuid4().hex[:12]
        self.scope = scope                 # "current" | "all"
        self.phases = tuple(phases)        # ("quotes",) | ("quotes", "news")
        self.views = list(views)           # portfolio names, in run order
        self.entries = dict(entries)       # view -> entries string, snapshotted
        self.days = days
        self.context = context
        self.created_at = time.time()
        self.started_at = None
        self.ended_at = None
        self.state = "queued"
        self.error = None
        # news_scored / news_failed split news_done by outcome: the chip says
        # "12/15 scored", and a refresh where the News read failed for every
        # ticker must never read as a plain "done".
        self.counts = {"quotes_done": 0, "quotes_total": 0,
                       "news_done": 0, "news_total": 0,
                       "news_scored": 0, "news_failed": 0, "rate_waits": 0}
        # Per-view symbol counts: the planner's estimate, and the real number
        # each view reports at its own phase start. quotes_total is always
        # actual-where-known + planned-for-the-rest, so it stays a whole-job
        # denominator from the first frame instead of growing per portfolio.
        self._planned_quotes: dict[str, int] = {}
        self._actual_quotes: dict[str, int] = {}
        self.phase = None
        self.cancel = threading.Event()
        self.cancel_reason = None
        self._cv = threading.Condition()
        self._events: deque = deque(maxlen=_EVENT_MAXLEN)
        self._seq = 0
        self._first_seq = 1                # oldest seq still in the ring
        self._thread = None

    # ----------------------------- events -----------------------------------

    # Frames that carry the job's cumulative counts. The client must never
    # derive totals itself: a per-view `phase` total is per-view, and adding it
    # to a count the server already bumped double-counts (the status chip read
    # "Quotes 4/8" for a 4-symbol portfolio). One authoritative source, assigned
    # not accumulated.
    _COUNTED = ("job", "phase", "item", "cancelled", "done", "error")

    def emit(self, kind: str, **body) -> dict:
        with self._cv:
            self._seq += 1
            frame = {"type": kind, "seq": self._seq, "t": round(time.time(), 3), **body}
            if kind in self._COUNTED and "counts" not in frame:
                frame["counts"] = dict(self.counts)
            if len(self._events) == _EVENT_MAXLEN:
                self._first_seq = self._events[0]["seq"] + 1
            self._events.append(frame)
            self._cv.notify_all()
        return frame

    def events_since(self, since: int) -> tuple[list[dict], bool]:
        """Retained frames newer than `since`, plus whether anything was lost.

        `dropped` means `since` predates the ring, so the replay would have a
        hole — the client must cold-re-read instead of trusting it. Same
        contract, and the same word, as logbuf.read().
        """
        with self._cv:
            dropped = since > 0 and since + 1 < self._first_seq
            return [e for e in self._events if e["seq"] > since], dropped

    def wait(self, since: int, timeout: float) -> list[dict]:
        """Block until a frame newer than `since` exists, or timeout."""
        with self._cv:
            if not any(e["seq"] > since for e in self._events):
                self._cv.wait(timeout)
            return [e for e in self._events if e["seq"] > since]

    # ----------------------------- state ------------------------------------

    def snapshot(self) -> dict:
        with self._cv:
            return {
                "id": self.id, "scope": self.scope, "state": self.state,
                "phases": list(self.phases), "phase": self.phase,
                "views": list(self.views), "days": self.days,
                "counts": dict(self.counts), "last_seq": self._seq,
                "error": self.error,
                "elapsed_s": round((self.ended_at or time.time()) - self.created_at, 2),
            }

    def is_terminal(self) -> bool:
        return self.state in ("done", "cancelled", "error")

    def bump(self, key: str, n: int = 1) -> int:
        with self._cv:
            self.counts[key] = self.counts.get(key, 0) + n
            return self.counts[key]

    def set_count(self, key: str, n: int) -> int:
        """Assign a count outright. Used for the two *_total keys, which are
        planned up front and then corrected — accumulating them instead is what
        made the denominator grow by a whole phase mid-run."""
        with self._cv:
            self.counts[key] = n
            return n

    def request_cancel(self, reason: str = "user") -> bool:
        if self.is_terminal():
            return False
        self.cancel_reason = reason
        self.cancel.set()
        # Emitted synchronously so the UI reflects the cancel in <100 ms rather
        # than whenever a worker next notices. `drained: false` says the
        # in-flight calls have not necessarily unwound yet — a later `job`
        # frame with drained:true marks that.
        self.emit("cancelled", reason=reason, at_phase=self.phase,
                  drained=False, counts=dict(self.counts))
        return True


# ----------------------------- registry -------------------------------------


def _reap_locked() -> None:
    now = time.time()
    finished = [j for j in _JOBS.values() if j.is_terminal()]
    for j in finished:
        if j.ended_at and now - j.ended_at > _RETAIN_S:
            _JOBS.pop(j.id, None)
    finished = [j for j in _JOBS.values() if j.is_terminal()]
    for j in finished[:-_MAX_FINISHED] if len(finished) > _MAX_FINISHED else []:
        _JOBS.pop(j.id, None)


def get(job_id: str) -> "Job | None":
    with _REG_LOCK:
        _reap_locked()
        return _JOBS.get(job_id)


def current() -> "Job | None":
    with _REG_LOCK:
        j = _CURRENT
    return j if (j is not None and not j.is_terminal()) else None


def cancel(job_id: str, reason: str = "user") -> bool:
    j = get(job_id)
    return bool(j and j.request_cancel(reason))


def submit(*, scope, phases, days, entries_by_view, context=None,
           on_conflict="reject") -> tuple["Job | None", str]:
    """Create and start a job. Returns (job, outcome).

    outcome is "created", or "rejected" with the running job returned instead
    (see the single-flight rationale in the module docstring).
    """
    job = Job(scope=scope, phases=phases, views=list(entries_by_view.keys()),
              entries=entries_by_view, days=days, context=context)
    # Before ANY frame is emitted — including the `queued` one below and the
    # snapshot a late-attaching client reads — so the progress bar has a real
    # denominator from the very first thing it ever sees.
    _plan_totals(job)
    # The conflict check and the _CURRENT assignment MUST be one atomic step.
    # Reading current() outside the lock is a TOCTOU: the server is a
    # ThreadingHTTPServer, so two near-simultaneous POSTs (a double-click, or a
    # second tab) both saw "nothing running" and both started — 10 times in 80
    # trials. _CURRENT then names only one of them, so the other is invisible
    # to current(): the UI can never cancel it, _on_rate_event bills its waits
    # to the wrong job, and both spend the same global Finnhub/NIM budget.
    with _REG_LOCK:
        running = _CURRENT
        if running is not None and running.is_terminal():
            running = None
        if running is not None:
            if on_conflict != "supersede":
                return running, "rejected"
            superseded = running
        else:
            superseded = None
        _reap_locked()
        _JOBS[job.id] = job
        globals()["_CURRENT"] = job
    # Outside the lock: request_cancel emits a frame, and the module's stated
    # lock order is registry -> job condvar, never held while emitting.
    if superseded is not None:
        superseded.request_cancel("superseded")
    job.emit("job", state="queued", scope=scope, views=job.views,
             days=days, phases=list(job.phases))
    job._thread = threading.Thread(target=_run, args=(job,),
                                   name=f"pt-refresh-{job.id}", daemon=True)
    job._thread.start()
    return job, "created"


def shutdown(grace_s: float = 1.5) -> None:
    """Cancel everything and give in-flight view writes a moment to land.

    Called from server.shutdown_server BEFORE its os._exit(0) — persistence
    writes are atomic (persistence._atomic_write), but a job mid-write still
    deserves the chance to finish rather than leaving an orphan temp file.
    """
    with _REG_LOCK:
        jobs = list(_JOBS.values())
    for j in jobs:
        if not j.is_terminal():
            j.cancel.set()
    deadline = time.time() + grace_s
    for j in jobs:
        if j._thread is not None:
            j._thread.join(max(0.0, deadline - time.time()))


# ----------------------------- rate observer --------------------------------


def _on_rate_event(event: dict) -> None:
    """helpers._notify_rate sink — turns backoff into progress frames.

    Correct only because of single-flight: there is at most one job to route
    to. See helpers.set_rate_observer.
    """
    job = current()
    if job is None:
        return
    if event.get("reason") == "cleared":
        job.emit("rate_cleared", **event)
        return
    job.bump("rate_waits")
    job.emit("rate_limited", phase=job.phase, **event)


helpers.set_rate_observer(_on_rate_event)


# ----------------------------- driver ---------------------------------------


def _plan_totals(job: Job) -> None:
    """Seed both denominators from the snapshotted entries, before any work.

    Everything needed is already known at this point — the entries strings for
    every view in the run were snapshotted at submit time — so there is no
    reason to make the client watch the total appear one phase at a time. Each
    phase corrects its own figure below once it has the authoritative count.
    """
    job._planned_quotes = {v: len(_parse_entries(job.entries.get(v) or "")) for v in job.views}
    job.set_count("quotes_total", sum(job._planned_quotes.values()))
    if "news" in job.phases:
        # The news phase runs over the union of RESOLVED symbols, which we don't
        # have yet; unique entries is the closest thing available and is exact
        # whenever the user typed tickers. +1 for the market-wide read. Corrected
        # in _run_news_phase, so an estimate that is off costs one small nudge.
        planned_news = {e.upper() for v in job.views
                        for e in _parse_entries(job.entries.get(v) or "")}
        job.set_count("news_total", len(planned_news) + 1)


def _run(job: Job) -> None:
    job.started_at = time.time()
    job.state = "running"
    job.emit("job", state="running")
    try:
        rows_by_symbol: dict[str, dict] = {}
        for i, view in enumerate(job.views):
            if job.cancel.is_set():
                break
            _run_quotes_phase(job, view, i, rows_by_symbol)
            if len(job.views) > 1 and i < len(job.views) - 1:
                # Portfolios run ONE AT A TIME with a gap. warmRecentTabs
                # established empirically that concurrent portfolio work makes
                # yfinance start returning empty close frames — i.e. exactly the
                # degraded rows the save guard would then have to throw away.
                time.sleep(0.5)
        if "news" in job.phases and not job.cancel.is_set():
            _run_news_phase(job, rows_by_symbol)
        elif "news" in job.phases:
            job.set_count("news_total", 0)
            job.emit("phase", phase="news", state="skipped", reason="cancelled")
    except helpers.Cancelled:
        # Control flow, not failure. The sleep gates (rolling rate limiter, the
        # 65 s circuit breaker) raise this to unwind promptly instead of parking
        # a worker for a minute after the user hit cancel — a bare
        # `except Exception` here reported it as "Refresh failed".
        _finish(job, "cancelled")
        return
    except Exception as exc:                       # pragma: no cover - defensive
        job.error = f"{type(exc).__name__}: {exc}"
        _finish(job, "error")
        return
    _finish(job, "cancelled" if job.cancel.is_set() else "done")


def _finish(job: Job, state: str) -> None:
    job.state = state
    job.ended_at = time.time()
    job.phase = None
    if state == "error":
        job.emit("error", error=job.error, counts=dict(job.counts))
    elif state == "cancelled":
        job.emit("job", state="cancelled", drained=True, counts=dict(job.counts))
    else:
        job.emit("done", counts=dict(job.counts), views=job.views,
                 elapsed_s=round(job.ended_at - job.created_at, 2))
    with _REG_LOCK:
        if _CURRENT is job:
            globals()["_CURRENT"] = None


def _run_quotes_phase(job: Job, view: str, index: int, rows_by_symbol: dict) -> None:
    job.phase = "quotes"
    entries = job.entries.get(view) or ""
    parts = _parse_entries(entries)
    rows: list[dict] = []
    started = False
    for msg in stream_quotes(parts, cancel=job.cancel):
        if msg["type"] == "start":
            started = True
            # Reconcile rather than accumulate: swap this view's estimate for its
            # real count and re-derive the whole-job total, so the denominator
            # stays the whole job's and only ever nudges by the estimate's error.
            job._actual_quotes[view] = msg["total"]
            job.set_count("quotes_total", sum(job._actual_quotes.values()) + sum(
                n for v, n in job._planned_quotes.items() if v not in job._actual_quotes))
            job.emit("phase", phase="quotes", state="start", view=view,
                     view_index=index, view_count=len(job.views),
                     total=msg["total"], symbols=msg["symbols"])
        elif msg["type"] == "row":
            row = msg["row"]
            rows.append(row)
            if row.get("symbol"):
                rows_by_symbol.setdefault(row["symbol"], row)
            job.bump("quotes_done")
            job.emit("item", phase="quotes", view=view, symbol=row.get("symbol"),
                     state="error" if row.get("error") else "ok",
                     done=msg["done"], total=msg["total"], row=row)
    if not started:
        # Nothing was fetched for this view (empty entries, or cancelled before
        # the first message). Drop its reservation or the denominator keeps
        # counting symbols that will never arrive and the bar can't reach 100%.
        job._actual_quotes[view] = 0
        job.set_count("quotes_total", sum(job._actual_quotes.values()) + sum(
            n for v, n in job._planned_quotes.items() if v not in job._actual_quotes))
        return

    failed = sum(1 for r in rows if r.get("error"))
    ok = len(rows) - failed
    saved = False
    reason = None
    if job.cancel.is_set():
        reason = "cancelled"
    elif not rows:
        reason = "no rows"
    elif failed / max(1, len(rows)) > _MAX_ERROR_FRACTION:
        # See _MAX_ERROR_FRACTION: never let an unattended job overwrite good
        # holdings with a batch Yahoo mostly failed to answer.
        reason = "degraded"
    else:
        # set_last=False: a background job must never repoint the
        # restore-on-launch target at whatever portfolio it happened to touch.
        payload = persistence.save_view(view, entries, rows, set_last=False)
        saved = True
        job.emit("view_saved", view=view, rows=len(rows),
                 saved_at=payload.get("saved_at"))
    job.emit("phase", phase="quotes", state="end", view=view, saved=saved,
             ok=ok, failed=failed, reason=reason)


def _run_news_phase(job: Job, rows_by_symbol: dict) -> None:
    job.phase = "news"
    # Every early return releases the planned reservation (_plan_totals seeded
    # it before we knew whether news would run at all): a phase that is skipped
    # must not leave items in the denominator that nothing will ever complete.
    if _ns is None:
        job.set_count("news_total", 0)
        job.emit("phase", phase="news", state="skipped",
                 reason="news_sentiment unavailable")
        return
    symbols = sorted(rows_by_symbol.keys())
    if not symbols:
        job.set_count("news_total", 0)
        job.emit("phase", phase="news", state="skipped", reason="no symbols")
        return
    context = job.context or _build_news_context(job, rows_by_symbol)
    # Assign, don't bump: this is the authoritative count replacing the plan's
    # estimate (+1 for the market-wide read).
    job.set_count("news_total", len(symbols) + 1)
    job.emit("phase", phase="news", state="start", view=None, market=True,
             days=job.days, total=len(symbols) + 1, symbols=symbols)

    def _progress(kind: str, body: dict) -> None:
        # Translate news_sentiment's vocabulary into the job's. `stage` values
        # (start|fetch|pass1|pass2|market|aggregate) pass through untouched to
        # NS_PROG_STAGE in app.js. An item's `state` is the News read outcome:
        # ok (fresh read), failed (the LLM produced nothing — the previous read
        # is kept, marked stale), empty (no news), cancelled.
        if kind == "plan":
            return
        if kind == "market_stage":
            job.emit("item_stage", phase="news", symbol="__market__",
                     stage=body.get("stage"), frac=body.get("frac"))
        elif kind == "symbol_stage":
            job.emit("item_stage", phase="news", symbol=body.get("symbol"),
                     stage=body.get("stage"), frac=body.get("frac"))
        elif kind in ("market", "symbol"):
            outcome = body.get("outcome") or ("ok" if body.get("sentiment") else "empty")
            if kind == "symbol" and outcome == "ok":
                job.bump("news_scored")
            elif kind == "symbol" and outcome == "failed":
                job.bump("news_failed")
            done = job.bump("news_done")
            job.emit("item", phase="news",
                     symbol="__market__" if kind == "market" else body.get("symbol"),
                     state=outcome, done=done, total=len(symbols) + 1,
                     sentiment=body.get("sentiment"))

    result = _ns.refresh_sentiment(symbols, context=context, progress_cb=_progress,
                                   cancel=job.cancel)
    st = result.get("status") or {}
    job.emit("phase", phase="news", state="end",
             scored=st.get("scored"), failed=st.get("failed"), empty=st.get("empty"),
             total=len(symbols), llm_error=st.get("llm_error") if st.get("failed") else None,
             reason="cancelled" if result.get("cancelled") else None)


def _build_news_context(job: Job, rows_by_symbol: dict) -> dict:
    """The same {weights, betas, rows, lookback_days} shape the client's
    nsRefreshContext builds — assembled server-side, because an all-portfolios
    job has no single loaded DATA to derive it from.

    Weights come from each view's active preset, else cap weight, else equal;
    merged across views by MAX so the staged ordering still puts the largest
    position anywhere in the account first. Betas ride on the freshly-fetched
    rows already (fetcher populates `beta`), so no analytics run is needed.
    """
    betas, rows, weights = {}, {}, {}
    for sym, r in rows_by_symbol.items():
        if r.get("beta") is not None:
            betas[sym] = r["beta"]
        rows[sym] = {"name": r.get("name"), "price": r.get("price"),
                     "pct_1d": r.get("pct_1d"), "pct_1w": r.get("pct_1w"),
                     "pct_ytd": r.get("pct_ytd"), "delta_ath": r.get("delta_ath")}
    for view in job.views:
        entry = persistence.load_view(view) or {}
        syms = [s for s in (r.get("symbol") for r in (entry.get("rows") or [])) if s]
        if not syms:
            continue
        w = _view_weights(entry, syms, rows_by_symbol)
        for s, v in w.items():
            if v > weights.get(s, 0.0):
                weights[s] = v
    return {"weights": weights, "betas": betas, "rows": rows,
            "lookback_days": job.days}


def _view_weights(entry: dict, syms: list[str], rows_by_symbol: dict) -> dict:
    active = entry.get("active_weight_preset")
    if active:
        for p in (entry.get("weight_presets") or []):
            if p.get("name") == active and isinstance(p.get("weights"), dict):
                return {s: float(p["weights"].get(s) or 0.0) for s in syms}
    caps = {s: (rows_by_symbol.get(s) or {}).get("market_cap") for s in syms}
    total = sum(c for c in caps.values() if c)
    if total:
        return {s: (caps[s] or 0.0) / total for s in syms}
    return {s: 1.0 / len(syms) for s in syms}
