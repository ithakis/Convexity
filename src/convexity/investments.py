"""My Investments: the user's real book (docs/plans/my-investments-roadmap.md).

This module owns ``state/investments.json`` and turns it into the page
payload. The maths lives in ``ledger.py`` (pure, no I/O); this file does the
I/O around it: the store, undo/redo, instruments, prices and FX.

The book file holds the user's real money, so the store is defensive:

- every read-modify-write runs under one lock, and every write is atomic
  (``persistence._atomic_write``) after copying the old file to ``.bak``;
- **a malformed file is never overwritten** (``BookUnreadable`` -> 409): the
  user may be able to repair it, and a silent "start again" would lose it;
- every mutation carries the ``rev`` the page last saw; a stale one is a 409,
  so two windows can never overwrite each other's edits;
- every mutation replays the whole candidate ledger before it is saved, so
  the file never holds a book the engine refuses (an oversell, say);
- logs name the operation, the entry id and the symbol, never amounts:
  users paste logs into public issues.

Performance (TWR, MWR, the chart) arrives in Phase 3. Until then the payload
leaves those keys empty (``None`` / no ``series``) and the page shows calm
gaps: a real book is never shown made-up numbers.
"""

import contextvars
import json
import math
import re
import secrets
import threading
import time
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from convexity import ledger, paths
from convexity.helpers import SUPPORTED_FX, major_ccy, price_in_major

SCHEMA_VERSION = 1
JOURNAL_MAX = 100
_LOCK = threading.Lock()

PERIODS = ("1M", "3M", "YTD", "1Y", "3Y", "ALL")
BENCH = "SPY"
# What the chart can compare against (owner, 2026-10-11: the Portfolios
# tab's list plus MSCI World). Each is a fund, not the index, so its
# adjusted close carries the dividends: Yahoo's ^STOXX50E, ^N225 and ^KS11
# are price indices that would flatter the book by their yield.
# key -> (Yahoo ticker, label, quote currency).
INV_BENCHMARKS = {
    "SPY": ("SPY", "S&P 500", "USD"),
    "QQQ": ("QQQ", "Nasdaq-100", "USD"),
    "STOXX50": ("SXRT.DE", "Euro Stoxx 50", "EUR"),
    "N225": ("1321.T", "Nikkei 225", "JPY"),
    "KOSPI": ("069500.KS", "KOSPI 200", "KRW"),
    "WORLD": ("URTH", "MSCI World", "USD"),
}


def bench_key() -> str:
    """The benchmark the owner picked (state/prefs.json), else the S&P 500.
    Imported here, not at the top: persistence reads INV_BENCHMARKS back."""
    from convexity.persistence import load_prefs

    k = load_prefs().get("inv_benchmark")
    return k if k in INV_BENCHMARKS else BENCH


_PERIOD_MONTHS = {"1M": 1, "3M": 3, "1Y": 12, "3Y": 36}

SOURCES = ("manual", "quick", "import")  # what the page may send; "demo" is the seed script's
IMPORT_MAX = 500  # entries in one Apply
DIVIDENDS_MAX = 5000  # suggested dividends in one "Add all"
_MAX_NUMBER = 1e12  # far above any real holding; keeps every sum finite (JSON has no inf)
_FIELDS = ("date", "type", "symbol", "qty", "price", "fee", "amount", "tax", "ratio",
           "fx_rate", "ccy", "qty_basis", "note")  # fmt: skip


class BookError(Exception):
    """A refused request. ``status`` is the HTTP status; ``message`` is plain
    words the page shows as is."""

    status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BookUnreadable(BookError):
    status = 409


class StaleRev(BookError):
    status = 409


class Refused(BookError):
    """The ledger engine refused the change (an oversell, a missing FX rate)."""

    status = 422


def period_starts(dates: list[str]) -> dict:
    """Index of each period's base point: the last date on or before its start.

    The server is the only place this rule lives. The page slices the chart
    with these indexes, so the headline return and the end of the chart line
    can never disagree (a JS copy once differed by a day in UTC+ zones).
    Month arithmetic clamps the day to 28, so 31 Mar - 1M is 28 Feb.
    """
    last, out = date.fromisoformat(dates[-1]), {"ALL": 0}
    for p in PERIODS[:-1]:
        if p == "YTD":
            start = date(last.year, 1, 1)
        else:
            m = last.month - _PERIOD_MONTHS[p]
            start = date(last.year + (m - 1) // 12, (m - 1) % 12 + 1, min(last.day, 28))
        out[p] = max((i for i, d in enumerate(dates) if d <= start.isoformat()), default=0)
    return out


# ------------------------------------------------------------------- store


def _path():
    return paths.state_file("investments")


def _new_book() -> dict:
    return {"version": SCHEMA_VERSION, "rev": 0, "settings": {"base_ccy": "USD", "benchmark": "SPY"},
            "instruments": {}, "entries": [], "journal": [], "redo": []}  # fmt: skip


def _load() -> dict | None:
    """The book file, ``None`` when there is none yet. Raises BookUnreadable
    for anything malformed, and never touches the file."""
    path = _path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BookUnreadable(
            "Your investments file couldn't be read. Nothing was changed."
        ) from exc
    try:
        raw = json.loads(text)
    except ValueError:
        raw = None
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise BookUnreadable(
            f"Your investments file ({path.name}) is damaged, so it was left untouched. "
            f"A copy of the previous version is next to it as {path.name}.bak."
        )
    version, rev = raw.get("version", 0), raw.setdefault("rev", 0)
    if (
        not isinstance(version, int)
        or not isinstance(rev, int)
        or not all(
            isinstance(e, dict)
            and isinstance(e.get("date"), str)
            and e.get("type") in ledger.TYPES
            and (e["type"] not in ledger.NEEDS_SYMBOL or isinstance(e.get("symbol"), str))
            for e in raw["entries"]
        )
    ):
        raise BookUnreadable(
            f"Your investments file ({path.name}) has entries Convexity can't read, so it was "
            "left untouched."
        )
    if version > SCHEMA_VERSION:
        raise BookUnreadable(
            "Your investments file was saved by a newer Convexity. Update the app to open it."
        )
    base = _new_book()
    for k in ("settings", "instruments", "journal", "redo"):
        if not isinstance(raw.get(k), type(base[k])):
            raw[k] = base[k]
    return raw


def _save(raw: dict) -> None:
    from convexity.persistence import _atomic_write

    path = _path()
    if path.exists():
        _atomic_write(path.with_name(path.name + ".bak"), path.read_text(encoding="utf-8"))
    _atomic_write(path, json.dumps(raw, indent=1, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -------------------------------------------------------------- market data
# Each fetch is a small function so the tests can stub the network. Every
# cache stores failures only briefly (CLAUDE.md §9: never cache empty forever).

# Split history. A failed fetch is not "no splits" (2026-10-11): yfinance
# swallows a failed download into an empty frame, and caching that as "no
# splits" replayed a pre-split trade at face value, so the ledger oversold
# and a sound book showed as broken. So: _SPLITS times the next try (12 h
# after an answer, a minute after a failure), _SPLITS_GOOD keeps the last
# good list per ticker (a failure never replaces it), and the good lists are
# kept on disk so a restart during a Yahoo outage still has them. A ticker
# with no good list at all is "pending": book() leaves it out of the totals.
_SPLITS: dict[str, tuple[float, bool]] = {}
_SPLITS_GOOD: dict[str, list[tuple[str, float]]] = {}
_SPLITS_TTL, _MISS_TTL = 12 * 3600.0, 60.0
_splits_loaded: str | None = None  # the file the good lists were read from
# Page loads and the refresh job run book() on different threads: the good
# lists change under one lock (never held across a Yahoo call).
_SPLITS_LOCK = threading.RLock()


def _fetch_splits(symbol: str) -> list[tuple[str, float]] | None:
    """Yahoo's splits for ``symbol``; None when Yahoo didn't answer (an
    error, or no price history at all), [] when it answered with none."""
    import yfinance as yf

    try:
        h = yf.Ticker(symbol).history(period="max", actions=True, auto_adjust=False)
    except Exception:
        return None
    if h is None or h.empty:
        return None
    if "Stock Splits" not in h.columns:
        return []
    s = h["Stock Splits"]
    return [(ts.date().isoformat(), float(r)) for ts, r in s.items() if r and float(r) > 0]


def _splits_file():
    return paths.state_file("splits")


def _load_good_splits() -> None:
    """The last good lists from disk, once per data folder; a malformed file
    is ignored (it is only a cache of Yahoo's answers)."""
    global _splits_loaded
    path = _splits_file()
    with _SPLITS_LOCK:
        if _splits_loaded == str(path):
            return
        _splits_loaded = str(path)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            got = {str(sym): [(str(d), float(r)) for d, r in rows if float(r) > 0]
                   for sym, rows in data.items()}  # fmt: skip
        except Exception as exc:
            print(f"[investments] split cache unreadable, ignored: {type(exc).__name__}")
            return
        for sym, rows in got.items():
            _SPLITS_GOOD.setdefault(sym, rows)


def _save_good_splits() -> None:
    from convexity.persistence import _atomic_write

    path = _splits_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _SPLITS_LOCK:
            body = {s: [list(x) for x in v] for s, v in sorted(_SPLITS_GOOD.items())}
            _atomic_write(path, json.dumps(body, separators=(",", ":")) + "\n")
    except OSError as exc:
        print(f"[investments] split cache not saved: {type(exc).__name__}")


def market_splits(symbols, *, fetch: bool = True) -> dict[str, list[tuple[str, float]]]:
    """Yahoo's split history per symbol: the last good list, refetched when
    due. ``fetch=False`` answers from memory and disk only (the cheap symbol
    list at startup). A symbol with no good list is missing from the answer;
    ``splits_unknown`` names it."""
    _load_good_splits()
    out, now, changed = {}, time.time(), False
    for sym in symbols:
        hit = _SPLITS.get(sym)
        due = not hit or now - hit[0] >= (_SPLITS_TTL if hit[1] else _MISS_TTL)
        if fetch and due:
            got = _fetch_splits(sym)
            with _SPLITS_LOCK:
                _SPLITS[sym] = (now, got is not None)
                if got is None:
                    print(f"[investments] split history unavailable: {sym}")
                elif _SPLITS_GOOD.get(sym) != got:
                    _SPLITS_GOOD[sym], changed = got, True
        good = _SPLITS_GOOD.get(sym)
        if good:
            out[sym] = good
    if changed:
        _save_good_splits()
    return out


def splits_unknown(symbols) -> set[str]:
    """Symbols whose split history has never loaded (call after
    ``market_splits``): their trade-basis quantities can't be trusted."""
    _load_good_splits()
    return {s for s in symbols if s not in _SPLITS_GOOD}


def _closes(symbols: list[str], period: str, retry: bool = True):
    """Daily closes as traded, quote unit, ``period`` back. Not
    dividend-adjusted, because the book books dividends as cash (an adjusted
    close would count each one twice); still split-adjusted, like the
    ledger's quantities (roadmap Appendix B)."""
    from convexity.analytics import _bulk_close

    return _bulk_close(symbols, period, adjusted=False, retry=retry)


def _bench_closes(period: str, ticker: str):
    """A benchmark's total-return closes (dividends reinvested), in its
    quote currency."""
    from convexity.analytics import _bulk_close

    df = _bulk_close([ticker], period)
    return df[ticker] if ticker in getattr(df, "columns", ()) else None


def _rf_closes(period: str):
    """The 13-week T-bill yield (^IRX, percent), the risk-free rate of a USD
    view's Sharpe and Sortino, as the Portfolio tab uses (analytics._daily_rf)."""
    from convexity.analytics import _RF_TICKER, _bulk_close

    df = _bulk_close([_RF_TICKER], period)
    return df[_RF_TICKER] if _RF_TICKER in getattr(df, "columns", ()) else None


def _dividends(symbol: str):
    """Yahoo's payouts for a share (ex-date -> per current share, quote
    unit), as fetched with its as-traded closes; None when unknown."""
    from convexity.analytics import dividends_cached

    return dividends_cached(symbol)


def _fx_mult(ccys, disp: str, dates: list[str]) -> dict:
    from convexity.fx import fx_multipliers

    return fx_multipliers(ccys, disp, dates)


def _fx_now(ccy: str, base: str) -> float | None:
    from convexity.fx import convert_amount

    return convert_amount(1.0, ccy, base)


def _dated(series) -> tuple[list[str], list[float]]:
    """A date-indexed series as sorted (ISO dates, values), NaNs dropped."""
    if series is None or len(series) == 0:
        return [], []
    s = series.dropna()
    return [d.date().isoformat() if hasattr(d, "date") else str(d)[:10] for d in s.index], [
        float(v) for v in s.values
    ]


def _make_fx_at(base: str, warnings: list[str], dates):
    """``fx_at(ccy, date)``: base units per one ``ccy`` on that date. The same
    path as the daily series (``fx.fx_multipliers``), read once per currency
    over the ledger's own ``dates``; today's rate, with a warning, when Yahoo
    has no history."""
    dates = sorted(set(dates))
    pos, memo = {d: i for i, d in enumerate(dates)}, {}

    def fx_at(ccy: str, iso: str) -> float | None:
        ccy = major_ccy(ccy)
        if ccy == base:
            return 1.0
        if iso in pos:
            if ccy not in memo:
                memo[ccy] = _fx_mult([ccy], base, dates).get(ccy)
            arr, i = memo[ccy], pos[iso]
        else:
            arr, i = _fx_mult([ccy], base, [iso]).get(ccy), 0
        if arr is not None and arr[i] > 0:
            return float(arr[i])
        spot = _fx_now(ccy, base)
        if spot:
            note = f"No {ccy} history from Yahoo: trades in {ccy} use today's rate."
            if note not in warnings:
                warnings.append(note)
        return spot

    return fx_at


def refresh_symbols() -> list[str]:
    """What a My Investments refresh fetches: every share the ledger names,
    then the benchmark."""
    try:
        raw = _load()
    except BookUnreadable:
        return []
    syms = sorted({e["symbol"] for e in (raw or {}).get("entries", []) if e.get("symbol")})
    return [*syms, INV_BENCHMARKS[bench_key()][0]] if syms else []


def refresh_market(symbols: list[str], *, progress=None, cancel=None, chunk: int = 4) -> None:
    """Drop the book's cached market data, then fetch it again in small
    chunks (the topbar Refresh on My Investments). Each chunk reports
    ``progress(chunk, done, total)``; ``cancel`` (an Event) is checked
    between chunks. Never touches the book file."""
    from convexity import cache

    _PAGE_MEMO.clear()  # the page recomputes with what this fetches
    raw = _load() or _new_book()
    first = min((e["date"] for e in raw["entries"]), default=date.today().isoformat())
    span = _span(first)
    drop = set(symbols)
    for key in [k for k in cache._BULK_CLOSE_CACHE if k[0] in drop]:
        cache._BULK_CLOSE_CACHE.pop(key, None)
    for s in drop:
        cache._DIVIDENDS_CACHE.pop(s, None)
        _SPLITS.pop(s, None)
        _QCCY.pop(s, None)
    for key in [k for k in cache._FX_CCY_HIST_CACHE if k.endswith("|max")]:
        cache._FX_CCY_HIST_CACHE.pop(key, None)
    done = 0
    for i in range(0, len(symbols), chunk):
        if cancel is not None and cancel.is_set():
            return
        part = symbols[i : i + chunk]
        bench = INV_BENCHMARKS[bench_key()][0]
        shares = [s for s in part if s != bench]
        if shares:
            _closes(shares, span)
            market_splits(shares)
        if bench in part:
            _bench_closes(span, bench)
        done += len(part)
        if progress:
            progress(part, done, len(symbols))
    # Earnings and ex-dividend dates for the rail, fetched again in the
    # background (attention.py): one call per share, so the job's 100% isn't
    # held up by them; held shares only.
    from convexity import attention

    try:
        attention.refresh_events_soon(_held_now(raw) or {}, force=True)
    except Exception as exc:
        print(f"[attention] events refresh failed: {type(exc).__name__}")


# -------------------------------------------------------------- instruments


_QCCY: dict[str, str] = {}  # ticker -> Yahoo quote currency; only successes are kept


def _fetch_quote_ccy(symbol: str) -> str | None:
    import yfinance as yf

    try:
        c = yf.Ticker(symbol).fast_info.get("currency")
        return str(c) if c else None
    except Exception:
        return None


def _resolve(text: str) -> str:
    from convexity.resolver import resolve_symbol

    return resolve_symbol(text) or text.strip().upper()


def _pack_hit(ticker: str):
    try:
        from convexity import symbol_db

        return symbol_db.get(ticker)
    except Exception:
        return None


def _yahoo_hit(ticker: str):
    """A listing the pack lacks, as Yahoo's Lookup names it (cached). The
    bare ticker's lookup comes first: it brings the company's other lines,
    whose fuller name the listing borrows ("RICHTER" alone on Budapest)."""
    try:
        from convexity import symbol_db

        hits = [h for q in dict.fromkeys([ticker.split(".")[0], ticker])
                for h in symbol_db.yahoo_lookup(q) if h.ticker == ticker]  # fmt: skip
        return max(hits, key=lambda h: len(h.name.split()), default=None)
    except Exception:
        return None


def ensure_instrument(raw: dict, text: str) -> str:
    """The ticker for ``text`` (a ticker or a name), recorded in
    ``raw["instruments"]`` with its name, exchange and quote currency."""
    text = (text or "").strip()
    if not text or len(text) > 60:
        raise BookError("Choose a company.")
    inst = raw["instruments"]
    if text.upper() in inst:
        return text.upper()
    hit = _pack_hit(text.upper())
    ticker = hit.ticker if hit else _resolve(text)
    if ticker in inst:
        return ticker
    hit = hit or _pack_hit(ticker) or _yahoo_hit(ticker)
    # A listing's quote currency never changes: an import's dry run and its
    # Apply would otherwise ask Yahoo for every new ticker twice, in a row.
    qccy = _QCCY.get(ticker) or _fetch_quote_ccy(ticker)
    if qccy:
        _QCCY[ticker] = qccy
    if not qccy:
        raise BookError(f"Couldn't find {text} on Yahoo Finance. Check the name or ticker.")
    inst[ticker] = {
        "name": (hit.name if hit else None) or ticker,
        "exchange": _exchange_name(hit.exchange) if hit else None,
        "quote_ccy": qccy,
        "ccy": major_ccy(qccy),
    }
    return ticker


def lookup(query: str, limit: int = 6) -> list[dict]:
    """Company typeahead for the Add popover, from the symbol pack; a name
    the pack lacks (Gedeon Richter) from Yahoo's live Lookup."""
    try:
        from convexity import symbol_db

        hits = symbol_db.lookup(query, limit=limit, min_score=60.0)
        if len((query or "").strip()) >= 4 and (not hits or hits[0].score < 85):
            have = {h.ticker for h in hits}
            hits += [h for h in symbol_db.yahoo_lookup(query, limit=limit) if h.ticker not in have]
    except Exception:
        hits = []
    return [
        {"ticker": h.ticker, "name": h.name, "exchange": _exchange_name(h.exchange)} for h in hits
    ]


def _exchange_name(code: str | None) -> str | None:
    """Yahoo's exchange code as people say it ("NMS" -> "NASDAQ")."""
    from convexity.search import EXCHANGE_NAMES

    return EXCHANGE_NAMES.get(code or "", code)


def price_on(symbol: str, iso: str) -> dict:
    """The close on or before ``iso`` as traded that day, so it matches a
    contract note: the Add popover's price auto-fill. Major unit, with the
    quote currency.

    ``auto_adjust=False`` drops the dividend adjustment only: Yahoo's closes
    are always split-adjusted, so every split after the day is multiplied
    back in (NVDA on 1 May 2024 traded near 830, not 83)."""
    import yfinance as yf

    d = date.fromisoformat(iso)
    try:
        h = yf.Ticker(symbol).history(start=(d - timedelta(days=10)).isoformat(),
                                      end=(d + timedelta(days=1)).isoformat(), auto_adjust=False)  # fmt: skip
        col = h["Close"].dropna() if h is not None and not h.empty else None
        raw = float(col.iloc[-1]) if col is not None and len(col) else None
        on = col.index[-1].date().isoformat() if raw is not None else None
        if raw is not None:
            sp = market_splits([symbol])
            # No split history yet: no price beats one off by a split factor.
            raw = (
                None
                if splits_unknown([symbol])
                else raw * ledger.split_factor(sp.get(symbol, []), on)
            )
        qccy = _fetch_quote_ccy(symbol)
    except Exception:
        raw, on, qccy = None, None, None
    return {"symbol": symbol, "date": on, "price": price_in_major(raw, qccy), "quote_ccy": qccy}


# ---------------------------------------------------------------- entries


def _f(v, name: str, *, positive=False, nonneg=False):
    if v is None or v == "":
        return None
    try:
        x = None if isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        x = None
    if x is None or not math.isfinite(x):
        raise BookError(f"{name} must be a number.")
    if abs(x) > _MAX_NUMBER:
        raise BookError(f"{name} is too large.")
    if positive and x <= 0:
        raise BookError(f"{name} must be more than zero.")
    if nonneg and x < 0:
        raise BookError(f"{name} can't be negative.")
    return x


def _clean(raw: dict, fields: dict, old: dict | None = None) -> dict:
    """A validated entry from the page's fields (merged over ``old`` for an
    edit). Prices and amounts are in the major unit of ``ccy``."""
    e = dict(old or {})
    for k in _FIELDS:
        if k in fields:
            e[k] = fields[k]
    t = e.get("type")
    if t not in ledger.TYPES:
        raise BookError("Choose what kind of entry this is.")
    try:
        d = date.fromisoformat(str(e.get("date") or ""))
    except ValueError:
        raise BookError("Enter a date.") from None
    if d > date.today():
        raise BookError("The date can't be in the future.")
    if d.year < 1970:
        raise BookError("The date is too far back.")
    out = dict.fromkeys(_FIELDS)
    out |= {"date": d.isoformat(), "type": t, "note": str(e.get("note") or "")[:200]}
    base = raw["settings"].get("base_ccy", "USD")
    if t in ledger.NEEDS_SYMBOL:
        sym = out["symbol"] = ensure_instrument(raw, str(e.get("symbol") or ""))
        if old and sym != old.get("symbol"):
            # Another company brings its own currency, unless one was sent.
            for k in ("ccy", "fx_rate"):
                e[k] = fields.get(k)
        out["ccy"] = major_ccy(str(e.get("ccy") or raw["instruments"][sym]["ccy"]))
    else:
        out["ccy"] = major_ccy(str(e.get("ccy") or base))
    if not re.fullmatch(r"[A-Z]{3}", out["ccy"]):
        raise BookError("Choose a currency, like USD or EUR.")
    if t in ledger.TRADES:
        out["qty"] = _f(e.get("qty"), "Shares", positive=True)
        out["price"] = _f(e.get("price"), "Price", nonneg=True)
        out["fee"] = _f(e.get("fee"), "Fee", nonneg=True) or 0.0
        if out["qty"] is None or out["price"] is None:
            raise BookError("Enter the shares and the price.")
        out["qty_basis"] = "current" if e.get("qty_basis") == "current" else "trade"
    elif t == "split":
        out["ratio"] = _f(e.get("ratio"), "Ratio", positive=True)
        if not out["ratio"] or out["ratio"] == 1:
            raise BookError("Enter the split ratio, e.g. 10 for a 10-for-1 split.")
    else:
        out["amount"] = _f(e.get("amount"), "Amount", positive=True)
        if out["amount"] is None:
            raise BookError("Enter the amount.")
        if t == "dividend":
            out["tax"] = _f(e.get("tax"), "Tax", nonneg=True) or 0.0
            if out["tax"] >= out["amount"]:
                raise BookError("The tax must be less than the dividend.")
    if out["ccy"] != base:
        out["fx_rate"] = _f(e.get("fx_rate"), "FX rate", positive=True)
    # A holding already owned: performance starts at its value. Read from the
    # page's fields (``e`` only carries _FIELDS), or kept from the old entry.
    if t == "buy" and (fields.get("opening") is True or (old or {}).get("opening") is True):
        out["opening"] = True
    return out


def _label(op: str, e: dict, raw: dict) -> str:
    """Plain words for the journal, the toast and the Undo hover."""
    name = _name(raw, e.get("symbol"))
    if e["type"] in ledger.TRADES:
        name = f"{ledger._qty(e['qty'])} {name}"
    return f"{op} {e['type']}" + (f" · {name}" if name else "")


_TAILS = (" Corporation", " Inc.", " Inc", " plc", " PLC", " Ltd", " N.V.", " NV", " SE", " AG", " S.A.", " SA",
          " S.p.A.", " SpA", " Nyrt.", " A/S", " ASA", " AB")  # fmt: skip


def _short(name: str) -> str:
    """The name without its legal form. The comma before one goes too:
    "Iberdrola, S.A." showed as "Iberdrola," (owner, 2026-10-09)."""
    for tail in _TAILS:
        if name.endswith(tail):
            return name[: -len(tail)].rstrip(" ,")
    return name


def _apply(entries: list[dict], ops: list[dict]) -> list[dict]:
    """Apply journal ops: {"put": entry, "at": i} replaces by id or inserts
    at ``at``; {"del": id} removes."""
    out = list(entries)
    for op in ops:
        if "del" in op:
            out = [e for e in out if e.get("id") != op["del"]]
        else:
            ent = op["put"]
            i = next((k for k, e in enumerate(out) if e.get("id") == ent["id"]), None)
            if i is not None:
                out[i] = ent
            else:
                out.insert(min(op.get("at", len(out)), len(out)), ent)
    return out


def _trade_basis_symbols(entries: list[dict]) -> list[str]:
    """Only trades on the contract-note basis need Yahoo's split history."""
    return sorted(
        {e["symbol"] for e in entries if e.get("qty_basis") == "trade" and e.get("symbol")}
    )


def _split_state(entries: list[dict]) -> tuple[dict, set[str], set[str]]:
    """The splits to replay with; the trade-basis symbols whose split
    history has never loaded ("pending": their quantities can't be
    trusted); and those Yahoo doesn't carry at all, whose trades count as
    entered, as before Phase 4."""
    syms = _trade_basis_symbols(entries)
    splits = ledger.merge_splits(entries, market_splits(syms))
    pending = splits_unknown(syms)
    unlisted = _not_on_yahoo(pending, entries) if pending else set()
    return splits, pending - unlisted, unlisted


def _not_on_yahoo(symbols: set[str], entries: list[dict]) -> set[str]:
    """Symbols Yahoo has no prices for while it does answer for the
    benchmark fund: a ticker it doesn't carry (delisted, or a line it never
    had), whose split history can never load. yfinance reports an outage and
    an unknown ticker the same way ("no timezone"), so the page's own price
    download tells them apart; in an outage the benchmark is missing too and
    the holdings stay pending. Same cached downloads as ``_performance``."""
    span = _span(min(e["date"] for e in entries))
    try:
        bench = _bench_closes(span, INV_BENCHMARKS[bench_key()][0])
        if bench is None or not np.isfinite(np.asarray(bench, dtype=float)).any():
            return set()
        closes = _closes(sorted(symbols), span)
    except Exception:
        return set()
    has = getattr(closes, "columns", ())
    out = {s for s in symbols if s not in has or not closes[s].notna().any()}
    with _SPLITS_LOCK:
        for s in out:  # asked again in 12 h, not every minute
            _SPLITS[s] = (time.time(), True)
    return out


def _without(entries: list[dict], symbols: set[str]) -> list[dict]:
    """The ledger minus every entry of ``symbols``, as if never bought: a
    holding's positions are independent of the others', and its cash flows
    go with it (a shortfall elsewhere is an implied deposit, never an error)."""
    return [e for e in entries if e.get("symbol") not in symbols] if symbols else entries


def _names(raw: dict, symbols) -> str:
    n = [_name(raw, s) for s in sorted(symbols)]
    return n[0] if len(n) == 1 else ", ".join(n[:-1]) + " and " + n[-1]


def _touched(before: list[dict], after: list[dict]) -> set[str]:
    """Symbols of the entries a change adds, removes or edits."""
    old = {e.get("id"): e for e in before}
    new = {e.get("id"): e for e in after}
    return {e.get("symbol") for k in old.keys() | new.keys()
            for e in (old.get(k), new.get(k)) if e and old.get(k) != new.get(k)} - {None}  # fmt: skip


def _check(raw: dict, entries: list[dict], changed: str | None = None) -> None:
    """Replay the candidate ledger. A refusal is explained in plain words:
    about the entry just changed, or about the later entry it breaks (an
    edit that leaves a later sell short of shares).

    A change to a holding whose split history hasn't loaded is refused until
    it has (it can't be checked); the rest of the book replays without it."""
    warnings: list[str] = []
    base = raw["settings"].get("base_ccy", "USD")
    splits, pending, _ = _split_state(entries)
    if hit := pending & _touched(raw["entries"], entries):
        raise Refused(
            f"Yahoo's split history for {_names(raw, hit)} didn't load, so this can't be "
            "checked yet. Try again in a minute."
        )
    entries = _without(entries, pending)
    try:
        ledger.replay(entries, base_ccy=base, splits=splits,
                      fx_at=_make_fx_at(base, warnings, [e["date"] for e in entries]))  # fmt: skip
    except ledger.OversellError as exc:
        name, when = _name(raw, exc.symbol), _when(exc.day)
        sell, have = ledger._qty(exc.sell), ledger._qty(exc.have)
        if changed is not None and exc.entry_id == changed:
            raise Refused(
                f"That sells {sell} {name} on {when}, but you hold {have} then."
            ) from None
        raise Refused(
            f"That would leave your sale of {sell} {name} on {when} selling more than you hold then "
            f"({have}). Change that sale first."
        ) from None
    except ledger.LedgerError as exc:
        bad = next((e for e in entries if e.get("id") == exc.entry_id), None)
        who = _describe(bad, raw) if bad else "An entry"
        raise Refused(f"{who} {exc.message}.") from None


def _name(raw: dict, symbol: str | None) -> str:
    return _short(raw["instruments"].get(symbol or "", {}).get("name") or symbol or "")


def _when(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d:%b %Y}"  # no %-d: Windows strftime lacks it


def _describe(e: dict, raw: dict) -> str:
    name = _name(raw, e.get("symbol"))
    return f"The {e['type']}{' of ' + name if name else ''} on {_when(e['date'])}"


def _check_rev(raw: dict, base_rev) -> None:
    """Every change names the rev the page last saw, so two windows can never
    silently overwrite each other's edits."""
    if type(base_rev) is not int or base_rev != raw["rev"]:
        raise StaleRev("Your investments changed in another window. Reload to see the latest.")


def _mutate(base_rev, change, ccy=None) -> dict:
    """The one read-check-write path, for edits and undo/redo alike.
    ``change(raw)`` updates the journal and returns (new_entries, the id of
    the entry it put, for the refusal wording) or raises BookError; nothing
    is saved unless the candidate ledger replays."""
    with _LOCK:
        raw = _load() or _new_book()
        _check_rev(raw, base_rev)
        entries, changed = change(raw)
        _check(raw, entries, changed)
        raw["entries"] = entries
        raw["rev"] += 1
        _save(raw)
    return page(ccy, fresh=True)


def _record(raw: dict, verb: str, e: dict, undo: list, redo: list):
    """Journal one change (a new change clears redo) and apply it."""
    item = {"label": _label(verb, e, raw), "undo": undo, "redo": redo}
    raw["journal"] = (raw["journal"] + [item])[-JOURNAL_MAX:]
    raw["redo"] = []
    print(f"[investments] {verb.lower()} {e['id']} {e.get('symbol') or e['type']}")
    return _apply(raw["entries"], redo), redo[0].get("put", {}).get("id")


def add(fields: dict, base_rev, ccy=None) -> dict:
    def change(raw):
        e = _clean(raw, fields)
        src = fields.get("source")
        e |= {"id": "e_" + secrets.token_hex(6), "source": src if src in SOURCES else "manual",
              "batch": None, "created_at": _now(), "updated_at": _now()}  # fmt: skip
        return _record(raw, "Added", e, [{"del": e["id"]}], [{"put": e}])

    return _mutate(base_rev, change, ccy)


def _clean_rows(raw: dict, items, source: str = "import", most: int = IMPORT_MAX) -> list[dict]:
    """An import's rows as entries, each cleaned like a hand-added one; the
    first refusal names its row and nothing is kept."""
    if not isinstance(items, list) or not items:
        raise BookError("Nothing to add.")
    if len(items) > most:
        raise BookError(f"Add at most {most} entries at a time.")
    batch, out = "b_" + secrets.token_hex(6), []
    for n, fields in enumerate(items, 1):
        if not isinstance(fields, dict):
            raise BookError(f"Row {n} couldn't be read.")
        try:
            e = _clean(raw, fields)
        except BookError as exc:
            raise BookError(f"Row {n}: {exc.message}") from None
        e |= {"id": "e_" + secrets.token_hex(6), "source": source, "batch": batch,
              "created_at": _now(), "updated_at": _now()}  # fmt: skip
        out.append(e)
    return out


ALIASES_MAX = 300


def import_context() -> dict:
    """What an import reads against, without any network call: the base
    currency, the owner's remembered names, and the shares held now (so a
    holdings screenshot is reconciled, not added twice)."""
    raw = _load() or _new_book()
    aliases = raw["settings"].get("aliases")
    return {"base": raw["settings"].get("base_ccy", "USD"), "held": _held_now(raw) or {},
            "aliases": aliases if isinstance(aliases, dict) else {}}  # fmt: skip


def _held_now(raw: dict) -> dict[str, float] | None:
    """Shares held now, without any network call (cached splits, FX unused);
    None when the ledger can't replay without fresh data."""
    entries = raw["entries"]
    syms = sorted({e["symbol"] for e in entries if e.get("symbol")})
    try:
        b = ledger.replay(entries, base_ccy=raw["settings"].get("base_ccy", "USD"), fx_at=lambda *_: 1.0,
                          splits=ledger.merge_splits(entries, market_splits(syms, fetch=False)))  # fmt: skip
    except ledger.LedgerError:
        return None
    return {p.symbol: p.qty for p in b.open_positions()}


def _remember(raw: dict, aliases, used: set[str]) -> None:
    """Keep the owner's corrections of names ("Shell" is SHEL.L): only for
    tickers this batch actually booked, newest first, at most ALIASES_MAX."""
    from convexity.importer import alias_key

    if not isinstance(aliases, dict):
        return
    keep = dict(raw["settings"].get("aliases") or {})
    for name, sym in list(aliases.items())[:50]:
        k = alias_key(str(name))
        if k and isinstance(sym, str) and sym.upper() in used:
            keep.pop(k, None)
            keep = {k: sym.upper(), **keep}
    raw["settings"]["aliases"] = dict(list(keep.items())[:ALIASES_MAX])


def add_batch(items, base_rev, aliases=None, ccy=None) -> dict:
    """An import's Apply: every entry in ONE journal item, so one Undo
    removes the whole batch (roadmap Phase 5). ``aliases`` are the names the
    owner corrected in the review, remembered for the next import."""

    def change(raw):
        new = _clean_rows(raw, items)
        _remember(raw, aliases, {e["symbol"] for e in new if e.get("symbol")})
        n = len(new)
        item = {"label": f"Imported {n} entr{'y' if n == 1 else 'ies'}",
                "undo": [{"del": e["id"]} for e in new], "redo": [{"put": e} for e in new]}  # fmt: skip
        raw["journal"] = (raw["journal"] + [item])[-JOURNAL_MAX:]
        raw["redo"] = []
        print(f"[investments] import {new[0]['batch']} {n} entries")
        return _apply(raw["entries"], item["redo"]), None

    return _mutate(base_rev, change, ccy)


def add_dividends(items, base_rev, ccy=None) -> dict:
    """The rail's suggested dividends, added on the owner's click (one or
    all): dividend entries only, in one journal item so one Undo takes them
    all back. Each replaces its estimate (the matching window in
    ``ledger.estimate_dividends``)."""

    def change(raw):
        if isinstance(items, list) and any(not isinstance(x, dict) or x.get("type") != "dividend"
                                           for x in items):  # fmt: skip
            raise BookError("Only dividends can be added here.")
        # Twenty years of quarterly payouts on forty holdings is 3,200.
        new = _clean_rows(raw, items, source="suggested", most=DIVIDENDS_MAX)
        n = len(new)
        item = {"label": f"Added {n} dividend{'' if n == 1 else 's'} from Yahoo",
                "undo": [{"del": e["id"]} for e in new], "redo": [{"put": e} for e in new]}  # fmt: skip
        raw["journal"] = (raw["journal"] + [item])[-JOURNAL_MAX:]
        raw["redo"] = []
        print(f"[investments] dividends {new[0]['batch']} {n} entries")
        return _apply(raw["entries"], item["redo"]), None

    return _mutate(base_rev, change, ccy)


_CLEAR_COPIES = 3


def _copy_before_clear() -> None:
    """A dated copy of the book next to it, before Clear empties it: Undo
    brings the entries back, and this survives even a full journal. The
    newest three are kept."""
    from convexity.persistence import _atomic_write

    path = _path()
    if not path.exists():
        return
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    _atomic_write(path.with_name(f"investments.before-clear-{stamp}.json"),
                  path.read_text(encoding="utf-8"))  # fmt: skip
    for old in sorted(path.parent.glob("investments.before-clear-*.json"))[:-_CLEAR_COPIES]:
        old.unlink(missing_ok=True)


def clear(base_rev, ccy=None) -> dict:
    """Settings -> Clear my investments: every entry out in ONE journal
    item, so Undo on My Investments puts them all back. Settings and the
    remembered import names stay."""

    def change(raw):
        n = len(raw["entries"])
        if not n:
            raise BookError("There is nothing to clear.")
        _copy_before_clear()
        item = {"label": f"Cleared {n} entr{'y' if n == 1 else 'ies'}",
                "undo": [{"put": e, "at": i} for i, e in enumerate(raw["entries"])],
                "redo": [{"del": e["id"]} for e in raw["entries"]]}  # fmt: skip
        raw["journal"] = (raw["journal"] + [item])[-JOURNAL_MAX:]
        raw["redo"] = []
        print(f"[investments] cleared {n} entries")
        return [], None

    return _mutate(base_rev, change, ccy)


def check_rows(items) -> dict[int, dict]:
    """A dry run of an import against the book as it is: {row index: problem}
    for rows the ledger would refuse, so the review flags them before Apply.
    Rows go in date order and a refused row is left out of the rest, so one
    problem doesn't hide another. Nothing is saved."""
    raw = _load() or _new_book()
    base = raw["settings"].get("base_ccy", "USD")
    probe = {**raw, "instruments": dict(raw["instruments"])}
    rows: list[dict] = []
    problems: dict[int, dict] = {}
    if not isinstance(items, list) or len(items) > IMPORT_MAX:
        raise BookError(f"Send a list of at most {IMPORT_MAX} entries.")
    for i, fields in enumerate(items):
        if not isinstance(fields, dict):
            problems[i] = {"kind": "invalid", "message": "This row couldn't be read."}
            continue
        try:
            rows.append(_clean(probe, fields) | {"id": f"r{i}"})
        except BookError as exc:
            problems[i] = {"kind": "invalid", "message": exc.message}
    kept = list(raw["entries"])
    warnings: list[str] = []
    # A holding whose split history hasn't loaded can't be checked: its rows
    # say so (never a false oversell), and the rest replays without it.
    splits, pending, _ = _split_state(kept + rows)
    for e in rows:
        if e.get("symbol") in pending:
            problems[int(e["id"][1:])] = {
                "kind": "splits_pending",
                "message": "Yahoo's split history for this share didn't load. Try again in a minute.",
            }
    kept, rows = _without(kept, pending), _without(rows, pending)
    # The usual case, everything books, costs one replay; only a refusal
    # pays for the row-by-row pass that finds which rows are at fault.
    whole = kept + rows
    dates = [e["date"] for e in whole]
    try:
        ledger.replay(whole, base_ccy=base, fx_at=_make_fx_at(base, warnings, dates), splits=splits)
        return problems
    except ledger.LedgerError:
        pass
    for e in sorted(rows, key=lambda x: x["date"]):
        trial, i = kept + [e], int(e["id"][1:])
        try:
            ledger.replay(
                trial, base_ccy=base, fx_at=_make_fx_at(base, warnings, dates), splits=splits
            )
        except ledger.OversellError as exc:
            if exc.entry_id == e["id"]:
                # The ledger counts today's shares; the statement the owner is
                # looking at counts shares as of the trade (5 NVIDIA in Feb
                # 2024 are 50 today), so the words use the statement's.
                f = 1.0
                if e.get("qty_basis") == "trade":
                    f = ledger.split_factor(splits.get(e["symbol"], []), e["date"]) or 1.0
                name, short = _name(probe, e["symbol"]), (exc.sell - exc.have) / f
                problems[i] = {"kind": "oversell", "short": short,
                               "message": f"Sells {ledger._qty(exc.sell / f)} {name} on {_when(e['date'])}, "
                                          f"but the book holds {ledger._qty(exc.have / f)} then."}  # fmt: skip
            else:
                problems[i] = {
                    "kind": "breaks",
                    "message": "It would leave a later sale short of shares.",
                }
            continue
        except ledger.LedgerError as exc:
            problems[i] = {"kind": "invalid", "message": exc.message}
            continue
        kept = trial
    return problems


def _find(raw: dict, entry_id: str) -> tuple[int, dict]:
    for i, e in enumerate(raw["entries"]):
        if e.get("id") == entry_id:
            return i, e
    raise BookError("That entry no longer exists. Reload to see the latest.")


def update(entry_id: str, fields: dict, base_rev, ccy=None) -> dict:
    def change(raw):
        i, old = _find(raw, entry_id)
        new = old | _clean(raw, fields, old) | {"updated_at": _now()}
        return _record(raw, "Edited", new, [{"put": old, "at": i}], [{"put": new}])

    return _mutate(base_rev, change, ccy)


def delete(entry_id: str, base_rev, ccy=None) -> dict:
    def change(raw):
        i, old = _find(raw, entry_id)
        return _record(raw, "Deleted", old, [{"put": old, "at": i}], [{"del": entry_id}])

    return _mutate(base_rev, change, ccy)


def _step(base_rev, frm: str, to: str, ccy=None) -> dict:
    """Undo (journal -> redo) or redo (redo -> journal)."""

    def change(raw):
        if not raw[frm]:
            raise BookError("Nothing to undo." if frm == "journal" else "Nothing to redo.")
        item = raw[frm].pop()
        raw[to] = (raw[to] + [item])[-JOURNAL_MAX:]
        print(f"[investments] {'undo' if frm == 'journal' else 'redo'}")
        return _apply(raw["entries"], item["undo" if frm == "journal" else "redo"]), None

    return _mutate(base_rev, change, ccy)


def undo(base_rev, ccy=None) -> dict:
    return _step(base_rev, "journal", "redo", ccy)


def redo(base_rev, ccy=None) -> dict:
    return _step(base_rev, "redo", "journal", ccy)


# ---------------------------------------------------------------- payload


def _cash_effect(e: dict) -> float | None:
    """The entry's own cash movement in its own currency (for Activity)."""
    t, n = e["type"], (lambda k: float(e.get(k) or 0.0))
    if t == "buy":
        return -(n("qty") * n("price") + n("fee"))
    if t == "sell":
        return n("qty") * n("price") - n("fee")
    if t == "dividend":
        return n("amount") - n("tax")
    if t == "deposit":
        return n("amount")
    if t in ("withdrawal", "fee"):
        return -n("amount")
    return None


def book_symbols() -> list[str]:
    """Open positions, without any network call: the Portfolio tab's pinned
    book pill needs only this at startup. Splits come from the cache; if the
    replay can't run without fresh ones, every bought symbol stands in."""
    try:
        raw = _load()
    except BookUnreadable:
        return []
    if not raw or not raw["entries"]:
        return []
    held = _held_now(raw)
    if held is None:
        return sorted({e["symbol"] for e in raw["entries"] if e.get("type") == "buy"})
    return sorted(held)


def _display_ccy(ccy, base: str) -> str:
    c = str(ccy or "").strip().upper()
    return c if c in SUPPORTED_FX else base


def _span(first: str) -> str:
    """The shortest Yahoo range that covers the whole ledger."""
    years = (date.today() - date.fromisoformat(first)).days / 365.25
    for limit, period in ((0.9, "1y"), (1.9, "2y"), (4.9, "5y"), (9.9, "10y")):
        if years < limit:
            return period
    return "max"


def _calendar(entries: list[dict]) -> list[str]:
    """Every weekday from the one before the first entry to today, plus any
    weekend entry dates. The first point is the zero day (nothing invested
    yet), so the ALL period includes the first day's own move."""
    d = date.fromisoformat(min(e["date"] for e in entries)) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    out, today = {e["date"] for e in entries}, date.today()
    while d <= today:
        if d.weekday() < 5:
            out.add(d.isoformat())
        d += timedelta(days=1)
    return sorted(out)


def _on_dates(series, keys) -> np.ndarray:
    """A date-indexed series read on each calendar day (the last value on or
    before it, NaN before the series starts)."""
    dates, vals = _dated(series)
    if not dates:
        return np.full(len(keys), np.nan)
    pos = np.searchsorted(np.array(dates, dtype="datetime64[D]"), keys, side="right") - 1
    return np.where(pos >= 0, np.array(vals)[np.maximum(pos, 0)], np.nan)


def _rebase_fx(entries: list[dict], base: str, disp: str, fx_at) -> list[dict]:
    """Entries' own ``fx_rate`` is in the book's base currency; viewed in
    another currency it is carried over at that day's base-to-display rate."""
    out = []
    for e in entries:
        if e.get("fx_rate") and (e.get("ccy") or base) not in (base, disp):
            r = fx_at(base, e["date"])
            e = e | {"fx_rate": float(e["fx_rate"]) * r if r else None}
        out.append(e)
    return out


def _annualise(r: float, days: int) -> float | None:
    """A period return as a yearly rate, in percent; None when undefined or
    too large to mean anything (a few days of a big move)."""
    try:
        return ((1 + r) ** (365.0 / days) - 1) * 100 if days > 0 and r > -1 else None
    except OverflowError:
        return None


_RISK_MIN_DAYS = 10  # fewer daily returns than this: no volatility or ratios


def _risk(idx, traded, i0: int, keys, rf_daily) -> dict:
    """Volatility, Sharpe, Sortino and max drawdown of the TWR index from
    ``i0`` on, over trading days only, with the Portfolio tab's formulas
    (analytics._stats: x252 / sqrt 252, excess returns over ^IRX in USD,
    0 otherwise). Percent for volatility and drawdown."""
    from convexity.analytics import _stats

    sel = np.flatnonzero(traded[i0:]) + i0
    if not len(sel) or sel[0] != i0:
        sel = np.concatenate([[i0], sel])
    if len(sel) <= _RISK_MIN_DAYS:
        return {"vol": None, "sharpe": None, "sortino": None, "max_dd": None}
    ix = pd.DatetimeIndex(keys[sel])
    val = pd.Series(np.asarray(idx, dtype=float)[sel], index=ix)
    ret = val.pct_change().iloc[1:]
    rf = pd.Series(rf_daily[sel], index=ix) if rf_daily is not None else None
    st = _stats(ret, val, rf)
    return {"vol": st.get("ann_vol"), "sharpe": st.get("sharpe"), "sortino": st.get("sortino"),
            "max_dd": st.get("max_dd")}  # fmt: skip


def _performance(raw: dict, b: ledger.Book, entries: list[dict], base: str, disp: str,
                 splits: dict, warnings: list[str], bench: str = "SPY") -> dict:  # fmt: skip
    """The daily book in the display currency: value, the money put in, the
    benchmark (``INV_BENCHMARKS[bench]``, the S&P 500 unless the owner picked
    another) bought with the same money, and the TWR index (roadmap Phase 3).

    Every day is valued with that day's as-traded close and that day's FX
    rate, so a GBP view shows what the pound did too. Performance flows
    follow Appendix B (inflows at the start of the day, outflows at the end)
    with two rules on top: an opening holding enters at its market value
    (``ledger.is_opening``), and unrecorded Yahoo dividends count as paid out
    to the owner (``ledger.estimate_dividends``): in returns and profit,
    never in value or cash.

    ``b`` is the replay in the book's base currency: its cash, deposits and
    withdrawals are converted at each day's rate, so uninvested cash moves
    with its currency like the shares do.
    """
    inst = raw["instruments"]
    dates = _calendar(entries)
    keys, n = np.array(dates, dtype="datetime64[D]"), len(dates)
    span = _span(dates[1] if n > 1 else dates[0])
    syms = sorted(b.positions)
    qccy = {s: inst.get(s, {}).get("quote_ccy") or b.positions[s].ccy or disp for s in syms}
    closes = _closes(syms, span) if syms else None
    if syms and closes is not None and not closes.empty:
        # A holding dated before its listing's first close (a renamed line,
        # a young secondary listing) is priced from the same company
        # elsewhere until then, rather than at the last trade price.
        from convexity.analytics import _when, extend_history

        names = {s: inst.get(s, {}).get("name") for s in syms}
        held_from = {}
        for e in entries:
            if e.get("symbol") in qccy and e["date"] < held_from.get(e["symbol"], "9999"):
                held_from[e["symbol"]] = e["date"]
        closes, proxied = extend_history(
            closes, syms, qccy, names, span, fetch=_closes, need=held_from
        )
        for s, (p, until) in proxied.items():
            warnings.append(f"{names.get(s) or s}: before {_when(until)}, priced from {p}, "
                            "the same company on another exchange.")  # fmt: skip
    has = getattr(closes, "columns", ())
    close = {s: _on_dates(closes[s] if s in has else None, keys) for s in syms}
    trades = ledger.trade_prices(entries, splits)
    b_tick, b_label, b_ccy = INV_BENCHMARKS.get(bench, INV_BENCHMARKS["SPY"])
    ccys = {major_ccy(c) for c in qccy.values()} | {major_ccy(c or disp) for t in trades.values()
                                                    for _, _, c in t} | {"USD", base, b_ccy}  # fmt: skip
    mult = _fx_mult(sorted(ccys), disp, dates)
    for c in sorted(ccys):
        if mult.get(c) is None:
            spot = 1.0 if c == disp else _fx_now(c, disp)
            mult[c] = np.full(n, spot) if spot else None
            if spot and c != "USD":
                warnings.append(f"No {c} history from Yahoo: {c} amounts use today's rate.")

    # Display-currency value of one current share on each day: the close, or
    # the last trade price on days the share has no close yet.
    vps, last_close = {}, {}
    for s in syms:
        m = mult.get(major_ccy(qccy[s]))
        v = close[s] * price_in_major(1.0, qccy[s]) * m if m is not None else np.full(n, np.nan)
        for d, px, c in trades.get(s, []):
            fm = mult.get(major_ccy(c or disp))
            if fm is None:
                continue
            i = int(np.searchsorted(keys, np.datetime64(d)))
            gap = np.isnan(v[i:])
            v[i:][gap] = px * fm[i:][gap]
        vps[s] = v
        finite = close[s][~np.isnan(close[s])]
        last_close[s] = float(finite[-1]) if len(finite) else None

    cr = mult[base] if mult.get(base) is not None else np.ones(n)  # display per base unit
    value, cash = np.zeros(n), np.zeros(n)
    d_in, w_out = np.zeros(n), np.zeros(n)  # the money actually put in and taken out
    perf_in = np.zeros(n)  # the same, with opening holdings at their market value
    open_mv = np.zeros(n)  # of which opening holdings, in at the day's close
    implied = 0.0
    # Per holding, for its contribution: shares held, money into it (fees
    # left out) and opening holdings at the day's value, display currency.
    qty_s = {s: np.zeros(n) for s in syms}
    flow_s = {s: np.zeros(n) for s in syms}
    open_s = {s: np.zeros(n) for s in syms}
    j, held, cur_cash = 0, {}, 0.0
    for i, d in enumerate(dates):
        while j < len(b.days) and b.days[j].date <= d:
            day = b.days[j]
            d_in[i] += day.inflow * cr[i]
            w_out[i] += day.outflow * cr[i]
            perf_in[i] += day.inflow * cr[i]
            implied += day.implied * cr[i]
            for s, q, cost in day.openings:
                if np.isfinite(vps[s][i]):
                    perf_in[i] += q * vps[s][i] - cost * cr[i]
                    open_mv[i] += q * vps[s][i]
                    open_s[s][i] += q * vps[s][i]
            for s, f in day.flows.items():
                flow_s[s][i] += f * cr[i]
            held, cur_cash = day.qty, day.cash
            j += 1
        for s, q in held.items():
            qty_s[s][i] = q
        cash[i] = cur_cash * cr[i]
        value[i] = cash[i] + sum(q * vps[s][i] for s, q in held.items() if np.isfinite(vps[s][i]))

    # Dividends Yahoo knows about that the ledger doesn't: paid out to you.
    paid = w_out.copy()
    recorded: dict[str, list[str]] = {}
    for e in entries:
        if e.get("type") == "dividend":
            recorded.setdefault(e["symbol"], []).append(e["date"])
    events = {}
    for s in syms:
        dv = _dividends(s)
        dd, dvals = _dated(dv)
        if dd:
            events[s] = list(zip(dd, dvals, strict=True))
    est_by_sym: dict[str, float] = {}
    est_s = {s: np.zeros(n) for s in syms}
    suggested = []  # each estimate, for the rail's "dividends to add"
    for ex, s, q, per in ledger.estimate_dividends(b, events, recorded):
        i = int(np.searchsorted(keys, np.datetime64(ex)))
        m = mult.get(major_ccy(qccy[s]))
        if i >= n or m is None:
            continue
        amt = float(q * price_in_major(per, qccy[s]) * m[i])
        paid[i] += amt
        est_by_sym[s] = est_by_sym.get(s, 0.0) + amt
        est_s[s][i] += amt
        suggested.append({"symbol": s, "date": ex, "qty": float(q), "ccy": major_ccy(qccy[s]),
                          "per_share": float(price_in_major(per, qccy[s])), "amount": amt})  # fmt: skip

    idx = ledger.twr_index([(d, value[i], perf_in[i], paid[i]) for i, d in enumerate(dates)])
    contrib_daily = _contrib_daily(syms, vps, qty_s, flow_s, open_s, est_s, value, perf_in, paid)

    # The benchmark (total return) bought with the same money on the same days,
    # one line per period: it starts with your value on the period's first
    # day (owner, 2026-10-11: the two lines meet at the left edge), then
    # buys with each inflow at the previous close (an opening holding's value
    # at the day's close, as it enters your value) and sells for each outflow
    # at the day's close. Estimated dividends count as outflows too: they
    # leave your value, so the S&P pays the same amount out.
    starts, last = period_starts(dates), date.fromisoformat(dates[-1])
    spy_raw = _bench_closes(span, b_tick)
    spy = _on_dates(spy_raw, keys)
    bench_value = {}
    if np.isfinite(spy).any() and mult.get(b_ccy) is not None:
        first = spy[np.isfinite(spy)][0]
        spy = np.where(np.isfinite(spy), spy, first) * mult[b_ccy]
        for p in PERIODS:
            i0 = starts[p]
            units, line = value[i0] / spy[i0], np.zeros(n - i0)
            line[0] = value[i0]
            for i in range(i0 + 1, n):
                units += (perf_in[i] - open_mv[i]) / spy[i - 1] + open_mv[i] / spy[i]
                units = max(units - paid[i] / spy[i], 0.0)
                line[i - i0] = units * spy[i]
            bench_value[p] = line
        bench_index = spy / spy[0]
    else:
        bench_value = bench_index = None
        warnings.append(f"No {b_label} prices from Yahoo right now: the comparison is missing.")

    # Trading days, for the risk figures: a weekday with no close anywhere
    # (a holiday) is a flat day that would shrink the volatility.
    traded = np.zeros(n, dtype=bool)
    for col in [spy_raw] + ([closes[c] for c in closes.columns] if len(has) else []):
        dd, _ = _dated(col)
        if dd:
            traded |= np.isin(keys, np.array(dd, dtype="datetime64[D]"))
    rf_daily = None
    if disp == "USD":
        y = _on_dates(_rf_closes(span), keys)
        if np.isfinite(y).any():
            rf_daily = (1.0 + np.nan_to_num(y, nan=float(np.nanmean(y))) / 100.0) ** (1 / 252) - 1

    per = {k: {} for k in ("twr", "bench_twr", "twr_ann", "mwr", "mwr_ann", "years", "since_start",
                           "value_change", "net_in", "vol", "sharpe", "sortino", "max_dd",
                           "bench_vol", "contrib")}  # fmt: skip
    for p in PERIODS:
        i0 = starts[p]
        days = (last - date.fromisoformat(dates[i0])).days
        r = idx[-1] / idx[i0] - 1.0
        per["twr"][p] = r * 100
        per["bench_twr"][p] = (
            (bench_index[-1] / bench_index[i0] - 1) * 100 if bench_index is not None else None
        )
        per["twr_ann"][p] = _annualise(r, days)
        flows = [(dates[t], paid[t] - perf_in[t]) for t in range(i0 + 1, n)]
        ann, rp = ledger.period_mwr(dates[i0], value[i0], flows, dates[-1], value[-1])
        per["mwr"][p] = rp * 100 if rp is not None else None
        per["mwr_ann"][p] = ann * 100 if ann is not None else None
        per["years"][p] = days / 365.0
        per["since_start"][p] = p != "ALL" and i0 == 0
        per["value_change"][p] = value[-1] - value[i0]
        per["net_in"][p] = float(np.sum(d_in[i0 + 1 :] - w_out[i0 + 1 :]))
        for k, v in _risk(idx, traded, i0, keys, rf_daily).items():
            per[k][p] = v
        per["contrib"][p] = _contrib_linked(contrib_daily, i0)
        per["bench_vol"][p] = (
            _risk(bench_index, traded, i0, keys, rf_daily)["vol"]
            if bench_index is not None
            else None
        )

    per = {
        k: {p: float(v) if isinstance(v, float) else v for p, v in d.items()}
        for k, d in per.items()
    }

    # The past month (the 1M pill's window), less the money put in or taken
    # out (opening holdings at their value, estimated dividends as paid out).
    ims = starts["1M"]
    month_abs = value[-1] - value[ims] - perf_in[ims + 1 :].sum() + paid[ims + 1 :].sum()
    month = {"abs": float(month_abs), "pct": float((idx[-1] / idx[ims] - 1) * 100), "ims": ims}

    rnd = lambda a, k: [round(float(x), k) for x in a]  # noqa: E731
    series = {
        "dates": dates,
        "value": rnd(value, 2),
        "invested": rnd(np.cumsum(d_in - w_out), 2),
        "bench_value": {p: rnd(v, 2) for p, v in bench_value.items()} if bench_value else None,
        "twr_index": rnd(idx, 8),
        # Money in less estimated dividends paid out, cumulative: a span's
        # gain is its value change less this one's (the month figure's
        # formula), which the weekly recap reads (notify.py, Phase 9).
        "flows": rnd(np.cumsum(perf_in - paid), 2),
        "bench_index": rnd(bench_index, 8) if bench_index is not None else None,
        "period_start": starts,
    }
    priced = not syms or any(np.isfinite(c).any() for c in close.values())
    return {"series": series if priced else None, "periods": per if priced else None,
            "cash": float(cash[-1]), "net_deposits": float(np.sum(d_in - w_out)),
            "implied": float(implied), "month": month, "close": close, "last_close": last_close, "mult": mult,
            "qccy": qccy, "est": est_by_sym, "suggested": suggested,
            "risk_share": _risk_share(syms, vps, qty_s, traded, starts["1Y"]),
            "xray": {s: _xray(closes[s] if s in has else None, _held_since(qty_s[s], dates), _dividends(s))
                     for s in syms if qty_s[s][-1] > 1e-9}}  # fmt: skip


CASH_BUCKET = "_cash"  # the contribution of cash, fees and currency on cash


def _held_since(qty: np.ndarray, dates: list[str]) -> str:
    """The first day of the current holding (after the last day it was 0)."""
    zero = np.flatnonzero(qty <= 1e-9)
    return dates[int(zero[-1]) + 1] if len(zero) and zero[-1] + 1 < len(dates) else dates[0]


# A daily sigma below this (about 3% a year) is a fund that barely moves: a
# money-market line's every tick would read as dozens of sigmas.
XRAY_SIGMA_FLOOR = 0.002
XRAY_SPIKE = 4.0  # a run of closes this far off both sides' medians is a Yahoo glitch
XRAY_SIDE = 10  # closes either side that the medians read
XRAY_FLIP = (60.0, 140.0)  # off by about x100 with one side only: pence read as pounds


def _xray_clean(pts: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Drop Yahoo glitches a rule would read as a crash or a peak (Phase 3
    found x52 days and pence read as pounds). A close more than
    ``XRAY_SPIKE`` times off the median of the ``XRAY_SIDE`` closes before it
    AND of those after it is a glitch: a run of a few bad days differs from
    both sides, a real level shift (a crash that stays) matches the side
    after it. At either end, with one side only, just a pence/pounds flip
    (about x100) is dropped, so a real -96% last day still shows
    (adversarial review, 2026-10-13)."""
    v = np.array([p[1] for p in pts])
    n, keep = len(v), []
    for i in range(n):
        before, after = v[max(0, i - XRAY_SIDE) : i], v[i + 1 : i + 1 + XRAY_SIDE]
        off = [
            max(v[i] / m, m / v[i]) for m in (np.median(s) for s in (before, after) if len(s) >= 3)
        ]
        if len(off) == 2:
            bad = min(off) > XRAY_SPIKE
        else:
            bad = bool(off) and XRAY_FLIP[0] < off[0] < XRAY_FLIP[1]
        if not bad:
            keep.append(pts[i])
    return keep


def _ex_dates(divs, days: list[str], px: np.ndarray) -> np.ndarray:
    """Each payout per share on the trading day its drop shows: Yahoo dates
    some a day late (a gilts fund fell on the 21st, "ex" on the 22nd) and
    lists some twice a day apart, which read as two false sigma moves. A
    repeat of the same amount within five days is dropped; a payout goes on
    its date or the trading day before, whichever fell more; a non-trading
    ex-date goes to the next trading day. Over a quarter of the price is a
    unit mismatch, not a dividend. Indexed like ``px[1:]``."""
    out = np.zeros(len(px) - 1)
    dd, vals = _dated(divs) if divs is not None else ([], [])
    last: tuple[date, float] | None = None
    for d, a in zip(dd, vals, strict=True):
        if not a or not math.isfinite(a) or a <= 0:
            continue
        day = date.fromisoformat(d)
        if last and (day - last[0]).days <= 5 and abs(a - last[1]) <= 1e-9 * max(a, 1.0):
            continue
        last = (day, a)
        k = int(np.searchsorted(days, d))  # the ex-date, or the next trading day
        if k >= len(days) or k == 0:
            continue
        cands = [c for c in (k, k - 1) if c >= 1]
        k = min(cands, key=lambda c: px[c] / px[c - 1])
        if a < 0.25 * px[k - 1]:
            out[k - 1] += a
    return out


def _xray(series, since: str, divs=None) -> dict:
    """What the Attention rail's X-ray rules read about one holding
    (``attention.py``), from its as-traded closes in its quote currency, on
    its own trading days (never the page's weekday calendar, whose holidays
    are flat days that would shrink the volatility), with each ex-date's
    dividend added back (``divs``, Yahoo's per-share payouts): the book pays
    dividends out, so an ex-date drop is not a loss, and a T-bill fund's
    monthly payout would otherwise read as a 4-sigma fall. So every figure
    is a total return:

    - the last day's move, and that move in sigmas of the year before it
      (daily log returns, the last 252 trading days less that day itself);
    - the last five trading days' move, against the same sigma x sqrt(5)
      (the News read divergence rule);
    - the highest point since the holding was bought, and how far below it
      the last close is.

    Computed with the page, so the rules cost nothing at response time and
    the cached page carries them; {} for a share with too little history."""
    dd, vals = _dated(series) if series is not None else ([], [])
    pts = _xray_clean(
        [(d, v) for d, v in zip(dd, vals, strict=True) if v and math.isfinite(v) and v > 0]
    )
    if len(pts) < 2:
        return {}
    days = [d for d, _ in pts]
    px = np.array([v for _, v in pts])
    gross = (px[1:] + _ex_dates(divs, days, px)) / px[:-1]
    lr = np.log(gross)
    tr = np.concatenate([[1.0], np.cumprod(gross)])
    hist = lr[-253:-1]
    sigma = float(np.std(hist, ddof=1)) if len(hist) >= 60 else None
    sigma = max(sigma, XRAY_SIGMA_FLOOR) if sigma is not None and math.isfinite(sigma) else None
    out = {"day_date": days[-1], "day_pct": (gross[-1] - 1) * 100,
           "day_z": float(lr[-1] / sigma) if sigma else None,
           "sigma_pct": (math.exp(sigma) - 1) * 100 if sigma else None}  # fmt: skip
    if len(px) >= 6:
        out["week_pct"] = (tr[-1] / tr[-6] - 1) * 100
        out["week_z"] = float(math.log(tr[-1] / tr[-6]) / (sigma * math.sqrt(5))) if sigma else None
        out["week_sigma_pct"] = (math.exp(sigma * math.sqrt(5)) - 1) * 100 if sigma else None
    held = [i for i, d in enumerate(days) if d >= since]
    if held:
        k = held[0] + int(np.argmax(tr[held[0] :]))
        out["peak_date"], out["peak_pct"] = days[k], (tr[-1] / tr[k] - 1) * 100
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in out.items()}


def _contrib_daily(syms, vps, qty_s, flow_s, open_s, est_s, value, perf_in, paid) -> pd.DataFrame:
    """Each holding's share of each day's TWR, as a fraction of that day's
    start capital (V_{t-1} + D_t, the TWR's own denominator, Appendix B).

    A holding's day P&L is its value change less the money put into it
    (fees left out), with dividends it paid (recorded or estimated) counted
    as its income; an opening holding enters at that day's value, so its
    first day is 0. What the holdings don't explain is ``CASH_BUCKET``: fees,
    and in another display currency what the currency did to cash. The row
    sums are exactly the TWR's daily returns."""
    n = len(value)
    prev = np.concatenate([[0.0], value[:-1]])
    base = prev + perf_in
    ok = base > 1e-9
    r = np.where(ok, (value + paid) / np.where(ok, base, 1.0) - 1.0, 0.0)
    cols = {}
    for s in syms:
        pv = qty_s[s] * np.nan_to_num(vps[s], nan=0.0)
        pnl = pv - np.concatenate([[0.0], pv[:-1]]) - flow_s[s] - open_s[s] + est_s[s]
        cols[s] = np.where(ok, pnl / np.where(ok, base, 1.0), 0.0)
    df = pd.DataFrame(cols, index=range(n))
    df[CASH_BUCKET] = r - (df.sum(axis=1).to_numpy() if syms else 0.0)
    return df


def _contrib_linked(daily: pd.DataFrame, i0: int) -> dict[str, float]:
    """The period's contributions in percentage points, Carino-linked so
    they add up exactly to the period's TWR (``idx[-1] / idx[i0] - 1``)."""
    from convexity.analytics import _carino_link

    rows = daily.iloc[i0 + 1 :]
    if rows.empty:
        return dict.fromkeys(daily.columns, 0.0)
    # A day that loses everything (r = -100%, a zero close from Yahoo on a
    # one-holding book) has no log link: no figures rather than a 500 or a
    # NaN, which isn't JSON.
    try:
        linked = _carino_link(rows)
    except (ValueError, ZeroDivisionError):
        return dict.fromkeys(daily.columns)
    return {k: v * 100 if math.isfinite(v) else None for k, v in linked.items()}


def _risk_share(syms, vps, qty_s, traded, i0: int) -> dict[str, float]:
    """Each holding's share of the holdings' variance at today's weights:
    RC_i = w_i (Σw)_i / σ², Σ from the last year of daily returns in the
    display currency (Ledoit-Wolf, ``mpt.annualized_cov``). Adds up to 1;
    {} with fewer than 20 shared trading days."""
    from convexity import mpt

    held = [s for s in syms if qty_s[s][-1] > 1e-9 and np.isfinite(vps[s][-1])]
    if not held:
        return {}
    days = np.flatnonzero(traded[i0:]) + i0
    px = pd.DataFrame({s: vps[s][days] for s in held})
    rets = px.pct_change(fill_method=None).iloc[1:]
    # A listing younger than a month (an IPO) is left out rather than
    # cutting everyone's window to its few days; it shows no figure.
    held = [s for s in held if rets[s].notna().sum() >= 20]
    rets = rets[held].dropna(how="any")
    w = np.array([qty_s[s][-1] * vps[s][-1] for s in held])
    if len(held) == 1:
        return {held[0]: 1.0}
    if len(held) < 1 or len(rets) < 20 or w.sum() <= 0:
        return {}
    w = w / w.sum()
    cov = mpt.annualized_cov(rets, "daily", "ledoit").to_numpy()
    sw = cov @ w
    var = float(w @ sw)
    if not np.isfinite(var) or var <= 0:
        return {}
    out = {s: float(w[k] * sw[k] / var) for k, s in enumerate(held)}
    return out if all(math.isfinite(v) for v in out.values()) else {}


def book(ccy: str | None = None) -> dict:
    """The page payload, in the display currency ``ccy`` (the book's base
    currency when it isn't one the app supports). The keys are the ones
    Phase 1 built the page against (roadmap, Phase 1 Findings).

    Everything is in the display currency, every day at its own rate: the
    value and the money put in come from the replay in the book's base
    currency, converted day by day (so cash moves with its currency), and
    each holding's cost and profit from a replay in the display currency.
    The gap between the two is the currency effect on cash (``cash_fx``)."""
    try:
        raw = _load()
    except BookUnreadable as exc:
        return {"empty": True, "error": exc.message}
    if not raw or not raw["entries"]:
        return {"empty": True, "rev": raw["rev"] if raw else 0,
                "undo_label": _top(raw, "journal"), "redo_label": _top(raw, "redo")}  # fmt: skip
    base = raw["settings"].get("base_ccy", "USD")
    disp = _display_ccy(ccy, base)
    inst, entries, warnings = raw["instruments"], raw["entries"], []
    splits, pending, unlisted = _split_state(entries)
    if unlisted:
        warnings.append(f"Yahoo has no history for {_names(raw, unlisted)}: "
                        "trades are counted as you entered them, with no splits.")  # fmt: skip
    if pending:
        # No split history for these yet: leave them out rather than replay a
        # pre-split trade at face value (owner's choice, 2026-10-11). The page
        # reloads itself while ``splits_pending`` is set.
        entries = _without(entries, pending)
        who = _names(raw, pending)
        warnings.append(f"{who} {'is' if len(pending) == 1 else 'are'} left out of the totals "
                        "until Yahoo's split history loads. Retrying every minute.")  # fmt: skip
        if not entries:
            return {"empty": False, "waiting": f"Waiting for Yahoo's split history for {who}. "
                                               "Retrying every minute.",
                    "splits_pending": _pending_rows(raw, pending), "rev": raw["rev"],
                    "ccy": _display_ccy(ccy, base), "entries": _entries_out(raw),
                    "undo_label": _top(raw, "journal"), "redo_label": _top(raw, "redo"),
                    "positions": [], "headline": None, "warnings": warnings}  # fmt: skip
    _prefetch(raw, entries, base, disp)
    try:
        # The book replays in its base currency (cash, deposits, the value);
        # a second replay in the display currency gives each holding's cost and
        # profit at its own days' rates (the price/FX split is against it).
        days = [e["date"] for e in entries]
        b = ledger.replay(entries, base_ccy=base, fx_at=_make_fx_at(base, warnings, days),
                          splits=splits)  # fmt: skip
    except ledger.LedgerError as exc:
        # The stored book no longer replays (new split data, say): show the
        # ledger so it can be fixed, never a half-computed book.
        return {"empty": False, "broken": f"{exc.message}.", "rev": raw["rev"], "ccy": disp,
                "entries": _entries_out(raw), "undo_label": _top(raw, "journal"),
                "redo_label": _top(raw, "redo"), "positions": [], "headline": None}  # fmt: skip
    bd = b
    if disp != base:
        try:
            fx_at = _make_fx_at(disp, warnings, days)
            bd = ledger.replay(_rebase_fx(entries, base, disp, fx_at), base_ccy=disp, fx_at=fx_at,
                               splits=splits)  # fmt: skip
        except ledger.LedgerError:
            # The book is fine; the display currency has no rates right now.
            warnings.append(f"No {disp} rates from Yahoo right now: shown in {base}.")
            disp = base
    bench = bench_key()
    perf = _performance(raw, b, entries, base, disp, splits, warnings, bench)
    ims = perf["month"]["ims"]
    extras = _research_extras(sorted(bd.positions))

    positions, value_pos = [], 0.0
    for p in bd.open_positions():
        meta = inst.get(p.symbol, {})
        name, qccy = meta.get("name") or p.symbol, perf["qccy"][p.symbol]
        last_q = perf["last_close"][p.symbol]
        price, pccy, note = price_in_major(last_q, qccy), major_ccy(qccy), None
        if price is None:
            price, pccy = p.last_price, p.ccy or disp
            note = "No price from Yahoo: valued at your last trade price."
            warnings.append(f"{name}: no price from Yahoo, valued at the last trade price.")
        m_arr = perf["mult"].get(major_ccy(pccy))
        rate = float(m_arr[-1]) if m_arr is not None else None
        if price is None or not rate:
            warnings.append(f"{name}: no price or {pccy} rate, so it is left out of the value.")
            continue
        m = ledger.mark(p, price, pccy, rate)
        ms_q = perf["close"][p.symbol][ims]
        # Prices show in the share's quote unit (pence for LSE), like Yahoo.
        to_quote = 100.0 if qccy != pccy and pccy == major_ccy(qccy) else 1.0
        avg = p.avg_cost_loc if p.ccy == pccy else None
        est = perf["est"].get(p.symbol, 0.0)
        positions.append({
            "symbol": p.symbol, "name": _short(name), "exchange": meta.get("exchange"),
            "quote_ccy": qccy, "shares": round(p.qty, 6),
            "avg_price": avg * to_quote if avg is not None else None,
            "price": price * to_quote, "value": m.value,
            "month_pct": ((last_q / ms_q - 1) * 100 if last_q and ms_q and np.isfinite(ms_q) else None),
            "profit_held": m.unrealised, "profit_sold": p.realised, "dividends": p.dividends,
            "dividends_est": est,
            "profit_price": m.profit_price if pccy != disp else None,
            "profit_fx": m.profit_fx if pccy != disp else None,
            "return_pct": m.unrealised / m.cost * 100 if m.cost > 0 else None,
            "note": note, "profit": m.unrealised + p.realised + p.dividends + est,
            "risk_share": perf["risk_share"].get(p.symbol),
            "yoc": _yield_on_cost(p.symbol, qccy, p.qty, m.cost, perf["mult"]),
            "xray": perf["xray"].get(p.symbol, {}),
            **extras.get(p.symbol, {}),
        })  # fmt: skip
        value_pos += m.value
    value = value_pos + perf["cash"]
    for x in positions:
        x["weight"] = x["value"] / value * 100 if value else None
    positions.sort(key=lambda x: -x["value"])

    # Sold out: what each made, for the table's collapsed "Closed (n)".
    closed = []
    for p in bd.positions.values():
        if p.qty > 1e-9 or p.symbol in pending:
            continue
        est = perf["est"].get(p.symbol, 0.0)
        closed.append({"symbol": p.symbol, "name": _short(inst.get(p.symbol, {}).get("name") or p.symbol),
                       "profit_sold": p.realised, "dividends": p.dividends, "dividends_est": est,
                       "profit": p.realised + p.dividends + est})  # fmt: skip
    closed.sort(key=lambda x: -x["profit"])

    est_total = sum(perf["est"].values())
    suggested = sorted(({**x, "name": _name(raw, x["symbol"])} for x in perf["suggested"]),
                       key=lambda x: x["date"], reverse=True)  # fmt: skip
    per = perf["periods"] or {}
    sold = sum(p.realised for p in bd.positions.values())
    divs = sum(p.dividends for p in bd.positions.values())
    held = sum(x["profit_held"] for x in positions)
    profit_total = value - perf["net_deposits"] + est_total
    # Viewed in another currency, uninvested cash gains or loses with its
    # currency; that is the only part the holdings' own profit doesn't hold.
    cash_fx = profit_total - (held + sold + divs + est_total - bd.fees) if disp != base else 0.0
    out = {
        "demo": all(e.get("source") == "demo" for e in raw["entries"]),
        "empty": False,
        "rev": raw["rev"],
        "ccy": disp,
        "base_ccy": base,
        "benchmark": {"key": bench, "label": INV_BENCHMARKS[bench][1]},
        "benchmarks": [{"key": k, "label": v[1]} for k, v in INV_BENCHMARKS.items()],
        "headline": {
            "value": value,
            "cash": perf["cash"],
            "cash_weight": perf["cash"] / value * 100 if value else None,
            "month_abs": perf["month"]["abs"] if perf["series"] else None,
            "month_pct": perf["month"]["pct"] if perf["series"] else None,
            "profit_total": profit_total,
            "profit_held": held,
            "profit_sold": sold,
            "dividends": divs,
            "dividends_est": est_total,
            "fees": bd.fees,
            "cash_fx": cash_fx,
            "net_deposits": perf["net_deposits"],
            "implied_deposits": perf["implied"],
            **{
                k: per.get(k) or dict.fromkeys(PERIODS)
                for k in (
                    "twr",
                    "bench_twr",
                    "twr_ann",
                    "mwr",
                    "mwr_ann",
                    "years",
                    "since_start",
                    "value_change",
                    "net_in",
                    "vol",
                    "sharpe",
                    "sortino",
                    "max_dd",
                    "bench_vol",
                    "contrib",
                )
            },
        },  # fmt: skip
        "positions": positions + _pending_rows(raw, pending),  # left out: last, no figures
        "entries": _entries_out(raw),
        "undo_label": _top(raw, "journal"),
        "redo_label": _top(raw, "redo"),
        "warnings": warnings,
        "splits_pending": _pending_rows(raw, pending),
        "closed": closed,
        "suggested_dividends": suggested,
    }
    if perf["series"]:
        out["series"] = perf["series"]
    return out


# ------------------------------------------------- the page, fast (2026-10-12)
#
# The page used to wait several seconds on every launch: ``book()`` reads two
# years of prices, FX and the benchmark from Yahoo, every cache is in memory,
# and the page asked twice at once. Three things fix it without ever showing
# a number that wasn't computed from the ledger:
#
# - one computation per display currency at a time (a second caller waits for
#   the first's answer instead of downloading it all again), kept for a minute;
# - the last good page on disk (``state/book_cache.json``), keyed by the book's
#   rev, the display currency and the benchmark: the page opens on it at once,
#   marked as such, while the fresh one computes. Any change bumps the rev, so
#   an old copy never stands in for an edited book;
# - the server computes it once at startup (``server.start_server``), while
#   the window is still loading.

_PAGE_LOCKS: dict[str, threading.Lock] = {}
_PAGE_GUARD = threading.Lock()
_PAGE_MEMO: dict[tuple, tuple[float, dict]] = {}
# The last page computed, healthy or not (a holding still waiting for its
# splits): the Attention rail's routes answer from it, never recompute.
_LAST_PAGE: dict = {"v": (None, None)}  # (key, page), swapped whole: read once, no race
PAGE_MEMO_TTL = 60.0


def _page_key(ccy) -> tuple | None:
    """(rev, display currency, benchmark, day) or None for an unreadable book."""
    try:
        raw = _load()
    except BookUnreadable:
        return None
    base = (raw or _new_book())["settings"].get("base_ccy", "USD")
    return (raw["rev"] if raw else 0, _display_ccy(ccy, base), bench_key(), date.today().isoformat(),
            str(_path()))  # fmt: skip


def _healthy(out: dict) -> bool:
    """A page worth keeping: computed, with prices, nothing left out."""
    return bool(out.get("series")) and not any(
        out.get(k) for k in ("empty", "broken", "waiting", "error", "splits_pending")
    )


def page(ccy: str | None = None, *, fresh: bool = False) -> dict:
    """``book(ccy)`` for the page route: one computation per currency at a
    time, reused for a minute, and saved to disk as the last good page."""
    key = _page_key(ccy)
    lock_key = key[1] if key else "?"
    with _PAGE_GUARD:
        lock = _PAGE_LOCKS.setdefault(lock_key, threading.Lock())
    with lock:
        hit = _PAGE_MEMO.get(key) if key and not fresh else None
        if hit and time.monotonic() - hit[0] < PAGE_MEMO_TTL:
            return with_attention(hit[1])
        t0 = time.monotonic()
        out = book(ccy)
        # When: the weekly recap keeps only a page computed after Friday's
        # close (notify.maybe_recap); the disk copy carries the same stamp.
        out["computed_at"] = _now()
        took = time.monotonic() - t0
        _LAST_PAGE["v"] = (key, out)
        if out.get("positions"):
            print(f"[investments] book computed in {took:.1f}s ({out.get('ccy')})")
        if key and _healthy(out):
            _PAGE_MEMO.clear()  # one book: older keys are stale by definition
            _PAGE_MEMO[key] = (time.monotonic(), out)
            _save_cached_page(key, out)
        return with_attention(out, fresh=key is not None and _healthy(out))


def with_attention(out: dict, *, fresh: bool = False) -> dict:
    """The page with its Attention rail (``attention.evaluate``), a copy:
    the memo and the cached page never hold the rail, which follows the
    thresholds and dismissals at the time of asking. On a fresh, complete
    page, items that stopped firing are forgotten and due event dates are
    fetched in the background."""
    from convexity import attention

    if out.get("empty") or not out.get("positions"):
        return out
    try:
        # Sector and fund come from the symbol pack when asked, not when the
        # page was computed: a first launch computes before the pack has
        # downloaded, and that page is the one saved to disk.
        held = [{**p, **_pack_kind(p["symbol"], p.get("name"))} for p in out["positions"]]
        # Plan levels in today's share terms (a split since they were set),
        # from the split lists already kept: no fetch on the page's path.
        splits = market_splits([p["symbol"] for p in held], fetch=False)
        try:
            plans = attention.split_adjusted(attention.load_plans(), splits)[0]
        except attention.PlansUnreadable:
            plans = None  # evaluate reports it
        att = attention.evaluate({**out, "positions": held}, plans=plans)
        if fresh and not out.get("stale"):
            attention.apply_splits(splits)
            attention.prune(set(att["firing"]), set(att["checked"]))
            # A price alert fires once (Phase 8): kept as fired from the first
            # fresh page that saw it cross, so it stays if the price goes back.
            attention.record_fired(att["fired"])
            attention.refresh_events_soon(
                p["symbol"] for p in out["positions"] if not p.get("pending")
            )
    except Exception as exc:  # the rail is extra: never lose the page over it
        print(f"[attention] evaluate failed: {type(exc).__name__}")
        return out
    return {**out, "attention": att}


def price_now(symbol: str, ccy: str | None = None) -> float | None:
    """A holding's last price in its quote unit, from what is already
    computed (the memo, the last page, the last good page on disk); never
    computes. A plan's "set at" and a price alert's side are fixed against
    this, not a price the page sends (adversarial review, 2026-10-14: a
    stale page's price made an alert fire the moment it was set)."""
    key = _page_key(ccy)
    hit = _PAGE_MEMO.get(key) if key else None
    lk, lp = _LAST_PAGE["v"]
    for out in (hit[1] if hit else None, lp if key and lk == key else None, cached_page(ccy)):
        for p in (out or {}).get("positions") or []:
            if p.get("symbol") == symbol and not p.get("pending") and not p.get("note"):
                v = p.get("price")
                if isinstance(v, int | float) and v > 0:
                    return float(v)
    return None


def computed_page(ccy: str | None = None) -> dict | None:
    """The page as already computed, with its rail (the memo, the last page
    computed, else the last good page on disk); never computes. The weekly
    recap and the notification passes read this (notify.py)."""
    key = _page_key(ccy)
    hit = _PAGE_MEMO.get(key) if key else None
    lk, lp = _LAST_PAGE["v"]
    last = lp if key and lk == key else None
    if hit:
        return with_attention(hit[1])
    if last:
        return with_attention(last)
    return cached_page(ccy)


def held_symbols() -> list[str]:
    """Shares held now, from the ledger alone (no network)."""
    try:
        raw = _load()
    except BookUnreadable:
        return []
    return sorted((_held_now(raw) or {}) if raw else {})


def attention_now(ccy: str | None = None) -> dict:
    """The rail for the topbar badge and the rail's own routes, from what is
    already computed (the memo, the last page computed even if it wasn't
    one to keep, else the last good page on disk); never computes the book.
    With none of those, the rules alone (Settings works without a book) and
    ``pending`` when there is a book whose page isn't computed yet, so the
    badge asks again."""
    from convexity import attention

    key = _page_key(ccy)
    hit = _PAGE_MEMO.get(key) if key else None
    lk, lp = _LAST_PAGE["v"]
    last = lp if key and lk == key else None
    out = hit[1] if hit else last or cached_page(ccy)
    att = with_attention(out).get("attention") if out else None
    if att:
        return att
    # ``page: false``: these are the rules alone, not "nothing needs you"
    # (the rail keeps what it shows; adversarial review, 2026-10-13).
    att = attention.evaluate({"positions": []})
    return {**att, "page": False, "pending": not out and bool(book_symbols())}


def _cache_path():
    return paths.state_file("book_cache")


def _save_cached_page(key: tuple, out: dict) -> None:
    from convexity.persistence import _atomic_write

    try:
        body = {"rev": key[0], "ccy": key[1], "bench": key[2], "computed_at": _now(), "book": out}
        _atomic_write(_cache_path(), json.dumps(body, ensure_ascii=False, separators=(",", ":")))
    except (OSError, TypeError, ValueError) as exc:
        print(f"[investments] saving the last page failed: {type(exc).__name__}")


def cached_page(ccy: str | None = None) -> dict | None:
    """The last good page when it still describes this book (same rev,
    currency and benchmark), marked ``stale`` with when it was computed;
    else None. A malformed file is ignored (it is only a cache)."""
    key = _page_key(ccy)
    if not key:
        return None
    try:
        body = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("book"), dict):
        return None
    if (body.get("rev"), body.get("ccy"), body.get("bench")) != key[:3]:
        return None
    return with_attention({**body["book"], "stale": True, "computed_at": body.get("computed_at")})


def warm(ccy: str | None = None) -> None:
    """Compute the page once in the background at startup (server.py), so
    the window's first request finds prices, FX and numba already loaded."""
    try:
        if book_symbols():
            page(ccy)
    except Exception as exc:  # best effort: the page computes it anyway
        print(f"[investments] warm-up failed: {type(exc).__name__}")


def _prefetch(raw: dict, entries: list[dict], base: str, disp: str) -> None:
    """Download what the page needs in parallel before it is computed in
    order (2026-10-12): the holdings' closes, then the stand-ins already
    known for young listings, in one worker (one ``yf.download`` at a time),
    beside every currency's history in others. The steps after it read the
    same caches, so a failure here only costs the time it took."""
    from concurrent.futures import ThreadPoolExecutor

    from convexity import analytics, fx

    syms = sorted({e["symbol"] for e in entries if e.get("symbol")})
    if not syms:
        return
    inst = raw["instruments"]
    dates = _calendar(entries)
    span = _span(dates[1] if len(dates) > 1 else dates[0])
    ccys = {major_ccy(inst.get(s, {}).get("quote_ccy") or base) for s in syms}
    ccys |= {major_ccy(e["ccy"]) for e in entries if isinstance(e.get("ccy"), str)}
    ccys |= {"USD", base, disp, INV_BENCHMARKS.get(bench_key(), INV_BENCHMARKS["SPY"])[2]}
    stand_ins = sorted({p for s in syms for p in analytics.known_proxies(s)} - set(syms))

    def closes() -> None:
        _closes(syms, span)
        if stand_ins:
            _closes(stand_ins, span, retry=False)
        _bench_closes(span, INV_BENCHMARKS.get(bench_key(), INV_BENCHMARKS["SPY"])[0])

    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            # The notification pass's ``cache.prefer_cached`` is a context
            # variable, which a pool's worker doesn't inherit: without the
            # copy every sold share was downloaded again on each pass
            # (adversarial review, 2026-10-15).
            jobs = [ex.submit(contextvars.copy_context().run, closes),
                    ex.submit(fx.prefetch_usd_series, sorted(ccys))]  # fmt: skip
            for j in jobs:
                j.result()
    except Exception as exc:
        print(f"[investments] prefetch failed: {type(exc).__name__}")


def _yield_on_cost(symbol: str, qccy: str, qty: float, cost: float, mult: dict) -> float | None:
    """The last twelve months of Yahoo's payouts per share, on what the
    holding cost: percent, gross, in the display currency at today's rate."""
    dd, vals = _dated(_dividends(symbol))
    m = mult.get(major_ccy(qccy))
    if not dd or m is None or cost <= 0:
        return None
    since = (date.today() - timedelta(days=365)).isoformat()
    ttm = sum(price_in_major(v, qccy) for d, v in zip(dd, vals, strict=True) if d > since)
    return ttm * qty * float(m[-1]) / cost * 100


_ANALYST_ROWS: tuple = (None, {})  # (views.json mtime, {symbol: row}) - the file is MBs


def _analyst_rows(persistence) -> dict:
    """The saved portfolios' rows that carry an analyst target, the book's
    mirror first; parsed again only when views.json changes."""
    global _ANALYST_ROWS
    try:
        vp = persistence._views_path()
        mtime = (str(vp), vp.stat().st_mtime_ns)
    except OSError:
        mtime = None
    if mtime is not None and _ANALYST_ROWS[0] == mtime:
        return _ANALYST_ROWS[1]
    with persistence._VIEWS_LOCK:
        views = persistence._read_views_raw().get("views") or {}
    if not isinstance(views, dict):
        views = {}
    rows: dict = {}
    for name in ["__book__", *(k for k in views if k != "__book__")]:
        view = views.get(name)
        for r in (view.get("rows") or []) if isinstance(view, dict) else []:
            if isinstance(r, dict) and r.get("symbol") and r.get("target_mean_price") is not None:
                rows.setdefault(r["symbol"], r)
    _ANALYST_ROWS = (mtime, rows)
    return rows


def _research_extras(symbols: list[str]) -> dict[str, dict]:
    """What the research tabs already know about each holding, from cache
    only (never a fetch on this page): the News and Market reads, and the
    analyst target from the Portfolios tab's mirror of the book, else from
    any saved portfolio that holds the share."""
    from convexity import news_sentiment as ns
    from convexity import persistence

    rows = _analyst_rows(persistence)
    out = {}
    for s in symbols:
        r = rows.get(s) or {}
        t, px = r.get("target_mean_price"), r.get("price")
        up = (t / px - 1) * 100 if _num_ok(t) and _num_ok(px) and px > 0 else None
        out[s] = {"news_sentiment": ns.get_cached_sentiment(s), "upside_pct": up}
    return out


_FUND_NAME = re.compile(
    r"\b(ETF|ETC|UCITS|Funds?|iShares|Vanguard|SPDR|Xtrackers|Fundsmith)\b", re.IGNORECASE
)


def _pack_kind(symbol: str, name: str | None = None) -> dict:
    """``sector`` and ``fund`` from the symbol pack (local SQLite, never a
    fetch), for the Attention rail: a fund is diversified already (the
    single-name rule skips it) and has no sector. A listing the pack lacks
    (Yahoo's screener misses some) is judged a fund by its name."""
    from convexity import symbol_db

    try:
        hit = symbol_db.get(symbol)
    except Exception:
        hit = None
    if not hit:
        return {"sector": None, "fund": bool(name and _FUND_NAME.search(name))}
    fund = hit.type in ("etf", "fund")
    return {"sector": None if fund else hit.sector or None, "fund": fund}


def _num_ok(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _pending_rows(raw: dict, pending: set[str]) -> list[dict]:
    """A holdings row per holding left out for want of split history: name
    and ticker only, since even its share count depends on the splits."""
    out = []
    for s in sorted(pending):
        meta = raw["instruments"].get(s, {})
        out.append({"symbol": s, "name": _short(meta.get("name") or s), "exchange": meta.get("exchange"),
                    "quote_ccy": meta.get("quote_ccy"), "pending": True})  # fmt: skip
    return out


def _top(raw: dict | None, key: str) -> str | None:
    return raw[key][-1].get("label") if raw and raw.get(key) else None


def _entries_out(raw: dict) -> list[dict]:
    """The ledger for Activity, newest first, with names and cash effects."""
    inst = raw["instruments"]
    out = []
    for e in reversed(ledger.sort_entries(raw["entries"])):
        meta = inst.get(e.get("symbol") or "", {})
        out.append({k: e.get(k) for k in ("id", *_FIELDS, "source")}
                   | {"name": _short(meta.get("name") or e.get("symbol") or "") or None,
                      "quote_ccy": meta.get("quote_ccy"), "cash": _cash_effect(e)})  # fmt: skip
    return out
