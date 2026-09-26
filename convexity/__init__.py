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


# Move pre-rename state files onto the current names before any submodule
# binds a data path (news_sentiment loads its cache at import). Never fatal.
try:
    from . import migrate as _migrate
    _migrate.run()
except Exception as _exc:  # pragma: no cover - defensive
    print(f"[migrate] skipped: {_exc}")
