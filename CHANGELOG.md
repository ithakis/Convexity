# Changelog

Version scheme: `1.X.Y` — X bumps on a major new feature/release, Y bumps on
smaller polish/fixes/infra in between. Inferred retroactively from merged PR
history; going forward, bump `__version__` in `portfolio_tracker/__init__.py`
when merging a PR and add a line here.

## 1.4.5 — 2026-07-08
The three built-in views (Default, Fundamentals, Momentum) are now editable in
place, just like custom views — reorder/add/remove columns and change per-column
color-coding, all persisted per view, with a "Reset to default" pill that
restores the factory layout. Color-coding type (Off/Percentile/Min-Max) is no
longer a single global setting shared across every view; it's now a property of
each view, so a column can be shaded one way in one view and another elsewhere.
As the first use of this, EV/EBITDA now defaults to **percentile** coloring in
Fundamentals (min-max still applies wherever a view doesn't override it). The
Customize Columns modal gained an "Update <view>" action for built-ins, and the
old pre-existing global `heat_prefs` remain as a fallback default under any
per-view setting. Backend: built-in overrides + per-view heat maps persist to
`.portfolio_tracker_column_views.json` via the extended `/api/column-views`
endpoints (new `/api/column-views/builtin-heat`; DELETE on a built-in name now
resets it instead of erroring). Column-view persistence gained unit-test
coverage (`tests/test_column_views.py`). No column widths or the "Fit to screen"
layout changed — the table renders exactly as before.

## 1.4.4 — 2026-07-08
Fixed a race condition in the "Fit to screen" table layout that could pin
the holdings table's wrapper a few pixels shorter than its actual content
(most likely during a fast streaming build, where `render()` fires once per
incoming row and an older, superseded async height measurement could land
after a newer one). Any shortfall turned the table into its own vertically
scrollable region — a "scroll trapped inside the table" experience distinct
from scrolling the page, so the table and the analyst-sentiment section
below it could end up scrolling independently instead of as one page.
Fixed with a render-generation guard (only the latest measurement pass ever
applies) plus a self-correcting height check (the wrapper can never end up
shorter than its content).

## 1.4.3 — 2026-07-08
Fundamentals columns (P/S, P/E, Fwd P/E, PEG, EV/Rev, EV/EBITDA, Op Mgn, D/E,
Curr Ratio, Div Yield) now shade blue instead of orange, scaled dynamically
against what's actually on screen instead of a fixed clip range, with a
per-column Off/Percentile/Min-Max mode (percentile-clipped Min-Max is the
default, fixing single-outlier columns washing out the rest of the ramp).
`20SMA`/`50SMA`/`200SMA` columns relabeled to `20MA`/`50MA`/`200MA` and
narrowed, freeing width for the rest of the table under "Fit to screen".
Customize Columns modal rebuilt: fixed a CSS specificity bug that stretched
it to 1120px with no padding, added per-column descriptions and the new
color-mode controls, and grouped the ~40-column list into labeled sections.

## 1.4.2 — 2026-07-07
Add a version tracker (startup banner, desktop splash/title, and app
footer now show the running version) and rename the app to
"Portfolio _App" everywhere — installer defaults (so future installs on
any computer pick it up automatically), window title, splash, dock/taskbar
name, browser tab, footer, and Excel export header.

## 1.4.0 — 2026-07-07 (PR #21)
Desktop app (PySide6 + QtWebEngine), install/update scripts, CI hardening
(installer + shortcut checks), and repo cleanup for public release.

## 1.3.1 — 2026-05-23 (PR #13)
Persist news/sentiment cache to disk, rate-limit refresh with retry,
robust fit-to-screen, symmetric Portfolio/News tab toggle.

## 1.3.0 — 2026-05-23 (PR #12)
News & Sentiment feature: Finnhub news + NVIDIA NIM AI sentiment scoring,
NS column, News tab.

## 1.2.1 — 2026-05-22 (PR #10)
Launch opens Chrome explicitly instead of the OS default browser.

## 1.2.0 — 2026-05-21 (PR #9)
Restructure the monolithic `dashboard.py` into the `portfolio_tracker/`
package (server, fetcher, analytics, fx, persistence, resolver, etc.).

## 1.1.2 — 2026-05-20 (PR #7)
Save rating distribution with rows, fix EPS bar direction, suppress
warning flood.

## 1.1.1 — 2026-05-20 (PR #6)
Fix MSPR coverage, Default-view overflow, Fit-to-screen centering.

## 1.1.0 — 2026-05-20 (PR #5)
Finnhub supplemental columns (MSPR, analyst rec trend) + min-CVaR
efficient frontier.

## 1.0.4 — 2026-05-19 (PR #4)
Metrics audit, Sortino bug fix, 26-test suite.

## 1.0.3 — 2026-05-19 (PR #3)
CI workflow, pre-commit hooks, quality-gate docs.

## 1.0.2 — 2026-05-19 (PR #2)
MPT overlay polish: denser cloud, brighter dots, click-to-apply.

## 1.0.1 — 2026-05-15 (PR #1)
Column-header hover tooltips with explanations.

## 1.0.0 — baseline
Initial single-user local portfolio dashboard: quotes, watchlists,
analytics, MPT efficient frontier, Excel export.
