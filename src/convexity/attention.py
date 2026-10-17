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
was dismissed, seen or notified at which level), never the book: a malformed
file is read as the defaults. ``notified`` is Phase 9's: an item reaches a
macOS notification once, under the same "a full step worse" rule as seen.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import threading
import time
from datetime import UTC, date, datetime, timedelta

from convexity import paths
from convexity.helpers import finite_num, major_ccy

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
    # Phase 8: the owner's own plan (set per holding, from its row). Switches
    # only: the levels are the plan's. A target or review level reached is
    # the plan asking for a decision, so it needs a look; a plain price
    # alert stays information (Appendix D, *Alert for Alerts*).
    "review": {"label": "A holding falls below your review level", "unit": None, "value": None,
               "step": 10.0, "severity": "high", "note": "Set in a holding's plan: click its row."},
    "target": {"label": "A holding reaches your target", "unit": None, "value": None,
               "step": 0.0, "severity": "high", "note": "Set in a holding's plan: click its row."},
    "price_alert": {"label": "Price alerts", "unit": None, "value": None, "step": 0.0,
                    "severity": "info", "note": "Each fires once. Set in a holding's plan."},
}  # fmt: skip

# A day move is news only while it is the last trading day's: a Friday move
# still shows on Monday, last week's doesn't.
MOVE_FRESH_DAYS = 4
STALE_READ_SLACK = 1.0  # days past its own window before a News read is too old

_LOCK = threading.Lock()


def _state_path():
    return paths.state_file("attention")


def _clean_rules(raw) -> dict:
    """{rule: {on, value}} for every rule, the defaults filling any gap;
    a value out of range is clamped, anything unknown dropped."""
    out = {}
    raw = raw if isinstance(raw, dict) else {}
    for k, r in RULES.items():
        got = raw.get(k) if isinstance(raw.get(k), dict) else {}
        v = (
            finite_num(got.get("value")) if r["value"] is not None else None
        )  # a switch has no value
        on = got.get("on")
        out[k] = {"on": on if isinstance(on, bool) else True,
                  "value": min(max(v, r["min"]), r["max"]) if v is not None else r["value"]}  # fmt: skip
    return out


def _clean_levels(raw) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    nums = {k[:200]: finite_num(v) for k, v in raw.items() if isinstance(k, str)}
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
            "seen": _clean_levels(raw.get("seen")), "snoozed": _clean_snoozed(raw.get("snoozed")),
            "notified": _clean_levels(raw.get("notified")), "armed": raw.get("armed") is True}  # fmt: skip


def _clean_snoozed(raw) -> dict[str, str]:
    """{key: "yyyy-mm-dd"}: the item stays hidden until that day."""
    if not isinstance(raw, dict):
        return {}
    return {k[:200]: v for k, v in raw.items() if isinstance(k, str) and _is_day(v)}


def _is_day(v) -> bool:
    try:
        return isinstance(v, str) and len(v) == 10 and bool(date.fromisoformat(v))
    except ValueError:
        return False


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
        if "value" in c and (RULES[k]["value"] is None or finite_num(c["value"]) is None):
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
    key, lvl = _check_key(key), finite_num(level)
    if lvl is None:
        raise ValueError("level must be a number")

    def change(st):
        st["dismissed"].pop(key, None)
        st["dismissed"][key] = lvl
        st["dismissed"] = _cap(st["dismissed"])

    _update(change)


SNOOZE_MAX_DAYS = 400


def snooze(key, until, today: date | None = None) -> None:
    """Hide an item until the day ``until`` (Phase 8: a day, a week, or the
    next earnings date; the page works the day out). Past snoozes go."""
    key, today = _check_key(key), today or date.today()
    if not _is_day(until):
        raise ValueError("until must be a date (yyyy-mm-dd)")
    days = (date.fromisoformat(until) - today).days
    if not 0 < days <= SNOOZE_MAX_DAYS:
        raise ValueError(f"until must be within {SNOOZE_MAX_DAYS} days from today")

    def change(st):
        st["snoozed"] = {
            k: v for k, v in st["snoozed"].items() if v > today.isoformat() and k != key
        }
        st["snoozed"][key] = until
        st["snoozed"] = _cap(st["snoozed"])

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
        if not isinstance(it, list | tuple) or len(it) != 2 or finite_num(it[1]) is None:
            raise ValueError("each item is [key, level]")
        pairs.append((_check_key(it[0]), finite_num(it[1])))

    def change(st):
        for k, lvl in pairs:
            st["seen"].pop(k, None)  # re-inserted last: the cap drops the oldest
            st["seen"][k] = lvl
        st["seen"] = _cap(st["seen"])

    if pairs:
        _update(change)


def mark_notified(items) -> None:
    """[[key, level], ...]: the items a notification has now gone out for
    (Phase 9). Same cap and pruning as seen: one that clears and fires
    again is new, one a full step worse notifies again."""
    pairs = [(_check_key(k), lvl) for k, lvl in items if finite_num(lvl) is not None]

    def change(st):
        for k, lvl in pairs:
            st["notified"].pop(k, None)
            st["notified"][k] = float(lvl)
        st["notified"] = _cap(st["notified"])
        st["armed"] = True

    _update(change)


def disarm() -> None:
    """Notifications switched off: the next switch-on takes what is in the
    rail then as its baseline, rather than sending all of it at once."""
    _update(lambda st: st.update(armed=False))


def should_notify(att: dict, state: dict | None = None) -> list[dict]:
    """The Needs-a-look items a notification is due for: shown in the rail
    (so not dismissed or snoozed), unread, and not notified at this level
    (or a full step worse since). Plain price alerts are information, so
    never here (Appendix D)."""
    state = state if state is not None else load_state()
    return [it for it in att.get("high") or [] if it.get("unread") and _over(it, state["notified"])]


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

    today = date.today().isoformat()
    with _LOCK:
        st = load_state()
        live = {
            k: v for k, v in st["snoozed"].items() if v > today
        }  # a snooze past its day is gone
        if all(keep(k) for k in (*st["dismissed"], *st["seen"], *st["notified"])) and len(
            live
        ) == len(st["snoozed"]):
            return
        st["dismissed"] = {k: v for k, v in st["dismissed"].items() if keep(k)}
        st["seen"] = {k: v for k, v in st["seen"].items() if keep(k)}
        st["notified"] = {k: v for k, v in st["notified"].items() if keep(k)}
        st["snoozed"] = live
        _save_state(st)


# ------------------------------------------------------------------ plans
#
# The owner's plan per holding (roadmap Phase 8): a target, a review level,
# a maximum weight, one line on why they hold it, and plain price alerts.
# ``state/plans.json`` is the owner's own input, so unlike ``attention.json``
# a malformed file is never read as "no plans" and never overwritten (the
# book's rule). It is not in the book file: a plan edit would bump the book's
# rev, which throws away the cached page and recomputes the book.
#
# Levels are in the share's quote unit (pence for LSE), like the page's
# Price column, and compared with the page's own last price: no fetch.

PLANS_VERSION = 1
WHY_MAX = 140
ALERTS_MAX = 10
_PLANS_LOCK = threading.Lock()
_PLAN_FIELDS = ("target", "review", "max_weight", "why")


class PlansUnreadable(ValueError):
    """``plans.json`` exists but can't be read: nothing is written over it."""


def _plans_path():
    return paths.state_file("plans")


def _clean_alert(a) -> dict | None:
    if (
        not isinstance(a, dict)
        or not isinstance(a.get("id"), str)
        or a.get("side") not in ("above", "below")
    ):
        return None
    lvl = finite_num(a.get("level"))
    if lvl is None or lvl <= 0:
        return None
    out = {"id": a["id"][:16], "level": lvl, "side": a["side"],
           "set_on": a["set_on"] if _is_day(a.get("set_on")) else None}  # fmt: skip
    if _is_day(a.get("levels_on")):
        out["levels_on"] = a["levels_on"]
    if _is_day(a.get("fired_on")) and finite_num(a.get("fired_price")) is not None:
        out.update(fired_on=a["fired_on"], fired_price=finite_num(a["fired_price"]))
    return out


def _clean_plan(v: dict) -> dict:
    out: dict = {}
    for k in ("target", "review", "set_price"):
        x = finite_num(v.get(k))
        if x is not None and x > 0:
            out[k] = x
    mw = finite_num(v.get("max_weight"))
    if mw is not None and 0 < mw <= 100:
        out["max_weight"] = mw
    if isinstance(v.get("why"), str) and v["why"].strip():
        out["why"] = v["why"].strip()[:WHY_MAX]
    for k in ("set_on", "levels_on"):
        if _is_day(v.get(k)):
            out[k] = v[k]
    alerts = (
        [a for a in map(_clean_alert, v.get("alerts") or []) if a]
        if isinstance(v.get("alerts"), list)
        else []
    )
    if alerts:
        out["alerts"] = alerts[:ALERTS_MAX]
    return out


def _has_plan(p: dict) -> bool:
    return any(k in p for k in (*_PLAN_FIELDS, "alerts"))


def load_plans() -> dict[str, dict]:
    """{symbol: plan}; no file is no plans. Raises ``PlansUnreadable`` on a
    file that isn't a plans file."""
    return _clean_plans(_read_plans())


def _clean_plans(raw: dict) -> dict[str, dict]:
    out = {}
    for sym, v in raw["plans"].items():
        if isinstance(sym, str) and isinstance(v, dict):
            plan = _clean_plan(v)
            if _has_plan(plan):
                out[sym[:32]] = plan
    return out


def _read_plans() -> dict:
    """The file as written, checked: a plans file of this version or older.
    A newer one (written by a later app) is refused, never rewritten."""
    try:
        text = _plans_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": PLANS_VERSION, "plans": {}}
    except OSError as exc:
        raise PlansUnreadable(f"plans.json can't be read ({type(exc).__name__})") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise PlansUnreadable("plans.json is damaged: it was left as it is") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("plans"), dict):
        raise PlansUnreadable("plans.json is not a plans file: it was left as it is")
    v = raw.get("version", PLANS_VERSION)
    if not isinstance(v, int) or v > PLANS_VERSION:
        raise PlansUnreadable("plans.json was written by a newer Convexity: it was left as it is")
    return raw


def _change_plans(change) -> dict:
    """Read, change and write ``plans.json`` under the lock; returns the plans."""
    from convexity.persistence import _atomic_write

    with _PLANS_LOCK:
        raw = _read_plans()  # raises on a damaged file: never overwritten
        before = _clean_plans(raw)
        plans = json.loads(json.dumps(before))  # change() edits a copy
        change(plans)
        plans = {s: p for s, p in plans.items() if _has_plan(p)}
        # The owner's own words: a holding this change didn't touch is
        # written back exactly as it was, fields this version doesn't know
        # included (adversarial review, 2026-10-14: a fired alert used to
        # rewrite the whole file through the cleaner).
        kept = {s: v for s, v in raw["plans"].items()
                if isinstance(s, str) and s in plans and before.get(s) == plans[s]}  # fmt: skip
        body = {**raw, "version": raw.get("version", PLANS_VERSION),
                "plans": {s: kept.get(s, p) for s, p in plans.items()}}  # fmt: skip
        _atomic_write(_plans_path(), json.dumps(body, ensure_ascii=False, indent=1))
        return plans


def _check_symbol(symbol) -> str:
    if not isinstance(symbol, str) or not symbol or len(symbol) > 32:
        raise ValueError("symbol required")
    return symbol


def _price_arg(price) -> float:
    v = finite_num(price)
    if v is None or v <= 0:
        raise ValueError("price must be a positive number")
    return v


def set_plan(symbol, fields, price, today: date | None = None) -> dict:
    """Merge {target?, review?, max_weight?, why?} into a holding's plan
    (``None`` clears one). A new target or review level records the day and
    today's price, for "set 3 Mar at 180". Raises ValueError on bad input."""
    symbol, today = _check_symbol(symbol), today or date.today()
    if not isinstance(fields, dict) or not fields or set(fields) - set(_PLAN_FIELDS):
        raise ValueError(f"plan fields are {', '.join(_PLAN_FIELDS)}")
    new: dict = {}
    for k in ("target", "review"):
        if k in fields and fields[k] is not None:
            v = finite_num(fields[k])
            if v is None or v <= 0:
                raise ValueError(f"{k} must be a positive price")
            new[k] = v
    if fields.get("max_weight") is not None:
        mw = finite_num(fields["max_weight"])
        if mw is None or not 0 < mw <= 100:
            raise ValueError("max weight must be between 0 and 100%")
        new["max_weight"] = mw
    if fields.get("why") is not None:
        if not isinstance(fields["why"], str):
            raise ValueError("why must be text")
        if fields["why"].strip():
            new["why"] = fields["why"].strip()[:WHY_MAX]
    levels_changed = False

    def change(plans):
        nonlocal levels_changed
        plan = dict(plans.get(symbol) or {})
        for k in _PLAN_FIELDS:
            if k in fields:
                if plan.get(k) != new.get(k) and k in ("target", "review"):
                    levels_changed = True
                plan.pop(k, None)
                if k in new:
                    plan[k] = new[k]
        t, r = plan.get("target"), plan.get("review")
        if t is not None and r is not None and r >= t:
            raise ValueError("the review level must be below the target")
        if levels_changed:
            plan.update(set_on=today.isoformat(), set_price=_price_arg(price))
            plan.pop("levels_on", None)  # typed today: in today's share terms
        if "target" not in plan and "review" not in plan:
            plan.pop("set_on", None)
            plan.pop("set_price", None)
        plans[symbol] = plan

    return _change_plans(change).get(symbol, {})


def clear_plan(symbol) -> None:
    """Clear target, review level, max weight and why; price alerts stay
    (each has its own ✕)."""
    symbol = _check_symbol(symbol)

    def change(plans):
        plan = plans.get(symbol) or {}
        plans[symbol] = {"alerts": plan["alerts"]} if plan.get("alerts") else {}

    _change_plans(change)


def add_alert(symbol, level, price, today: date | None = None) -> dict:
    """A one-shot alert at ``level``. Its side is fixed now, against today's
    price, so a crossing can't be read both ways."""
    symbol, today = _check_symbol(symbol), today or date.today()
    lvl, now = finite_num(level), _price_arg(price)
    if lvl is None or lvl <= 0:
        raise ValueError("the alert price must be a positive number")
    if abs(lvl / now - 1) < 1e-6:
        raise ValueError("that is today's price: pick a level above or below it")
    alert = {"id": secrets.token_hex(4), "level": lvl, "side": "above" if lvl > now else "below",
             "set_on": today.isoformat()}  # fmt: skip

    def change(plans):
        plan = plans.setdefault(symbol, {})
        if len(plan.get("alerts") or []) >= ALERTS_MAX:
            raise ValueError(f"at most {ALERTS_MAX} price alerts per holding")
        plan["alerts"] = [*(plan.get("alerts") or []), alert]

    _change_plans(change)
    return alert


def remove_alert(symbol, alert_id) -> None:
    symbol = _check_symbol(symbol)
    if not isinstance(alert_id, str) or not alert_id:
        raise ValueError("alert id required")

    def change(plans):
        plan = plans.get(symbol) or {}
        plan["alerts"] = [a for a in plan.get("alerts") or [] if a["id"] != alert_id]
        if not plan["alerts"]:
            plan.pop("alerts")

    _change_plans(change)


def record_fired(fired) -> None:
    """[[symbol, id, price, day], ...]: alerts that crossed, kept as fired
    so each shows once, even if the price goes back (owner's pick)."""
    fired = [f for f in fired or [] if isinstance(f, list | tuple) and len(f) == 4]
    if not fired:
        return

    def change(plans):
        for sym, aid, price, day in fired:
            for a in (plans.get(sym) or {}).get("alerts") or []:
                if a["id"] == aid and "fired_on" not in a:
                    a.update(fired_on=day, fired_price=round(price, 6))

    with contextlib.suppress(
        PlansUnreadable
    ):  # shown again next time; the damaged file is left alone
        _change_plans(change)


def split_adjusted(plans: dict, splits: dict) -> tuple[dict, bool]:
    """The plans with every level in today's share terms: a 10:1 split after
    a level was set divides it by 10, as it divides the price (a review
    level at 160 would otherwise read "fell below" the morning after).
    ``splits`` is ``investments.market_splits``: {symbol: [(day, ratio)]}.
    A split on the day a level was set is taken as already in it. Pure;
    returns (plans, whether anything moved)."""
    moved = False
    out = {}
    for sym, plan in plans.items():
        sp = [(d, r) for d, r in splits.get(sym) or () if r and r > 0 and r != 1]
        if not sp:
            out[sym] = plan
            continue

        def factor(since):
            f = 1.0
            for d, r in sp:
                if since and d > since:
                    f *= r
            return f

        plan = dict(plan)
        f = factor(plan.get("levels_on") or plan.get("set_on"))
        if f != 1.0:
            moved = True
            for k in ("target", "review", "set_price"):
                if k in plan:
                    plan[k] = plan[k] / f
            plan["levels_on"] = max(d for d, _ in sp)
        alerts = []
        for a in plan.get("alerts") or []:
            fa = factor(a.get("levels_on") or a.get("set_on"))
            if fa != 1.0:
                moved = True
                a = {**a, "level": a["level"] / fa, "levels_on": max(d for d, _ in sp)}
                if "fired_price" in a:
                    a["fired_price"] = a["fired_price"] / fa
            alerts.append(a)
        if alerts:
            plan["alerts"] = alerts
        out[sym] = plan
    return out, moved


def apply_splits(splits: dict) -> None:
    """Save ``split_adjusted`` once (on a fresh page), so a level is never
    divided twice; a damaged file is left alone."""

    def change(plans):
        adj, _ = split_adjusted(plans, splits)
        plans.clear()
        plans.update(adj)

    with contextlib.suppress(PlansUnreadable):
        if split_adjusted(load_plans(), splits)[1]:
            _change_plans(change)


def _lvl(v: float) -> str:
    """A level inside a key: exact to 1e-6, so a changed target is a new key
    (``:g`` kept six digits: 12345.67 and 12345.71 were one key)."""
    return f"{round(v, 6):.15g}"


def _quote(v: float, qccy: str | None) -> str:
    """A level in the share's quote unit: "2,450p", "210", "212.40"."""
    if qccy == "GBp":
        return f"{v:,.0f}p"
    return f"{v:,.0f}" if abs(v - round(v)) < 0.005 else f"{v:,.2f}"


def _plan_items(held: list[dict], plans: dict, on, today: date, checked: set[str], add) -> list:
    """Target, review level and price-alert items; returns the alerts that
    crossed for the first time ([symbol, id, price, day])."""
    fired = []
    for p in held:
        plan, sym = plans.get(p["symbol"]), p["symbol"]
        price = finite_num(p.get("price"))
        if not plan or price is None or p.get("note"):  # no Yahoo price: can't say
            continue
        q, name = p.get("quote_ccy"), p["name"]
        day = (p.get("xray") or {}).get("day_date") or today.isoformat()
        set_at = (f"Set {_day(plan['set_on'])} at {_quote(plan['set_price'], q)} · "
                  if plan.get("set_on") and plan.get("set_price") else "")  # fmt: skip
        why = f"\u201c{plan['why']}\u201d" if plan.get("why") else None
        t, r = plan.get("target"), plan.get("review")
        if on("target") and t:
            checked.add(f"target|{sym}")
            if price >= t:
                add("target", f"target|{sym}|{_lvl(t)}", 0.0, f"{name} reached your {_quote(t, q)} target",
                    f"{set_at}now {_quote(price, q)}", [sym], value=(price / t - 1) * 100, plan=True)  # fmt: skip
        if on("review") and r:
            checked.add(f"review|{sym}")
            if price <= r:
                add("review", f"review|{sym}|{_lvl(r)}", round((1 - price / r) * 100, 2),
                    f"{name} fell below your {_quote(r, q)} review level",
                    why or f"{set_at}now {_quote(price, q)}", [sym], value=-(1 - price / r) * 100,
                    plan=True)  # fmt: skip
        if on("price_alert") and plan.get("alerts"):
            checked.add(f"price|{sym}")
            for a in plan["alerts"]:
                up = a["side"] == "above"
                crossed = price >= a["level"] if up else price <= a["level"]
                if not crossed and "fired_on" not in a:
                    continue
                if "fired_on" not in a:
                    fired.append([sym, a["id"], price, day])
                on_day = a.get("fired_on") or day
                add("price_alert", f"price|{sym}|{a['id']}", 0.0, f"{name} crossed {_quote(a['level'], q)}",
                    f"Price alert · {'rose' if up else 'fell'} through it on {_day(on_day)}", [sym],
                    alert_id=a["id"], plan=True)  # fmt: skip
    return fired


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


def _items(page: dict, rules: dict, today: date, checked: set[str], plans: dict | None = None,
           fired: list | None = None) -> list[dict]:  # fmt: skip
    held = [
        p
        for p in page.get("positions") or []
        if not p.get("pending") and finite_num(p.get("weight"))
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
    plans = plans or {}
    checked.update(f"holding|{p['symbol']}" for p in held if on("holding") or
                   (plans.get(p["symbol"]) or {}).get("max_weight") is not None)  # fmt: skip
    for p in held:
        # A holding's own max weight (its plan) replaces the book's limit,
        # funds included: the owner set it for that one (Phase 8). It is part
        # of the plan, so it holds with the book rule switched off, and its
        # key carries the cap: a tighter cap is a new item, never hidden by a
        # dismissal made under the old one (adversarial review, 2026-10-14).
        own = (plans.get(p["symbol"]) or {}).get("max_weight")
        if own is None and not on("holding"):
            continue
        cap = own if own is not None else lim["holding"]
        key = f"holding|{p['symbol']}" + (f"|max{_lvl(own)}" if own is not None else "")
        # A fund is diversified already: 30% in a world ETF isn't one bet.
        if p["weight"] > cap and (own is not None or not p.get("fund")):
            add("holding", key, round(p["weight"] - cap, 2),
                f"{p['name']} is {_pct(p['weight'], cap)} of your book",
                f"Above your {cap:g}% {'plan ' if own is not None else ''}limit", [p["symbol"]],
                value=p["weight"], plan=own is not None)  # fmt: skip
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
        cash_w, base = finite_num(hl.get("cash_weight")) or 0.0, page.get("base_ccy") or disp
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
        r = finite_num(p.get("return_pct")) if not p.get("note") else None
        if on("from_cost") and r is not None:
            checked.add(f"from_cost|{p['symbol']}|in:{disp}")
        if on("from_cost") and r is not None and -r > lim["from_cost"]:
            add("from_cost", f"from_cost|{p['symbol']}|in:{disp}", round(-r - lim["from_cost"], 2),
                f"{p['name']} is {_pct(r, lim['from_cost'])} below what you paid",
                f"Your limit is {lim['from_cost']:g}%", [p["symbol"]], value=r)  # fmt: skip
        pk = finite_num(x.get("peak_pct"))
        if on("from_peak") and pk is not None:
            checked.add(f"from_peak|{p['symbol']}")
        if on("from_peak") and pk is not None and -pk > lim["from_peak"]:
            add("from_peak", f"from_peak|{p['symbol']}", round(-pk - lim["from_peak"], 2),
                f"{p['name']} is {_pct(pk, lim['from_peak'])} below its peak",
                f"Peak {_day(x['peak_date'])} · your limit is {lim['from_peak']:g}%", [p["symbol"]],
                value=pk)  # fmt: skip
        z, dp, dd = finite_num(x.get("day_z")), finite_num(x.get("day_pct")), x.get("day_date")
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
    got = _plan_items(held, plans, on, today, checked, add)
    if fired is not None:
        fired.extend(got)
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
        finite_num(x.get("week_z")),
        finite_num(x.get("week_pct")),
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
             today: date | None = None, plans: dict | None = None) -> dict:  # fmt: skip
    """The rail for one page payload: {high, info, upcoming, unread,
    unread_high, hidden, firing, checked, rules, plans, fired}. Pure: no I/O
    when ``state``, ``events`` and ``plans`` are given. ``firing`` is every
    item's key before dismissals and ``checked`` what the page could check
    (for ``prune``); ``fired`` the price alerts that crossed for the first
    time (for ``record_fired``)."""
    state = state if state is not None else load_state()
    events = events if events is not None else load_events()
    today = today or date.today()
    plans_error = None
    if plans is None:
        try:
            plans = load_plans()
        except PlansUnreadable as exc:
            plans, plans_error = {}, str(exc)
    rules, dismissed, seen = state["rules"], state["dismissed"], state["seen"]
    snoozed = {k for k, until in state.get("snoozed", {}).items() if until > today.isoformat()}
    checked: set[str] = set()
    fired: list = []
    items = _items(page, rules, today, checked, plans, fired) if page.get("positions") else []
    upcoming = _upcoming(page, rules, events, today, checked)
    for it in items:  # "Snooze until next earnings" needs the date (events cache)
        e = (
            (events.get(it["symbols"][0]) or {}).get("earnings")
            if it.get("plan") and it["symbols"]
            else None
        )
        if _is_day(e) and e > today.isoformat():
            it["earnings"] = e
    shown = lambda it: it["key"] not in snoozed and _over(it, dismissed)  # noqa: E731
    visible = [it for it in items if shown(it)]
    order = {
        "review": -2,
        "target": -1,
        "move": 0,
        "holding": 1,
        "from_cost": 2,
        "sector": 3,
        "currency": 4,
        "from_peak": 5,
        "divergence": 6,
        "price_alert": 7,
    }
    visible.sort(key=lambda it: (order[it["rule"]], -abs(it.get("value") or 0)))
    unread = [it for it in visible if _over(it, seen)]
    for it in unread:  # Phase 9: only an unread item is notified
        it["unread"] = True
    return {
        "high": [it for it in visible if it["severity"] == "high"],
        "info": [it for it in visible if it["severity"] == "info"],
        "upcoming": [u for u in upcoming if shown(u)],
        "unread": len(unread),
        "unread_high": any(it["severity"] == "high" for it in unread),
        "unread_high_n": sum(1 for it in unread if it["severity"] == "high"),
        "hidden": len(items) - len(visible),
        "firing": sorted({it["key"] for it in items} | {u["key"] for u in upcoming}),
        "checked": sorted(checked),
        "rules": [{"key": k, **{f: r.get(f) for f in ("label", "unit", "min", "max", "severity")},
                   "note": r.get("note"), "default": r["value"], **rules[k]} for k, r in RULES.items()],
        # The held holdings' plans, for the row popover and the Price track.
        "plans": {p["symbol"]: plans[p["symbol"]] for p in page.get("positions") or [] if p.get("symbol") in plans},
        "plans_error": plans_error,
        "snoozed": sum(1 for it in items if it["key"] in snoozed),
        "fired": fired,
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
