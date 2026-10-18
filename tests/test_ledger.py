"""The ledger engine (ledger.py): the engine-rules acceptance test (backend.md) and
its companions. Synthetic tickers only; no network, no files."""

import pytest

from convexity import ledger

USD = {"base_ccy": "USD", "fx_at": None}


def _e(day, typ, **kw):
    return {"date": day, "type": typ, **kw}


ACCEPTANCE = [
    _e("2025-01-02", "deposit", amount=10000),
    _e("2025-01-02", "buy", symbol="AAA", qty=40, price=100, fee=10),
    _e("2025-04-01", "deposit", amount=5000),
    _e("2025-04-01", "buy", symbol="AAA", qty=40, price=125, fee=10),
    _e("2025-07-01", "sell", symbol="AAA", qty=30, price=130, fee=10),
    _e("2025-10-01", "dividend", symbol="AAA", amount=50),
    _e("2025-10-01", "fee", amount=20),
    _e("2026-01-02", "withdrawal", amount=1000),
]
CLOSES = {
    "2025-01-02": 100,
    "2025-04-01": 125,
    "2025-07-01": 130,
    "2025-10-01": 120,
    "2026-01-02": 140,
}


def _points(book, closes):
    return [
        (d.date, d.cash + sum(q * closes[d.date] for q in d.qty.values()), d.inflow, d.outflow)
        for d in book.days
    ]


def _flows(book):
    return [(d.date, d.outflow - d.inflow) for d in book.days]


def test_acceptance_numbers():
    b = ledger.replay(ACCEPTANCE, **USD)
    p = b.positions["AAA"]
    assert p.cost_base / p.qty == pytest.approx(112.75)
    assert p.realised == pytest.approx(507.50)
    assert p.qty == pytest.approx(50)
    m = ledger.mark(p, 140.0, "USD", 1.0)
    assert m.unrealised == pytest.approx(1362.50)
    assert p.dividends == pytest.approx(50) and b.fees == pytest.approx(20)
    value = m.value + b.cash
    assert value == pytest.approx(15900) and b.net_deposits == pytest.approx(14000)
    assert value - b.net_deposits == pytest.approx(1900)
    # Total profit splits exactly into its parts.
    assert m.unrealised + p.realised + p.dividends - b.fees == pytest.approx(1900)
    pts = _points(b, CLOSES)
    t = ledger.twr_index(pts)[-1] - 1
    mw, _ = ledger.period_mwr("2025-01-02", 0.0, _flows(b), "2026-01-02", pts[-1][1])
    assert t * 100 == pytest.approx(12.629086, abs=5e-7)
    assert mw * 100 == pytest.approx(13.848000, abs=5e-7)
    assert (mw - t) * 100 == pytest.approx(1.2189, abs=5e-5)


def test_trade_and_current_basis_agree_across_a_split():
    splits = {"BBB": [("2025-06-01", 2.0)]}
    trade = [_e("2025-01-02", "buy", symbol="BBB", qty=10, price=100, qty_basis="trade")]
    current = [_e("2025-01-02", "buy", symbol="BBB", qty=20, price=50, qty_basis="current")]
    a = ledger.replay(trade, **USD, splits=splits).positions["BBB"]
    b = ledger.replay(current, **USD, splits=splits).positions["BBB"]
    assert (a.qty, a.cost_base, a.avg_cost_loc) == pytest.approx(
        (b.qty, b.cost_base, b.avg_cost_loc)
    )
    assert a.qty == 20
    # A sale after the split is already in the new shares.
    sell = trade + [_e("2025-07-01", "sell", symbol="BBB", qty=20, price=60, qty_basis="trade")]
    assert ledger.replay(sell, **USD, splits=splits).positions["BBB"].qty == 0


def test_a_trade_on_the_split_day_is_already_in_new_shares():
    assert ledger.split_factor([("2024-06-10", 10.0)], "2024-06-10") == 1.0
    assert ledger.split_factor([("2024-06-10", 10.0)], "2024-06-07") == 10.0


def test_manual_split_entry_wins_over_yahoo_on_the_same_day():
    entries = [_e("2024-06-10", "split", symbol="CCC", ratio=4)]
    merged = ledger.merge_splits(entries, {"CCC": [("2024-06-10", 10.0), ("2020-01-01", 2.0)]})
    assert merged["CCC"] == [("2020-01-01", 2.0), ("2024-06-10", 4.0)]


def test_gbp_holding_splits_profit_into_price_and_fx():
    # 100 shares at 10 GBP when 1 GBP = 0.975 USD; now 1500p and 1.30.
    b = ledger.replay(
        [_e("2025-01-02", "buy", symbol="DDD.L", qty=100, price=10.0, ccy="GBP", fx_rate=0.975)],
        base_ccy="USD", fx_at=None,
    )  # fmt: skip
    p = b.positions["DDD.L"]
    m = ledger.mark(p, 15.0, "GBP", 1.30)
    assert m.unrealised == pytest.approx(975)
    assert m.profit_price == pytest.approx(650)
    assert m.profit_fx == pytest.approx(325)
    assert b.implied == pytest.approx(975)  # a quick-add: the cost became an implied deposit


def test_quick_adds_only_twr_is_the_price_return():
    b = ledger.replay(
        [_e("2025-01-02", "buy", symbol="EEE", qty=10, price=100, qty_basis="current")], **USD
    )
    pts = _points(b, {"2025-01-02": 100}) + [("2026-01-02", 10 * 130.0, 0.0, 0.0)]
    assert ledger.twr_index(pts)[-1] - 1 == pytest.approx(0.30)
    assert b.net_deposits == pytest.approx(1000)


def test_oversell_is_refused():
    with pytest.raises(ledger.OversellError) as ex:
        ledger.replay(
            [_e("2025-01-02", "buy", symbol="FFF", qty=5, price=10, id="b"),
             _e("2025-02-03", "sell", symbol="FFF", qty=6, price=10, id="s")], **USD)  # fmt: skip
    assert ex.value.entry_id == "s" and ex.value.have == 5 and ex.value.sell == 6


def test_an_edit_that_breaks_a_later_sell_is_refused():
    entries = [
        _e("2025-01-02", "buy", symbol="GGG", qty=30, price=10, id="b"),
        _e("2025-03-03", "sell", symbol="GGG", qty=30, price=12, id="s"),
    ]
    ledger.replay(entries, **USD)
    entries[0] = entries[0] | {"qty": 20}
    with pytest.raises(ledger.OversellError) as ex:
        ledger.replay(entries, **USD)
    assert ex.value.entry_id == "s"


def test_same_day_order_lets_a_sale_fund_a_buy_and_a_deposit_come_first():
    entries = [
        _e("2025-01-02", "buy", symbol="HHH", qty=10, price=10),
        _e("2025-01-05", "buy", symbol="III", qty=10, price=12),  # entered before the sell
        _e("2025-01-05", "sell", symbol="HHH", qty=10, price=13),
        _e("2025-01-05", "withdrawal", amount=5),
        _e("2025-01-02", "deposit", amount=100),  # entered last, booked first
    ]
    b = ledger.replay(entries, **USD)
    assert b.implied == 0  # the deposit paid for day one, the sale for the buy
    assert b.cash == pytest.approx(100 - 100 + 130 - 120 - 5)


def test_implied_deposit_covers_a_shortfall_and_counts_as_an_inflow():
    b = ledger.replay(
        [_e("2025-01-02", "deposit", amount=50), _e("2025-01-02", "buy", symbol="JJJ", qty=10, price=10, fee=1)],
        **USD,
    )  # fmt: skip
    assert b.cash == 0 and b.implied == pytest.approx(51)
    assert b.days[0].inflow == pytest.approx(101)
    assert b.net_deposits == pytest.approx(101)


def test_dividend_tax_is_withheld_and_fx_applies():
    b = ledger.replay(
        [_e("2025-01-02", "buy", symbol="KKK", qty=1, price=10, ccy="EUR", fx_rate=1.1),
         _e("2025-05-02", "dividend", symbol="KKK", amount=10, tax=2, ccy="EUR")],
        base_ccy="USD", fx_at=lambda ccy, day: 1.2,
    )  # fmt: skip
    assert b.positions["KKK"].dividends == pytest.approx(9.6)


def test_missing_fx_rate_is_a_ledger_error():
    with pytest.raises(ledger.LedgerError):
        ledger.replay([_e("2025-01-02", "deposit", amount=10, ccy="EUR", id="d")],
                      base_ccy="USD", fx_at=lambda *a: None)  # fmt: skip


def test_mwr_is_none_when_flows_never_change_sign():
    assert ledger.period_mwr("2025-01-01", 0.0, [("2025-01-02", -100.0)], "2026-01-02", 0.0) == (
        None,
        None,
    )
    assert ledger.twr_index([]) == []


def test_qty_at_reads_holdings_at_a_date():
    b = ledger.replay(ACCEPTANCE, **USD)
    q, cash = ledger.qty_at(b, "2025-06-30")
    assert q == {"AAA": 80} and cash == pytest.approx(5980)
    assert ledger.qty_at(b, "2024-12-31") == ({}, 0.0)


def test_a_sell_with_nothing_held_is_an_oversell_not_a_crash():
    # A sliver below the tolerance used to divide by the zero holding.
    with pytest.raises(ledger.OversellError):
        ledger.replay([_e("2025-01-02", "sell", symbol="AAA", qty=1e-12, price=1)], **USD)


# ------------------------------------------------------- Phase 3: performance


def test_acceptance_through_the_daily_index_and_period_mwr():
    """The page's path: a running TWR index from a zero day, and the period
    MWR solver, give the acceptance-test numbers."""
    b = ledger.replay(ACCEPTANCE, **USD)
    pts = [("2024-12-31", 0.0, 0.0, 0.0), *_points(b, CLOSES)]
    idx = ledger.twr_index(pts)
    assert idx[0] == 1.0 and (idx[-1] - 1) * 100 == pytest.approx(12.629086, abs=5e-7)
    flows = [(d, w - i) for d, _v, i, w in pts[1:]]
    ann, per = ledger.period_mwr("2024-12-31", 0.0, flows, "2026-01-02", pts[-1][1])
    # The zero day moves the start, never the rate (XIRR is shift-invariant).
    assert ann * 100 == pytest.approx(13.848000, abs=5e-7)
    span = (366 + 1) / 365.0
    assert per == pytest.approx((1 + ann) ** span - 1)


def test_a_short_period_mwr_never_overflows():
    """A 3-day period's yearly rate is huge; the period rate is solved
    directly, so it is still there (the page shows it under a year)."""
    ann, per = ledger.period_mwr("2026-01-02", 100.0, [], "2026-01-05", 110.0)
    assert per == pytest.approx(0.10)
    assert ann is None or ann > 100


def test_an_opening_holding_is_recorded_for_the_performance_swap():
    quick = _e("2025-03-03", "buy", symbol="QQQ", qty=10, price=50, source="quick")
    opened = _e("2025-03-03", "buy", symbol="RRR", qty=5, price=20, opening=True)
    trade = _e("2025-03-03", "buy", symbol="SSS", qty=1, price=10)
    b = ledger.replay([quick, opened, trade], **USD)
    assert sorted(b.days[0].openings) == [("QQQ", 10, 500.0), ("RRR", 5, 100.0)]
    assert ledger.is_opening(quick) and not ledger.is_opening(trade)
    assert not ledger.is_opening(_e("2025-03-03", "sell", symbol="Q", opening=True))
    # An import's holding saved before Apply kept the flag (2026-10-11).
    legacy = _e("2025-03-03", "buy", symbol="Q", source="import", qty_basis="current")
    assert ledger.is_opening(legacy)
    assert not ledger.is_opening(legacy | {"qty_basis": "trade"})


def test_opening_holding_performance_starts_at_its_market_value():
    """Bought at an average of 50, worth 80 on the Since day, 88 a day later:
    the gap is lifetime profit, never a one-day return (+60%); TWR is +10%."""
    entries = [_e("2025-03-03", "buy", symbol="QQQ", qty=10, price=50, source="quick")]
    b = ledger.replay(entries, **USD)
    day = b.days[0]
    closes = {"2025-03-03": 80.0, "2025-03-04": 88.0}
    swap = sum(q * closes["2025-03-03"] - cost for _s, q, cost in day.openings)
    pts = [
        ("2025-03-02", 0.0, 0.0, 0.0),
        ("2025-03-03", 800.0, day.inflow + swap, 0.0),
        ("2025-03-04", 880.0, 0.0, 0.0),
    ]
    assert (ledger.twr_index(pts)[-1] - 1) == pytest.approx(0.10)
    assert 880.0 - b.net_deposits == pytest.approx(380.0)  # Total profit keeps the gap


def test_estimated_dividends_match_recorded_ones_one_to_one():
    """A monthly payer: three payouts, two recorded (paid ~2 weeks after the
    ex-date). Each recorded dividend covers the nearest payout before it, so
    only the unrecorded one is estimated."""
    entries = [
        _e("2025-01-02", "buy", symbol="MMM", qty=100, price=10),
        _e("2025-02-14", "dividend", symbol="MMM", amount=10),
        _e("2025-04-15", "dividend", symbol="MMM", amount=10),
    ]
    b = ledger.replay(entries, **USD)
    events = {"MMM": [("2025-02-01", 0.1), ("2025-03-01", 0.1), ("2025-04-01", 0.1)]}
    recorded = {"MMM": ["2025-02-14", "2025-04-15"]}
    assert ledger.estimate_dividends(b, events, recorded) == [("2025-03-01", "MMM", 100.0, 0.1)]


def test_a_recorded_dividend_outside_the_window_does_not_cover_a_payout():
    entries = [_e("2025-01-02", "buy", symbol="MMM", qty=10, price=10)]
    b = ledger.replay(entries, **USD)
    events = {"MMM": [("2025-03-01", 0.5)]}
    # 46 days after the ex-date is too late; 6 days before it is too early.
    for late in ("2025-04-16", "2025-02-23"):
        assert ledger.estimate_dividends(b, events, {"MMM": [late]}) == [
            ("2025-03-01", "MMM", 10.0, 0.5)
        ]
    assert ledger.estimate_dividends(b, events, {"MMM": ["2025-02-25"]}) == []


def test_no_estimate_before_the_shares_were_held_or_after_they_were_sold():
    entries = [
        _e("2025-03-01", "buy", symbol="MMM", qty=10, price=10),  # bought ON the ex-date
        _e("2025-05-01", "sell", symbol="MMM", qty=10, price=12),
    ]
    b = ledger.replay(entries, **USD)
    events = {"MMM": [("2025-03-01", 0.5), ("2025-04-01", 0.5), ("2025-06-02", 0.5)]}
    assert ledger.estimate_dividends(b, events, {}) == [("2025-04-01", "MMM", 10.0, 0.5)]


def test_trade_prices_are_per_current_share():
    entries = [
        _e("2024-05-01", "buy", symbol="NVDA", qty=2, price=830, qty_basis="trade", ccy="USD"),
        _e("2025-01-02", "buy", symbol="NVDA", qty=5, price=140, qty_basis="current"),
    ]
    tp = ledger.trade_prices(entries, {"NVDA": [("2024-06-10", 10.0)]})
    assert tp["NVDA"] == [("2024-05-01", pytest.approx(83.0), "USD"), ("2025-01-02", 140.0, None)]
