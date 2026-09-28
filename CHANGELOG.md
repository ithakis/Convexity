# Changelog

Version scheme: `1.X.Y` — X bumps on a major new feature/release, Y bumps on
smaller polish/fixes/infra in between. Inferred retroactively from merged PR
history; going forward, bump `__version__` in `src/convexity/__init__.py`
when merging a PR and add a line here.

## Unreleased

Roadmap Phase 8 (`distribution` branch): the reference pack.

- The S&P 500 constituent list ships with the app (`src/convexity/data/sp500.json`, source and date recorded in the file) as the fixed universe of the daily reference pack. Public index membership only.
- New command `convexity build-reference-pack --out DIR [--limit N] [--previous DIR]` builds the reference pack: news for the universe (Finnhub + yfinance, the app's own fetch and rate limits), the Market read only (the local model, SHA-256 checked — no LLM), forward returns once known, written as `anchor.json.gz`, `history.json.gz` and `manifest.json`. Only tickers, dates and scores — never a headline, summary or link. It reads its Finnhub key from the `FINNHUB_API_KEY` environment variable only, and a degraded run (fewer than half the names scored) writes nothing. `--returns-only` just checks that the prices can be fetched.
- **Reference data (S&P 500).** The app downloads the daily reference pack from this project's GitHub release at most once a day (at launch and when news refreshes), checks every file against the pack's manifest (SHA-256, size caps, a strict format that only allows tickers, dates and numbers) and otherwise ignores it — a failed or odd download is logged and the previous copy stays. **Settings → Models & Data → Reference data** shows its date and coverage, has **Check now**, and a switch to turn it off (the download reveals to GitHub that a copy of Convexity is running, never what you hold).
- **A calibrated Market read from day one.** Until the app has 200 Market reads of its own, the percentile is ranked against the reference data (the same model's last 90 days over the S&P 500) instead of the 2023 backtest; the News tab names which one it is ("vs 500 S&P names, last 90 days (reference data, 1 day old)"). Your own reads take over as soon as there are enough.
- **Track record: "Model (500 names)".** Next to "Your holdings", the Track record can show the Market read scored across the S&P 500 from the reference data — the same date-clustered statistics on a far larger sample, so there is an answer long before your own history is big enough. (The News read is not in it: it runs on your own keys.)
- New workflow `.github/workflows/reference-pack.yml` builds the pack every weekday after the US close (and on demand) from the same commit as the app, and replaces the three files of the rolling `reference-pack` release. It never commits to the repository; only its publish job can write, and the Finnhub key is an Actions secret used by the build step alone. A `yahoo-check` run tests first that Yahoo answers from GitHub's servers. Only a missing release starts the pack's history afresh — any other failure to fetch the previous pack fails the run — and a publish that broke off half-way is repaired by the next run instead of blocking every later one.

Roadmap Phase 7 (`distribution` branch): repository tidy-up, for the v2.0.0 release.

- The package moved to `src/convexity/` (the standard `src/` layout). Nothing changes for installed apps or your data; developers run the same `uv sync` / `uv run convexity`.
- **Tidier repository root.** The Windows installer, the update scripts and the macOS dev launcher moved to `packaging/`. `install.sh` stays at the root, so the macOS/Linux one-liner is unchanged; the **Windows one-liner is now** `irm https://raw.githubusercontent.com/ithakis/Convexity/main/packaging/install.ps1 | iex` (the old `main/install.ps1` address stops working with this release).
- `build_symbol_db.py` is now a command, `convexity build-symbols`, so an installed app can build its fuzzy ticker database too. The old `dashboard.py` shim is gone: run `convexity` (or `python -m convexity`).
- README: status badges, the install command per OS, what you need (free Finnhub + NVIDIA NIM keys, added in Settings → API keys), where your data lives, how to uninstall, and an up-to-date architecture overview.
- GitHub issue forms for bug reports and feature requests (blank issues off; security reports go to the private channel in SECURITY.md).
- The engineering notes are split: `CLAUDE.md` keeps the rules, the file map and an index of the gotchas; the deep sections moved unchanged to `docs/architecture/` (backend, news, security, frontend, mpt, ci, desktop, jobs).
- Developer tooling: ruff replaces pyflakes (config in `pyproject.toml`); lint and formatting are now blocking in CI, after a one-off `ruff format` of the codebase.

## 1.15.0 — 2026-09-28

Roadmap Phase 6 (`distribution` branch): API keys in Settings.

- **Settings → API keys.** Enter the free Finnhub and NVIDIA NIM keys News needs, test each with one cheap call (works / rejected / rate-limited / can't reach the provider), or remove it — with links to where to get them. No more hand-editing `config.json`.
- **Keys take effect immediately** — no restart. Saving a new NVIDIA key also clears an earlier "key rejected" state, so the News read retries straight away.
- **First-run banner**: "Add your free API keys to enable News" until both keys are set; it can be dismissed.
- Keys stay private: stored only in `config.json` in the data folder (readable only by you), never shown back, logged or sent anywhere but the provider — the page shows only whether a key is set and where it comes from. A key set by environment variable still wins, and Settings says so. A `config.json` that is not valid JSON is never overwritten; Settings says to fix or delete it. The key routes only accept requests from the app's own page, so another website open in the same browser cannot change or remove a key.
- **Other websites can no longer touch your portfolios.** The app's server only listens on your own machine, but any web page open in your browser could still send it requests — enough to overwrite, rename or delete a saved portfolio, start refreshes that spend your API quota, or (by a trick called DNS rebinding) read your holdings. Every request now has to come from the app's own page; anything else is refused.

## 1.14.2 — 2026-09-28

Fixes from the Phase 3 verification findings (`distribution` branch).

- **Old data left behind by the move into the data folder is now visible.** When an old file and a different copy in the data folder both exist, the app uses the data-folder copy and never overwrites either — that used to be one log line. Now a banner says so, and Settings → About lists each old file with what to do (move it over the file in use, or delete it); the notice clears once that is done.
- **The app keeps port 8765 when restarted.** Its free-port check was stricter than the server's own bind, so for ~30 s after any restart the app moved to 8766+ (and a bookmarked `localhost:8765` stopped working). A port another app is actually using is still skipped.
- Developer checkout inside iCloud-synced `~/Documents`: iCloud hides every file in `.venv`, which broke `uv run` and the desktop window. The real venv now lives in `.venv.nosync` (a name iCloud leaves alone) with `.venv` as a symlink; `Launch Dashboard.command` sets this up by itself when it finds the problem.

## 1.14.1 — 2026-09-27

Roadmap Phase 5 (`distribution` branch).

- **The Market read works on a fresh install.** When the model is missing, the app downloads it in the background on first launch (from the `model-mlsent-v1.1` GitHub release, ~1.4 MB), verifies it against a SHA-256 pinned in the code, installs it and starts the Market read — no restart. Startup never waits for it.
- Settings → Models & Data shows the download (progress, installed, or the reason it failed) and a **Retry download** button; failures are also in the log and the Track record.
- A model folder that is present but incomplete (emptied or half-copied by hand) is treated as missing: its contents are moved aside to `mlsent-v1.1.incomplete-<time>` (never deleted) and the model is downloaded again; Settings offers Retry in that state too.
- Safe by construction: https only, the checksum is checked before the archive is opened, and only the six expected model files are extracted (absolute paths, `..`, links and anything unexpected are rejected).
- `ml/scripts/10_export_artifact.py --tarball` builds the release asset reproducibly; the model release procedure is in CLAUDE.md §4. The asset is published as the `model-mlsent-v1.1` release (a data release, separate from the app's `vX.Y.Z` releases).

## 1.14.0 — 2026-09-27

Distribution work, roadmap Phases 2–4 (`distribution` branch): an installable package, user data outside the code, and one-command installers on uv. conda is retired.

- **One-command install** (Phase 4): `curl -LsSf https://raw.githubusercontent.com/ithakis/Convexity/main/install.sh | bash` on macOS/Linux, `irm .../install.ps1 | iex` on Windows. The installer gets [uv](https://docs.astral.sh/uv/) if needed, installs `convexity[desktop]` from the latest GitHub release with `uv tool install` (from the release's source archive, so git is not required), checks the result with the installed interpreter, and builds `Convexity.app` (macOS) or Start Menu + Desktop shortcuts (Windows). Running it again updates; `update.sh` / `update.ps1` just do that.
- macOS: the installer checks for Homebrew `libomp` (needed by the Market read's LightGBM model) and offers to install it.
- Upgrading from a checkout: API keys in `.finnhub_key` / `.nvidia_key` next to the installer are copied into the data folder's `config.json` (never overwriting an existing key), since an installed package cannot find them in the checkout.
- **conda retired**: `requirements.txt`, `environment.yml` and the Miniforge / `pt` env code are gone. Development is `uv sync` / `uv run`; `Launch Dashboard.command` runs `uv run convexity`.
- The app icon ships inside the package (`convexity/assets/icon.png`), so an installed app has its splash and window icon.
- In-app fix hints (missing-dependency banner, Settings → Models & Data, Updating) now point at the installer instead of `./update.sh` + conda.
- CI: new `tool-install-smoke` job (installs the package as a user would and boots it); the macOS and Windows jobs now run the whole installer end to end instead of only the icon/shortcut commands. Dependabot watches `uv.lock`.

- **`pyproject.toml` is the dependency manifest** (hatchling, version read from `convexity/__init__.py`), with a committed `uv.lock`. Extras: `desktop` (PySide6), `dev` (pytest, ruff, pyflakes), `train` (duckdb, flaml). Python 3.11–3.14 (the test suite passes on 3.11 and 3.14).
- **Entry points:** `convexity` (browser mode) and `convexity-app` (desktop window) — `uv sync --extra desktop && uv run convexity-app`.
- CI installs through uv from the lockfile (`uv sync --locked`) and fails when `uv.lock` is stale.
- `scripts/check_dependency_manifests.py` checks `envcheck.REQUIRED` against `pyproject.toml`.
- Documented: lightgbm from PyPI needs Homebrew `libomp` on macOS (conda-forge's build does not).
- **User data moved out of the repo folder** (Phase 3). New `convexity/paths.py`: a per-user data folder — macOS `~/Library/Application Support/Convexity/`, Windows `%APPDATA%\Convexity\`, Linux `$XDG_DATA_HOME/convexity/`; `CONVEXITY_HOME` overrides. Inside: `state/*.json` (views, watchlists, mpt, column_views, news, sentiment_history — the old `.convexity_*.json`), `models/<version>/` (was `~/.convexity/ml_model/`), `symbol_db.sqlite`, `config.json` (API keys) and `logs/desktop.log` (was `~/Library/Logs/Convexity.log`).
- Fixes: an installed wheel (`uv pip install`) wrote the user's watchlists into `site-packages/convexity/`, where an upgrade would delete them. The migration rescues such a file.
- **Automatic migration on first launch of the app** (`convexity/migrate.py`; a plain `import convexity` never migrates): copy → verify size + SHA-256 → remove the old file. Never overwrites an existing destination, safe to interrupt and to re-run, never fatal. `python -m convexity.migrate --dry-run` shows what it would move. With `CONVEXITY_HOME` set it migrates nothing unless `CONVEXITY_LEGACY_ROOT` / `CONVEXITY_LEGACY_HOME` name the source.
- API keys: environment variable → `config.json` (`finnhub_api_key`, `nvidia_api_key`) → the old `.finnhub_key` / `.nvidia_key` files, which keep working for this release (logged when used, never deleted). The old model folder and a checkout-root `symbol_db.sqlite` also stay readable as logged fallbacks.
- Tests always run against a temp data folder (`tests/conftest.py`).
- Settings → About shows the data folder (`/api/health` carries `data_dir`). Two app instances launched at once migrate safely without false conflict warnings. A malformed `config.json` is reported in the log instead of silently ignored.

## 1.13.1 — 2026-09-26

Housekeeping before the distribution work (roadmap Phase 1, `distribution` branch). No app behaviour changes.

- README no longer claims the launcher opens a browser (removed in v1.12.3); it prints the URL.
- The root wishlist file is retired: its still-open items are now GitHub Issues (labels `feature` / `idea`); the shipped ones were dropped.
- `docs/Finance & Market Data APIs.md` renamed to `docs/market-data-apis.md`.
- `.github/dependabot.yml`: weekly version updates for pip and GitHub Actions (reviewed by hand, never auto-merged).

## 1.13.0 — 2026-09-26

- **Public repository: `ithakis/Convexity`.** History was rewritten before publication so no runtime state (holdings, watchlists, weights, optimizer runs), local settings or personal email is in any commit; the old private repo is archived.
- **Full internal rename** to Convexity: package `convexity/` (`python -m convexity`, `python -m convexity.desktop`), state files `.convexity_*.json`, model directory `~/.convexity/`, bundle ID `com.ithakis.convexity`, log `~/Library/Logs/Convexity.log`. Existing installs migrate automatically on first launch (`convexity/migrate.py`: atomic rename, never overwrites, never fatal). Re-run `install.sh` / `install.ps1` once to rebuild the launcher.
- **Security hardening:** CI `secrets` job (gitleaks over full history + forbidden-file check), actions pinned to commit SHAs with a read-only default token, a stricter local commit guard (runtime state, `settings.local.json`, SQLite, absolute home paths), `SECURITY.md`, and CLAUDE.md §18 "Security & public-repo rules" for AI agents.
- Removed hardcoded home-directory paths from docs and hooks.

## 1.12.3 — 2026-09-26

- Renamed the app to **Convexity** — "A portfolio optimization app built for long-term horizon investing." (window title, splash, page title, Settings → About, Excel export, installers, docs). Bundle ID, package name and log path unchanged so existing installs keep working.
- Risk & Return benchmark picker is now a styled popover (pill trigger, check-marked options, keyboard navigation) instead of a native `<select>`.
- The benchmark pick only drives the Risk & Return stats; the portfolio chart always compares against the S&P 500.
- Browser mode no longer launches Chrome on start — it prints the URL instead.

## 1.12.2 — 2026-09-26

### Portfolio chart: correct moving averages, TradingView-style measure, benchmark picker

- **Moving averages now span the whole chart.** Analytics fetches at least 200
  trading days before the displayed period and computes SMA 20/50/200 on that
  wider window, so an SMA no longer starts a third of the way across the
  chart. Only where truly no earlier data exists (MAX range, a recently
  listed holding) does an average start on fewer bars.
- **Drag-to-measure is transient, like TradingView.** While the button is
  held, a shaded band and a floating badge show every plotted line's return
  over the span; both vanish on release. Replaces the old design where a
  selection persisted until Esc or a click elsewhere, and its bottom summary
  bar (now removed).
- **Benchmark picker.** "vs SPY" in Risk & Return is now a dropdown — S&P 500,
  Nasdaq-100, Sector mix, Euro Stoxx 50, Nikkei 225, KOSPI — driving the
  comparison column, Beta/R²/Tracking error and the chart's comparison line.
  Nasdaq / Sector-mix overlays now list their period return under Risk &
  Return instead of the chart legend.
- Non-USD holdings and the Sector-mix blend are now FX-converted in a display
  currency other than USD (previously left in native currency).
- License changed from MIT to all-rights-reserved / viewing-only.
- Internal: both charts now share one crosshair/tooltip and one drag handler;
  `analytics._stats`/`_relative` are importable and directly unit-tested.

## 1.12.1 — 2026-09-26

### Repeated tickers no longer break portfolio analytics

- **Portfolio analytics returned HTTP 500 for any portfolio whose saved rows
  named a ticker twice** (`float() argument must be ... not 'Series'`). The
  duplicates came from the frontend: `build()` appended streamed rows while a
  background refresh job was also patching the table, and Cmd/Ctrl+Enter could
  start a second build past the disabled button. The build then saved the
  polluted table ("Data Center Builders": 20 rows, 16 names), so the portfolio
  broke on every load after.
- **One row per symbol, at every layer.** A new
  `helpers._dedupe_rows_by_symbol` (keeps the first position; the later row
  wins; a good row is never replaced by an error row) runs in `save_view`,
  `load_view` (portfolios already saved with repeats load clean) and the
  view list's row count. It runs again at the top of the analytics and
  optimizer entry points, which receive rows straight from the browser.
  `_bulk_close` and the refresh job's entry parsing drop repeats too.
- **Only the newest build writes the table.** `build()` now replaces rows by
  symbol, and a build started after it takes over the table, the save and
  the analytics request. Refresh-job rows are ignored while a build is
  filling the table.
- **Analytics 500s are diagnosable.** Both analytics routes now log the full
  traceback plus a "rows vs distinct symbols" summary to Settings → Logs.

## 1.12.0 — 2026-09-26

### News v2: two reads of the same headlines, never blended

- **The LLM engine had been dead for a month.** NVIDIA retired
  `nvidia-nemotron-nano-9b-v2` on 2026-08-26; every call returned HTTP 410, the
  failure was logged and swallowed, and refreshes kept serving August's reads
  over new headlines. The News tab is rebuilt around two engines presented as
  peers — no blended score, no "primary":
- **News read (what the news says).** `nvidia/nemotron-3-super-120b-a12b` reads
  each holding's 15 most relevant headlines through five lenses — Financials,
  Outlook, Competition, Regulation, Street view (plus `other` for M&A,
  buybacks, dividends, financing and insiders, and `none` for headlines not
  about the company). One strict flat JSON schema, two concurrent passes with
  their agreement shown, fixed tiers on a −2…+2 scale. Gold set of 149
  hand-labelled headlines: lens 85.6%, direction 87.5%, "not about it"
  precision 87.8%; Financial PhraseBank direction 94%; two-pass agreement 96%.
- **Relevance-ranked reading.** The read takes the most relevant headlines in
  the window, not the newest: NVDA's newest 15 were all listicles that merely
  mentioned it (15/15 "not about NVDA", no read at all); now four lens reads.
- **Market read (how prices reacted to news like this).** The per-headline
  LightGBM encoder, weighted over the 7-day window and recalibrated as
  `mlsent-v1.1`. The old frozen cut-points had drifted to labelling ~93% of
  holding-days bearish; tiers now speak only in the tails (5/10/70/10/5) and the
  percentile is anchored to the app's own last 90 days of reads once 200 exist.
  The Methodology and the Track record say plainly that no out-of-sample edge
  has been shown (Oct–Dec 2023 daily IC 0.016, both extreme tiers wrong-signed).
  A retrained ticker-day model (v2) missed its gates at both horizons (holdout
  IC 0.0095 / 0.0065 vs a 0.03 bar) and is not shipped.
- **Divergence flag (⇄).** One factual sentence when the engines disagree, or
  when a stock sells off ≥ 2σ on good news.

### Surfaces

- **NS column + hover card**: Market read then News read dots; one card with a
  row per lens (fact + linked headlines), agreement, the Market read's expected
  move, percentile (naming what it is ranked against) and tier, divergence,
  staleness and ages.
- **News tab**: two-half Portfolio signal, lens chips on the Flash Tape, the
  timeline and tape limited to headlines the News read found are about a
  holding, and a constituent table with per-lens cells.
- **Track record** replaces Model Diagnostics: date-clustered statistics only
  (daily cross-sectional IC, plain verdict — "Too early" under 40 trading
  days), long-short, hit rates with Wilson intervals, per-lens hit rates.
- **Methodology**: two side-by-side explainers with the real validation
  numbers; formulas in collapsible sections.
- **Excel export**: News read tier, one column per lens, Market read tier and
  z, divergence.

### Failure handling

- A retired model (404/410), a rejected key (401/403) or a missing key blocks
  the News read until the next refresh — zero further calls — and a banner
  names the reason. NIM 503s retry with jitter but no rate-limit penalty. A
  failed read keeps the previous one, faded and marked stale with its reason
  and original timestamp, together with the headlines it was made from (the
  tape and timeline keep their tags). The refresh chip counts "n failed".

### Removed

- Routes `GET /api/news-articles` and `POST /api/news-refresh`; the blended
  systematic/idiosyncratic score and its κ, quantile tier calibration, the
  LLM-vs-lexicon disagreement flag, ML-vs-LLM diagnostics panels; scripts
  `kappa_sensitivity.py`, `backfill_ml_history.py`,
  `benchmark_sentiment_prompt.py` (replaced by `benchmark_news_read.py`), and
  `ml/scripts/11_shadow_compare_llm.py`, `12_model_selection.py`.

## 1.11.3 — 2026-08-02

### The cloud and the frontier now share one feasible set

- **The scatter cloud ignored the per-position limits the optimizer respects.**
  `cvar_return_cloud` sampled the bare simplex (pure Dirichlet, no `w_min`/
  `w_max` parameter at all) while `mean_cvar_frontier` solved inside the box, so
  with limits set the two were drawn from different feasible sets. Measured on a
  book whose mins summed to 80% across four names: **52.7% of 20,000 cloud
  points sat at a lower CVaR than the frontier's own min-CVaR portfolio**. The
  frontier floated in the middle of the cloud instead of hugging its upper-left
  edge, and the axis stretched to a risk level nothing feasible could reach
  (cloud 10.6–35.8% vs frontier 20.4–25.7%). The cloud is now sampled inside the
  same box, cash rule, and rf the frontier was solved under — cloud 20.2–25.7%,
  left-of-frontier down to 0.6%, which is the pre-existing daily-vs-30-day
  estimator gap rather than a sampling error (the *unconstrained* control's min
  sits 3.6% below its own frontier, relatively worse).
- Sampling is `w = w_min + free_budget × Dirichlet` followed by a water-fill of
  the overflow into remaining headroom — not rejection sampling, whose
  acceptance rate collapses to nothing under a tight box. With no box the free
  budget is 1 and the mixture passes through untouched, so unconstrained runs
  are bit-identical to before.

### Chart framing

- **The rf floor no longer owns the frame.** Flooring the y-domain at rf spent
  ~85% of the height on empty space once every feasible portfolio sat between
  25% and 29% return against a 4.5% rf. rf may now pull the floor down by at
  most half the data's own span; past that the reference line is simply not
  drawn. Wide unconstrained runs still show it, unchanged.
- **A finished run always re-measures its own domain.** The streaming handler
  reused a frame fixed from the frontier before any cloud point existed, which
  is wrong in both directions — too small clips the scatter flat against the
  canvas edge ("zoomed in"), too large strands the plot in an empty frame.

## 1.11.2 — 2026-08-02

### Analyst consensus: upside, not price

- **The median and range columns showed target *prices*, not upside.** Deleted
  and replaced with two columns to the right of Upside: **Upside (median)**
  (implied upside at the median analyst target — reads against the mean upside
  beside it, so a visibly stronger or weaker tint says the mean is being
  pulled by an outlier) and **Upside range** (low↔high, sorted by relative
  spread). Both share the diverging heat ramp and the same 30% anchor as
  Upside, since they're the same quantity on the same scale.
- **10 of 15 largest holdings showed `—` for target low/median/high.**
  `_analyst_for` fetches the target trio on 8 pool threads at once; Yahoo
  answers a throttled burst with an empty dict rather than an error,
  indistinguishable from "no published range" without a retry. Added two
  jittered retries on the all-`None` path only. Live coverage went 10/15 →
  15/15.

### Optimization panel: scrolling, centering, click-to-edit

- **Per-position limits scrolled ~100px and then stopped dead.** The grid had
  its own `overscroll-behavior: contain` nested inside the panel's own
  scroller — a short inner scroller hits its end and refuses to chain to the
  parent. Collapsed to one scroll surface (`.pf-mpt-scroll`) for the whole
  chart/side/limits column.
- **The limits panel deformed the chart when it opened.** It was a normal flow
  sibling of `.pf-mpt-body`, so opening it shrank the chart's box and the
  canvases — sized only inside `mptSizeCanvases()` — rescaled a stale bitmap
  into it (a visible squash, plus drifted hover/click hit-testing). The panel
  now **pushes** the workspace down instead: `.pf-mpt-body`'s height is
  measured and pinned (`--mpt-body-h`) the instant before the panel opens, so
  the chart's geometry is untouched and the "Risk / Min-CVaR / Max-return" row
  simply scrolls out of view. Closing re-renders only if the window was
  resized while the panel was open.
- **"Selected portfolio" and the cloud figure had their own boxed panels.**
  Removed the backgrounds/borders so the workspace reads as one surface; the
  side panel keeps a single hairline divider as a column gutter.
- **Hovering a limits row showed a question-mark cursor and a stats tooltip.**
  Removed — the tooltip's per-`mousemove` reflow was also a real (if minor)
  contributor to the scroll slowness above.
- **MIN % / MAX % values were off-center under their headers**, and clicking a
  cell often landed on dead space around the input rather than the input
  itself. Centered both; the input now stretches to fill its row
  (`align-self: stretch`), so a click anywhere in the cell focuses it —
  verified with a 96-point hit-test sweep, 96/96 resolve to the input.
  Focusing a cell also now selects its value, so typing replaces it instead of
  appending.

## 1.11.1 — 2026-08-02

### Column presets are first-class

- **Custom presets are chips on the column bar**, appended after Momentum in the
  order you created them and rendered in green so it stays obvious which ones
  are yours (accent when selected, like any other chip). The `Custom ▾` dropdown
  they used to hide behind is gone — `setActiveView` always treated the two
  kinds identically, so there was no reason for one to be second-class.
- **"Modified · Save · Reset"** replaces "Save as new view". Built-ins are
  edited in place and saved the instant you change them, so the amber pill only
  ever meant "this no longer matches the factory layout": **Save** now says
  "yes, deliberate" and silences it (new `acked` flag, `POST
  /api/column-views/builtin-ack`), while **Reset** still restores the factory
  columns and colors. Editing the preset again — columns or colors — re-arms
  the pill.
- **New Settings → Column Presets.** Per-preset *Revert to default* for each
  built-in, available whether or not you pressed Save and without switching to
  that preset first, plus Delete for your own presets. Deleting moved here from
  the old dropdown so the column bar carries nothing destructive.

### Refresh progress is one honest bar

- **The bar filled to 100% during quotes and then rewound when news started.**
  `news_total` was only counted at the start of the news phase, so the client's
  denominator was quotes-only until then; an all-portfolios run had the same
  problem once per portfolio. `jobs._plan_totals` now seeds both totals from the
  snapshotted entries **before any frame is emitted** — including the `queued`
  frame and the snapshot a reattaching client reads — and each phase reconciles
  its own figure to the real count. Skipped phases and empty views release their
  reservation, so 100% is always reachable. The chip leads with the unified
  percentage, and the client clamps it monotonically.

### Visual fixes

- **The cancel `×` on the status chip rendered as a bordered 30px square.**
  Second instance of the `.topbar button` cascade trap: `.rf-chip-x` is `(0,1,0)`
  and lost every declaration — including its own `border: none` — to
  `.topbar button` `(0,1,1)`. Now `.topbar .rf-chip-x`, an SVG mark instead of a
  font-dependent `×` glyph, and a soft `--neg` tint on hover rather than a solid
  red fill. Verified in light, dark and Bloomberg.
- **The Analyst Sentiment section lost its outer card.** Its four cards were
  already boxed, so the wrapper was a border inside a border — and it cost 32px
  of width the cards now use. Holdings has no outer card either, so the two
  sections finally match.

## 1.11.0 — 2026-07-31

### ML sentiment: readable, and actually populated

- **The displayed ML score was unreadable by construction.** `score` was
  `tanh(ml_sar/2)` while the trained tier cuts are
  `[-0.0203, -0.0007, +0.0051, +0.0150]` — so the model's entire
  5th-to-95th-percentile range spanned under 0.02 of display width and the whole
  neutral band (45% of the mass by construction) rendered as a literal `-0.00`,
  while the tier label next to it said something else. New
  `ml_sentiment.display_score()` maps SAR through the trained cuts themselves
  onto `(-1, +1)`, pinning each cut to `app.js`'s own tier thresholds
  (`-0.5/-0.15/+0.15/+0.5`) so the number and the label can never disagree.
  Measured on real cached articles: `ml_sar = -0.0143` rendered `-0.01` and
  bucketed *neutral*; it now renders `-0.39` and buckets *bearish*, matching
  `ml_tier`. Raw `ml_sar`/`ml_score` are untouched everywhere including the
  sentiment history, so `compute_diagnostics`' IC, calibration curve and
  agreement grid keep their exact semantics. Applied per-article too, so the
  Flash Tape, briefs and timeline get real color and working tier filters.
- **Publish-before-initialize race in `ml_sentiment._load()`.** The `loaded`
  flag was set *before* the ~3 s lightgbm/scipy/sklearn import while the fast
  path reads it outside the lock, so any thread arriving mid-load saw
  `{loaded: True, ok: False, reason: ""}` and quietly fell back to the LLM. The
  tell was the log line "model not loaded" — that is `news_sentiment`'s
  fallback string for an *empty* reason, not a real failure, which always
  populates `reason`. It is also what "ML coverage 14/15" was. The flag now
  publishes in a `finally`. The model is additionally warmed on a daemon thread
  from `start_server()` (not `main()` — the desktop app never runs `main()`,
  which is why the numba warm has never applied there either).

### Refresh: one cancellable background job

- **One Refresh control.** Click (or `R`) refreshes the current portfolio —
  quotes, then news + sentiment. Long-press 600 ms (or `Shift+R`) refreshes
  every saved portfolio, behind a dialog stating real cost (portfolios, unique
  symbols deduplicated across them, duration, API budget). Progress renders
  inside the button; the per-ticker modal is now opt-in behind a status chip.
  The News-panel Refresh button is gone.
- **New `portfolio_tracker/jobs.py`.** Work runs on a daemon thread behind a
  replayable NDJSON stream, so a refresh survives switching tabs *and* a full
  page reload. Single-flight (a second submit gets `409` and attaches to the
  running job), and genuinely cancellable — queued items are de-queued and
  in-flight work checks a token at five points, including the two sleep gates
  that could otherwise park a worker for 65 s after you hit cancel.
- **News-window changes are instant.** `rescore_window` re-aggregates
  already-scored cached articles under the new window's tau: no network, no
  LLM, no ML inference, ~10 ms. Widening cannot conjure articles that were
  never fetched, and the UI says so rather than presenting a re-weighting of 7
  days of evidence as a 30-day read.
- **Two latent data-loss bugs fixed on the way.** Every `persistence` write was
  a bare `write_text` while the readers catch `JSONDecodeError` and return
  `{}` — a truncated write made *every saved portfolio silently disappear*.
  Writes are atomic now. And a background job will never overwrite good
  holdings with a batch Yahoo mostly failed to answer (>20% error rows keeps
  the prior data), nor repoint the restore-on-launch target.
- `/api/quotes-stream`'s body is extracted to `fetcher.stream_quotes` and the
  route rebuilt on it, which fixes a live bug: the old disconnect path left a
  `with ThreadPoolExecutor` block that *joined* in-flight futures, so an
  abandoned 150-symbol build kept hitting Yahoo for another ~30 s.
  `finnhub_adapter` now shares the Finnhub limiter and retries on 429; it had
  neither, so a quotes build and a news refresh each burned the same quota.
  **Note the trade-off:** that limiter is a sleep gate, and `fetch_one` makes
  two Finnhub calls per row, so a *cold* build of a large portfolio is now
  paced by the 55/min budget (~5 min for 150 new symbols) instead of racing
  ahead and getting 429s that blanked `Rec Δ6M` / `MSPR`. Warm builds are
  unaffected — the adapter's own 30–60 min caches mean repeat symbols cost
  nothing.

### Charts

- **Proportional granularity.** Every range was hardcoded `interval="1d"`, so
  1M was ~21 points on an 800px chart. 1M now draws 30m bars (~280 points), 3M
  and 6M share one 1h payload (~435 / ~870), and MAX is thinned to 1500. The
  fetch window is deliberately much wider than the display window, which is
  what lets SMA 200 cover a 1M chart instead of starting three-quarters across.
- **Drag-to-measure persists.** It used to self-destruct 1.8 s after mouseup —
  you could not read a measurement and then look at the chart. Selections now
  survive until you click elsewhere, press Esc, or change the range, and either
  edge can be dragged. One shared implementation replaces two ~150-line copies,
  closing a listener leak that accumulated a `window` handler per drag.
- **SMA 20/50/200** on both charts, off by default, with legends naming the bar
  frequency ("SMA 50 · 30m bars").
- **The detail modal header follows the active range**, not the 1-day return,
  with a badge naming the period; a live selection wins over the range tab.
- **Overlay scroll chaining fixed.** Twelve full-screen overlays, three of which
  each stashed their own "previous body overflow" and nine of which — including
  the detail modal — locked nothing. One `showOverlay`/`hideOverlay` pair over a
  nesting counter, plus `overscroll-behavior: contain`, enforced statically by
  `tests/test_frontend_overlays.py`.
- **Contribution-table headers** are sticky and opaque; the topbar used to
  scroll straight through them.

**Honest flag:** the ML model's holdout metrics are unchanged (`r2 = -0.004`,
`group_spearman_ic = 0.026`). These fixes make the signal readable and actually
populated — they do not make it more predictive.

## 1.10.2 — 2026-07-28
- **`symbol_db.py`'s local fuzzy ticker lookup was silently dead since the
  package restructure (#9).** `_DB_PATH` pointed at
  `portfolio_tracker/symbol_db.sqlite`, but the real 3.8MB database built by
  `build_symbol_db.py` has always lived at the repo root — `sqlite3.connect()`
  auto-creates an empty file at a missing path, so every lookup queried an
  empty table and returned `[]`, falling through to the ~20x-slower
  `yf.Search` fallback with no error or log line. Fixed by pointing `_DB_PATH`
  at the repo root, matching both the on-disk build artifact and CLAUDE.md's
  documented file layout — `"microsoft"` → `MSFT`, `"DaVita"` → `DVA`, and
  typo correction are fast again.
- **Flash Tape no longer hijacks page scrolling.** The inline card
  (`.ns-tape-body`) had its own `overflow-y: auto` scrollport, so a two-finger
  trackpad swipe or mouse wheel over it scrolled the headline list instead of
  the page. It's now `overflow: hidden` — the inline card shows a fixed
  preview slice, and scrolling only works in the fullscreen view (still
  opened by clicking the card).
- **QF12 is retired.** `pt` (originally the desktop app's env, §14) is now the
  one conda env used to run and test this app; `CLAUDE.md`, `README.md`,
  `Launch Dashboard.command`, `.pre-commit-config.yaml`,
  `.claude/settings.json`, and a few script docstrings no longer point at
  QF12.

## 1.10.1 — 2026-07-28
**1.10.0 fixed the dependency *declaration*; this release makes the ML model
actually run, and makes the failure impossible to miss next time.**

- **The ML model was still dead after 1.10.0.** Adding `lightgbm` and
  `scikit-learn` to `environment.yml` changes nothing until someone re-solves
  the environment, and nothing in the app, the launcher, or CI ever said so.
  On the reference machine the `pt` env's last solve was **2026-07-07 — twenty
  days before the 1.10.0 commit**; pulling the fix and restarting could not
  help, because the packages had never been installed. A dependency
  declaration is not a dependency. **If you are upgrading from 1.10.0, run
  `./update.sh` and fully relaunch the app.**
- **New `portfolio_tracker/envcheck.py` — one manifest, three enforcement
  points.** `REQUIRED` lists every runtime dependency with the feature it
  kills. It is checked at server start (a loud report into the terminal *and*
  Settings → Logs), served to the UI, and run by `install.sh`/`update.sh`
  (and the PowerShell twins) against the freshly-solved env, so a half-built
  environment fails the installer instead of exiting 0.
- **New CI gate:** `scripts/check_dependency_manifests.py` asserts every
  `REQUIRED` entry is declared in **both** `requirements.txt` (pip, what CI
  installs) and `environment.yml` (conda, what the desktop app ships on). The
  two were never compared before, which is the entire root cause of the
  original bug. Verified to fail when a dependency is removed.
- **A stale environment now announces itself.** `/api/health` carries a cheap
  `env_ok` flag (import-spec probe only, no imports), which renders a banner
  directly under the topbar instead of the app silently running degraded.
  A new cheap `GET /api/runtime-status` serves ML availability, the dependency
  check and provider-key status without the pandas/price-fetch cost of
  `/api/news-diagnostics`.
- **`tests/test_ml_sentiment.py` no longer skips itself into silence.** It used
  bare `pytest.importorskip`, so in exactly the broken environment the entire
  ML module skipped rather than failed. Missing deps are now a failure unless
  `PT_ALLOW_MISSING_ML=1` is set deliberately.
- **Settings overlay rebuilt** around a section registry: a search box at the
  top of the sidebar, grouped nav categories below it, and a right pane that
  titles and explains each section. `General` is no longer a placeholder — it
  carries the theme picker (making the long-press-only **Bloomberg** theme
  discoverable) and the "Fit to screen" toggle, both mirroring the topbar
  controls through single-mutator/single-painter sync helpers. New
  **Models & Data** section surfaces ML runtime state where someone would
  actually look for it, plus **About**. Adding a section is now one array
  entry, not an HTML edit.
- **"AI" renamed to "LLM" throughout the news-sentiment UI** — the gauge badge,
  all three dual-engine legends, tier tooltips, the constituent-table column,
  Model Diagnostics headings and the Methodology prose. "AI" was too general
  for what is specifically the LLM challenger to the ML model. The Excel
  export's "hand it to an AI agent" copy is a different meaning and unchanged.
- **The settings gear was never actually the size it claimed.** `.gear-btn`
  declared `font-size: 15px` but `.topbar button` (higher specificity) won the
  cascade, so it rendered at 12.5px. Now correctly specified and enlarged to
  20px, with the 30×30 footprint and info-button pairing unchanged.
- `requirements.txt` no longer calls `lightgbm`/`scikit-learn` "Optional",
  which contradicted `environment.yml` and is how they became droppable.

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
