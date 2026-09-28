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

App side (bottom half): `start()` downloads the pack in a daemon thread when
the local copy is older than 24 h — at boot and at the start of each news
refresh. Trust model: the release asset is REPLACED every day, so unlike the
model (model_fetch.MODEL_SHA256) no hash can be pinned in code; what protects
the app is HTTPS to GitHub, the manifest's SHA-256 + size for each file, hard
size caps, and full schema validation of data that is only ever parsed as JSON
into numbers. Any failure is logged (`[reference_pack]`), shown in Settings ->
Models & Data, and otherwise ignored: the previous copy stays, and without one
the Market read simply keeps its training anchor.

Privacy: the download reveals to GitHub that a copy of Convexity is running —
never what it holds; nothing is sent but the GET. Settings has the switch
(`<data>/state/reference_pack.json`); CONVEXITY_REFERENCE_PACK=0 forces it off.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

from convexity import paths

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


# =================================================================== app side
BASE_URL = "https://github.com/ithakis/Convexity/releases/download/reference-pack/"
FRESH_S = 24 * 3600  # re-check at most once a day
MAX_AGE_DAYS = 14  # an older pack is not used (the workflow has been failing)

_TIMEOUT_S = 30.0
_CHUNK = 64 * 1024
_ATTEMPTS = 2
_BACKOFF_S = 3.0

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STATUS: dict = {"state": "idle", "error": "", "checked_at": None, "source": ""}
_LOADED: dict = {"key": None, "manifest": None, "history": None, "anchor": None, "error": ""}
IN_FLIGHT = ("checking", "downloading")


def _log(msg: str) -> None:
    print(f"[reference_pack] {msg}", flush=True)


def _model_version() -> str:
    from convexity import ml_sentiment  # module import is cheap; the model is lazy

    return ml_sentiment.ARTIFACT_VERSION


def source_url() -> str:
    base = os.environ.get("CONVEXITY_REFERENCE_URL", "").strip() or BASE_URL
    return base if base.endswith("/") else base + "/"


def _settings_file() -> Path:
    return paths.state_file("reference_pack")


def env_disabled() -> bool:
    return os.environ.get("CONVEXITY_REFERENCE_PACK", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )


def enabled() -> bool:
    """On unless switched off in Settings or by CONVEXITY_REFERENCE_PACK=0.
    Off means neither downloaded NOR used — the Market read goes back to its
    own history and the training anchor."""
    if env_disabled():
        return False
    try:
        return json.loads(_settings_file().read_text("utf-8")).get("enabled", True) is not False
    except (OSError, ValueError, AttributeError):
        return True


def set_enabled(on: bool) -> dict:
    f = _settings_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"enabled": bool(on)}) + "\n", encoding="utf-8")
    os.replace(tmp, f)
    _log(f"download {'enabled' if on else 'disabled'} in Settings")
    if on:
        start()
    return status()


def _set(**kw) -> None:
    with _LOCK:
        _STATUS.update(kw)


# ------------------------------------------------------------------ download
def _get(url: str, cap: int) -> bytes:
    """GET with a byte cap. https only (http for loopback: the test stub),
    re-checked after GitHub's redirect to its CDN."""
    from convexity.model_fetch import FetchError, check_url

    check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "convexity-reference-pack"})
    last: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                check_url(resp.geturl())
                total = resp.headers.get("Content-Length")
                if total and total.isdigit() and int(total) > cap:
                    raise PackError(f"{url.rsplit('/', 1)[-1]} too large ({total} bytes)")
                buf = bytearray()
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > cap:
                        raise PackError(f"{url.rsplit('/', 1)[-1]} exceeded {cap} bytes")
                if total and total.isdigit() and len(buf) != int(total):
                    raise PackError(f"download truncated ({len(buf)} of {total} bytes)")
                return bytes(buf)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise PackError("no reference pack published yet (HTTP 404)") from None
            last = PackError(f"HTTP {e.code}")
            if e.code < 500 and e.code != 429:
                raise last from None
        except urllib.error.URLError as e:
            last = PackError(f"network unavailable ({e.reason})")
        except FetchError as e:
            raise PackError(str(e)) from None
        except OSError as e:
            last = PackError(f"network error ({type(e).__name__}: {e})")
        if attempt < _ATTEMPTS:
            time.sleep(_BACKOFF_S * attempt)
    raise last or PackError("download failed")


def _fresh() -> bool:
    try:
        age = time.time() - (paths.reference_dir() / MANIFEST).stat().st_mtime
    except OSError:
        return False
    return 0 <= age < FRESH_S


def start(force: bool = False) -> dict:
    """Check for a newer pack in a daemon thread. No-op while disabled, while
    one is running, or (unless `force`, the Settings "Check now") when the
    local copy was checked in the last 24 h. Never blocks, never raises."""
    global _THREAD
    on = enabled()
    # A copy checked in the last 24 h is left alone — unless it is not usable
    # (e.g. scored by a model this app no longer runs). Outside _LOCK:
    # _load_local takes it.
    fresh = on and not force and _fresh() and _load_local(quiet=True)
    with _LOCK:
        if _STATUS["state"] in IN_FLIGHT:
            return dict(_STATUS)
        if not on:
            _STATUS.update(state="disabled", error="")
            return dict(_STATUS)
        if fresh:
            if _STATUS["state"] in ("idle", "disabled"):  # e.g. switched back on
                _STATUS["state"] = "up_to_date"
            return dict(_STATUS)
        _STATUS.update(state="checking", error="")
        _THREAD = threading.Thread(target=_run, name="pt-reference-pack", daemon=True)
        _THREAD.start()
        return dict(_STATUS)


def wait(timeout: float | None = None) -> dict:
    """Test hook: block until the current check ends."""
    t = _THREAD
    if t is not None:
        t.join(timeout)
    return dict(_STATUS)


def _run() -> None:
    base = source_url()
    try:
        _set(source=urllib.parse.urlparse(base).hostname or "")
        mv = _model_version()
        raw = _get(base + MANIFEST, MAX_MANIFEST_BYTES)
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise PackError(f"manifest is not valid JSON ({e})") from None
        validate_manifest(manifest, mv)
        dest = paths.reference_dir()
        try:
            same = (dest / MANIFEST).read_bytes() == raw
        except OSError:
            same = False
        if same and _load_local(quiet=True):
            os.utime(dest / MANIFEST)  # re-checked: fresh for another 24 h
            _set(state="up_to_date", error="", checked_at=time.time())
            return
        # Never go backwards: a published pack older than the installed, valid
        # one (a restored asset, a stale mirror) would replace newer data.
        with _LOCK:
            have = _LOADED["manifest"] if _LOADED["key"] else None
        if have is None and _load_local(quiet=True):
            with _LOCK:
                have = _LOADED["manifest"]
        if have is not None and manifest["date"] < have["date"]:
            raise PackError(
                f"published pack ({manifest['date']}) is older than the installed one "
                f"({have['date']})"
            )
        _set(state="downloading")
        blobs = {name: _get(base + name, MAX_FILE_BYTES) for name in DATA_FILES}
        verify_files(manifest, blobs, mv)  # hash, size, gunzip caps, schema
        _install(dest, raw, blobs)
        _invalidate()
        _set(state="installed", error="", checked_at=time.time())
        _log(
            f"installed the {manifest['date']} pack ({manifest['rows']['history']} history "
            f"rows, {manifest['rows']['anchor']} anchor rows)"
        )
    except PackError as e:
        _fail(str(e))
    except Exception as e:  # never let the thread die without a status
        _fail(f"{type(e).__name__}: {e}")


def _fail(reason: str) -> None:
    _set(state="failed", error=reason, checked_at=time.time())
    _log(f"not updated: {reason} — ignored; the previous copy (if any) stays in use")


def _install(dest: Path, manifest_raw: bytes, blobs: dict[str, bytes]) -> None:
    """Staging dir + per-file os.replace, manifest LAST: a reader that sees
    the new manifest also sees the files it describes (and load_dir re-verifies
    the hashes anyway, so a crash in between is caught, not trusted)."""
    dest.mkdir(parents=True, exist_ok=True)
    staging = dest / f".staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        for name, data in blobs.items():
            (staging / name).write_bytes(data)
        (staging / MANIFEST).write_bytes(manifest_raw)
        for name in (*DATA_FILES, MANIFEST):
            os.replace(staging / name, dest / name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ------------------------------------------------------------------ readers
def _invalidate() -> None:
    with _LOCK:
        _LOADED.update(key=None, manifest=None, history=None, anchor=None, error="")


def _load_local(quiet: bool = False) -> bool:
    """Load (and fully re-verify) the installed copy, cached until it changes."""
    d = paths.reference_dir()
    try:
        raw = (d / MANIFEST).read_bytes()[: MAX_MANIFEST_BYTES + 1]
    except OSError:
        with _LOCK:
            _LOADED.update(key=None, manifest=None, history=None, anchor=None, error="")
        return False
    # Keyed by the manifest's own bytes: a re-check that only touched its
    # mtime keeps the loaded copy; a new pack (new hashes) reloads it.
    key = (str(d), sha256(raw))
    with _LOCK:
        if _LOADED["key"] == key:
            return _LOADED["manifest"] is not None
    try:
        manifest, history, anchor = load_dir(d, _model_version())
        err = ""
    except PackError as e:
        manifest = history = anchor = None
        err = str(e)
        if not quiet:
            _log(f"installed copy not usable: {err} — ignored")
    with _LOCK:
        _LOADED.update(key=key, manifest=manifest, history=history, anchor=anchor, error=err)
    return manifest is not None


def _age_days(manifest: dict) -> int | None:
    try:
        d = datetime.strptime(manifest["date"], "%Y-%m-%d").date()
    except (KeyError, ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc).date() - d).days


def _usable() -> dict | None:
    """The loaded pack when it may be used: enabled, valid, this model's, and
    no older than MAX_AGE_DAYS."""
    if not enabled() or not _load_local():
        return None
    with _LOCK:
        m = _LOADED["manifest"]
        if m is None:
            return None
        age = _age_days(m)
        if age is None or age > MAX_AGE_DAYS:
            return None
        return {"manifest": m, "history": _LOADED["history"], "anchor": _LOADED["anchor"]}


def anchor_scores(model_version: str, exclude: tuple[str, str] | None = None) -> list[float]:
    """The pack's last-90-day Market read scores (the `reference` anchor), or
    [] when there is no usable pack for this model."""
    p = _usable()
    if p is None or p["manifest"].get("model_version") != model_version:
        return []
    return [float(r[2]) for r in p["anchor"] if exclude is None or (r[0], r[1]) != tuple(exclude)]


def history_records() -> list[dict]:
    p = _usable()
    return list(p["history"]) if p else []


def info() -> dict | None:
    """{date, age_days, n_names, rows} of the usable pack, for the UI labels."""
    p = _usable()
    if p is None:
        return None
    m = p["manifest"]
    return {
        "date": m["date"],
        "age_days": _age_days(m),
        "n_names": (m.get("universe") or {}).get("n"),
        "rows": m.get("rows"),
        "model_version": m.get("model_version"),
    }


def status() -> dict:
    """Settings -> Models & Data: switch, last check, installed copy."""
    on = enabled()
    _load_local(quiet=True)
    with _LOCK:
        st = dict(_STATUS)
        m, err = _LOADED["manifest"], _LOADED["error"]
    if not on and st["state"] not in IN_FLIGHT:
        st["state"] = "disabled"
    age = _age_days(m) if m else None
    return {
        **st,
        "enabled": on,
        "env_disabled": env_disabled(),
        "installed": None
        if m is None
        else {
            "date": m["date"],
            "age_days": age,
            "n_names": (m.get("universe") or {}).get("n"),
            "rows": m.get("rows"),
            "model_version": m.get("model_version"),
            "stale": age is None or age > MAX_AGE_DAYS,
        },
        "installed_error": err,
        "in_use": on and m is not None and age is not None and age <= MAX_AGE_DAYS,
    }


def reset_for_tests() -> None:
    global _THREAD
    wait(10)
    _THREAD = None
    _invalidate()
    _set(state="idle", error="", checked_at=None, source="")
