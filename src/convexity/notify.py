"""My Investments notifications (roadmap Phase 9): a background pass while
the app runs, macOS notifications for new "Needs a look" items, and a weekly
recap after the Friday US close.

What it reuses, so the rail and the notifications never disagree:

- **What to send** is the rail's own answer (``attention.evaluate``): an item
  shown (so not dismissed or snoozed), unread, and not notified at this level
  or a full step better (``attention.should_notify``). Plain price alerts are
  information, so they stay in-app (Appendix D, *Alert for Alerts*).
- **The pass is a job** (``jobs.submit``, phase ``watch``): single-flight
  with every refresh, cancellable, and marked ``background`` so the topbar
  chip doesn't show it and a Refresh by the owner supersedes it. It fetches
  held shares only, cache-first (``cache.prefer_cached``), then computes the
  page through ``investments.page`` like any request; a degraded page sends
  nothing and records nothing (the jobs rule: never save a degraded batch).
- **The recap** is computed from a page already computed, never a fetch:
  the TWR index, the benchmark index and ``series.flows`` (the month
  figure's formula), and a dash for anything the page can't support.

Delivery: the desktop app registers a sink in-process (``add_sink``) and
shows each message through Qt; the page polls ``outbox`` and, in browser
mode only, shows a quiet toast. Logs name the rule and the ticker, never an
amount (Appendix B: users paste logs into public issues).
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections import deque
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from convexity import paths

DEFAULTS = {"notify_on": True, "quiet_on": True, "quiet_from": "22:00", "quiet_to": "08:00"}

INTERVAL_S = 30 * 60  # owner's pick (2026-10-15): every 30 min in market hours
AFTER_CLOSE_S = 20 * 60  # one pass this long after a close, when the close is in
AFTER_CLOSE_WINDOW_S = 3 * 3600  # ...if the app is open within this window
TICK_S = 60
FIRST_TICK_S = 90  # after launch: the page's own warm-up computes first
MAX_EACH = 3  # notifications per pass; the rest fold into one "+N more"
DEGRADED_FRACTION = 0.20  # the jobs rule (_MAX_ERROR_FRACTION)
RECAPS_KEPT = 12

_NY = "America/New_York"
RECAP_AFTER = (16, 30)  # Friday, New York time: the US close and its prints
# A recap is news until Monday's US close: a launch after that still builds
# it (the rail shows it), but sends no notification about last week.
RECAP_NOTIFY_DAYS = 3

# Trading hours per Yahoo suffix (local time, weekdays; holidays are not
# listed: a pass on one just finds the cache). Anything else: US hours.
_US = (_NY, (9, 30), (16, 0))
_EURO = ("Europe/Paris", (9, 0), (17, 30))
_MARKETS = {
    "L": ("Europe/London", (8, 0), (16, 30)),
    "IL": ("Europe/London", (8, 0), (16, 30)),
    "IR": ("Europe/Dublin", (8, 0), (16, 30)),
    **dict.fromkeys(("PA", "AS", "BR", "LS", "DE", "F", "MI", "MC", "VI", "HE", "ST", "CO", "OL"), _EURO),
    "SW": ("Europe/Zurich", (9, 0), (17, 30)),
    "TO": ("America/Toronto", (9, 30), (16, 0)),
    "V": ("America/Toronto", (9, 30), (16, 0)),
    "T": ("Asia/Tokyo", (9, 0), (15, 30)),
    "HK": ("Asia/Hong_Kong", (9, 30), (16, 0)),
    "AX": ("Australia/Sydney", (10, 0), (16, 0)),
    "SI": ("Asia/Singapore", (9, 0), (17, 0)),
}  # fmt: skip


def market(symbol: str) -> tuple[str, tuple[int, int], tuple[int, int]]:
    tail = symbol.rsplit(".", 1)[1].upper() if "." in symbol else ""
    return _MARKETS.get(tail, _US)


# ---------------------------------------------------------------- settings


def settings() -> dict:
    """The switch and quiet hours (``prefs.json``, never localStorage)."""
    from convexity.persistence import load_prefs

    prefs = load_prefs()
    return {k: prefs.get(k, v) for k, v in DEFAULTS.items()}


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def in_quiet(now_local: datetime, s: dict) -> bool:
    """Inside quiet hours (local time); a span may wrap midnight."""
    if not s.get("quiet_on"):
        return False
    f, t, m = (
        _minutes(s["quiet_from"]),
        _minutes(s["quiet_to"]),
        now_local.hour * 60 + now_local.minute,
    )
    if f == t:
        return False
    return f <= m < t if f < t else (m >= f or m < t)


# --------------------------------------------------------------- schedule


def due(now: datetime, symbols, last: datetime | None) -> str | None:
    """Why a pass is due now, or None: every ``INTERVAL_S`` while a held
    listing's market is open, and once ``AFTER_CLOSE_S`` after its close.
    Nothing at night or at weekends. Pure (``now`` is aware)."""
    for tz, (oh, om), (ch, cm) in sorted({market(s) for s in symbols}):
        local = now.astimezone(ZoneInfo(tz))
        if local.weekday() >= 5:
            continue
        opens = local.replace(hour=oh, minute=om, second=0, microsecond=0)
        closes = local.replace(hour=ch, minute=cm, second=0, microsecond=0)
        if opens <= local < closes and (last is None or (now - last).total_seconds() >= INTERVAL_S):
            return "session"
        after = closes + timedelta(seconds=AFTER_CLOSE_S)
        if after <= local < after + timedelta(seconds=AFTER_CLOSE_WINDOW_S) and (
            last is None or last < after
        ):
            return "after-close"
    return None


# ---------------------------------------------------------------- outbox

_OUT: deque = deque(maxlen=50)
_OUT_LOCK = threading.Lock()
_SEQ = {"v": 0}
_SINKS: list = []


def add_sink(fn) -> None:
    """``fn(message)`` for every message sent (the desktop app's tray). Called
    from the scheduler's thread: the sink hands it to its own GUI thread."""
    if fn not in _SINKS:
        _SINKS.append(fn)


def remove_sink(fn) -> None:
    if fn in _SINKS:
        _SINKS.remove(fn)


def send(kind: str, title: str, body: str, key: str, subtitle: str | None = None) -> dict:
    with _OUT_LOCK:
        _SEQ["v"] += 1
        msg = {"seq": _SEQ["v"], "kind": kind, "title": title, "subtitle": subtitle, "body": body,
               "key": key, "at": datetime.now(UTC).isoformat(timespec="seconds")}  # fmt: skip
        _OUT.append(msg)
    for fn in list(_SINKS):
        try:
            fn(msg)
        except Exception as exc:  # a broken sink never stops the others
            print(f"[notify] sink failed: {type(exc).__name__}")
    return msg


def outbox(since: int = 0) -> dict:
    """What the page polls: the messages after ``since``, and whether a
    native sink (the desktop app) shows them already."""
    with _OUT_LOCK:
        seq = _SEQ["v"]
        # A cursor past ours is a page that outlived a server restart (the
        # count starts again at 0): everything here is new to it.
        since = 0 if since > seq else since
        items = [m for m in _OUT if m["seq"] > since]
    return {"native": bool(_SINKS), "seq": seq, "items": items}


# ------------------------------------------------------------ the rail -> out

_HELD = {"items": False, "recap": None}  # due during quiet hours, sent at their end
NEEDS = "Needs a look"
RAIL_KEY = "__rail__"
RECAP_KEY = "__recap__"
# One rail-to-notification step at a time: the job's pass, the quiet-hours
# end (the scheduler's thread) and the switch going off each read and write
# ``notified``/``armed``; two at once sent the same item twice, and a pass
# finishing after a switch-off re-armed it (adversarial review, 2026-10-15).
_RAIL_LOCK = threading.Lock()


def _now_local(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone()


def notify_rail(att: dict | None, *, now: datetime | None = None, s: dict | None = None,
                summary: bool = False) -> int:  # fmt: skip
    """Send what the rail says is due (``attention.should_notify``); returns
    how many items went out. The first pass after notifications are switched
    on takes the rail as it is as a baseline (no burst of old items). Inside
    quiet hours nothing is sent or recorded: the items wait, still unread,
    and go out as one summary (``summary``, from ``_quiet_ended``) when quiet
    hours end."""
    from convexity import attention

    with _RAIL_LOCK:
        s = s or settings()
        if not s["notify_on"] or not att or att.get("page") is False:
            return 0
        st = attention.load_state()
        items = attention.should_notify(att, st)
        if not st["armed"]:
            attention.mark_notified([[it["key"], it["level"]] for it in items])
            print(f"[notify] armed; {len(items)} item(s) already in the rail kept as the baseline")
            return 0
        if not items:
            return 0
        if in_quiet(_now_local(now), s):
            _HELD["items"] = True
            return 0
        _send_items(items, summary)
        attention.mark_notified([[it["key"], it["level"]] for it in items])
    for it in items:
        print(f"[notify] sent {it['rule']} {','.join(it.get('symbols') or [])}")
    return len(items)


def _send_items(items: list[dict], summary: bool) -> None:
    if summary and len(items) > 1:
        send("summary", f"{len(items)} items came up overnight",
             " · ".join(it["text"] for it in items[:2]) + (" · …" if len(items) > 2 else ""),
             RAIL_KEY, NEEDS)  # fmt: skip
    else:
        for it in items[:MAX_EACH]:
            send("item", it["text"], it.get("sub") or "", it["key"], NEEDS)
        rest = items[MAX_EACH:]
        if rest:
            send("more", f"+{len(rest)} more need{'s' if len(rest) == 1 else ''} a look",
                 " · ".join(it["text"] for it in rest[:2]), RAIL_KEY, NEEDS)  # fmt: skip


# ------------------------------------------------------------------- watch


def _ccy() -> str:
    """The currency the page opens in (app.js ``FX_QUOTE``: the default, else
    USD). Not the book's base: currency and down-from-cost items are keyed by
    the view, so a pass in another currency than the owner reads would find
    their items unread and send them again (adversarial review, 2026-10-15)."""
    from convexity.persistence import load_prefs

    return load_prefs().get("default_ccy") or "USD"


def degraded(out: dict) -> str | None:
    """Why a page must not drive notifications, or None."""
    from convexity import investments

    if not investments._healthy(out):
        return "unhealthy"
    if "attention" not in out:
        return "no rail"
    held = [p for p in out.get("positions") or [] if not p.get("pending")]
    blind = sum(1 for p in held if p.get("note") or not isinstance(p.get("price"), int | float))
    if held and blind / len(held) > DEGRADED_FRACTION:
        return f"{blind} of {len(held)} without a price"
    return None


def watch(cancel=None) -> str:
    """One background pass (``jobs._run_watch_phase``): held shares only,
    cache-first; then the page, the rail, and what is due."""
    from convexity import investments
    from convexity.analytics import _RF_TICKER
    from convexity.cache import prefer_cached

    if not settings()["notify_on"]:
        return "off"
    held = investments.held_symbols()
    if not held:
        return "no holdings"
    # The benchmark and the risk-free rate stay live too: the page shows
    # "vs S&P" and the ratios, which a stale line would make up.
    live = {*held, investments.INV_BENCHMARKS[investments.bench_key()][0], _RF_TICKER}
    if cancel is not None and cancel.is_set():
        return "cancelled"
    with prefer_cached(live):
        out = investments.page(_ccy(), fresh=True)
    if cancel is not None and cancel.is_set():
        return "cancelled"
    why = degraded(out)
    if why:
        print(f"[notify] pass skipped: the page is degraded ({why})")
        return "degraded"
    n = notify_rail(out.get("attention"))
    maybe_recap()
    return f"sent {n}"


# ------------------------------------------------------------- scheduler

_STATE: dict = {"last": None, "quiet": False, "thread": None, "stop": threading.Event()}


def _submit():
    from convexity import jobs

    return jobs.submit(scope="current", phases=["watch"], days=7,
                       entries_by_view={jobs.BOOK_VIEW: ""}, context={"background": True})  # fmt: skip


def tick(now: datetime | None = None, *, submit=None) -> str | None:
    """One scheduler step (every minute): quiet-hours end, the recap, and a
    pass when one is due. A job already running means "try next minute"."""
    from convexity import investments

    now = now or datetime.now(UTC)
    s = settings()
    if not s["notify_on"]:
        cancel_watch()
        return "off"
    quiet = in_quiet(_now_local(now), s)
    if _STATE["quiet"] and not quiet:
        _quiet_ended(now, s)
    _STATE["quiet"] = quiet
    maybe_recap(now)
    reason = due(now, investments.held_symbols(), _STATE["last"])
    if not reason:
        return None
    _job, outcome = (submit or _submit)()
    if outcome != "created":
        return "busy"
    _STATE["last"] = now
    return reason


def _quiet_ended(now: datetime, s: dict) -> None:
    """Send what waited, from the page already computed (no fetch). The
    flag is cleared whatever happens: items read or gone overnight leave
    nothing to say, and a later pass in the day must not say "overnight"."""
    from convexity import investments

    if _HELD["items"]:
        _HELD["items"] = False
        out = investments.computed_page(_ccy())
        # The last page computed may be one a pass refused (degraded).
        if out and not degraded(out):
            notify_rail(out.get("attention"), now=now, s=s, summary=True)
    rec, _HELD["recap"] = _HELD["recap"], None
    if rec:
        _send_recap(rec)


def run_now() -> tuple[object, str]:
    """Settings -> "Check now": a pass at once (still a job, still single-flight)."""
    if not settings()["notify_on"]:
        return None, "off"
    job, outcome = _submit()
    if outcome == "created":
        _STATE["last"] = datetime.now(UTC)
    return job, outcome


def cancel_watch() -> None:
    from convexity import jobs

    j = jobs.current()
    if j is not None and j.phases == ("watch",):
        j.request_cancel("notifications off")


def on_prefs(saved: dict) -> None:
    """After Settings saved: switched off means nothing scheduled or sent,
    and the next switch-on starts from the rail as it is then."""
    if saved.get("notify_on", DEFAULTS["notify_on"]) is False:
        from convexity import attention

        cancel_watch()
        with _RAIL_LOCK:  # after any pass already sending: it would re-arm
            attention.disarm()
            _HELD["items"], _HELD["recap"] = False, None


def _loop() -> None:
    stop = _STATE["stop"]
    if stop.wait(FIRST_TICK_S):
        return
    while True:
        try:
            tick()
        except Exception as exc:  # the scheduler outlives any one bad tick
            print(f"[notify] tick failed: {type(exc).__name__}: {exc}")
        if stop.wait(TICK_S):
            return


def start() -> None:
    """Start the scheduler thread once (server.start_server, both modes)."""
    if os.environ.get("CONVEXITY_NOTIFY") == "0":  # the test suite's servers
        return
    t = _STATE["thread"]
    if t is not None and t.is_alive():
        return
    _STATE["stop"].clear()
    _STATE["thread"] = threading.Thread(target=_loop, name="notify-scheduler", daemon=True)
    _STATE["thread"].start()


def stop() -> None:
    _STATE["stop"].set()


# ------------------------------------------------------------------- recap
#
# One recap a week, for the week to the last Friday whose US close has
# passed; built from a page already computed (the memo or the last good page
# on disk), kept in ``state/recaps.json`` (derived: a malformed file is {}),
# and caught up on the next launch when the app was closed on Friday.

_RECAP_LOCK = threading.Lock()


def recap_friday(now: datetime | None = None) -> date:
    """The last Friday whose 16:30 New York time has passed."""
    et = (now or datetime.now(UTC)).astimezone(ZoneInfo(_NY))
    d = et.date()
    fri = d - timedelta(days=(d.weekday() - 4) % 7)
    if fri == d and (et.hour, et.minute) < RECAP_AFTER:
        fri -= timedelta(days=7)
    return fri


def _recaps_path():
    return paths.state_file("recaps")


def load_recaps() -> dict[str, dict]:
    try:
        raw = json.loads(_recaps_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    weeks = raw.get("weeks") if isinstance(raw, dict) else None
    return {k: v for k, v in (weeks or {}).items() if isinstance(k, str) and isinstance(v, dict)}


def _save_recap(r: dict) -> None:
    from convexity.persistence import _atomic_write

    weeks = load_recaps()
    weeks[r["week_end"]] = r
    keep = dict(sorted(weeks.items())[-RECAPS_KEPT:])
    _atomic_write(_recaps_path(), json.dumps({"weeks": keep}, ensure_ascii=False, indent=1))


def _num(v) -> float | None:
    return (
        float(v)
        if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)
        else None
    )


def _last_at_or_before(dates: list[str], iso: str) -> int | None:
    i = None
    for k, d in enumerate(dates):
        if d <= iso:
            i = k
        else:
            break
    return i


def build_recap(out: dict, friday: date, today: date, events: dict | None = None) -> dict | None:
    """The week to ``friday`` from one computed page, or None when the page
    can't say yet (Friday's close not in, no series). Every figure is the
    page's own; one it can't support is None (a dash on screen)."""
    ser = out.get("series") or {}
    dates = ser.get("dates") or []
    end = _last_at_or_before(dates, friday.isoformat())
    if end is None:
        return None
    if dates[-1] < friday.isoformat() and (today - friday).days < 3:
        return None  # Friday's close not in yet: try again
    start = _last_at_or_before(dates, (friday - timedelta(days=7)).isoformat())
    partial = start is None
    start = 0 if start is None else start
    if end <= start:
        return None

    def ratio(arr):
        a, b = (_num(arr[start]), _num(arr[end])) if arr and len(arr) > end else (None, None)
        return (b / a - 1) * 100 if a and b else None

    val, fl = ser.get("value") or [], ser.get("flows")
    gain = None
    if fl and len(fl) > end and len(val) > end:
        gain = (val[end] - val[start]) - (fl[end] - fl[start])
    movers = []
    for p in out.get("positions") or []:
        x = p.get("xray") or {}
        wk = _num(x.get("week_pct"))
        # A holding's week only when its last close is that Friday: a page
        # computed later would give a different week (a dash then).
        if (
            wk is not None
            and x.get("day_date") == dates[end]
            and not p.get("pending")
            and not p.get("note")
        ):
            movers.append({"symbol": p["symbol"], "name": p.get("name") or p["symbol"], "pct": wk})
    movers.sort(key=lambda m: -m["pct"])
    divs = [e for e in out.get("entries") or [] if e.get("type") == "dividend"
            and dates[start] < (e.get("date") or "") <= dates[end] and _num(e.get("cash")) is not None]  # fmt: skip
    # An entry's ``cash`` is in its own currency: in the base currency at
    # its own ``fx_rate`` (base per unit, the ledger's), and one without a
    # rate would need that day's FX (a fetch), so the total is a dash then.
    base = out.get("base_ccy") or out.get("ccy")
    in_base = [_num(e["cash"]) * (1.0 if (e.get("ccy") or base) == base else _num(e.get("fx_rate")) or math.nan)
               for e in divs]  # fmt: skip
    att = out.get("attention") or {}
    names = {p["symbol"]: p.get("name") or p["symbol"] for p in out.get("positions") or []}
    nxt = []
    lo, hi = friday.isoformat(), (friday + timedelta(days=7)).isoformat()
    for sym, ev in sorted((events or {}).items()):
        if sym not in names:
            continue
        for field, what in (("earnings", "earnings"), ("ex_div", "ex-dividend")):
            d = ev.get(field)
            if isinstance(d, str) and lo < d <= hi:
                nxt.append({"symbol": sym, "name": names[sym], "what": what, "date": d})
    nxt.sort(key=lambda e: (e["date"], e["name"]))
    bench = out.get("benchmark") or {}
    return {
        "week_end": friday.isoformat(),
        "from": dates[start],
        "to": dates[end],
        "partial": partial,
        "ccy": out.get("ccy"),
        "return_pct": ratio(ser.get("twr_index")),
        "bench_pct": ratio(ser.get("bench_index")),
        "bench_label": bench.get("label"),
        "gain": gain,
        "best": movers[0] if movers else None,
        "worst": movers[-1] if len(movers) > 1 else None,
        "dividends": {
            "amount": _num(sum(in_base)),
            "ccy": base,
            "count": len(divs),
        },  # fmt: skip
        "needs_look": len(att["high"]) if isinstance(att.get("high"), list) else None,
        "next_week": nxt,
        "demo": bool(out.get("demo")),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _pct(v: float | None) -> str | None:
    return None if v is None else f"{'+' if v >= 0 else '−'}{abs(v):.1f}%"


def _day(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def recap_message(r: dict) -> tuple[str, str]:
    parts = []
    if (p := _pct(r.get("return_pct"))) is not None:
        parts.append(p)
    if (b := _pct(r.get("bench_pct"))) is not None:
        parts.append(f"{r.get('bench_label') or 'Benchmark'} {b}")
    n = r.get("needs_look")
    if n is not None:
        parts.append(
            "nothing needs a look" if n == 0 else f"{n} need{'s' if n == 1 else ''} a look"
        )
    return f"Your week to {_day(date.fromisoformat(r['week_end']))}", " · ".join(parts)


def _send_recap(r: dict) -> None:
    title, body = recap_message(r)
    send("recap", title, body, RECAP_KEY, "Weekly recap")
    print(f"[notify] sent the weekly recap ({r['week_end']})")


def maybe_recap(now: datetime | None = None, *, notify: bool = True) -> dict | None:
    """This week's recap: built once, from a computed page, and sent (or held
    for quiet hours). The page route builds it too (``notify=False``): one
    the owner opened in the app needs no notification."""
    from convexity import attention, investments

    fri = recap_friday(now)
    with _RECAP_LOCK:
        have = load_recaps().get(fri.isoformat())
        if have:
            return have
        out = investments.computed_page(_ccy())
        # Kept for good, so never from a page short of the truth: the last
        # page computed may be one a pass would refuse (a cold start's
        # holding still waiting for its splits left out of the return: live
        # /verify, 2026-10-15). The last good page on disk is fine once it
        # has Friday's close; one from days before would pass the holiday
        # allowance in build_recap and freeze a short week.
        if not out or degraded(out):
            return None
        # Computed after Friday's close, or the recap would keep the day's
        # intraday prices for good (a busy after-close pass leaves the
        # 15:50 page in the memo; adversarial review, 2026-10-15).
        cutoff = datetime(fri.year, fri.month, fri.day, *RECAP_AFTER, tzinfo=ZoneInfo(_NY))
        try:
            at = datetime.fromisoformat(str(out.get("computed_at")))  # 3.11+ reads "Z"
        except ValueError:
            return None
        if at.tzinfo is None or at < cutoff:
            return None
        if (
            out.get("stale")
            and ((out.get("series") or {}).get("dates") or [""])[-1] < fri.isoformat()
        ):
            return None
        today = (now or datetime.now(UTC)).astimezone(ZoneInfo(_NY)).date()
        r = build_recap(out, fri, today, attention.load_events())
        if r is None:
            return None
        try:
            _save_recap(r)
        except OSError as exc:
            print(f"[notify] saving the recap failed: {type(exc).__name__}")
            return r
    s = settings()
    late = today > fri + timedelta(days=RECAP_NOTIFY_DAYS) or (
        today == fri + timedelta(days=RECAP_NOTIFY_DAYS)
        and (now or datetime.now(UTC)).astimezone(ZoneInfo(_NY)).hour >= 16
    )
    if notify and s["notify_on"] and not late:
        if in_quiet(_now_local(now), s):
            _HELD["recap"] = r
        else:
            _send_recap(r)
    return r


def latest_recap() -> dict | None:
    """For the page: this week's (built now if due and possible), else the
    newest kept."""
    r = maybe_recap(notify=False)
    if r:
        return r
    weeks = load_recaps()
    return weeks[max(weeks)] if weeks else None


def test_message() -> dict:
    return send("test", "Notifications are on", "This is how a Needs-a-look item will reach you.",
                RAIL_KEY, "Convexity")  # fmt: skip
