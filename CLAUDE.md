# Convexity — Engineering Notes for AI Agents

This file is the source of truth for AI agents working on this codebase.
It captures the architecture, conventions, gotchas, and design contracts
that aren't obvious from the code alone. Update it whenever you add a
new system or change an established pattern.

---

## 1. What this app is

Single-user, local-only portfolio dashboard. Runs as a Python HTTP server
on `127.0.0.1:8765` and prints its URL — it does **not** auto-open a browser
(removed in v1.12.3 at the user's request); or runs in a native PySide6 window — §14. No accounts, no network calls except to yfinance
(Yahoo Finance), Finnhub (news), and NVIDIA NIM (the News read), no build
step. Almost all logic lives in the `convexity/` package, split
into focused modules (server, fetcher, analytics, fx, persistence, etc. —
see the table below); `dashboard.py` at the repo root is a thin
backward-compat shim (`python dashboard.py` still works).

User profile: quantitative-finance background, power user, runs the app
locally on macOS, expects complete autonomous delivery on requests, fast
iteration, no hand-holding. When in doubt about scope, ship a working
slice and document follow-ups rather than asking permission for each
sub-decision.

---

## 2. File layout

```
.
├── dashboard.py                 ← Backward-compat shim: `python dashboard.py` → convexity.server.main()
├── build_symbol_db.py           ← CLI to (re)build symbol_db.sqlite from public sources
├── convexity/           ← The package. Everything below is imported by server.py or desktop.py.
│   ├── __init__.py
│   ├── __main__.py              ← `python -m convexity` entry point
│   ├── server.py                ← HTTP server, route handlers, start_server()/shutdown_server() — §4, §14
│   ├── fetcher.py                ← fetch_one() per-symbol row builder — §4
│   ├── analytics.py             ← analyze_portfolios_multi(), bulk close, analyst blocks — §4
│   ├── fx.py                    ← FX spot rates, basket index history, currency conversion — §4
│   ├── frontier.py              ← Mean-CVaR frontier orchestrator (BL returns + CVaR risk, wraps mpt.py) — §12
│   ├── mpt.py                   ← Optimization primitives: Black-Litterman + mean-CVaR numba solver — §12
│   ├── persistence.py           ← JSON CRUD for views/watchlists/presets/MPT runs/column views — §4
│   ├── cache.py                 ← Process-global TTL cache dicts shared across modules
│   ├── logbuf.py                ← stdout/stderr tee → ring buffer behind /api/logs (Settings → Logs) — §16
│   ├── jobs.py                  ← background refresh job registry (single-flight, cancellable, replayable) — §17
│   ├── envcheck.py              ← runtime dependency manifest + self-check; the v1.10.0 guardrail — §4
│   ├── paths.py                 ← THE data-folder resolver (state/, models/, logs/, config.json, symbol DB) — §4 "Where user data lives"
│   ├── migrate.py               ← Runs from __init__: v1.13 rename (§18) + v1.14 copy→verify→remove into the data folder — §4
│   ├── resolver.py              ← resolve_symbol() pipeline (fuzzy input → Yahoo ticker) — §4
│   ├── symbol_db.py             ← Local fuzzy ticker DB (provider-agnostic schema) — §6
│   ├── helpers.py                ← Shared small utilities (dividend-yield normalisation, etc.)
│   ├── xlsx_export.py           ← One-sheet-per-portfolio Excel export — §7
│   ├── finnhub_adapter.py       ← Optional Finnhub supplemental columns (MSPR, rec trend) — §4
│   ├── news_sentiment.py        ← News fetch/cache, the News read (LLM, five lenses), refresh orchestration — §4
│   ├── ml_sentiment.py          ← The Market read (mlsent-v1.1 statistical model) — §4
│   ├── ml_features.py           ← Featurizer shared by ml/ training and the Market read (train/serve parity) — §4
│   ├── relevance.py             ← Deterministic relevance, title dedup, window_sample (shared with ml/) — §4
│   ├── lexicon.py               ← Loughran-McDonald scorer (data/lm_lexicon.json), an encoder feature
│   ├── news_diagnostics.py      ← Track record statistics (date-clustered IC, verdicts, hit rates) — §4
│   ├── model_fetch.py           ← First-run download of the Market read model (pinned URL + SHA-256) — §4
│   ├── desktop.py               ← Desktop app entry point (PySide6 + QtWebEngine) — §14
│   └── static/
│       ├── index.html           ← Main HTML template
│       ├── app.js                ← All frontend JS (state, columns, rendering, panels) — §5
│       └── style.css             ← CSS (themes, layout, components) — §8
│   └── assets/icon.png          ← Source icon (512×512 RGBA, transparent glyph), package data — §14
├── tests/                        ← pytest suite; tests/data/news_gold.jsonl is the News read gold set
├── docs/                         ← Reference/audit notes not needed to run the app day-to-day
├── scripts/                      ← Dev scripts (check_syntax.py, smoke_test_server.py, benchmark_news_read.py, …)
├── ml/                           ← FNSPID training pipeline for the Market read — docs/ml_sentiment_design.md
├── Launch Dashboard.command      ← macOS dev launcher: `uv run convexity` from this checkout, restarts cleanly
├── pyproject.toml                ← THE dependency manifest + entry points (`convexity`, `convexity-app`) — §3, §4
├── uv.lock                       ← uv lockfile solved from pyproject.toml (CI installs exactly this) — §13
├── .python-version               ← 3.11, the interpreter `uv` uses for this checkout
├── install.sh / install.ps1      ← Installer: uv + `uv tool install` of the latest release + launcher (macOS/Linux / Windows) — §14
├── update.sh / update.ps1        ← Thin wrappers: re-run the installer (that is the update) — §14
├── README.md
├── CLAUDE.md                     ← This file
├── LICENSE
├── .finnhub_key / .nvidia_key     ← LEGACY key files (gitignored); read as a logged fallback in 1.14, then config.json
└── .openrouter_key               ← Legacy OpenRouter key (superseded, still gitignored)
```

**User data is not in the repo (v1.14, roadmap Phase 3).** It lives in the
per-user data folder resolved by `convexity/paths.py` (`CONVEXITY_HOME`
overrides it):

```
~/Library/Application Support/Convexity/   (macOS; Windows %APPDATA%\Convexity\, Linux $XDG_DATA_HOME/convexity/)
├── config.json                 ← API keys {finnhub_api_key, nvidia_api_key} (Settings writes it in Phase 6)
├── symbol_db.sqlite            ← Built by `python build_symbol_db.py` — §6
├── state/
│   ├── views.json              ← Per-portfolio cached rows + metadata + weight presets
│   ├── watchlists.json         ← Per-portfolio entries strings
│   ├── mpt.json                ← Saved MPT efficient-frontier runs per portfolio
│   ├── column_views.json       ← Custom column-view definitions
│   ├── news.json               ← News + sentiment cache, LLM status
│   └── sentiment_history.json  ← One record per ticker-day read: Track record + live anchor
├── models/mlsent-v1.1/         ← The Market read artifact (was ~/.convexity/ml_model/)
└── logs/desktop.log            ← Desktop-app boot log (was ~/Library/Logs/Convexity.log)
```

Pre-1.14 copies of these (`.convexity_*.json` and `symbol_db.sqlite` in the
checkout root, `~/.convexity/ml_model/`) are moved there by `migrate.py` on
first launch; they stay gitignored because an un-migrated checkout still has
them. A fresh clone starts with an empty dashboard, not a previous owner's
holdings, and using the app leaves `git status --ignored` unchanged apart from
`__pycache__/` and `.venv/`.

**History is clean — keep it that way.** The repo is **public** as
`ithakis/Convexity` (v1.13.0). Its history was rewritten with `git filter-repo`
before publication to drop every runtime-state file and machine path; the
pre-rename private repo `ithakis/PortfolioTracker` is archived and must stay
private (its PR refs still point at the unscrubbed commits). Nothing that is
committed now can be taken back — see §18 before every commit.

---

## 3. How to run / restart

```bash
# Development checkout (the only dev workflow since v1.14):
uv sync --extra dev --extra desktop   # .venv/ from uv.lock
CONVEXITY_HOME=$(mktemp -d) uv run convexity       # browser mode (convexity.server:main)
CONVEXITY_HOME=$(mktemp -d) uv run convexity-app   # desktop window (convexity.desktop:main)
uv run pytest

# Or double-click the launcher (pid-file cleanup, clears .venv hidden flags,
# then `uv run convexity`):
./"Launch Dashboard.command"
```

After editing `pyproject.toml` run `uv lock` — CI's `uv sync --locked` and
`uv lock --check` fail on a stale lockfile.

**The installed app is a uv tool, not the checkout** (v1.14). `install.sh`
puts it in `$(uv tool dir)/convexity/` (`~/.local/share/uv/tools/convexity/`)
and `Convexity.app` execs that tool's `bin/convexity-app`. Editing the checkout
therefore does **not** change the user's running app; re-install it
(`CONVEXITY_SOURCE=<checkout> ./install.sh` for unreleased code, plain
`./install.sh` for the latest release) and relaunch.

**conda and the `pt` env are retired** (v1.14, roadmap Phase 4).
`requirements.txt` / `environment.yml` are gone; nothing builds or updates
`pt` any more. It may still exist on disk (as may the older QF12) — do not use
or recommend either.

**iCloud hides `.venv`, which breaks the app — ROOT-CAUSED (2026-09-27).**
`~/Documents` is synced by iCloud Desktop & Documents, and iCloud sets the
macOS `UF_HIDDEN` flag on every file under a **dot-named** folder. Python skips
a hidden `.pth` (`uv run convexity` → `No module named 'convexity'`) and Qt its
hidden plugins (`uv run convexity-app` → `Could not find the Qt platform plugin
"cocoa"`). Controlled test in `~/Documents`: a dot-named folder was fully
re-flagged within minutes; a plain folder and folders *created* with a
`.nosync` suffix (dot-named or not) never were. `chflags -R nohidden` does not
last (~20 files/s re-flagged), and **renaming an existing `.venv` to
`.venv.nosync` is not enough** — iCloud keeps flagging items it already
tracks (7k files in 4 min), while a venv *created* as `.nosync` stayed at 0.
**Setup on this Mac:** the real venv is `.venv.nosync`, built fresh
(`UV_PROJECT_ENVIRONMENT=.venv.nosync uv sync --extra dev --extra desktop`),
and `.venv` is a symlink to it, so plain `uv sync` / `uv run` work unchanged
(uv keeps the symlink). Both names are gitignored. `Launch Dashboard.command`
does this rebuild by itself when it finds a real `.venv` carrying the flag.
Diagnose with `find .venv/ -flags +hidden | wc -l` (trailing slash: follow the
symlink); a checkout outside `~/Documents` (e.g. `/private/tmp`) is unaffected.
`QT_DEBUG_PLUGINS=1` shows Qt scanning the right directory and finding nothing.

**Agents: never run the app against the user's real data.** Every dev, test
and verification run sets `CONVEXITY_HOME` to a temp dir (§4 "Where user data
lives"); without it a run reads and writes the real data folder, and the first
launch of 1.14 code migrates the checkout's state files into it. The user's
own app keeps running on 8765 meanwhile; `_pick_port` moves yours to 8766+.

**Critical gotchas when restarting:**

1. **System Python is missing yfinance** — always go through `uv run`
   (or the tool's own interpreter). `python dashboard.py` with a bare
   interpreter crashes on import.
2. **Port 8765 after a restart** — fixed after v1.14.1: `_pick_port()`'s probe
   used to bind without `SO_REUSEADDR` while the real server binds with it, so
   the ~30 s of TIME_WAIT after any restart pushed the app to 8766+. The probe
   now matches the server; a port something is *listening* on is still refused
   (verified), so `_pick_port()` still falls through to 8766, 8767, 8768, then
   any free port when 8765 is genuinely taken. Check
   `lsof -nP -iTCP -p <PID> | grep LISTEN` to see which port a server bound.
3. **Existing process check** — kill only what you started (keep its PID);
   the user's installed app runs as
   `~/.local/share/uv/tools/convexity/bin/python …/bin/convexity-app`, a
   checkout run as `.venv/bin/convexity`. Never `pkill -f convexity` blindly.
   The launcher stops its own previous run via `.dashboard.pid`.
4. **Python module caching** — restarting the server is the ONLY way to
   pick up changes to `symbol_db.py` or `xlsx_export.py`. Same for
   `dashboard.py` itself. No autoreload.
5. **Background warm-up** — the server runs `warmRecentTabs()` +
   `preloadFxIndexes()` ~1.2s after startup. Those write yfinance
   deprecation warnings to stdout — they look scary but are harmless.

---

## 4. Backend (`convexity/` package — server side)

### Caching layers (in order of speed)
- **`_CACHE`** — generic TTL cache, default 300s, 1800s for analytics.
  Keys are `f"{kind}|{symbol}|{params}"`. Used by `fetch_detail`,
  analytics, and a few helpers.
- **`_RESOLVED_CACHE`** — entry-string → final Yahoo ticker. Cached for
  the process lifetime; the goal is "type 'microsoft' once per session,
  pay the lookup cost once."
- **`_FX_RATES_CACHE`** (30 min), **`_FX_HIST_CACHE`** (4 h),
  **`_FX_CCY_HIST_CACHE`** (4 h) — currency-specific caches.
- **`_ANALYST_CACHE`** (in `xlsx_export.py`, 30 min) — analyst info for
  Excel export when analytics doesn't supply it.
- **`_VIEWS_LOCK`, `_WATCHLISTS_LOCK`** — threading locks around the
  persistence JSON files. Always acquire before read-modify-write.

### Where user data lives (`paths.py`, v1.14)
Nothing is ever written next to the code. Before 1.14 every path came from
`helpers._repo_root()` (walk up to `.git`, else the package dir), so an
installed wheel wrote the user's watchlists into `site-packages/convexity/`,
where the next upgrade deletes them. Now `convexity/paths.py` is the only place
a data path is built: `data_dir()`, `state_file(name)`, `models_dir()`,
`logs_dir()`, `config_file()`, `symbol_db_file()`. Stdlib only, no import-time
side effects, never creates a directory — writers `mkdir` their own parent.
`_repo_root()` survives only as `paths.legacy_root()` for migration/fallbacks.

- **`CONVEXITY_HOME` is mandatory for every test and manual/dev run.**
  `tests/conftest.py` sets it at import time (modules bind paths at import,
  and `__init__` runs the migration) plus per test, and points
  `PORTFOLIO_SYMBOL_DB` away from the checkout's DB. A dev run is
  `CONVEXITY_HOME=$(mktemp -d) uv run convexity`.
- **Migration** (`migrate.migrate_to_data_dir`, from `__init__`, **only when
  the process is the app** — `convexity._launched_as_app()`: the `convexity` /
  `convexity-app` scripts, `-m convexity[.server|.desktop]`, `dashboard.py`).
  A bare `import convexity` never migrates: during Phase 3 a dev script
  (`scripts/check_dependency_manifests.py`) run without `CONVEXITY_HOME`
  migrated the user's real data out from under two running old-code apps; it
  was restored byte-identical within minutes. Per file: copy
  to a temp beside the destination → fsync → size + SHA-256 check → `os.link`
  into place (no clobber) → re-check → re-hash the source → unlink it. Model
  dirs are staged whole and renamed in one step. An existing destination is
  **never overwritten** (identical ⇒ source removed; different ⇒ both kept,
  logged CONFLICT — and shown: `migrate.conflicts()` → `/api/health`
  `migration_conflicts` → a banner plus a list in Settings → About with what to
  do; it repeats every launch until the old file is moved over or deleted).
  Two instances launched at once race for each destination:
  the loser re-judges it (identical ⇒ dedup, not CONFLICT) and tolerates the
  winner having already removed the source. Temps of dead runs are swept —
  verified by `kill -9` at 20 points during a real launch. A `symbol_db.sqlite` with a
  non-empty `-wal` (live writer) is skipped. Key files are never moved.
  **Gate:** with `CONVEXITY_HOME` set it migrates nothing unless
  `CONVEXITY_LEGACY_ROOT` / `CONVEXITY_LEGACY_HOME` name the source — each
  separately, so pointing at a test checkout can't drag the real
  `~/.convexity` along. `python -m convexity.migrate --dry-run` prints the plan
  (running it that way skips the automatic run in `__init__` via
  `sys.orig_argv`).
- **One-release fallbacks, each logged once via `paths.note_legacy`:** keys
  (env → `config.json` → walk-up `.finnhub_key` / `.nvidia_key`), the model
  (`MLSENT_MODEL_DIR` → `models/<ver>` → `~/.convexity/ml_model/<ver>`), the
  symbol DB (`PORTFOLIO_SYMBOL_DB` → data dir → checkout root; the builder
  writes to `symbol_db.write_path()`, never the legacy place). Remove them
  after 1.14. A malformed `config.json` is logged once (`[config] ignoring …`),
  never silently treated as "no key".
- `/api/health` carries `data_dir` (Settings → About shows it).

### Persistence
Two separate JSON files (in `<data>/state/`) because they evolve independently:
- **`watchlists.json`** — `{name: entries_string}` —
  the source of truth for constituents.
- **`views.json`** — `{views: {name: {entries, rows,
  saved_at, stale}}, last_view: name}` — cached row payloads + stale
  flag + which view to restore on next launch. The `__current__` key is
  the ad-hoc / unsaved tab.

Renaming a portfolio touches BOTH files (`rename_watchlist` +
`rename_view`) — see `/api/portfolio/rename` handler. Failure on the
view side rolls back the watchlist rename.

**One row per symbol, everywhere.** A repeated `symbol` in a
rows list turns `closes[[...]]` into a frame with duplicate columns, so
`closes[s]` is a DataFrame and `analyze_portfolios_multi` 500s with
`float() argument must be ... not 'Series'` (it also doubles the holding under
equal weight). The repeats came from the frontend (`build()` appending while a
refresh job patched `DATA`, see §5) and `save_view` persisted them, so the
portfolio broke on every load. `helpers._dedupe_rows_by_symbol` (first
position, later row wins, never a good row for an error row) now runs in
`save_view`, `load_view` (heals already-corrupted views on read) and
`list_views`' `row_count`, **and again** at the top of
`analyze_portfolios_multi` and `compute_efficient_frontier_stream`, because
rows reach those straight from the client. `_bulk_close` also dedupes its
input. Resolved-ticker repeats (`microsoft` + `MSFT`) were already collapsed
by `resolver._ordered_resolve` at build time; row-level dedupe covers any that
slip through. Any new consumer that indexes a price frame by `symbol` should
go through the same helper.

### Symbol resolution pipeline (`resolver.resolve_symbol`, ~line 164)
Tries each layer in order, short-circuiting on first hit:

1. **`_RESOLVED_CACHE`** — instant (already resolved this session).
2. **`_normalize_google_colon`** — `"BME: SAN"` → `"BME.SAN"`. Just a
   pre-pass so the next layer can recognise it.
3. **`_normalize_exchange_prefix`** — accepts BOTH orderings:
   - Forward: `"XETRA.SAP"`, `"NASDAQ.MSFT"`, `"Euronext Amsterdam.IMAE"`
   - Reverse: `"SAP.XETRA"`, `"MSFT.NASDAQ"`, `"IMAE.Euronext Amsterdam"`
   Looks up the prefix in `_EXCHANGE_SUFFIX` (60+ exchanges), composes
   `tail + suffix` for yfinance.
4. **`_yf_symbol_has_data`** — single `fast_info.last_price` ping to
   verify the symbol actually exists on Yahoo.
5. **`_refine_via_search`** — yf.Search if the literal mapping failed.
6. **`_looks_like_ticker`** — uppercase ticker-shape regex (`AAPL`,
   `BRK-B`, `0700.HK`). Pass through unchanged.
7. **`_symbol_db_lookup`** — local fuzzy DB via `symbol_db.py`. Handles
   `"microsoft"`, typos like `"Microsft"`, company names like
   `"DaVita"`. Threshold 72 (rapidfuzz WRatio re-ranked by composite).
8. **`yf.Search`** — final fallback for anything else.
9. **Uppercase fallback** — if all else fails, return the input
   uppercased. Yields a row with `error="no data"` in the UI, which
   gives the user clear feedback.

### Streaming row build (`/api/quotes-stream`, NDJSON)
The fast path that powers "Build Dashboard". Calls `fetch_one(symbol)`
in a 5-worker `ThreadPoolExecutor`, writes one NDJSON message per row
as soon as it lands (`type: "start"`, then many `type: "row"`, then
`type: "done"`). The frontend renders progressively — DO NOT add any
heavy fetches (recommendations, EBITDA history, etc.) to this path.
Heavy data goes through `/api/detail` (per-row click) or analytics.

`fetch_one` returns this dict shape (every key shows up downstream):
```
symbol, name, price, change_abs_1d, pct_1d,
pct_1w, pct_1m, pct_3m, pct_6m, pct_ytd, pct_1y,
w52_high, w52_low, ath, delta_ath,
sparkline (list of 252 closes), volume,
sma_20, above_sma_20, sma_50, above_sma_50, sma_200, above_sma_200,
above_1m, rs_rank (list of 12 monthly samples),
market_cap, currency, exchange, sector, industry, quote_type, website,
ps_ratio, pe_ratio,
error  (only on failure)
```

Adding a new field: extend `fetch_one` (`convexity/fetcher.py`), add
it to the column registry (`COLS` in `convexity/static/app.js`,
line ~6), and `xlsx_export.py`'s `HOLDINGS_PRIMARY_COLS` auto-picks up
extras through the "extras pass" mechanism.

**Dividend-yield contract:** normalise Yahoo dividend yields to a
fraction at ingestion (`0.0315` = `3.15%`). Yahoo sometimes returns
`dividendYield` as a fraction and sometimes as a percent-like number
(e.g. `11.15` for `11.15%`); `_normalize_dividend_yield(...)` is the
shared guardrail. Prefer `dividendRate / price` when available, then
fall back to the raw yield fields with a `> 1 => divide by 100` fixup.
UI formatters and analytics should assume the normalized fractional
value, not re-detect units downstream.

**EPS surprise (`_earnings_surprise_yf`, yfinance):** `fetch_one`'s
`earnings_surprise` row field (8-quarter EPS surprise list, most-recent
first, shape `{period, actual, estimate, surprise_pct}`) comes from
yfinance's `get_earnings_dates`, NOT Finnhub. Finnhub's free
`/stock/earnings` is hard-capped at 4 quarters regardless of the `limit`
param, but the bar column renders 8 slots. yfinance reliably carries 8+
reported quarters from a single consensus-estimate snapshot, so we source
the whole series from one provider — mixing Finnhub's recent 4 with
yfinance's older 4 produced a visible discontinuity (same actual EPS,
different estimate snapshot => different surprise %). No API key needed;
cached for the analytics TTL (1800s), so cold build = one extra yfinance
call/symbol, warm build = zero. `finnhub_adapter.get_earnings_surprise`
still exists but is no longer wired into `fetch_one`.

**Finnhub supplemental columns (`finnhub_adapter.py`):** `fetch_one`
adds two optional row fields — `insider_mspr` (Form-4 Monthly Share
Purchase Ratio) and `rec_trend_fh` (6-month analyst recommendation
trend). They come from the Finnhub free API via
`from convexity import finnhub_adapter as _fh` in `fetcher.py`
(line ~32). The key is resolved by
`_load_api_key()`:
`FINNHUB_API_KEY` env var first, then a `.finnhub_key` file found by
**walking up** from the module directory (one line, the raw key). Walking
up matters: the key lives at the repo root, not inside `convexity/`,
and in a worktree run the package sits several levels below the checkout —
this mirrors `news_sentiment._load_key`. (Previously `_load_api_key` only
checked the module's own directory, so it never found the repo-root key and
the `Rec Δ6M` / `MSPR` columns silently rendered `—` even with a valid key
present.) When neither env var nor file is
present, `_fh` returns `None` for every call and the three fields are
written as `None`, so the columns render `—` and the server log stays
clean.

**Key stays local — never commit it.** `.finnhub_key` (plus `.env.local`
and `*.secret`) is gitignored, and a pre-commit guard at
`.claude/hooks/check-secrets.sh` (wired as the first PreToolUse hook in
`.claude/settings.json`) blocks any `git commit` that either stages one
of those files or leaks the key value into the staged diff. The file is
gitignored, so it does NOT travel between checkouts/worktrees via git —
if you run the app from a different directory, put it in the data folder's
`config.json` (`finnhub_api_key`, v1.14) or export `FINNHUB_API_KEY`. The adapter is stdlib-only (`urllib.request`), self-caches with
TTLs (3600/1800/3600s), and each call is individually try/excepted so a
failing endpoint never breaks a row. Cold build = up to 2 calls/symbol
(within Finnhub's 60/min free budget at 5 workers); warm build = zero.
Non-US tickers return empty data silently; only HTTP 429 logs one line.
The columns are `Rec Δ6M` and `MSPR` (Fundamentals preset). The third
Finnhub-style column, `EPS Surp.`, is now yfinance-sourced (see above).

**News & Sentiment — two engines, never blended (v1.12).** The News tab and
the NS column show two independent reads of each holding's headlines as
**peers**. There is no blended score, no "primary" and no "challenger" — do
not reintroduce one; the engines answer different questions.

| | **News read** (`news_sentiment.py`) | **Market read** (`ml_sentiment.py`) |
|---|---|---|
| Question | what the news says | how prices have reacted to news like this |
| Engine | LLM on NVIDIA NIM, `nvidia/nemotron-3-super-120b-a12b` | mlsent-v1.1: the v1 LightGBM encoder, weighted over the window |
| Output | five lens scores on −2…+2, overall score, fixed tiers | expected next-day SAR, z, percentile, tier (tails only) |
| Window | the user's 3/7/14/30D | always 7 days |

*Fetch and cache (`news_sentiment.py`).*
- Finnhub `/company-news` + yfinance `tk.news`, merged and deduplicated with
  the title dedup in `relevance.py` (rapidfuzz optional, like symbol_db); the
  syndication count survives as `n_duplicates` and every article gets a stable
  `aid` (md5 of url, else normalised title) that lens reads link back to. The
  retained set is `relevance.window_sample(60, min_recent=15)` — a
  window-spanning sample, not newest-N.
- `_NEWS_CACHE` / `_SENTIMENT_CACHE` (30-day TTL, 1800 s negative, transient
  failures never cached) are disk-backed in `<data>/state/news.json`
  (format v3, which also persists the LLM status). Sentiment entries without a
  `news` key (pre-1.12 shape) are ignored on load. Refresh is user-driven only.
  `get_cached_sentiment(symbol)` is the O(1) cache-only read `fetch_one` uses —
  the streaming build never triggers a fetch.
- Keys resolve through `helpers._load_local_secret` (env var, then
  `config.json` in the data folder, then — 1.14 only, logged — `.finnhub_key` /
  `.nvidia_key` found by walking up) **once, at import** —
  changing a key needs a restart.
- Finnhub 429: 3 attempts × 5 s, each `_FH_LIMITER.penalize()` backfills the
  rolling window so the next acquire waits out the minute, then a 65 s breaker.
  Measured: ~125 s inside one call. That is the budget working as designed, and
  it degrades rather than blanks: the read proceeds on yfinance-only articles,
  cached for 30 minutes instead of 30 days.

*News read (LLM).*
- Reads the **15 most relevant** headlines in the window (`_read_batch`:
  `relevance_score` descending, newest first within a tie), presented newest
  first — not the newest 15. Finnhub tags a mega-cap onto every listicle that
  mentions it: on 2026-09-25 NVDA's newest 15 were all "not about NVDA" while
  the headlines naming it sat just outside the cut. Recency still weights the
  aggregate.
- Each headline gets `{lens, score, fact}`: lens ∈ financials / outlook /
  competition / regulation / street / other / none, an integer score −2…+2, a
  fact ≤ 14 words. `other` (M&A, buybacks, dividends, financing, insiders)
  counts toward the overall read but has no lens row; `none` (not about the
  target — listicles, "X vs Y", market wraps) carries zero weight.
- What makes the output reliable (measured, keep all three): thinking off via
  `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` **plus**
  the `/no_think` system prefix; `response_format` json_schema with
  `strict: True` (`extra_body.nvext.guided_json` is rejected with a 400 on this
  backend); a **flat per-headline schema** — nested per-lens JSON written by
  the model came back malformed, so lens verdicts are computed in Python.
- Two passes run concurrently. `merge_passes`: mean score; a headline counts
  only if both passes attribute it to the target; `agreement` = share with the
  same lens and |Δscore| ≤ 1 (shown on every card). A single surviving pass
  gives agreement None and a 0.75 confidence factor. `validate_items` is
  strict (every id exactly once, lens enum, finite integer score); a failed
  pass is re-asked once.
- `aggregate_items`: weight = recency (τ = 3 days on 7D, scaled with the
  window) × source × novelty; lens score = weighted mean; overall = weighted
  mean over everything not `none`. Tiers are **fixed semantic cuts**
  (`tier_for`): ≤ −1.25 very bearish, ≤ −0.4 bearish, < 0.4 neutral (shown as
  "Mixed"), < 1.25 bullish, else very bullish. Not quantile-calibrated, on
  purpose — "Bullish" means the same thing every day.
- Failure semantics: 404/410 ("model retired"), 401/403 ("key rejected") and a
  missing key are **permanent** — `_LLM_STATUS.permanent` short-circuits every
  later call with zero HTTP, and `llm_unblock()` at the start of each refresh
  re-arms it. 503 → jittered 1.5–5 s retries with **no limiter penalty** (one
  penalised 503 cost a single ticker 64 s). 429 → penalty + 5 s + breaker. A
  failed read keeps the previous one with `stale: true` + `stale_reason` and
  its ORIGINAL `assessed_at` (never re-dated). Per-ticker outcome is ok /
  failed / empty; the refresh job counts them, `/api/health` carries
  `llm_ok` / `llm_error`, and the topbar banner names the reason.
- The same call returns the constituent **brief**, written to the Bloomberg
  Way contract in `_NEWS_READ_PROMPT`: ≤ 60 words, a compressed lead (theme →
  numbers vs expectations → what is at stake), ticker-first, active voice, a
  banned-word list, every claim traceable to a headline or the QUANT CONTEXT
  block, one sentence when coverage is thin. The market-wide read
  (`_MARKET_READ_PROMPT`, lens enum `other`/`none` only) gets the cross-asset
  tape (SPY, QQQ, ^TNX 1-day moves via yfinance, `_market_tape_context`)
  injected and returns a 1–2 sentence risk line, not a wrap.
- Budget: two NIM calls per ticker plus two for the market, behind the
  60/min `_NV_LIMITER` — a 15-name portfolio's news phase takes ~1.5–2 min
  at 2 workers.
- NIM availability changes without notice: `nvidia-nemotron-nano-9b-v2` was
  retired on 2026-08-26 and every call became a 410 while refreshes silently
  kept August's reads. Probe a model before switching (`_MODEL`).
- Certification: `scripts/benchmark_news_read.py` on
  `tests/data/news_gold.jsonl` (149 hand-labelled headlines, 51 tickers) and
  Financial PhraseBank. Current: lens 85.6%, none-precision 87.8%, direction
  87.5%, two-pass agreement 96%, PhraseBank 94%, p95 9.6 s per ticker. **Re-run
  after any prompt or model change**; the gates are in the script.

*Market read (`ml_sentiment.py`, mlsent-v1.1).*
- Per ticker: the 7-day articles → `window_sample(60, 15)` → the v1 encoder
  predicts each headline's SAR (one batched predict: hashed TF-IDF + a dense
  block with the LM lexicon, event flags and price context as of the article's
  day; session class `dateonly_cc`, because FNSPID is 98.5% date-only) →
  `ml_features.weighted_sar` (recency × source × novelty × relevance) →
  `calibrate`. Horizon: the next trading day.
- **The percentile is live-anchored.** Once the history holds
  `MIN_LIVE_HISTORY = 200` scores from the running model in the last 90 days
  (`_market_history`, excluding the ticker-day being scored), z and the
  percentile are computed against those; until then against the Jul–Sep 2023
  knots in `tier_cuts.json`. The dict carries `anchor` ("live" / "training")
  and `n_history`, and the UI names the reference. Tiers by percentile: ≤ 5
  very bearish, ≤ 15 bearish, 15–85 **No edge**, ≥ 85 bullish, ≥ 95 very
  bullish. Fixed 2023 cut-points drifted into "93% bearish" live; that is why.
- **No out-of-sample edge has been shown** (Oct–Dec 2023: daily IC 0.016,
  t 2.2 over 59 days, both extreme tiers wrong-signed). The Methodology and
  the Track record say so plainly — keep it that way. The v2 ticker-day
  retrain failed its gates and is not shipped. Full record:
  `docs/ml_sentiment_design.md` §11.
- Artifact at `<data>/models/mlsent-v1.1/` (`MLSENT_MODEL_DIR` overrides;
  `~/.convexity/ml_model/` is the logged 1.14 fallback): encoder booster, idf, df-pruning mask, `feature_schema.json`
  (must equal `ml_features.feature_schema()`), `tier_cuts.json`, `meta.json`.
  Build: `ml/scripts/09_tier_cuts.py --model v1.1`, then
  `10_export_artifact.py --deploy`.
- **First-run download (`model_fetch.py`, roadmap Phase 5).** The model is not
  in the package. When `models/<ver>/` is missing **or incomplete** — present
  means all six `ARTIFACT_FILES` (`model_fetch.complete()`), not just the
  folder; an emptied folder once blocked both the download and Retry — (and neither
  `MLSENT_MODEL_DIR` nor the legacy folder applies), `start_server()`'s warm-up
  calls `model_fetch.start()`: a daemon thread downloads the GitHub Release
  asset at `MODEL_URL`, checks it against `MODEL_SHA256` **before opening it**
  (mismatch ⇒ deleted, never retried), extracts member by member (only the six
  `ARTIFACT_FILES` under `<ver>/`; absolute paths, `..`, links, devices and
  extra files are rejected — never `extractall`) into a
  `.<ver>.<pid>.staging` dir, moves an incomplete existing folder aside to
  `<ver>.incomplete-<timestamp>` (never deleted, only after a verified
  download), `os.rename`s the new one into place and calls
  `ml_sentiment.reload()`. Boot never waits on it. Network errors / 5xx retry
  3× with backoff; 64 MB download cap; leftovers of dead runs are swept.
  Status (`downloading/verifying/installing/installed/failed/disabled/not_needed`)
  rides on `runtime_status()["download"]` (plus `model_missing`, which the
  Settings pane keys Retry on), and a missing-model `reason` names
  the download failure, so both Settings → Models & Data (with **Retry
  download** → `POST /api/model-download`, which ignores the disable flag) and
  the Track record say why. Every transition prints a `[model_fetch]` line.
  Env: `CONVEXITY_MODEL_URL` overrides the **URL only** (http allowed for
  loopback only) — the hash is never overridable, it is the trust anchor;
  `CONVEXITY_MODEL_DOWNLOAD=0` disables the automatic start
  (`tests/conftest.py` sets it — no test touches the network).
  `ml_sentiment.reload()` takes `_LOCK` first, so a boot warm-up load that is
  already running finishes before it, and an already-loaded model is left alone.
- **Model release procedure** (retrain or recalibrate → users get it):
  1. retrain / `09_tier_cuts.py`, then `10_export_artifact.py --deploy --tarball`
     (or `--tarball --from <bundle dir>` to pack an existing bundle — the
     tarball is deterministic, so the same six files always hash the same);
  2. check every file is free of home paths / private data (the asset is public, §18);
  3. with the user's explicit OK, `gh release create model-<ver> <ver>.tar.gz`
     (a separate tag from the app's `vX.Y.Z` releases);
  4. download the published asset and confirm its SHA-256 matches;
  5. bump `ARTIFACT_VERSION` (if the version changed), `model_fetch.MODEL_VERSION`,
     `MODEL_URL` and `MODEL_SHA256` together (`test_pins_are_consistent`), and
     ship an app release. Never replace the asset of an existing tag — old
     installs pin its hash.
- Graceful degradation is the contract: a missing artifact, missing
  lightgbm / scikit-learn / scipy, or a schema mismatch ⇒ `available()` False,
  every call `None`, the News read untouched. It must not be *silent*: the
  reason is in `runtime_status()`, logged once (`_warn_ml_once`), and shown in
  the Track record and Settings → Models & Data. `_STATE` caches a failed load
  for the process lifetime — installing a dependency needs an app restart.
- **Train/serve parity is the invariant.** `ml_features.py` (featurizer,
  `weighted_sar`, window constants) and `relevance.py` (relevance heuristic,
  title dedup, `window_sample`) are imported by both `ml/` and production;
  changing them invalidates the artifact, and the parity tests in
  `tests/test_ml_sentiment.py` guard it. Relevance stays hand-set — never
  fitted on FNSPID — so a future relevance model can be trained on that corpus
  cleanly. `window_vector` / `attention_shock` build the v2 research panel only.
- `confidence = 1 − exp(−wsum/3.0)` (`_CONF_SCALE`, both engines) is display
  only; 3.0 came from sweeping the real `wsum` distribution (p10/p50/p90 =
  1.17/3.34/5.86 → ~32/67/86%). Re-derive it the same way if news volume
  changes materially.
- **A dependency declaration is not a dependency** (the v1.10 lesson: the ML
  model was dead in every installed copy for weeks because `lightgbm` was only
  in `requirements.txt`, and the fix then sat unsolved in the `pt` env).
  `convexity/envcheck.py` is the single runtime-dependency manifest,
  enforced at boot, on `/api/health` (`env_ok` → banner), by
  `install.sh`/`install.ps1` (run with the installed tool's interpreter), and
  in CI by `scripts/check_dependency_manifests.py` (every `REQUIRED` entry must
  be in `pyproject.toml`'s `[project].dependencies` — not an extra; since v1.14
  pyproject is the only manifest). Verify env fixes **with the interpreter
  that runs the app** — the installed tool:
  `~/.local/share/uv/tools/convexity/bin/python -c "from convexity import ml_sentiment as m; print(m.runtime_status())"`,
  or `uv run python -c ...` for a checkout's venv.
- **The installers resolve from pyproject ranges, not `uv.lock`.**
  `uv tool install` does not read a lockfile, so a new user gets the newest
  versions inside the declared bounds; CI's `tool-install-smoke` job boots
  exactly that resolution. A breaking upstream release shows up there first —
  tighten the bound in `pyproject.toml` rather than pinning in the installer.
- **lightgbm from PyPI needs Homebrew `libomp` on macOS** (verified
  2026-09-26, lightgbm 4.7.0): `lib_lightgbm.dylib` loads `@rpath/libomp.dylib`
  with rpaths `/opt/homebrew/opt/libomp/lib` and `/opt/local/lib/libomp` only.
  With those rpaths pointed elsewhere the load fails even after importing
  sklearn, whose bundled `.dylibs/libomp.dylib` does not satisfy it. (The
  retired conda build bundled llvm-openmp, which is why this only surfaced with
  uv.) `envcheck` can't see this (it uses `find_spec`), but
  `ml_sentiment.runtime_status()` reports the failed load. Fix:
  `brew install libomp`. `install.sh` checks the three rpath locations before
  installing, offers `brew install libomp` (from `/dev/tty`, so it works under
  `curl | bash`), and after installing tries `import lightgbm` with the tool's
  interpreter — a warning, never a failed install.
  `tests/test_ml_sentiment.py` fails (not skips) on missing deps unless
  `PT_ALLOW_MISSING_ML=1`.
- Training-only dependencies stay out of `envcheck` and the runtime
  dependencies: DuckDB (all v2 parquet IO) and FLAML are the `train` extra;
  pyarrow (stages A–C only) is deliberately in neither — it switches pandas 3's
  string backing.
- **Retraining gotcha (hard-won):** FLAML+LightGBM on the raw 262k-column
  sparse matrix re-bins per trial×fold and stalls (16 h in
  `PushDataToMultiValBin`). Keep the df-pruning mask, `log_max_bin=5` and the
  capped search space in `07_train_flaml.py`. After any retrain re-run `09`
  (and re-verify the tier shares on a later window).

*Divergence, history, Track record.*
- `compute_divergence` emits one factual sentence when the engines disagree:
  `good_news_weak_reaction` (News bullish+, Market bearish−),
  `weak_news_strong_reaction` (the mirror), `sold_the_news` (News bullish+
  while today's move is ≤ −2σ of 20-day vol). Rendered as ⇄.
- `<data>/state/sentiment_history.json`: one record per (UTC date,
  symbol) per refresh — news score/tier, lens scores, agreement,
  `market_score/sar/z/pct/tier`, `market_model`, price, beta. It feeds both
  the Market read's live anchor and the Track record.
- `news_diagnostics.compute` (the Track record; `/api/news-diagnostics`) uses
  **date-clustered statistics only**: daily cross-sectional Spearman IC vs the
  forward idiosyncratic return, t from the daily series (Newey-West, lag h−1,
  at horizons > 1), verdict < 40 days "Too early — N of ~60 trading days",
  t ≥ 2 and mean > 0 "Evidence of an edge", 1 ≤ t < 2 "Weak evidence", else
  "No evidence"; long-short curve; hit rates with Wilson CIs (also per lens);
  two-pass agreement; a collapsed "For quants" block (daily IC, tier table,
  Market calibration). The Market read is ranked on the raw `market_score` —
  z is re-anchored when the anchor switches, the score never is.
  `load_records` maps pre-1.12 `s_idio`/`ml_sar` records (a compat shim with a
  delete-after date in its docstring).
- The Excel export writes `SENTIMENT_COLS` (News read, one column per lens,
  Market read, its z, divergence) via `xlsx_export._sentiment_value`; the raw
  payload is in `HOLDINGS_SKIP_EXTRAS`.

### Analytics (`/api/portfolio-analytics-multi`)
`analyze_portfolios_multi(rows, weight_sets, period, display_ccy)`.
Returns either `{set_name: analytics_dict | {"error": ...}}` OR
`{"results": {...}}` OR `{"error": ...}` — handle all three shapes.

Per-set analytics_dict has these top-level keys (all important — none
flat strings except metadata):
```
period, display_ccy,           ← scalar metadata
weights_applied, active_symbols, missing_symbols,
series {portfolio, drawdown, sma {"20","50","200"}},  ← time-series (skip in exports)
stats {total_return, ann_return, ann_vol, sharpe, sortino, calmar, max_dd},
benchmarks {SPY, QQQ, STOXX50, N225, KOSPI, SECTOR: {
    label, series,
    stats {…same keys + beta, r2, te vs SPY…},
    rel {beta, r2, te}          ← the PORTFOLIO measured against this benchmark
}},
weighted, contribution,        ← per-holding breakdowns
analyst {mean_rating, rating_coverage_weight, weighted_target_upside_pct,
         target_coverage_weight, n_analysts_total,
         distribution_pct {strongBuy, buy, hold, sell, strongSell},
         holdings [{symbol, name, weight, price, target_mean,
                    target_low, target_high, upside_pct, mean_rating,
                    rec_key, n_analysts, dist}, ...]},
exposure {by_sector, by_industry, by_country, by_currency},
concentration {top5, herfindahl, effective_n},
warnings
```

**One wide fetch, then slice.** `_bulk_close` pulls holdings + every
benchmark + the sector ETFs over `_WARMUP_YF[period]` (≥200 trading days
before the period start), FX-converts all of it (`_apply_fx_to_closes` — the
indices quote in EUR/JPY/KRW, and a USD display still converts non-USD
holdings), then slices to `_period_start()` for every stat. Only the SMAs use
the warm-up, which is why SMA 200 spans the whole chart; `min_periods=1` means
an average starts on fewer bars only where nothing earlier exists (MAX, a
young holding). The calendar is days *a holding* traded — foreign indices are
ffilled onto it, never allowed to add their own holidays as zero-return days.
Same-day beta vs Nikkei/KOSPI is understated (they close before the US
opens); the Beta tooltip says so. `_stats` / `_relative` are module-level and
tested directly in `tests/test_metrics.py`.

The `analyst.holdings` list is a HUGE win for Excel export — it means we
don't need a separate parallel `Ticker.info` fetch when analytics
succeeded. `xlsx_export.build_workbook` does exactly this.

### FX
- **`fx_rates(base)`** — spot rates against the 6 majors. 30-min cache.
- **`fx_index_history(base, period)`** — synthetic trade-weighted index
  level (base vs the basket of other majors), normalised to 100 at
  window start. 4-hour cache. Has a "bulk download → if empty, retry
  sequentially with jittered sleep + per-pair retry" hardening for
  yfinance rate limits.
- The frontend FX hover cache (`FX_INDEX_CACHE`) only stores successful
  results (`length >= 2`). Empty arrays are NEVER cached — fixes the
  old "no data sticks forever" bug.

### HTTP routes (`Handler` in `server.py`, ~line 92)
**GET**
- `/`                              — serves `static/index.html`
- `/api/views`                     — list of saved views (metadata only)
- `/api/views/<name>`              — full view payload (rows included)
- `/api/watchlists`                — `{name: entries_string}` map
- `/api/fx-rates?base=USD`         — spot rates
- `/api/fx-index?ccy=…`            — one synthetic basket index
- `/api/fx-indexes-bulk?ccys=…`    — many in one call (sequential server-side)
- `/api/detail?symbol=…`           — heavy per-symbol payload for the modal
- `/api/export-xlsx`               — Excel export of every saved portfolio

**POST**
- `/api/quotes` (legacy, blocking) — `{entries: [...]}` → `{rows: [...]}`
- `/api/quotes-stream` (preferred) — NDJSON streaming, the fast path
- `/api/watchlists`                — upsert `{name, entries}`
- `/api/views/<name>`              — save view body `{entries, rows, set_last?}`
- `/api/last-view`                 — set the restore-on-launch target
- `/api/portfolio/rename`          — `{old, new}` — atomic rename of both files
- `/api/portfolio-analytics`       — single weight vector
- `/api/portfolio-analytics-multi` — multiple weight vectors (e.g. equal+cap+custom)
- `/api/column-views`              — upsert custom column view `{name, columns}`
- `/api/column-views/active`       — set active column view `{name}`
- `/api/weight-presets`            — upsert `{view, name, weights, rename_from?, set_active?}`
- `/api/weight-presets/active`     — set `{view, name|null}` as the active preset for a portfolio
- `/api/efficient-frontier`        — **streams NDJSON** (`progress`/`done`/`error`) for the mean-CVaR frontier. Body `{rows, lookback, rf, alpha, fully_invested, bounds, cov_model, haircut, budget, current_weights, display_ccy}` where `bounds` = `{sym:{min,max}}` per-position weight fractions and `budget` is a wall-clock tier (light≈5s/standard≈15s/dense≈60s). Client renders a real pct/ETA bar; aborting the request cancels the 8-core compute.
- `/api/model-download`            — start (or retry) the first-run model download; `202` + `model_fetch.status()` (§4 Market read)
- `/api/mpt-runs`                  — save `{view, run}` onto the portfolio's **last-3 run history** (newest-first, cap 3; a run whose `params` match the newest replaces it instead of duplicating)

**DELETE**
- `/api/views/<name>`              — drop a view (cascades to the saved run)
- `/api/watchlists?name=…`         — drop a watchlist (cascades to view delete)
- `/api/column-views/<name>`       — drop a custom column view (built-ins rejected with 400)
- `/api/weight-presets?view=…&name=…` — drop a per-portfolio weight preset

**GET (also)**
- `/api/column-views`              — `{builtins, custom, active}`
- `/api/weight-presets?view=…`     — `{presets: [...], active: name|null}` for a portfolio
- `/api/mpt-runs?view=…`           — `{last: run|null, runs: [...]}` — newest run + the last-3 history (rendered under *Apply to Portfolio*)
- `/api/logs?since=<seq>&limit=<n>` — backend console tail from `logbuf` (§16)
- `/api/runtime-status`            — cheap ML availability + `envcheck.status()` +
  provider-key booleans + LLM model/status + version. Powers Settings → Models & Data. Deliberately
  separate from `/api/news-diagnostics` (pandas + `_bulk_close`, possibly networked)
  and from `/api/health` (which runs on every page load, while `runtime_status()`
  triggers the LightGBM/artifact load). `/api/health` carries only the cheap
  find_spec-based `env_ok` flag that drives the missing-dependency banner, plus
  `llm_ok` / `llm_error` for the News-read banner.
- `/api/news-sentiment?symbols=…&days=` — cache-only per-ticker reads (both engines)
- `/api/news-market?days=`         — cache-only market-wide News read + tape
- `/api/news-tape?symbols=…&days=` — cache-only merged article feed (lens, score, fact per headline)
- `/api/news-diagnostics`          — the Track record (`news_diagnostics.compute`) + `market_runtime` / `news_runtime`

**POST (also)**
- `/api/refresh-job`               — `{scope: "current"|"all", view?, entries?, phases?, days, context?, on_conflict?}` → `202 {job_id}` or **`409`** with the running job (§17)
- `/api/refresh-job/<id>/cancel`   — cancel; the `cancelled` frame is emitted synchronously
- `/api/news-rescore`              — `{symbols, days}` → cache-only re-aggregation onto a new window. Sub-50 ms, no network, no LLM, no ML inference. Deliberately **not** a job (§17)

**GET (also, v1.11.0)**
- `/api/history?symbol=…&range=…&bench=SPY,XLK` — intraday half of the chart granularity ladder (§5)
- `/api/refresh-job/current`       — `{job: snapshot|null}`; one cheap call on page load is what lets a job survive a full reload
- `/api/refresh-job/<id>/stream?since=<seq>` — replayable NDJSON progress (§17)

---

## 5. Frontend (`convexity/static/`)

Real static files served by `server.py` — `index.html`, `app.js`,
`style.css`. No frameworks, no build step. KaTeX is the only external
dependency (loaded from CDN, used only for column-guide formulas).

### State (`STATE`, `DATA`, `VIEWS`, `WATCHLISTS`)
- `DATA` — currently-rendered rows
- `VIEWS` — server-side view metadata (mirrors `/api/views` response)
- `WATCHLISTS` — `{name: entries_string}`
- `STATE.activeView` — current portfolio name, or `AD_HOC_KEY`
  (`"__current__"`) for the unsaved tab
- `STATE.mode` — `"equal" | "cap" | "custom" | "preset:<name>"`. `"custom"`
  is the legacy ad-hoc path (anonymous, Apply-without-Save). Named modes
  use the `preset:` prefix and resolve via `STATE.weightPresets`.
- `STATE.customWeights` — `{symbol: fraction}` after the user applies the
  weights popup without saving (ad-hoc Custom only)
- `STATE.weightPresets` — `[{name, weights, saved_at}]` for the active
  portfolio. Loaded by `loadPresetsForView()` when the tab changes; lives
  inside the view JSON on disk.
- `STATE.period` — `"1M" | "3M" | "6M" | "YTD" | "1Y" | "3Y" | "5Y" | "MAX"`
- `STATE.analyticsByTab` — `{tabName: {cacheKey: result}}` — keeps each
  tab's analytics warm so switching tabs is instant. Cache key is
  `${mode}|${period}|${fxQuote}` and `mode` may be `"preset:<name>"`.
- `STATE.fitColumns` — boolean toggle for the optional "Fit to screen"
  table mode. When enabled, the frontend scales column widths, font size,
  and chart cells down just enough to keep the active view inside the
  current table width. Preference lives in `localStorage.fit_columns`.
- `MPT` (separate top-level) — overlay state: `{result, selectedIdx,
  hoverIdx, view, runs, busy}`. `result` is the latest
  `/api/efficient-frontier` payload; `selectedIdx` is the frontier index
  controlled by the slider.
- `SORT` — `{key, dir}` for the row table

### Column registry (`COLS` + `BUILTIN_VIEWS`)
`COLS` (≈line 3714) is the single source of truth for every available
column — `key`, `label`, `w`, `align`, `sortable`, `render(r)`, optional
`heat`, `bg`, `sortValue`, `td_cls`. A `heat.kind === "yo_dyn"` column exposes
a per-view background mode in Customize: **Off / 2C-Quantile / Quantile /
Min-Max** (`getHeatMode`/`cellStyleHeat`). "Quantile" = single-blue quintile
buckets, "2C-Quantile" = orange↔blue diverging quintiles, "Min-Max" =
continuous 10th/90th-clipped blue. `favor:"low"` flips which end is best (e.g.
`analyst_rating`: 1 = Strong Buy reads blue). The old `"percentile"` mode +
`kind:"yo"` fixed-orange ramp were removed; a stored `"percentile"` migrates to
`"quantile"` on read. New modes must also be whitelisted in
`persistence._HEAT_MODES`. `BUILTIN_VIEWS` (just below COLS)
maps each preset (`Default`, `Fundamentals`, `Momentum`) to an ordered
list of column keys.

**The column bar is one flat row of chips (v1.11.1).** `renderColumnViewBar()`
emits the three built-ins, a `.cv-seg-div` hairline, then every custom preset
(`customViewNames()`, **creation order** so a new one lands at the end) as a
`.cv-custom-chip` in `var(--pos)`. The `Custom ▾` dropdown is gone; deleting a
preset lives in Settings → Column Presets (`deleteCustomView`), so the bar holds
no destructive control. The two green rules must stay **below**
`.cv-seg button.active` in `style.css` — the first is the same (0,2,1)
specificity and source order is what breaks the tie.

**Built-ins are edited in place and saved instantly**, so the amber pill is
purely informational and the two predicates deliberately differ:
`builtinIsModified()` (differs from factory — drives Settings' Revert, always)
vs `builtinShowsDirtyPill()` (that, minus an `acked` flag — drives the pill).
**Save = acknowledge**, not "write": `ackViewOverride` → `POST
/api/column-views/builtin-ack` only sets `acked`. Any later edit must re-arm the
pill, which is free in `upsert_column_view` (it rebuilds the entry) but has to be
done **by hand** in `set_builtin_view_heat` (it mutates one). An override
carrying only `acked` is dropped on read, preserving the
empty-override-disappears invariant. Reset/Revert is still
`DELETE /api/column-views/<name>`; `resetViewOverride(name)` takes an optional
name so Settings can revert a preset without switching to it.
**Wire these through arrow functions** — `onclick = resetViewOverride` passes
the MouseEvent as the `name` argument. Legacy names (`IB View`, `Trader View`) are still
accepted and normalised through the alias helpers so saved state migrates
without user intervention. `COLS_BY_KEY` is the lookup table; rendering goes
through `getActiveColumns()` which resolves the active view from
`STATE.activeViewName` / `STATE.customViews` / `STATE.activeColumnOverride`
(set when the user drags headers on a built-in preset — kept
in-memory until they Save-as-new or Reset). User-defined views persist
to `<data>/state/column_views.json` via `/api/column-views`.

**To add a new column**: append an entry to `COLS`, add a `COL_INFO`
tooltip, and (if it belongs in a preset) include its key in
`BUILTIN_VIEWS`. The XLSX export auto-discovers row-payload keys via
`HOLDINGS_PRIMARY_COLS` + the extras pass — add it to that list (or to
`HOLDINGS_SKIP_EXTRAS` if it's duplicated elsewhere, e.g. analyst
columns).

### Key UI behaviours added in Passes A/B/C
- **Smart primary button**: `#build` swaps label between "Build
  Dashboard" / "Update Portfolio" based on `primaryButtonMode()`.
  `runPrimary()` routes to the right handler; Cmd/Ctrl+Enter triggers it.
  **Cmd/Ctrl+Enter bypasses the disabled button**, so `build()` calls can
  overlap. `BUILD_GEN` means only the newest build writes `DATA`, saves and
  requests analytics; a superseded one cancels its reader and leaves
  progress/disabled state alone. `BUILD_STREAMING` keeps refresh-job row
  frames out while a build repopulates `DATA`. Both write through
  `upsertDataRow()`, never `push()`. Before this, overlapping writers left
  symbols in `DATA` twice and the build persisted them (§4 "One row per
  symbol").
- **Inline tab rename**: double-click a `.pf-tab-label` →
  `beginTabRename()` swaps the span for an input; Enter commits, Esc
  cancels, blur commits. Calls `/api/portfolio/rename`.
- **Loading chip** (`lc-anchor`, `lc-spin`, `lcHtml`, `lcShow`,
  `lcHide`): a small terminal-flavoured indicator. Braille spinner cycles
  ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ via CSS keyframes on `content`. Optional shimmer bar +
  tabular-numerics counter (e.g. `21·47`). Used in: streaming status,
  startup, analytics empty state, modal detail load, FX hover load,
  Excel export button. CSS in `style.css` (~line 313), JS helpers
  (`lcHtml`/`lcShow`/`lcHide`) in `app.js` (~line 591).
- **Export → Excel** (`exportXlsx`): client just hits
  `/api/export-xlsx`; server handles everything. Button shows the
  loading chip during the ~40s cold-cache export. The maintenance
  contract for "what goes in the file" lives both in the inline HTML
  comment next to the button AND in `xlsx_export.py`'s module docstring.
- **Topbar + column-bar hover copy**: actionable hover text on
  `Portfolio`, `Refresh`, `Export`, `Sort`, and `Fit to screen` uses the
  custom `data-tip` pseudo-element pattern, not native `title`, so the
  help text is consistently visible in-browser.
- **Settings overlay** (`#settings-btn` gear, `openSettings`/`closeSettings`):
  78vw × 76vh over a blurred backdrop; search box at the top of the sidebar,
  grouped nav below it, right pane titled + described. Follows the
  `openMptOverlay` idiom (the `document.body.style.overflow` lock +
  `dataset.*PrevOverflow` restore) and registers in the global Esc handler.
  See §16 for why Logs exists. Rebuilt in v1.10.1 — five load-bearing rules:
  1. **`SETTINGS_SECTIONS` is the only place a section is declared**
     (`{id, group, label, icon, description, keywords, items, render}`).
     `renderSettingsNav()` rebuilds the nav from it. Add a section there, never
     back in `index.html`. Sections today: General, Models & Data, Logs, About.
  2. **The search `<input>` stays static in `index.html`.** Only
     `#settings-nav-list` is re-rendered; an input inside that subtree would be
     destroyed mid-keystroke, blurring the field. For the same reason typing
     re-renders the **nav only** — re-rendering the pane per keystroke would
     tear down `#log-body` and restart the log poller ~10×/second.
  3. **`renderSettingsPane()` calls `stopLogPolling()` unconditionally as its
     first statement**; only `renderSettingsLogs` restarts it. A new section can
     therefore never leak a 1.5s timer against `/api/logs`.
  4. **Nav clicks are delegated** on `#settings-nav-list` — per-node handlers
     would be bound to detached elements after the first search.
  5. **Esc**: the search field clears a non-empty query and stops there; on an
     empty query it must **bubble** to the global handler (`app.js` ~line 1999)
     which closes seven overlays in one pass. Never `preventDefault` it.
  Mirrored controls (theme, Fit to screen) follow a strict
  **single-mutator / single-painter** rule: `setTheme()` and
  `toggleFitColumns()` remain the only mutators; `syncThemeControls()` /
  `syncFitControls()` paint *both* the topbar and the settings widgets. The
  settings controls must never write `STATE.fitColumns` or the theme directly.
  **Gotcha:** the theme segmented control uses `data-theme-opt`, NOT
  `data-theme` — `style.css` themes via unscoped `[data-theme="dark"]` /
  `[data-theme="bloomberg"]` attribute selectors, so a button carrying
  `data-theme="bloomberg"` silently adopts the whole Bloomberg palette while
  sitting inside a dark-theme page. Verified in all three themes.
- **Topbar single-class overrides lose the cascade.** `.topbar button` is
  (0,1,1) and sets `font-size: 12.5px`; a bare `.gear-btn` / `.info-btn` rule is
  (0,1,0) and is silently ignored. The gear's declared `font-size: 15px` never
  applied for that reason and it rendered at 12.5px until v1.10.1, which added
  `.topbar button.gear-btn { font-size: 20px }`. Measure computed styles in the
  browser rather than trusting a declaration.
  **This has now bitten twice — assume it, don't rediscover it.** The refresh
  status chip's cancel button was the second case (fixed v1.11.1): `.rf-chip-x`
  (0,1,0) lost *every* declaration to `.topbar button`, including its own
  `border: none; background: transparent`, so it rendered as a bordered 30px
  `--r-md` square with the canvas background inside a 999px pill. It is now
  `.topbar .rf-chip-x` (0,2,0), draws an **inline SVG** mark rather than a `×`
  glyph (whose size and baseline vary by system font), and hovers to
  `rgba(var(--neg-rgb), 0.16)` instead of a solid red fill. Any new control
  placed inside the topbar needs the `.topbar` prefix on its rules.
  A cheap way to iterate on one of these without touching the app: build a
  standalone lab page that inlines the real `:root`/`[data-theme]` variable
  blocks plus the competing `.topbar button` rule, render the candidates side by
  side in all three themes at 1× and 3×, and open it in the browser pane. Write
  the losing/shipping variant at its **real** specificity or the lab will
  "fix" the bug for you and prove nothing.
- **Terminology (v1.12): the engines are the "News read" and the "Market
  read"** in all UI copy. Engine names (LLM, statistical model) appear only in
  the Methodology and Settings → Models & Data. The middle News tier is
  labelled "Mixed" (`neutral` in data), the middle Market tier "No edge"
  (`no_edge`). The shared vocabulary (`NS_COLORS` from the theme's `--ns-*`
  variables, `NS_LABELS`, `NS_LENSES`, `nsTierDot`, `nsPill`, `nsScale`) sits
  in one block near the top of `app.js`; the `--ns-*` colours are defined per
  theme in `style.css` and must stay in all three.
- **NS column + hover card.** `nsDot(s, sym)` renders two dots, **Market read
  first, then News read**; faded = stale, hollow = no read. There is no
  per-dot tooltip: one delegated handler opens a single `.ns-hc-tip` card
  (`nsHoverCardHtml`, positioned by `placeTip`) with a lens row per lens (score
  cells + the fact, "No news" when empty), two-pass agreement, the Market
  tiles (expected move, percentile + what it is ranked against, tier), the
  divergence sentence, the stale notice and both ages. Escape and scroll hide it.
- **Timeline and Flash Tape show only headlines the News read read and found
  to be about a holding** (`nsReadAbout`). Unread headlines (outside the 15 per
  ticker) and "not about it" ones are half the feed on a mega-cap; the tape's
  "Not about it" chip still shows the latter. One dot per headline (the News
  score); tier chips filter on it, lens chips replace the old event chips.
- **Flash Tape full-screen** (`openTapeFullscreen`, 99vw × 99vh): clicking the
  `#ns-tape-card` body opens it; `e.target.closest("a, select, button, input,
  label")` guards the links and filter controls. `renderNsTape({fullscreen})`
  is ONE function serving both surfaces — the inline panel keeps its 120-row cap
  and single-line ellipsised headlines, the overlay lifts the cap to 1000 and
  adds a numeric score column plus a 2-line-clamped summary. Filter changes in
  either view re-render the other so the two never diverge.
- **Track record and Methodology.** "Model Diagnostics" is now the Track
  record (`toggleTrackRecord` / `renderTrackRecord`): two verdict cards, a
  long-short chart (`svgLine`), per-lens hit rates, consistency, and a
  collapsed "For quants" block — all from `/api/news-diagnostics`. The
  Methodology modal (`openMethodology`) is two side-by-side explainers with
  frozen validation numbers and KaTeX formulas in collapsed `<details>`; live
  numbers belong in the Track record, not there.

- **Overlays: `showOverlay()` / `hideOverlay()` are the ONLY way to open and
  close one** (v1.11.0). They own `lockBodyScroll`/`unlockBodyScroll`, a single
  **nesting counter** on `document.body`'s overflow. Never write
  `body.style.overflow`, and never toggle the `show` class by hand — both are
  enforced by `tests/test_frontend_overlays.py`, which also asserts the overlay
  inventory in that test matches every `id="*-bg"` in `index.html`, so a new
  backdrop can't skip the contract.
  Why a counter: overlays nest (About-MPT over MPT, the inline prompt over the
  weights popup) and the global Escape handler (`app.js` ~line 2087) closes
  seven of them in one **unconditional** pass. The old design had three
  overlays each stashing `dataset.<name>PrevOverflow` and the other nine
  locking nothing at all — so the detail modal scroll-chained to the page, and
  a nested pair could restore each other's value. Both helpers no-op when the
  overlay is already in the requested state, which is what makes the
  seven-closer pass safe.
  Second half of the fix is CSS: the grouped `overscroll-behavior: contain`
  rule in `style.css` (just above `/* ===== Detail modal ===== */`). **Every
  new overlay scroll container goes in that list.** Both halves are needed —
  the body lock alone still lets the trackpad's elastic bounce chain out, and
  containment alone does nothing for a gesture starting on the backdrop.
  Scroll *position* needs no saving: `overflow:hidden` on `<body>` freezes the
  viewport where it is (it propagates because `html` is `overflow: visible`).
  The `padding-right` compensation replaces the scrollbar's width, without
  which the page visibly jumps ~15px wider the instant anything opens.

- **Detail-modal header shows the ACTIVE RANGE's return**, not `pct_1d`
  (v1.11.0). `modalPriceBlockHtml()` / `renderModalPriceBlock()` own
  `#m-price-block`; a drag in progress (the brush's `onUpdate(sel)`) wins over
  the range tab, and the `.p-range` badge names whichever period is being reported. It
  also reads price from `DETAIL.data` once the detail payload lands — the
  skeleton is never re-rendered after the fetch resolves, so the old code kept
  the table row's cached price for the modal's whole lifetime. It computes from
  **`activeChartSeries().full`**, not `d.history`: on an intraday range those
  two disagree about where the window starts, and a header contradicting the
  summary line right beneath it is worse than no header.

- **Chart granularity ladder** (v1.11.0). Everything was hardcoded
  `interval="1d"`, so 1M was ~21 points on an 800px-wide chart.

  | Range | Source | Fetch | Displayed |
  |---|---|---|---|
  | 1M | `GET /api/history` | `30m` / `60d` | last 1M (~280 of 780 bars) |
  | 3M | `GET /api/history` | `1h` / `1y` | last 3M (~435 of 1749) |
  | 6M | *same cached payload as 3M* | — | last 6M (~870) |
  | YTD · 1Y · 5Y · MAX | the daily `period="max"` payload from `/api/detail` | — | client slice, thinned to ≤1500 |

  **The fetch window is deliberately far wider than the display window, and
  that is load-bearing, not waste** — an SMA 200 over 30m bars needs 200 bars of
  warm-up, so computing on exactly the visible window leaves the line empty on
  every short range. It also means a symbol costs at most **two** intraday
  fetches no matter how the user tabs around. `fetcher._RANGE_INTRADAY` is the
  table; `fetcher._INTERVAL_MAX_DAYS` holds Yahoo's own caps (`1m ≤ 7d`,
  `2–90m ≤ 60d`, `1h ≤ 730d`). Exceeding a cap returns an **empty frame, not an
  error**, which is why `_period_days()` returns 10 000 for anything it can't
  parse — guessing small would let an over-cap request through and blank the
  chart instead of falling back. `range_history()` returns `fallback: true` for
  the daily ranges, for a cap violation, and for listings with no intraday data;
  the client then just draws the daily series and the legend says "daily".
  Benchmarks are fetched at the **same interval** (the client passes `bench=`,
  since it already knows the sector ETF) or the overlay renders as a staircase
  against a smooth line. Modal open stays instant: intraday is fetched lazily
  only when 1M/3M/6M is picked, with a loading chip on the range tab.

- **Both charts share their interaction code**: `attachChartHover()`
  (crosshair + tooltip) and `attachRangeBrush()` (drag-to-measure), each fed
  `lines: [{label, pts, dot}]` with `lines[0]` the main series. The measure is
  **TradingView-style and transient** (the user's call): while the button
  is held a band and a floating `.chart-measure` badge show every line's return
  over the span; both vanish on release. There is deliberately no persisted
  selection state, no Escape handler and nothing to `destroy()` — every
  listener sits on the chart's own overlay element, rebuilt each render. (The
  earlier persistent design needed an AbortController, a capture-phase Escape
  handler with a `SCROLL_LOCK` guard, and teardown in `closeModal()`; all of
  that went with it.) The `.sel`/`.pf-sel` rect carries its own `y`/`height` in
  the markup. `setPointerCapture` stays in try/catch — it throws for a dead
  pointer id.

- **Benchmark picker.** "vs <select>" in the Risk & Return header is a native
  `<select>` (`benchSelectHtml`) — keyboard/Esc/outside-click for free, no
  popover code. `STATE.bench` (localStorage `pf_bench`) drives the "/ x"
  column, Beta/R²/TE (the portfolio's `rel` against it) and the chart's purple
  comparison line; `activeBench(a)` falls back to SPY. The Nasdaq / Sector-mix
  pills add extra lines (`pfBenchLines`, deduped), and their period returns
  are listed *under* Risk & Return, not in the chart legend. The first overlay
  pill (`#pf-show-bench`) is relabelled with the chosen benchmark on render.

- **`thinPoints(pts, maxN)` must pin BOTH endpoints.** A 45-year MAX window is
  ~11.5k daily closes. Last-in-bucket downsampling naturally starts at index
  `ceil(step)-1`, which silently moved AAPL's MAX start date forward ~7 trading
  days and changed the reported return from +339417% to +316048%. The window the
  header and chart report must be the window the user asked for.

- **SMAs (20/50/200)** are off by default and share **one object**, `CHART_SMA`
  — `DETAIL.sma` and `STATE.pfSma` are both that same reference, persisted to
  `localStorage.chart_sma`. A shared *key* was not enough: two independent
  copies read at different times silently clobbered each other (enable SMA 200
  in the modal, then click a portfolio pill, and the pill's stale page-load
  copy was written back over it). Toggling from the modal calls
  `syncSmaPills()` so the pills' `data-on` repaints too.
  `smaSeries()` takes the **full** series and the caller slices the result —
  never the other way round. Legends name the bar frequency ("SMA 50 · 30m
  bars"), since the same period means something very different on 30m vs daily
  bars. The portfolio chart's SMAs are computed **server-side** (`series.sma`,
  see §4 "One wide fetch") since the client only ever has the period slice.
  At the true start of a series `smaSeries` averages what exists so far.
  `SMA_COLORS` in `app.js` and `.swatch.sma*` in `style.css` must stay in sync.
- **Fit to screen toggle** (`#cv-fit-toggle`): optional table compaction
  mode for dense presets. `applyTableFitMode()` computes a scale from the
  active columns' declared widths versus `.table-wrap` width and applies
  it through the `--table-scale` CSS variable. The active view should
  remain readable, but the explicit goal is "keep the current preset on
  screen before falling back to horizontal overflow."

- **The limits panel PUSHES the workspace down; it never takes height from it.**
  `.pf-mpt-scroll` (wrapping `.pf-mpt-bounds` + `.pf-mpt-body`) is the single
  scroll column under the controls strip. `mptPinBodyHeight(true)` — called
  **before** `panel.hidden` flips, or it measures the already-pushed height —
  freezes `.pf-mpt-body` at its current height via `--mpt-body-h`, which the CSS
  reads as its `min-height`; `flex-shrink:0` is what makes the body refuse to
  yield. The column then overflows by exactly the panel's height, so the chart,
  slider and legend keep their **exact geometry** and simply move below the fold.
  Measured across an open/close cycle: backing store, CSS box, host height and
  `MPT._proj` all byte-identical; only `scrollHeight` changes.
  Closing must still call `mptRender()` — releasing the pin is a no-op unless the
  window was resized while the panel was up, in which case the body lands at a new
  height and the chart has to be re-measured (verified: 531px pinned during a
  860→1180px resize, correctly re-rendered at 851px on close).
- **The canvases CROP, they never rescale** — the second line of defence, and
  still load-bearing. They used to be `inset:0; width:100%; height:100%`, but
  their backing stores are only resized inside `mptSizeCanvases()`. The limits
  panel used to be a flow sibling in the `flex-column` modal, stole ~450px from
  `.pf-mpt-chart`, and nothing re-measured — so the browser rescaled a stale
  bitmap and the entire plot visibly squashed; `MPT._proj` also kept the old
  `cssH`, so hover/click hit-testing silently drifted off the frontier.
  The contract: `mptSizeCanvases` publishes the height it actually drew at
  to `--mpt-chart-h`, the canvases read **that** rather than `100%`, and
  `.pf-mpt-chart` is `min-height:0; overflow:hidden`. A shorter parent therefore
  clips the canvas instead of stretching it, and it returns intact.
  While the panel is open the height is **locked** (`MPT._chartH` +
  `mptBoundsPanelOpen()`) so a reflow of the obstructed box (a window resize)
  cannot re-measure it.
  **`mptSettleChartHeight()` must be called after anything that paints the side
  panel** — `mptRenderSide()` writes the legend *below* the chart, shrinking the
  host ~22px after `mptRenderChart()` already locked it, which left the x-axis
  label (drawn at `cssH − 6`) clipped. It is called from `mptRender()` **and**
  from the streaming `done` handler, which finalises via
  `mptRenderChart({fixedProj})` and so never reaches `mptRender()` — that is the
  path every completed run takes, so missing it there fixes nothing.
  **Do not "improve" this with a ResizeObserver.** RO delivery is tied to the
  frame lifecycle and is throttled or dropped outright in a backgrounded window
  — measured here as *zero* callbacks for a real 1185→735px change — so the
  correction would fail exactly when the user tabs away and back. The settle
  pass is synchronous and deterministic instead.

- **`textOnHeat` measures contrast; it does not guess a threshold**.
  It used to flip to white above a fixed `|t|` (0.55 light / 0.65 dark). That was
  wrong: on the light theme's green ramp `--text` beats white at *every*
  saturation (4.14:1 vs 3.82:1 even at full tint), so the rule went white
  precisely where dark text was still winning 6-8:1 and a mid-range cell (a +20%
  upside) rendered white-on-light-green at **2.2:1**. It now reproduces the
  background `colorDiverging` will paint and keeps whichever of `--text` / white
  / near-black actually measures best (`relLuminance` + `contrastRatio`, ~10
  float ops per cell). Near-black is a candidate because a saturated tint on
  dark/bloomberg is a *bright* green/red where both white and the near-white
  `--text` fail — the same problem `--on-accent` solves with `#050505` (§8).
  Two consequences: **`THEME_COLORS` now carries a `text` triple per theme and
  it must stay in sync with `--text` in style.css**, and the `0.9` mix factor is
  duplicated from `colorDiverging` — change one, change both. Measured floor
  across the analyst table went 2.2 → 5.28 (light) / 4.7 (dark) / 4.04 (bbg).

- **Per-position limits grid is a spreadsheet, not a form.** `.pf-mpt-bnd`
  inputs are borderless/transparent with the spinners suppressed; `:focus` draws
  an inset accent outline (Excel active-cell) and `.pf-mpt-bnd-row:focus-within`
  tints the row — `:focus-within` so keyboard tabbing highlights without any JS.
  `:not(:placeholder-shown)` colours a *set* constraint in `--accent`, which is
  why the `0` / `100` placeholders are load-bearing, not decoration. Rows carry
  `logoImg(sym)` + the company name pulled from `DATA`. Values and their column
  headers are both centred, and `align-self:stretch` makes the input fill the
  row's **full** height (the row is `align-items:center`, which otherwise leaves a
  dead strip above and below) — verified by hit-testing a 96×23 cell on a 6×4 grid,
  96/96 points resolve to the input, so a click anywhere in the column lands in the
  number.
  Focusing a cell **selects its value** (`focusin` on the grid + a rAF-deferred
  `select()`), so typing over `25` yields `4`, not `254`. Three non-obvious bits:
  `focusin` rather than `click` covers keyboard Tab and does not re-fire inside an
  already-focused cell (which would wipe a deliberate caret placement mid-edit);
  the rAF is required because the browser sets the caret from the click position
  *after* focus and would undo a synchronous `select()`; and on `type="number"`
  **`selectionStart` reads `null`** — that is an API limitation, not a failure, so
  assert the behaviour by typing over the value, not by reading the selection.
  **It is the only box left in the overlay, and that is deliberate** — a
  transient editor dropped on the tool should read as a distinct sheet, whereas
  `.pf-mpt-chartwrap` / `.pf-mpt-side` are the workspace itself and are now
  boxless (transparent, no border; the side panel keeps a single hairline
  `border-left` as a gutter rule). Padding on `.pf-mpt-bounds` is symmetric so
  the head and the last row sit the same distance from the frame.
  **Do NOT wire `mptWireAssetTips` to this grid, and do not give the rows
  `data-mpt-sym`.** That tooltip fires on `mousemove` and each event rebuilds the
  tip's `innerHTML` and calls `placeTip()` (a forced synchronous layout), i.e. a
  parse + reflow per frame while the pointer rests over the list. The stats it
  showed belong to the chart anyway.
  **The grid is not a scroller.** It used to have its own `max-height` *and*
  `overscroll-behavior: contain`, which is what made scrolling feel broken: a
  short inner scroller hits its end after ~100px and then **stops dead** instead
  of chaining to the parent, so getting down a 20-name book took a stack of
  separate gestures. One surface (`.pf-mpt-scroll`), one gesture. `.pf-mpt-bnd-head`
  is correspondingly non-sticky — `.pf-mpt-bounds` is `overflow:hidden`, so it is
  the sticky containing block and never scrolls; sticky there would be inert
  anyway, just with a promoted layer for nothing.

- **Analyst consensus table** (`renderAnalystDashboard`): Weight uses the blue
  quintile ramp, and Upside **and Upside (median)** share the diverging ramp
  anchored at 30 — all via `cellStyleHeat` with a synthetic column literal, so
  they read identically to the main grid's heat columns. The two upside columns
  must keep the **same ramp and the same anchor**: they are the same quantity on
  the same scale, and reading them as a pair is the point of the median column —
  a visibly weaker median tint means the mean is being dragged up by one high
  outlier. `setTheme()` re-renders this table, because
  `render()` only rebuilds the main grid and the tints are baked into inline
  styles at build time. **There is no true q25/q75 of analyst targets** — Yahoo
  publishes only low/mean/median/high and no per-analyst data exists in the feed
  — so nothing quartile-shaped is fabricated. The median and the low/high band
  are reported **as upside, not as target price** (`Upside (median)` and
  `Upside range`, both immediately right of `Upside`): upside is the decision
  variable — a target of 768 means nothing until you know the price it is
  measured against — and three upside figures side by side make the median-vs-mean
  skew and the width of the band readable in one scan. Raw target prices stay in
  the hover. `Upside range` sorts on `(high−low)/mean`, the same dispersion
  `frontier.py` uses for BL view confidence. `quickAnalystPreview()` nulls the
  trio (the streaming row payload has only the mean) so the optimistic paint
  shows `—` and fills in. **Do not add them to `fetch_one`** — that is the hot
  loop; they come from `analytics._analyst_for`.
- **`_analyst_for`'s target trio needs its retry** (`analytics.py`). It runs on 8
  pool threads, and Yahoo answers a burst of `.info` calls by handing some of them
  an **empty dict** rather than an error — a throttle, not "this name publishes no
  range". Verified directly: names that came back thin returned a full
  low/median/high on a sequential call moments later. Without the retry the
  biggest holdings rendered `—` in exactly the two columns above, and the 300 s
  negative cache meant the next refresh usually failed the same way. Two jittered
  escalating retries, **only** on the all-None path: one is not enough, because
  the retries themselves collide. Live coverage went 10/15 → 15/15.

---

## 6. `symbol_db.py` + `build_symbol_db.py`

### Why
Map fuzzy human input (`"microsoft"`, `"Microsft"`, `"DaVita"`) to a
provider ticker without round-tripping every typo to upstream search.
~20× faster than `yf.Search` for the common case (~25ms vs ~500ms).

### Schema (SQLite)
```
CREATE TABLE symbols (
    provider TEXT, ticker TEXT, name TEXT,
    exchange TEXT, country TEXT, instrument_type TEXT,
    name_norm TEXT,   -- folded for fuzzy match (lowercase, alnum)
    source TEXT,
    updated_at TEXT,
    PRIMARY KEY (provider, ticker)
);
```

`provider` is in the PK so the same SQLite can hold multiple providers
(yfinance today, future Refinitiv/IEX/Polygon mappings tomorrow).
**Provider-agnostic on purpose.**

### Lookup ranking
`lookup(query, min_score=72, limit=1)` runs in this order:
1. Exact ticker match (case-insensitive) → 100
2. Exact name_norm match → 100
3. rapidfuzz WRatio over name_norm with low cutoff (60), then **composite
   re-rank** that rewards exact / prefix / substring matches with small
   length deltas. This is why `"microsoft"` → `MSFT` (not `Smith Micro
   Software`).
4. Substring scan fallback when rapidfuzz isn't installed.

### Builder (`build_symbol_db.py`)
```bash
python build_symbol_db.py                # all sources
python build_symbol_db.py --sources nasdaq
python build_symbol_db.py --db /tmp/syms.sqlite
```

### Shipped sources (US-focused today)
- **NASDAQ Trader** — `nasdaqlisted.txt` + `otherlisted.txt`. ~12k rows.
- **SEC company_tickers_exchange.json** — ~10k rows.
- After dedup: ~15.9k unique tickers.

### Adding a new source
1. Write `def source_xxx(session) -> Iterable[SymbolRow]:` in
   `symbol_db.py`.
2. Register it in the `SOURCES` dict at the bottom.
3. Re-run `python build_symbol_db.py`.

Good targets when expanding coverage:
- LSE listings (lseg.com publishes a public CSV)
- XETRA / Deutsche Börse
- TSX, ASX, JPX, HKEX — most exchanges publish issuer lists
- OpenFIGI for ISIN-keyed mapping (free tier, requires API key)
- Wikipedia constituent lists for index members

### Wiring
`resolver._symbol_db_lookup()` is the bridge. Both `symbol_db.py`
and `symbol_db.sqlite` (in the data folder since v1.14; `symbol_db.db_path()`)
are OPTIONAL — if either is missing, the dashboard
still works, the lookup just gracefully returns `None` and the existing
`yf.Search` fallback runs.

---

## 7. `xlsx_export.py`

### Design contract (mirror in app.js too)
**"Everything you can see in the app, in one file."** Each saved
portfolio → one sheet. When new columns / analytics / fields land in the
dashboard, extend `xlsx_export.py` so the export stays comprehensive.

Two places have the contract documented; keep them in sync:
1. Inline HTML comment next to the topbar `#export` button
   (`convexity/static/app.js`)
2. Module docstring at the top of `xlsx_export.py`

### Per-sheet structure
1. **Overview sheet** (first) — every portfolio with headline metrics
   side-by-side.
2. **One sheet per portfolio**:
   - Meta block (name, constituents, cached-at, holdings count)
   - **Portfolio Metrics** — 4-column table Portfolio / SPY / NASDAQ. The
     Portfolio column honors the view's saved **active weight preset**
     (`_resolve_active_weights`, projected onto row symbols + renormalized), and
     the section header names the actual weighting (e.g. "Portfolio Metrics
     (Tilt, 1Y)"); it falls back to cap-weight only when the view has no active
     preset. (Was previously hardcoded cap-weight regardless of saved weights.)
   - **Analyst Coverage** — mean rating, target upside, distribution
   - **Sector Exposure**
   - **Concentration** — top-5, Herfindahl, effective N
   - **Holdings table** — every row field + per-symbol analyst columns

### Performance contract
- Analytics is run per-portfolio sequentially (the inner yfinance bulk
  download is what's slow — it has its own threadpool).
- When `analytics["analyst"]["holdings"]` covers a symbol, we DO NOT
  hit `Ticker.info` again. Only uncovered symbols hit
  `gather_analyst_info` in parallel.
- Cold cache: ~40s for 7 portfolios / ~150 unique symbols.
- Warm cache: ~5s (analytics + analyst caches hit).

### Adding a new column to the holdings table
- If it's a row-payload key, it appears automatically via the "extras"
  pass (any key not in `HOLDINGS_PRIMARY_COLS` or `HOLDINGS_SKIP_EXTRAS`
  gets appended).
- If it's an analyst column, add a `(key, label)` tuple to `ANALYST_COLS`
  and a mapping branch in `_write_portfolio_sheet`'s analyst loop.

---

## 8. CSS / theming patterns

- CSS variables (`--accent`, `--bg-canvas`, `--text`, `--muted`,
  `--border`, `--pos`, `--neg`) defined in `:root` and overridden under
  `[data-theme="dark"]`.
- **Corner-radius scale** (`--r-lg` / `--r-md` / `--r-sm` / `--r-xs` in the base
  `:root`, currently the "Sharp" 6/4/2/1 px tier). Every non-circular
  `border-radius` reads a token, so app-wide roundness tunes from these four
  values alone; pills/circles (`999px` / `50%`) stay literal on purpose.
- **Three themes: `light`, `dark`, `bloomberg`** (a Bloomberg-terminal
  palette added in v1.5.3). Because the whole app is variable-driven, a
  theme is a `[data-theme="…"]` block plus a matching `THEME_COLORS.<name>`
  RGB-triplet entry in `app.js` (the latter feeds the JS-computed
  heatmap/spark/RS-bar/delta-bar colours). To add a fourth theme, copy those
  two blocks. **Bloomberg's colour hierarchy is the point — don't flatten
  it**: `--text` is WHITE (data values), `--muted` is AMBER (labels/headers/
  secondary), `--hover` is the terminal's dark selection blue, borders are
  neutral gray. The first cut made body text amber too and the user rejected
  it ("everything is the same color"). A short fidelity-override block right
  under the variable block additionally paints table `th`, ticker `.sym`
  cells, and the `#tickers` textarea amber ("amber = editable" is the
  terminal's own convention). `--on-accent` is
  the text/thumb colour placed *on* an `--accent` fill (white in light/dark,
  near-black in Bloomberg so text stays legible on the bright orange); any
  new accent-filled control must use `color: var(--on-accent)`, never a
  hardcoded `#fff`. Two JS branches that ask "is this a dark canvas?" use the
  `isDarkTheme(t)` helper (true for `dark` **and** `bloomberg`) rather than
  `=== "dark"`.
- **Theme switch interaction** (`setupThemeSwitch` in `app.js`): a short
  click toggles light↔dark (Bloomberg counts as non-light, so a click exits
  it to light); a **long-press (≥500ms)** on the switch activates the hidden
  Bloomberg theme (pointer events cover mouse+touch; the terminating click is
  swallowed via a `longFired` flag). Choice persists in `localStorage.theme`
  and is restored by `readTheme()` (which accepts all of `THEME_NAMES`).
- Tooltips use TWO patterns:
  - **Pseudo-element `::after`** on `[data-tip]` — fast, declarative.
    Used for header tooltips and overlay-pill info icons.
  - **JS-rendered `.pf-metric-tip` div** — heavier but supports rich
    content (LaTeX formulas, etc.). Used for analytics metric labels.
- Hover-tooltip cursor is `cursor: default` — explicitly NOT `cursor:
  help`. The user dislikes the question-mark cursor.

---

## 9. Conventions

### Code style
- **Modular by concern** — the backend lives in `convexity/` as
  one module per concern (server, fetcher, analytics, fx, persistence,
  resolver, cache, mpt, frontier, xlsx_export, finnhub_adapter,
  news_sentiment, symbol_db, desktop). Put new logic in the module it
  belongs to; only add a new module when the code is genuinely a new
  concern. Don't fragment further for its own sake — e.g. `fetch_one`'s
  helpers stay in `fetcher.py`, not split one-function-per-file.
- **Top-of-block docstrings** that explain WHY, not WHAT. The user
  reads them; they're part of the deliverable.
- **Comments around tricky behaviour** — every non-obvious gate or
  retry has a comment explaining the failure mode it's defending against
  (see FX hover cache, sequential FX pair fallback, view rename rollback).
- **Performance-aware code paths** are explicitly labelled (e.g.
  "fast streaming path" vs "heavy detail path"). When in doubt, ask:
  "could this end up in the per-row hot loop?"

### Workflow
- After non-trivial edits: `python -c "import ast; ast.parse(open(f).read())"`
  on every changed `.py` file (or run `scripts/check_syntax.py`).
- After backend edits that change behaviour: restart the server you started
  (`kill <its pid>`, then `CONVEXITY_HOME=<tmp> uv run convexity &`). §3
  gotcha 3: never pkill the user's installed app.
- Validate via `curl` against the actual endpoint when possible —
  faster than asking the user to refresh.
- Use `Bash`'s `run_in_background=true` for the dashboard process;
  capture stdout to `/tmp/dashlog.txt`.

### Naming
- Frontend JS: camelCase (`renderTabs`, `requestAnalytics`).
- Python: snake_case (`fetch_one`, `resolve_symbol`).
- HTML IDs: kebab-case (`#pf-analytics-body`, `#fx-hover-foot`).
- CSS classes: kebab-case with semantic prefixes (`.pf-tab`, `.lc-anchor`,
  `.fx-btn`).

### Don'ts
- **Don't add CDN dependencies** other than the already-loaded KaTeX
  unless you've checked first. The app's selling point is "no network
  except yfinance."
- **Don't break the fast streaming row path** with heavy fetches —
  measure latency impact when extending `fetch_one`.
- **Don't silently cache empty/failed results forever** — every cache
  needs a TTL or a "only cache non-empty" gate (FX hover learned this
  the hard way).
- **Don't add emojis to code/docs unless the user explicitly asks.**

---

## 10. Open work + design intent for upcoming passes

### Open work
Tracked as GitHub Issues on `ithakis/Convexity` — label `feature` (planned) or
`idea` (not yet scoped): `gh issue list --label feature` / `--label idea`. The
old root wishlist file was retired in v1.13.1; file new wishlist items as
issues, never back into a file. Distribution/packaging work follows
`docs/plans/distribution-roadmap.md`.

### Done / archived (don't redo)
- News v2 (v1.12) — two peer engines (News read with five lenses, Market read
  recalibrated and live-anchored), Track record, Methodology rewrite
- News & Sentiment — Finnhub news + NVIDIA NIM AI sentiment, NS column, News tab, disk-backed cache, rate limiter
- ★ Save Watchlist button removal
- CSV → Excel export (Pass B)
- FX hover stale-cache fix
- Robust ticker input (Pass C) — colon syntax, prefix reversal, fuzzy DB
- Tab rename via double-click
- Loading chip system
- Square icon removal from Portfolio button
- NASDAQ blurb removal
- Desktop app (PySide6 + QtWebEngine) — see §14
- Pass D — column-view registry (`COLS`/`COLS_BY_KEY`/`BUILTIN_VIEWS`), custom
  presets via `.convexity_column_views.json`, Default/Fundamentals/
  Momentum built-ins

---

## 11. Quick reference — current line landmarks

The frontend (HTML/CSS/JS) is embedded in `convexity/static/`, not
in `dashboard.py` (that file is now an 11-line backward-compat shim — see
§1/§2). Landmarks below are within `convexity/static/app.js` unless
noted otherwise. (Approximate. Use `grep -n` to confirm before editing.)

| What | File | Where |
|---|---|---|
| Process-global cache dicts | `cache.py` | ~14–16 |
| Views/weight-presets persistence | `persistence.py` | `save_view` ~109, `save_mpt_run` ~501 |
| Watchlists persistence | `persistence.py` | `load_watchlists` ~379 |
| Google-colon normaliser | `resolver.py` | `_normalize_google_colon` ~153 |
| `resolve_symbol` pipeline | `resolver.py` | ~164 |
| `fetch_one` (per-symbol row) | `fetcher.py` | ~117 |
| `fetch_detail` (modal payload) | `fetcher.py` | ~398 |
| FX layer (spot rates, basket index) | `fx.py` | `fx_rates` ~139, `fx_index_history` ~198 |
| Analytics (`analyze_portfolios_multi`) | `analytics.py` | ~214 |
| `compute_efficient_frontier` | `frontier.py` | ~72 |
| HTTP `Handler` (GET/POST/DELETE) | `server.py` | ~92 |
| `_pick_port` | `server.py` | ~689 |
| `main()` (browser-mode entry) | `server.py` | ~739 |
| Topbar HTML | `static/index.html` | ~13 |
| Loading-chip CSS (`.lc-*`, spinner keyframes) | `static/style.css` | ~313–332 |
| Tab rendering + rename | `static/app.js` | `renderTabs` ~2768, `beginTabRename` ~2814 |
| `exportXlsx` | `static/app.js` | ~3032 |
| Analytics request / render | `static/app.js` | `requestAnalytics` ~3108 |
| Mode pill bar (`renderModeBar`) | `static/app.js` | ~4454 |
| `runPrimary` (Build/Update button router) | `static/app.js` | ~4571 |

Use `grep -n "<symbol>" convexity/*.py convexity/static/*.{js,html,css}`
to relocate anything not listed above — the package is small enough that
this is faster than trusting a stale line table.

---

## 12. `mpt.py` — Black-Litterman + mean-CVaR optimizer

Standalone module (dependency-light, testable in isolation) implementing the two
engines behind the Optimize tab. Used by `compute_efficient_frontier` in
`convexity/frontier.py`. **No scipy on the hot path** — the LP solver is a
custom numba interior-point method; scipy/HiGHS lives only in
`tests/_cvar_reference.py` and certifies the fast solver to 1e-6 in CI.

The Markowitz mean-variance path (CLA, tangency, vol/return Monte-Carlo cloud,
`annualize`) was fully removed in v1.8.0 — do not resurrect it.

### Public API

```python
mpt.compute_returns(closes_df, freq="daily") -> returns_df
mpt.annualized_cov(returns_df, freq, model) -> cov_df           # sample / ledoit / ewma
mpt.ledoit_wolf_shrink(cov, returns=None) -> cov_df             # toward constant-corr target
mpt.black_litterman(symbols, cov, mkt_weights, views, *, rf, tau, haircut, ...) -> dict
    # -> {mu, prior, q, delta, tau, haircut, viewed, no_view}  (μ = total annual return)
mpt.mean_cvar_frontier(returns_df, mu, *, alpha, w_min, w_max, fully_invested, rf,
                       n_points, cov) -> {ok, frontier, min_cvar, max_ret, n_nonconv, _ctx}
    # w_min/w_max are scalar OR per-asset vectors (per-position box); _as_bound_vec
    # coerces. each point: {ret, cvar (√252-annualized, ORDERING only), var30, cvar30
    #   (10d→30d display loss), vol, mdd, cdar, weights, _t}
    # _ctx = {a_ret,l,h,kappa,fi,targets} — reused by bootstrap_cvar (strip before JSON)
mpt.bootstrap_cvar(R, ctx, seeds) -> cvar_ann[B,K]   # parallel (prange) frontier-stability
    # band: each seed resamples the T scenarios w/ replacement + re-solves at every target
mpt.portfolio_risk_metrics(weights, mu, cov, returns, alpha, rf, fully_invested) -> {...}
mpt.cvar_return_cloud(returns_df, mu, alpha, n, seed, w_min, w_max, fully_invested, rf)
    # -> [[cvar_30day, ret_ann], ...]  # prange
    # MUST receive the SAME constraints the frontier was solved under (frontier.py
    # passes its own lo_b/hi_b/fully_invested/rf). The cloud is read as the
    # achievable set, so the frontier has to be its upper-left envelope.
mpt.cvar_of(port_ret, alpha) / max_drawdown(port_ret) / cdar(port_ret, beta)
mpt.overlapping_h_returns(port_ret, h) -> overlapping h-day COMPOUNDED returns (len T−h+1)
mpt.var_cvar_horizon(port_ret, alpha, h_base=10, h_target=30) -> (var30, cvar30)
mpt.asset_risk_stats(returns_df, alpha) -> {sym: {ret_ann, ret_total, var30, cvar30}}
```

### The cloud and the frontier must share one feasible set (v1.11.3)
`cvar_return_cloud` sampled the bare simplex while `mean_cvar_frontier` solved
inside the per-position box, so with limits set the scatter was drawn from a
*different, larger* set than the frontier. Measured on the reported book (mins
summing to 80% across four names): **52.7% of 20 000 cloud points sat at a lower
CVaR than the frontier's own min-CVaR portfolio**, the frontier floated
mid-cloud instead of hugging its edge, and the axis stretched to a risk level
nothing feasible could reach (cloud 10.6–35.8% vs frontier 20.4–25.7%). After
passing the box through: cloud 20.2–25.7%, 0.6% left-of-frontier. The residual
is the **daily-vs-30-day estimator gap** (the LP minimises daily CVaR, the axis
shows the 10d→30d one), not a sampling error — the *unconstrained* control's
min sits 3.6% below its own frontier, relatively worse.

Sampling is `w = w_min + free_budget × Dirichlet`, then `_project_into_box`
water-fills the overflow into remaining headroom. **Not rejection sampling** —
with a tight box the acceptance rate collapses and the cloud never fills. With
no box, `free_budget = 1` and the mixture passes through untouched, so the
unconstrained cloud stays bit-identical (asserted in the scratch harness by
comparing a default call against an explicit `w_min=0, w_max=1` call).

Two frontend consequences, both in `mptComputeProj`:
- **The rf floor is bounded.** `yMin = min(yMin, rf)` alone spent ~85% of the
  height on empty space once every feasible portfolio sat between 25% and 29%
  return against a 4.5% rf. rf may now pull the floor down by at most half the
  data's own span; past that the line simply isn't drawn (`drawAxes` already
  guards on the domain). Wide unconstrained runs still show it, unchanged.
- **The streaming `done` handler always re-measures** (`mptRender()`), never
  reusing `_streamProj`. That domain is fixed from the frontier before any cloud
  point exists and is wrong in *both* directions — too small clips the scatter
  flat against the edge, too large strands the plot in an empty frame. Verifying
  it instead needs a magic "close enough" threshold; one honest re-measure of a
  finished cloud does not. `cloudRoom`'s reservation is correspondingly modest
  (1.25×) since it is now only a transient frame.

### Displayed tail risk — 10d→30d (FRTB-style), v1.9
The optimizer still minimises **daily** CVaR (the LP is unchanged). What the UI
*shows* is a separate, more interpretable estimator: empirical VaR **and** CVaR on
**overlapping 10-day compounded returns** (FRTB liquidity-horizon convention),
scaled 10→30d by **√3** (`LIQ_HORIZON`/`DISP_HORIZON` in mpt.py — the single place
to retune). Confidence follows the α slider. This replaced the old `×√252`
annualized CVaR on the x-axis, which routinely exceeded 100% and wasn't a real
loss number. Frontier points therefore carry BOTH `cvar` (daily×√252, used only to
keep the solver's ordering) and `var30`/`cvar30` (display). **The efficient
envelope is cleaned on `cvar30`** so the plotted line is monotone on the axis the
user actually sees. The bootstrap band stays daily (i.i.d. resampling destroys the
time-ordering the overlapping estimator needs) and is transferred onto the 30-day
value multiplicatively — `cvar30_{lo,hi} = cvar30·(band_{lo,hi}/band_med)` — the one
deliberate approximation. `app.js` reads everything through `mptCvar/mptVar/
mptCvarLo/mptCvarHi`, which fall back to the legacy annualized `cvar` for restored
pre-1.9 runs; the axis + stats rows then self-label "annualized · legacy".

**8-core (`prange`).** `_cloud_kernel` and `_bootstrap_cvar` are `@njit(parallel=True)`;
each `prange` iteration keeps its scratch buffers thread-local (bugs here = silent
races). `_bootstrap_cvar` seeds per-replica RNG (`np.random.seed(seeds[b])`) so the
band is deterministic regardless of thread count. `_warm_jit()` (import-time) and a
background thread in `server.main()` pre-compile so the first Optimize click never
pays the ~7 s JIT cost.

**Streaming orchestration (`frontier.py`).** `compute_efficient_frontier_stream(...)`
is a generator yielding `{"type":"progress"|"done"|"error"}`; `compute_efficient_frontier`
is a thin blocking wrapper that drains it (tests + non-streaming callers; pass
`max_seconds=0` to skip the wall-clock fill and run only the minimum). Budgets are
**wall-clock targets** (`_BUDGETS` light/standard/dense ≈ 5/15/60 s): the fixed-size
cloud is drawn first, then the bootstrap band runs until `t_total + target_s`
(`_BOOT_MIN` floor / `_BOOT_CAP` ceiling). pct = elapsed/target, ETA = target−elapsed
(both literally true). The server route stops the compute when the client
disconnects — it stops pulling the generator (`gen.close()`), so cancellation lands
at the next chunk boundary.

### Black-Litterman (return engine)
- Work in **excess-return space** (over rf); add rf back at the end so callers see
  total returns. Prior **Π = δ·Σ·w_mkt** (reverse optimization). δ calibrated from a
  target market risk premium / market variance (`risk_premium / (w_mktᵀΣw_mkt)`,
  clamped, default fallback ~2.5). `w_mkt` = market-cap weights — **caps are
  FX-normalized to USD in frontier.py** first (yfinance reports marketCap in native
  currency; a ¥ cap is ~150× a $ cap numerically and would swamp the prior).
- Views: P = identity rows for assets with a valid analyst target;
  q_i = target/price − 1 − rf. Ω diagonal, per-asset confidence from analyst count
  (`n/(n+k0)`) × dispersion (`1/(1+((hi-lo)/tgt)/d0)`). The **analyst-trust haircut**
  H ∈ [0,1] scales Ω by `(1-H)/H`: H→0 ⇒ Ω→∞ ⇒ posterior→prior; H→1 ⇒ Ω→0 ⇒
  posterior→views. Posterior μ_BL = Π + τΣPᵀ(PτΣPᵀ+Ω)⁻¹(Q−PΠ), k×k inverse (k≤N).
- Posterior covariance is **not** used for risk — risk is empirical CVaR.

### Mean-CVaR frontier (risk engine)
- Scenarios = **daily** FX-adjusted returns (~750/3Y). CVaR_α is optimized in daily
  units. The `cvar` field is the legacy ×√252 annualization, now kept only as the
  solver's ordering key — **what is plotted/reported is `cvar30`/`var30`** (see
  "Displayed tail risk" above).
- Frontier: sweep the return floor from the min-CVaR portfolio to the max-return
  portfolio (closed-form `_max_return_weights`), solving the Rockafellar-Uryasev LP
  at each. Constraints: long-only box `[w_min, w_max]`, and `Σw=1` (fully invested)
  or `Σw≤1` (cash allowed, remainder credited at rf via μ_ex = μ−rf).

### Solver — numba primal-dual interior-point (`_cvar_pdip`)
- Mehrotra predictor-corrector on the RU LP. Variables (w, ζ, u∈R^T). The u-block of
  the Newton system is diagonal, so u is eliminated by a Schur complement each
  iteration → a dense **(N+1)×(N+1)** solve (+1 border row for Σw=1). T-sized work
  stays as two matmuls. Balanced dual start (λ·s ≈ 1) → ~20–25 iters; cap 60 (cash
  degeneracies need more). Convergence gap 1e-9 (far past the 1e-6 accuracy bar).
- `mean_cvar_frontier` returns `n_nonconv`; frontier.py turns it into a warning.
- Certified vs `tests/_cvar_reference.py` (scipy HiGHS): |ΔCVaR|<1e-6 over hundreds
  of random problems. **Re-run `pytest tests/test_metrics.py -k cvar_pdip` after any
  solver edit.**

### Performance
- Data fetch dominates the *base* result (cold ~1–5 s); the base optimization math is
  ~40–150 ms (N≤30). The **budget itself is intentional wall-clock spend** on the
  bootstrap band (~5/15/60 s), all on `prange`, and is streamed with a real ETA — so
  "slow" here is the user's dial, not a regression. Cloud (fixed 20k/40k/80k) ~0.3–1.5 s.
- Reuse: `analytics._bulk_close` (price history), `fx._apply_fx_to_closes` (currency),
  `frontier._risk_free_history` / `/api/risk-free-history` (rf sparkline).

### `/api/efficient-frontier` params (`frontier.py`)
`_MPT_LOOKBACK_YF = {"1Y":"1y","3Y":"3y","5Y":"5y","10Y":"10y"}`. Request fields:
`lookback, display_ccy, rf, alpha, fully_invested, bounds ({sym:{min,max}} fractions),
cov_model, haircut, budget (light/standard/dense wall-clock tiers), current_weights`.
Legacy scalar `w_min`/`w_max` and `cloud_budget` are still accepted as fallbacks.
Frequency is fixed **daily**. Response frontier points carry `cvar_lo/cvar_med/cvar_hi`
(bootstrap band) plus `cvar30_lo/med/hi`; `meta.n_boot` is the achieved replica count.

**Streaming message order (v1.9):** the route is message-type-agnostic (it JSON-writes
every yielded dict), and the generator now emits
`progress…` → **`frontier`** (frontier + anchors + bl + params, so the client can fix
the axis domain and draw the line immediately) → **`cloud`** chunks (≤5000 `[cvar30,
ret]` pairs each — the client paints them additively so the scatter visibly fills)
→ `done`. **`done.cloud` is deliberately `[]`** — the cloud already went out in the
chunks; re-shipping 20-80k points would double the payload. The blocking wrapper
`compute_efficient_frontier` reassembles the chunks into `result["cloud"]` so tests
and non-streaming callers are unaffected. `done` also carries `asset_stats` and
`analyst_detail` (the per-company hover data).

### Saved run (`<data>/state/mpt.json`) — last 3 runs per portfolio
```jsonc
{ "runs": { "<portfolio name>": [          // newest-first LIST, capped at 3
    { "id": "run_...", "saved_at": "...",
      "params": {lookback, alpha, rf, fully_invested, cov_model, haircut, budget, bounds, display_ccy},
      "symbols": [...], "missing": [...],
      "frontier": [{ret, cvar, var30, cvar30, cvar30_lo/med/hi, cvar_lo/med/hi, vol, mdd, cdar, weights}, ...],
      "min_cvar": {...}, "max_ret": {...},
      "anchors": {equal, cap, current}, "bl": {...}, "meta": {...},
      "asset_stats": {...}, "analyst_detail": {...},
      "cloud": [[cvar30, ret], ...] },   // DOWNSAMPLED to ~2.5k client-side
    ...] } }
```
`save_mpt_run` unshifts and truncates to `_MPT_MAX_RUNS = 3`; a run whose `params`
equal the newest entry's **replaces** it (dedupe) rather than duplicating.
`get_mpt_runs` returns the list, `get_last_mpt_run` its head; `_as_run_list`
tolerates both legacy formats (bare dict, or an older list). Rename/delete cascades
move the whole value and are format-agnostic. The cloud **is** persisted now (it was
previously stripped and never re-sampled, so restored runs rendered an empty
scatter) — downsampled to ~2.5k points so 3 runs stay small on disk.
`save_mpt_run` overwrites (single run); `get_last_mpt_run` reads it (tolerates the
legacy list format → newest entry). Cascades on view rename/delete. The heavy `cloud`
is stripped client-side before saving and re-sampled on load.

---

## 13. CI / Quality gates

### GitHub Actions (`.github/workflows/ci.yml`)

Runs on every push to `main` or `claude/**` branches and on every PR to
`main`. Python jobs install through `astral-sh/setup-uv` (SHA-pinned, cache
on) and `uv sync --locked` from `uv.lock`; `server-smoke` uses the base deps,
`desktop-import-smoke` adds `--extra desktop`. The three installer jobs
instead do what a user's machine does: `uv tool install`, which resolves from
pyproject's ranges, not the lockfile (§4). Eight jobs — the
first two gate the rest (`needs: [lint, test]`), so a trivial syntax error
fails in seconds instead of waiting on the platform-specific jobs first:

| Job | Runner | What it proves |
|---|---|---|
| `secrets` | ubuntu | gitleaks (checksum-pinned binary) over the **full history**, plus a filename check that no runtime-state / key / `settings.local.json` file exists in any commit — §18. Independent of the others so a leak fails fast |
| `lint` | ubuntu | Every `.py` parses (`scripts/check_syntax.py`); `pyflakes` on all `convexity/*.py` + `build_symbol_db.py` + `scripts/*.py` (non-blocking); dependency manifests cover `envcheck.REQUIRED`; `uv lock --check` (lockfile matches pyproject); `dashboard.py` parses; `install.ps1`/`update.ps1` parse via PowerShell Core's own `Parser.ParseFile`; `install.sh`/`update.sh`/`Launch Dashboard.command` pass `bash -n`. Runs `uv run --no-project` — no dependency install |
| `test` | ubuntu | `uv sync --locked --extra dev` then `uv run pytest tests/` — the full unit suite |
| `server-smoke` | ubuntu | Real HTTP requests against a real running server (`scripts/smoke_test_server.py`) — `/`, `/api/watchlists`, `/api/views`, `/static/*` must return real 200s with real bodies. This is the answer to "is the app actually working," not just "does it import." |
| `desktop-import-smoke` | ubuntu | `convexity.desktop` imports cleanly under a real (headless, `QT_QPA_PLATFORM=offscreen`) `QApplication` — catches PySide6/QtWebEngine API breakage the plain lint job can't see, since lint never installs PySide6. Needs a handful of system graphics libraries (`libegl1`, `libgl1`, etc.) installed via `apt-get` first — the bare runner has none, not even for the offscreen platform plugin |
| `tool-install-smoke` | ubuntu | `uv tool install .`, envcheck with the tool's interpreter, then boots the **installed** `convexity` from a directory outside the checkout and asserts `/api/health` reports this `__version__` and `data_dir == CONVEXITY_HOME` — the package (static files, icon, lexicon) works without a checkout |
| `macos-install-smoke` | **macos-latest** | The whole `install.sh` against the checkout (`CONVEXITY_SOURCE`) into throwaway dirs (`INSTALL_APPS_DIR`, no Desktop shortcut, stdin `/dev/null` so the libomp prompt answers no): real `sips`/`iconutil`, then asserts the bundle's `.icns`, the launcher's exec target and `CFBundleShortVersionString` |
| `windows-install-smoke` | **windows-latest** | The whole `install.ps1 -Source .` on a disposable runner — uv tool install, envcheck, the `uv run --with pillow` `.ico`, and the **real** Start Menu + Desktop shortcuts, re-read through `WScript.Shell` (TargetPath = the tool's `convexity-app.exe`, IconLocation = the `.ico`). `WScript.Shell` has no equivalent on macOS/Linux, not even under PowerShell Core |

Until v1.14 the two platform jobs ran only the icon/shortcut commands: the
conda installers cost 10-15 minutes per platform per push, mostly re-testing
conda-forge's solver. The uv installers take a couple of minutes, so CI now
runs them end to end. What CI still cannot show is a **clean machine**: the
runners have git, Homebrew and preinstalled Pythons. The closest local check is
an empty `HOME` and a minimal `PATH` with the installer piped in, as a
`curl | bash` user would run it:
`cat install.sh | env -i HOME=<tmp>/home PATH=/usr/bin:/bin CONVEXITY_SOURCE=<checkout> INSTALL_APPS_DIR=<tmp>/Apps CONVEXITY_HOME=<tmp>/data bash`
— that exercises the official uv installer and a managed Python download too.

`pyflakes` stays `|| true` (non-blocking) — tighten by removing that once
the false-positive rate on the wider `convexity/*.py` glob has been
measured over a few weeks.

**To tighten a check:** remove `|| true` from the relevant step in `ci.yml`
and commit — the next push will enforce it.

### Pre-commit hooks (`.claude/settings.json`)

Two hooks fire on every `git commit` inside a Claude Code session:

1. **Syntax check** (`command` hook, hard gate) — runs `python -c "import ast;
   ast.parse(open(f).read())"` on every staged `.py` file. Exits non-zero
   (blocks the commit) on any syntax error.

2. **AI code review** (`agent` hook, Haiku model) — runs `git diff --cached`,
   reviews for critical issues only (runtime exceptions, accidental secrets,
   broken cross-references). Blocks on genuine problems; passes silently on
   style/TODOs. Timeout 90 s.

To **evolve** the pre-commit checks as the project grows:
- Add new file types to the syntax-check step (e.g., `grep '\.js$'` for
  external JS if the app ever gains a separate JS bundle).
- Tighten the AI reviewer prompt in `.claude/settings.json` — e.g., add
  "also check that any new API route has a matching DELETE handler" once that
  pattern is established.
- Wire pytest once tests exist: add a third command hook that runs
  `python -m pytest tests/ -q` after the syntax gate.

### Manual review before committing

Run `/review` (or `/security-review`) at any point during a session to get a
full structured review of all staged changes. This is separate from the
pre-commit hook and can be used to catch architectural issues early.

### Merging PRs without leaving Claude Code

```bash
# Squash-merge (recommended — keeps main history linear)
gh pr merge <number> --squash --delete-branch

# Auto-merge once CI passes
gh pr merge <number> --squash --auto --delete-branch

# Check PR status
gh pr status
gh pr checks <number>
```

**After a merge, make the new version the live one — this does not happen
automatically.** PR work happens in a worktree (`.claude/worktrees/...`);
`gh pr merge` updates `origin/main` on GitHub, but neither the user's main
checkout at the repo root nor their installed app picks that up by itself.
Steps, all required:

1. **Pull the main checkout.** `git -C <repo root> pull origin main` (or
   `git status --branch` first to confirm it's actually behind — merging
   from a worktree never touches the root checkout's working tree).
2. **Tag + release** when the merge bumped the version (§15) — the installer
   installs the latest *release*, so an untagged version cannot reach the
   user's app.
3. **Reinstall + relaunch the app.** The installed app is a uv tool, not the
   checkout (§3): run `./install.sh` (latest release) and ask the user to
   quit and reopen Convexity. A long-lived process keeps serving the code it
   loaded at start — Python doesn't hot-reload, so an old process keeps
   reporting the old `__version__` via `/api/health` indefinitely.

Verify with `curl -s http://127.0.0.1:8765/api/health` and confirm
`version` matches the just-merged `__version__` before telling the user
the update is live.

---

## 14. Desktop app (PySide6 + QtWebEngine)

### Why this exists

Browser mode (`python dashboard.py`) historically auto-launched Google Chrome
(removed in v1.12.3; it now only prints the URL), which failed on a machine
with no Chrome. The desktop app
wraps the *identical* HTTP server in a native window instead, using
QtWebEngine (which bundles its own Chromium — same rendering engine as
Chrome, so nothing about the frontend's behavior changes). Browser mode is
untouched and remains the documented fallback; this is purely additive.

**Why single-process, not Electron-style sidecar:** the HTTP server runs on
a background thread inside the same Python process as the Qt event loop
(see `start_server()` below). No subprocess, no IPC, no second language
runtime.

**Why a uv tool, not a packaged installer:** this is personal/friends-and-
family distribution. `install.sh` / `install.ps1` are the "build step"
(v1.14): install uv, `uv tool install` the package from a release, generate
the icon, and drop a launcher (`.app` on macOS, Start Menu/Desktop shortcuts
on Windows). No PyInstaller, no code signing. Updating is re-running the
installer (`update.sh` / `update.ps1` just do that). The server lives and dies
with the app process, so an update takes effect on the next launch. (Until
v1.13 this was a Miniforge `pt` conda env built from a checkout; that is
retired — see "Installers" below.)

### The `start_server()` / `shutdown_server()` seam (`convexity/server.py`)

`main()` (browser mode) and `desktop.py` (app mode) both need the same
port-pick + `ThreadingHTTPServer` construction but manage their own
lifecycle, so that piece is factored into two small functions:

- **`start_server() -> (server, port)`** — picks a port via the existing
  `_pick_port()`, constructs `ThreadingHTTPServer`, sets
  `server.daemon_threads = True`, and runs `serve_forever()` on a daemon
  background thread. Returns immediately (non-blocking) — this is the key
  change from the original code, where `serve_forever()` ran inline on the
  main thread. `daemon_threads = True` matters: `socketserver.ThreadingMixIn`
  defaults per-connection handler threads to **non-daemon**, which — without
  this — can block process exit on a stuck/long-lived connection (e.g. an
  open NDJSON stream) even after `shutdown()` + `server_close()`.
- **`shutdown_server(server)`** — `server.shutdown()`, `server.server_close()`,
  flush stdout, then **`os._exit(0)`**. The `os._exit()` is deliberate and
  non-obvious: `fetch_one`/analytics/`news_sentiment` all use module-level
  `concurrent.futures.ThreadPoolExecutor` pools, and `ThreadPoolExecutor`
  registers an **atexit hook that joins any in-flight work** before a normal
  interpreter shutdown can complete — verified directly (see "Gotchas
  discovered" below) that this can stall process exit for as long as the
  slowest pending network call takes. The existing browser-mode launcher
  (`Launch Dashboard.command`) never hit this because it always kills the
  process outright (SIGTERM's default disposition is an unclean, instant
  stop — no handler installed catches it, so it's no different from
  SIGKILL here); `os._exit()` gives the same guarantee from *inside* the
  process, which the desktop app needs since its quit path is triggered by
  Qt (`aboutToQuit`), not an external signal. The OS reclaims the socket and
  any WebEngine helper process either way (verified: an abruptly-killed
  parent's `QtWebEngineProcess` self-terminates within a few seconds via
  Chromium's own parent-death detection — this is normal, not a hang).
  `main()` (browser mode) calls `shutdown_server()` from its
  `finally:` block; `desktop.py` connects it to `app.aboutToQuit`.

`main()` itself now blocks on an interruptible `while True: time.sleep(1.0)`
loop instead of calling `serve_forever()` directly, since that call moved
into `start_server()` — Ctrl+C behavior is unchanged.

### `convexity/desktop.py`

Run via `python -m convexity.desktop` (what the installed launcher
actually invokes). Single file, ~160 lines:

- **Splash contract**: shown immediately via `QSplashScreen` (icon +
  "Convexity" + "Made by Alexander Tsoskounoglou 2026 · v{version_display}",
  colors matching the dashboard's own dark theme — `#0d1117`/`#e6edf3`/`#7d8590`
  from `style.css`). A `QElapsedTimer` starts the moment it's shown. The
  main window is revealed on `max(0, 6000ms - elapsed)` after the
  `QWebEngineView`'s `loadFinished` fires, via `reveal()` (guarded by a
  `nonlocal` flag so it only runs once). A 20s **safety timer** also calls
  `reveal()` unconditionally, so a stalled/failed page load can never trap
  the user on the splash forever. The subtitle's font is 12pt, sized down
  from an original 14pt specifically to keep the longer
  "v{version} (dd Mon yyyy)" string comfortably inside the 480px-wide
  splash pixmap — verified by rendering the real pixmap headlessly
  (`QT_QPA_PLATFORM=offscreen`) rather than guessing at text width.
- **External-link routing** (`_ExternalLinkPage`, a `QWebEnginePage`
  subclass): the dashboard has exactly two `target="_blank"` links —
  company website (`app.js` detail modal) and news article links (`app.js`
  news section) — both plain anchors, no `window.open()` calls. Chromium
  routes these through **either** `acceptNavigationRequest` (in-place
  navigation attempts) **or** `createWindow` (real new-window/tab
  requests), depending on how the click is dispatched, so both are
  overridden: `acceptNavigationRequest` blocks (`return False`) and hands
  off to `QDesktopServices.openUrl()` for any `NavigationTypeLinkClicked`
  whose host isn't our own loopback server; `createWindow` returns a
  throwaway `QWebEnginePage` whose first `urlChanged` triggers
  `QDesktopServices.openUrl()` then `deleteLater()` — no in-app popup
  window is ever shown. Verified directly against both code paths with
  `QDesktopServices.openUrl` mocked (not just by clicking through the UI).
- **Icon**: `convexity/assets/icon.png` — **package data** since v1.14 (it
  was at the repo root, which an installed package cannot reach, so a tool
  install would have run iconless). Resolved relative to `desktop.py`, set on
  both `QApplication` (dock/taskbar) and the window. Both call sites guard on
  `.exists()` — the app never crashes if the icon is missing, it just runs
  iconless. The installers read the same file through the tool's interpreter.
- **Shutdown**: `app.aboutToQuit.connect(lambda: shutdown_server(server))`.
  Verified via three independent paths: `app.quit()`, `window.close()`
  (which reaches `aboutToQuit` because Qt's `quitOnLastWindowClosed`
  defaults to `True`), and clicking the real close button through the
  Accessibility API — all three cleanly free the port immediately, even
  with an NDJSON stream actively in flight.

### Installers (`install.sh` / `install.ps1`, v1.14)

One command on a clean account, and safe to re-run (that is the update):

1. **uv** — found on PATH or in `~/.local/bin` / `~/.cargo/bin`, else Astral's
   official installer (`curl -LsSf https://astral.sh/uv/install.sh | sh`,
   `irm https://astral.sh/uv/install.ps1 | iex`).
2. **libomp (macOS)** — see §4; checked *before* installing, never fatal.
3. **Source** — the latest GitHub release (`api.github.com/.../releases/latest`),
   installed from the tag's **source archive**
   (`github.com/ithakis/Convexity/archive/refs/tags/vX.Y.Z.tar.gz`), not
   `git+https`: a clean Mac has no git (`/usr/bin/git` is a shim that offers
   the Xcode tools), and uv would need it. Tags older than `MIN_TAG` (v1.14.0,
   the first with a pyproject) are refused with a clear message.
   `uv tool upgrade` cannot move a tag-pinned URL requirement, so updating
   re-runs `uv tool install --force` at the newest tag.
4. `uv tool install --force --python 3.11 "convexity[desktop] @ <src>"` —
   uv downloads a managed 3.11 when the machine has none.
5. **Verify** with the tool's own interpreter: `python -m convexity.envcheck`
   (fails the install on a critical miss) and `import lightgbm` (warning).
6. **Keys from an old checkout** — when the script runs from a checkout that
   has `.finnhub_key` / `.nvidia_key`, they are copied into the data folder's
   `config.json` (0600, never overwriting a key already there, values never
   printed). An installed package cannot find them by walking up from
   `site-packages`, so without this an upgrading user's News read went dark.
   Piped from curl there is no checkout and nothing to copy.
7. **Launcher** — macOS: `Convexity.app` (bundle ID `com.ithakis.convexity`,
   `CFBundleShortVersionString` = the installed version) whose executable is
   `exec "<uv tool dir>/convexity/bin/convexity-app"` — an absolute path,
   because LaunchServices starts bundles with a minimal PATH. The bundle and
   Desktop symlink are recreated on every run. Windows: Start Menu + Desktop
   `.lnk` straight to the tool's `Scripts\convexity-app.exe` (uv builds
   `[project.gui-scripts]` as a windowless launcher). The conda-era hidden
   `.vbs` + `conda run` wrapper is gone: it existed only because conda's
   native DLLs needed the activation DLL search path (a bare `pythonw.exe`
   hard-crashed with 0xc06d007f); PyPI wheels carry their own DLLs.

Undocumented overrides (how the scripts are tested without touching the real
install): `CONVEXITY_SOURCE` (local checkout or archive URL; `-Source`),
`CONVEXITY_VERSION` (tag; `-Version`), `APP_NAME` (`-AppName`),
`INSTALL_APPS_DIR`, `INSTALL_DESKTOP_DIR` (empty = no shortcut), plus uv's own
`UV_TOOL_DIR` / `UV_TOOL_BIN_DIR`. Prompts read `/dev/tty` so they work under
`curl | bash`; with no terminal they answer no.

### Icon generation

- **macOS** (`install.sh`): `sips -z` renders 16/32/128/256/512 (+@2x) PNGs
  from the unmodified transparent `icon.png` into a `.iconset`, then
  `iconutil -c icns` writes straight into the bundle's `Contents/Resources/`.
  Both tools are macOS built-ins. Nothing is written into a checkout.
  **Non-obvious**: `mktemp`'s printed path must be used directly — appending
  a suffix after the fact (e.g. `TMP=$(mktemp -t x).sh`) creates a *second*,
  different path and leaves the original mktemp-created file/dir behind as
  an orphaned empty temp file on every run (reproduced directly: an earlier
  version of this script did exactly that). Fix: `mktemp -d` once, place a
  properly-named file/dir *inside* it, `rm -rf` the directory afterward. (The
  conda-era Miniforge download hit the same mistake as a hard failure: its
  installer refuses to run unless its own path ends in `.sh`.)
- **Windows** (`install.ps1`): Pillow is not an app dependency, so it runs in
  a throwaway `uv run --no-project --with pillow` environment and writes a
  multi-resolution `.ico` (`sizes=[(16,16)…(256,256)]`) to
  `%LOCALAPPDATA%\Convexity\icon.ico` — app-owned, deliberately not the
  `%APPDATA%` user-data folder.

### Gotchas discovered during implementation

- **`ThreadingHTTPServer.daemon_threads` defaults to `False`.** Without
  `start_server()` setting it `True`, a per-connection handler thread stuck
  on a long-lived request can block process exit indefinitely even after
  `shutdown()` + `server_close()` — reproduced directly with an in-flight
  `/api/quotes-stream` request still open at shutdown time.
- **`concurrent.futures.ThreadPoolExecutor` registers an atexit hook that
  joins pending work.** This is why `shutdown_server()` calls `os._exit(0)`
  instead of letting the interpreter exit normally — reproduced in
  isolation (a bare `ThreadPoolExecutor` with one pending future blocks
  process exit for the full task duration; `os._exit(0)` bypasses it
  cleanly, confirmed the OS still reclaims the socket).
- **PowerShell's `$ErrorActionPreference = "Stop"` does not catch a
  non-zero exit code from a native command** (`.bat`/`.exe` invoked via
  `&`) the way bash's `set -e` does — reproduced directly (`& false`
  followed by more `Write-Host` calls executes them anyway, script exits
  0). `install.ps1` defines an `Assert-Success` helper and calls it after
  every native call (uv installer, `uv tool install`, `uv tool dir`, envcheck,
  the icon and key-copy Python calls) — otherwise a failed install would
  silently fall through to building shortcuts against a broken tool.
- **The Windows one-liner runs Windows PowerShell 5.1, not pwsh 7** — three
  consequences found while verifying 1.14, all invisible to pwsh-only testing:
  (1) under `$ErrorActionPreference = "Stop"`, *redirected* native stderr
  (`2>$null`, `2>&1`) becomes a terminating error in 5.1, so a failed
  `import lightgbm` probe would abort the install — `install.ps1` relaxes the
  preference around that one call; (2) `Invoke-RestMethod` may not offer TLS 1.2
  on older .NET, so the script ORs `Tls12` into `SecurityProtocol` (as uv's own
  installer does); (3) `[Uri]"<path>"` leaves `AbsoluteUri` empty for a Unix
  path, and `[System.Uri](Resolve-Path $x).Path` casts before `.Path` is read —
  use `[System.Uri]::new(<path>, [System.UriKind]::Absolute)`.
  `windows-install-smoke` therefore runs the installer with `shell: powershell`
  (5.1). To exercise `install.ps1` on macOS: `pwsh -File install.ps1 -Source .`
  with `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`/`CONVEXITY_HOME` in scratch gets through
  `uv tool install`; symlink `Scripts\python.exe` / `convexity-app.exe` to the
  tool's `bin/` and set `LOCALAPPDATA`/`TEMP` to reach the `.ico`; only the
  `WScript.Shell` COM step is Windows-only.
- **Blank window when launched from the .app bundle (ROOT-CAUSED & FIXED —
  `--single-process`).** The single most important desktop-app gotcha. When
  launched from the installed `.app` via LaunchServices (Finder / Dock /
  Spotlight / `open -a`), QtWebEngine's default **multi-process** Chromium
  cannot establish its Mojo IPC channel to the helper (network / renderer)
  processes. The browser process logs
  `mojo/core/channel_mac.cc ... mach_msg receive: (ipc/rcv) msg too large
  (0x10004004)`, every network request — including the initial
  `http://127.0.0.1:<port>/` page load — fails, `QWebEngineView.loadFinished`
  fires **`ok=False`**, and the window comes up **blank white**. This is what
  the earlier "intermittent invisible window / `Compositor returned null
  texture`" observation actually was: not a GPU/display quirk, but a failed
  page load rendering as blank. Root cause: a LaunchServices-launched process
  gets a **different Mach bootstrap namespace** than a shell child, so the
  Chromium helpers can't check back in. Verified exhaustively:
  - Terminal/shell launch of the *same* module → `ok=True`, renders. Bundle
    launch → `ok=False` **every time** (8/8), blank.
  - `urllib` (and `curl`) reach the server fine and the readiness probe gets
    a 200 — it is specifically Chromium's multi-process networking that
    breaks, not the server.
  - `--no-sandbox`, `--in-process-gpu`, `--disable-gpu` do **not** help
    (still `ok=False`). `--use-gl=swiftshader` crashes (SIGABRT — not
    available in this build). **Only `--single-process` fixes it** — it
    collapses renderer/network/GPU into the main process so there are no
    child processes to do Mach IPC with. Confirmed 8/8 bundle launches render
    and are fully interactive with it set.
  `desktop.py:_configure_chromium()` sets
  `QTWEBENGINE_CHROMIUM_FLAGS=--single-process` **before** `QApplication` is
  constructed (Qt reads it at web-engine init). Safe here: single-user,
  single-origin, fully-local content (external links are handed to the real
  browser), so the single-process downsides — no site isolation, a renderer
  crash takes the one window with it — don't apply. **Do not remove this
  flag.** If a future agent sees a blank window, check `loadFinished ok=` and
  the `channel_mac.cc mach_msg` line in `<data>/logs/desktop.log`
  / Chromium stderr before touching anything else.
- **Two robustness layers back up the flag** (in `desktop.py`): a **server
  readiness probe** in the boot worker (`urllib` GETs `/` until HTTP 200
  before the URL is handed to the view — kills the start_server-vs-first-
  request race that also produced `ok=False`), and a **load-retry** on
  `loadFinished(ok=False)` (up to 4 `view.reload()` attempts) with the
  min-splash / safety timer as the final backstop. Belt, braces, and a
  second belt — the app must open 100% of the time.
- **Dock icon before/after-launch mismatch (FIXED).** The old code called
  `app.setWindowIcon(QIcon("icon.png"))` unconditionally; on a bundle launch
  that *replaces* the Dock's bundle-`.icns` rendition (which gets macOS's
  standard rounded-tile treatment) with a raw PNG rendered differently — so
  the icon visibly changed the moment the app launched. Fix: `desktop.py`
  only calls `setWindowIcon` when **not** launched from the bundle (detected
  via `os.environ["__CFBundleIdentifier"] == BUNDLE_ID`); inside the bundle
  the `.icns` is left to win everywhere, so before/after are identical. The
  `.icns` is generated from `icon.png` by `install.sh` and matches it pixel-
  for-pixel (verified).
- **Startup speed / splash.** The splash (branded pixmap + a live **accent
  progress bar + braille spinner + percent**, mirroring the in-app
  `.progress-bar` / `.lc` loading-chip assets) is shown *before* the heavy
  import thread starts — starting the import first makes its GIL-heavy work
  contend with the main thread and visibly delays the splash. Two backend
  imports were made **lazy** to get `import convexity.server` off the
  startup path from ~6s down to ~1–3s (warm): `openai` (deferred into
  `news_sentiment._get_client()`, ~1.2s — the News read is on-demand) and
  `convexity.frontier`/`mpt`/`numba` (deferred to its two call sites
  in `server.py`, ~1.7s — the Optimize/MPT feature is on-demand). `pandas` +
  `yfinance` (~2.5s) stay eager since the first data render needs them. Net:
  the whole import now finishes inside the 6s minimum-splash window, so the
  perceived launch time is the intended 6s floor, not import time. The 6s is
  a **minimum** visible duration (deliberately longer than the boot itself
  needs, so there's always a moment to watch it); a slower
  boot keeps the splash up longer, and a 20s safety timer + boot-still-running
  re-arm guarantees the splash never traps the user.

### Windows verification status

This repo is developed on macOS; `install.ps1` has never run on a real
user's Windows machine. What is covered: PowerShell Core parses both scripts
in the lint job, and since v1.14 `windows-install-smoke` runs the **whole**
`install.ps1` on a real `windows-latest` runner — uv tool install, envcheck,
the Pillow `.ico` and real `WScript.Shell` shortcuts re-read and checked
(§13). The conda-era installer had a real Windows-only bug (NSIS `/D=`
quoting of the Miniforge install path) that only a real runner caught; that
code path is gone with conda.

**Still unverified** (needs a real end-user machine): that double-clicking
the shortcut gives a windowless launch (uv's GUI launcher should; never
observed by a human), first-launch SmartScreen/Defender prompts for the
unsigned app, the uv installer and `uv tool` paths on an account whose name
contains a space (the runner's account is `runneradmin`), and whether the
desktop window itself renders (CI never starts the GUI on Windows).

---

## 15. Version tracking

Single source of truth: `__version__` in `convexity/__init__.py`
(read it there — do not trust a hardcoded number in this doc). Scheme is `1.X.Y` — X bumps on a major new
feature/release, Y bumps on smaller polish/fixes in between. `CHANGELOG.md`
maps every version to the PR(s) it came from.

The module also exports `__version_date__` (ISO `yyyy-mm-dd`, the release
date of `__version__`) and `__version_display__` (pre-formatted
`"1.5.0 (09 Jul 2026)"` — day-month-year, deliberately different from the
app's general "Jul 9, 2026" date format; this is the one place that uses
day-first). `__version_display__` is what Python-side surfaces (splash,
window title, terminal banner) should use directly; the frontend instead
reads the raw `version`/`version_date` fields from `/api/health` and
formats them with its own `fmtDateDMY()` helper, so both sides format from
the same two source values without duplicating the format string.

**Where it's surfaced:**
- Terminal startup banner (`convexity/server.py`'s `main()`) — uses `__version_display__`.
- `GET /api/health` → `{"ok", "ts", "version", "version_date"}` — the frontend's source.
- The app footer/version tag lives in the Analyst Sentiment section's
  coverage-footer row (`.an-coverage-foot` in `app.js`'s
  `renderAnalystDashboard`, right-aligned via `.an-version`), populated by
  `loadAppVersion()` + `versionLabel()`. **Not** a fixed-position page
  overlay — that was removed (the old `#app-footer` div/CSS no longer
  exist); the version tag now only renders where the Analyst Sentiment
  section is in view, which was a deliberate user-requested tradeoff.
- Desktop app splash subtitle and main window title
  (`convexity/desktop.py`), both via `__version_display__`.

**When bumping:** update `__version__` and `__version_date__`, add a line
to `CHANGELOG.md`. No other files need touching — every surface above reads
the same two constants (browser mode via `/api/health`, desktop mode via
direct import).

**Reasoning before a bump (standing policy — do not skip):** before touching
`__version__`, reason out loud in the response to the user about whether the
change actually warrants a version increment at all, and if so whether it's
a major (X, new feature) or minor (Y, polish/fix) bump per the scheme above
— then ask the user to confirm before changing the file. Never bump silently,
even for changes that look small; the user wants to make this call
explicitly every time, not have it inferred.

**GitHub Release per version (standing policy — do not skip).** The
`CHANGELOG.md`-derived timeline lives on GitHub too, not just in the repo:
every version from `v1.0.0` onward has a matching git tag + GitHub Release
(`gh release list`), notes copied verbatim from that version's
`CHANGELOG.md` section. **Once a version-bump PR merges to `main`, tag +
release it in the same sitting — this is not a separate/optional step:**
```bash
git tag -a vX.Y.Z <merge-commit-sha> -m "vX.Y.Z — <date>"
git push origin vX.Y.Z
gh release create vX.Y.Z --title "vX.Y.Z — <date>" --notes-file <(sed -n '/^## X.Y.Z/,/^## /p' CHANGELOG.md | sed '1d;$d')
```
(the `sed` slice pulls just that version's section out of `CHANGELOG.md`
between its own header and the next one — adjust the range if it's the
newest entry with no following `## ` line). The merge commit is the tag
target, i.e. what actually landed on `main`, not a branch-tip commit that
may still get rebased. If a version was pushed straight to `main` without a
PR (rare, but see `1.4.2`–`1.4.4` in history), tag that direct commit
instead. A version bump with no changelog section of its own (e.g. `1.4.1`,
whose content got folded into `1.4.2`'s writeup) gets no separate tag/release
— never fabricate release notes to fill the gap.

---

## 16. `logbuf.py` — the backend console (Settings → Logs)

The entire backend logs via bare `print()` (30+ call sites in news_sentiment,
ml_sentiment, server). In browser mode those land in the launching terminal. In
**desktop mode they land nowhere**: the `.app` has no terminal, and
`desktop.py`'s `_setup_logging()` redirects the `logging` module, not
`sys.stdout`. That is how the ML model stayed dead for weeks — the one line
explaining why was written to a file descriptor no human could read (§4).

`logbuf.install()` (called from `server.start_server()`, idempotent) wraps
`sys.stdout`/`sys.stderr` in a write-through tee that also appends whole lines
to a `deque(maxlen=4000)` of `{seq, ts, stream, text, http}`. Every existing
`print()` is captured with **zero edits to the call sites** — do not "clean this
up" by converting them to `logging` without keeping the tee, or desktop-mode
output goes dark again.

- `seq` is monotonic and never reset. `read(since, limit)` returns only newer
  lines plus `dropped`, so the UI can show a "lines dropped" marker instead of
  silently skipping output.
- `http` tags `server.py`'s per-request log lines. The frontend hides them by
  default — one per request drowns everything else.
- Writes still reach the real stream, so the terminal and the launcher log file
  behave exactly as before. This is purely additive.
- The frontend polls `/api/logs?since=` every 1.5s **only while the Logs pane is
  open** (`stopLogPolling` clears the timer in `closeSettings`). Autoscroll
  pauses itself when the user scrolls up and resumes at the bottom.

---

## 17. `jobs.py` — background refresh jobs (v1.11.0)

Refreshing used to be two disconnected, blocking buttons (`#refresh` for
quotes, `#ns-refresh` for news). Nothing survived a tab switch, nothing was
cancellable, and there was no way to refresh the whole account. Now there is
**one Refresh control** in the topbar:

| Gesture | Scope | Blocking |
|---|---|---|
| Click, or `R` | Current portfolio: quotes → then News read + Market read | No |
| Long-press ≥600 ms → confirm, or `Shift+R` | **Every** saved portfolio | No |

There is no separate news refresh button or route — news is a phase of the job.

### Four rules that are load-bearing

1. **Single-flight.** `submit()` rejects a second concurrent job with `409` and
   the client attaches to the running one; only the long-press path (behind its
   confirm dialog) may `on_conflict: "supersede"`. This is correctness, not
   politeness: the Finnhub/NIM limiters are process-global, so two jobs spend
   each other's budget, and `_refresh_symbol_sentiment`'s `_cache_take` /
   restore-on-failure pair races destructively — A takes the entry, B takes
   nothing, A fails and restores the STALE value over B's fresh one.
   Single-flight is also what makes the single global
   `helpers._RATE_OBSERVER` unambiguous; **relaxing it requires a context-local
   binding propagated into every pool worker.**
   The conflict check and the `_CURRENT` assignment are **one atomic step under
   `_REG_LOCK`** — reading `current()` outside it was a TOCTOU that let two
   simultaneous POSTs both start (10 of 80 trials on a `ThreadingHTTPServer`,
   which a double-click or a second tab reaches). `_CURRENT` then named only
   one, so the other was invisible to `current()`: uncancellable from the UI,
   and billing its rate waits to the wrong job. `request_cancel` on a
   superseded job is called **after** the lock is released — the stated lock
   order is registry → job condvar, never held while emitting.
2. **A dead client is not a cancel.** The stream handler returns on BrokenPipe
   and never touches `job.cancel`. Only the chip's × cancels. (The old news
   route did the opposite: it silenced writes while the work — and the API
   quota — carried on unattended.)
3. **Cancel actually stops work.** Queued items are de-queued by
   `pool.shutdown(wait=False, cancel_futures=True)` — **never** use a `with`
   block for these pools, `__exit__` joins everything. In-flight work checks a
   duck-typed `cancel` token (`.is_set()`, so `news_sentiment` never imports
   `jobs`) at five points, including the two sleep gates —
   `helpers._RateLimiter.acquire` and `_wait_for_circuit_breaker`, which can
   hold for 65 s and would otherwise make "cancelled" a lie for a full minute.
   `helpers.Cancelled` (aliased `news_sentiment.RefreshCancelled`) is **control
   flow, not failure** — `jobs._run` catches it separately, and a bare
   `except Exception` around it reports a user cancel as "Refresh failed".
4. **Counts are server-authoritative.** Every frame in `Job._COUNTED` carries
   the job's cumulative counts and the client *assigns* them. A `phase` frame's
   `total` is per-view and the server has already folded it in; accumulating
   client-side double-counted (the chip read "Quotes 4/8" for a 4-symbol
   portfolio).
5. **Both totals are planned before any frame exists** (v1.11.1).
   `_plan_totals(job)` runs in `submit()` — *before* the `queued` emit and
   before the worker thread, so the earliest frame and the reattach snapshot
   both carry a whole-job denominator. Each phase then **reconciles** its own
   figure (`Job.set_count`, never `bump`): quotes swaps each view's estimate for
   its real count and re-derives `actual-where-known + planned-for-the-rest`;
   news assigns the exact `len(symbols) + 1`. Every skip path — no
   `news_sentiment`, no symbols, cancelled, a view that emitted no `start` —
   must **release its reservation**, or the bar can never reach 100%.
   `_parse_entries` is shared by the planner and the quotes phase and must stay
   that way; if they disagree the denominator visibly corrects on the first
   item, which is the whole thing this removes. Why it matters: `news_total`
   used to be bumped at the news phase's start, so the client's denominator was
   quotes-only until then and the bar filled completely and then rewound by
   half (once per portfolio on an all-scope run). `rfRender` also clamps the
   painted percentage monotonically (`REFRESH.pct`, reset in `rfStart`/
   `rfReset`) so a late upward correction can't walk it backwards.

### Event stream

`GET /api/refresh-job/<id>/stream?since=<seq>`. Job events carry monotonic
`seq ≥ 1` and are replayed on reconnect; **connection frames (`hello`, `ping`,
`end`) carry `seq: 0` and are never replayed** — advancing the cursor on them
desyncs it. Types: `job`, `phase`, `item`, `item_stage`, `rate_limited` /
`rate_cleared`, `view_saved`, and the terminals `done` / `error` / `job`
{state: cancelled, drained: true}. `item_stage.stage` is
`start|fetch|pass1|pass2|market|aggregate` — `NS_PROG_STAGE` in `app.js` must
match; the per-ticker modal is opt-in behind the status chip. Each news `item`
carries its `outcome` (ok / failed / empty), and the counts include
`news_scored` / `news_failed`; the chip reads "News x/y · n failed". `dropped: true` (the `since` predates the 6000-frame
ring, same contract as `logbuf.read`) ⇒ the client must cold-re-read.
Client state is `REFRESH` + `{id, lastSeq}` in `sessionStorage`, saved per
frame; `GET /api/refresh-job/current` on load is what makes a job survive F5.

### Two save guards (both would silently destroy user data without them)

- **Never save a degraded batch.** `save_view` overwrites `rows` wholesale, and
  Yahoo answers an overloaded batch with `error` rows rather than an exception.
  `>20%` errors (`_MAX_ERROR_FRACTION`) ⇒ skip the save, emit
  `phase {saved: false, reason: "degraded"}`, keep the prior rows.
- **Always `set_last=False`.** A background job must never repoint the
  restore-on-launch target at whatever portfolio it happened to touch.

Also: **all `persistence` writes are atomic now** (`_atomic_write`, tmp file in
the same dir + `os.replace`). They were bare `write_text`, and the readers catch
`JSONDecodeError` and return `{}` — so a truncated write made *every saved
portfolio silently disappear*. Survivable when writes only followed an
interactive build; not with an unattended job and `os._exit(0)` on quit
(`shutdown_server` now calls `jobs.shutdown(1.5)` first).

The all-scope quotes phase runs **one portfolio at a time** with a ~500 ms gap
(5 workers *within* a portfolio) — `warmRecentTabs` established empirically that
concurrent portfolio work makes yfinance return empty frames, i.e. exactly the
degraded rows the guard above would then discard. Symbols shared across views
are fetched once. The news phase runs **once per job** over the union of
symbols, so the market-wide News read is refreshed once, not per portfolio.

### `rescore_window(symbols, days)` — instant News-window change

Changing the window (3/7/14/30D) used to do nothing until the next Refresh. It
now re-aggregates the **already-read cached headlines** (their lens and score)
under the new window's tau: zero network, zero LLM, zero ML inference. The
Market read is carried unchanged — it is defined on its 7-day window. ~10 ms.

**The honest limitation, and it is surfaced rather than hidden.** News is cached
per window (`news|{sym}|{days}`), so *widening cannot conjure articles that were
never fetched* — narrowing is exact, widening re-weights the same evidence.
`coverage[sym].truncated_by_fetch` is derived from **which windows were actually
fetched** (`max(from_windows) < days`), not from how old the newest article is —
a quiet ticker with no week-old news is not a truncated fetch, and an earlier
timestamp heuristic flagged both. `#ns-cov-hint` renders it. The carried-forward
LLM brief is stamped `brief_stale: true`; **never `_history_append`** from a
rescore — it would double-count the day in the Track record and in the Market
read's live-anchor reference.

---

## 18. Security & public-repo rules (standing policy — read before every commit)

This repository is **public**. Treat every commit, branch, tag, PR, issue,
release note and CI log as published **permanently**: rewriting history does
not reach existing clones, forks, GitHub's PR refs or search-engine caches.
There is no "clean it up later". The user is not a software developer and
relies on these rules being followed without being asked.

### Never commit
- **Runtime state** — anything from the data folder (`state/*.json`,
  `config.json`, …) and the legacy `.convexity_*.json` / `.portfolio_tracker_*.json`
  that an un-migrated checkout still has: the user's real holdings, watchlists,
  weights, MPT runs, news history. The data folder is outside the repo since
  v1.14, so the danger is copying from it into tests, fixtures or docs.
- **Keys** — `.finnhub_key`, `.nvidia_key`, `.openrouter_key`, `.env*`, `*.secret`,
  or any key *value* pasted into code, tests, docs, logs or fixtures.
- **Machine specifics** — `.claude/settings.local.json`, absolute home paths
  (`/Users/<name>/…` — write `~/…`, `$HOME/…` or repo-relative), `*.sqlite`, `.dashboard.pid`.
- **Personal data** — the user's email, phone, address, account numbers, or
  screenshots showing their real portfolio. README/docs screenshots must use a
  synthetic demo portfolio.
- **Real holdings as examples** — tests, fixtures and docs use well-known
  public tickers (AAPL, MSFT, NVDA, SPY…) or synthetic ones, never "the user's
  book". If unsure whether a list reflects the user's positions, don't use it.

Before every commit: read `git diff --cached --stat` and the diff itself.
The guards are a backstop, not the process.

### The guards (do not weaken, bypass or `--no-verify` them)
1. `.claude/hooks/check-secrets.sh` (PreToolUse on `git commit`) blocks staged
   secret files, runtime state, `settings.local.json`, SQLite files, key values
   and absolute home paths.
2. The pre-commit AI reviewer (`.claude/settings.json`) is told the repo is
   public and blocks secrets/private data.
3. CI job `secrets` runs gitleaks over the **full history** (checksum-verified
   binary) and fails on any forbidden filename anywhere in history.
4. GitHub: secret scanning + push protection, Dependabot alerts, private
   vulnerability reporting, branch protection on `main` (no force-push,
   no deletion).
Never disable any of these, change repo visibility, add collaborators, create
deploy keys/webhooks, or loosen branch protection without the user's explicit
approval in chat for that specific action.

### If something private is ever pushed
Stop and tell the user immediately. **A leaked key is rotated first** (Finnhub
/ NVIDIA dashboards — the user does this), cleanup second. Do not attempt a
silent force-push: it is blocked by branch protection, does not remove the data
from forks/PR refs/caches, and hides the incident from the user. Removal from
GitHub's side needs a GitHub Support request by the owner.

### Code rules that keep the attack surface near zero
- The server binds **127.0.0.1 only**. Never `0.0.0.0`, never a tunnel, never
  CORS `*` on anything that reads or writes user state.
- Keys load only through `helpers._load_local_secret` (env var, then
  `config.json` in the data folder, then a gitignored legacy file). They never appear in code, logs, `/api/*` responses or the
  frontend — `/api/runtime-status` exposes booleans only.
- No new outbound hosts, CDNs, analytics or telemetry without asking (today:
  Yahoo Finance, Finnhub, NVIDIA NIM, KaTeX CDN, and GitHub Releases for the
  one-time model download — `model_fetch.py`, §4).
- No `eval`/`exec`, `shell=True`, `pickle`/`joblib.load` of anything downloaded,
  or `yaml.load` without `SafeLoader`. Anything downloaded at runtime (model
  artifact — `model_fetch.MODEL_SHA256`, checked before the archive is opened;
  future reference packs) is verified against a SHA-256 pinned in code.
  Release assets are public the moment they are uploaded: creating one needs
  the user's OK, and an existing tag's asset is never replaced.
- Dependencies: only well-known packages, declared in both manifests (§4
  envcheck rule). Review Dependabot PRs like any other change; never auto-merge.
  Version updates are configured in `.github/dependabot.yml` (pip + github-actions, weekly).

### GitHub Actions
- Workflow default `permissions: contents: read`; grant more per job only when
  needed. Third-party actions pinned to a **commit SHA** with the tag in a
  comment. Never `pull_request_target`, never echo secrets, never write
  secrets into artifacts or caches. Downloaded tools are checksum-verified.
- Collector/scheduled workflows (future) read keys from Actions secrets only
  and publish derived data, never raw licensed article text.

### Periodic check (before each release, or when the user asks "is it secure?")
```bash
gitleaks git --redact .                                   # full history
uvx pip-audit --disable-pip -r <(uv export --frozen --no-emit-project --all-extras)   # known CVEs in uv.lock
gh api repos/ithakis/Convexity/secret-scanning/alerts --jq length
gh api repos/ithakis/Convexity/dependabot/alerts --jq '[.[]|select(.state=="open")]|length'
```
Report the results plainly, including "all clean".

### Rename history (for context)
v1.13.0 renamed `portfolio_tracker` → `convexity` everywhere (package, state
files, `~/.convexity/`, bundle ID `com.ithakis.convexity`, log
`~/Library/Logs/Convexity.log`). `convexity/migrate.py` moves old files on first
launch (delete after 2027-06-30).
v1.14 then moved all of it out of the checkout into the data folder (§2, §4
"Where user data lives"); the same module does that, copy → verify → remove.

