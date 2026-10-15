"""My Investments notifications (notify.py, roadmap Phase 9): the schedule,
the rail's own dedup (once, seen, dismissed, snoozed), the cap, quiet hours,
the switch, the degraded guard, cache-first fetching, and the weekly recap.
Public tickers, synthetic numbers, no network."""

import threading
import time
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from convexity import attention, cache, jobs, notify
from convexity import investments as inv
from convexity.persistence import load_prefs, save_prefs

TODAY = date(2026, 10, 13)  # a Tuesday
QUIET_OFF = {"notify_on": True, "quiet_on": False, "quiet_from": "22:00", "quiet_to": "08:00"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    sent: list[dict] = []
    notify._OUT.clear()
    notify._HELD.update(items=False, recap=None)
    notify._STATE.update(last=None, quiet=False)
    notify.add_sink(sent.append)
    jobs.__dict__["_CURRENT"] = None
    yield sent
    notify.remove_sink(sent.append)
    jobs.__dict__["_CURRENT"] = None


def _pos(sym, weight, **kw):
    return {"symbol": sym, "name": sym.title(), "weight": weight, "quote_ccy": "USD",
            "return_pct": 5.0, "sector": None, "price": 100.0, "xray": {}, **kw}  # fmt: skip


def _page(positions):
    return {
        "ccy": "USD",
        "base_ccy": "USD",
        "positions": positions,
        "headline": {"cash_weight": 0.0},
    }


def _att(positions):
    return attention.evaluate(_page(positions), events={}, today=TODAY, plans={})


def _armed():
    attention.mark_notified([])  # arms with nothing: what follows is new


# ---------------------------------------------------------------- schedule


def _utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_due_in_us_session_every_30_minutes():
    now = _utc(2026, 10, 13, 15, 0)  # 11:00 New York
    assert notify.due(now, ["AAPL"], None) == "session"
    assert notify.due(now, ["AAPL"], now - timedelta(minutes=10)) is None
    assert notify.due(now, ["AAPL"], now - timedelta(minutes=31)) == "session"


def test_due_once_after_the_close_and_never_at_night_or_weekends():
    after = _utc(2026, 10, 13, 20, 25)  # 16:25 New York, the close + 25 min
    assert notify.due(after, ["AAPL"], _utc(2026, 10, 13, 19, 50)) == "after-close"
    assert notify.due(after, ["AAPL"], after - timedelta(minutes=2)) is None  # already done
    assert notify.due(_utc(2026, 10, 14, 3, 0), ["AAPL"], None) is None  # 23:00 New York
    assert notify.due(_utc(2026, 10, 17, 15, 0), ["AAPL"], None) is None  # Saturday
    assert notify.due(_utc(2026, 10, 13, 15, 0), [], None) is None  # nothing held


def test_due_follows_each_listing_market():
    london_morning = _utc(2026, 10, 13, 9, 0)  # 10:00 London, 05:00 New York
    assert notify.due(london_morning, ["SHEL.L"], None) == "session"
    assert notify.due(london_morning, ["AAPL"], None) is None


def test_quiet_hours_wrap_midnight():
    s = {"quiet_on": True, "quiet_from": "22:00", "quiet_to": "08:00"}
    at = lambda h, m=0: datetime(2026, 10, 13, h, m)  # noqa: E731
    assert notify.in_quiet(at(23), s) and notify.in_quiet(at(7, 59), s)
    assert not notify.in_quiet(at(8), s) and not notify.in_quiet(at(21, 59), s)
    assert not notify.in_quiet(at(23), {**s, "quiet_on": False})
    assert notify.in_quiet(at(13), {**s, "quiet_from": "12:00", "quiet_to": "14:00"})


# ----------------------------------------------------------------- dedup


def test_first_pass_is_a_baseline_not_a_burst(_clean):
    att = _att([_pos("NVDA", 24.0)])
    assert notify.notify_rail(att, s=QUIET_OFF) == 0 and _clean == []
    assert attention.load_state()["armed"]
    assert notify.notify_rail(_att([_pos("NVDA", 24.0)]), s=QUIET_OFF) == 0  # still the same item


def test_an_item_notifies_once(_clean):
    _armed()
    assert notify.notify_rail(_att([_pos("NVDA", 24.0)]), s=QUIET_OFF) == 1
    assert [m["title"] for m in _clean] == ["Nvda is 24% of your book"]
    assert _clean[0]["key"] == "holding|NVDA" and _clean[0]["subtitle"] == "Needs a look"
    assert notify.notify_rail(_att([_pos("NVDA", 24.5)]), s=QUIET_OFF) == 0
    # A full step worse (5 points of weight): once more.
    assert notify.notify_rail(_att([_pos("NVDA", 29.5)]), s=QUIET_OFF) == 1


def test_seen_snoozed_and_dismissed_never_notify(_clean):
    _armed()
    attention.mark_seen([["holding|NVDA", 9.0]])
    attention.snooze("holding|MSFT", (date.today() + timedelta(days=3)).isoformat())
    attention.dismiss("holding|AAPL", 9.0)
    ps = [_pos("NVDA", 24.0), _pos("MSFT", 24.0), _pos("AAPL", 24.0)]
    assert notify.notify_rail(_att(ps), s=QUIET_OFF) == 0 and _clean == []
    # The dismissed one comes back a full step worse: shown, unread, sent.
    ps[2] = _pos("AAPL", 29.0)
    assert notify.notify_rail(_att(ps), s=QUIET_OFF) == 1
    assert _clean[0]["key"] == "holding|AAPL"


def test_information_items_stay_in_app(_clean):
    _armed()
    peak = _pos("NVDA", 10.0, xray={"peak_pct": -40.0, "peak_date": "2026-09-01"})
    att = _att([peak])
    assert att["info"] and not att["high"]  # down from peak is information
    assert notify.notify_rail(att, s=QUIET_OFF) == 0 and _clean == []


def test_plan_targets_notify(_clean):
    _armed()
    plans = {"NVDA": {"target": 90.0, "set_on": "2026-03-03", "set_price": 80.0}}
    att = attention.evaluate(_page([_pos("NVDA", 10.0)]), events={}, today=TODAY, plans=plans)
    assert notify.notify_rail(att, s=QUIET_OFF) == 1
    assert _clean[0]["key"].startswith("target|NVDA")


def test_more_than_three_fold_into_one(_clean):
    _armed()
    ps = [_pos(s, 20.0 + i) for i, s in enumerate(["AAPL", "MSFT", "NVDA", "KO", "JNJ"])]
    assert notify.notify_rail(_att(ps), s=QUIET_OFF) == 5
    assert [m["kind"] for m in _clean] == ["item", "item", "item", "more"]
    assert _clean[-1]["title"] == "+2 more need a look" and _clean[-1]["key"] == notify.RAIL_KEY


def test_quiet_hours_hold_then_one_summary(_clean, monkeypatch):
    _armed()
    quiet = {**QUIET_OFF, "quiet_on": True, "quiet_from": "00:00", "quiet_to": "23:59"}
    ps = [_pos("NVDA", 24.0), _pos("AAPL", 24.0)]
    assert notify.notify_rail(_att(ps), s=quiet) == 0 and _clean == []
    assert attention.load_state()["notified"] == {}  # nothing recorded: they wait, unread
    out = {**_page(ps), "series": {"dates": ["2026-10-13"]}, "attention": _att(ps)}
    monkeypatch.setattr(inv, "computed_page", lambda ccy=None: out)
    notify._quiet_ended(datetime.now(UTC), QUIET_OFF)
    assert [m["kind"] for m in _clean] == ["summary"]
    assert _clean[0]["title"] == "2 items came up overnight"


def test_items_read_overnight_leave_no_summary_for_later(_clean, monkeypatch):
    """Held items the owner read before quiet hours ended: nothing at their
    end, and the afternoon's items go out one by one, not as "overnight"."""
    _armed()
    quiet = {**QUIET_OFF, "quiet_on": True, "quiet_from": "00:00", "quiet_to": "23:59"}
    ps = [_pos("NVDA", 24.0), _pos("AAPL", 24.0)]
    notify.notify_rail(_att(ps), s=quiet)
    attention.mark_seen([["holding|NVDA", 9.0], ["holding|AAPL", 9.0]])
    out = {**_page(ps), "series": {"dates": ["2026-10-13"]}, "attention": _att(ps)}
    monkeypatch.setattr(inv, "computed_page", lambda ccy=None: out)
    notify._quiet_ended(datetime.now(UTC), QUIET_OFF)
    assert _clean == []
    assert notify.notify_rail(_att([_pos("MSFT", 24.0), _pos("KO", 24.0)]), s=QUIET_OFF) == 2
    assert [m["kind"] for m in _clean] == ["item", "item"]


def test_quiet_end_never_sends_from_a_degraded_page(_clean, monkeypatch):
    _armed()
    quiet = {**QUIET_OFF, "quiet_on": True, "quiet_from": "00:00", "quiet_to": "23:59"}
    ps = [_pos("NVDA", 24.0), _pos("AAPL", 24.0)]
    notify.notify_rail(_att(ps), s=quiet)
    bad = [_pos("NVDA", 24.0), _pos("AAPL", 24.0, note="valued at the last trade price")]
    out = {**_page(bad), "series": {"dates": ["2026-10-13"]}, "attention": _att(bad)}
    monkeypatch.setattr(inv, "computed_page", lambda ccy=None: out)
    notify._quiet_ended(datetime.now(UTC), QUIET_OFF)
    assert _clean == [] and attention.load_state()["notified"] == {}


def test_two_passes_at_once_send_an_item_once(_clean):
    """The job's pass and the quiet-hours end (another thread) at once."""
    _armed()
    inside, release = threading.Event(), threading.Event()

    def slow(msg):
        inside.set()
        release.wait(2)

    notify.add_sink(slow)
    try:
        a = threading.Thread(target=notify.notify_rail, args=(_att([_pos("NVDA", 24.0)]),),
                             kwargs={"s": QUIET_OFF})  # fmt: skip
        a.start()
        assert inside.wait(2)
        b = threading.Thread(target=notify.notify_rail, args=(_att([_pos("NVDA", 24.0)]),),
                             kwargs={"s": QUIET_OFF})  # fmt: skip
        b.start()
        b.join(0.3)
        release.set()
        a.join(2)
        b.join(2)
    finally:
        notify.remove_sink(slow)
    assert [m["key"] for m in _clean] == ["holding|NVDA"]


def test_a_restarted_server_resends_its_outbox_to_an_old_page():
    notify._SEQ["v"] = 0
    notify.send("item", "a", "", "k")
    assert [m["title"] for m in notify.outbox(since=12)["items"]] == ["a"]
    assert notify.outbox(since=1)["items"] == []


def test_switch_off_sends_nothing_and_schedules_nothing(_clean, monkeypatch):
    _armed()
    assert notify.notify_rail(_att([_pos("NVDA", 24.0)]), s={**QUIET_OFF, "notify_on": False}) == 0
    save_prefs({"notify_on": False})
    called = []
    monkeypatch.setattr(inv, "held_symbols", lambda: ["AAPL"])
    assert notify.tick(_utc(2026, 10, 13, 15, 0), submit=lambda: called.append(1)) == "off"
    assert called == [] and _clean == []


def test_switching_off_disarms_the_baseline():
    _armed()
    notify.on_prefs(save_prefs({"notify_on": False}))
    assert not attention.load_state()["armed"]


def test_a_running_job_means_try_next_minute(monkeypatch):
    monkeypatch.setattr(inv, "held_symbols", lambda: ["AAPL"])
    monkeypatch.setattr(notify, "maybe_recap", lambda *a, **k: None)
    now = _utc(2026, 10, 13, 15, 0)
    assert notify.tick(now, submit=lambda: (object(), "rejected")) == "busy"
    assert notify._STATE["last"] is None  # retried on the next tick
    assert notify.tick(now, submit=lambda: (object(), "created")) == "session"
    assert notify._STATE["last"] == now


def test_a_degraded_page_sends_and_records_nothing(_clean, monkeypatch):
    _armed()
    ps = [_pos("NVDA", 24.0), _pos("AAPL", 24.0, note="valued at the last trade price")]
    out = {**_page(ps), "series": {"dates": ["2026-10-13"]}, "attention": _att(ps)}
    monkeypatch.setattr(inv, "held_symbols", lambda: ["NVDA", "AAPL"])
    monkeypatch.setattr(inv, "page", lambda ccy=None, fresh=False: out)
    assert notify.watch() == "degraded"  # half the holdings without a price
    assert _clean == [] and attention.load_state()["notified"] == {}
    monkeypatch.setattr(inv, "page", lambda ccy=None, fresh=False: {**out, "error": "Yahoo"})
    assert notify.watch() == "degraded"


def test_watch_fetches_held_shares_only(monkeypatch):
    """Inside the pass, a sold share's expired closes are served from the
    cache; a held one's are fetched again."""
    old = time.time() - 10 * cache._BULK_CLOSE_TTL
    ser = pd.Series([1.0, 2.0])
    cache._BULK_CLOSE_CACHE[("XOM", "5y|raw")] = (old, ser)
    cache._BULK_CLOSE_CACHE[("AAPL", "5y|raw")] = (old, ser)
    try:
        with cache.prefer_cached({"AAPL"}):
            assert cache._bulk_close_get_cached("XOM", "5y|raw") is ser
            assert cache._bulk_close_get_cached("AAPL", "5y|raw") is cache._BULK_CLOSE_MISS
        assert (
            cache._bulk_close_get_cached("XOM", "5y|raw") is cache._BULK_CLOSE_MISS
        )  # normal again
        seen = {}
        t = threading.Thread(
            target=lambda: seen.update(v=cache._bulk_close_get_cached("XOM", "5y|raw"))
        )
        with cache.prefer_cached({"AAPL"}):
            t.start()
            t.join()
        assert seen["v"] is cache._BULK_CLOSE_MISS  # another thread (the page's) is untouched
    finally:
        for k in (("XOM", "5y|raw"), ("AAPL", "5y|raw")):
            cache._BULK_CLOSE_CACHE.pop(k, None)


def test_the_books_prefetch_keeps_prefer_cached(monkeypatch):
    """investments._prefetch downloads in a pool's worker thread: the pass's
    held-only rule must reach it (a context variable doesn't by itself)."""
    from convexity import analytics, fx

    asked: list[str] = []

    def download(tickers, **k):
        asked.extend(tickers)
        cols = pd.MultiIndex.from_product([tickers, ["Close", "Dividends"]])
        return pd.DataFrame(1.0, index=pd.to_datetime(["2026-10-12", "2026-10-13"]), columns=cols)

    monkeypatch.setattr(analytics.yf, "download", download)
    monkeypatch.setattr(fx, "prefetch_usd_series", lambda *a, **k: None)
    monkeypatch.setattr(analytics, "known_proxies", lambda s: [])
    old = time.time() - 10 * cache._BULK_CLOSE_TTL
    ser = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-10-12", "2026-10-13"]))
    keys = [("XOM", "1y|raw"), ("AAPL", "1y|raw"), ("SPY", "1y")]
    for k in keys:
        cache._BULK_CLOSE_CACHE[k] = (old, ser)
        cache._DIVIDENDS_CACHE[k[0]] = (old, ser.iloc[:0])
    try:
        entries = [{"symbol": s, "date": (date.today() - timedelta(days=60)).isoformat()}
                   for s in ("XOM", "AAPL")]  # fmt: skip
        with cache.prefer_cached({"AAPL", "SPY"}):
            inv._prefetch({"instruments": {}}, entries, "USD", "USD")
        assert "XOM" not in asked and {"AAPL", "SPY"} <= set(asked)
    finally:
        for k in keys:
            cache._BULK_CLOSE_CACHE.pop(k, None)
            cache._DIVIDENDS_CACHE.pop(k[0], None)


# ------------------------------------------------------------------- jobs


def test_owner_refresh_supersedes_a_background_pass(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(notify, "watch", lambda cancel=None: gate.wait(5) and "done")
    bg, outcome = notify._submit()
    assert outcome == "created" and bg.background
    # The scheduler never supersedes: a second pass waits for the next tick.
    assert notify._submit()[1] == "rejected"
    monkeypatch.setattr(jobs, "_run_book_phase", lambda job: None)
    mine, outcome = jobs.submit(scope="current", phases=["book"], days=7,
                                entries_by_view={jobs.BOOK_VIEW: ""})  # fmt: skip
    assert outcome == "created" and bg.cancel.is_set() and not mine.background
    gate.set()


def test_a_page_cannot_make_its_refresh_a_background_job(monkeypatch):
    """``background`` is the scheduler's: a client-sent one would hide the
    owner's job from the chip and let the next refresh supersede it."""
    import http.server
    import json
    import urllib.request

    from convexity import server

    got = {}

    def submit(**kw):
        got.update(kw)
        job = jobs.Job(scope=kw["scope"], phases=kw["phases"], views=list(kw["entries_by_view"]),
                       entries=kw["entries_by_view"], days=kw["days"], context=kw["context"])  # fmt: skip
        return job, "created"

    monkeypatch.setattr(jobs, "submit", submit)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    try:
        body = {
            "phases": ["quotes"],
            "entries": "AAPL",
            "context": {"background": True, "tab": "x"},
        }
        req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/refresh-job",
                                     method="POST", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})  # fmt: skip
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 202
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert got["context"] == {"tab": "x"}


# ------------------------------------------------------------------ prefs


def test_prefs_keep_only_valid_switches_and_times():
    save_prefs({"notify_on": "yes", "quiet_on": False, "quiet_from": "25:00", "quiet_to": "07:30"})
    assert load_prefs() == {"quiet_on": False, "quiet_to": "07:30"}
    assert notify.settings() == {**notify.DEFAULTS, "quiet_on": False, "quiet_to": "07:30"}
    # Digits str.isdigit() takes but int() refuses (or reads): never saved.
    for bad in ("²²:00", "２２:００", "24:00", "-1:00", "7:30", "07:60", 730):
        assert save_prefs({"quiet_from": bad}) == {"quiet_on": False, "quiet_to": "07:30"}


def test_the_pass_uses_the_currency_the_page_opens_in():
    assert notify._ccy() == "USD"  # nothing saved: the page's own default, not the base
    save_prefs({"default_ccy": "EUR"})
    assert notify._ccy() == "EUR"


# ------------------------------------------------------------------ recap


def _weekdays(start: date, end: date) -> list[str]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _recap_page(last: date = date(2026, 10, 16)):
    dates = _weekdays(date(2026, 10, 5), last)
    n = len(dates)
    idx = [1.0 + 0.01 * i for i in range(n)]
    bench = [1.0 + 0.005 * i for i in range(n)]
    value = [10_000.0 + 150.0 * i for i in range(n)]
    flows = [0.0] * n
    flows[7] = 1_000.0  # money in on Wed 14 Oct
    flows = [sum(flows[: i + 1]) for i in range(n)]
    fri = "2026-10-16"
    ps = [_pos("NVDA", 30.0, xray={"day_date": fri, "week_pct": 6.2}),
          _pos("AAPL", 30.0, xray={"day_date": fri, "week_pct": -3.1}),
          _pos("KO", 30.0, xray={"day_date": "2026-10-15", "week_pct": 9.0})]  # fmt: skip
    entries = [{"type": "dividend", "date": "2026-10-14", "cash": 48.0, "symbol": "KO"},
               {"type": "dividend", "date": "2026-10-09", "cash": 30.0, "symbol": "KO"}]  # fmt: skip
    return {"ccy": "USD", "base_ccy": "USD", "positions": ps, "entries": entries,
            "benchmark": {"label": "S&P 500"},
            "series": {"dates": dates, "twr_index": idx, "bench_index": bench, "value": value, "flows": flows},
            "attention": {"high": [{"key": "x"}, {"key": "y"}]},
            "computed_at": "2026-10-17T12:00:00Z"}  # fmt: skip


def test_recap_friday_after_the_us_close():
    assert notify.recap_friday(_utc(2026, 10, 16, 20, 0)) == date(2026, 10, 9)  # 16:00 New York
    assert notify.recap_friday(_utc(2026, 10, 16, 20, 31)) == date(2026, 10, 16)
    assert notify.recap_friday(_utc(2026, 10, 19, 12, 0)) == date(2026, 10, 16)  # Monday


def test_recap_figures_are_the_pages_own():
    out = _recap_page()
    events = {"AAPL": {"earnings": "2026-10-21"}, "SPY": {"earnings": "2026-10-20"}}
    r = notify.build_recap(out, date(2026, 10, 16), date(2026, 10, 17), events)
    s, e = 4, 9  # Fri 9 Oct and Fri 16 Oct
    ser = out["series"]
    assert r["from"] == "2026-10-09" and r["to"] == "2026-10-16" and not r["partial"]
    assert r["return_pct"] == pytest.approx((ser["twr_index"][e] / ser["twr_index"][s] - 1) * 100)
    assert r["bench_pct"] == pytest.approx(
        (ser["bench_index"][e] / ser["bench_index"][s] - 1) * 100
    )
    assert r["gain"] == pytest.approx(5 * 150.0 - 1_000.0)  # value change less the money put in
    # KO's last close isn't that Friday: it can't be a mover (no made-up week).
    assert r["best"]["symbol"] == "NVDA" and r["worst"]["symbol"] == "AAPL"
    assert r["dividends"] == {"amount": 48.0, "ccy": "USD", "count": 1}
    assert r["needs_look"] == 2
    assert r["next_week"] == [
        {"symbol": "AAPL", "name": "Aapl", "what": "earnings", "date": "2026-10-21"}
    ]
    assert notify.recap_message(r) == (
        "Your week to Fri 16 Oct",
        "+4.8% · S&P 500 +2.5% · 2 need a look",
    )


def test_recap_waits_for_fridays_close_and_dashes_what_it_cannot_say():
    thursday = _recap_page(last=date(2026, 10, 15))
    assert notify.build_recap(thursday, date(2026, 10, 16), date(2026, 10, 16)) is None
    old = _recap_page()
    old["series"].pop("flows")  # a page cached before Phase 9
    r = notify.build_recap(old, date(2026, 10, 16), date(2026, 10, 17))
    assert r["gain"] is None and r["return_pct"] is not None
    late = _recap_page(last=date(2026, 10, 19))  # computed on Monday
    for p in late["positions"]:
        p["xray"]["day_date"] = "2026-10-19"
    r = notify.build_recap(late, date(2026, 10, 16), date(2026, 10, 19))
    assert r["to"] == "2026-10-16" and r["best"] is None and r["worst"] is None


def test_recap_is_built_once_and_caught_up_on_launch(_clean, monkeypatch):
    monkeypatch.setattr(inv, "computed_page", lambda ccy=None: _recap_page())
    monday = _utc(2026, 10, 19, 13, 0)  # the app was closed over the weekend
    save_prefs({"quiet_on": False})
    r = notify.maybe_recap(monday)
    assert r["week_end"] == "2026-10-16"
    assert [m["kind"] for m in _clean] == ["recap"] and _clean[0]["key"] == notify.RECAP_KEY
    assert notify.maybe_recap(monday)["week_end"] == "2026-10-16"
    assert len(_clean) == 1  # once a week
    assert notify.load_recaps().keys() == {"2026-10-16"}


def test_a_recap_is_not_sent_once_the_next_week_is_under_way(_clean, monkeypatch):
    monkeypatch.setattr(inv, "computed_page", lambda ccy=None: _recap_page())
    save_prefs({"quiet_on": False})
    thursday = _utc(2026, 10, 22, 15, 0)  # the app opened for the first time since
    assert notify.recap_friday(thursday) == date(2026, 10, 16)
    r = notify.maybe_recap(thursday)
    assert r["week_end"] == "2026-10-16" and _clean == []  # built, shown in the app, not sent


def test_recap_dividends_are_in_one_currency_or_a_dash():
    out = _recap_page()
    out["entries"] += [{"type": "dividend", "date": "2026-10-15", "cash": 10.0, "ccy": "GBP",
                        "fx_rate": 1.3, "symbol": "SHEL.L"}]  # fmt: skip
    r = notify.build_recap(out, date(2026, 10, 16), date(2026, 10, 17))
    assert r["dividends"] == {"amount": pytest.approx(61.0), "ccy": "USD", "count": 2}
    out["entries"][-1].pop("fx_rate")  # that day's rate: a fetch, so no total
    r = notify.build_recap(out, date(2026, 10, 16), date(2026, 10, 17))
    assert r["dividends"]["amount"] is None and r["dividends"]["count"] == 2


def test_recap_is_never_kept_from_a_page_short_of_the_truth(_clean, monkeypatch):
    """A cold start's last page can leave a holding out (splits pending):
    a recap from it would be a made-up week, kept for good (live /verify)."""
    save_prefs({"quiet_on": False})
    monday = _utc(2026, 10, 19, 13, 0)
    blind = _recap_page()
    for p in blind["positions"][:2]:
        p["note"] = "valued at the last trade price"
    short = _recap_page(last=date(2026, 10, 14))  # the copy on disk from Wednesday
    for bad in ({**_recap_page(), "splits_pending": [{"symbol": "NVDA"}]},
                {**_recap_page(), "error": "Yahoo"}, blind, {**short, "stale": True}):  # fmt: skip
        monkeypatch.setattr(inv, "computed_page", lambda ccy=None, b=bad: b)
        assert notify.maybe_recap(monday) is None
    assert notify.load_recaps() == {} and _clean == []
    # The last good page on disk is fine once it has Friday's close.
    monkeypatch.setattr(inv, "computed_page", lambda ccy=None: {**_recap_page(), "stale": True})
    assert notify.maybe_recap(monday)["to"] == "2026-10-16"


def test_a_recap_waits_for_a_page_computed_after_fridays_close(_clean, monkeypatch):
    """A busy after-close pass leaves the 15:50 page in the memo: the recap
    must not keep Friday's intraday prices for the week."""
    page = {**_recap_page(), "computed_at": "2026-10-16T19:50:00Z"}  # 15:50 New York
    monkeypatch.setattr(inv, "computed_page", lambda ccy=None: page)
    save_prefs({"quiet_on": False})
    after = _utc(2026, 10, 16, 20, 40)
    assert notify.maybe_recap(after) is None and notify.load_recaps() == {}
    page["computed_at"] = "2026-10-16T20:35:00Z"  # the after-close pass
    assert notify.maybe_recap(after)["week_end"] == "2026-10-16"
