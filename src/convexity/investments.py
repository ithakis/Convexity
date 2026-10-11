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

_SPLITS: dict[str, tuple[float, list]] = {}
_SPLITS_TTL, _MISS_TTL = 12 * 3600.0, 600.0


def _fetch_splits(symbol: str) -> list[tuple[str, float]] | None:
    import yfinance as yf

    try:
        s = yf.Ticker(symbol).splits
    except Exception:
        return None
    if s is None:
        return None
    return [(ts.date().isoformat(), float(r)) for ts, r in s.items() if r and float(r) > 0]


def market_splits(symbols, *, fetch: bool = True) -> dict[str, list[tuple[str, float]]]:
    """Yahoo's split history per symbol, cached. ``fetch=False`` answers from
    the cache only (the cheap symbol list at startup)."""
    out, now = {}, time.time()
    for sym in symbols:
        hit = _SPLITS.get(sym)
        if hit and now - hit[0] < (_SPLITS_TTL if hit[1] is not None else _MISS_TTL):
            got = hit[1]
        elif fetch:
            got = _fetch_splits(sym)
            _SPLITS[sym] = (now, got)
        else:
            got = None
        if got:
            out[sym] = got
    return out


def _closes(symbols: list[str], period: str):
    """Daily closes as traded, quote unit, ``period`` back. Not
    dividend-adjusted, because the book books dividends as cash (an adjusted
    close would count each one twice); still split-adjusted, like the
    ledger's quantities (roadmap Appendix B)."""
    from convexity.analytics import _bulk_close

    return _bulk_close(symbols, period, adjusted=False)


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
            raw *= ledger.split_factor(market_splits([symbol]).get(symbol, []), on)
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


def _check(raw: dict, entries: list[dict], changed: str | None = None) -> None:
    """Replay the candidate ledger. A refusal is explained in plain words:
    about the entry just changed, or about the later entry it breaks (an
    edit that leaves a later sell short of shares)."""
    warnings: list[str] = []
    base = raw["settings"].get("base_ccy", "USD")
    splits = ledger.merge_splits(entries, market_splits(_trade_basis_symbols(entries)))
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
    return book(ccy)


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


def _clean_rows(raw: dict, items) -> list[dict]:
    """An import's rows as entries, each cleaned like a hand-added one; the
    first refusal names its row and nothing is kept."""
    if not isinstance(items, list) or not items:
        raise BookError("Nothing to add.")
    if len(items) > IMPORT_MAX:
        raise BookError(f"Add at most {IMPORT_MAX} entries at a time.")
    batch, out = "b_" + secrets.token_hex(6), []
    for n, fields in enumerate(items, 1):
        if not isinstance(fields, dict):
            raise BookError(f"Row {n} couldn't be read.")
        try:
            e = _clean(raw, fields)
        except BookError as exc:
            raise BookError(f"Row {n}: {exc.message}") from None
        e |= {"id": "e_" + secrets.token_hex(6), "source": "import", "batch": batch,
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
    # The usual case, everything books, costs one replay; only a refusal
    # pays for the row-by-row pass that finds which rows are at fault.
    whole = kept + rows
    dates = [e["date"] for e in whole]
    try:
        ledger.replay(whole, base_ccy=base, fx_at=_make_fx_at(base, warnings, dates),
                      splits=ledger.merge_splits(whole, market_splits(_trade_basis_symbols(whole))))  # fmt: skip
        return problems
    except ledger.LedgerError:
        pass
    for e in sorted(rows, key=lambda x: x["date"]):
        trial, i = kept + [e], int(e["id"][1:])
        splits = ledger.merge_splits(trial, market_splits(_trade_basis_symbols(trial)))
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
            held, cur_cash = day.qty, day.cash
            j += 1
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
    for ex, s, q, per in ledger.estimate_dividends(b, events, recorded):
        i = int(np.searchsorted(keys, np.datetime64(ex)))
        m = mult.get(major_ccy(qccy[s]))
        if i >= n or m is None:
            continue
        amt = float(q * price_in_major(per, qccy[s]) * m[i])
        paid[i] += amt
        est_by_sym[s] = est_by_sym.get(s, 0.0) + amt

    idx = ledger.twr_index([(d, value[i], perf_in[i], paid[i]) for i, d in enumerate(dates)])

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
                           "bench_vol")}  # fmt: skip
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
        "bench_index": rnd(bench_index, 8) if bench_index is not None else None,
        "period_start": starts,
    }
    priced = not syms or any(np.isfinite(c).any() for c in close.values())
    return {"series": series if priced else None, "periods": per if priced else None,
            "cash": float(cash[-1]), "net_deposits": float(np.sum(d_in - w_out)),
            "implied": float(implied), "month": month, "close": close, "last_close": last_close, "mult": mult,
            "qccy": qccy, "est": est_by_sym}  # fmt: skip


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
    splits = ledger.merge_splits(entries, market_splits(_trade_basis_symbols(entries)))
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
        })  # fmt: skip
        value_pos += m.value
    value = value_pos + perf["cash"]
    for x in positions:
        x["weight"] = x["value"] / value * 100 if value else None
    positions.sort(key=lambda x: -x["value"])

    est_total = sum(perf["est"].values())
    if perf["est"]:
        k = len(perf["est"])
        warnings.append(
            f"Dividends from {k} holding{'s' if k > 1 else ''} are estimated from Yahoo. "
            "Add them for exact figures."
        )
    per = perf["periods"] or {}
    sold = sum(p.realised for p in bd.positions.values())
    divs = sum(p.dividends for p in bd.positions.values())
    held = sum(x["profit_held"] for x in positions)
    profit_total = value - perf["net_deposits"] + est_total
    # Viewed in another currency, uninvested cash gains or loses with its
    # currency; that is the only part the holdings' own profit doesn't hold.
    cash_fx = profit_total - (held + sold + divs + est_total - bd.fees) if disp != base else 0.0
    out = {
        "demo": all(e.get("source") == "demo" for e in entries),
        "empty": False,
        "rev": raw["rev"],
        "ccy": disp,
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
                )
            },
        },  # fmt: skip
        "positions": positions,
        "entries": _entries_out(raw),
        "undo_label": _top(raw, "journal"),
        "redo_label": _top(raw, "redo"),
        "warnings": warnings,
        "upcoming": [],
        "attention": [],
    }
    if perf["series"]:
        out["series"] = perf["series"]
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
