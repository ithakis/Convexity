"""First-run download of the Market read model (roadmap Phase 5).

A fresh install has no ``<data>/models/mlsent-v1.1/`` — the artifact is ~4 MB
of trained weights that are not in the package — so without this the Market
read is dead on every new machine. ``start()`` fetches it in the background
from a GitHub Release asset, then reloads ``ml_sentiment``.

Trust model (CLAUDE.md §18 "anything downloaded at runtime is verified against
a SHA-256 pinned in code"):

- ``MODEL_URL`` and ``MODEL_SHA256`` are pinned here. ``CONVEXITY_MODEL_URL``
  may override the **URL only** (dev runs against a local server); the hash is
  never overridable, so whatever is served must be byte-identical to the
  published tarball. Plain ``http://`` is accepted for loopback hosts only.
- The hash is checked **before** the archive is opened. A mismatch deletes the
  download and is not retried — a different file will not become the right one.
- Extraction never calls ``extractall``: each member must be a regular file
  named ``<version>/<one of ARTIFACT_FILES>``; absolute paths, ``..``,
  symlinks, hardlinks and devices are rejected, and all six files must be
  present. Files are written into a staging dir inside ``models/`` and moved
  into place with one ``os.rename`` — a crash never leaves a half model that
  ``ml_sentiment`` would try to load.

Never blocks startup, never fails silently: every state change prints a
``[model_fetch]`` line (tee'd into Settings -> Logs by logbuf) and ``status()``
feeds ``/api/runtime-status`` -> Settings -> Models & Data, which offers Retry.

Releasing a new model: CLAUDE.md §4 "Model release procedure".
Stdlib only.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import ipaddress
import os
import shutil
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from convexity import paths

MODEL_VERSION = "mlsent-v1.1"  # must equal ml_sentiment.ARTIFACT_VERSION (tested)
MODEL_URL = (
    "https://github.com/ithakis/Convexity/releases/download/model-mlsent-v1.1/mlsent-v1.1.tar.gz"
)
MODEL_SHA256 = "9e05d4af6b2546651af6b65bb2352a92834bdf4c7e7dce64d8e406dfffecf46d"
ARTIFACT_FILES = (
    "col_mask.npy",
    "feature_schema.json",
    "idf.npy",
    "meta.json",
    "model.lgbm.txt",
    "tier_cuts.json",
)
# The tarball is ~3 MB; the six files ~4 MB. Generous, but capped: a hostile
# or broken server cannot fill the disk.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_EXTRACT_BYTES = 128 * 1024 * 1024

_ATTEMPTS = 3
_BACKOFF_S = 2.0  # 2 s, 4 s between attempts (tests shrink it)
_TIMEOUT_S = 30.0
_CHUNK = 64 * 1024

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STATUS: dict = {
    "state": "idle",
    "bytes": 0,
    "total": None,
    "error": "",
    "started_at": None,
    "finished_at": None,
    "source": "",
}
IN_FLIGHT = ("downloading", "verifying", "installing")


class FetchError(Exception):
    """A failure with a user-facing reason. ``retry`` = worth another attempt."""

    def __init__(self, msg: str, retry: bool = False):
        super().__init__(msg)
        self.retry = retry


def _log(msg: str) -> None:
    print(f"[model_fetch] {msg}", flush=True)


def _set(**kw) -> None:
    with _LOCK:
        _STATUS.update(kw)


def status() -> dict:
    with _LOCK:
        return dict(_STATUS)


def target_dir() -> Path:
    return paths.models_dir() / MODEL_VERSION


def disabled() -> bool:
    return os.environ.get("CONVEXITY_MODEL_DOWNLOAD", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )


def source_url() -> str:
    return os.environ.get("CONVEXITY_MODEL_URL", "").strip() or MODEL_URL


def check_url(url: str) -> None:
    """https anywhere; http only to a loopback host (the local test server)."""
    u = urllib.parse.urlparse(url)
    if u.scheme == "https" and u.hostname:
        return
    if u.scheme == "http" and u.hostname:
        host = u.hostname
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
    raise FetchError(f"refusing to download from {url!r}: https required (http only for localhost)")


def complete(d: Path) -> bool:
    """All six artifact files present. Existence of the folder is not enough:
    a folder emptied or half-copied by hand used to count as "installed", so
    nothing downloaded and Settings offered no Retry (found in verification)."""
    return all((d / f).is_file() for f in ARTIFACT_FILES)


def needed() -> bool:
    """True when the Market read has no complete artifact to load. An explicit
    MLSENT_MODEL_DIR is the user's choice and is never downloaded into; a
    complete copy in the one-release legacy location still counts as present
    (ml_sentiment only falls back to it while the data-folder one is absent)."""
    if os.environ.get("MLSENT_MODEL_DIR"):
        return False
    t = target_dir()
    if complete(t):
        return False
    if not t.exists() and complete(paths.legacy_model_root() / MODEL_VERSION):
        return False
    return True


# ------------------------------------------------------------------ lifecycle
def start(force: bool = False) -> dict:
    """Begin the download in a daemon thread if the model is missing.

    Single-flight: while one is running a second call returns its status.
    ``force`` (the Retry button) ignores CONVEXITY_MODEL_DOWNLOAD=0 and a
    previous failure, but still does nothing when the model is present."""
    global _THREAD
    with _LOCK:
        if _STATUS["state"] in IN_FLIGHT:
            return dict(_STATUS)
        if not needed():
            _STATUS.update(state="not_needed", error="")
            return dict(_STATUS)
        if disabled() and not force:
            if _STATUS["state"] != "disabled":
                _log(
                    "model missing; automatic download disabled "
                    "(CONVEXITY_MODEL_DOWNLOAD=0) — use Settings -> Models & Data -> Retry"
                )
            _STATUS.update(state="disabled", error="")
            return dict(_STATUS)
        _STATUS.update(
            state="downloading",
            bytes=0,
            total=None,
            error="",
            started_at=time.time(),
            finished_at=None,
            source="",
        )
        _THREAD = threading.Thread(target=_run, name="pt-model-fetch", daemon=True)
        _THREAD.start()
        return dict(_STATUS)


def wait(timeout: float | None = None) -> dict:
    """Test/CLI hook: block until the current download thread ends."""
    t = _THREAD
    if t is not None:
        t.join(timeout)
    return status()


def _run() -> None:
    url = source_url()
    try:
        check_url(url)
        _set(source=urllib.parse.urlparse(url).hostname or "")
        _log(f"{MODEL_VERSION} missing or incomplete — downloading from {url}")
        models = paths.models_dir()
        models.mkdir(parents=True, exist_ok=True)
        _sweep(models)
        part = models / f".{MODEL_VERSION}.{os.getpid()}.part"
        try:
            _download_verified(url, part)
            _set(state="installing")
            _install(part, models)
        finally:
            part.unlink(missing_ok=True)
        _finish_install()
    except FetchError as e:
        _fail(str(e))
    except Exception as e:  # never let the thread die without a status
        _fail(f"{type(e).__name__}: {e}")


def _fail(reason: str) -> None:
    _set(state="failed", error=reason, finished_at=time.time())
    _log(
        f"download FAILED: {reason} — the Market read stays unavailable; "
        "retry from Settings -> Models & Data"
    )


def _finish_install() -> None:
    from convexity import ml_sentiment

    st = ml_sentiment.reload()
    _set(state="installed", error="", finished_at=time.time())
    if st.get("ok"):
        _log(f"installed {MODEL_VERSION} -> {target_dir()}; Market read available")
    else:
        # The files are in place and verified; loading them failed (e.g.
        # lightgbm missing) and ml_sentiment's reason — shown beside this
        # status in Settings — says why. Re-downloading would not help.
        _log(
            f"installed {MODEL_VERSION} -> {target_dir()}, but the model did not load: "
            f"{st.get('reason')}"
        )


# ------------------------------------------------------------------ download
def _download_verified(url: str, dest: Path) -> None:
    last: FetchError | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            digest = _download(url, dest)
            break
        except FetchError as e:
            last = e
            dest.unlink(missing_ok=True)
            if not e.retry or attempt == _ATTEMPTS:
                raise
            wait_s = _BACKOFF_S * attempt
            _log(f"attempt {attempt}/{_ATTEMPTS} failed ({e}); retrying in {wait_s:.0f}s")
            _set(bytes=0, total=None)
            time.sleep(wait_s)
    else:  # pragma: no cover - loop always breaks or raises
        raise last or FetchError("download failed")
    _set(state="verifying")
    if digest != MODEL_SHA256:
        dest.unlink(missing_ok=True)
        raise FetchError(
            f"checksum mismatch (got sha256 {digest[:12]}…, "
            f"expected {MODEL_SHA256[:12]}…) — file discarded"
        )


def _download(url: str, dest: Path) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": f"convexity-model-fetch/{MODEL_VERSION}"}
    )
    h = hashlib.sha256()
    n = 0
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            # urllib follows GitHub's redirect to its CDN; re-check where we
            # ended up so a redirect cannot downgrade to plain http.
            check_url(resp.geturl())
            total = resp.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None
            if total is not None and total > MAX_DOWNLOAD_BYTES:
                raise FetchError(f"download too large ({total} bytes)")
            _set(total=total, bytes=0)
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > MAX_DOWNLOAD_BYTES:
                        raise FetchError(f"download exceeded {MAX_DOWNLOAD_BYTES} bytes")
                    h.update(chunk)
                    f.write(chunk)
                    _set(bytes=n)
                f.flush()
                os.fsync(f.fileno())
            if total is not None and n != total:
                raise FetchError(f"download truncated ({n} of {total} bytes)", retry=True)
    except urllib.error.HTTPError as e:
        raise FetchError(
            f"HTTP {e.code} from {urllib.parse.urlparse(url).hostname}",
            retry=e.code >= 500 or e.code == 429,
        ) from None
    except urllib.error.URLError as e:
        raise FetchError(f"network unavailable ({e.reason})", retry=True) from None
    except OSError as e:  # timeouts, resets, a full disk
        raise FetchError(f"network error ({type(e).__name__}: {e})", retry=True) from None
    return h.hexdigest()


# ------------------------------------------------------------------ extract
def _safe_member_name(m: tarfile.TarInfo) -> str | None:
    """The artifact file name for an acceptable member, None for the top
    directory entry; raises for anything else."""
    name = m.name
    if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
        raise FetchError(f"archive rejected: absolute path {name!r}")
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise FetchError(f"archive rejected: path traversal {name!r}")
    if m.issym() or m.islnk():
        raise FetchError(f"archive rejected: link {name!r}")
    if m.isdir():
        if parts == [MODEL_VERSION]:
            return None
        raise FetchError(f"archive rejected: unexpected directory {name!r}")
    if not m.isfile():
        raise FetchError(f"archive rejected: special file {name!r}")
    if len(parts) != 2 or parts[0] != MODEL_VERSION or parts[1] not in ARTIFACT_FILES:
        raise FetchError(f"archive rejected: unexpected file {name!r}")
    return parts[1]


def _install(archive: Path, models: Path) -> None:
    staging = models / f".{MODEL_VERSION}.{os.getpid()}.staging"
    shutil.rmtree(staging, ignore_errors=True)
    out = staging / MODEL_VERSION
    out.mkdir(parents=True)
    try:
        seen: set[str] = set()
        total = 0
        try:
            tf = tarfile.open(archive, mode="r:gz")
        except (tarfile.TarError, OSError) as e:
            raise FetchError(f"archive unreadable ({e})") from None
        with tf:
            for m in tf:
                fname = _safe_member_name(m)
                if fname is None:
                    continue
                if fname in seen:
                    raise FetchError(f"archive rejected: duplicate {m.name!r}")
                total += m.size
                if total > MAX_EXTRACT_BYTES:
                    raise FetchError("archive rejected: expands past the size cap")
                src = tf.extractfile(m)
                if src is None:  # pragma: no cover - isfile() guaranteed above
                    raise FetchError(f"archive rejected: unreadable {m.name!r}")
                with src, open(out / fname, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                seen.add(fname)
        missing = sorted(set(ARTIFACT_FILES) - seen)
        if missing:
            raise FetchError(f"archive incomplete: missing {', '.join(missing)}")
        final = target_dir()
        if final.exists() and not complete(final):
            _set_aside(final)
        try:
            os.rename(out, final)
        except OSError:
            if complete(final):
                # Another instance installed it first; its copy passed the same
                # checks, so ours is redundant.
                _log(f"{final} appeared during install (another instance); keeping it")
            else:
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _set_aside(d: Path) -> None:
    """Move an incomplete model folder out of the way instead of deleting it —
    whatever is in there may be something the user put there by hand."""
    dest = d.with_name(f"{d.name}.incomplete-{time.strftime('%Y%m%d-%H%M%S')}")
    n = 1
    while dest.exists():
        n += 1
        dest = d.with_name(f"{d.name}.incomplete-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
    present = sorted(p.name for p in d.iterdir()) if d.is_dir() else []
    os.rename(d, dest)
    _log(f"{d.name} was incomplete (had {present or 'nothing'}); moved aside to {dest.name}")


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but not ours (PermissionError) — leave it alone
    return True


def _sweep(models: Path) -> None:
    """Remove .part / .staging leftovers of runs that died mid-download."""
    prefix = f".{MODEL_VERSION}."
    for p in models.iterdir():
        if not p.name.startswith(prefix):
            continue
        rest = p.name[len(prefix) :]
        pid_s, _, kind = rest.partition(".")
        if kind not in ("part", "staging") or not pid_s.isdigit():
            continue
        if _pid_alive(int(pid_s)):
            continue
        _log(f"removing leftover {p.name} from a dead run")
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


# ------------------------------------------------------------------ packaging
def pack(src_dir: Path, out: Path) -> str:
    """Write the release tarball for ``src_dir`` and return its SHA-256.

    Deterministic — same six files in, same bytes out: sorted names, mtime 0,
    uid/gid 0, mode 0644, gzip header mtime 0, no xattrs. (macOS ``tar`` adds
    ``._*`` AppleDouble entries, which _install rightly rejects.) Used by
    ``ml/scripts/10_export_artifact.py --tarball`` and the tests."""
    src_dir = Path(src_dir)
    missing = [f for f in ARTIFACT_FILES if not (src_dir / f).is_file()]
    if missing:
        raise FileNotFoundError(f"{src_dir}: missing {missing}")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        d = tarfile.TarInfo(MODEL_VERSION)
        d.type, d.mode, d.mtime = tarfile.DIRTYPE, 0o755, 0
        tf.addfile(d)
        for f in sorted(ARTIFACT_FILES):
            data = (src_dir / f).read_bytes()
            ti = tarfile.TarInfo(f"{MODEL_VERSION}/{f}")
            ti.size, ti.mode, ti.mtime = len(data), 0o644, 0
            tf.addfile(ti, io.BytesIO(data))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fh:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0, compresslevel=9) as gz:
            gz.write(raw.getvalue())
    return hashlib.sha256(out.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    """``python -m convexity.model_fetch`` — download now (ignores the
    disable flag), print the outcome. ``--status`` only reports."""
    argv = sys.argv[1:] if argv is None else argv
    if "--status" in argv:
        print({"needed": needed(), "target": str(target_dir()), "url": source_url()})
        return 0
    start(force=True)
    st = wait()
    print(st)
    return 0 if st["state"] in ("installed", "not_needed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
