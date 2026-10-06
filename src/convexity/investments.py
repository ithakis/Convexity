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
from bisect import bisect_right
from datetime import UTC, date, datetime, timedelta

from convexity import ledger, paths
from convexity.helpers import major_ccy, price_in_major

SCHEMA_VERSION = 1
JOURNAL_MAX = 100
_LOCK = threading.Lock()

PERIODS = ("1M", "3M", "YTD", "1Y", "3Y", "ALL")
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


def _closes(symbols: list[str]):
    """Daily closes for the last few months (split-adjusted, quote unit)."""
    from convexity.analytics import _bulk_close

    return _bulk_close(symbols, "3mo")


def _fx_series(ccy: str):
    from convexity.fx import _fx_usd_series

    return _fx_usd_series(ccy, "max")


def _fx_now(ccy: str, base: str) -> float | None:
    from convexity.fx import convert_amount

    return convert_amount(1.0, ccy, base)


def _dated(series) -> tuple[list[str], list[float]]:
    """A date-indexed series as sorted (ISO dates, values), NaNs dropped, so
    a lookup is a bisect: the FX history runs to decades of days and is
    looked up twice per foreign entry."""
    if series is None or len(series) == 0:
        return [], []
    s = series.dropna()
    return [d.date().isoformat() if hasattr(d, "date") else str(d)[:10] for d in s.index], [
        float(v) for v in s.values
    ]


def _on_or_before(dated: tuple[list[str], list[float]], iso: str) -> float | None:
    """The last value on or before ``iso`` of a ``_dated`` series."""
    i = bisect_right(dated[0], iso)
    return dated[1][i - 1] if i else None


def _make_fx_at(base: str, warnings: list[str]):
    """``fx_at(ccy, date)``: base units per one ``ccy`` on that date, from
    Yahoo's history; today's rate (with a warning) when history is missing."""
    memo: dict = {}

    def usd_per(ccy: str, iso: str) -> float | None:
        if ccy == "USD":
            return 1.0
        if ccy not in memo:
            memo[ccy] = _dated(_fx_series(ccy))
        dates, vals = memo[ccy]
        # Before the history starts, its first rate is the best there is.
        return vals[max(bisect_right(dates, iso), 1) - 1] if vals else None

    def fx_at(ccy: str, iso: str) -> float | None:
        ccy = major_ccy(ccy)
        if ccy == base:
            return 1.0
        a, b = usd_per(ccy, iso), usd_per(base, iso)
        if a and b:
            return a / b
        spot = _fx_now(ccy, base)
        if spot:
            note = f"No {ccy} history from Yahoo: trades in {ccy} use today's rate."
            if note not in warnings:
                warnings.append(note)
        return spot

    return fx_at


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
    hit = hit or _pack_hit(ticker)
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
    """Company typeahead for the Add popover, from the symbol pack."""
    try:
        from convexity import symbol_db

        hits = symbol_db.lookup(query, limit=limit, min_score=60.0)
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
    return out


def _label(op: str, e: dict, raw: dict) -> str:
    """Plain words for the journal, the toast and the Undo hover."""
    name = _name(raw, e.get("symbol"))
    if e["type"] in ledger.TRADES:
        name = f"{ledger._qty(e['qty'])} {name}"
    return f"{op} {e['type']}" + (f" · {name}" if name else "")


_TAILS = (" Corporation", " Inc.", " Inc", " plc", " PLC", " Ltd", " N.V.", " SE", " AG", " S.A.")


def _short(name: str) -> str:
    for tail in _TAILS:
        if name.endswith(tail):
            return name[: -len(tail)]
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
        ledger.replay(entries, base_ccy=base, fx_at=_make_fx_at(base, warnings), splits=splits)
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


def _mutate(base_rev, change) -> dict:
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
    return book()


def _record(raw: dict, verb: str, e: dict, undo: list, redo: list):
    """Journal one change (a new change clears redo) and apply it."""
    item = {"label": _label(verb, e, raw), "undo": undo, "redo": redo}
    raw["journal"] = (raw["journal"] + [item])[-JOURNAL_MAX:]
    raw["redo"] = []
    print(f"[investments] {verb.lower()} {e['id']} {e.get('symbol') or e['type']}")
    return _apply(raw["entries"], redo), redo[0].get("put", {}).get("id")


def add(fields: dict, base_rev) -> dict:
    def change(raw):
        e = _clean(raw, fields)
        src = fields.get("source")
        e |= {"id": "e_" + secrets.token_hex(6), "source": src if src in SOURCES else "manual",
              "batch": None, "created_at": _now(), "updated_at": _now()}  # fmt: skip
        return _record(raw, "Added", e, [{"del": e["id"]}], [{"put": e}])

    return _mutate(base_rev, change)


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


def add_batch(items, base_rev, aliases=None) -> dict:
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

    return _mutate(base_rev, change)


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
    try:
        ledger.replay(whole, base_ccy=base, fx_at=_make_fx_at(base, warnings),
                      splits=ledger.merge_splits(whole, market_splits(_trade_basis_symbols(whole))))  # fmt: skip
        return problems
    except ledger.LedgerError:
        pass
    for e in sorted(rows, key=lambda x: x["date"]):
        trial, i = kept + [e], int(e["id"][1:])
        splits = ledger.merge_splits(trial, market_splits(_trade_basis_symbols(trial)))
        try:
            ledger.replay(trial, base_ccy=base, fx_at=_make_fx_at(base, warnings), splits=splits)
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


def update(entry_id: str, fields: dict, base_rev) -> dict:
    def change(raw):
        i, old = _find(raw, entry_id)
        new = old | _clean(raw, fields, old) | {"updated_at": _now()}
        return _record(raw, "Edited", new, [{"put": old, "at": i}], [{"put": new}])

    return _mutate(base_rev, change)


def delete(entry_id: str, base_rev) -> dict:
    def change(raw):
        i, old = _find(raw, entry_id)
        return _record(raw, "Deleted", old, [{"put": old, "at": i}], [{"del": entry_id}])

    return _mutate(base_rev, change)


def _step(base_rev, frm: str, to: str) -> dict:
    """Undo (journal -> redo) or redo (redo -> journal)."""

    def change(raw):
        if not raw[frm]:
            raise BookError("Nothing to undo." if frm == "journal" else "Nothing to redo.")
        item = raw[frm].pop()
        raw[to] = (raw[to] + [item])[-JOURNAL_MAX:]
        print(f"[investments] {'undo' if frm == 'journal' else 'redo'}")
        return _apply(raw["entries"], item["undo" if frm == "journal" else "redo"]), None

    return _mutate(base_rev, change)


def undo(base_rev) -> dict:
    return _step(base_rev, "journal", "redo")


def redo(base_rev) -> dict:
    return _step(base_rev, "redo", "journal")


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


def book() -> dict:
    """The page payload. The keys are the ones Phase 1 built the page
    against (roadmap, Phase 1 Findings); performance keys stay empty until
    Phase 3."""
    try:
        raw = _load()
    except BookUnreadable as exc:
        return {"empty": True, "error": exc.message}
    if not raw or not raw["entries"]:
        return {"empty": True, "rev": raw["rev"] if raw else 0,
                "undo_label": _top(raw, "journal"), "redo_label": _top(raw, "redo")}  # fmt: skip
    base = raw["settings"].get("base_ccy", "USD")
    inst, entries, warnings = raw["instruments"], raw["entries"], []
    splits = ledger.merge_splits(entries, market_splits(_trade_basis_symbols(entries)))
    fx_at = _make_fx_at(base, warnings)
    try:
        b = ledger.replay(entries, base_ccy=base, fx_at=fx_at, splits=splits)
    except ledger.LedgerError as exc:
        # The stored book no longer replays (new split data, say): show the
        # ledger so it can be fixed, never a half-computed book.
        return {"empty": False, "broken": f"{exc.message}.", "rev": raw["rev"], "ccy": base,
                "entries": _entries_out(raw), "undo_label": _top(raw, "journal"),
                "redo_label": _top(raw, "redo"), "positions": [], "headline": None}  # fmt: skip

    ms_iso = (date.today().replace(day=1) - timedelta(days=1)).isoformat()  # last month's close
    ms_qty, ms_cash = ledger.qty_at(b, ms_iso)
    syms = sorted({p.symbol for p in b.open_positions()} | set(ms_qty))
    closes = _closes(syms) if syms else None
    dated = {s: _dated(closes.get(s) if closes is not None else None) for s in syms}
    fx_memo: dict[str, float | None] = {}

    def fxn(ccy: str) -> float | None:
        if ccy not in fx_memo:
            fx_memo[ccy] = 1.0 if ccy == base else _fx_now(ccy, base)
        return fx_memo[ccy]

    positions, value_pos = [], 0.0
    for p in b.open_positions():
        meta = inst.get(p.symbol, {})
        name, qccy = meta.get("name") or p.symbol, meta.get("quote_ccy") or p.ccy or base
        last_q = (dated[p.symbol][1] or [None])[-1]
        price, pccy, note = price_in_major(last_q, qccy), major_ccy(qccy), None
        if price is None:
            price, pccy = p.last_price, p.ccy or base
            note = "No price from Yahoo: valued at your last trade price."
            warnings.append(f"{name}: no price from Yahoo, valued at the last trade price.")
        rate = fxn(pccy)
        if price is None or not rate:
            warnings.append(f"{name}: no price or {pccy} rate, so it is left out of the value.")
            continue
        m = ledger.mark(p, price, pccy, rate)
        ms_q = _on_or_before(dated[p.symbol], ms_iso)
        # Prices show in the share's quote unit (pence for LSE), like Yahoo.
        to_quote = 100.0 if qccy != pccy and pccy == major_ccy(qccy) else 1.0
        avg = p.avg_cost_loc if p.ccy == pccy else None
        positions.append({
            "symbol": p.symbol, "name": _short(name), "exchange": meta.get("exchange"),
            "quote_ccy": qccy, "shares": round(p.qty, 6),
            "avg_price": avg * to_quote if avg is not None else None,
            "price": price * to_quote, "value": m.value,
            "month_pct": ((last_q / ms_q - 1) * 100 if last_q and ms_q else None),
            "profit_held": m.unrealised, "profit_sold": p.realised, "dividends": p.dividends,
            "profit_price": m.profit_price if pccy != base else None,
            "profit_fx": m.profit_fx if pccy != base else None,
            "return_pct": m.unrealised / m.cost * 100 if m.cost > 0 else None,
            "note": note, "profit": m.unrealised + p.realised + p.dividends,
        })  # fmt: skip
        value_pos += m.value
    value = value_pos + b.cash
    for x in positions:
        x["weight"] = x["value"] / value * 100 if value else None
    positions.sort(key=lambda x: -x["value"])

    # This month: the change in value since last month's close, less the
    # money put in or taken out this month. Month-start holdings are valued
    # at that day's close and today's FX (Phase 3 brings daily FX).
    ms_value = ms_cash
    for sym, q in ms_qty.items():
        qccy = inst.get(sym, {}).get("quote_ccy") or base
        px = price_in_major(_on_or_before(dated[sym], ms_iso), qccy)
        rate = fxn(major_ccy(qccy))
        if px is None or not rate:
            ms_value = None
            break
        ms_value += q * px * rate
    flows_in = sum(d.inflow for d in b.days if d.date > ms_iso)
    flows_out = sum(d.outflow for d in b.days if d.date > ms_iso)
    month_abs = value - ms_value - flows_in + flows_out if ms_value is not None else None
    denom = (ms_value or 0.0) + flows_in
    sold = sum(p.realised for p in b.positions.values())
    divs = sum(p.dividends for p in b.positions.values())
    held_profit = sum(x["profit_held"] for x in positions)
    return {
        "demo": all(e.get("source") == "demo" for e in entries),
        "empty": False,
        "rev": raw["rev"],
        "ccy": base,
        "headline": {
            "value": value,
            "cash": b.cash,
            "cash_weight": b.cash / value * 100 if value else None,
            "month_abs": month_abs,
            "month_pct": month_abs / denom * 100 if month_abs is not None and denom > 0 else None,
            "profit_total": value - b.net_deposits,
            "profit_held": held_profit,
            "profit_sold": sold,
            "dividends": divs,
            "fees": b.fees,
            "net_deposits": b.net_deposits,
            "implied_deposits": b.implied,
            "twr": dict.fromkeys(PERIODS),
            "bench_twr": dict.fromkeys(PERIODS),
            "mwr_ann": None,
        },
        "positions": positions,
        "entries": _entries_out(raw),
        "undo_label": _top(raw, "journal"),
        "redo_label": _top(raw, "redo"),
        "warnings": warnings,
        "upcoming": [],
        "attention": [],
    }


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
