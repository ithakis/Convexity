"""One-time migration of on-disk state from the pre-Convexity names.

v1.13.0 renamed the package (``<_OLD>`` -> ``convexity``, see below) and with
it every runtime file: the repo-root ``.<old>_*.json`` state files and the
``~/.<old>/`` home directory (ML model artifact). The readers treat a missing
file as "empty", so without this step an upgraded install would silently open
with no portfolios, no saved runs and no Market read — the data is still on
disk, just under a name nothing looks for any more.

``run()`` is called from ``convexity/__init__.py`` so it executes before any
submodule binds (or, like news_sentiment, eagerly loads) a data path. It is:

- idempotent — a file is moved only when the new name is absent and the old
  one exists, so a second run is a handful of ``stat`` calls;
- non-destructive — ``os.replace`` within one directory is an atomic rename,
  and an existing new-name file is never overwritten;
- never fatal — any OSError is logged and swallowed; a failed migration must
  not stop the app from starting (the old files stay where they were).

The legacy prefix is assembled from two pieces on purpose, so a future
mechanical rename pass over the source cannot rewrite the very names this
module exists to find.

Delete after 2027-06-30 (by then every install has launched at least once).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_OLD = "portfolio" + "_tracker"
_NEW = "convexity"

# Repo-root runtime state files, by suffix (see .gitignore "Runtime state").
_STATE_SUFFIXES = (
    "views", "watchlists", "mpt", "column_views", "session",
    "news", "sentiment_history",
)


def _repo_root() -> Path:
    # Same rule as helpers._repo_root, duplicated so this module imports
    # nothing from the package (it runs during package __init__).
    current = Path(__file__).resolve()
    for parent in (current.parent, *current.parents):
        if (parent / ".git").exists():
            return parent
    return current.parent


def _move(old: Path, new: Path) -> bool:
    if new.exists() or not old.exists():
        return False
    try:
        os.replace(old, new)
    except OSError as exc:
        print(f"[migrate] could not move {old} -> {new}: {exc}", file=sys.stderr)
        return False
    print(f"[migrate] {old.name} -> {new.name}")
    return True


def run(root: Path | None = None, home: Path | None = None) -> int:
    """Move legacy state onto the Convexity names. Returns files moved."""
    root = root or _repo_root()
    home = home or Path.home()
    moved = 0
    for suffix in _STATE_SUFFIXES:
        moved += _move(root / f".{_OLD}_{suffix}.json", root / f".{_NEW}_{suffix}.json")
    moved += _move(home / f".{_OLD}", home / f".{_NEW}")
    moved += _move(home / f".{_OLD}_desktop.log", home / f".{_NEW}_desktop.log")
    return moved
