"""My Investments' Attention rail (roadmap Phase 7): X-ray rules, events,
severity, dismiss and the unread badge.

The rail tells the owner what needs a look, calmly (roadmap Appendix D:
price-tracking alerts make retail investors trade more and earn less). So the
rules are few, static and the owner's own: every threshold is editable in
Settings -> Attention, and a dismissed item stays away until it gets worse.

How it stays off the page's cold-open path (Phase 4):

- ``investments.book()`` computes what the rules read (each holding's weight,
  sector, drawdowns and sigma moves, in its ``xray`` block) with the page, so
  the last good page on disk (``state/book_cache.json``) carries it too;
- ``evaluate()`` here is pure arithmetic over that payload, run on every
  response (fresh or cached), so a changed threshold or a dismiss shows at
  once without recomputing the book;
- events (earnings and ex-dividend dates) come from a cache on disk
  (``state/events.json``), filled in the background a day at a time and by
  the topbar Refresh; the page only reads it.

State (``state/attention.json``) holds preferences only (thresholds, what
was dismissed or seen at which level), never the book: a malformed file is
read as the defaults.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import UTC, date, datetime, timedelta

from convexity import paths
from convexity.helpers import major_ccy

# ------------------------------------------------------------------ rules
#
# One registry for Settings, validation and the rail. ``step`` is how much
# worse (in the rule's unit, past the level it was dismissed at) an item must
# get to come back (owner, 2026-10-13: "when it gets worse"). Severity is the owner's pick: the four
# risk rules are "Needs a look", the rest "For your information".

RULES: dict[str, dict] = {
    "holding": {"label": "One holding above", "unit": "%", "value": 15.0, "min": 1, "max": 100,
                "step": 5.0, "severity": "high", "note": "Funds and ETFs don't count."},
    "sector": {"label": "One sector above", "unit": "%", "value": 35.0, "min": 1, "max": 100,
               "step": 5.0, "severity": "high"},
    "currency": {"label": "One currency above", "unit": "%", "value": 60.0, "min": 1, "max": 100,
                 "step": 5.0, "severity": "high",
                 "note": "Your display currency doesn't count."},
    "from_cost": {"label": "Down from cost by", "unit": "%", "value": 20.0, "min": 1, "max": 95,
                  "step": 10.0, "severity": "high"},
    "move": {"label": "A day move above", "unit": "σ", "value": 3.0, "min": 1.5, "max": 10,
             "step": 0.0, "severity": "high"},
    "from_peak": {"label": "Down from its peak by", "unit": "%", "value": 25.0, "min": 1, "max": 95,
                  "step": 10.0, "severity": "info"},
    "divergence": {"label": "News read disagrees with the week's price", "unit": "σ", "value": 1.0,
                   "min": 0.5, "max": 5, "step": 0.0, "severity": "info"},
    "events": {"label": "Earnings and ex-dividend dates, next", "unit": "days", "value": 14.0,
               "min": 1, "max": 90, "step": 0.0, "severity": "upcoming"},
}  # fmt: skip

# A day move is news only while it is the last trading day's: a Friday move
# still shows on Monday, last week's doesn't.
MOVE_FRESH_DAYS = 4
STALE_READ_SLACK = 1.0  # days past its own window before a News read is too old

_LOCK = threading.Lock()


def _state_path():
    return paths.state_file("attention")


def _num(v) -> float | None:
    if not isinstance(v, int | float) or isinstance(v, bool):
        return None
    try:
        f = float(v)  # an int with 400 digits overflows (a 400, never a 500)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _clean_rules(raw) -> dict:
    """{rule: {on, value}} for every rule, the defaults filling any gap;
    a value out of range is clamped, anything unknown dropped."""
    out = {}
    raw = raw if isinstance(raw, dict) else {}
    for k, r in RULES.items():
        got = raw.get(k) if isinstance(raw.get(k), dict) else {}
        v = _num(got.get("value"))
        on = got.get("on")
        out[k] = {"on": on if isinstance(on, bool) else True,
                  "value": min(max(v, r["min"]), r["max"]) if v is not None else r["value"]}  # fmt: skip
    return out


def _clean_levels(raw) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    nums = {k[:200]: _num(v) for k, v in raw.items() if isinstance(k, str)}
    return {k: v for k, v in nums.items() if v is not None}


def load_state() -> dict:
    """Thresholds, dismissed and seen; a missing or malformed file is the
    defaults (it is a preference, never the book)."""
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    return {"rules": _clean_rules(raw.get("rules")), "dismissed": _clean_levels(raw.get("dismissed")),
            "seen": _clean_levels(raw.get("seen"))}  # fmt: skip


def _save_state(st: dict) -> None:
    from convexity.persistence import _atomic_write

    _atomic_write(_state_path(), json.dumps(st, ensure_ascii=False, indent=1))


def _update(change) -> dict:
    with _LOCK:
        st = load_state()
        change(st)
        _save_state(st)
        return st


def set_rules(changes) -> dict:
    """Merge {rule: {on?, value?}} into the thresholds. Raises ValueError on
    an unknown rule or a value that isn't a number."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("rules must be an object")
    for k, c in changes.items():
        if k not in RULES or not isinstance(c, dict):
            raise ValueError(f"unknown rule {str(k)[:40]}")
        if "value" in c and _num(c["value"]) is None:
            raise ValueError(f"{k}: value must be a number")
        if "on" in c and not isinstance(c["on"], bool):
            raise ValueError(f"{k}: on must be true or false")

    def change(st):
        for k, c in changes.items():
            st["rules"][k] = {**st["rules"][k], **{x: c[x] for x in ("on", "value") if x in c}}
        st["rules"] = _clean_rules(st["rules"])

    return _update(change)["rules"]


def _check_key(key) -> str:
    if not isinstance(key, str) or not key or len(key) > 200:
        raise ValueError("key required")
    return key


def dismiss(key, level) -> None:
    """Hide an item until it gets worse than ``level`` (or clears and fires
    again: ``prune`` forgets it then)."""
    key, lvl = _check_key(key), _num(level)
    if lvl is None:
        raise ValueError("level must be a number")

    def change(st):
        st["dismissed"].pop(key, None)
        st["dismissed"][key] = lvl
        st["dismissed"] = _cap(st["dismissed"])

    _update(change)


DATED_KEEP_DAYS = 30
MAX_KEPT = 2000  # dismissed / seen entries kept; the oldest go first


def _cap(d: dict) -> dict:
    return dict(list(d.items())[-MAX_KEPT:]) if len(d) > MAX_KEPT else d


def mark_seen(items) -> None:
    """[[key, level], ...]: the items the owner has now seen (the page opened)."""
    if not isinstance(items, list) or len(items) > MAX_KEPT:
        raise ValueError("items must be a list")
    pairs = []
    for it in items:
        if not isinstance(it, list | tuple) or len(it) != 2 or _num(it[1]) is None:
            raise ValueError("each item is [key, level]")
        pairs.append((_check_key(it[0]), _num(it[1])))

    def change(st):
        for k, lvl in pairs:
            st["seen"].pop(k, None)  # re-inserted last: the cap drops the oldest
            st["seen"][k] = lvl
        st["seen"] = _cap(st["seen"])

    if pairs:
        _update(change)


def scope(key: str) -> str:
    """What a page must have checked to say an item no longer fires: the
    rule and the holding (``from_peak|NVDA``), the rule alone for sectors,
    the rule and the view for currencies, and for down-from-cost, whose
    figure depends on the display currency, the key itself."""
    parts = key.split("|")
    if parts[0] == "sector":
        return "sector"
    if parts[0] == "currency":
        return f"currency|{parts[-1]}"
    if parts[0] == "from_cost":
        return key
    return "|".join(parts[:2])


def prune(firing: set[str], checked: set[str]) -> None:
    """Forget dismissed and seen items that were checked on this page and no
    longer fire, so one that clears and fires again is new. Only called on
    a fresh, complete page. An item the page couldn't check stays: its rule
    switched off, a holding without prices today, a currency item seen in
    another display currency (adversarial review, 2026-10-13: each of these
    used to forget a dismissal, and the item came back)."""

    old = (date.today() - timedelta(days=DATED_KEEP_DAYS)).isoformat()

    def keep(k: str) -> bool:
        # A dated item (a day's move, a read, an event) long past is gone
        # whatever was checked: a read that expired is never checked again.
        tail = k.rsplit("|", 1)[-1]
        if len(tail) == 10 and tail[4] == "-" and tail < old:
            return False
        return k in firing or scope(k) not in checked

    with _LOCK:
        st = load_state()
        if all(keep(k) for k in (*st["dismissed"], *st["seen"])):
            return
        st["dismissed"] = {k: v for k, v in st["dismissed"].items() if keep(k)}
        st["seen"] = {k: v for k, v in st["seen"].items() if keep(k)}
        _save_state(st)


# ---------------------------------------------------------------- evaluate


def _pct(v: float, limit: float | None = None) -> str:
    """Whole percent from 10 up, one decimal below, and one decimal whenever
    whole would read as the limit itself ("15% · above your 15% limit")."""
    a = abs(v)
    whole = a >= 10 and (limit is None or round(a) > limit)
    return f"{a:.0f}%" if whole else f"{a:.1f}%"


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _over(it: dict, past: dict[str, float]) -> bool:
    """Not dismissed (or seen), or a full step worse than when it was: the
    level is how far past the limit it is, in the rule's unit. A rule with
    no step (a dated item: a day's move, a read, an event) never comes back
    under the same key."""
    if it["key"] not in past:
        return True
    step = RULES[it["rule"]]["step"] if it.get("rule") in RULES else 0.0
    return step > 0 and it["level"] >= past[it["key"]] + step


def _names(xs: list[dict], most: int = 3, extra: str | None = None) -> str:
    """ "NVIDIA, Microsoft, Apple, 2 more and cash": the biggest first."""
    parts = [x["name"] for x in sorted(xs, key=lambda x: -(x.get("weight") or 0))[:most]]
    if len(xs) > most:
        parts.append(f"{len(xs) - most} more")
    if extra:
        parts.append(extra)
    return ", ".join(parts[:-1]) + f" and {parts[-1]}" if len(parts) > 1 else "".join(parts)


def _items(page: dict, rules: dict, today: date, checked: set[str]) -> list[dict]:
    held = [
        p for p in page.get("positions") or [] if not p.get("pending") and _num(p.get("weight"))
    ]
    disp = page.get("ccy") or "USD"
    out: list[dict] = []

    def add(rule, key, level, text, sub, symbols=(), **extra):
        out.append({"rule": rule, "key": key, "level": level, "severity": RULES[rule]["severity"],
                    "text": text, "sub": sub, "symbols": list(symbols),
                    "limit": rules[rule]["value"], **extra})  # fmt: skip

    def on(rule):
        return rules[rule]["on"]

    lim = {k: r["value"] for k, r in rules.items()}
    if on("holding"):
        checked.update(f"holding|{p['symbol']}" for p in held)
        for p in held:
            # A fund is diversified already: 30% in a world ETF isn't one bet.
            if p["weight"] > lim["holding"] and not p.get("fund"):
                add("holding", f"holding|{p['symbol']}", round(p["weight"] - lim["holding"], 2),
                    f"{p['name']} is {_pct(p['weight'], lim['holding'])} of your book",
                    f"Above your {lim['holding']:g}% limit", [p["symbol"]], value=p["weight"])  # fmt: skip
    if on("sector"):
        groups: dict[str, list[dict]] = {}
        for p in held:
            if p.get("sector"):
                groups.setdefault(p["sector"], []).append(p)
        if groups:
            checked.add("sector")
        for sec, xs in groups.items():
            w = sum(x["weight"] for x in xs)
            if w > lim["sector"] and len(xs) > 1:
                add("sector", f"sector|{sec}", round(w - lim["sector"], 2),
                    f"{sec} is {_pct(w, lim['sector'])} of your book",
                    f"Above {lim['sector']:g}% · {_names(xs)}", [x["symbol"] for x in xs], value=w)  # fmt: skip
    if on("currency"):
        groups = {}
        for p in held:
            c = major_ccy(p.get("quote_ccy") or disp)
            if c != disp:
                groups.setdefault(c, []).append(p)
        checked.add(f"currency|in:{disp}")
        hl = page.get("headline") or {}
        cash_w, base = _num(hl.get("cash_weight")) or 0.0, page.get("base_ccy") or disp
        for c in sorted(set(groups) | ({base} if base != disp and cash_w > 0 else set())):
            xs = groups.get(c, [])
            w = sum(x["weight"] for x in xs) + (cash_w if c == base else 0.0)
            if w > lim["currency"]:
                who = _names(xs, extra="cash" if c == base and cash_w > 0 else None)
                # Keyed by the view too: which currencies fire depends on it.
                add("currency", f"currency|{c}|in:{disp}", round(w - lim["currency"], 2),
                    f"{_pct(w, lim['currency'])} of your book is in {c}", f"Above {lim['currency']:g}% · {who}",
                    [x["symbol"] for x in xs], value=w)  # fmt: skip
    for p in held:
        x = p.get("xray") or {}
        # A holding valued at its last trade price (no Yahoo price) can't say.
        r = _num(p.get("return_pct")) if not p.get("note") else None
        if on("from_cost") and r is not None:
            checked.add(f"from_cost|{p['symbol']}|in:{disp}")
        if on("from_cost") and r is not None and -r > lim["from_cost"]:
            add("from_cost", f"from_cost|{p['symbol']}|in:{disp}", round(-r - lim["from_cost"], 2),
                f"{p['name']} is {_pct(r, lim['from_cost'])} below what you paid",
                f"Your limit is {lim['from_cost']:g}%", [p["symbol"]], value=r)  # fmt: skip
        pk = _num(x.get("peak_pct"))
        if on("from_peak") and pk is not None:
            checked.add(f"from_peak|{p['symbol']}")
        if on("from_peak") and pk is not None and -pk > lim["from_peak"]:
            add("from_peak", f"from_peak|{p['symbol']}", round(-pk - lim["from_peak"], 2),
                f"{p['name']} is {_pct(pk, lim['from_peak'])} below its peak",
                f"Peak {_day(x['peak_date'])} · your limit is {lim['from_peak']:g}%", [p["symbol"]],
                value=pk)  # fmt: skip
        z, dp, dd = _num(x.get("day_z")), _num(x.get("day_pct")), x.get("day_date")
        fresh = isinstance(dd, str) and (today - date.fromisoformat(dd)).days <= MOVE_FRESH_DAYS
        if on("move") and z is not None:
            checked.add(f"move|{p['symbol']}")
        if on("move") and z is not None and dp is not None and fresh and abs(z) >= lim["move"]:
            add("move", f"move|{p['symbol']}|{dd}", 0.0,
                f"{p['name']} {'rose' if dp > 0 else 'fell'} {_pct(dp)} on {_day(dd)}",
                f"{abs(z):.1f}σ · its usual day moves {x['sigma_pct']:.1f}%", [p["symbol"]],
                value=z)  # fmt: skip
        if on("divergence") and _divergence(p, x, lim["divergence"], today, add):
            checked.add(f"divergence|{p['symbol']}")
    return out


_UP, _DOWN = {"bullish", "very_bullish"}, {"bearish", "very_bearish"}
_TIER = {"very_bullish": "very bullish", "bullish": "bullish", "bearish": "bearish",
         "very_bearish": "very bearish"}  # fmt: skip


def _divergence(p: dict, x: dict, limit: float, today: date, add) -> bool:
    """The News read leans one way while the week's price went the other by
    more than ``limit`` sigmas. Cache only: whatever the last News refresh
    scored, never a model call; a read older than its own window is ignored.
    True when there was a usable read to check (for ``prune``)."""
    news = ((p.get("news_sentiment") or {}).get("news")) or {}
    tier, at, wz, wp = (
        news.get("tier"),
        news.get("assessed_at"),
        _num(x.get("week_z")),
        _num(x.get("week_pct")),
    )
    if (
        not isinstance(tier, str)
        or not isinstance(at, str)
        or wz is None
        or wp is None
        or news.get("stale")
    ):
        return False
    try:
        read = datetime.fromisoformat(at)
        read = read if read.tzinfo else read.replace(tzinfo=UTC)
    except ValueError:
        return False
    age = (datetime.now(UTC) - read).total_seconds() / 86400
    if age > (news.get("lookback_days") or 7) + STALE_READ_SLACK:
        return False
    if (tier in _UP and wz <= -limit) or (tier in _DOWN and wz >= limit):
        ago = "today" if age < 1 else f"{int(age)} day{'s' if int(age) > 1 else ''} ago"
        add("divergence", f"divergence|{p['symbol']}|{at[:10]}", 0.0,
            f"{p['name']}: News read {_TIER[tier]}, price {'+' if wp > 0 else '−'}{_pct(wp)} this week",
            f"Read {ago} · its week usually moves {x.get('week_sigma_pct') or 0:.1f}%", [p["symbol"]],
            value=wz, ai=True)  # fmt: skip
    return True


def _upcoming(page: dict, rules: dict, events: dict, today: date, checked: set[str]) -> list[dict]:
    if not rules["events"]["on"]:
        return []
    until = today + timedelta(days=int(rules["events"]["value"]))
    out = []
    for p in page.get("positions") or []:
        ev = events.get(p.get("symbol")) or {}
        if ev:
            checked.update((f"earnings|{p['symbol']}", f"ex_div|{p['symbol']}"))
        for field, what in (("earnings", "earnings"), ("ex_div", "ex-dividend")):
            d = ev.get(field)
            try:
                when = date.fromisoformat(d) if isinstance(d, str) else None
            except ValueError:
                when = None
            if when and today <= when <= until:
                out.append({"key": f"{field}|{p['symbol']}|{d}", "level": 0.0, "name": p["name"],
                            "symbol": p["symbol"], "what": what, "date": d})  # fmt: skip
    return sorted(out, key=lambda u: (u["date"], u["name"]))


def evaluate(page: dict, state: dict | None = None, events: dict | None = None,
             today: date | None = None) -> dict:  # fmt: skip
    """The rail for one page payload: {high, info, upcoming, unread,
    unread_high, hidden, firing, checked, rules}. Pure: no I/O when ``state``
    and ``events`` are given. ``firing`` is every item's key before
    dismissals and ``checked`` what the page could check (for ``prune``)."""
    state = state if state is not None else load_state()
    events = events if events is not None else load_events()
    today = today or date.today()
    rules, dismissed, seen = state["rules"], state["dismissed"], state["seen"]
    checked: set[str] = set()
    items = _items(page, rules, today, checked) if page.get("positions") else []
    upcoming = _upcoming(page, rules, events, today, checked)
    shown = lambda it: _over(it, dismissed)  # noqa: E731
    visible = [it for it in items if shown(it)]
    order = {
        "move": 0,
        "holding": 1,
        "from_cost": 2,
        "sector": 3,
        "currency": 4,
        "from_peak": 5,
        "divergence": 6,
    }
    visible.sort(key=lambda it: (order[it["rule"]], -abs(it.get("value") or 0)))
    unread = [it for it in visible if _over(it, seen)]
    return {
        "high": [it for it in visible if it["severity"] == "high"],
        "info": [it for it in visible if it["severity"] == "info"],
        "upcoming": [u for u in upcoming if shown(u)],
        "unread": len(unread),
        "unread_high": any(it["severity"] == "high" for it in unread),
        "hidden": len(items) - len(visible),
        "firing": sorted({it["key"] for it in items} | {u["key"] for u in upcoming}),
        "checked": sorted(checked),
        "rules": [{"key": k, **{f: r[f] for f in ("label", "unit", "min", "max", "severity")},
                   "note": r.get("note"), "default": r["value"], **rules[k]} for k, r in RULES.items()],
    }  # fmt: skip


# ------------------------------------------------------------------ events
#
# Earnings and ex-dividend dates from Yahoo's calendar (``Ticker.calendar``,
# the same source as the detail panel's next earnings), one call per held
# share, a day at a time. A failed call keeps the last good dates and is
# retried after ``EVENTS_RETRY`` (never "no events" on an outage: the
# split-fetch lesson, Phase 4).

EVENTS_TTL = 24 * 3600
EVENTS_RETRY = 15 * 60
_EVENTS_LOCK = threading.Lock()  # single flight: one refresh at a time
_EVENTS_FILE_LOCK = threading.Lock()
_EVENTS_TRIED: dict[str, float] = {}
_FORCE_AGAIN: set[str] = (
    set()
)  # forced while a refresh ran: fetched before it ends  # symbol -> monotonic time of the last failed try


def _events_path():
    return paths.state_file("events")


def load_events() -> dict[str, dict]:
    """{symbol: {earnings, ex_div, at}}; a malformed file is {} (a cache)."""
    try:
        raw = json.loads(_events_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {s: v for s, v in raw.items() if isinstance(s, str) and isinstance(v, dict)}


def _iso(v) -> str | None:
    if isinstance(v, list | tuple):
        v = v[0] if v else None
    if v is None:
        return None
    try:
        import pandas as pd

        return pd.Timestamp(v).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def fetch_events(symbol: str) -> dict | None:
    """Yahoo's next earnings and ex-dividend dates, or None when the call
    failed (an empty calendar, a fund's, is an answer: both dates None)."""
    import yfinance as yf

    try:
        cal = yf.Ticker(symbol).calendar
    except Exception as exc:
        print(f"[attention] calendar {symbol} failed: {type(exc).__name__}")
        return None
    if cal is None:
        return None
    if not isinstance(cal, dict):
        cal = {}
    return {"earnings": _iso(cal.get("Earnings Date")), "ex_div": _iso(cal.get("Ex-Dividend Date"))}


def _due(symbols, cache: dict, force: bool) -> list[str]:
    now, wall = time.monotonic(), time.time()
    out = []
    for s in symbols:
        if now - _EVENTS_TRIED.get(s, -1e12) < EVENTS_RETRY:
            continue
        at = (cache.get(s) or {}).get("at")
        if force or not isinstance(at, int | float) or wall - at > EVENTS_TTL:
            out.append(s)
    return out


def refresh_events(symbols, *, force: bool = False, cancel=None, fetch=None) -> int:
    """Fetch the dates for every symbol due (older than a day, or all with
    ``force``); returns how many were fetched. Single flight: a second
    caller returns 0 at once, and a forced one leaves its symbols for the
    running refresh to fetch before it ends (adversarial review: the topbar
    Refresh during a page's background refresh used to be dropped)."""
    if not _EVENTS_LOCK.acquire(blocking=False):
        if force:
            _FORCE_AGAIN.update(symbols)
        return 0
    try:
        fetch = fetch or fetch_events  # looked up at call time (tests stub it)
        n = _refresh_locked(sorted(set(symbols)), force, cancel, fetch)
        while _FORCE_AGAIN and not (cancel is not None and cancel.is_set()):
            again = sorted(_FORCE_AGAIN)
            _FORCE_AGAIN.clear()
            n += _refresh_locked(again, True, cancel, fetch)
        return n
    finally:
        _EVENTS_LOCK.release()


def _refresh_locked(symbols: list[str], force: bool, cancel, fetch) -> int:
    cache0 = load_events()
    got = {}
    today = date.today().isoformat()
    for s in _due(symbols, cache0, force):
        if cancel is not None and cancel.is_set():
            break
        ev = fetch(s)
        # yfinance hides HTTP errors (429, 5xx) and hands back an empty
        # calendar, which looks like "no dates". Dates we already know that
        # are still ahead aren't dropped on that (adversarial review,
        # 2026-10-13): it counts as a failed call.
        known = [v for v in (cache0.get(s) or {}).values() if isinstance(v, str) and v >= today]
        if ev is not None and not any(ev.values()) and known:
            ev = None
        if ev is None:
            _EVENTS_TRIED[s] = time.monotonic()
            continue
        _EVENTS_TRIED.pop(s, None)
        got[s] = {**ev, "at": time.time()}
    if got:
        from convexity.persistence import _atomic_write

        with _EVENTS_FILE_LOCK:
            cache = load_events()
            cache.update(got)
            _atomic_write(_events_path(), json.dumps(cache, indent=1))
    return len(got)


def refresh_events_soon(symbols, *, force: bool = False) -> None:
    """``refresh_events`` in a background thread when anything is due (the
    page calls this after computing, the topbar Refresh with ``force``;
    neither waits on Yahoo's calendar, one call per share)."""
    symbols = sorted(set(symbols))
    if force and symbols and _EVENTS_LOCK.locked():
        _FORCE_AGAIN.update(symbols)  # the running refresh fetches these before it ends
        return
    if symbols and (force or _due(symbols, load_events(), False)) and not _EVENTS_LOCK.locked():
        threading.Thread(
            target=_refresh_quietly, args=(symbols, force), name="attention-events", daemon=True
        ).start()


def _refresh_quietly(symbols, force: bool = False) -> None:
    try:
        refresh_events(symbols, force=force)
    except Exception as exc:  # a cache: the page shows no dates rather than failing
        print(f"[attention] events refresh failed: {type(exc).__name__}")
