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
        tk, name, typ, *labels, mcap, grp, home = r
        if not isinstance(tk, str) or not _TICKER.match(tk):
            raise rp.PackError(f"symbols[{i}]: bad ticker")
        _text(name, f"symbols[{i}].name", 160, optional=False)
        if typ not in TYPES:
            raise rp.PackError(f"symbols[{i}]: bad type")
        for k, v in zip(_LABELS, labels, strict=True):
            _text(v, f"symbols[{i}].{k}", 64)
        rp._num(mcap, f"symbols[{i}].mcap_usd")
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
                    sector TEXT, industry TEXT, mcap_usd REAL, grp INTEGER NOT NULL,
                    home INTEGER NOT NULL);
                CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
                """
            )
            try:
                con.executemany("INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", values())
            except sqlite3.IntegrityError:
                raise rp.PackError(f"{DATA}: a ticker appears twice") from None
            if not n or (expect is not None and n != expect):
                raise rp.PackError(f"{DATA}: {n} rows, the manifest says {expect}")
            # Name search runs in SQLite, not in memory (a Python index of
            # ~400k names cost ~300 MB): a trigram index proposes candidates,
            # rapidfuzz ranks them. Home listings only.
            con.executescript(
                """
                CREATE INDEX symbols_grp ON symbols(grp);
                CREATE INDEX symbols_home ON symbols(home, mcap_usd);
                CREATE INDEX symbols_acr ON symbols(acr) WHERE home = 1;
                CREATE VIRTUAL TABLE names USING fts5(key, tokenize='trigram', content='', detail='none');
                INSERT INTO names(rowid, key) SELECT rowid, key FROM symbols WHERE home = 1;
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
        _THREAD = threading.Thread(target=_run, name="pt-symbol-pack", daemon=True)
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
    r"|public limited company|kabushiki kaisha|naamloze vennootschap|societa per azioni)\b"
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


_COLS = "ticker, name, type, exchange, region, sector, industry, mcap_usd, grp"
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
    """Exact > prefix > whole-word substring > fuzzy, each shorter-is-better:
    without the length penalty 'microsoft' scores Smith Micro Software as
    high as Microsoft."""
    if key == q:
        return 100.0
    extra = len(key) - len(q)
    if key.startswith(q):
        return min(99.0, 96.0 - extra * 0.4)
    if f" {q} " in f" {key} ":
        return max(75.0, 92.0 - extra * 0.6)
    return base - 6.0


def _rank(score: float, mcap: float | None, typ: str) -> float:
    """Match quality first; size (up to +6 for $10T) and type break near-ties,
    so 'apple' is Apple Inc., not a micro-cap Apple Hospitality fund."""
    size = min(6.0, max(0.0, math.log10(mcap) - 8.0) * 1.2) if mcap else 0.0
    return score + size + _TYPE_ADJ.get(typ, 0.0)


def _hit(row, score: float) -> Hit:
    return Hit(*row[:8], group=row[8], score=round(score, 1))


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
            match = " OR ".join(f'"{k[i : i + 3]}"' for i in range(len(k) - 2))
            cols = ", ".join("s." + c for c in _COLS.split(", "))
            for *row, key in con.execute(
                f"SELECT {cols}, s.key FROM names JOIN symbols s ON s.rowid = names.rowid"
                " WHERE names MATCH ? ORDER BY rank LIMIT 300",
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
            out[row[0]] = (_rank(s, row[7], row[2]), _hit(row, s))
    return tuple(h for _r, h in sorted(out.values(), key=lambda x: -x[0]))


def alternates(group: int, exclude: str = "") -> list[dict]:
    """The other listings of one company, largest first."""
    con = _connect()
    if con is None:
        return []
    with closing(con):
        rows = con.execute(
            "SELECT ticker, exchange, region FROM symbols WHERE grp = ? AND ticker != ?"
            " ORDER BY home DESC, mcap_usd DESC LIMIT 12",
            (group, exclude),
        ).fetchall()
    return [{"ticker": t, "exchange": x, "region": r} for t, x, r in rows]


def home(group: int) -> Hit | None:
    """A company's home listing."""
    con = _connect()
    if con is None:
        return None
    with closing(con):
        row = con.execute(_SELECT + " WHERE grp = ? AND home = 1", (group,)).fetchone()
    return _hit(row, 100.0) if row else None


def get(ticker: str) -> Hit | None:
    con = _connect()
    if con is None:
        return None
    with closing(con):
        row = con.execute(_SELECT + " WHERE ticker = ?", ((ticker or "").upper(),)).fetchone()
    return _hit(row, 100.0) if row else None


def category(
    *,
    sectors=(),
    industries=(),
    regions=(),
    types=("stock",),
    limit: int = 5,
    offset: int = 0,
) -> list[Hit]:
    """Largest home listings matching every non-empty filter."""
    where, args = ["home = 1"], []
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
            f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY mcap_usd DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    return [_hit(r, 100.0) for r in rows]
