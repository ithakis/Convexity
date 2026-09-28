"""The reference pack: a daily Market read of the S&P 500, built in CI.

A new install has fewer than ml_sentiment.MIN_LIVE_HISTORY scores of its own,
so its Market read percentile falls back to the 2023 training knots, and its
Track record says "too early" for months. The reference pack fixes both from
day one: `.github/workflows/reference-pack.yml` runs `convexity
build-reference-pack` (reference_build.py) every weekday after the US close
and publishes three files to the rolling GitHub release `reference-pack`:

    manifest.json     date, row counts, model + schema version, SHA-256 + size
                      of the two data files
    anchor.json.gz    the last 90 days of raw Market read scores (date,
                      symbol, score) — the percentile reference
    history.json.gz   one record per ticker-day, shaped like the local
                      sentiment history plus the forward returns joined once
                      known — the "Model (500 names)" Track record

What is in the pack is DATA, never code, and small: tickers, dates and derived
numbers. No headline, summary or URL (news licensing) — the validators below
reject any field that is not on the allow-list, so a pack carrying text could
not even be loaded. Nothing from users is ever uploaded.

This module is the one place that defines the format. The builder writes
through it and verifies a previous pack with it; the app validates every
download with it. Stdlib only, json only — never pickle, never exec.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import zlib
from pathlib import Path

SCHEMA_VERSION = 1
MANIFEST = "manifest.json"
ANCHOR = "anchor.json.gz"
HISTORY = "history.json.gz"
DATA_FILES = (ANCHOR, HISTORY)

# The anchor window: the same 90 days the live anchor uses
# (ml_sentiment.LIVE_WINDOW_DAYS), and the history horizon the local
# sentiment history keeps (news_sentiment._HISTORY_MAX_DAYS).
ANCHOR_DAYS = 90
HISTORY_DAYS = 400

# Caps. A full history (400 days x ~500 names) is ~30 MB of JSON and ~5 MB
# gzipped; the caps leave room to grow but a hostile or broken server cannot
# fill the disk or memory.
MAX_MANIFEST_BYTES = 64 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 256 * 1024 * 1024

TIERS = ("very_bearish", "bearish", "no_edge", "bullish", "very_bullish")

# The ONLY keys a history record may carry. Strings are limited to date,
# symbol, tier and model id; everything else is a number or null.
HISTORY_NUMERIC = (
    "market_score",
    "market_sar",
    "market_z",
    "market_pct",
    "n_articles",
    "price",
    "beta",
    "fwd_1d",
    "fwd_5d",
)
HISTORY_FIELDS = ("date", "symbol", "market_tier", "market_model", *HISTORY_NUMERIC)

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SYMBOL = re.compile(r"^[A-Z0-9^][A-Z0-9.^=-]{0,11}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MODEL = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,39}$")


class PackError(Exception):
    """A pack (or one of its files) that must not be used; the message is the
    user-facing reason shown in Settings and the log."""


# ----------------------------------------------------------------- bytes
def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gunzip_json(data: bytes, max_bytes: int = MAX_JSON_BYTES):
    """Decompress a gzip member and parse it as JSON, capped.

    zlib's max_length bounds each step, so a small file that expands to
    gigabytes (a "gzip bomb") fails at the cap instead of exhausting memory;
    a truncated stream fails because the decompressor never reaches EOF."""
    d = zlib.decompressobj(wbits=31)  # gzip container only
    out = bytearray()
    buf = data
    try:
        while True:
            chunk = d.decompress(buf, 1024 * 1024)
            out += chunk
            if len(out) > max_bytes:
                raise PackError(f"decompresses past the {max_bytes}-byte cap")
            buf = d.unconsumed_tail
            if d.eof or (not buf and not chunk):
                break
    except zlib.error as e:
        raise PackError(f"not valid gzip ({e})") from None
    if not d.eof:
        raise PackError("gzip stream truncated")
    if d.unused_data.strip(b"\0"):
        raise PackError("trailing data after the gzip stream")
    try:
        return json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PackError(f"not valid JSON ({e})") from None


def gzip_json(obj) -> bytes:
    """Deterministic gzip of compact JSON (mtime 0, no file name), so the
    same content always hashes the same."""
    import gzip
    import io

    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(raw)
    return buf.getvalue()


# ----------------------------------------------------------------- validation
def _num(v, what: str) -> None:
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise PackError(f"{what}: not a finite number")


def validate_manifest(m, model_version: str | None = None) -> dict:
    """The manifest's shape; with `model_version`, also that the pack was
    scored by that model (another model's scores are on another scale)."""
    if not isinstance(m, dict):
        raise PackError("manifest is not an object")
    if m.get("schema_version") != SCHEMA_VERSION:
        raise PackError(
            f"unsupported schema_version {m.get('schema_version')!r} (expected {SCHEMA_VERSION})"
        )
    mv = m.get("model_version")
    if not isinstance(mv, str) or not _MODEL.match(mv):
        raise PackError("manifest: bad model_version")
    if model_version is not None and mv != model_version:
        raise PackError(f"pack scored by {mv}, this app runs {model_version}")
    if not isinstance(m.get("date"), str) or not _DATE.match(m["date"]):
        raise PackError("manifest: bad date")
    files = m.get("files")
    if not isinstance(files, dict) or set(files) != set(DATA_FILES):
        raise PackError(f"manifest: files must be exactly {list(DATA_FILES)}")
    for name, meta in files.items():
        if not isinstance(meta, dict) or not _HEX64.match(str(meta.get("sha256", ""))):
            raise PackError(f"manifest: bad sha256 for {name}")
        b = meta.get("bytes")
        if isinstance(b, bool) or not isinstance(b, int) or not 0 < b <= MAX_FILE_BYTES:
            raise PackError(f"manifest: bad size for {name}")
    rows = m.get("rows")
    if not isinstance(rows, dict) or not all(
        isinstance(rows.get(k), int) and rows[k] >= 0 for k in ("history", "anchor")
    ):
        raise PackError("manifest: bad row counts")
    return m


def validate_history(d, model_version: str) -> list[dict]:
    if not isinstance(d, dict) or d.get("schema") != SCHEMA_VERSION:
        raise PackError("history: wrong schema")
    recs = d.get("records")
    if not isinstance(recs, list):
        raise PackError("history: records is not a list")
    for i, r in enumerate(recs):
        if not isinstance(r, dict):
            raise PackError(f"history[{i}]: not an object")
        extra = set(r) - set(HISTORY_FIELDS)
        if extra:
            raise PackError(f"history[{i}]: unexpected field(s) {sorted(extra)}")
        if not _DATE.match(str(r.get("date", ""))) or not _SYMBOL.match(str(r.get("symbol", ""))):
            raise PackError(f"history[{i}]: bad date or symbol")
        if r.get("market_model") not in (None, model_version):
            raise PackError(f"history[{i}]: scored by another model")
        if r.get("market_tier") not in (None, *TIERS):
            raise PackError(f"history[{i}]: bad tier")
        for k in HISTORY_NUMERIC:
            _num(r.get(k), f"history[{i}].{k}")
    return recs


def validate_anchor(d, model_version: str) -> list[list]:
    if not isinstance(d, dict) or d.get("schema") != SCHEMA_VERSION:
        raise PackError("anchor: wrong schema")
    if d.get("model_version") != model_version:
        raise PackError("anchor: scored by another model")
    rows = d.get("rows")
    if not isinstance(rows, list):
        raise PackError("anchor: rows is not a list")
    for i, r in enumerate(rows):
        if (
            not isinstance(r, list)
            or len(r) != 3
            or not _DATE.match(str(r[0]))
            or not _SYMBOL.match(str(r[1]))
        ):
            raise PackError(f"anchor[{i}]: expected [date, symbol, score]")
        if r[2] is None:
            raise PackError(f"anchor[{i}]: missing score")
        _num(r[2], f"anchor[{i}]")
    return rows


def verify_files(manifest: dict, blobs: dict[str, bytes], model_version: str) -> tuple:
    """Hash + size of each data file against the manifest, then parse and
    validate them. Returns (history records, anchor rows)."""
    for name in DATA_FILES:
        data = blobs.get(name)
        meta = manifest["files"][name]
        if data is None:
            raise PackError(f"{name} missing")
        if len(data) != meta["bytes"]:
            raise PackError(f"{name}: size {len(data)} != manifest {meta['bytes']}")
        got = sha256(data)
        if got != meta["sha256"]:
            raise PackError(
                f"{name}: checksum mismatch (got {got[:12]}…, manifest {meta['sha256'][:12]}…)"
            )
    history = validate_history(gunzip_json(blobs[HISTORY]), model_version)
    anchor = validate_anchor(gunzip_json(blobs[ANCHOR]), model_version)
    return history, anchor


def load_dir(d: Path, model_version: str) -> tuple[dict, list[dict], list[list]]:
    """Read and fully verify a pack in a directory (the builder's --previous,
    the app's installed copy). Raises PackError."""
    d = Path(d)
    try:
        raw = (d / MANIFEST).read_bytes()
    except OSError as e:
        raise PackError(f"no manifest ({e.strerror or e})") from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise PackError("manifest too large")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PackError(f"manifest is not valid JSON ({e})") from None
    validate_manifest(manifest, model_version)
    blobs = {}
    for name in DATA_FILES:
        p = d / name
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                raise PackError(f"{name} too large")
            blobs[name] = p.read_bytes()
        except OSError as e:
            raise PackError(f"{name} unreadable ({e.strerror or e})") from None
    history, anchor = verify_files(manifest, blobs, model_version)
    return manifest, history, anchor
