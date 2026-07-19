# Changelog

Version scheme: `1.X.Y` — X bumps on a major new feature/release, Y bumps on
smaller polish/fixes/infra in between. Inferred retroactively from merged PR
history; going forward, bump `__version__` in `portfolio_tracker/__init__.py`
when merging a PR and add a line here.

## 1.6.2 — 2026-07-19
Windows desktop-app fixes. `install.ps1` shortcuts launched `pythonw.exe`
directly, which starts fine but hard-crashes with no Python traceback
(0xc06d007f in KERNELBASE.dll) as soon as numpy/scipy/numba's MKL +
llvmlite DLLs get exercised — the plain launch never gets the DLL search
path that `conda activate`/`conda run` sets up. Shortcuts now target a
hidden `launch_desktop.vbs` wrapper that runs the app via `conda run -n pt`.
Browser-mode's auto-open hard-coded macOS's `open -a "Google Chrome"` and
silently did nothing on Windows; now branches on `sys.platform`
(`os.startfile` on Windows, `webbrowser.open` elsewhere). The desktop app's
Export button produced no file — `QWebEngineProfile.downloadRequested` had
no connected handler, so Qt silently cancelled every download; now wired to
save into the OS Downloads folder with a status-bar confirmation.

## 1.6.1 — 2026-07-16
News tab v1.6.1 — the ML model is **promoted to the primary displayed signal**
(the LLM becomes a challenger). Backend: `news_sentiment.get_news_sentiment`
writes per-article ML scores onto the article feed and sets the displayed
`tier`/`score` to ML (with `disp_source`, `llm_tier`, `llm_score` preserved for
comparison); `compute_diagnostics` gains four live panels — rolling expanding-
window IC (ML vs LLM), a predicted-vs-realized calibration curve, a 5×5 ML–LLM
tier agreement grid, and ML coverage/confidence. Frontend: the portfolio gauge,
constituent table (new **Signal** + **AI** columns), NS dots, tape and timeline
now lead with ML and fall back to the LLM when the artifact is absent; a new
**Methodology** link opens a 90%-viewport technical article (7 sections, six
self-contained interactive SVG charts, KaTeX SAR formula) explaining how the
model was built and validated; the News Timeline gains a sentiment-tier filter
mirroring the Flash Tape. No model/artifact change — train/serve parity intact.

## 1.6.0 — 2026-07-16
ML News Sentiment (branch MLNews): a LightGBM model trained on FNSPID (5.75M
articles, 2009–2023) predicts the vol-standardized beta-adjusted abnormal
return (SAR) implied by news text; deployed artifact at
`~/.portfolio_tracker/ml_model/mlsent-v1/` with graceful degradation. New
modules `ml_sentiment.py` / `relevance.py` / `ml_features.py` (train/serve
parity contract), `ml_*` fields in the sentiment dict + history, and an ML
shadow scoreboard in the News tab's Model Diagnostics comparing ML vs LLM
rank-IC on identical records. Validation: 8/8 gates (IC 0.0263 holdout, 6.4σ
above shuffled-label null; IC>0 in 9/9 walk-forward years; monotone tier
means OOS). Training pipeline in `ml/scripts/`, design record in
`docs/ml_sentiment_design.md`, 60-paper lit review in
`docs/ml_sentiment_lit_review.md`.

## 1.5.4 — 2026-07-12
Rework the Bloomberg theme's colour hierarchy to match the real terminal.
v1.5.3 painted body text amber, flattening everything into one colour; the
actual Bloomberg look is **white data on black with amber reserved for
labels** — so `--text` is now white `#f2f2f2`, `--muted` is amber `#f49f31`
(labels/headers/secondary), borders are neutral gray `#333`, `--hover` is
the terminal's dark selection blue `#14273f`, and panel surfaces are
`#121212` (per the palette in feremabraz/bloomberg-terminal). A small
fidelity-override block paints table headers, ticker symbols, and the
constituents textarea amber — "amber = editable" being the terminal's own
convention. Light/dark themes untouched.

## 1.5.3 — 2026-07-11
Add a third UI theme: a **Bloomberg terminal** palette (pure-black canvas,
amber body text, Bloomberg-orange accent, colour-blind-safe green/red for
up/down). It's a hidden Easter-egg — **long-press the theme switch** (≥500ms)
to activate; a normal click still toggles light↔dark. Implemented purely
through the existing CSS-variable system: a new `[data-theme="bloomberg"]`
block plus a `THEME_COLORS.bloomberg` entry in `app.js`, so every widget
themes in one shot. A new `--on-accent` variable keeps text legible on the
bright orange accent (near-black in Bloomberg, white elsewhere). The choice
persists in `localStorage` like the other themes.

## 1.5.2 — 2026-07-11
Fix the News Timeline collapsing to the last ~1 day even with a 7-day (or
wider) window selected. `get_cached_articles` — which backs both the flash
tape and the Timeline — merged every constituent's cache newest-first and
truncated the union to a flat newest-250. A multi-symbol portfolio publishes
hundreds of articles/day collectively, so the newest 250 all fell within the
last ~19 hours even though each symbol's per-window cache already spanned the
full week (verified: 15 hyperscalers cached 696 articles across Jul 4-11, but
newest-250 kept only Jul 10-11). Now time-stratifies the merged feed via
`_window_sample` above a raised cap — the newest `min_recent` stay intact for
the flash tape, the rest spread across the window's time buckets so every day
of the Timeline stays populated; below the cap it's a no-op so normal-size
portfolios pass through at full density. Also sweeps widest-to-narrowest
cached window per symbol, fixing a stale-key read left over from 1.5.1's
window-scoped cache keys. Verified live: 692 rendered timeline bars span the
full week instead of clumping into two days.

## 1.5.1 — 2026-07-10
News tab refresh reworked to actually honour the selected analysis window.
`_fetch_yf_news` now pulls via `get_news(count=N)` (scaled to the window,
capped at 100) instead of the `.news` property, which was hard-capped at ~10
most-recent items and so never spanned more than a day or two on an active
ticker regardless of the 3D/7D/14D/30D control. yfinance news calls are now
globally throttled (`_YF_LIMITER`, 40/min) with jittered retry so a full
windowed refresh can't burst Yahoo into a 429, and the market feed is windowed
too — merging Finnhub's general feed with benchmark-index news (`^GSPC`/
`^IXIC`) so the market timeline spans the window instead of showing only the
latest. Retained-article cap raised 20 → 60, via a new `_window_sample()` that
time-stratifies the retained set across the window instead of keeping only
the newest N (a plain newest-N cut still collapsed to hours for high-volume
tickers like NVDA even after deepening the fetch — the newest 60 articles for
a firehose ticker can all land within the same afternoon). AI scoring still
reads the newest 15 (`_SCORE_BATCH`, now a shared module constant instead of
duplicated literals). Finnhub's per-request article slice now scales with the
window (`min(300, days*20)`) instead of a flat cap, so a 30D lookback on a
high-volume name doesn't get truncated back down to its newest few days.
Market/company news caches no longer lock in a Finnhub-outage-degraded
(yfinance-only) result for the full 30-day TTL — degraded results get a short
TTL so a Finnhub recovery isn't masked for a month. Both market AND per-symbol
news/sentiment cache keys are now windowed (`|{days}`) so a 7D and 30D refresh
never shadow each other; `get_cached_sentiment` (the fast per-row NS-dot read)
sweeps all lookback buckets since it isn't tied to the News tab's selected
window.

New refresh progress modal: a centered popup lists every job (market + each
constituent, weight-sorted) with a per-job bar that fills through fetch → AI
pass 1 → pass 2 → aggregate; two rows animate at once (the 2-worker pool),
completed jobs show tier color + score, and it auto-closes when done. Backed
by new `plan`/`market_stage`/`symbol_stage` NDJSON events on `/api/news-refresh`.

Coloring: Movers and the Market·Systematic cross-asset tape now magnitude-color
each return (muted near zero → full green/red at magnitude) via a new
`returnColor()` helper — Movers were previously uncolored (dead `pos`/`neg`
classes with no CSS rule). News Timeline "very bullish/bearish" tiers deepened
(`#16a34a`/`#dc2626`) against paler plain tiers so the two steps read clearly
apart; unified across timeline bars, per-article dots, and tier badges.

## 1.5.0 — 2026-07-09
Major News tab redesign. The four top cards (Portfolio Signal, Market·
Systematic Risk, Movers, What to Watch) are now one resizable quad — drag
either gutter to re-split columns/rows zero-sum, double-click to reset,
split persists. Added a News Timeline card (per-article bars by sentiment
strength, period tabs, ticker filter) above Flash Tape, and a lookback
window control (3D/7D/14D/30D) that actually threads through to the
backend scoring pipeline — recency decay (τ) now scales with the selected
window instead of a fixed 3-day constant, and the Portfolio Signal footer
shows which window a given score was computed with. New Bloomberg-style
"hairline rule + kicker" section anchors mark the start of the holdings
table and Analyst Sentiment. Section titles across the News tab are one
size larger, Title Case, and text-colored instead of small/uppercase/
muted; fixed Model Diagnostics not collapsing after a second click (a
missing `display:none` CSS rule, not a JS bug). All user-visible dates
standardized to "Jul 9, 2026" everywhere.

Ten new opt-in fundamentals columns (Customize Columns only, not in any
built-in preset): P/B, ROE, ROA, Gross Mgn, Net Mgn, FCF Yield, Rev Grw,
EPS Grw, Quick Ratio, Payout % — sourced from the same yfinance `info`
call `fetch_one` already makes, so no extra latency on the streaming
build path. All support the existing Off/Percentile/Min-Max heat modes.

Follow-up polish: the quad's default column split moved from 60/40 to a
near-even 47/53 (right column gets slightly more room); desktop splash
minimum floor shortened 7s → 6s; the app version now shows its release
date next to it (e.g. "v1.5.0 (09 Jul 2026)") in the splash, window
title, terminal banner, and app UI; the version tag moved out of a
permanent fixed-position corner overlay into the Analyst Sentiment
section's footer row (right-aligned), and the redundant date that used
to sit at the bottom-left of that same row was removed.

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
