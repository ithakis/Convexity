# Changelog

Version scheme: `1.X.Y` — X bumps on a major new feature/release, Y bumps on
smaller polish/fixes/infra in between. Inferred retroactively from merged PR
history; going forward, bump `__version__` in `portfolio_tracker/__init__.py`
when merging a PR and add a line here.

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
