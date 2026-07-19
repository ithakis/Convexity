"""Portfolio Tracker — local-only portfolio dashboard.

Version history — see CHANGELOG.md for the full PR-by-PR mapping.
X bumps on a major new feature/release, Y bumps on smaller polish/fixes.
"""

__version__ = "1.6.1"
__version_date__ = "2026-07-16"  # release date of __version__, ISO yyyy-mm-dd


def _format_version_date(date_str: str) -> str:
    # Runs at import time, so a malformed __version_date__ must never crash
    # `import portfolio_tracker` (that would take down every entry point over
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
