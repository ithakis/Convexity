"""The My Investments ledger engine: entries in, a book out. No I/O.

Everything the page shows about the real book is derived here by replaying
the ledger from the first entry, every time. Nothing derived is ever stored,
so an edit to a 2023 trade can never leave a stale number behind
(docs/plans/my-investments-roadmap.md, Appendix B):

- **Average cost** (what brokers show). Trade fees are part of the cost;
  a sell realises (proceeds - fee) - average cost x shares.
- **One cash pool in the base currency.** A trade, dividend or cash event in
  another currency settles at its own ``fx_rate`` when it has one, else at
  ``fx_at(ccy, date)``. Cash never goes below zero: any shortfall becomes an
  *implied deposit*, which is what makes a book of quick-added holdings work.
- **Quantities are in today's shares.** A trade entered on its contract-note
  basis (``qty_basis = "trade"``) is scaled by every split after its date;
  ``"current"`` is already in today's shares. Price history from Yahoo is
  split-adjusted the same way, so quantities and closes always agree.
- **Same-day order:** deposit, dividend, sell, buy, fee, withdrawal, so money
  that arrives on a day can pay for that day's buys, and a buy and a sell on
  one day never trip the oversell check in the wrong order.
- **No shorts.** Selling more than is held raises ``OversellError``.

TWR and MWR are pure functions over valuation points; the Appendix B
acceptance test checks them here, and Phase 3 feeds them daily closes.
"""

from dataclasses import dataclass, field
from datetime import date

from scipy.optimize import brentq

TYPES = ("deposit", "dividend", "sell", "buy", "fee", "withdrawal", "split")
_DAY_ORDER = {t: i for i, t in enumerate(TYPES)}
TRADES = ("buy", "sell")
NEEDS_SYMBOL = ("buy", "sell", "dividend", "split")
_EPS = 1e-9


class LedgerError(ValueError):
    """An entry the engine cannot book. ``message`` is plain words for the UI."""

    def __init__(self, message: str, entry_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.entry_id = entry_id


class OversellError(LedgerError):
    """A sell of more shares than the book holds on that day."""

    def __init__(self, symbol: str, day: str, have: float, sell: float, entry_id: str | None):
        self.symbol, self.day, self.have, self.sell = symbol, day, have, sell
        super().__init__(
            f"sells {_qty(sell)} {symbol} on {day}, but only {_qty(have)} are held then", entry_id
        )


def _qty(q: float) -> str:
    return f"{q:,.6f}".rstrip("0").rstrip(".")


@dataclass
class Position:
    symbol: str
    ccy: str | None = None  # the trade currency; None once trades disagree
    qty: float = 0.0
    cost_base: float = 0.0  # open cost in the base currency, fees included
    cost_loc: float = 0.0  # open cost in the trade currency
    realised: float = 0.0
    dividends: float = 0.0  # net of withholding tax, base currency
    last_price: float | None = None  # last trade price per current share, trade ccy

    @property
    def avg_cost_loc(self) -> float | None:
        return self.cost_loc / self.qty if self.qty > _EPS and self.ccy else None


@dataclass
class Day:
    """The book at the end of one ledger date: what TWR and MWR need."""

    date: str
    qty: dict[str, float]
    cash: float
    inflow: float  # deposits + implied deposits, counted at the start of the day
    outflow: float  # withdrawals, counted at the end of the day


@dataclass
class Book:
    base_ccy: str
    positions: dict[str, Position] = field(default_factory=dict)
    cash: float = 0.0
    deposits: float = 0.0
    withdrawals: float = 0.0
    implied: float = 0.0
    fees: float = 0.0  # standalone fees only; trade fees sit in the cost
    days: list[Day] = field(default_factory=list)

    @property
    def net_deposits(self) -> float:
        return self.deposits + self.implied - self.withdrawals

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.qty > _EPS]


def split_factor(splits: list[tuple[str, float]], after: str) -> float:
    """Product of the split ratios strictly after ``after`` (an ISO date).

    Yahoo dates a split on its ex-date: a trade on that day is already in the
    new shares, hence strictly after."""
    f = 1.0
    for d, ratio in splits:
        if d > after:
            f *= ratio
    return f


def merge_splits(
    entries: list[dict], market: dict[str, list[tuple[str, float]]] | None
) -> dict[str, list[tuple[str, float]]]:
    """Yahoo's splits plus the manual ``split`` entries; a manual entry wins
    on the same (symbol, date), so a split is never applied twice."""
    out: dict[str, dict[str, float]] = {s: dict(v) for s, v in (market or {}).items()}
    for e in entries:
        if e.get("type") == "split" and e.get("symbol") and e.get("ratio"):
            out.setdefault(e["symbol"], {})[e["date"]] = float(e["ratio"])
    return {s: sorted(v.items()) for s, v in out.items()}


def sort_entries(entries: list[dict]) -> list[dict]:
    """Date, then the same-day order; ties keep the order they were entered."""
    indexed = list(enumerate(entries))
    indexed.sort(key=lambda ie: (ie[1]["date"], _DAY_ORDER.get(ie[1]["type"], 99), ie[0]))
    return [e for _, e in indexed]


def _num(e: dict, k: str) -> float:
    v = e.get(k)
    return float(v) if v is not None else 0.0


def replay(entries: list[dict], *, base_ccy: str, fx_at, splits: dict | None = None) -> Book:
    """Replay the whole ledger into a Book.

    ``fx_at(ccy, iso_date)`` returns base-currency units per one ``ccy`` and
    is only called for amounts not in the base currency and without their own
    ``fx_rate``. ``splits`` maps symbol -> [(iso_date, ratio)], already merged
    with the manual split entries (``merge_splits``).
    """
    splits = splits or {}
    book = Book(base_ccy=base_ccy)
    cur: Day | None = None

    def fx(e: dict) -> float:
        ccy = e.get("ccy") or base_ccy
        if ccy == base_ccy:
            return 1.0
        if e.get("fx_rate"):
            return float(e["fx_rate"])
        rate = fx_at(ccy, e["date"])
        if not rate:
            raise LedgerError(f"has no {ccy} to {base_ccy} rate for {e['date']}", e.get("id"))
        return float(rate)

    def take_cash(amount: float) -> None:
        # Cash never goes negative: the shortfall was money put in that the
        # ledger doesn't show, so it is booked as an implied deposit.
        if amount > book.cash + _EPS:
            short = amount - book.cash
            book.implied += short
            cur.inflow += short
            book.cash = 0.0
        else:
            book.cash -= amount

    def close_day() -> None:
        if cur is not None:
            cur.qty = {s: p.qty for s, p in book.positions.items() if p.qty > _EPS}
            cur.cash = book.cash
            book.days.append(cur)

    for e in sort_entries(entries):
        if cur is None or cur.date != e["date"]:
            close_day()
            cur = Day(date=e["date"], qty={}, cash=0.0, inflow=0.0, outflow=0.0)
        t = e["type"]
        if t == "split":
            continue  # applied through ``splits``; quantities are in today's shares
        if t == "deposit":
            amt = _num(e, "amount") * fx(e)
            book.cash += amt
            book.deposits += amt
            cur.inflow += amt
        elif t == "withdrawal":
            amt = _num(e, "amount") * fx(e)
            take_cash(amt)
            book.withdrawals += amt
            cur.outflow += amt
        elif t == "fee":
            amt = _num(e, "amount") * fx(e)
            take_cash(amt)
            book.fees += amt
        elif t == "dividend":
            pos = book.positions.setdefault(
                e["symbol"], Position(e["symbol"], e.get("ccy") or base_ccy)
            )
            net = (_num(e, "amount") - _num(e, "tax")) * fx(e)
            book.cash += net
            pos.dividends += net
        elif t in TRADES:
            sym, ccy = e["symbol"], e.get("ccy") or base_ccy
            factor = (
                split_factor(splits.get(sym, []), e["date"])
                if e.get("qty_basis") == "trade"
                else 1.0
            )
            qty = _num(e, "qty") * factor
            price = _num(e, "price") / factor  # per current share, trade ccy
            fee, rate = _num(e, "fee"), fx(e)
            pos = book.positions.setdefault(sym, Position(sym, ccy))
            if pos.qty <= _EPS and pos.ccy != ccy:
                pos.ccy = ccy  # a fresh position may change currency
            elif pos.ccy != ccy:
                pos.ccy = None  # mixed currencies: no local average or FX split
            pos.last_price = price if pos.ccy else None
            if t == "buy":
                gross_loc = qty * price + fee
                take_cash(gross_loc * rate)
                pos.qty += qty
                pos.cost_base += gross_loc * rate
                pos.cost_loc += gross_loc
            else:
                if pos.qty <= _EPS or qty > pos.qty + _EPS:
                    raise OversellError(sym, e["date"], pos.qty, qty, e.get("id"))
                share = qty / pos.qty
                cost_b, cost_l = pos.cost_base * share, pos.cost_loc * share
                proceeds = (qty * price - fee) * rate
                book.cash += proceeds
                pos.realised += proceeds - cost_b
                pos.qty -= qty
                pos.cost_base -= cost_b
                pos.cost_loc -= cost_l
                if pos.qty <= _EPS:
                    pos.qty = pos.cost_base = pos.cost_loc = 0.0
        else:
            raise LedgerError(f"has an unknown type {t!r}", e.get("id"))
    close_day()
    return book


def qty_at(book: Book, iso_date: str) -> tuple[dict[str, float], float]:
    """Holdings and cash at the end of ``iso_date`` (before any later entry)."""
    qty: dict[str, float] = {}
    cash = 0.0
    for d in book.days:
        if d.date > iso_date:
            break
        qty, cash = d.qty, d.cash
    return qty, cash


@dataclass
class Mark:
    """One open position marked to market, all amounts in the base currency."""

    symbol: str
    qty: float
    value: float
    cost: float
    unrealised: float
    profit_price: float | None  # (P*q - C_loc) * FX_now
    profit_fx: float | None  # C_loc * FX_now - C_base


def mark(pos: Position, price: float, price_ccy: str, fx_now: float) -> Mark:
    """Value one position at ``price`` (major unit of ``price_ccy``, per
    current share) with ``fx_now`` base units per one ``price_ccy``.

    The price/FX split (Appendix B) only exists when the position was bought
    in the currency it is quoted in; otherwise both parts are None.
    """
    value = price * pos.qty * fx_now
    unrl = value - pos.cost_base
    split = pos.ccy == price_ccy
    return Mark(
        symbol=pos.symbol,
        qty=pos.qty,
        value=value,
        cost=pos.cost_base,
        unrealised=unrl,
        profit_price=(price * pos.qty - pos.cost_loc) * fx_now if split else None,
        profit_fx=pos.cost_loc * fx_now - pos.cost_base if split else None,
    )


# --------------------------------------------------------------- performance


def twr(points: list[tuple[str, float, float, float]]) -> float | None:
    """Time-weighted return over valuation points (date, value, inflow, outflow).

    r_t = (V_t + W_t) / (V_{t-1} + D_t) - 1: inflows count at the start of the
    day, outflows at the end. The first point's V_{t-1} is 0, so the first
    deposit is the base. Returns a fraction, or None with nothing invested.
    """
    growth, prev, seen = 1.0, 0.0, False
    for _d, v, inflow, outflow in points:
        base = prev + inflow
        if base > _EPS:
            growth *= (v + outflow) / base
            seen = True
        prev = v
    return growth - 1.0 if seen else None


def mwr(flows: list[tuple[str, float]], end: str, end_value: float) -> float | None:
    """Money-weighted return per year (XIRR), as a fraction.

    ``flows`` are (date, amount) from the investor's side: money put in is
    negative, money taken out positive; the end value is the last inflow.
    None when the flows never change sign (nothing to solve for).
    """
    d0 = date.fromisoformat(min([d for d, _ in flows] + [end]))
    cfs = [((date.fromisoformat(d) - d0).days / 365.0, a) for d, a in flows if abs(a) > _EPS]
    cfs.append(((date.fromisoformat(end) - d0).days / 365.0, end_value))
    if not (any(a < 0 for _, a in cfs) and any(a > 0 for _, a in cfs)):
        return None

    def npv(r: float) -> float:
        return sum(a / (1.0 + r) ** t for t, a in cfs)

    try:
        return brentq(npv, -0.9999, 100.0, xtol=1e-12, maxiter=500)
    except ValueError:
        return None
