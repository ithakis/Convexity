"""On-disk migrations of user data, run once per launch from ``__init__``.

Two steps, in order:

1. **v1.13 rename** — ``<_OLD>`` -> ``convexity`` (see below): the checkout-root
   ``.<old>_*.json`` state files and ``~/.<old>/`` are renamed in place.
   Idempotent, atomic (``os.replace`` within one directory), never overwrites.
2. **v1.14 data folder** — the checkout-root ``.convexity_*.json`` files,
   ``symbol_db.sqlite`` and ``~/.convexity/ml_model/<ver>/`` move into the
   per-user data folder (``paths.py``). An installed package must never write
   next to its own code; for a wheel the "checkout root" is the package
   directory in site-packages, which is exactly where the pre-1.14 code wrote
   state, so that data is rescued too.

Step 2 is built so it cannot lose data:

- **copy -> verify -> remove.** Each file is copied to a temp name beside its
  destination, fsynced, verified (size + SHA-256) against the source, linked
  into place without clobbering (``os.link`` fails if the name exists),
  verified again, and only then is the source removed — after one more hash
  of the source, so a write that landed mid-migration is never discarded.
- **Never overwrite.** An existing destination that differs from the source
  is left alone and so is the source; the conflict is logged. An existing
  destination that is byte-identical means an earlier run got as far as the
  copy (e.g. crashed before removing the source): the source is removed.
- **Crash-safe and re-runnable.** Until its verified copy is in place the
  source is untouched. Leftover temps from a dead run are swept first. Model
  directories are assembled in a staging dir and renamed into place in one
  step, so ``ml_sentiment`` never sees a partial artifact.
- **Only in the app.** ``__init__`` runs this only when the process *is* the
  app (``convexity``, ``convexity-app``, ``-m convexity[.server|.desktop]`` —
  ``convexity._launched_as_app``); a bare import from a
  script or test never moves anything.
- **Gated.** With ``CONVEXITY_HOME`` set (tests, dev runs) nothing is migrated
  unless the legacy source is named explicitly too (``CONVEXITY_LEGACY_ROOT``
  for the checkout-root files, ``CONVEXITY_LEGACY_HOME`` for ``~/.convexity``).
  A temp-dir run can therefore never empty the real checkout or model folder.
- **Never fatal.** Every error is logged and swallowed; the app starts anyway
  and the legacy fallbacks (``paths.note_legacy``) keep it working.
- Key files (``.finnhub_key`` ...) are not migrated: they stay a logged
  fallback for one release and ``config.json`` takes over (Settings, later).

CLI: ``python -m convexity.migrate [--dry-run]`` prints what it would do / did.
(Running it that way skips the automatic run in ``__init__``, so a dry run
reports the real plan instead of the aftermath.)

Only ``convexity.paths`` (stdlib-only) is imported from the package: this
module runs during package ``__init__``. The legacy prefix is assembled from
two pieces on purpose, so a future mechanical rename pass over the source
cannot rewrite the very names this module exists to find.

Delete step 1 after 2027-06-30, step 2 after the 1.14 release cycle.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from convexity import paths

_OLD = "portfolio" + "_tracker"
_NEW = "convexity"

# Checkout-root runtime state files, by suffix (see .gitignore "Runtime state").
# In the data folder each becomes state/<suffix>.json.
_STATE_SUFFIXES = (
    "views", "watchlists", "mpt", "column_views", "session",
    "news", "sentiment_history",
)
_TMP_TAG = ".migrating-"


def _log(msg: str) -> None:
    print(f"[migrate] {msg}", file=sys.stderr)


# ------------------------------------------------------------ step 1: rename

def _move(old: Path, new: Path) -> bool:
    if new.exists() or not old.exists():
        return False
    try:
        os.replace(old, new)
    except OSError as exc:
        _log(f"could not move {old} -> {new}: {exc}")
        return False
    _log(f"{old.name} -> {new.name}")
    return True


def rename_legacy_names(root: Path, home: Path) -> int:
    """v1.13: move pre-rename state onto the Convexity names. Returns moves."""
    moved = 0
    for suffix in _STATE_SUFFIXES:
        moved += _move(root / f".{_OLD}_{suffix}.json", root / f".{_NEW}_{suffix}.json")
    moved += _move(home / f".{_OLD}", home / f".{_NEW}")
    moved += _move(home / f".{_OLD}_desktop.log", home / f".{_NEW}_desktop.log")
    return moved


# ------------------------------------------------------- step 2: data folder

@dataclass
class Report:
    dry_run: bool = False
    copied: list[str] = field(default_factory=list)      # new copy in place, source removed
    deduped: list[str] = field(default_factory=list)     # dest already identical, source removed
    kept: list[str] = field(default_factory=list)        # copied, source kept (changed / remove=False)
    conflicts: list[str] = field(default_factory=list)   # dest differs: both left alone
    # The same conflicts as (legacy source, destination in use) pairs, for the
    # UI (/api/health -> banner + Settings -> About). A conflict repeats on
    # every launch until the user deals with the old file, so it must be
    # visible somewhere a user looks, not only in a log line.
    conflict_pairs: list[tuple[str, str]] = field(default_factory=list)

    def conflict(self, src: Path, dst: Path, label: str) -> None:
        self.conflicts.append(label)
        self.conflict_pairs.append((str(src), str(dst)))
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)     # dry run only

    @property
    def moved(self) -> int:
        return len(self.copied) + len(self.deduped)

    def summary(self) -> str:
        if self.dry_run:
            return f"dry run: {len(self.planned)} to migrate, {len(self.conflicts)} conflicts, {len(self.skipped)} skipped"
        return (f"{len(self.copied)} migrated, {len(self.deduped)} already there, "
                f"{len(self.kept)} copied (source kept), {len(self.conflicts)} conflicts, "
                f"{len(self.skipped)} skipped, {len(self.errors)} errors")


def _fingerprint(p: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return p.stat().st_size, h.hexdigest()


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return  # not supported (Windows) — the file itself was fsynced
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists (or can't tell): leave its temp alone
    return True


def _sweep_temps(*dirs: Path) -> None:
    """Remove temps left by a migration that died mid-copy. A temp whose
    process is still alive belongs to a concurrent run and is left alone."""
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if _TMP_TAG not in p.name:
                continue
            try:
                pid = int(p.name.rsplit(_TMP_TAG, 1)[1])
            except ValueError:
                continue
            if _pid_alive(pid):
                continue
            try:
                shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
                _log(f"removed leftover temp {p}")
            except OSError as exc:
                _log(f"could not remove leftover temp {p}: {exc}")


def _copy_to_temp(src: Path, tmp: Path) -> None:
    shutil.copy2(src, tmp)  # content + mode (state files are 0600) + mtime
    with open(tmp, "rb+") as fh:
        os.fsync(fh.fileno())


def _place_no_clobber(tmp: Path, dst: Path) -> None:
    """Put ``tmp`` at ``dst`` without ever replacing an existing ``dst``."""
    try:
        os.link(tmp, dst)  # atomic, raises FileExistsError if dst appeared
    except FileExistsError:
        raise
    except OSError:
        # Filesystem without hard links: re-check, then rename.
        if dst.exists():
            raise FileExistsError(str(dst))
        os.replace(tmp, dst)
        return
    tmp.unlink()


def _remove_source(src: Path, fp: tuple[int, str], label: str, rep: Report, remove: bool) -> None:
    """Unlink ``src`` only if it still has the fingerprint that was copied."""
    if not remove:
        rep.kept.append(label)
        return
    try:
        if _fingerprint(src) != fp:
            _log(f"{label}: source changed during migration — kept it ({src})")
            rep.kept.append(label)
            return
        src.unlink()
        _fsync_dir(src.parent)
    except FileNotFoundError:
        pass  # a concurrent run (second app instance) removed it first
    except OSError as exc:
        _log(f"{label}: copied, but could not remove the old file {src}: {exc}")
        rep.kept.append(label)
        return
    rep.copied.append(label)


def _migrate_file(src: Path, dst: Path, rep: Report, *, remove: bool, _retry: bool = True) -> None:
    label = f"{src} -> {dst}"
    if not src.is_file():
        return
    try:
        src_fp = _fingerprint(src)
        if dst.exists():
            if dst.is_file() and _fingerprint(dst) == src_fp:
                if rep.dry_run:
                    rep.planned.append(f"{label} (already there; remove old copy)")
                    return
                before = len(rep.copied)
                _remove_source(src, src_fp, label, rep, remove)
                if len(rep.copied) > before:  # re-file as a dedup, not a copy
                    rep.copied.pop()
                    rep.deduped.append(label)
                    _log(f"{src.name}: identical copy already at {dst}; removed the old file")
                return
            rep.conflict(src, dst, label)
            _log(f"CONFLICT {label}: destination exists and differs — left both untouched")
            return
        if rep.dry_run:
            rep.planned.append(label)
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.parent / f".{dst.name}{_TMP_TAG}{os.getpid()}"
        try:
            _copy_to_temp(src, tmp)
            if _fingerprint(tmp) != src_fp:
                raise OSError("copy does not match the source (size/SHA-256)")
            _place_no_clobber(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()
        _fsync_dir(dst.parent)
        if _fingerprint(dst) != src_fp:
            raise OSError("placed copy does not match the source (size/SHA-256)")
        _log(f"copied {label} (sha256 {src_fp[1][:12]}…, {src_fp[0]} bytes)")
        _remove_source(src, src_fp, label, rep, remove)
    except FileExistsError:
        # Another process (a second app instance launched at the same moment)
        # placed it first. Judge it like any existing destination: identical
        # => just drop our source; different => a real conflict.
        if _retry:
            _migrate_file(src, dst, rep, remove=remove, _retry=False)
        else:
            rep.conflict(src, dst, label)
            _log(f"CONFLICT {label}: destination appeared during migration — source kept")
    except FileNotFoundError:
        if src.exists():
            rep.errors.append(f"{label}: file vanished mid-copy")
            _log(f"ERROR {label}: file vanished mid-copy — source kept")
        # else: a concurrent run already moved it — nothing to do
    except OSError as exc:
        rep.errors.append(f"{label}: {exc}")
        _log(f"ERROR {label}: {exc} — source kept")


def _tree_fingerprints(root: Path, *, skip_vanished: bool = False) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            out[str(p.relative_to(root))] = _fingerprint(p)
        except FileNotFoundError:
            if not skip_vanished:
                raise
    return out


def _prune_empty(d: Path, stop: Path) -> None:
    """rmdir ``d`` and its parents while empty, never above ``stop``."""
    while True:
        try:
            d.rmdir()
        except OSError:
            return
        if d == stop or d.parent == d:
            return
        d = d.parent


def _migrate_model_dir(src: Path, dst: Path, rep: Report, *, remove: bool, prune_to: Path,
                       _retry: bool = True) -> None:
    label = f"{src}/ -> {dst}/"
    try:
        src_fps = _tree_fingerprints(src)
        if not src_fps:
            return
        if dst.exists():
            if _tree_fingerprints(dst) == src_fps:
                if rep.dry_run:
                    rep.planned.append(f"{label} (already there; remove old copy)")
                    return
                if remove:
                    _remove_tree(src, src_fps, label, rep, prune_to, dedup=True)
                else:
                    rep.kept.append(label)
                return
            rep.conflict(src, dst, label)
            _log(f"CONFLICT {label}: destination exists and differs — left both untouched")
            return
        if rep.dry_run:
            rep.planned.append(f"{label} ({len(src_fps)} files)")
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        stage = dst.parent / f".{dst.name}{_TMP_TAG}{os.getpid()}"
        try:
            for rel in src_fps:
                (stage / rel).parent.mkdir(parents=True, exist_ok=True)
                _copy_to_temp(src / rel, stage / rel)
            if _tree_fingerprints(stage) != src_fps:
                raise OSError("staged copy does not match the source (size/SHA-256)")
            if dst.exists():
                raise FileExistsError(str(dst))
            try:
                os.rename(stage, dst)
            except OSError as exc:  # ENOTEMPTY/EEXIST: lost a race for dst
                if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                    raise FileExistsError(str(dst)) from exc
                raise
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
        _fsync_dir(dst.parent)
        if _tree_fingerprints(dst) != src_fps:
            raise OSError("placed copy does not match the source (size/SHA-256)")
        _log(f"copied {label} ({len(src_fps)} files, all SHA-256 verified)")
        if remove:
            _remove_tree(src, src_fps, label, rep, prune_to, dedup=False)
        else:
            rep.kept.append(label)
    except FileExistsError:
        if _retry:  # see _migrate_file: a concurrent run placed it first
            _migrate_model_dir(src, dst, rep, remove=remove, prune_to=prune_to, _retry=False)
        else:
            rep.conflict(src, dst, label)
            _log(f"CONFLICT {label}: destination appeared during migration — source kept")
    except FileNotFoundError:
        if src.exists():
            rep.errors.append(f"{label}: file vanished mid-copy")
            _log(f"ERROR {label}: file vanished mid-copy — source kept")
    except OSError as exc:
        rep.errors.append(f"{label}: {exc}")
        _log(f"ERROR {label}: {exc} — source kept")


def _remove_tree(src: Path, fps: dict, label: str, rep: Report, prune_to: Path, *, dedup: bool) -> None:
    now = _tree_fingerprints(src, skip_vanished=True) if src.exists() else {}
    # Files missing from ``now`` were removed by a concurrent run; only a file
    # that is new or different means the source changed under us.
    if any(fps.get(rel) != fp for rel, fp in now.items()):
        _log(f"{label}: source changed during migration — kept it")
        rep.kept.append(label)
        return
    for rel in fps:
        (src / rel).unlink(missing_ok=True)
    for d in sorted({(src / rel).parent for rel in fps}, key=lambda p: len(p.parts), reverse=True):
        _prune_empty(d, prune_to)
    (rep.deduped if dedup else rep.copied).append(label)
    if dedup:
        _log(f"{label}: identical copy already in place; removed the old folder")


def migrate_to_data_dir(
    legacy_root: Path | None,
    legacy_home: Path | None,
    data: Path,
    *,
    remove: bool = True,
    dry_run: bool = False,
) -> Report:
    """v1.14: move checkout-root state + ~/.convexity/ml_model into ``data``.
    ``None`` for a legacy location skips it. Never raises."""
    rep = Report(dry_run=dry_run)
    state = data / "state"
    models = data / "models"
    try:
        if not dry_run:
            _sweep_temps(data, state, models)
        if legacy_root is not None:
            for suffix in _STATE_SUFFIXES:
                _migrate_file(legacy_root / f".{_NEW}_{suffix}.json",
                              state / f"{suffix}.json", rep, remove=remove)
            db = legacy_root / "symbol_db.sqlite"
            wal = legacy_root / "symbol_db.sqlite-wal"
            if db.is_file():
                if wal.is_file() and wal.stat().st_size > 0:
                    rep.skipped.append(f"{db}: uncommitted WAL (a writer is open) — try again later")
                    _log(rep.skipped[-1])
                else:
                    n_before = rep.moved
                    _migrate_file(db, data / "symbol_db.sqlite", rep, remove=remove)
                    if rep.moved > n_before and not dry_run:
                        for side in ("-wal", "-shm"):  # empty/derived sidecars
                            s = legacy_root / f"symbol_db.sqlite{side}"
                            try:
                                if s.is_file() and (side == "-shm" or s.stat().st_size == 0):
                                    s.unlink()
                            except OSError:
                                pass
        if legacy_home is not None:
            old_models = legacy_home / f".{_NEW}" / "ml_model"
            if old_models.is_dir():
                for ver in sorted(p for p in old_models.iterdir() if p.is_dir()):
                    _migrate_model_dir(ver, models / ver.name, rep, remove=remove,
                                       prune_to=legacy_home / f".{_NEW}")
                if not dry_run and remove:
                    _prune_empty(old_models, legacy_home / f".{_NEW}")
                    try:
                        (legacy_home / f".{_NEW}").rmdir()  # only if now empty
                    except OSError:
                        pass
    except Exception as exc:  # pragma: no cover - defensive, never fatal
        rep.errors.append(str(exc))
        _log(f"ERROR: {exc}")
    if rep.planned or rep.moved or rep.kept or rep.conflicts or rep.errors or rep.skipped:
        _log(rep.summary())
    return rep


def _legacy_sources() -> tuple[Path | None, Path | None]:
    """Which legacy locations the automatic run may migrate from. With
    ``CONVEXITY_HOME`` overridden, only explicitly named ones."""
    if not paths.is_overridden():
        return paths.legacy_root(), paths.legacy_home()
    root = paths.legacy_root() if os.environ.get("CONVEXITY_LEGACY_ROOT", "").strip() else None
    home = paths.legacy_home() if os.environ.get("CONVEXITY_LEGACY_HOME", "").strip() else None
    return root, home


# The report of this process's automatic run (``run()``), or None if it did
# not run. Read by server.py for /api/health's ``migration_conflicts``.
LAST_REPORT: "Report | None" = None


def conflicts() -> list[dict]:
    """Conflicts from this launch's migration: [{old, used}], empty if none."""
    rep = LAST_REPORT
    return [{"old": o, "used": u} for o, u in rep.conflict_pairs] if rep else []


def run(root: Path | None = None, home: Path | None = None) -> int:
    """Both steps. Explicit ``root``/``home`` (tests) are used as given;
    otherwise the gated legacy locations. Returns files/dirs moved."""
    auto_root, auto_home = _legacy_sources()
    root = root if root is not None else auto_root
    home = home if home is not None else auto_home
    moved = 0
    if root is not None and home is not None:
        moved += rename_legacy_names(root, home)
    elif root is not None:
        for suffix in _STATE_SUFFIXES:
            moved += _move(root / f".{_OLD}_{suffix}.json", root / f".{_NEW}_{suffix}.json")
    # Step 2 only for the gated sources: explicit test roots stay in step 1.
    global LAST_REPORT
    LAST_REPORT = migrate_to_data_dir(auto_root, auto_home, paths.data_dir())
    moved += LAST_REPORT.moved
    return moved


def _main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m convexity.migrate",
        description="Move pre-1.14 Convexity data into the per-user data folder.")
    ap.add_argument("--dry-run", action="store_true", help="show what would move; change nothing")
    args = ap.parse_args(argv)
    root, home = _legacy_sources()
    print(f"data folder : {paths.data_dir()}")
    print(f"legacy root : {root or '(skipped: CONVEXITY_HOME set, CONVEXITY_LEGACY_ROOT not)'}")
    print(f"legacy home : {home or '(skipped: CONVEXITY_HOME set, CONVEXITY_LEGACY_HOME not)'}")
    if not args.dry_run and root is not None and home is not None:
        rename_legacy_names(root, home)
    rep = migrate_to_data_dir(root, home, paths.data_dir(), dry_run=args.dry_run)
    for title, items in (("would migrate", rep.planned), ("migrated", rep.copied),
                         ("already there", rep.deduped), ("copied, source kept", rep.kept),
                         ("CONFLICT", rep.conflicts), ("skipped", rep.skipped),
                         ("ERROR", rep.errors)):
        for it in items:
            print(f"  {title:>20}: {it}")
    if not (rep.planned or rep.moved or rep.kept or rep.conflicts or rep.errors or rep.skipped):
        print(rep.summary())  # otherwise migrate_to_data_dir already logged it
    return 1 if rep.errors else 0


if __name__ == "__main__":
    raise SystemExit(_main())
