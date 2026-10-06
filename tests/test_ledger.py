"""The ledger engine (ledger.py): roadmap Appendix B's acceptance test and
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
    t = ledger.twr(pts)
    mw = ledger.mwr(_flows(b), "2026-01-02", pts[-1][1])
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
    assert ledger.twr(pts) == pytest.approx(0.30)
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
    assert ledger.mwr([("2025-01-02", -100.0)], "2026-01-02", 0.0) is None
    assert ledger.twr([]) is None


def test_qty_at_reads_holdings_at_a_date():
    b = ledger.replay(ACCEPTANCE, **USD)
    q, cash = ledger.qty_at(b, "2025-06-30")
    assert q == {"AAA": 80} and cash == pytest.approx(5980)
    assert ledger.qty_at(b, "2024-12-31") == ({}, 0.0)


def test_a_sell_with_nothing_held_is_an_oversell_not_a_crash():
    # A sliver below the tolerance used to divide by the zero holding.
    with pytest.raises(ledger.OversellError):
        ledger.replay([_e("2025-01-02", "sell", symbol="AAA", qty=1e-12, price=1)], **USD)
