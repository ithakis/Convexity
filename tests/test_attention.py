"""My Investments' Attention rail (attention.py, Phase 7): each rule
fires on a crafted book and stays quiet on a calm one; dismiss, seen and
prune; thresholds; the events cache; the routes. Public tickers, synthetic
numbers, no network."""

import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from convexity import attention, paths
from convexity import investments as inv

TODAY = date(2026, 10, 13)  # a Tuesday
REAL_FETCH = attention.fetch_events  # conftest stubs it per test; this is the real one


def _pos(sym, weight, **kw):
    return {"symbol": sym, "name": sym.title(), "weight": weight, "quote_ccy": "USD",
            "return_pct": 5.0, "sector": None, "xray": {}, **kw}  # fmt: skip


def _page(positions, ccy="USD", **kw):
    return {"ccy": ccy, "base_ccy": ccy, "positions": positions,
            "headline": {"cash_weight": 0.0}, **kw}  # fmt: skip


def _state(**rules):
    st = attention.load_state()
    for k, v in rules.items():
        st["rules"][k]["value"] = v
    return st


def _keys(att):
    return [it["key"] for it in att["high"] + att["info"]]


def _ev(page, **kw):
    return attention.evaluate(page, kw.pop("state", None) or _state(), kw.pop("events", {}), TODAY)


CALM = _page([_pos(s, 12.0, sector=f"S{i}") for i, s in enumerate(["AAPL", "MSFT", "NVDA", "KO", "JNJ",
                                                                     "XOM", "JPM", "PG"])])  # fmt: skip


def test_calm_book_is_quiet():
    att = _ev(CALM)
    assert att["high"] == [] and att["info"] == [] and att["unread"] == 0


def test_single_name_above_limit():
    att = _ev(_page([_pos("NVDA", 24.0), _pos("AAPL", 10.0)]))
    assert _keys(att) == ["holding|NVDA"]
    it = att["high"][0]
    assert it["text"] == "Nvda is 24% of your book" and it["level"] == 9.0  # points past the limit
    assert _ev(_page([_pos("NVDA", 24.0)]), state=_state(holding=25))["high"] == []
    assert _ev(_page([_pos("VWCE.DE", 40.0, fund=True)]))["high"] == []  # a fund is not one bet


def test_sector_cluster_needs_two_names():
    ps = [_pos("NVDA", 14.0, sector="Technology"), _pos("MSFT", 14.0, sector="Technology"),
          _pos("ASML.AS", 10.0, sector="Technology"), _pos("KO", 10.0, sector="Consumer")]  # fmt: skip
    att = _ev(_page(ps))
    assert _keys(att) == ["sector|Technology"]
    assert "Nvda, Msft and Asml.As" in att["high"][0]["sub"]
    # One holding alone is the single-name rule's job, not a "cluster".
    assert (
        _ev(_page([_pos("NVDA", 14.0, sector="Technology")]), state=_state(sector=10))["high"] == []
    )


def test_currency_cluster_skips_the_display_currency_and_counts_cash():
    ps = [_pos("SHEL.L", 40.0, quote_ccy="GBp"), _pos("ULVR.L", 25.0, quote_ccy="GBp"),
          _pos("AAPL", 30.0)]  # fmt: skip
    st = _state(holding=90)
    att = _ev(_page(ps), state=st)
    assert _keys(att) == ["currency|GBP|in:USD"]
    assert att["high"][0]["text"] == "65% of your book is in GBP"
    assert _ev(_page(ps, ccy="GBP"), state=st)["high"] == []  # viewed in pounds: home currency
    # Uninvested cash in another base currency is that currency's exposure.
    cash = _page([_pos("AAPL", 30.0)], ccy="EUR", base_ccy="USD", headline={"cash_weight": 70.0})
    att = _ev(cash, state=st)
    assert _keys(att) == ["currency|USD|in:EUR"] and att["high"][0]["sub"].endswith("Aapl and cash")


def test_drawdowns_from_cost_and_from_peak():
    ps = [_pos("INTC", 10.0, return_pct=-31.0, xray={"peak_pct": -40.0, "peak_date": "2026-07-02"}),
          _pos("KO", 10.0, return_pct=-5.0, xray={"peak_pct": -8.0, "peak_date": "2026-09-01"})]  # fmt: skip
    att = _ev(_page(ps))
    assert [it["key"] for it in att["high"]] == ["from_cost|INTC|in:USD"]
    assert [it["key"] for it in att["info"]] == ["from_peak|INTC"]
    assert att["high"][0]["level"] == 11.0
    assert "Peak Thu 2 Jul" in att["info"][0]["sub"]


def test_sigma_move_only_while_fresh():
    x = {"day_date": "2026-10-09", "day_pct": -7.9, "day_z": -3.4, "sigma_pct": 2.3}
    att = _ev(_page([_pos("ASML.AS", 10.0, xray=x)]))
    assert _keys(att) == ["move|ASML.AS|2026-10-09"]
    assert att["high"][0]["text"] == "Asml.As fell 7.9% on Fri 9 Oct"
    assert att["high"][0]["sub"] == "3.4σ · its usual day moves 2.3%"
    old = {**x, "day_date": "2026-10-01"}
    assert _ev(_page([_pos("ASML.AS", 10.0, xray=old)]))["high"] == []
    small = {**x, "day_z": -2.5}
    assert _ev(_page([_pos("ASML.AS", 10.0, xray=small)]))["high"] == []


def _news(tier, days_ago=1.0, **kw):
    at = (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat()
    return {"news": {"tier": tier, "assessed_at": at, "lookback_days": 7, **kw}}


def test_news_read_divergence():
    x = {"week_pct": -6.1, "week_z": -1.8, "week_sigma_pct": 3.4}
    att = _ev(_page([_pos("SHEL.L", 10.0, xray=x, news_sentiment=_news("bullish"))]))
    assert [it["rule"] for it in att["info"]] == ["divergence"]
    it = att["info"][0]
    assert it["ai"] and it["text"] == "Shel.L: News read bullish, price −6.1% this week"
    # Agreeing, mixed, too old or flagged stale: quiet.
    for news in (_news("bearish"), _news("neutral"), _news("bullish", days_ago=10),
                 _news("bullish", stale=True), None):  # fmt: skip
        assert _ev(_page([_pos("SHEL.L", 10.0, xray=x, news_sentiment=news)]))["info"] == []


def test_rules_switch_off_and_pending_rows_are_skipped():
    st = _state()
    st["rules"]["holding"]["on"] = False
    assert _ev(_page([_pos("NVDA", 30.0)]), state=st)["high"] == []
    assert _ev(_page([{"symbol": "NVDA", "name": "NVIDIA", "pending": True}]))["high"] == []


def test_dismiss_holds_until_worse_and_prune_forgets():
    page = _page([_pos("NVDA", 24.0)])
    att = attention.evaluate(page, None, {}, TODAY)
    assert att["unread"] == 1 and att["unread_high"]
    attention.dismiss("holding|NVDA", att["high"][0]["level"])
    att = attention.evaluate(page, None, {}, TODAY)
    assert att["high"] == [] and att["hidden"] == 1 and att["unread"] == 0
    # Back only a full 5-pt step worse than when dismissed (24% -> 29%).
    assert attention.evaluate(_page([_pos("NVDA", 28.9)]), None, {}, TODAY)["high"] == []
    assert _keys(attention.evaluate(_page([_pos("NVDA", 29.0)]), None, {}, TODAY)) == [
        "holding|NVDA"
    ]
    # It clears (a fresh page without it): forgotten, so it is new next time.
    attention.prune(set(), {"holding|NVDA"})
    assert _keys(attention.evaluate(page, None, {}, TODAY)) == ["holding|NVDA"]


def test_seen_clears_unread_until_worse():
    page = _page([_pos("NVDA", 24.0)])
    att = attention.evaluate(page, None, {}, TODAY)
    attention.mark_seen([[it["key"], it["level"]] for it in att["high"]])
    assert attention.evaluate(page, None, {}, TODAY)["unread"] == 0
    assert attention.evaluate(_page([_pos("NVDA", 27.0)]), None, {}, TODAY)["unread"] == 0
    assert attention.evaluate(_page([_pos("NVDA", 31.0)]), None, {}, TODAY)["unread"] == 1


def test_state_file_malformed_or_hostile_reads_as_defaults():
    p = paths.state_file("attention")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    st = attention.load_state()
    assert st["rules"]["holding"] == {"on": True, "value": 15.0} and st["dismissed"] == {}
    p.write_text(json.dumps({"rules": {"holding": {"on": "yes", "value": 1e9}, "evil": {}},
                             "dismissed": {"a": "x", "b": 2}, "seen": []}))  # fmt: skip
    st = attention.load_state()
    assert st["rules"]["holding"] == {"on": True, "value": 100}
    assert "evil" not in st["rules"] and st["dismissed"] == {"b": 2.0} and st["seen"] == {}


def test_set_rules_validates_and_clamps():
    assert attention.set_rules({"move": {"value": 0.1}})["move"]["value"] == 1.5
    assert attention.set_rules({"holding": {"on": False}})["holding"] == {
        "on": False,
        "value": 15.0,
    }
    for bad in (
        {"nope": {"value": 1}},
        {"holding": {"value": "x"}},
        {"holding": {"on": 1}},
        {},
        [],
    ):
        with pytest.raises(ValueError):
            attention.set_rules(bad)


def test_upcoming_events_window():
    page = _page([_pos("MSFT", 10.0), _pos("SHEL.L", 10.0)])
    events = {"MSFT": {"earnings": "2026-10-23", "ex_div": "2026-08-14"},
              "SHEL.L": {"earnings": "2026-11-30", "ex_div": "2026-10-13"}}  # fmt: skip
    up = attention.evaluate(page, _state(), events, TODAY)["upcoming"]
    assert [(u["symbol"], u["what"], u["date"]) for u in up] == [
        ("SHEL.L", "ex-dividend", "2026-10-13"), ("MSFT", "earnings", "2026-10-23")]  # fmt: skip
    attention.dismiss(up[0]["key"], 0)
    assert len(attention.evaluate(page, None, events, TODAY)["upcoming"]) == 1
    assert attention.evaluate(page, _state(events=7), events, TODAY)["upcoming"] == []


def test_events_refresh_keeps_last_good_on_failure():
    attention._EVENTS_TRIED.clear()
    good = {"earnings": "2026-10-23", "ex_div": None}
    assert attention.refresh_events(["MSFT"], fetch=lambda s: good) == 1
    assert attention.load_events()["MSFT"]["earnings"] == "2026-10-23"
    # A failed call later (forced): the old dates stay, and it waits to retry.
    assert attention.refresh_events(["MSFT"], force=True, fetch=lambda s: None) == 0
    assert attention.load_events()["MSFT"]["earnings"] == "2026-10-23"
    calls = []
    attention.refresh_events(["MSFT"], force=True, fetch=lambda s: calls.append(s) or good)
    assert calls == []  # inside EVENTS_RETRY
    attention._EVENTS_TRIED.clear()
    # Fresh within a day: not fetched again unless forced.
    assert (
        attention.refresh_events(["MSFT"], fetch=lambda s: calls.append(s) or good) == 0
        and calls == []
    )


def test_calendar_parsing(monkeypatch):
    import yfinance as yf

    class T:
        def __init__(self, s):
            self.calendar = {"Earnings Date": [date(2026, 10, 23), date(2026, 10, 27)],
                             "Ex-Dividend Date": date(2026, 11, 19)}  # fmt: skip

    monkeypatch.setattr(yf, "Ticker", T)
    assert REAL_FETCH("MSFT") == {"earnings": "2026-10-23", "ex_div": "2026-11-19"}

    class Fund:
        calendar = {}

    monkeypatch.setattr(yf, "Ticker", lambda s: Fund())
    assert REAL_FETCH("SPY") == {"earnings": None, "ex_div": None}  # an answer, not a failure

    class Down:
        @property
        def calendar(self):
            raise ConnectionError("Yahoo down")

    monkeypatch.setattr(yf, "Ticker", lambda s: Down())
    assert REAL_FETCH("MSFT") is None


# ------------------------------------------------------------------ x-ray


def _series(vals, end="2026-10-09"):
    idx = pd.bdate_range(end=end, periods=len(vals))
    return pd.Series(vals, index=idx)


def test_xray_sigma_move_and_peak():
    rng = np.random.default_rng(7)
    rets = rng.normal(0, 0.01, 300)
    px = 100 * np.exp(np.cumsum(rets))
    px = np.append(px, px[-1] * 0.95)  # a -5% day on 1% daily vol: about -5 sigma
    x = inv._xray(_series(px), "2025-01-01")
    assert x["day_date"] == "2026-10-09" and x["day_pct"] == pytest.approx(-5.0, abs=1e-6)
    assert x["day_z"] == pytest.approx(np.log(0.95) / np.std(rets[-252:], ddof=1), rel=1e-3)
    held = list(pd.bdate_range(end="2026-10-09", periods=len(px)).strftime("%Y-%m-%d"))
    k = int(np.argmax(px))
    assert x["peak_date"] == held[k] and x["peak_pct"] == pytest.approx(
        (px[-1] / px[k] - 1) * 100, abs=1e-4
    )
    assert "week_z" in x
    # The peak counts only since the holding was bought.
    late = inv._xray(_series(px), held[-3])
    assert late["peak_date"] >= held[-3]


def test_xray_short_history_has_no_sigma():
    x = inv._xray(_series([10.0, 10.5, 9.0]), "2020-01-01")
    assert x["day_z"] is None and x["peak_pct"] == pytest.approx((9.0 / 10.5 - 1) * 100, abs=1e-4)
    assert inv._xray(None, "2020-01-01") == {} and inv._xray(_series([5.0]), "2020-01-01") == {}


def test_held_since_restarts_after_a_full_sale():
    dates = ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"]
    assert inv._held_since(np.array([1.0, 0.0, 2.0, 2.0]), dates) == "2026-01-05"
    assert inv._held_since(np.array([1.0, 1.0, 1.0, 1.0]), dates) == "2026-01-01"


def test_pack_kind_marks_funds_and_skips_their_sector(monkeypatch):
    from convexity import symbol_db

    hits = {"VWCE.DE": ("etf", None), "NVDA": ("stock", "Technology"), "VFIAX": ("fund", "x")}
    monkeypatch.setattr(
        symbol_db,
        "get",
        lambda s: (h := hits.get(s)) and type("H", (), {"type": h[0], "sector": h[1]}),
    )
    assert inv._pack_kind("VWCE.DE") == {"sector": None, "fund": True}
    assert inv._pack_kind("VFIAX") == {"sector": None, "fund": True}
    assert inv._pack_kind("NVDA") == {"sector": "Technology", "fund": False}
    assert inv._pack_kind("ZZZZ") == {"sector": None, "fund": False}


def test_wording_never_reads_as_the_limit_itself():
    att = _ev(_page([_pos("AAPL", 15.16), _pos("NVDA", 24.0)]))
    assert [it["text"] for it in att["high"]] == [
        "Nvda is 24% of your book",
        "Aapl is 15.2% of your book",
    ]
    assert attention._names([{"name": n, "weight": w} for n, w in (("A", 3), ("B", 2), ("C", 1), ("D", 0), ("E", 0))],
                            extra="cash") == "A, B, C, 2 more and cash"  # fmt: skip
    assert attention._names([], extra="cash") == "cash"
    assert attention._names([{"name": "A", "weight": 1}]) == "A"


def test_sector_and_fund_are_read_when_asked_not_when_computed(monkeypatch):
    # The page on disk was computed before the symbol pack arrived: no sector,
    # no fund flag. The rail still knows them once the pack is there.
    monkeypatch.setattr(inv, "_pack_kind", lambda s, name=None: {"sector": None, "fund": True} if s == "VWCE.DE"
                        else {"sector": "Technology", "fund": False})  # fmt: skip
    page = {**_page([_pos("VWCE.DE", 30.0), _pos("NVDA", 20.0), _pos("MSFT", 20.0)]), "series": {}}
    att = inv.with_attention(page)["attention"]
    assert _keys(att) == ["holding|NVDA", "holding|MSFT", "sector|Technology"]


def test_prune_forgets_only_what_the_page_could_check():
    """Adversarial review, 2026-10-13: a currency switch, a holding without
    prices for a day, or a rule switched off used to forget dismissals."""
    for k, lvl in (("currency|USD|in:GBP", 10.0), ("from_cost|SHEL.L|in:USD", 5.0),
                   ("from_peak|NVDA", 15.0), ("holding|NVDA", 5.0)):  # fmt: skip
        attention.dismiss(k, lvl)
    # A fresh EUR page where NVDA has no prices today (no xray, a note) and
    # nothing fires: it can't speak for GBP's currencies, SHEL.L's cost in
    # USD or NVDA's peak; it did check NVDA's weight.
    page = _page([_pos("NVDA", 5.0, note="No price from Yahoo"), _pos("SHEL.L", 5.0, quote_ccy="GBp")],
                 ccy="EUR")  # fmt: skip
    att = attention.evaluate(page, None, {}, TODAY)
    attention.prune(set(att["firing"]), set(att["checked"]))
    assert attention.load_state()["dismissed"] == {"currency|USD|in:GBP": 10.0,
                                                   "from_cost|SHEL.L|in:USD": 5.0,
                                                   "from_peak|NVDA": 15.0}  # fmt: skip
    # The same in USD with prices: checked and quiet, so forgotten.
    page = _page([_pos("NVDA", 5.0, xray={"peak_pct": -1.0, "peak_date": "2026-10-01"}),
                  _pos("SHEL.L", 5.0, quote_ccy="GBp")])  # fmt: skip
    att = attention.evaluate(page, None, {}, TODAY)
    attention.prune(set(att["firing"]), set(att["checked"]))
    assert attention.load_state()["dismissed"] == {"currency|USD|in:GBP": 10.0}
    # A rule switched off checks nothing: its dismissal stays.
    attention.dismiss("holding|NVDA", 5.0)
    st = _state()
    st["rules"]["holding"]["on"] = False
    att = attention.evaluate(_page([_pos("NVDA", 5.0)]), st, {}, TODAY)
    attention.prune(set(att["firing"]), set(att["checked"]))
    assert "holding|NVDA" in attention.load_state()["dismissed"]


def test_huge_numbers_are_refused_not_a_500_and_the_lists_are_capped():
    with pytest.raises(ValueError):
        attention.dismiss("holding|NVDA", 10**400)
    with pytest.raises(ValueError):
        attention.set_rules({"holding": {"value": 10**400}})
    with pytest.raises(ValueError):
        attention.mark_seen([["k", 10**400]])
    for i in range(3):
        attention.mark_seen([[f"k{i}-{j}", 1] for j in range(attention.MAX_KEPT)])
    seen = attention.load_state()["seen"]
    assert len(seen) == attention.MAX_KEPT and f"k2-{attention.MAX_KEPT - 1}" in seen


def test_an_empty_calendar_does_not_wipe_known_dates():
    """yfinance turns a 429 into an empty calendar (adversarial review)."""
    attention._EVENTS_TRIED.clear()
    ahead = (date.today() + timedelta(days=10)).isoformat()
    attention.refresh_events(["MSFT"], fetch=lambda s: {"earnings": ahead, "ex_div": None})
    attention.refresh_events(
        ["MSFT"], force=True, fetch=lambda s: {"earnings": None, "ex_div": None}
    )
    assert attention.load_events()["MSFT"]["earnings"] == ahead
    # Once the known date has passed, an empty calendar is believed.
    attention._EVENTS_TRIED.clear()
    past = (date.today() - timedelta(days=1)).isoformat()
    attention.refresh_events(["KO"], fetch=lambda s: {"earnings": past, "ex_div": None})
    attention.refresh_events(["KO"], force=True, fetch=lambda s: {"earnings": None, "ex_div": None})
    assert attention.load_events()["KO"]["earnings"] is None


def test_xray_adds_dividends_back():
    """A T-bill fund: +0.017%/day, its monthly payout taken on the ex-date.
    As traded it read as a -4.4 sigma fall every month (adversarial review)."""
    days = pd.bdate_range(end="2026-10-09", periods=294)  # the last day is an ex-date
    px, divs, p = [], {}, 100.0
    for i, d in enumerate(days):
        p *= 1.00017
        if i % 21 == 20:
            divs[d] = p * 0.0035
            p -= p * 0.0035
        px.append(p)
    s = pd.Series(px, index=days)
    raw = inv._xray(s, "2020-01-01")
    fixed = inv._xray(s, "2020-01-01", pd.Series(divs))
    # As traded, the ex-date is a fall and a new low; with the payout added back, neither.
    assert raw["day_pct"] == pytest.approx(-0.333, abs=1e-3) and raw["peak_pct"] < -0.3
    assert fixed["day_pct"] == pytest.approx(0.017, abs=1e-6)
    assert abs(fixed["day_z"]) < 1 and fixed["peak_pct"] == pytest.approx(0.0, abs=1e-6)
    assert fixed["sigma_pct"] == pytest.approx(0.2002, abs=1e-3)  # the floor: it barely moves


def test_xray_ignores_yahoo_glitches():
    rng = np.random.default_rng(3)
    px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    spiked = px.copy()
    spiked[150] *= 52  # one x52 day that reverts
    x = inv._xray(_series(spiked), "2020-01-01")
    assert x["peak_pct"] > -30
    flipped = np.append(px, px[-1] / 100)  # the last close in pounds, not pence
    assert inv._xray(_series(flipped), "2020-01-01")["day_pct"] == pytest.approx(
        inv._xray(_series(px), "2020-01-01")["day_pct"]
    )
    crash = np.append(px, px[-1] * 0.3)  # a real -70% day stays
    assert inv._xray(_series(crash), "2020-01-01")["day_pct"] == pytest.approx(-70.0, abs=1e-6)


def test_a_fund_the_pack_lacks_is_judged_by_its_name(monkeypatch):
    from convexity import symbol_db

    monkeypatch.setattr(symbol_db, "get", lambda s: None)
    assert inv._pack_kind("XYZW.DE", "Xtrackers MSCI World UCITS ETF 1C")["fund"] is True
    assert inv._pack_kind("RICHT.BD", "Richter Gedeon")["fund"] is False


# ---------------------------------------------- adversarial review, round 2


def test_dividend_dated_a_day_late_or_listed_twice():
    """A gilts fund fell on the 21st, Yahoo's ex-date the 22nd, and one
    payout listed twice a day apart: two false sigma moves before the fix."""
    rng = np.random.default_rng(5)
    days = pd.bdate_range(end="2026-10-09", periods=120)
    px = 10 * np.exp(np.cumsum(rng.normal(0, 0.003, 120)))
    px[-2:] -= 0.2  # the payout comes off on the second-to-last day
    late = pd.Series({days[-1]: 0.2, days[-1] + pd.Timedelta(days=1): 0.2})  # late, then a repeat
    x = inv._xray(pd.Series(px, index=days), "2020-01-01", late)
    assert abs(x["day_z"]) < 3  # the last day: no false rise
    tr_moves = inv._ex_dates(late, list(days.strftime("%Y-%m-%d")), px)
    assert tr_moves.sum() == pytest.approx(0.2) and tr_moves[-2] == pytest.approx(0.2)


def test_glitch_runs_first_points_and_real_crashes():
    rng = np.random.default_rng(9)
    px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    two = px.copy()
    two[120:122] *= 52  # a two-day x52 run
    assert inv._xray(_series(two), "2020-01-01")["peak_pct"] > -40
    first = px.copy()
    first[0] *= 100  # the very first close in pence
    assert inv._xray(_series(first), "2020-01-01")["peak_pct"] > -40
    pence = px.copy()
    pence[-2:] /= 100  # the last two closes in pounds
    x = inv._xray(_series(pence), "2020-01-01")
    assert x["peak_pct"] > -40 and x["day_date"] < "2026-10-09"
    crash = np.append(px, px[-1] * 0.04)  # a real -96% last day stays
    assert inv._xray(_series(crash), "2020-01-01")["day_pct"] == pytest.approx(-96.0, abs=1e-6)
    level = px.copy()
    level[150:] *= 0.2  # a crash that stays: every later close kept
    assert len(inv._xray_clean(list(zip(range(200), level, strict=True)))) == 200


def test_a_forced_refresh_during_another_runs_before_it_ends():
    attention._EVENTS_TRIED.clear()
    attention._FORCE_AGAIN.clear()
    calls = []

    def fetch(s):
        if s == "AAPL":  # while the first refresh runs, the topbar's forced one arrives
            attention.refresh_events_soon(["NVDA", "MSFT"], force=True)
        calls.append(s)
        return {"earnings": None, "ex_div": None}

    attention.refresh_events(["AAPL"], fetch=fetch)
    assert calls == ["AAPL", "MSFT", "NVDA"] and not attention._FORCE_AGAIN


def test_old_dated_keys_are_cleaned_up():
    attention.dismiss("divergence|SHEL.L|2026-01-02", 0)
    attention.dismiss(f"move|NVDA|{date.today().isoformat()}", 0)
    attention.prune(set(), set())
    assert list(attention.load_state()["dismissed"]) == [f"move|NVDA|{date.today().isoformat()}"]


def test_no_page_answers_say_so():
    att = inv.attention_now("USD")  # no book, no page
    assert att["page"] is False and att["rules"] and att["unread"] == 0


# ------------------------------------------------- Phase 8: plans and alerts


def _px(sym, weight, price, **kw):
    return _pos(sym, weight, price=price, xray={"day_date": "2026-10-12"}, **kw)


def test_target_and_review_fire_and_stay_quiet_between():
    attention.set_plan("AAPL", {"target": 210, "review": 160, "why": "Services margin"}, 180,
                       today=date(2026, 3, 3))  # fmt: skip
    quiet = _ev(_page([_px("AAPL", 10.0, 190.0)]))
    assert quiet["high"] == [] and quiet["plans"]["AAPL"]["target"] == 210
    hit = _ev(_page([_px("AAPL", 10.0, 212.4)]))
    assert _keys(hit) == ["target|AAPL|210"]
    assert hit["high"][0]["text"] == "Aapl reached your 210 target"
    assert hit["high"][0]["sub"] == "Set Tue 3 Mar at 180 · now 212.40" and hit["high"][0]["plan"]
    low = _ev(_page([_px("AAPL", 10.0, 150.0)]))
    assert _keys(low) == ["review|AAPL|160"]
    assert low["high"][0]["sub"] == "“Services margin”"  # the owner's why, quoted
    assert _ev(_page([_px("AAPL", 10.0, 150.0)]), state=_state())["unread_high"] is True
    st = _state()
    st["rules"]["review"]["on"] = False
    assert _ev(_page([_px("AAPL", 10.0, 150.0)]), state=st)["high"] == []


def test_levels_are_in_the_quote_unit():
    attention.set_plan("SHEL.L", {"review": 2500}, 2600)
    att = _ev(_page([_px("SHEL.L", 8.0, 2450.0, quote_ccy="GBp")]))
    assert att["high"][0]["text"] == "Shel.L fell below your 2,500p review level"


def test_max_weight_overrides_the_book_limit_funds_included():
    attention.set_plan("VWCE.DE", {"max_weight": 10}, 130)
    attention.set_plan("NVDA", {"max_weight": 25}, 100)
    att = _ev(_page([_px("VWCE.DE", 17.0, 130.0, fund=True), _px("NVDA", 20.0, 100.0)]))
    assert _keys(att) == ["holding|VWCE.DE|max10"]  # NVDA's own 25% allows its 20%
    assert att["high"][0]["sub"] == "Above your 10% plan limit"


def test_price_alert_fires_once_and_stays_until_removed():
    a = attention.add_alert("MSFT", 420, 400, today=TODAY)
    assert a["side"] == "above"
    assert _ev(_page([_px("MSFT", 9.0, 410.0)]))["info"] == []
    att = _ev(_page([_px("MSFT", 9.0, 421.1)]))
    assert _keys(att) == [f"price|MSFT|{a['id']}"] and att["fired"] == [
        ["MSFT", a["id"], 421.1, "2026-10-12"]
    ]
    assert att["info"][0]["sub"] == "Price alert · rose through it on Mon 12 Oct"
    attention.record_fired(att["fired"])
    back = _ev(_page([_px("MSFT", 9.0, 400.0)]))  # the price went back: it still shows, once
    assert _keys(back) == [f"price|MSFT|{a['id']}"] and back["fired"] == []
    attention.record_fired([["MSFT", a["id"], 1.0, "2026-10-13"]])  # never re-dated
    assert attention.load_plans()["MSFT"]["alerts"][0]["fired_on"] == "2026-10-12"
    attention.remove_alert("MSFT", a["id"])
    assert (
        _ev(_page([_px("MSFT", 9.0, 430.0)]))["info"] == [] and "MSFT" not in attention.load_plans()
    )


def test_snooze_hides_until_the_day_then_returns():
    attention.set_plan("AAPL", {"review": 160}, 180)
    page = _page([_px("AAPL", 10.0, 150.0)])
    key = _keys(_ev(page))[0]
    attention.snooze(key, "2026-10-20", today=TODAY)
    att = _ev(page)
    assert att["high"] == [] and att["snoozed"] == 1 and att["unread"] == 0
    back = attention.evaluate(page, attention.load_state(), {}, date(2026, 10, 20))
    assert _keys(back) == [key]
    for bad in ("2026-10-13", "2026-13-01", "soon", "2028-01-01"):
        with pytest.raises(ValueError):
            attention.snooze(key, bad, today=TODAY)


def test_plan_items_carry_the_next_earnings_for_snooze():
    attention.set_plan("AAPL", {"target": 100}, 90)
    att = _ev(_page([_px("AAPL", 10.0, 120.0)]), events={"AAPL": {"earnings": "2026-10-30"}})
    assert att["high"][0]["earnings"] == "2026-10-30"
    past = _ev(_page([_px("AAPL", 10.0, 120.0)]), events={"AAPL": {"earnings": "2026-10-01"}})
    assert "earnings" not in past["high"][0]


def test_set_plan_validates_and_records_when_levels_change():
    p = attention.set_plan("AAPL", {"target": 210}, 180, today=date(2026, 3, 3))
    assert p["set_on"] == "2026-03-03" and p["set_price"] == 180
    p = attention.set_plan("AAPL", {"why": "  a reason  "}, 999, today=TODAY)
    assert p["set_on"] == "2026-03-03" and p["why"] == "a reason"  # why alone keeps the date
    for fields in ({"review": 220}, {"target": -1}, {"max_weight": 0}, {"max_weight": 101},
                   {"why": 5}, {"nope": 1}, {}, {"target": 10**400}):  # fmt: skip
        with pytest.raises(ValueError):
            attention.set_plan("AAPL", fields, 180)
    with pytest.raises(ValueError):
        attention.set_plan("AAPL", {"target": 300}, None)  # a new level needs today's price
    with pytest.raises(ValueError):
        attention.add_alert("AAPL", 180, 180)  # today's price: no side
    assert len(attention.set_plan("AAPL", {"why": "x" * 500}, 1)["why"]) == attention.WHY_MAX
    attention.add_alert("AAPL", 250, 180)
    attention.clear_plan("AAPL")
    assert set(attention.load_plans()["AAPL"]) == {"alerts"}  # alerts have their own ✕


def test_a_damaged_plans_file_is_never_overwritten():
    path = paths.state_file("plans")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(attention.PlansUnreadable):
        attention.set_plan("AAPL", {"target": 1}, 1)
    attention.record_fired([["AAPL", "x", 1.0, "2026-10-13"]])  # quietly skipped
    att = _ev(_page([_px("AAPL", 10.0, 150.0)]))
    assert att["plans_error"] and att["plans"] == {}
    assert path.read_text(encoding="utf-8") == "{not json"
    path.write_text(json.dumps({"plans": []}), encoding="utf-8")
    with pytest.raises(attention.PlansUnreadable):
        attention.load_plans()


def test_plan_rules_are_switches_in_settings():
    rules = {r["key"]: r for r in _ev(CALM)["rules"]}
    assert rules["target"]["value"] is None and rules["target"]["on"] is True
    with pytest.raises(ValueError):
        attention.set_rules({"target": {"value": 5}})
    assert attention.set_rules({"price_alert": {"on": False}})["price_alert"] == {
        "on": False,
        "value": None,
    }


def test_a_split_after_a_plan_rescales_its_levels_once():
    attention.set_plan("NVDA", {"target": 1500, "review": 900}, 1000, today=date(2024, 5, 1))
    attention.add_alert("NVDA", 1200, 1000, today=date(2024, 5, 1))
    splits = {"NVDA": [("2024-06-10", 10.0)]}
    page = _page([_px("NVDA", 10.0, 95.0)])  # 950 before the split: above review
    assert _ev(page)["high"][0]["rule"] == "review"  # unadjusted: a false alarm
    adj, moved = attention.split_adjusted(attention.load_plans(), splits)
    assert moved and adj["NVDA"]["review"] == 90 and adj["NVDA"]["alerts"][0]["level"] == 120
    assert attention.evaluate(page, attention.load_state(), {}, TODAY, plans=adj)["high"] == []
    attention.apply_splits(splits)
    attention.apply_splits(splits)  # never divided twice
    saved = attention.load_plans()["NVDA"]
    assert saved["review"] == 90 and saved["target"] == 150 and saved["alerts"][0]["level"] == 120
    assert not attention.split_adjusted({"NVDA": saved}, splits)[1]
    # A level typed after the split is already in today's terms.
    attention.set_plan("NVDA", {"review": 80}, 95, today=TODAY)
    assert not attention.split_adjusted(attention.load_plans(), splits)[1]


# ---------------------------------- adversarial review (2026-10-14), each proven


def test_a_tighter_plan_cap_is_never_hidden_by_an_old_dismissal():
    page = _page([_px("AAPL", 35.0, 100.0)])
    it = _ev(page)["high"][0]
    attention.dismiss(it["key"], it["level"])  # dismissed under the book's 15%
    attention.set_plan("AAPL", {"max_weight": 30}, 100)
    assert _keys(_ev(page)) == ["holding|AAPL|max30"]


def test_a_plan_cap_holds_with_the_book_rule_switched_off():
    attention.set_plan("AAPL", {"max_weight": 10}, 100)
    st = _state()
    st["rules"]["holding"]["on"] = False
    att = _ev(_page([_px("AAPL", 40.0, 100.0), _px("MSFT", 40.0, 100.0)]), state=st)
    assert _keys(att) == ["holding|AAPL|max10"]  # MSFT has no plan: the switch rules it


def test_level_keys_are_exact():
    attention.set_plan("SAM", {"target": 12345.67}, 1)
    k1 = _keys(_ev(_page([_px("SAM", 5.0, 20000.0)])))
    attention.set_plan("SAM", {"target": 12345.71}, 1)
    k2 = _keys(_ev(_page([_px("SAM", 5.0, 20000.0)])))
    assert k1 != k2 and k1 == ["target|SAM|12345.67"]


def test_writes_keep_what_this_version_does_not_know():
    path = paths.state_file("plans")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {"version": 1, "future": 1, "plans": {"MSFT": {"target": "450", "stop": 90, "review": 300},
                                                 "AAPL": {"target": 210}}}  # fmt: skip
    path.write_text(json.dumps(raw), encoding="utf-8")
    attention.set_plan("AAPL", {"why": "x"}, 200)
    got = json.loads(path.read_text(encoding="utf-8"))
    assert (
        got["future"] == 1 and got["plans"]["MSFT"] == raw["plans"]["MSFT"]
    )  # untouched, verbatim
    assert got["plans"]["AAPL"] == {"target": 210, "why": "x"}
    path.write_text(json.dumps({**raw, "version": 2}), encoding="utf-8")
    with pytest.raises(attention.PlansUnreadable):
        attention.set_plan("AAPL", {"why": "y"}, 200)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2


def test_prune_drops_snoozes_past_their_day():
    attention.snooze("review|AAPL|160", (date.today() + timedelta(days=1)).isoformat())
    st = attention.load_state()
    st["snoozed"]["old|KEY"] = "2020-01-01"
    attention._save_state(st)
    attention.prune(set(), set())
    assert list(attention.load_state()["snoozed"]) == ["review|AAPL|160"]


def test_events_read_the_report_date_from_the_primary_line(monkeypatch):
    """WMT.DE's own calendar is empty on Yahoo: the report date is Walmart's.
    The listing's own ex-dividend date wins when it has one."""
    from convexity import capm

    monkeypatch.setattr(capm, "primary_line", lambda s: "WMT" if s == "WMT.DE" else s)
    cals = {"WMT.DE": {"earnings": None, "ex_div": "2026-12-05"},
            "WMT": {"earnings": "2026-11-19", "ex_div": "2026-12-04"}}  # fmt: skip
    monkeypatch.setattr(attention, "_calendar", lambda s: cals.get(s))
    assert REAL_FETCH("WMT.DE") == {"earnings": "2026-11-19", "ex_div": "2026-12-05"}
    assert REAL_FETCH("WMT") == cals["WMT"]
    cals["WMT.DE"] = None  # the own call failed: the company's dates still answer
    assert REAL_FETCH("WMT.DE") == {"earnings": "2026-11-19", "ex_div": "2026-12-04"}
