"""The symbol pack: every listing Yahoo has, searchable offline.

What it is
----------
`convexity build-symbols` (symbol_build.py, run weekly by
`.github/workflows/symbol-pack.yml`) sweeps Yahoo's screener for every
equity, ETF and mutual fund in every region, plus a fixed list of indices,
and publishes two files to the rolling `reference-pack` release:

    symbols-manifest.json   date, row count, SHA-256 + size of the data file
    symbols.ndjson.gz       a {"schema", "columns"} line, then one JSON array
                            per listing (streamed on install: flat memory)

Each row carries ticker, name, type, exchange, region, sector, industry,
size in USD (market cap; net assets for funds), a company group id and a
`home` flag. Listings of one company share a group; exactly one of them is
its home listing (NOVO-B.CO, not NVO or NOV.DE). Search shows home listings
and offers the rest as alternates.

App side
--------
`start()` downloads the pack in a daemon thread when the local copy is older
than a week, behind the same Settings switch as the reference pack, and
writes it into `symbol_db.sqlite` in the data folder (a new file, then one
`os.replace`). Trust model as reference_pack.py: HTTPS to this repo's
releases, the manifest's hash + size, byte caps, JSON only, every field
checked against an allow-list. Names are the one free-text field: printable,
length-capped, and only ever displayed or fuzzy-matched, never executed.

The `symbols` table keeps its `ticker` and `name` columns because
relevance.load_company_names() reads them for the Market read.

Lookup
------
`lookup()` maps fuzzy input to listings: exact ticker, then the fuzzy name
match re-ranked by size and type (see `_rank`). `category()` lists the
largest home listings for a sector / industry / region filter. Both read the
SQLite file; a trigram index inside it proposes name candidates, so nothing
is held in memory.
"""

import gzip
import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
import zlib
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz

from convexity import paths, reference_pack as rp

SCHEMA_VERSION = 1  # the pack format
# PRAGMA user_version of the local file; 1 was the NASDAQ/SEC table. The file
# stores name_key()/acronym() of every row, so BUMP THIS whenever either
# changes: an older file then reads as empty and is downloaded again at boot.
DB_VERSION = 2
MANIFEST = "symbols-manifest.json"
DATA = "symbols.ndjson.gz"
_MAX_LINE = 4096  # one row; a longer line is not a row
COLUMNS = (
    "ticker",
    "name",
    "type",
    "exchange",
    "region",
    "sector",
    "industry",
    "mcap_usd",
    "adv_usd",  # average daily traded value: ranks ETFs and funds, which have no cap
    "group",
    "home",
)
TYPES = ("stock", "etf", "fund", "index")
FRESH_S = 7 * 24 * 3600

_TICKER = re.compile(r"^[A-Z0-9^][A-Z0-9.^=&_-]{0,23}$")
_LABELS = ("exchange", "region", "sector", "industry")


def db_path() -> Path:
    return paths.symbol_db_file()


# =================================================================== format
def _text(v, what: str, cap: int, optional: bool = True) -> None:
    if v is None and optional:
        return
    if not isinstance(v, str) or not 0 < len(v) <= cap or not v.isprintable():
        raise rp.PackError(f"{what}: not a short printable string")


def validate_manifest(m) -> dict:
    if not isinstance(m, dict) or m.get("schema_version") != SCHEMA_VERSION:
        raise rp.PackError("symbols manifest: wrong schema_version")
    if not isinstance(m.get("date"), str) or not rp._DATE.match(m["date"]):
        raise rp.PackError("symbols manifest: bad date")
    n = m.get("rows")
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        raise rp.PackError("symbols manifest: bad row count")
    meta = (m.get("files") or {}).get(DATA)
    if set(m.get("files") or {}) != {DATA} or not isinstance(meta, dict):
        raise rp.PackError(f"symbols manifest: files must be exactly [{DATA!r}]")
    if not rp._HEX64.match(str(meta.get("sha256", ""))):
        raise rp.PackError("symbols manifest: bad sha256")
    b = meta.get("bytes")
    if isinstance(b, bool) or not isinstance(b, int) or not 0 < b <= rp.MAX_FILE_BYTES:
        raise rp.PackError("symbols manifest: bad size")
    return m


def encode(rows: list[list]) -> bytes:
    """The data file: gzip (deterministic, mtime 0) of a header line and one
    JSON array per row — line by line, so the app can stream it: parsed whole,
    630k rows took ~550 MB of memory to install."""
    head = json.dumps({"schema": SCHEMA_VERSION, "columns": list(COLUMNS)})
    body = (json.dumps(r, separators=(",", ":"), ensure_ascii=False, allow_nan=False) for r in rows)
    return gzip.compress(
        ("\n".join([head, *body]) + "\n").encode("utf-8"), compresslevel=9, mtime=0
    )


def _lines(blob: bytes) -> Iterator[bytes]:
    """Decompress a gzip member a megabyte at a time and yield its lines,
    capped (a small file that expands to gigabytes fails at the cap)."""
    d, data, buf, total = zlib.decompressobj(wbits=31), blob, b"", 0
    while True:
        try:
            chunk = d.decompress(data, 1 << 20)
        except zlib.error as e:
            raise rp.PackError(f"{DATA}: not valid gzip ({e})") from None
        data, total = d.unconsumed_tail, total + len(chunk)
        if total > rp.MAX_JSON_BYTES:
            raise rp.PackError(f"{DATA}: decompresses past the {rp.MAX_JSON_BYTES}-byte cap")
        *lines, buf = (buf + chunk).split(b"\n")
        if len(buf) > _MAX_LINE or any(len(x) > _MAX_LINE for x in lines):
            raise rp.PackError(f"{DATA}: a line longer than {_MAX_LINE} bytes")
        yield from (x for x in lines if x)
        if d.eof or (not data and not chunk):
            break
    if not d.eof:
        raise rp.PackError(f"{DATA}: gzip stream truncated")
    if d.unused_data.strip(b"\0") or buf.strip():
        raise rp.PackError(f"{DATA}: trailing data")


def _json(line: bytes, what: str):
    try:
        return json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise rp.PackError(f"{what}: not valid JSON") from None


def iter_rows(blob: bytes) -> Iterator[list]:
    """The data file's rows, each checked against the allow-list as it is
    read. Repeated tickers are caught by write_db (the primary key)."""
    lines = _lines(blob)
    head = _json(next(lines, b"null"), "symbols header")
    if not isinstance(head, dict) or head.get("schema") != SCHEMA_VERSION:
        raise rp.PackError("symbols: wrong schema")
    if head.get("columns") != list(COLUMNS):
        raise rp.PackError("symbols: unexpected columns")
    for i, line in enumerate(lines):
        r = _json(line, f"symbols[{i}]")
        if not isinstance(r, list) or len(r) != len(COLUMNS):
            raise rp.PackError(f"symbols[{i}]: expected {len(COLUMNS)} fields")
        tk, name, typ, *labels, mcap, adv, grp, home = r
        if not isinstance(tk, str) or not _TICKER.match(tk):
            raise rp.PackError(f"symbols[{i}]: bad ticker")
        _text(name, f"symbols[{i}].name", 160, optional=False)
        if typ not in TYPES:
            raise rp.PackError(f"symbols[{i}]: bad type")
        for k, v in zip(_LABELS, labels, strict=True):
            _text(v, f"symbols[{i}].{k}", 64)
        rp._num(mcap, f"symbols[{i}].mcap_usd")
        rp._num(adv, f"symbols[{i}].adv_usd")
        if isinstance(grp, bool) or not isinstance(grp, int) or grp < 0 or home not in (0, 1):
            raise rp.PackError(f"symbols[{i}]: bad group or home flag")
        yield r


def write_db(rows, manifest_raw: bytes, path: Path | None = None, expect: int | None = None) -> int:
    """Stream (validated) rows into a new SQLite file, then swap it in with
    one `os.replace`. Nothing is replaced unless every row is valid, the
    tickers are unique and, with `expect`, the count matches the manifest.
    The old file's -wal/-shm sidecars are removed first: SQLite would
    otherwise try to apply a stale WAL to the new database. Returns the count."""
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    n = 0

    def values():
        nonlocal n
        for r in rows:
            n += 1
            yield (r[0], r[1], name_key(r[1]), acronym(r[1]), *r[2:])

    try:
        with closing(sqlite3.connect(tmp)) as con:
            con.executescript(
                """
                CREATE TABLE symbols (
                    ticker TEXT PRIMARY KEY, name TEXT NOT NULL, key TEXT NOT NULL,
                    acr TEXT NOT NULL, type TEXT NOT NULL, exchange TEXT, region TEXT,
                    sector TEXT, industry TEXT, mcap_usd REAL, adv_usd REAL, grp INTEGER NOT NULL,
                    home INTEGER NOT NULL);
                CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
                """
            )
            try:
                con.executemany("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values())
            except sqlite3.IntegrityError:
                raise rp.PackError(f"{DATA}: a ticker appears twice") from None
            if not n or (expect is not None and n != expect):
                raise rp.PackError(f"{DATA}: {n} rows, the manifest says {expect}")
            # Name search runs in SQLite, not in memory (a Python index of
            # ~450k names cost ~300 MB): a trigram index proposes candidates,
            # rapidfuzz ranks them. Home listings only, funds apart: ~330k
            # fund names would crowd companies out of the candidate list
            # ("jp morgan" -> JPMorgan funds, not JPM). Keep detail=full (the
            # default): with detail='none' bm25 cannot weigh how many trigrams
            # a name shares, and "nestle" ranked Nestlé S.A. 740th of 37k.
            con.executescript(
                """
                CREATE INDEX symbols_grp ON symbols(grp);
                CREATE INDEX symbols_home ON symbols(home, mcap_usd);
                CREATE INDEX symbols_acr ON symbols(acr) WHERE home = 1;
                CREATE VIRTUAL TABLE names USING fts5(key, tokenize='trigram', content='');
                INSERT INTO names(rowid, key) SELECT rowid, key FROM symbols
                    WHERE home = 1 AND type != 'fund';
                CREATE VIRTUAL TABLE fund_names USING fts5(key, tokenize='trigram', content='');
                INSERT INTO fund_names(rowid, key) SELECT rowid, key FROM symbols
                    WHERE home = 1 AND type = 'fund';
                """
            )
            con.executemany(
                "INSERT INTO meta VALUES (?,?)",
                [
                    ("manifest", manifest_raw.decode("utf-8")),
                    ("manifest_sha", rp.sha256(manifest_raw)),
                ],
            )
            con.execute(f"PRAGMA user_version = {DB_VERSION}")
            con.commit()
        for side in ("-wal", "-shm"):
            Path(f"{path}{side}").unlink(missing_ok=True)
        for attempt in range(5):  # Windows refuses while a reader has the file open
            try:
                os.replace(tmp, path)
                return n
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.5)
    finally:
        tmp.unlink(missing_ok=True)
    raise AssertionError("unreachable")


# =================================================================== app side
_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STATUS: dict = {"state": "idle", "error": "", "checked_at": None}


def _log(msg: str) -> None:
    print(f"[symbols] {msg}", flush=True)


def _connect() -> sqlite3.Connection | None:
    """Read-only connection to a current-format file, else None."""
    p = db_path()
    if not p.exists():
        return None
    try:
        # as_uri(): a "#", "?" or "%" in the data-folder path, or a Windows
        # path, would otherwise break the file: URI.
        con = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        if con.execute("PRAGMA user_version").fetchone()[0] != DB_VERSION:
            con.close()
            return None
        return con
    except sqlite3.DatabaseError:
        return None


def _meta() -> dict:
    con = _connect()
    if con is None:
        return {}
    with closing(con):
        return dict(con.execute("SELECT k, v FROM meta").fetchall())


def _fresh() -> bool:
    try:
        age = time.time() - db_path().stat().st_mtime
    except OSError:
        return False
    return 0 <= age < FRESH_S and bool(_meta())


def start(force: bool = False) -> dict:
    """Check for a newer pack in a daemon thread (weekly, or now when
    `force`). Off when the reference-pack switch is off. Never raises."""
    global _THREAD
    on = rp.enabled()
    fresh = on and not force and _fresh()
    with _LOCK:
        if _STATUS["state"] in rp.IN_FLIGHT:
            return dict(_STATUS)
        if not on or fresh:
            _STATUS.update(state="disabled" if not on else "up_to_date", error="")
            return dict(_STATUS)
        _STATUS.update(state="checking", error="")
        _THREAD = threading.Thread(target=_run, name="cx-symbol-pack", daemon=True)
        _THREAD.start()
        return dict(_STATUS)


def wait(timeout: float | None = None) -> dict:
    """Test hook: block until the current check ends."""
    if _THREAD is not None:
        _THREAD.join(timeout)
    return dict(_STATUS)


def _run() -> None:
    base = rp.source_url()
    try:
        raw = rp._get(base + MANIFEST, rp.MAX_MANIFEST_BYTES)
        try:
            manifest = validate_manifest(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, ValueError) as e:
            raise rp.PackError(f"symbols manifest is not valid JSON ({e})") from None
        if _meta().get("manifest_sha") == rp.sha256(raw):
            os.utime(db_path())  # unchanged: fresh for another week
            _set(state="up_to_date", error="", checked_at=time.time())
            return
        _set(state="downloading")
        blob = rp._get(base + DATA, rp.MAX_FILE_BYTES)
        meta = manifest["files"][DATA]
        if len(blob) != meta["bytes"] or rp.sha256(blob) != meta["sha256"]:
            raise rp.PackError(f"{DATA}: size or checksum does not match the manifest")
        n = write_db(iter_rows(blob), raw, expect=manifest["rows"])
        _invalidate()
        _set(state="installed", error="", checked_at=time.time())
        _log(f"installed the {manifest['date']} symbol pack ({n} listings)")
    except Exception as e:  # never let the thread die without a status
        reason = str(e) if isinstance(e, rp.PackError) else f"{type(e).__name__}: {e}"
        _set(state="failed", error=reason, checked_at=time.time())
        _log(f"not updated: {reason} — ignored; the previous copy (if any) stays in use")


def _set(**kw) -> None:
    with _LOCK:
        _STATUS.update(kw)


def status() -> dict:
    """Settings -> Models & Data."""
    with _LOCK:
        st = dict(_STATUS)
    if not rp.enabled() and st["state"] not in rp.IN_FLIGHT:
        st["state"] = "disabled"
    try:
        m = json.loads(_meta().get("manifest", "null"))
    except ValueError:
        m = None
    st["installed"] = None if not m else {"date": m["date"], "rows": m["rows"]}
    return st


def reset_for_tests() -> None:
    global _THREAD
    wait(10)
    _THREAD = None
    _invalidate()
    _set(state="idle", error="", checked_at=None)


# =================================================================== lookup
_SUFFIX = re.compile(
    r"\b(the|incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings?|group"
    r"|n v|nv|s a|sa|ag|se|s p a|spa|asa|ab|oyj|a s|lp|llc|common stock|ordinary shares"
    r"|class [a-c]|adr|ads|depositary receipts?|aktiengesellschaft|societe anonyme"
    r"|public limited company|kabushiki kaisha|naamloze vennootschap|societa per azioni"
    r"|societe europeenne)\b"
)
_STOP = {"and", "of", "the", "de", "la"}
# Brand names whose company name shares no letters with them.
ALIASES = {"google": "GOOGL", "facebook": "META", "instagram": "META", "youtube": "GOOGL"}
_TYPE_ADJ = {"stock": 0.0, "index": -1.0, "etf": -1.5, "fund": -3.0}


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def name_key(name: str) -> str:
    """'Novo Nordisk A/S' -> 'novo nordisk': folded, legal forms dropped."""
    f = _fold(name)
    return " ".join(_SUFFIX.sub(" ", f).split()) or f


# Words that say what or where a company is, not which one.
_GENERIC = frozenset(
    [
        "bank",
        "banco",
        "banca",
        "banque",
        "of",
        "de",
        "del",
        "la",
        "le",
        "the",
        "and",
        "royal",
        "first",
        "national",
        "general",
        "united",
        "american",
        "international",
        "global",
        "china",
        "chinese",
        "hong",
        "kong",
        "japan",
        "korea",
        "india",
        "indian",
        "new",
        "power",
        "energy",
        "capital",
        "resources",
        "group",
        "holding",
        "holdings",
        "industries",
        "industrial",
        "grupo",
        "financiero",
        "financial",
        "investment",
        "investments",
        "trust",
        "fund",
        "etf",
        "ishares",
        "vanguard",
        "spdr",
        "amundi",
        "xtrackers",
        "lyxor",
        "invesco",
        "wisdomtree",
        "ubs",
        "world",
        "europe",
        "european",
        "asia",
        "pacific",
        "emerging",
        "market",
        "markets",
        "north",
        "south",
        "east",
        "west",
        "real",
        "estate",
        "insurance",
        "life",
        "petroleum",
        "oil",
        "gas",
        "mining",
        "metals",
        "steel",
        "motor",
        "motors",
        "electric",
        "telecom",
        "communications",
        "pharma",
        "pharmaceutical",
        "pharmaceuticals",
        "technologies",
        "technology",
        "systems",
    ]
)


def same_company(a: str | None, b: str | None) -> bool:
    """Do two listing names name one company? Equal once legal forms are
    dropped ("Iberdrola, S.A." / "Iberdrola"), or the shorter, of two words or
    more, inside the longer ("Richter Gedeon" / "Richter Gedeon Chemical
    Factory"), or the same first two words. One word inside a longer name
    is not enough: Apple is not Apple Hospitality REIT."""
    ka, kb = name_key(a or "").split(), name_key(b or "").split()
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    small, big = sorted((ka, kb), key=len)
    if len(small) < 2:
        return False
    if set(small) <= set(big):
        return True
    # Or the same first two words, when they name the company rather than
    # its kind or place: one company's lines under a translated legal name
    # ("Richter Gedeon Vegyeszeti Gyar" on Stuttgart), but Bank of America
    # is not Bank of Ireland, nor China Resources Beverage China Resources
    # Land (adversarial review, 2026-10-10).
    return ka[:2] == kb[:2] and all(w not in _GENERIC and len(w) > 2 for w in ka[:2])


def acronym(name: str) -> str:
    """'Taiwan Semiconductor Manufacturing Company Limited' -> 'tsmcl', so a
    query of 'tsmc' or 'ibm' finds the company by its initials."""
    return "".join(w[0] for w in _fold(name).split() if w not in _STOP)


@dataclass(frozen=True)
class Hit:
    ticker: str
    name: str
    type: str
    exchange: str | None
    region: str | None
    sector: str | None
    industry: str | None
    mcap_usd: float | None
    group: int
    score: float  # match quality 0..100, before the size/type tie-breaks


_COLS = "ticker, name, type, exchange, region, sector, industry, mcap_usd, adv_usd, grp"
_SELECT = f"SELECT {_COLS} FROM symbols"


def _invalidate() -> None:
    """A new file: the Market read's ticker -> name memo comes from it too.
    (Lookups are memoised per file version, so they need nothing.)"""
    try:
        from convexity import relevance

        relevance.load_company_names.cache_clear()
    except Exception:
        pass


def _sig():
    """Identifies the file's current version (path, mtime, size)."""
    p = db_path()
    try:
        st = p.stat()
    except OSError:
        return None
    return (str(p), st.st_mtime_ns, st.st_size)


def _structural(q: str, key: str, base: float) -> float:
    """Exact > prefix > whole-word substring > fuzzy. Within a tier, fewer
    extra words wins, then fewer extra letters: "samsung" is Samsung
    Electronics (one word more), not Samsung C&T (two short ones), and
    "microsoft" is not Smith Micro Software. Exact and prefix also hold with
    the spaces removed ("jp morgan" -> "jpmorgan chase")."""
    if key == q:
        return 100.0
    more = max(0, len(key.split()) - len(q.split()))
    for a, b in ((q, key), (q.replace(" ", ""), key.replace(" ", ""))):
        if b == a:
            return 98.0
        if b.startswith(a):
            return min(99.0, 96.0 - 1.5 * more - 0.05 * (len(b) - len(a)))
    if f" {q} " in f" {key} ":
        return max(75.0, 92.0 - 1.5 * more - 0.05 * (len(key) - len(q)))
    return base - 6.0


def _rank(score: float, mcap: float | None, adv: float | None, typ: str) -> float:
    """Match quality first; size (up to +6 for $10T) and type break near-ties,
    so 'apple' is Apple Inc., not a micro-cap Apple Hospitality fund. Without
    a market cap (ETFs, funds) 100 days of traded value stands in for size."""
    size = mcap or (adv * 100 if adv else None)
    size = min(6.0, max(0.0, math.log10(size) - 8.0) * 1.2) if size else 0.0
    return score + size + _TYPE_ADJ.get(typ, 0.0)


def _hit(row, score: float) -> Hit:
    return Hit(*row[:8], group=row[9], score=round(score, 1))


def lookup(query: str, *, limit: int = 5, min_score: float = 72.0) -> list[Hit]:
    """Fuzzy listing search. An exact ticker (any listing, NVO as well as
    NOVO-B.CO) comes first; names match home listings only."""
    q = (query or "").strip()
    return list(_lookup(q, min_score, _sig())[:limit]) if q else []


@lru_cache(maxsize=256)
def _lookup(q: str, min_score: float, sig) -> tuple[Hit, ...]:
    """Every hit for `q`, best first; memoised per file version (a search asks
    for the same name up to three times).

    Candidates come from SQLite: the exact ticker (or an alias), up to 300
    home listings sharing the most trigrams with the cleaned name (typos and
    partial names: "novo nordsk", "berkshire"), and initials ("tsmc"). Only
    those are scored with rapidfuzz."""
    out: dict[str, tuple[float, Hit]] = {}
    con = _connect()
    if con is None:
        return ()
    k = name_key(q)
    cand: dict = {}
    with closing(con):
        for tk in dict.fromkeys(t for t in (q.upper(), ALIASES.get(k)) if t):
            row = con.execute(_SELECT + " WHERE ticker = ?", (tk,)).fetchone()
            if row:
                out[row[0]] = (1000.0, _hit(row, 100.0))
        if len(k) >= 3:  # keys are [a-z0-9 ] only, so the quoting is safe
            # Trigrams of the name with and without spaces: "jp morgan" must
            # reach "jpmorgan chase".
            tri = {w[i : i + 3] for w in (k, k.replace(" ", "")) for i in range(len(w) - 2)}
            match = " OR ".join(f'"{t}"' for t in sorted(tri))
            cols = ", ".join("s." + c for c in _COLS.split(", "))
            for table, n in (("names", 300), ("fund_names", 100)):
                for *row, key in con.execute(
                    f"SELECT {cols}, s.key FROM {table} JOIN symbols s ON s.rowid = {table}.rowid"
                    f" WHERE {table} MATCH ? ORDER BY rank LIMIT {n}",
                    (match,),
                ):
                    cand[row[0]] = (row, _structural(k, key, fuzz.WRatio(k, key)))
        if " " not in k and 3 <= len(k) <= 6:
            for row in con.execute(
                _SELECT + " WHERE home = 1 AND acr >= ? AND acr < ? LIMIT 200", (k, k + "~")
            ):
                cand[row[0]] = (row, max(cand.get(row[0], (None, 0.0))[1], 90.0))
    for row, s in cand.values():
        if s >= min_score and row[0] not in out:
            out[row[0]] = (_rank(s, row[7], row[8], row[2]), _hit(row, s))
    return tuple(h for _r, h in sorted(out.values(), key=lambda x: -x[0]))


# Venues that mostly re-list companies whose home is elsewhere: German
# regional exchanges, Cboe/LSE international order books, OTC, and the Latin
# American foreign-share boards (NVDACL.SN). Shared with symbol_build.py.
SECONDARY = {"FRA", "STU", "BER", "MUN", "HAM", "HAN", "DUS", "IOB", "CXE", "CXA", "DXE", "AQS", "TLO",
             "NEO", "PNK", "OQB", "OQX", "MEX", "SGO"}  # fmt: skip
# Re-listing lines whose venue code alone does not say so: the LSE's
# international order book (0KZC.L, a London line of SPY) and Brazilian
# depositary receipts (AAPL34.SA; local shares end in 3, 4 or 11).
IOB = re.compile(r"^0[A-Z0-9]{3}\.L$")
BDR = re.compile(r"^[A-Z0-9]{4}3[1-9]\.SA$")


def relisting(ticker: str, exchange: str | None) -> bool:
    return exchange in SECONDARY or bool(IOB.match(ticker) or BDR.match(ticker))


def _listings(group: int) -> list[tuple]:
    """A company's real listings (no re-listing venues), most traded first."""
    con = _connect()
    if con is None:
        return []
    with closing(con):
        rows = con.execute(
            _SELECT + " WHERE grp = ? ORDER BY COALESCE(adv_usd, 0) DESC, home DESC", (group,)
        ).fetchall()
    return [r for r in rows if not relisting(r[0], r[3])]


def listings(group: int) -> list[Hit]:
    """Every listing of a company, re-listing venues included, most traded
    first (an import's proxy may need a Frankfurt line)."""
    con = _connect() if group != YAHOO_GROUP else None
    if con is None:
        return []
    with closing(con):
        rows = con.execute(
            _SELECT + " WHERE grp = ? ORDER BY home DESC, COALESCE(adv_usd, 0) DESC", (group,)
        ).fetchall()
    return [_hit(r, 100.0) for r in rows]


def top(group: int, exchanges=()) -> Hit | None:
    """The company's most traded listing — what a card shows and adds (NVO
    for Novo Nordisk, 7203.T for Toyota); its home listing when none trades.
    With `exchanges`, the most traded listing there (SKHY for SK hynix on a
    Nasdaq screen), when it has one."""
    rows = _listings(group)
    rows = [r for r in rows if r[3] in exchanges] or rows if exchanges else rows
    return _hit(rows[0], 100.0) if rows else home(group)


def on_exchanges(group: int, codes) -> bool:
    """Does any listing of this company trade on one of these exchanges?"""
    con = _connect()
    if con is None:
        return False
    with closing(con):
        q = f"SELECT 1 FROM symbols WHERE grp = ? AND exchange IN ({','.join('?' * len(codes))})"
        return con.execute(q, (group, *codes)).fetchone() is not None


_ALT_SHARE = 0.05  # an alternative must trade >= 5% of the top listing's value


def alternates(group: int, exclude: str = "") -> list[dict]:
    """Other listings someone would actually buy: main exchanges and ADRs
    trading at least 5% of the top listing's value, most traded first.
    Re-listing venues (German regional, order books, OTC, BDRs) and thin
    lines (an Argentine CEDEAR) are left out."""
    rows = _listings(group)
    floor = (rows[0][8] or 0) * _ALT_SHARE if rows else 0
    return [{"ticker": r[0], "exchange": r[3], "region": r[4]} for r in rows
            if r[0] != exclude and (r[8] or 0) >= floor][:6]  # fmt: skip


def home(group: int) -> Hit | None:
    """A company's home listing."""
    con = _connect()
    if con is None:
        return None
    with closing(con):
        row = con.execute(_SELECT + " WHERE grp = ? AND home = 1", (group,)).fetchone()
    return _hit(row, 100.0) if row else None


def research_candidates(symbol: str) -> list[str]:
    """Other listings of the same company that may carry the research a thin
    listing lacks (analyst targets, ratings, fundamentals): its home line and
    its US line, home first. Pack only, never the network; empty for a
    listing that is its own home, or one the pack doesn't group.

    NOV.DE -> [NOVO-B.CO, NVO]; CCC3.DE -> [KO]."""
    s = (symbol or "").upper()
    me = get(s) if s else None
    if me is None or me.group == YAHOO_GROUP or me.type != "stock":
        return []
    out = []
    h = home(me.group)
    if h and h.ticker != s:
        out.append(h.ticker)
    us = us_line(s)
    if us and us != s and us not in out:
        out.append(us)
    return out


def get(ticker: str) -> Hit | None:
    con = _connect()
    if con is None:
        return None
    with closing(con):
        row = con.execute(_SELECT + " WHERE ticker = ?", ((ticker or "").upper(),)).fetchone()
    return _hit(row, 100.0) if row else None


# Exchange lines before OTC ones when a company has several US lines (TTE
# over a TTE pink sheet); the home line beats both (KO for CCC3.DE).
_US_EXCHANGES = {"NYQ", "NMS", "NGM", "NCM", "NAS", "ASE", "BTS", "PCX"}
# US lines found off the hot path by a name lookup (the pack misses some
# ADRs: SCGLY for Société Générale). {symbol: US ticker}, this process only.
_US_LEARNED: dict[str, str] = {}


def us_line(symbol: str) -> str | None:
    """The same company's US line, for sources that only cover US tickers
    (Finnhub's free plan answers 403 to REP.MC, but REPYY has its analysts
    and news). A US ticker is its own line; a company with none (Philogen,
    a UCITS ETF) is None. Reads only the pack (and what `learn_us_line`
    found), never the network: `fetch_one`'s hot path calls it."""
    s = (symbol or "").upper()
    if s in _US_LEARNED:
        return _US_LEARNED[s] or None
    return _us_line(s, _sig())


def discover_us_line(symbol: str) -> str | None:
    """`us_line`, plus Yahoo's live Lookup by the company's name when the
    pack has no US line for a company that may have one (SCGLY, Société
    Générale's ADR, is missing from the screener). A network call, so never
    from the hot path: the news refresh asks, and the answer (a miss too)
    is kept for this process, where `us_line` then finds it."""
    s = (symbol or "").upper()
    found = us_line(s)
    if found or s in _US_LEARNED:
        return found
    me = get(s)
    if me is None or me.type != "stock" or me.group == YAHOO_GROUP:
        return None
    try:
        hits = yahoo_lookup(me.name, limit=10)
    except Exception:
        return None  # a timeout is asked again next time
    us = [h for h in hits if _looks_us(h.ticker) and same_company(h.name, me.name)]
    # Exchange lines, then sponsored ADRs (five letters ending in Y: SCGLY,
    # not the ordinary-share pink sheet SCGLF), then Yahoo's order.
    us.sort(key=lambda h: (h.exchange not in _US_EXCHANGES, not h.ticker.endswith("Y")))
    _US_LEARNED[s] = us[0].ticker if us else ""
    return us[0].ticker if us else None


def _looks_us(ticker: str) -> bool:
    return bool(ticker) and not any(c in ticker for c in ".^=")


@lru_cache(maxsize=2048)
def _us_line(s: str, sig) -> str | None:
    me = get(s) if s else None
    if me is None:
        # Not in the pack (or no pack yet): a plain ticker is taken as US,
        # which is what every caller assumed before this lookup existed.
        return s if _looks_us(s) else None
    if me.region == "us":
        return s
    lines = _us_lines(s, sig)
    return lines[0] if lines else None


def us_lines(symbol: str) -> list[str]:
    """Every US line of the company in the pack, best first (`us_line`'s
    order), plus what `discover_us_line` learned. For callers that judge
    the lines themselves: the σ/μ chain search ranks them by option open
    interest (an ADR's chain, never its pink sheet's empty one)."""
    s = (symbol or "").upper()
    out = list(_us_lines(s, _sig())) if s else []
    learned = _US_LEARNED.get(s)
    if learned and learned not in out:
        out.append(learned)
    return out


# Yahoo's screener leaves `region` empty for its OTC "OID" board (1,029
# lines, 2026-10-11): sponsored ADRs like NSRGY, ISNPY and SCGLY, which a
# region = 'us' filter alone never saw. They count as US lines.
@lru_cache(maxsize=2048)
def _us_lines(s: str, sig) -> tuple[str, ...]:
    me = get(s) if s else None
    if me is None or me.group == YAHOO_GROUP or me.type != "stock":
        return ()
    con = _connect()
    if con is None:
        return ()
    with closing(con):
        rows = con.execute(
            "SELECT ticker, exchange, home, COALESCE(adv_usd, 0) FROM symbols"
            " WHERE grp = ? AND (region = 'us' OR (region IS NULL AND exchange = 'OID'))",
            (me.group,),
        ).fetchall()
    rows = [r for r in rows if _looks_us(r[0])]
    rows.sort(key=lambda r: (-r[2], r[1] not in _US_EXCHANGES, -r[3]))
    return tuple(r[0] for r in rows)


def category(
    *,
    sectors=(),
    industries=(),
    regions=(),
    types=("stock",),
    exchanges=(),
    limit: int = 5,
    offset: int = 0,
) -> list[Hit]:
    """Largest home listings matching every non-empty filter; `exchanges`
    keeps companies with any listing there (ASML is on Nasdaq too)."""
    where, args = ["home = 1"], []
    if exchanges:
        where.append(
            f"grp IN (SELECT grp FROM symbols WHERE exchange IN ({','.join('?' * len(exchanges))}))"
        )
        args += list(exchanges)
    for col, vals in (
        ("sector", sectors),
        ("industry", industries),
        ("region", regions),
        ("type", types),
    ):
        if vals:
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            args += list(vals)
    con = _connect()
    if con is None:
        return []
    with closing(con):
        rows = con.execute(
            f"{_SELECT} WHERE {' AND '.join(where)}"
            " ORDER BY COALESCE(mcap_usd, adv_usd * 100) DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    return [_hit(r, 100.0) for r in rows]


# ------------------------------------------------------------ Yahoo fallback
# Yahoo's screener, which the pack is built from, does not list every
# listing: Gedeon Richter (Budapest) is in no screener result at all, on any
# venue (pack of 2026-10-04). Its live Lookup endpoint does know it. So a
# name the pack can't place is looked up live, verified (a line must have a
# close in the last three months: renamed and dead lines have none) and
# cached. Group -1: these listings belong to no pack group.
YAHOO_GROUP = -1
_YL: dict[str, tuple[float, tuple]] = {}
_YL_LOCK = threading.Lock()
_YL_TTL_S, _YL_EMPTY_TTL_S = 24 * 3600.0, 600.0
_YL_TYPES = {"equity": "stock", "etf": "etf"}


@lru_cache(maxsize=1)
def _exch_region() -> dict:
    try:
        from yfinance import const

        maps = (const.EQUITY_SCREENER_EQ_MAP, const.ETF_SCREENER_EQ_MAP)
        return {x: r for m in maps for r, xs in m["exchange"].items() for x in xs}
    except Exception:
        return {}


def _yl_name(raw) -> str:
    n = " ".join(raw.split())[:160] if isinstance(raw, str) else ""
    return n.title() if n.isupper() else n


def _yl_clean(n: str) -> bool:
    """A name worth showing: words ("Apple Inc."), not a bare ticker-like
    word ("RICHTER") or abbreviations ("Richt.Gede.Veg.Gyar Uf100")."""
    return bool(n) and not re.search(r"\.\w", n) and len(n.split()) >= 2


def _yahoo_fetch(q: str, timeout: float) -> tuple:
    import concurrent.futures as cf

    import yfinance as yf

    def work():
        df = yf.Lookup(q).get_all(count=25)
        if df is None or not len(df):
            return ()
        rows = [
            (str(tk).upper(), r) for tk, r in df.iterrows()
            if _YL_TYPES.get(str(r.get("quoteType") or "").lower())
            and _TICKER.match(str(tk).upper()) and "-USD" not in str(tk).upper()
        ][:12]  # fmt: skip
        if not rows:
            return ()
        closes = yf.download([t for t, _ in rows], period="3mo", auto_adjust=False,
                             progress=False, threads=True)["Close"]  # fmt: skip
        seen = closes.notna().sum() if hasattr(closes, "notna") else {}
        # The fullest name among the lines stands for the company ("RICHTER"
        # on Budapest, "Richter Gedeon Vegyeszeti Gyar" on Frankfurt).
        names = [_yl_name(r.get("shortName")) for _, r in rows]
        out = []
        for i, (tk, r) in enumerate(rows):
            n = int(seen.get(tk, 0)) if hasattr(seen, "get") else 0
            if n < 1:
                continue
            nm = names[i]
            if not _yl_clean(nm):
                # Borrow only a name that contains this one's word ("RICHTER"
                # in "Richter Gedeon..."), or shares the start of an
                # abbreviation ("Richt.Gede..."): never AIRBUS -> Airbnb.
                own = name_key(nm).split()
                stem = name_key(nm)[:4]
                fuller = [x for x in names if _yl_clean(x) and own and (
                    (len(own) == 1 and own[0] in name_key(x).split())
                    or (re.search(r"\.\w", nm) and name_key(x)[:4] == stem))]  # fmt: skip
                nm = max(fuller, key=len) if fuller else nm
            if not nm:  # a nameless line can't be told apart from another company
                continue
            ex = str(r.get("exchange") or "") or None
            sector = r.get("industryName") if isinstance(r.get("industryName"), str) else None
            hit = Hit(tk, nm, _YL_TYPES[str(r.get("quoteType")).lower()], ex,
                      _exch_region().get(ex), sector, None, None, YAHOO_GROUP, 100.0)  # fmt: skip
            # Home venues first in Yahoo's own order, then re-listing lines
            # by how much history they have.
            out.append(((relisting(tk, ex), -n if relisting(tk, ex) else 0, i), hit))
        return tuple(h for _k, h in sorted(out, key=lambda x: x[0]))

    pool = cf.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(work).result(timeout=timeout)
    finally:
        pool.shutdown(wait=False)


def yahoo_lookup(query: str, *, limit: int = 6, timeout: float = 8.0) -> list[Hit]:
    """Listings for ``query`` from Yahoo's live Lookup, for names the pack
    lacks. Verified and cached (a day; an empty answer ten minutes); never
    raises: a failure is an empty list and a log line."""
    q = " ".join((query or "").split())
    if not 2 <= len(q) <= 60:
        return []
    key = q.lower()
    now = time.time()
    with _YL_LOCK:
        c = _YL.get(key)
    if c and now - c[0] < (_YL_TTL_S if c[1] else _YL_EMPTY_TTL_S):
        return list(c[1][:limit])
    try:
        hits = _yahoo_fetch(q, timeout)
    except Exception as e:
        _log(f"Yahoo lookup for {q!r} failed: {type(e).__name__}")
        hits = ()
    with _YL_LOCK:
        if len(_YL) > 200:
            _YL.clear()
        _YL[key] = (now, hits)
    return list(hits[:limit])
