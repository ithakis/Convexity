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
    symbol_db.sqlite     the downloaded symbol pack (symbol_db.py)
    state/<name>.json    views, watchlists, mpt, column_views, news, ...
    models/<version>/    the Market read artifact
    logs/desktop.log     desktop-app boot log

Stdlib only, no import-time side effects: nothing here creates a directory.
Writers ``mkdir`` their own parent right before writing.
"""

import os
import sys
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


def reference_dir() -> Path:
    """The downloaded reference pack (reference_pack.py) — a cache: deleting
    it only means the next launch downloads it again."""
    return data_dir() / "reference"


def config_file() -> Path:
    return data_dir() / "config.json"


def symbol_db_file() -> Path:
    return data_dir() / "symbol_db.sqlite"
