"""Provider-agnostic symbol database.

Purpose
-------
The dashboard needs to map fuzzy human input ("microsoft", "saop", "Royal
Dutch") to a concrete data-provider ticker ("MSFT", "SAP.DE", "SHEL.L")
without round-tripping every typo to the upstream search endpoint. This
module owns the local lookup table.

Provider-agnostic on purpose
----------------------------
The schema stores `provider` + `ticker` so the same SQLite file can hold
multiple provider mappings (yfinance today, something else tomorrow). To
switch providers you rebuild the DB with a new set of source functions —
nothing in the dashboard needs to change beyond the `_PROVIDER` constant.

Adding a new source
-------------------
A source is just a function ``fn(session) -> Iterable[SymbolRow]``.
Register it in the ``SOURCES`` dict at the bottom and the builder will
pick it up. See ``source_nasdaq_trader`` for the minimal pattern.

Adding a new provider
---------------------
1. Write per-provider transform helpers (e.g. how does Refinitiv format a
   German XETRA ticker vs. yfinance's "SAP.DE"?).
2. Wrap your source functions to emit rows with the right ``provider``
   field. The schema and lookup code are already provider-aware.

Fuzzy matching uses ``rapidfuzz`` when available and falls back to a
slower exact-substring scan when not — so the dashboard never hard-depends
on it for startup, but install it for the good experience.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

try:
    from rapidfuzz import fuzz, process as rf_process
    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover - optional dependency
    _HAS_RAPIDFUZZ = False
    fuzz = None  # type: ignore
    rf_process = None  # type: ignore


_PROVIDER = "yfinance"
# Repo root, not the package dir — build_symbol_db.py writes the built DB
# next to dashboard.py, and CLAUDE.md's file layout documents it there too.
_DB_PATH = Path(__file__).resolve().parent.parent / "symbol_db.sqlite"
_DB_LOCK = threading.Lock()
_NAME_NORM_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class SymbolRow:
    """One row destined for the symbols table. ``provider`` defaults to
    yfinance — override when adding non-yfinance sources."""
    ticker: str           # in provider format
    name: str             # canonical human-readable name
    exchange: Optional[str] = None
    country: Optional[str] = None
    instrument_type: Optional[str] = None  # "stock"|"etf"|"adr"|"index"|...
    provider: str = _PROVIDER


@dataclass(frozen=True)
class LookupHit:
    ticker: str
    name: str
    exchange: Optional[str]
    score: float          # 0..100, higher is closer


def db_path() -> Path:
    """Resolve the SQLite path. ``PORTFOLIO_SYMBOL_DB`` env var overrides
    the default location — useful for tests / alt providers."""
    env = os.environ.get("PORTFOLIO_SYMBOL_DB")
    return Path(env) if env else _DB_PATH


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = path or db_path()
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS symbols (
            provider        TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            name            TEXT NOT NULL,
            exchange        TEXT,
            country         TEXT,
            instrument_type TEXT,
            name_norm       TEXT NOT NULL,
            source          TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            PRIMARY KEY (provider, ticker)
        );
        CREATE INDEX IF NOT EXISTS idx_symbols_name_norm ON symbols(name_norm);
        CREATE INDEX IF NOT EXISTS idx_symbols_exchange  ON symbols(exchange);
        CREATE INDEX IF NOT EXISTS idx_symbols_provider  ON symbols(provider);
        """
    )
    conn.commit()


def normalize_name(s: str) -> str:
    """Fold a name for fuzzy comparison. Lowercase, alnum-only, no spaces.
    `'  Apple, Inc.'` → `'appleinc'`. Stable enough that the same input
    always hashes to the same bucket."""
    return _NAME_NORM_RE.sub("", (s or "").lower())


# --------------------------------------------------------------------------
# Builder API — called from build_symbol_db.py
# --------------------------------------------------------------------------

def init_db(path: Optional[Path] = None) -> None:
    """Create the schema if missing. Idempotent."""
    with _connect(path) as conn:
        _ensure_schema(conn)


def upsert_rows(rows: Iterable[SymbolRow], source: str, *, path: Optional[Path] = None) -> int:
    """Insert/update a batch of rows, tagged with the source name. Returns
    the count actually written."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = []
    for r in rows:
        ticker = (r.ticker or "").strip()
        name = (r.name or "").strip()
        if not ticker or not name:
            continue
        payload.append((
            r.provider, ticker, name,
            (r.exchange or None),
            (r.country or None),
            (r.instrument_type or None),
            normalize_name(name),
            source, now,
        ))
    if not payload:
        return 0
    with _DB_LOCK, _connect(path) as conn:
        _ensure_schema(conn)
        conn.executemany(
            """
            INSERT INTO symbols
                (provider, ticker, name, exchange, country,
                 instrument_type, name_norm, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, ticker) DO UPDATE SET
                name = excluded.name,
                exchange = COALESCE(excluded.exchange, symbols.exchange),
                country = COALESCE(excluded.country, symbols.country),
                instrument_type = COALESCE(excluded.instrument_type, symbols.instrument_type),
                name_norm = excluded.name_norm,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            payload,
        )
        conn.commit()
    return len(payload)


def db_stats(path: Optional[Path] = None) -> dict:
    """Quick row counts — handy for the CLI summary."""
    try:
        with _connect(path) as conn:
            _ensure_schema(conn)
            total = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            by_src = dict(conn.execute(
                "SELECT source, COUNT(*) FROM symbols GROUP BY source ORDER BY 2 DESC"
            ).fetchall())
            by_xch = dict(conn.execute(
                "SELECT COALESCE(exchange,'?'), COUNT(*) FROM symbols GROUP BY exchange ORDER BY 2 DESC LIMIT 12"
            ).fetchall())
        return {"total": total, "by_source": by_src, "top_exchanges": by_xch}
    except sqlite3.DatabaseError:
        return {"total": 0, "by_source": {}, "top_exchanges": {}}


# --------------------------------------------------------------------------
# Lookup API — called from the dashboard
# --------------------------------------------------------------------------

# In-process cache: keep the full (name_norm, ticker, name, exchange) list
# in memory after first read so rapidfuzz can scan it in microseconds.
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"loaded_at": 0.0, "rows": None, "mtime": 0.0}
_CACHE_RELOAD_AFTER_S = 300.0   # check disk mtime at most every 5 min


def _load_cache(force: bool = False) -> list[tuple[str, str, str, Optional[str]]]:
    """Return a list of (name_norm, ticker, name, exchange) tuples. Auto-
    reloads when the on-disk file changes."""
    with _CACHE_LOCK:
        p = db_path()
        try:
            mtime = p.stat().st_mtime
        except OSError:
            _CACHE["rows"] = []
            _CACHE["loaded_at"] = time.time()
            return _CACHE["rows"]
        stale = (
            force
            or _CACHE["rows"] is None
            or mtime > _CACHE["mtime"]
            or (time.time() - _CACHE["loaded_at"]) > _CACHE_RELOAD_AFTER_S
        )
        if not stale:
            return _CACHE["rows"]
        try:
            with _connect(p) as conn:
                _ensure_schema(conn)
                rows = conn.execute(
                    "SELECT name_norm, ticker, name, exchange "
                    "FROM symbols WHERE provider = ?",
                    (_PROVIDER,),
                ).fetchall()
            _CACHE["rows"] = [(r[0], r[1], r[2], r[3]) for r in rows]
        except sqlite3.DatabaseError:
            _CACHE["rows"] = []
        _CACHE["loaded_at"] = time.time()
        _CACHE["mtime"] = mtime
        return _CACHE["rows"]


def _composite_score(q_norm: str, cand_norm: str, base: float) -> float:
    """Re-rank a rapidfuzz candidate score so that close-length, prefix and
    substring matches beat lexically-similar-but-long matches.

    Without this, "microsoft" matches "smithmicrosoftwareinc" with the same
    WRatio as "microsoftcorp", because both contain the query as a substring.
    Penalizing extra-length restores the obvious winner.
    """
    if cand_norm == q_norm:
        return 100.0
    if cand_norm.startswith(q_norm):
        # Prefix match — the closer the lengths, the higher the score.
        extra = len(cand_norm) - len(q_norm)
        return min(99.5, 96.0 - extra * 0.4)
    if q_norm in cand_norm:
        # Substring — fall in the 80–94 band, penalising length growth.
        extra = len(cand_norm) - len(q_norm)
        return max(70.0, 92.0 - extra * 0.6)
    # No structural match: keep the fuzzy score but shave a bit so it can't
    # beat structural matches above.
    return base - 6.0


def lookup(query: str, *, min_score: float = 72.0, limit: int = 1) -> list[LookupHit]:
    """Fuzzy-match a free-form query against the local symbol database.

    Returns up to ``limit`` hits sorted by descending score.

    Ranking:
      1. Exact ticker match (case-insensitive) — score 100.
      2. Exact normalised-name match — score 100.
      3. Rapidfuzz candidate selection (WRatio, low cutoff), re-ranked by
         a composite that rewards prefix / substring matches with small
         length deltas.
      4. Substring fallback if rapidfuzz is unavailable.
    """
    q = (query or "").strip()
    if not q:
        return []
    rows = _load_cache()
    if not rows:
        return []

    # 1) Exact ticker — short-circuit.
    q_up = q.upper()
    exact: list[LookupHit] = []
    for nn, tk, nm, xch in rows:
        if tk.upper() == q_up:
            exact.append(LookupHit(tk, nm, xch, 100.0))
            if len(exact) >= limit:
                return exact

    q_norm = normalize_name(q)
    if not q_norm:
        return exact

    # 2) Exact name_norm.
    for nn, tk, nm, xch in rows:
        if nn == q_norm:
            cand = LookupHit(tk, nm, xch, 100.0)
            if cand not in exact:
                exact.append(cand)
            if len(exact) >= limit:
                return exact

    # 3) Fuzzy + composite re-rank.
    if _HAS_RAPIDFUZZ and rf_process is not None:
        choices = [r[0] for r in rows]
        # Lower base cutoff (60) lets typos through; composite re-rank filters.
        cands = rf_process.extract(
            q_norm, choices,
            scorer=fuzz.WRatio,
            limit=40,
            score_cutoff=60.0,
        )
        scored: list[LookupHit] = []
        seen_tickers: set[str] = {h.ticker for h in exact}
        for _matched_value, base_score, idx in cands:
            nn, tk, nm, xch = rows[idx]
            if tk in seen_tickers:
                continue
            s = _composite_score(q_norm, nn, float(base_score))
            if s >= min_score:
                scored.append(LookupHit(tk, nm, xch, s))
                seen_tickers.add(tk)
        scored.sort(key=lambda h: h.score, reverse=True)
        return (exact + scored)[:limit]

    # 4) Substring fallback — slow but always correct-shape.
    subs: list[LookupHit] = []
    seen_tickers = {h.ticker for h in exact}
    for nn, tk, nm, xch in rows:
        if tk in seen_tickers:
            continue
        if q_norm in nn:
            s = _composite_score(q_norm, nn, 70.0)
            if s >= min_score:
                subs.append(LookupHit(tk, nm, xch, s))
    subs.sort(key=lambda h: h.score, reverse=True)
    return (exact + subs)[:limit]


# --------------------------------------------------------------------------
# Sources — add more by writing a function and registering it below.
# Each function MUST return an iterable of SymbolRow.
# --------------------------------------------------------------------------

def source_nasdaq_trader(session) -> Iterable[SymbolRow]:
    """NASDAQ-listed + Other-listed (NYSE, AMEX, ARCA) US securities. The
    files are pipe-delimited, refreshed nightly, no auth.
    Docs: https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs
    """
    urls = [
        ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",  "nasdaq"),
        ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",   "other"),
    ]
    exch_map = {  # otherlisted's single-letter codes
        "A": "AMEX", "N": "NYSE", "P": "ARCA", "Z": "BATS", "V": "IEX",
    }
    out: list[SymbolRow] = []
    for url, kind in urls:
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
        except Exception as exc:
            print(f"  ! {url}: {exc}")
            continue
        lines = r.text.splitlines()
        if not lines:
            continue
        header = lines[0].split("|")
        for line in lines[1:]:
            cells = line.split("|")
            if len(cells) != len(header):
                continue
            row = dict(zip(header, cells))
            # The file ends with a "File Creation Time" footer row.
            if row.get("Symbol", "").startswith("File Creation"):
                continue
            if (row.get("Test Issue") or "").upper() == "Y":
                continue
            ticker = (row.get("Symbol") or row.get("ACT Symbol") or "").strip()
            name = (row.get("Security Name") or "").strip()
            if not ticker or not name:
                continue
            if kind == "nasdaq":
                exchange = "NASDAQ"
            else:
                exchange = exch_map.get((row.get("Exchange") or "").strip(), "NYSE")
            etf = (row.get("ETF") or "").upper() == "Y"
            out.append(SymbolRow(
                ticker=ticker,
                name=name,
                exchange=exchange,
                country="US",
                instrument_type="etf" if etf else "stock",
            ))
    return out


def source_sec_company_tickers(session) -> Iterable[SymbolRow]:
    """SEC's master list of company → ticker mappings. JSON, refreshed
    weekly. Mostly US filers + cross-listed foreign issuers.
    NOTE: SEC requires a descriptive User-Agent. Set the env var
    ``SEC_USER_AGENT`` to something like ``"Your Name your@email"``."""
    ua = os.environ.get("SEC_USER_AGENT", "PortfolioTracker contact@example.com")
    headers = {"User-Agent": ua, "Accept": "application/json"}
    url = "https://www.sec.gov/files/company_tickers_exchange.json"
    try:
        r = session.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"  ! {url}: {exc}")
        return []
    # Schema: {"fields": ["cik","name","ticker","exchange"], "data": [[...], ...]}
    fields = data.get("fields") or []
    rows = data.get("data") or []
    try:
        i_name = fields.index("name")
        i_tick = fields.index("ticker")
        i_xch = fields.index("exchange")
    except ValueError:
        return []
    out: list[SymbolRow] = []
    for r2 in rows:
        try:
            ticker = (r2[i_tick] or "").strip()
            name = (r2[i_name] or "").strip()
            xch = (r2[i_xch] or "").strip() or None
            if not ticker or not name:
                continue
            out.append(SymbolRow(
                ticker=ticker, name=name, exchange=xch, country="US",
                instrument_type="stock",
            ))
        except (IndexError, TypeError):
            continue
    return out


# Register sources here. Key is the CLI `--sources` token.
SOURCES: dict[str, Callable] = {
    "nasdaq": source_nasdaq_trader,
    "sec":    source_sec_company_tickers,
}
