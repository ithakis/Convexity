"""Where Convexity keeps user data — the single source of truth.

Every data path used to be derived from the code's own location (walk up to
``.git``, else the package directory). In a dev checkout that is the repo
root; in an installed wheel it is ``site-packages/convexity/`` — a
``uv pip install`` then wrote the user's watchlists *into the package*, where
the next upgrade deletes them. Data now lives in a per-user folder that has
nothing to do with where the code is:

    macOS    ~/Library/Application Support/Convexity/
    Windows  %APPDATA%\\Convexity\\
    Linux    $XDG_DATA_HOME/convexity/   (default ~/.local/share/convexity/)

``CONVEXITY_HOME`` overrides it — tests and every dev/manual run point it at a
temp dir so they can never touch the real data.

Layout::

    config.json          API keys (read here; written by Settings in a later phase)
    symbol_db.sqlite     fuzzy ticker DB (build_symbol_db.py)
    state/<name>.json    views, watchlists, mpt, column_views, news, ...
    models/<version>/    the Market read artifact
    logs/desktop.log     desktop-app boot log

Stdlib only, no import-time side effects: nothing here creates a directory.
Writers ``mkdir`` their own parent right before writing.

The ``legacy_*`` helpers are the one-release fallbacks for data that has not
been migrated yet (``migrate.py`` moves it). Each use is logged once, so a
fallback that never goes away is visible in Settings -> Logs rather than
silent. Delete them with the fallbacks after the 1.14 release cycle.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

APP_NAME = "Convexity"
HOME_ENV = "CONVEXITY_HOME"


def data_dir() -> Path:
    env = os.environ.get(HOME_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else home / ".local" / "share"
    return base / APP_NAME.lower()


def is_overridden() -> bool:
    return bool(os.environ.get(HOME_ENV, "").strip())


def state_dir() -> Path:
    return data_dir() / "state"


def state_file(name: str) -> Path:
    """``state_file("views")`` -> ``<data>/state/views.json``."""
    return state_dir() / f"{name}.json"


def models_dir() -> Path:
    return data_dir() / "models"


def logs_dir() -> Path:
    return data_dir() / "logs"


def config_file() -> Path:
    return data_dir() / "config.json"


def symbol_db_file() -> Path:
    return data_dir() / "symbol_db.sqlite"


# ------------------------------------------------------------ legacy locations

def legacy_root() -> Path:
    """Where pre-1.14 code wrote state: the checkout root (walk up to .git),
    else the package directory — which, for an installed wheel, is exactly the
    site-packages folder the bug above wrote into. ``CONVEXITY_LEGACY_ROOT``
    overrides it (migration tests and dry runs)."""
    env = os.environ.get("CONVEXITY_LEGACY_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    current = Path(__file__).resolve()
    for parent in (current.parent, *current.parents):
        if (parent / ".git").exists():
            return parent
    return current.parent


def legacy_home() -> Path:
    """Parent of the pre-1.14 ``~/.convexity/`` folder (the ML model).
    ``CONVEXITY_LEGACY_HOME`` overrides it."""
    env = os.environ.get("CONVEXITY_LEGACY_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home()


def legacy_model_root() -> Path:
    return legacy_home() / ".convexity" / "ml_model"


_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()


def note_legacy(kind: str, path: Path, hint: str = "") -> None:
    """Log (once per ``kind``) that a legacy fallback location is in use."""
    with _WARN_LOCK:
        if kind in _WARNED:
            return
        _WARNED.add(kind)
    msg = f"[paths] using legacy {kind} at {path} (fallback, kept for one release)"
    print(msg + (f" — {hint}" if hint else ""), file=sys.stderr)
