# Background jobs and the log console

[← CLAUDE.md](../../CLAUDE.md) · part of the engineering notes; `§N` references name the original CLAUDE.md sections (map in CLAUDE.md).

Sections: §16 (`logbuf.py`), §17 (`jobs.py`).

## 16. `logbuf.py` — the backend console (Settings → Logs)

The entire backend logs via bare `print()` (30+ call sites in news_sentiment,
ml_sentiment, server). In browser mode those land in the launching terminal. In
**desktop mode they land nowhere**: the `.app` has no terminal, and
`desktop.py`'s `_setup_logging()` redirects the `logging` module, not
`sys.stdout`. That is how the ML model stayed dead for weeks — the one line
explaining why was written to a file descriptor no human could read (§4).

`logbuf.install()` (called from `server.start_server()`, idempotent) wraps
`sys.stdout`/`sys.stderr` in a write-through tee that also appends whole lines
to a `deque(maxlen=4000)` of `{seq, ts, stream, text, http}`. Every existing
`print()` is captured with **zero edits to the call sites** — do not "clean this
up" by converting them to `logging` without keeping the tee, or desktop-mode
output goes dark again.

- `seq` is monotonic and never reset. `read(since, limit)` returns only newer
  lines plus `dropped`, so the UI can show a "lines dropped" marker instead of
  silently skipping output.
- `http` tags `server.py`'s per-request log lines. The frontend hides them by
  default — one per request drowns everything else.
- Writes still reach the real stream, so the terminal and the launcher log file
  behave exactly as before. This is purely additive.
- The frontend polls `/api/logs?since=` every 1.5s **only while the Logs pane is
  open** (`stopLogPolling` clears the timer in `closeSettings`). Autoscroll
  pauses itself when the user scrolls up and resumes at the bottom.

## 17. `jobs.py` — background refresh jobs (v1.11.0)

Refreshing used to be two disconnected, blocking buttons (`#refresh` for
quotes, `#ns-refresh` for news). Nothing survived a tab switch, nothing was
cancellable, and there was no way to refresh the whole account. Now there is
**one Refresh control** in the topbar:

| Gesture | Scope | Blocking |
|---|---|---|
| Click, or `R` | Current portfolio: quotes → then News read + Market read | No |
| Long-press ≥600 ms → confirm, or `Shift+R` | **Every** saved portfolio | No |

There is no separate news refresh button or route — news is a phase of the job.

### Four rules that are load-bearing

1. **Single-flight.** `submit()` rejects a second concurrent job with `409` and
   the client attaches to the running one; only the long-press path (behind its
   confirm dialog) may `on_conflict: "supersede"`. This is correctness, not
   politeness: the Finnhub/NIM limiters are process-global, so two jobs spend
   each other's budget, and `_refresh_symbol_sentiment`'s `_cache_take` /
   restore-on-failure pair races destructively — A takes the entry, B takes
   nothing, A fails and restores the STALE value over B's fresh one.
   Single-flight is also what makes the single global
   `helpers._RATE_OBSERVER` unambiguous; **relaxing it requires a context-local
   binding propagated into every pool worker.**
   The conflict check and the `_CURRENT` assignment are **one atomic step under
   `_REG_LOCK`** — reading `current()` outside it was a TOCTOU that let two
   simultaneous POSTs both start (10 of 80 trials on a `ThreadingHTTPServer`,
   which a double-click or a second tab reaches). `_CURRENT` then named only
   one, so the other was invisible to `current()`: uncancellable from the UI,
   and billing its rate waits to the wrong job. `request_cancel` on a
   superseded job is called **after** the lock is released — the stated lock
   order is registry → job condvar, never held while emitting.
2. **A dead client is not a cancel.** The stream handler returns on BrokenPipe
   and never touches `job.cancel`. Only the chip's × cancels. (The old news
   route did the opposite: it silenced writes while the work — and the API
   quota — carried on unattended.)
3. **Cancel actually stops work.** Queued items are de-queued by
   `pool.shutdown(wait=False, cancel_futures=True)` — **never** use a `with`
   block for these pools, `__exit__` joins everything. In-flight work checks a
   duck-typed `cancel` token (`.is_set()`, so `news_sentiment` never imports
   `jobs`) at five points, including the two sleep gates —
   `helpers._RateLimiter.acquire` and `_wait_for_circuit_breaker`, which can
   hold for 65 s and would otherwise make "cancelled" a lie for a full minute.
   `helpers.Cancelled` (aliased `news_sentiment.RefreshCancelled`) is **control
   flow, not failure** — `jobs._run` catches it separately, and a bare
   `except Exception` around it reports a user cancel as "Refresh failed".
4. **Counts are server-authoritative.** Every frame in `Job._COUNTED` carries
   the job's cumulative counts and the client *assigns* them. A `phase` frame's
   `total` is per-view and the server has already folded it in; accumulating
   client-side double-counted (the chip read "Quotes 4/8" for a 4-symbol
   portfolio).
5. **Both totals are planned before any frame exists** (v1.11.1).
   `_plan_totals(job)` runs in `submit()` — *before* the `queued` emit and
   before the worker thread, so the earliest frame and the reattach snapshot
   both carry a whole-job denominator. Each phase then **reconciles** its own
   figure (`Job.set_count`, never `bump`): quotes swaps each view's estimate for
   its real count and re-derives `actual-where-known + planned-for-the-rest`;
   news assigns the exact `len(symbols) + 1`. Every skip path — no
   `news_sentiment`, no symbols, cancelled, a view that emitted no `start` —
   must **release its reservation**, or the bar can never reach 100%.
   `_parse_entries` is shared by the planner and the quotes phase and must stay
   that way; if they disagree the denominator visibly corrects on the first
   item, which is the whole thing this removes. Why it matters: `news_total`
   used to be bumped at the news phase's start, so the client's denominator was
   quotes-only until then and the bar filled completely and then rewound by
   half (once per portfolio on an all-scope run). `rfRender` also clamps the
   painted percentage monotonically (`REFRESH.pct`, reset in `rfStart`/
   `rfReset`) so a late upward correction can't walk it backwards.

### Event stream

`GET /api/refresh-job/<id>/stream?since=<seq>`. Job events carry monotonic
`seq ≥ 1` and are replayed on reconnect; **connection frames (`hello`, `ping`,
`end`) carry `seq: 0` and are never replayed** — advancing the cursor on them
desyncs it. Types: `job`, `phase`, `item`, `item_stage`, `rate_limited` /
`rate_cleared`, `view_saved`, and the terminals `done` / `error` / `job`
{state: cancelled, drained: true}. `item_stage.stage` is
`start|fetch|pass1|pass2|market|aggregate` — `NS_PROG_STAGE` in `app.js` must
match; the per-ticker modal is opt-in behind the status chip. Each news `item`
carries its `outcome` (ok / failed / empty), and the counts include
`news_scored` / `news_failed`; the chip reads "News x/y · n failed". `dropped: true` (the `since` predates the 6000-frame
ring, same contract as `logbuf.read`) ⇒ the client must cold-re-read.
Client state is `REFRESH` + `{id, lastSeq}` in `sessionStorage`, saved per
frame; `GET /api/refresh-job/current` on load is what makes a job survive F5.

### Two save guards (both would silently destroy user data without them)

- **Never save a degraded batch.** `save_view` overwrites `rows` wholesale, and
  Yahoo answers an overloaded batch with `error` rows rather than an exception.
  `>20%` errors (`_MAX_ERROR_FRACTION`) ⇒ skip the save, emit
  `phase {saved: false, reason: "degraded"}`, keep the prior rows.
- **Always `set_last=False`.** A background job must never repoint the
  restore-on-launch target at whatever portfolio it happened to touch.

Also: **all `persistence` writes are atomic now** (`_atomic_write`, tmp file in
the same dir + `os.replace`). They were bare `write_text`, and the readers catch
`JSONDecodeError` and return `{}` — so a truncated write made *every saved
portfolio silently disappear*. Survivable when writes only followed an
interactive build; not with an unattended job and `os._exit(0)` on quit
(`shutdown_server` now calls `jobs.shutdown(1.5)` first).

The all-scope quotes phase runs **one portfolio at a time** with a ~500 ms gap
(5 workers *within* a portfolio) — `warmRecentTabs` established empirically that
concurrent portfolio work makes yfinance return empty frames, i.e. exactly the
degraded rows the guard above would then discard. Symbols shared across views
are fetched once. The news phase runs **once per job** over the union of
symbols, so the market-wide News read is refreshed once, not per portfolio.

### `rescore_window(symbols, days)` — instant News-window change

Changing the window (3/7/14/30D) used to do nothing until the next Refresh. It
now re-aggregates the **already-read cached headlines** (their lens and score)
under the new window's tau: zero network, zero LLM, zero ML inference. The
Market read is carried unchanged — it is defined on its 7-day window. ~10 ms.

**The honest limitation, and it is surfaced rather than hidden.** News is cached
per window (`news|{sym}|{days}`), so *widening cannot conjure articles that were
never fetched* — narrowing is exact, widening re-weights the same evidence.
`coverage[sym].truncated_by_fetch` is derived from **which windows were actually
fetched** (`max(from_windows) < days`), not from how old the newest article is —
a quiet ticker with no week-old news is not a truncated fetch, and an earlier
timestamp heuristic flagged both. `#ns-cov-hint` renders it. The carried-forward
LLM brief is stamped `brief_stale: true`; **never `_history_append`** from a
rescore — it would double-count the day in the Track record and in the Market
read's live-anchor reference.
