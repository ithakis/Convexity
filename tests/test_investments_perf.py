"""My Investments performance (Phase 3): the daily series, TWR/MWR per
period, the display currency, the S&P 500 line, estimated dividends and the
opening-holding rule, through ``investments.book``. Every network call is
stubbed with deterministic prices; public tickers only."""

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from convexity import investments as inv
from convexity import paths

TODAY = date.today()
# The last trading day: on a weekend the book's latest close is Friday's.
LAST = TODAY - timedelta(days=max(0, TODAY.weekday() - 4))


def _weekdays(start: date, end: date = TODAY) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _ago(days: int) -> str:
    """An ISO weekday about ``days`` ago (rolled back to a Friday)."""
    d = TODAY - timedelta(days=days)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


class Market:
    """Deterministic prices: each symbol grows at its own daily rate from a
    start price; FX is USD per one unit, also a smooth function of the day."""

    def __init__(self, prices=None, fx=None, splits=None, divs=None, holidays=(), wiggle=0.0):
        self.prices = prices or {}  # sym -> (start_date, start_price, daily growth)
        self.holidays = set(holidays)  # weekdays with no close anywhere
        self.wiggle = wiggle  # +/- this, alternating by day, on top of the growth
        self.fx = fx or {}  # ccy -> (rate at the start of 2020, daily growth)
        self.splits = splits or {}
        self.divs = divs or {}  # sym -> [(iso, per share)]

    def px(self, sym, d: date) -> float:
        start, p0, g = self.prices[sym]
        k = (d - start).days
        return p0 * (1 + g) ** k * (1 + self.wiggle * (1 if k % 2 else -1))

    def usd(self, ccy, d: date) -> float:
        if ccy == "USD":
            return 1.0
        r0, g = self.fx[ccy]
        return r0 * (1 + g) ** (d - date(2020, 1, 1)).days

    def install(self, mp, quote=None):
        quote = quote or {}
        inv._SPLITS.clear()
        inv._SPLITS_GOOD.clear()
        inv._PAGE_MEMO.clear()
        inv._QCCY.clear()
        mp.setattr(inv, "_fetch_quote_ccy", lambda s: quote.get(s, "USD"))
        mp.setattr(inv, "_pack_hit", lambda t: None)
        mp.setattr(inv, "_resolve", lambda text: text.strip().upper())
        mp.setattr(inv, "_fetch_splits", lambda s: self.splits.get(s, []))
        mp.setattr(inv, "_fx_now", lambda c, b: self.usd(c, TODAY) / self.usd(b, TODAY))

        def closes(symbols, period):
            cols = {}
            for s in symbols:
                if s in self.prices:
                    ds = [d for d in _weekdays(self.prices[s][0]) if d not in self.holidays]
                    cols[s] = pd.Series([self.px(s, d) for d in ds], index=pd.to_datetime(ds))
            return pd.DataFrame(cols)

        mp.setattr(inv, "_closes", closes)
        mp.setattr(
            inv,
            "_bench_closes",
            lambda period, ticker: (
                closes([ticker], period)[ticker] if ticker in self.prices else None
            ),
        )
        mp.setattr(inv, "_rf_closes", lambda period: None)
        mp.setattr(
            inv,
            "_dividends",
            lambda s: pd.Series(
                [a for _, a in self.divs.get(s, [])],
                index=pd.to_datetime([d for d, _ in self.divs.get(s, [])]),
                dtype=float,
            ),
        )
        mp.setattr(
            inv,
            "_fx_mult",
            lambda ccys, disp, dates: {
                c: np.array(
                    [
                        self.usd(c, date.fromisoformat(d)) / self.usd(disp, date.fromisoformat(d))
                        for d in dates
                    ]
                )
                for c in ccys
            },
        )


def _book(entries, instruments=None):
    raw = {"version": 1, "rev": 1, "settings": {"base_ccy": "USD"},
           "instruments": instruments or {}, "journal": [], "redo": [],
           "entries": [{"id": f"e{i}", "source": "manual", **e} for i, e in enumerate(entries)]}  # fmt: skip
    path = paths.state_file("investments")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")


USD_INST = {"name": "x", "quote_ccy": "USD", "ccy": "USD"}


def _at(b, iso):
    return b["series"]["dates"].index(iso)


def test_nvda_quick_add_from_2023_is_valued_across_the_split(monkeypatch):
    """A quick-add of 100 NVDA (today's shares) dated 2023 and a contract-note
    buy of 10 pre-split shares the same day hold the same thing: Yahoo's
    split-adjusted closes x today's shares, before and after 2024-06-10."""
    m = Market(prices={"NVDA": (date(2023, 1, 2), 15.0, 0.001)},
               splits={"NVDA": [("2024-06-10", 10.0)]})  # fmt: skip
    m.install(monkeypatch)
    quick = {"date": "2023-06-01", "type": "buy", "symbol": "NVDA", "qty": 100, "price": 40.0,
             "fee": 0, "ccy": "USD", "qty_basis": "current", "source": "quick"}  # fmt: skip
    note = quick | {"qty": 10, "price": 400.0, "qty_basis": "trade", "source": "manual"}
    for e in (quick, note):
        _book([e], {"NVDA": USD_INST})
        b = inv.book()
        for iso in ("2024-06-07", "2024-06-11", _ago(0)):
            i = _at(b, iso)
            px = m.px("NVDA", date.fromisoformat(iso))
            assert b["series"]["value"][i] == pytest.approx(100 * px, abs=0.01)
        assert b["positions"][0]["shares"] == 100


def test_quick_add_return_starts_at_its_since_day_value(monkeypatch):
    m = Market(prices={"AAA": (date(2024, 1, 1), 100.0, 0.0005)})
    m.install(monkeypatch)
    since = _ago(400)
    entry = {"date": since, "type": "buy", "symbol": "AAA", "qty": 10, "price": 20.0, "fee": 0,
             "ccy": "USD", "qty_basis": "current", "source": "quick"}  # fmt: skip
    _book([entry], {"AAA": USD_INST})
    b = inv.book()
    h = b["headline"]
    growth = m.px("AAA", LAST) / m.px("AAA", date.fromisoformat(since)) - 1
    assert h["twr"]["ALL"] / 100 == pytest.approx(growth, rel=1e-9)
    # The average-price gap stays in lifetime Total profit.
    assert h["profit_total"] == pytest.approx(10 * m.px("AAA", LAST) - 200, abs=0.01)


def test_a_book_fully_in_spy_matches_the_spy_line(monkeypatch):
    m = Market(prices={"SPY": (date(2023, 1, 2), 380.0, 0.0004)})
    m.install(monkeypatch)
    day = _ago(800)
    prev = date.fromisoformat(day) - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    entries = [
        {"date": day, "type": "deposit", "amount": 10000.0, "ccy": "USD"},
        {"date": day, "type": "buy", "symbol": "SPY", "qty": 25, "price": m.px("SPY", prev),
         "fee": 0, "ccy": "USD", "qty_basis": "trade"},
    ]  # fmt: skip
    _book(entries, {"SPY": USD_INST})
    h = inv.book()["headline"]
    for p in ("1M", "3M", "YTD", "1Y"):
        assert h["twr"][p] == pytest.approx(h["bench_twr"][p], abs=1e-9)


def test_the_display_currency_values_every_day_at_its_own_rate(monkeypatch):
    """Viewed in GBP, a USD book's return is the USD return times the move in
    USD per GBP; with a flat rate the two views agree exactly."""
    entries = [
        {"date": _ago(500), "type": "deposit", "amount": 10000.0, "ccy": "USD"},
        {"date": _ago(500), "type": "buy", "symbol": "AAA", "qty": 50, "price": 100.0, "fee": 0,
         "ccy": "USD", "qty_basis": "trade"},
    ]  # fmt: skip
    for g, same in ((0.0, True), (0.0002, False)):
        m = Market(prices={"AAA": (date(2024, 1, 1), 100.0, 0.0003)}, fx={"GBP": (1.25, g)})
        m.install(monkeypatch)
        _book(entries, {"AAA": USD_INST})
        usd, gbp = inv.book("USD"), inv.book("GBP")
        assert gbp["ccy"] == "GBP"
        s0 = gbp["series"]["dates"][gbp["series"]["period_start"]["1Y"]]
        move = m.usd("GBP", date.fromisoformat(s0)) / m.usd("GBP", LAST)
        want = (1 + usd["headline"]["twr"]["1Y"] / 100) * move - 1
        assert gbp["headline"]["twr"]["1Y"] / 100 == pytest.approx(want, rel=1e-9)
        if same:
            assert gbp["headline"]["twr"]["1Y"] == pytest.approx(usd["headline"]["twr"]["1Y"])
        # Profit is value less the money put in, each at its own day's rate;
        # the USD cash (half the book) moves with the dollar, and that part of
        # the profit is the currency on cash, so the breakdown still adds up.
        h = gbp["headline"]
        dep = 10000 / m.usd("GBP", date.fromisoformat(_ago(500)))
        assert h["net_deposits"] == pytest.approx(dep)
        assert h["profit_total"] == pytest.approx(h["value"] - dep)
        assert h["cash"] == pytest.approx(5000 / m.usd("GBP", LAST))
        parts = h["profit_held"] + h["profit_sold"] + h["dividends"] - h["fees"] + h["cash_fx"]
        assert parts == pytest.approx(h["profit_total"])
        cash_fx = 5000 / m.usd("GBP", LAST) - dep / 2
        assert h["cash_fx"] == pytest.approx(cash_fx, abs=1e-6)
        assert usd["headline"]["cash_fx"] == 0.0


def test_estimated_dividends_count_as_paid_out_never_as_value(monkeypatch):
    m = Market(prices={"AAA": (date(2024, 1, 1), 100.0, 0.0)})
    m.install(monkeypatch)
    buy = {"date": _ago(300), "type": "buy", "symbol": "AAA", "qty": 10, "price": 100.0, "fee": 0,
           "ccy": "USD", "qty_basis": "trade"}  # fmt: skip
    _book([buy], {"AAA": USD_INST})
    plain = inv.book()
    m.divs = {"AAA": [(_ago(200), 1.0), (_ago(100), 1.0)]}
    _book([buy, {"date": _ago(195), "type": "dividend", "symbol": "AAA", "amount": 10.0,
                 "ccy": "USD"}], {"AAA": USD_INST})  # fmt: skip
    b = inv.book()
    h = b["headline"]
    assert h["dividends_est"] == pytest.approx(10.0)  # the second payout only
    assert h["value"] == pytest.approx(
        plain["headline"]["value"] + 10.0
    )  # the recorded one is cash
    assert h["profit_total"] == pytest.approx(h["value"] - h["net_deposits"] + 10.0)
    assert b["positions"][0]["dividends_est"] == pytest.approx(10.0)
    # A flat price: the return is the two dividends alone, 10 on 1,000 then 10
    # on 1,010 (the recorded one stayed as cash): exactly 2%.
    assert h["twr"]["ALL"] / 100 == pytest.approx(0.02, rel=1e-9)
    # The unrecorded payout is offered for the rail, never booked.
    (sug,) = b["suggested_dividends"]
    assert sug["symbol"] == "AAA" and sug["date"] == _ago(100) and sug["qty"] == pytest.approx(10)
    assert sug["per_share"] == pytest.approx(1.0) and sug["amount"] == pytest.approx(10.0)
    # Contributions: the dividends are AAA's, and with no fees and a flat
    # price nothing is left for cash; the parts add up to the TWR.
    for p in inv.PERIODS:
        c = h["contrib"][p]
        assert sum(c.values()) == pytest.approx(h["twr"][p], abs=1e-9)
    assert h["contrib"]["ALL"]["AAA"] == pytest.approx(2.0, abs=1e-9)
    assert h["contrib"]["ALL"][inv.CASH_BUCKET] == pytest.approx(0.0, abs=1e-9)


def test_a_young_book_reads_since_you_started(monkeypatch):
    m = Market(prices={"AAA": (date(2024, 1, 1), 100.0, 0.0002)})
    m.install(monkeypatch)
    _book([{"date": _ago(60), "type": "buy", "symbol": "AAA", "qty": 1, "price": 100.0, "fee": 0,
            "ccy": "USD", "qty_basis": "trade"}], {"AAA": USD_INST})  # fmt: skip
    h = inv.book()["headline"]
    assert h["since_start"]["1Y"] and h["since_start"]["3M"] and not h["since_start"]["1M"]
    assert not h["since_start"]["ALL"]
    assert h["twr"]["1Y"] == pytest.approx(h["twr"]["ALL"])
    assert h["years"]["1Y"] < 1  # so the page shows the period figure, not "/ yr"


def test_unknown_display_currency_falls_back_to_the_base(monkeypatch):
    Market(prices={"AAA": (date(2024, 1, 1), 100.0, 0.0)}, fx={"GBP": (1.25, 0.0)}).install(
        monkeypatch
    )
    _book([{"date": _ago(30), "type": "deposit", "amount": 100.0, "ccy": "USD"}])
    assert inv.book("XXX")["ccy"] == "USD"
    assert inv.book("gbp")["ccy"] == "GBP"
    assert inv.book("<script>")["ccy"] == "USD"


def test_no_prices_at_all_means_no_chart_not_made_up_numbers(monkeypatch):
    Market().install(monkeypatch)
    _book([{"date": _ago(30), "type": "buy", "symbol": "ZZZ", "qty": 1, "price": 10.0, "fee": 0,
            "ccy": "USD", "qty_basis": "trade"}], {"ZZZ": USD_INST})  # fmt: skip
    b = inv.book()
    assert "series" not in b and b["headline"]["twr"]["1Y"] is None
    assert b["positions"][0]["note"]  # valued at the last trade price, and says so


def test_a_display_currency_without_rates_falls_back_to_the_base(monkeypatch):
    """No JPY rates at all is not a broken book: it is shown in USD, and says so."""
    Market(prices={"AAA": (date(2024, 1, 1), 100.0, 0.0004)}).install(monkeypatch)
    monkeypatch.setattr(
        inv,
        "_fx_mult",
        lambda ccys, disp, dates: {c: np.ones(len(dates)) if c == disp else None for c in ccys},
    )
    monkeypatch.setattr(inv, "_fx_now", lambda c, b: None)
    _book([{"date": _ago(20), "type": "buy", "symbol": "AAA", "qty": 1, "price": 50.0, "fee": 0,
            "ccy": "USD", "qty_basis": "trade"}], {"AAA": USD_INST})  # fmt: skip
    b = inv.book("JPY")
    assert "broken" not in b and b["ccy"] == "USD"
    assert any("No JPY rates" in w for w in b["warnings"])


def test_each_periods_spy_line_starts_at_your_value_that_day(monkeypatch):
    """Owner, 2026-10-11: at the left edge of every pill the two lines meet.
    Money put in after the start buys the S&P too, so a deposit inside the
    window keeps them comparable."""
    m = Market(prices={"AAA": (date(2023, 1, 2), 50.0, 0.0008),
                       "SPY": (date(2023, 1, 2), 380.0, 0.0003)})  # fmt: skip
    m.install(monkeypatch)
    _book([
        {"date": _ago(700), "type": "buy", "symbol": "AAA", "qty": 100, "price": 60.0, "fee": 0,
         "ccy": "USD", "qty_basis": "current", "source": "quick"},
        {"date": _ago(20), "type": "deposit", "amount": 5000.0, "ccy": "USD"},
    ], {"AAA": USD_INST, "SPY": USD_INST})  # fmt: skip
    b = inv.book()
    s = b["series"]
    for p in inv.PERIODS:
        i0 = s["period_start"][p]
        line = s["bench_value"][p]
        assert len(line) == len(s["dates"]) - i0
        assert line[0] == pytest.approx(s["value"][i0], abs=0.01)
    # In the 1M window the 5,000 goes in on its day: the line jumps by it.
    i0, j = s["period_start"]["1M"], _at(b, _ago(20))
    one = s["bench_value"]["1M"]
    spy = lambda iso: m.px("SPY", date.fromisoformat(iso))  # noqa: E731
    prev = s["dates"][j - 1]
    assert one[j - i0] == pytest.approx(
        one[j - 1 - i0] * spy(s["dates"][j]) / spy(prev) + 5000 * spy(s["dates"][j]) / spy(prev),
        abs=0.01,
    )


def test_a_book_in_spy_with_estimated_dividends_matches_its_spy_line(monkeypatch):
    """Estimated dividends leave your value (paid out), so the S&P line pays
    the same amount out: a book that is only SPY sits on its own line. The
    line is bought on total-return closes (each payout reinvested), the book
    valued on as-traded ones."""
    divs = [(_ago(250), 1.5), (_ago(160), 1.6), (_ago(70), 1.7)]
    m = Market(prices={"SPY": (date(2023, 1, 2), 380.0, 0.0004)}, divs={"SPY": divs})
    m.install(monkeypatch)
    traded = inv._closes(["SPY"], "max")["SPY"]
    factor = pd.Series(1.0, index=traded.index)
    for iso, d in divs:
        factor[factor.index >= pd.Timestamp(iso)] *= 1 + d / traded[pd.Timestamp(iso)]
    monkeypatch.setattr(inv, "_bench_closes", lambda period, ticker: traded * factor)
    _book([{"date": _ago(400), "type": "buy", "symbol": "SPY", "qty": 20, "price": 400.0, "fee": 0,
            "ccy": "USD", "qty_basis": "current", "source": "quick"}], {"SPY": USD_INST})  # fmt: skip
    b = inv.book()
    s = b["series"]
    assert b["headline"]["dividends_est"] > 0
    for p in ("1Y", "ALL"):
        i0 = s["period_start"][p]
        for k in (0, len(s["bench_value"][p]) // 2, len(s["bench_value"][p]) - 1):
            assert s["bench_value"][p][k] == pytest.approx(s["value"][i0 + k], rel=1e-9)


def test_change_in_value_counts_the_money_put_in(monkeypatch):
    m = Market(prices={"AAA": (date(2023, 1, 2), 50.0, 0.0005)})
    m.install(monkeypatch)
    _book([
        {"date": _ago(500), "type": "deposit", "amount": 10000.0, "ccy": "USD"},
        {"date": _ago(40), "type": "deposit", "amount": 2500.0, "ccy": "USD"},
        {"date": _ago(10), "type": "withdrawal", "amount": 500.0, "ccy": "USD"},
    ])  # fmt: skip
    b = inv.book()
    h, s = b["headline"], b["series"]
    for p in inv.PERIODS:
        i0 = s["period_start"][p]
        assert h["value_change"][p] == pytest.approx(s["value"][-1] - s["value"][i0], abs=1e-6)
    assert h["net_in"]["3M"] == pytest.approx(2000.0)
    assert h["net_in"]["1M"] == pytest.approx(-500.0)
    assert h["net_in"]["ALL"] == pytest.approx(12000.0)


def test_risk_figures_use_trading_days_and_the_portfolio_tabs_formulas(monkeypatch):
    holiday = date.fromisoformat(_ago(100))
    m = Market(prices={"AAA": (date(2023, 1, 2), 50.0, 0.0003)}, wiggle=0.01, holidays=[holiday])
    m.install(monkeypatch)
    _book([{"date": _ago(600), "type": "buy", "symbol": "AAA", "qty": 10, "price": 50.0, "fee": 0,
            "ccy": "USD", "qty_basis": "current", "source": "quick"}], {"AAA": USD_INST})  # fmt: skip
    b = inv.book()
    h, s = b["headline"], b["series"]
    i0 = s["period_start"]["1Y"]
    days = [date.fromisoformat(d) for d in s["dates"][i0:]]
    px = np.array([m.px("AAA", d) for d in days if d != holiday])
    r = px[1:] / px[:-1] - 1
    vol = r.std(ddof=1) * np.sqrt(252) * 100
    assert h["vol"]["1Y"] == pytest.approx(vol, rel=1e-6)
    assert h["sharpe"]["1Y"] == pytest.approx(
        r.mean() * 252 / (r.std(ddof=1) * np.sqrt(252)), rel=1e-6
    )
    down = np.sqrt((np.clip(r, None, 0) ** 2).mean())
    assert h["sortino"]["1Y"] == pytest.approx(r.mean() * 252 / (down * np.sqrt(252)), rel=1e-6)
    assert h["max_dd"]["1Y"] < 0


def test_too_few_days_give_no_risk_figures(monkeypatch):
    Market(prices={"AAA": (date(2023, 1, 2), 50.0, 0.0003)}).install(monkeypatch)
    _book([{"date": _ago(5), "type": "buy", "symbol": "AAA", "qty": 1, "price": 50.0, "fee": 0,
            "ccy": "USD", "qty_basis": "trade"}], {"AAA": USD_INST})  # fmt: skip
    h = inv.book()["headline"]
    assert h["vol"]["ALL"] is None and h["sharpe"]["ALL"] is None


def test_an_imported_holding_starts_at_its_market_value(monkeypatch):
    """The 2026-10-11 bug: Apply dropped ``opening``, so an imported holding
    was bought at its average price on its date and the gap to the market
    read as that day's return. Books saved then (source "import", today's
    shares, no flag) read as opening holdings too."""
    m = Market(prices={"AAA": (date(2023, 1, 2), 100.0, 0.0004)})
    m.install(monkeypatch)
    since = _ago(300)
    row = {"date": since, "type": "buy", "symbol": "AAA", "qty": 10, "price": 250.0, "fee": 0,
           "ccy": "USD", "qty_basis": "current"}  # fmt: skip
    growth = m.px("AAA", LAST) / m.px("AAA", date.fromisoformat(since)) - 1
    _book([row], {"AAA": USD_INST})
    raw = json.loads(paths.state_file("investments").read_text())
    raw["entries"][0]["source"] = "import"  # saved without the flag, as before the fix
    paths.state_file("investments").write_text(json.dumps(raw))
    assert inv.book()["headline"]["twr"]["ALL"] / 100 == pytest.approx(growth, rel=1e-9)


def test_a_picked_benchmark_is_priced_in_its_own_currency(monkeypatch):
    """Owner, 2026-10-11: compare with something other than the S&P 500. The
    Euro Stoxx 50 fund trades in euros, so a USD view moves with EUR too, and
    its line still starts at your value."""
    from convexity import persistence

    m = Market(prices={"AAA": (date(2023, 1, 2), 50.0, 0.0004),
                       "SXRT.DE": (date(2023, 1, 2), 150.0, 0.0002)},
               fx={"EUR": (1.10, 0.0001)}, wiggle=0.004)  # fmt: skip
    m.install(monkeypatch)
    persistence.save_prefs({"inv_benchmark": "STOXX50"})
    _book([{"date": _ago(500), "type": "buy", "symbol": "AAA", "qty": 100, "price": 50.0, "fee": 0,
            "ccy": "USD", "qty_basis": "current", "source": "quick"}], {"AAA": USD_INST})  # fmt: skip
    b = inv.book("USD")
    assert b["benchmark"] == {"key": "STOXX50", "label": "Euro Stoxx 50"}
    assert [x["key"] for x in b["benchmarks"]] == list(inv.INV_BENCHMARKS)
    s, h = b["series"], b["headline"]
    i0 = s["period_start"]["1Y"]
    d0, d1 = (date.fromisoformat(s["dates"][i]) for i in (i0, -1))
    want = m.px("SXRT.DE", d1) * m.usd("EUR", d1) / (m.px("SXRT.DE", d0) * m.usd("EUR", d0)) - 1
    assert h["bench_twr"]["1Y"] / 100 == pytest.approx(want, rel=1e-9)
    assert s["bench_value"]["1Y"][0] == pytest.approx(s["value"][i0], abs=0.01)
    assert h["bench_vol"]["1Y"] > 0
    assert inv.refresh_symbols() == ["AAA", "SXRT.DE"]


# ---------------------------------------------- holdings depth (Phase 4)


def test_contributions_add_up_to_the_twr_and_risk_shares_to_one(monkeypatch):
    m = Market(
        prices={"AAA": (date(2024, 1, 1), 100.0, 0.0004), "BBB": (date(2024, 1, 1), 50.0, -0.0002),
                "CCC": (date(2024, 1, 1), 20.0, 0.0001)},
        fx={"USD": (1.0, 0.0), "GBP": (1.25, 0.00005)}, wiggle=0.01,
        divs={"BBB": [(_ago(150), 0.5)]},
    )  # fmt: skip
    m.install(monkeypatch)
    t = {"fee": 2.0, "ccy": "USD", "qty_basis": "trade"}
    _book([
        {"date": _ago(500), "type": "deposit", "amount": 20000.0, "ccy": "USD"},
        {"date": _ago(500), "type": "buy", "symbol": "AAA", "qty": 50, "price": m.px("AAA", date.fromisoformat(_ago(500))), **t},
        {"date": _ago(400), "type": "buy", "symbol": "BBB", "qty": 100, "price": m.px("BBB", date.fromisoformat(_ago(400))), **t},
        {"date": _ago(300), "type": "buy", "symbol": "CCC", "qty": 100, "price": m.px("CCC", date.fromisoformat(_ago(300))), **t},
        {"date": _ago(200), "type": "sell", "symbol": "CCC", "qty": 100, "price": m.px("CCC", date.fromisoformat(_ago(200))), **t},
        {"date": _ago(120), "type": "fee", "amount": 15.0, "ccy": "USD"},
        {"date": _ago(90), "type": "withdrawal", "amount": 1000.0, "ccy": "USD"},
    ], dict.fromkeys(("AAA", "BBB", "CCC"), USD_INST))  # fmt: skip
    for ccy in ("USD", "GBP"):
        h = inv.book(ccy)["headline"]
        for p in inv.PERIODS:
            assert sum(h["contrib"][p].values()) == pytest.approx(h["twr"][p], abs=1e-8), (ccy, p)
        if ccy == "USD":
            # In the base currency the cash bucket is the fees alone: a cost.
            assert h["contrib"]["ALL"][inv.CASH_BUCKET] < 0
    b = inv.book()
    rs = [x["risk_share"] for x in b["positions"]]
    assert len(rs) == 2 and sum(rs) == pytest.approx(1.0)
    (c,) = b["closed"]
    assert c["symbol"] == "CCC" and c["profit"] == pytest.approx(c["profit_sold"])
    bbb = next(x for x in b["positions"] if x["symbol"] == "BBB")
    assert bbb["yoc"] > 0  # one 0.5 payout in the last year, on what it cost


def test_a_young_listing_has_no_risk_share_and_the_rest_still_do(monkeypatch):
    ipo = TODAY - timedelta(days=12)
    m = Market(prices={"AAA": (date(2024, 1, 1), 100.0, 0.0004), "BBB": (date(2024, 1, 1), 50.0, -0.0002),
                       "NEW": (ipo, 10.0, 0.001)}, wiggle=0.01)  # fmt: skip
    m.install(monkeypatch)
    t = {"fee": 0, "ccy": "USD", "qty_basis": "trade"}
    _book([
        {"date": _ago(400), "type": "buy", "symbol": "AAA", "qty": 10, "price": 100.0, **t},
        {"date": _ago(400), "type": "buy", "symbol": "BBB", "qty": 10, "price": 50.0, **t},
        {"date": _ago(5), "type": "buy", "symbol": "NEW", "qty": 10, "price": 10.0, **t},
    ], dict.fromkeys(("AAA", "BBB", "NEW"), USD_INST))  # fmt: skip
    rs = {x["symbol"]: x["risk_share"] for x in inv.book()["positions"]}
    assert rs["NEW"] is None and rs["AAA"] + rs["BBB"] == pytest.approx(1.0)
