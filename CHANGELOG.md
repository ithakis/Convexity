# Changelog

Version scheme: `1.X.Y` — X bumps on a major new feature/release, Y bumps on
smaller polish/fixes/infra in between. Inferred retroactively from merged PR
history; going forward, bump `__version__` in `portfolio_tracker/__init__.py`
when merging a PR and add a line here.

## 1.10.0 — 2026-07-27
**The ML sentiment model has been dead in every installed copy — plus a
backend console, a full-screen tape, and dual-engine sentiment everywhere.**

- **Fixed: ML news-sentiment never ran in the desktop app.** `environment.yml`
  listed neither `lightgbm` nor `scikit-learn` — they were only in
  `requirements.txt`, which `install.sh` never pip-installs — so the `pt` env
  could not load the `mlsent-v1` artifact, while QF12 (where it was always
  tested) could. The `ModuleNotFoundError` was swallowed by a bare
  `except Exception` with no logging, so every sentiment-history record quietly
  recorded `ml_sar: null`: **1 non-null row out of 172**. Both packages are now
  conda-forge dependencies. **Run `./update.sh` once and restart the app** —
  the failed load is cached for the process lifetime.
- **The same failure can no longer hide.** `ml_sentiment.runtime_status()`
  reports `{available, reason, model_dir, …}` on `/api/news-diagnostics`, and
  Model Diagnostics renders an amber banner naming the actual cause and fix
  instead of the old, wrong "no deployed artifact" copy. The exception is also
  logged once per process. `_load()` now imports `scipy.sparse`/`sklearn` too,
  so `available()` can't report True in an env where every scoring call
  returns None.
- **New: Settings overlay** (gear button, 70% viewport, blurred backdrop) with
  `General` (empty placeholder) and **`Logs`** — a live tail of the backend
  console. `portfolio_tracker/logbuf.py` tees `sys.stdout`/`sys.stderr` into a
  4000-line ring behind `GET /api/logs`, capturing every existing `print()`
  with no call-site changes. This is the only way to see backend output in the
  desktop app, where the `.app` has no terminal. Autoscroll pauses when you
  scroll up; HTTP request lines are hidden by default.
- **Flash Tape expands to full screen** (99% viewport) on a click anywhere in
  the card. The row cap goes from 120 to 1000, headlines wrap instead of
  truncating, and each row gains a numeric score and a two-line summary. The
  inline panel is unchanged; filters stay in sync between the two.
- **Both engines are visible at once.** The flash tape, constituent briefs and
  the main table's NS column now show two dots (ML first, AI second, each with
  its own tooltip); the news timeline draws paired bars per article. Previously
  the two were collapsed into one value, which hid exactly the interesting case
  — on the test book, 108 of 654 articles carry opposite-sign ML and LLM scores.
- **Constituent Breakdown: `% 2D` and `% 1W` added beside `% 1D`, all three
  heat-tinted** red/green using the same `cellStyleHeat`/`colorDiverging`
  helpers as the main holdings table (anchors 5 / 7 / 20), so a stock reads
  identically in both. The Signal and AI cells get the same tint at anchor 0.5.
  `pct_2d` is a new backend field (trading bars, not calendar days) — **existing
  portfolios show `—` until a ↻ Refresh**. `pct_1d`/`pct_2d` are also registered
  in the main column registry and the Excel export, but deliberately left out of
  every built-in preset, so the holdings table is unchanged unless you add them.
- **`ml_confidence` recalibrated twice** (`1 - exp(-wsum/SCALE)`) — first from
  `/2.0` to `/0.35` because a real ticker rendered as "Confidence 6%", then
  measured for real and moved to **`/3.0`**: `/0.35` was also a guess, and it
  overcorrected — 95% of real ticker-days pinned to 90-100% confidence. `/3.0`
  was chosen by computing the actual evidence-mass distribution across 124
  real ticker-days and picking the scale that spreads it (p10/p50/p90 →
  ~32%/67%/86%) instead of saturating it. Display-only; the trained artifact
  is untouched.
- **New `scripts/backfill_ml_history.py`** re-scores the history records left
  null by the bug, from articles still in the news cache, so the diagnostics
  panels have real data immediately instead of accumulating for weeks.
  Idempotent, atomic, stamps `ml_backfilled: true`; `--force` recomputes
  already-scored records too, which is how the confidence recalibration above
  was applied to existing history.

## 1.9.1 — 2026-07-27
**Trackpad discipline + keyboard zoom in the desktop app.** A dense financial
dashboard should hold still; on a macOS trackpad it drifted sideways and
rubber-banded on every stray two-finger swipe. No zoom, wheel, gesture, or
overscroll code existed anywhere in the app before this — all of it was native
browser behaviour.
- **No more elastic bounce or swipe-back.** `overscroll-behavior: none` on
  `html, body` kills the vertical bounce past the page edges, the horizontal
  rubber-band, and Chrome's two-finger swipe-to-go-back. It only stops scroll
  *chaining* out to the document, so `.table-wrap` / `.an-table-wrap` still
  scroll normally.
- **Two-finger horizontal pan is dead at 100% zoom** — a non-passive `wheel`
  listener in `app.js` `preventDefault()`s horizontal-dominant scrolls. It
  deliberately stands down in three cases: while pinch-zoomed (checked via
  `visualViewport.scale`, because panning is the only way to navigate a zoomed
  page), on `ctrlKey` (that's how a trackpad pinch arrives — pinch-to-zoom
  keeps working), and on `shiftKey`.
- **Shift + scroll is the escape hatch** for reaching off-screen columns in a
  wide table when "Fit to screen" is off.
- **Cmd/Ctrl +, −, 0 zoom the desktop app.** `QWebEngineView` ships no zoom
  shortcuts of its own, so the desktop app previously had no way to zoom at
  all. `QShortcut`s now step a Chrome-matching ladder (67→200%) via
  `setZoomFactor`, with `Cmd+0` resetting to 100%. `Ctrl+=` is bound alongside
  `StandardKey.ZoomIn` because "+" needs Shift on most layouts. Not persisted —
  every launch starts at 100%. Browser mode is unchanged; Chrome's own Cmd +/−
  already works there and cannot be intercepted from JS.

## 1.9.0 — 2026-07-27
**Optimize tab: interpretable tail risk, a live-filling cloud, run history, and
per-company context.** Six targeted improvements on top of the v1.8 mean-CVaR
engine. The optimization math itself is unchanged — the LP still minimises daily
CVaR; what changed is what the tab *reports*, *persists*, and *shows*.
- **Tail risk is now a real, readable loss number (FRTB-style).** The x-axis used
  to plot daily CVaR scaled by √252, which routinely read >100% — not a loss
  anyone can act on. Displayed risk is now empirical VaR **and** CVaR computed on
  **overlapping 10-day compounded returns** (the FRTB liquidity-horizon
  convention), scaled 10→30 days by √3, at the α the slider is set to. The axis
  reads "CVaR 95% · 30-day loss" and typical values land in the 10–25% range.
  `LIQ_HORIZON`/`DISP_HORIZON` in `mpt.py` are the single place to retune.
- **VaR is reported at all.** The LP solver has always computed ζ = VaR_α and
  thrown it away; the stats panel, chart tooltip, and slider readout now show
  30-day VaR beside 30-day CVaR.
- **The displayed frontier is monotone on the displayed axis.** Points are still
  solved on daily CVaR but the efficient envelope is cleaned on `cvar30`, so the
  plotted line can't double back or be visibly dominated by a cloud point.
- **The cloud fills in as it computes.** The backend streams `frontier` (so axes +
  frontier draw immediately) then `cloud` chunks; a new dedicated canvas paints
  each chunk additively beneath the frontier. The 5–60s bootstrap wait is no
  longer a blank chart. `done` no longer re-ships the 20–80k cloud points.
- **Saved runs keep their cloud.** It was stripped on save and — despite a comment
  claiming otherwise — never re-sampled, so every restored run rendered an empty
  scatter. Runs now persist a ~2.5k-point downsampled cloud.
- **Last 3 runs per portfolio.** `.portfolio_tracker_mpt.json` moves from one run
  per view to a newest-first list capped at 3 (a re-run with identical params
  replaces the newest instead of duplicating). A "Recent runs" list under *Apply to
  Portfolio* shows each run's params, age, and headline return/CVaR, and reloads it
  on click. Both legacy on-disk formats are read transparently.
- **Per-company hover in the Weights section** (and the per-position bounds editor):
  weight at the selected point, realized return (annualized + total over the
  lookback), the asset's own 30-day VaR/CVaR, its BL posterior return, and the
  analyst target — mean, implied upside, low–high range, and dispersion with the
  analyst count. Served by new `asset_stats` / `analyst_detail` payload blocks,
  computed from data already fetched (no extra network calls).
- Restored pre-1.9 runs fall back to the old annualized CVaR and self-label
  "annualized · legacy" rather than mislabelling it as a 30-day figure.

## 1.8.0 — 2026-07-22
**Optimize tab reborn: Black-Litterman returns + mean-CVaR frontier.** The
Markowitz mean-variance engine is fully removed and replaced end-to-end.
- **Return engine — Black-Litterman.** Expected returns are now the BL posterior,
  not a historical mean. The prior is the market-cap equilibrium (reverse
  optimization Π = δ·Σ·w_mkt, caps FX-normalized to a common currency first);
  views are absolute per-asset returns implied by 12-month analyst price targets,
  with per-asset uncertainty tightening on analyst count / low dispersion. A single
  **Analyst-trust dial** [0,1] scales the view uncertainty Ω — 0 = pure market
  prior, 1 = full trust in targets. Covariance estimator is selectable
  (Sample / Ledoit-Wolf default / EWMA); Ledoit-Wolf now shrinks in per-period
  units then annualizes (was a ~10× under-shrink unit mismatch).
- **Risk engine — CVaR.** Risk is Conditional Value-at-Risk on the daily FX-adjusted
  return scenarios (~750 over 3Y), α adjustable 90–99% (default 95). CDaR and max
  drawdown are always computed and displayed for the selected portfolio (shown, not
  optimized); annualized vol is kept as a reference metric.
- **Optimizer — true mean-CVaR frontier.** Sweeps the return floor to trace a
  Return-vs-CVaR efficient frontier via the Rockafellar-Uryasev LP. Constraints:
  long-only + fully-invested toggle (off ⇒ cash allowed, Σw ≤ 1) and optional
  per-position min/max box. **Solver is a bespoke numba primal-dual interior-point
  method** that eliminates the T scenario variables analytically each Newton step
  (whole frontier ≪100 ms; ~40–60 ms for a typical portfolio); scipy/HiGHS is kept
  as a **test-only** reference (`tests/_cvar_reference.py`) certifying the fast
  solver to 1e-6. No scipy on the production hot path.
- **UI.** New chart: X = CVaR 95% (annualized, ×√252), Y = expected return (BL);
  single risk slider riding the frontier (min-CVaR → max-return); return-colored
  Monte-Carlo cloud rebased into (CVaR, return) space; 1/N, cap-weight, current
  anchors. Controls: risk slider, CVaR confidence, fully-invested toggle,
  per-position min/max, analyst-trust dial, covariance selector, lookback, cloud
  density. Side panel shows expected return, CVaR95, CDaR/max-drawdown, vol (ref.),
  analyst-view coverage, and the weights table. About-guide and all tooltips
  rewritten; tests rewritten against the new surface.
- **8 cores + streaming + stability bands (v2 rework).** The compute now saturates
  all cores via numba `prange` and streams honest progress. Specifically:
  - **All-cores compute.** `_cloud_kernel` and the new `_bootstrap_cvar` are
    `@njit(parallel=True)` over `prange`, so the Monte-Carlo cloud and the bootstrap
    band run on every core (~650 LP solves/s, ~65k cloud pts/s on 8 cores).
  - **Bootstrap frontier-stability band.** The compute budget is spent on something
    real: each of B replicas resamples the daily scenarios with replacement and
    re-solves the frontier, yielding a per-point CVaR uncertainty band (10th–90th
    pct) drawn on the chart and reported in the side panel — not visual padding.
  - **Wall-clock budgets.** Light / Standard / Dense are now **time targets**
    (~5 / ~15 / ~60 s), not fixed counts: the cloud is a fixed modest density,
    then the bootstrap runs until the target elapses, adapting to portfolio size.
  - **Honest streaming progress + ETA + Cancel.** `/api/efficient-frontier` now
    streams NDJSON (`progress`/`done`/`error`); the bar shows true pct = elapsed/target
    and a live ETA. A **Cancel** button aborts the request — the server stops the
    8-core work at the next chunk boundary. (Replaces the old fixed-duration fake bar.)
  - **Per-position min/max.** Weight limits are now **per holding** (an editable
    Symbol / Min% / Max% grid with an "All positions" broadcast row), not one global
    box; the solver already took per-asset boxes.
  - **Analyst-trust default lowered to 25%** (was 50%), from a 25-paper review: raw
    sell-side price-target *levels* are optimism-biased and only weakly/negatively
    predictive, so the posterior leans mostly on the equilibrium prior by default.
  - **Run history dropped.** Only the last run per portfolio is persisted and
    restored on open (`get_last_mpt_run`); the runs list + `GET/DELETE
    /api/mpt-runs/<id>` were removed. First-Optimize latency also eliminated by
    warming the numba JIT in a background thread at server startup.
- **Removed:** `critical_line[_with_floor]`, `frontier_curve`, `tangency_portfolio`,
  `monte_carlo_cloud`, `annualize`, `portfolio_stats`, `cvar_curve`, the vol/return
  cloud + MV compute-budget tiers + the second CVaR slider, and the `tangency` /
  `min_vol` / `cvar_frontier` JSON keys.

## 1.7.1 — 2026-07-21
Senior code pass — two owner-reported bugs + correctness/perf/LOC cleanup:
- **Saved weights now applied everywhere.** Switching portfolios no longer leaks
  the previous tab's weight mode: `loadPresetsForView` restores each portfolio's
  own saved active preset deterministically (was gated on `STATE.mode === "cap"`,
  so a leaked equal/preset mode silently fell back to cap-weight with no pill lit).
  Excel export previously **ignored saved weights entirely** and always cap-weighted
  every sheet; it now resolves each view's active preset (cap fallback) and labels
  the metrics section with the actual weighting used.
- **Portfolio performance graph is saved with the portfolio.** The cumulative
  1M/1Y/5Y curve (analytics `series`) was stripped before persisting and never
  rebuilt on reopen, so the chart showed "Not enough data to plot" after any reload.
  `series` is now persisted in the analytics cache; older series-less caches paint
  the panel instantly and silently backfill the curve via one live fetch (a fetch
  failure leaves the cached panel intact — no error-wipe, no refetch loop).
- **Correctness:** removed a duplicate `fmtPctSigned` (the dead earlier definition
  meant signed-% cells rendered `—` instead of the `n/a` span); fixed `_fx_usd_series`
  caching an empty series for the full 4h TTL on a transient failure (poisoned
  non-USD conversion) — now short-cached like `fx_index_history`.
- **Performance:** analytics `_analyst_for` reuses the per-symbol analyst fields the
  rows already carry and drops its redundant `Ticker.info` + `recommendations`
  re-fetch (N fewer yfinance calls on cold analytics; `fetch_one`'s single
  `recommendations` call is now the sole source, feeding both the column and
  analytics); the streaming build coalesces per-row `render()` via
  `requestAnimationFrame` (was ~150 full-table rebuilds per build).
- **LOC / de-dup:** shared `_safe_num` / `_normalize_dividend_yield` (removed copies
  in `xlsx_export`/`finnhub_adapter`); a `ynum(...)` COLS factory for the numeric-heat
  columns; a `Handler._read_json()` helper replacing the repeated Content-Length +
  `json.loads` boilerplate across all POST routes; a `_write_header_cells` xlsx helper;
  removed a dead `#pf-mode-toggle` listener and orphaned `updateModeButtons` alias.
- **Docs:** CLAUDE.md §12 MPT cloud-budget table reconciled to the shipped
  `frontier.py` configs; §7 export contract updated to reflect saved-weight honoring.

## 1.7.0 — 2026-07-19
UI/UX batch (7 features):
- **Cell-background modes.** `yo_dyn` columns now offer Off / 2C-Quantile
  (orange↔blue diverging quintiles) / Quantile (single-blue quintiles) / Min-Max
  in Customize. The `Rating` column joins them (blue = bullish low rating),
  replacing its old fixed-orange ramp; the removed `"percentile"` mode migrates
  to `"quantile"` on read.
- **Analyst Sentiment header de-duplicated.** Removed the redundant white
  title/subtitle; the "Aggregated by" control moved onto the Per-holding
  consensus line, reclaiming vertical space (the blue section kicker stays).
- **Built-in views edit in place.** Customize makes `Update "<view>"` the primary
  action and demotes "Save as new", so editing a default view no longer nags to
  fork a copy.
- **Sharper corners.** New `--r-lg/md/sm/xs` radius tokens (Sharp 6/4/2/1 px);
  all non-circular radii read a token so app-wide roundness tunes from four
  values.
- **RS peak highlight.** The strongest of the 12 monthly RS bars gets a
  full-saturation fill + darker outline so the peak reads at a glance.
- **Visible textarea selection.** The Portfolio constituents box now paints an
  accent selection highlight instead of the faint browser default.
- **Export confirmation popup.** A centered "Export complete" modal with the
  filename as a live re-download link (blob revoked on close), replacing the
  bare toast; the error path still toasts.

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
