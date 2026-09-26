"""Convexity — a portfolio optimization app built for long-term horizon investing.

Version history — see CHANGELOG.md for the full PR-by-PR mapping.
X bumps on a major new feature/release, Y bumps on smaller polish/fixes.
"""

__version__ = "1.13.1"
__version_date__ = "2026-09-26"  # release date of __version__, ISO yyyy-mm-dd


def _format_version_date(date_str: str) -> str:
    # Runs at import time, so a malformed __version_date__ must never crash
    # `import convexity` (that would take down every entry point over
    # a one-character typo). Fall back to the raw string on any parse failure.
    from datetime import datetime
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b %Y")
    except (ValueError, TypeError):
        return date_str


# "1.5.0 (09 Jul 2026)" — used anywhere the version is displayed (splash,
# window title, terminal banner). The frontend renders its own copy from the
# raw `version`/`version_date` fields returned by /api/health so it can share
# the same date-formatting helper as the rest of the UI.
__version_display__ = f"{__version__} ({_format_version_date(__version_date__)})"


# Move legacy state (pre-rename names, then pre-1.14 checkout-root files and
# ~/.convexity/ml_model) into the data folder before any submodule binds a
# data path (news_sentiment loads its cache at import). Never fatal.
#
# ONLY when this process is the app itself. A bare `import convexity` — a dev
# script (scripts/check_dependency_manifests.py), `python -c`, pytest, the
# `python -m convexity.migrate` CLI — must never move the user's data: during
# development that once emptied the checkout under two running copies of the
# old app, which still read their state from there.
_APP_MODULES = ("convexity", "convexity.server", "convexity.desktop")
_APP_SCRIPTS = ("convexity", "convexity-app", "dashboard.py")


def _launched_as_app() -> bool:
    import sys
    from pathlib import Path
    argv = list(getattr(sys, "orig_argv", ()))
    if "-m" in argv:
        i = argv.index("-m")
        return i + 1 < len(argv) and argv[i + 1] in _APP_MODULES
    name = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    for suffix in (".exe", "-script.pyw", "-script.py"):  # Windows launchers
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name in _APP_SCRIPTS


def _auto_migrate() -> None:
    if not _launched_as_app():
        return
    try:
        from . import migrate
        migrate.run()
    except Exception as exc:  # pragma: no cover - defensive
        import sys
        print(f"[migrate] skipped: {exc}", file=sys.stderr)


_auto_migrate()
