"""My Investments: the user's real book (docs/plans/my-investments-roadmap.md).

Phase 1 is the page shell only. ``book_preview()`` serves the payload shape the
page is built against, so the visual language can be signed off before any
maths exists. It answers in exactly two ways:

- no book file yet -> ``{"empty": True}`` (the page shows its empty state);
- the synthetic demo book from ``scripts/seed_demo_book.py`` -> fixed demo
  figures flagged ``"demo": True`` (the page labels them as demo data).

A real, user-entered book is never shown fake numbers: anything other than
the demo book also answers ``empty`` until Phase 2 computes it for real.
Phases 2-3 replace the demo figures with the ledger engine behind the same
keys, so the frontend does not change shape.
"""

import json
from datetime import date, timedelta

from convexity import paths

# Demo figures, in USD. Positions + cash add up to the value, and weights to
# 100%, so the prototype never shows a number that contradicts another one.
_DEMO_POSITIONS = [
    # symbol, name, exchange, quote_ccy, shares, avg_price (quote units),
    # price (quote units), value_usd, month_pct, profit_held, profit_sold,
    # dividends, profit_price, profit_fx, return_pct, note
    (
        "MSFT",
        "Microsoft",
        "NASDAQ",
        "USD",
        50,
        293.79,
        502.60,
        25130.0,
        3.1,
        10440.0,
        0.0,
        25.5,
        None,
        None,
        71.1,
        None,
    ),
    (
        "NVDA",
        "NVIDIA",
        "NASDAQ",
        "USD",
        120,
        37.93,
        186.75,
        22410.0,
        5.6,
        17860.0,
        6300.0,
        0.0,
        None,
        None,
        392.3,
        "Split 10:1 on 10 Jun 2024",
    ),
    (
        "VWCE.DE",
        "Vanguard FTSE All-World",
        "XETRA",
        "EUR",
        120,
        105.14,
        115.40,
        15870.0,
        1.2,
        2310.0,
        0.0,
        0.0,
        1350.0,
        960.0,
        17.0,
        None,
    ),
    (
        "AAPL",
        "Apple",
        "NASDAQ",
        "USD",
        60,
        126.38,
        258.10,
        15486.0,
        0.8,
        7903.2,
        0.0,
        26.0,
        None,
        None,
        104.5,
        None,
    ),
    (
        "SHEL.L",
        "Shell",
        "LSE",
        "GBp",
        300,
        2541.0,
        2690.0,
        10320.0,
        -2.0,
        530.0,
        0.0,
        0.0,
        410.0,
        120.0,
        5.4,
        None,
    ),
]
_DEMO_VALUE = 93040.0
_DEMO_CASH = _DEMO_VALUE - sum(p[7] for p in _DEMO_POSITIONS)
# Money put in = value - total profit, so the chart's "money you put in" line
# and the Total profit cell can never disagree. Net of the demo's two flows
# (+20,000 and -5,000, below), this is the opening deposit.
_DEMO_PROFIT = sum(p[9] + p[10] + p[11] for p in _DEMO_POSITIONS)
_DEMO_FLOWS = {date(2024, 1, 2): 20000.0, date(2026, 3, 2): -5000.0}
_DEMO_FIRST_DEPOSIT = _DEMO_VALUE - _DEMO_PROFIT - sum(_DEMO_FLOWS.values())


def _demo_noise(n: int, seed: int) -> list[float]:
    """Deterministic standard-normal-ish draws (Irwin-Hall over a fixed LCG).

    No ``random`` module, so the demo chart is identical on every run and
    every machine, which is what makes screenshots comparable.
    """
    out, x = [], seed
    for _ in range(n):
        acc = 0.0
        for _ in range(12):
            x = (x * 1103515245 + 12345) % 2**31
            acc += x / 2**31
        out.append(acc - 6.0)
    return out


def _demo_series() -> dict:
    """Three years of daily points (weekdays), deterministic, ending on the
    demo value. The book and the S&P 500 share a common factor, so they move
    together the way a stock portfolio and the index do."""
    end = date(2026, 10, 2)
    days, d = [], end - timedelta(days=3 * 365)
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    mkt, own = _demo_noise(len(days), 41), _demo_noise(len(days), 120)
    dates, value, invested, bench_value, twr, bench = [], [], [], [], [], []
    cap = v = b = _DEMO_FIRST_DEPOSIT
    t = s = 1.0
    for i, day in enumerate(days):
        if i:
            rb = 0.00055 + 0.0095 * mkt[i]
            r = 0.0007 + 1.1 * 0.0095 * mkt[i] + 0.0055 * own[i]
            v *= 1 + r
            b *= 1 + rb
            t *= 1 + r
            s *= 1 + rb
        flow = _DEMO_FLOWS.get(day, 0.0)
        v, b, cap = v + flow, b + flow, cap + flow
        dates.append(day.isoformat())
        value.append(v)
        invested.append(cap)
        bench_value.append(b)
        twr.append(t)
        bench.append(s)
    k = _DEMO_VALUE / value[-1]  # pin the last point to the headline value
    return {
        "dates": dates,
        "value": [round(x * k, 2) for x in value],
        "invested": invested,
        "bench_value": [round(x * k, 2) for x in bench_value],
        "twr_index": [round(x, 6) for x in twr],
        "bench_index": [round(x, 6) for x in bench],
    }


_PERIOD_MONTHS = {"1M": 1, "3M": 3, "1Y": 12, "3Y": 36}


def _period_returns(series: dict, key: str) -> dict:
    """Return % per period pill, read off the chart's own index.

    Same base rule as the page's invSlice(): the last point on or before the
    period start. The headline can then never disagree with where the chart
    line ends.
    """
    dates = [date.fromisoformat(d) for d in series["dates"]]
    idx, last = series[key], dates[-1]
    out = {}
    for p in ("1M", "3M", "YTD", "1Y", "3Y", "ALL"):
        if p == "ALL":
            i0 = 0
        else:
            if p == "YTD":
                start = date(last.year, 1, 1)
            else:
                m = last.month - _PERIOD_MONTHS[p]
                y, m = last.year + (m - 1) // 12, (m - 1) % 12 + 1
                start = date(y, m, min(last.day, 28))
            i0 = max((i for i, d in enumerate(dates) if d <= start), default=0)
        out[p] = round((idx[-1] / idx[i0] - 1) * 100, 2)
    return out


def _demo_book() -> dict:
    series = _demo_series()
    positions = []
    for (
        sym,
        name,
        exch,
        qccy,
        shares,
        avg,
        px,
        val,
        mpct,
        held,
        sold,
        div,
        ppx,
        pfx,
        ret,
        note,
    ) in _DEMO_POSITIONS:
        positions.append(
            {
                "symbol": sym,
                "name": name,
                "exchange": exch,
                "quote_ccy": qccy,
                "shares": shares,
                "avg_price": avg,
                "price": px,
                "value": val,
                "weight": val / _DEMO_VALUE * 100,
                "month_pct": mpct,
                "profit": held + sold + div,
                "profit_held": held,
                "profit_sold": sold,
                "dividends": div,
                "profit_price": ppx,
                "profit_fx": pfx,
                "return_pct": ret,
                "note": note,
            }
        )
    held = sum(p["profit_held"] for p in positions)
    sold = sum(p["profit_sold"] for p in positions)
    div = sum(p["dividends"] for p in positions)
    return {
        "demo": True,
        "empty": False,
        "ccy": "USD",
        "as_of": "2026-10-02",
        "headline": {
            "value": _DEMO_VALUE,
            "cash": _DEMO_CASH,
            "month_abs": 2140.0,
            "month_pct": 2.4,
            "profit_total": held + sold + div,
            "profit_held": held,
            "profit_sold": sold,
            "dividends": div,
            "twr": _period_returns(series, "twr_index"),
            "bench_twr": _period_returns(series, "bench_index"),
            "mwr_ann": round(
                _period_returns(series, "twr_index")["1Y"] + 1.2, 2
            ),  # demo: good timing
        },
        "positions": positions,
        "series": series,
        "upcoming": [
            {"name": "Microsoft", "what": "Earnings", "date": "2026-10-28"},
            {"name": "Apple", "what": "Ex-dividend", "date": "2026-11-10"},
        ],
        "attention": [],
    }


def book_preview() -> dict:
    """The page payload for Phase 1. Reads the book file, never writes it."""
    path = paths.state_file("investments")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"empty": True}
    except (OSError, ValueError):
        print("[investments] book file unreadable; showing the empty state")
        return {"empty": True}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if entries and all(isinstance(e, dict) and e.get("source") == "demo" for e in entries):
        return _demo_book()
    return {"empty": True}
