# Convexity — Engineering Notes for AI Agents

This file is the source of truth for AI agents working on this codebase.
It captures the architecture, conventions, gotchas, and design contracts
that aren't obvious from the code alone. Update it whenever you add a
new system or change an established pattern.

Since Phase 7 it is split: this file holds the rules, the map and an index
of the gotchas (§0); the deep sections are in `docs/architecture/*.md`.
Update whichever file owns the section you changed, and keep the §0 table
and gotcha index in step.

---

## 0. Where everything is

This file keeps the rules and the map: §1–3 (what the app is, file layout,
how to run it safely), §9 conventions, §10 open work, §15 version policy and
§18 security, all in full. The deep sections moved to `docs/architecture/`
in Phase 7 — nothing was dropped. `§N` references anywhere in the notes keep
their original numbers:

| § | Topic | Where |
|---|---|---|
| 1–3 | What the app is, file layout, how to run / restart | this file |
| 4 | Backend: caching, data folder, persistence, symbol resolution, streaming build, analytics, FX, HTTP routes | [docs/architecture/backend.md](docs/architecture/backend.md) |
| 4 | News & Sentiment: News read, Market read, model download/release, Track record | [docs/architecture/news.md](docs/architecture/news.md) |
| 4 | Request-origin guard, Settings → API keys (`keys.py`) | [docs/architecture/security.md](docs/architecture/security.md) |
| 5, 8, 11 | Frontend, CSS / theming, line landmarks | [docs/architecture/frontend.md](docs/architecture/frontend.md) |
| 6, 7 | Symbol DB (`convexity build-symbols`), Excel export | [docs/architecture/backend.md](docs/architecture/backend.md) |
| 9, 10 | Conventions, open work | this file |
| 12 | Black-Litterman + mean-CVaR optimizer | [docs/architecture/mpt.md](docs/architecture/mpt.md) |
| 13 | CI, pre-commit, merging and making a release live | [docs/architecture/ci.md](docs/architecture/ci.md) |
| 14 | Desktop app, installers, icons | [docs/architecture/desktop.md](docs/architecture/desktop.md) |
| 15 | Version tracking and releases | this file |
| 16, 17 | Log console (`logbuf.py`), background refresh jobs (`jobs.py`) | [docs/architecture/jobs.md](docs/architecture/jobs.md) |
| 18 | Security & public-repo rules | this file |

Read the section for the code you are about to touch before touching it.

### Must-read gotchas (index)

Each of these has cost real debugging time; the link goes to the full story.

- **Never run against the user's real data** — every run sets
  `CONVEXITY_HOME` to a temp dir; the checkout's legacy key files load in any
  checkout run, so no news refreshes or key Test calls. §3 below.
- **iCloud hides `.venv`** in `~/Documents` — the real venv is `.venv.nosync`
  behind a `.venv` symlink. §3 below.
- **The installed app is a uv tool, not the checkout** — editing the checkout
  changes nothing the user runs until reinstalled. §3 below.
- **One row per symbol, everywhere** — duplicate symbols 500 the analytics.
  [backend.md → Persistence](docs/architecture/backend.md#persistence)
- **Yahoo's units are not uniform** — `debtToEquity` and `dividendYield` are
  percent, margins are fractions, LSE prices are pence while caps are pounds, and
  ADR multiples mix currencies. Store one unit at ingestion; compare caps in USD.
  [backend.md → Streaming row build](docs/architecture/backend.md#streaming-row-build-apiquotes-stream-ndjson)
- **Don't put heavy fetches in `fetch_one`** (the streaming hot path).
  [backend.md → Streaming row build](docs/architecture/backend.md#streaming-row-build-apiquotes-stream-ndjson)
- **Request-origin guard** — every frontend POST sends
  `Content-Type: application/json`, no `sendBeacon`, never a `do_OPTIONS`.
  [security.md](docs/architecture/security.md#request-origin-guard)
- **Any module that caches an API key needs a `reload_keys()`** hooked into
  `keys.reload_all()`. [news.md](docs/architecture/news.md)
- **Two news engines, never blended**; re-run `scripts/benchmark_news_read.py`
  after any prompt or model change; train/serve parity (`ml_features.py`,
  `relevance.py`) is the Market read's invariant. [news.md](docs/architecture/news.md)
- **A dependency declaration is not a dependency** (`envcheck.py`), lightgbm
  needs Homebrew `libomp`, and the installers resolve from pyproject ranges,
  not `uv.lock`. [news.md](docs/architecture/news.md)
- **Topbar single-class CSS overrides lose the cascade** — prefix with
  `.topbar`. **Overlays open/close only through `showOverlay()` /
  `hideOverlay()`.** Settings sections live in `SETTINGS_SECTIONS`; the theme
  control uses `data-theme-opt`, never `data-theme`.
  [frontend.md → Key UI behaviours](docs/architecture/frontend.md#key-ui-behaviours-added-in-passes-abc)
- **The MPT cloud and frontier share one feasible set**; re-run
  `pytest tests/test_metrics.py -k cvar_pdip` after any solver edit.
  [mpt.md](docs/architecture/mpt.md#the-cloud-and-the-frontier-must-share-one-feasible-set-v1113)
- **Desktop: `--single-process` is load-bearing** (blank window otherwise),
  and `shutdown_server()` must `os._exit(0)`.
  [desktop.md → Gotchas](docs/architecture/desktop.md#gotchas-discovered-during-implementation)
- **Windows PowerShell 5.1 ≠ pwsh 7** for the installer (native stderr, TLS,
  `[Uri]`). [desktop.md → Gotchas](docs/architecture/desktop.md#gotchas-discovered-during-implementation)
- **Refresh jobs are single-flight, a dead client is not a cancel, and a
  degraded batch is never saved.**
  [jobs.md](docs/architecture/jobs.md#four-rules-that-are-load-bearing)
- **All backend output goes through `print()` + the `logbuf` tee** — don't
  convert to `logging` without keeping the tee. [jobs.md](docs/architecture/jobs.md)
- **After a merge, the new version is not live** until pulled, tagged and
  reinstalled. [ci.md](docs/architecture/ci.md#merging-prs-without-leaving-claude-code)
- **The reference pack is data, never code** — allow-listed JSON only, any
  failure ignored and logged; the builder reads its Finnhub key from the
  environment only; the `reference-pack` release must never become "Latest"
  (the installers install `/releases/latest`).
  [news.md](docs/architecture/news.md) · [ci.md](docs/architecture/ci.md#reference-pack-workflow-githubworkflowsreference-packyml-phase-8)
- **ruff check + format are blocking in CI.** [ci.md](docs/architecture/ci.md)

---

## 1. What this app is

Single-user, local-only portfolio dashboard. Runs as a Python HTTP server
on `127.0.0.1:8765` and prints its URL — it does **not** auto-open a browser
(removed in v1.12.3 at the user's request); or runs in a native PySide6 window — §14. No accounts, no network calls except to yfinance
(Yahoo Finance), Finnhub (news), NVIDIA NIM (the News read) and this repo's
GitHub Releases (the one-time model download and the daily reference pack),
no build step. Almost all logic lives in the `src/convexity/` package, split
into focused modules (server, fetcher, analytics, fx, persistence, etc. —
see the table below). The `convexity` command is `cli.main()`: no
arguments runs the server, `convexity build-symbols` builds the symbol DB,
`convexity build-reference-pack` builds the reference pack (CI).
(The root `dashboard.py` shim and `build_symbol_db.py` were removed in
Phase 7.)

User profile: quantitative-finance background, power user, runs the app
locally on macOS, expects complete autonomous delivery on requests, fast
iteration, no hand-holding. When in doubt about scope, ship a working
slice and document follow-ups rather than asking permission for each
sub-decision.

---

## 2. File layout

```
.
├── src/convexity/               ← The package (src layout since Phase 7). Everything below is imported by server.py or desktop.py.
│   ├── __init__.py
│   ├── __main__.py              ← `python -m convexity [build-symbols]` → cli.main()
│   ├── cli.py                   ← The `convexity` command: no args = server, `build-symbols` = symbol DB builder — §6, `build-reference-pack`
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
│   ├── reference_pack.py        ← Reference pack: format + allow-list validators, daily download, anchor/Track record readers — §4
│   ├── reference_build.py       ← `convexity build-reference-pack` (run by CI): S&P 500 Market read → the pack — §4, §13
│   ├── data/                    ← Package data: lm_lexicon.json, sp500.json (the pack's universe, source + date inside)
│   ├── keys.py                  ← Settings → API keys: config.json write/clear, live reload, Test calls — §4
│   ├── desktop.py               ← Desktop app entry point (PySide6 + QtWebEngine) — §14
│   └── static/
│       ├── index.html           ← Main HTML template
│       ├── app.js                ← All frontend JS (state, columns, rendering, panels) — §5
│       └── style.css             ← CSS (themes, layout, components) — §8
│   └── assets/icon.svg          ← THE logo (master); icon.png + icon-rounded.png are generated by scripts/build_icon.py, package data — §14
├── tests/                        ← pytest suite; tests/data/news_gold.jsonl is the News read gold set
├── docs/                         ← Reference/audit notes not needed to run the app day-to-day
│   ├── architecture/             ← The deep sections of these notes (backend, news, security, frontend, mpt, ci, desktop, jobs) — §0
│   └── plans/distribution-roadmap.md  ← Distribution/packaging roadmap (Phases 1–9)
├── scripts/                      ← Dev scripts (check_syntax.py, smoke_test_server.py, benchmark_news_read.py, …)
├── ml/                           ← FNSPID training pipeline for the Market read — docs/ml_sentiment_design.md
├── pyproject.toml                ← THE dependency manifest + entry points (`convexity`, `convexity-app`) — §3, §4
├── uv.lock                       ← uv lockfile solved from pyproject.toml (CI installs exactly this) — §13
├── .python-version               ← 3.14, the interpreter `uv` uses for this checkout
├── install.sh                    ← Installer, macOS/Linux: uv + `uv tool install` of the latest release + launcher — §14. Stays at the root: it is the curl target
├── packaging/
│   ├── install.ps1               ← The Windows installer (same steps, Start Menu + Desktop shortcuts) — §14
│   ├── update.sh / update.ps1    ← Thin wrappers: re-run the installer (that is the update) — §14
│   └── Launch Dashboard.command  ← macOS dev launcher: `uv run convexity` from this checkout (cds to the repo root), restarts cleanly
├── README.md
├── CHANGELOG.md                  ← Every version → what changed (release notes are copied from it) — §15
├── SECURITY.md                   ← How to report a vulnerability (public repo)
├── AGENTS.md                     ← Pointer to this file for non-Claude agents
├── CLAUDE.md                     ← This file (rules + map); deep sections in docs/architecture/
├── .github/                      ← CI + reference-pack workflows, Dependabot, issue forms (bug, feature) — §13
├── LICENSE
├── .finnhub_key / .nvidia_key     ← LEGACY key files (gitignored); read as a logged fallback in 1.14, then config.json
└── .openrouter_key               ← Legacy OpenRouter key (superseded, still gitignored)
```

**User data is not in the repo (v1.14, roadmap Phase 3).** It lives in the
per-user data folder resolved by `src/convexity/paths.py` (`CONVEXITY_HOME`
overrides it):

```
~/Library/Application Support/Convexity/   (macOS; Windows %APPDATA%\Convexity\, Linux $XDG_DATA_HOME/convexity/)
├── config.json                 ← API keys {finnhub_api_key, nvidia_api_key}, 0600 — written by Settings → API keys (keys.py)
├── symbol_db.sqlite            ← Built by `convexity build-symbols` — §6
├── state/
│   ├── views.json              ← Per-portfolio cached rows + metadata + weight presets
│   ├── watchlists.json         ← Per-portfolio entries strings
│   ├── mpt.json                ← Saved MPT efficient-frontier runs per portfolio
│   ├── column_views.json       ← Custom column-view definitions
│   ├── news.json               ← News + sentiment cache, LLM status
│   ├── sentiment_history.json  ← One record per ticker-day read: Track record + live anchor
│   └── reference_pack.json     ← {"enabled": bool} — the Settings switch for the reference pack
├── models/mlsent-v1.1/         ← The Market read artifact (was ~/.convexity/ml_model/)
├── reference/                  ← The downloaded reference pack (a cache; reference_pack.py)
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
./packaging/"Launch Dashboard.command"
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
**Never run `convexity build-reference-pack` or a news refresh from the
checkout** with its legacy key files in reach: use a clean `git archive`
install (`.claude/skills/verify/SKILL.md`), a `--limit` of a few names and a
local Finnhub stub (`CONVEXITY_FINNHUB_BASE`); for the app, point
`CONVEXITY_REFERENCE_URL` at a locally served pack.

**Critical gotchas when restarting:**

1. **System Python is missing yfinance** — always go through `uv run`
   (or the tool's own interpreter). `python -m convexity` with a bare
   interpreter crashes on import.
2. **Port 8765 after a restart** — fixed in v1.14.2: `_pick_port()`'s probe
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
   pick up changes to `symbol_db.py` or `xlsx_export.py` (or any other
   module). No autoreload.
5. **Background warm-up** — the server runs `warmRecentTabs()` +
   `preloadFxIndexes()` ~1.2s after startup. Those write yfinance
   deprecation warnings to stdout — they look scary but are harmless.

---

## 9. Conventions

### Code style
- **Modular by concern** — the backend lives in `src/convexity/` as
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
- After non-trivial edits: `uv run ruff check . && uv run ruff format .`
  (CI blocks on both) and `scripts/check_syntax.py`.
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
issues, never back into a file (the issue forms in `.github/ISSUE_TEMPLATE/`
apply `bug` / `feature`). Distribution/packaging work follows
`docs/plans/distribution-roadmap.md` — Phases 0–8 done on `distribution`
(pushed; CI green; the reference-pack `yahoo-check` passed from GitHub — its
`build` run waits for the owner to create the `reference-pack` release);
Phase 9 (website) remains, then the single v2.0.0 release.

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

## 15. Version tracking

Single source of truth: `__version__` in `src/convexity/__init__.py`
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
- Terminal startup banner (`src/convexity/server.py`'s `main()`) — uses `__version_display__`.
- `GET /api/health` → `{"ok", "ts", "version", "version_date"}` — the frontend's source.
- The app footer/version tag lives in the Analyst Sentiment section's
  coverage-footer row (`.an-coverage-foot` in `app.js`'s
  `renderAnalystDashboard`, right-aligned via `.an-version`), populated by
  `loadAppVersion()` + `versionLabel()`. **Not** a fixed-position page
  overlay — that was removed (the old `#app-footer` div/CSS no longer
  exist); the version tag now only renders where the Analyst Sentiment
  section is in view, which was a deliberate user-requested tradeoff.
- Desktop app splash subtitle and main window title
  (`src/convexity/desktop.py`), both via `__version_display__`.

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
- Binding to loopback does not keep *the user's browser* out: any open web page
  can send requests to 127.0.0.1. The request-origin guard (§4 HTTP routes:
  loopback `Host`, same-origin `Origin`, JSON-only POSTs, no `do_OPTIONS`)
  is what stops cross-site writes and DNS-rebinding reads. Don't weaken it,
  and don't add a route handler that bypasses `do_GET`/`do_POST`/`do_DELETE`.
- Keys load only through `helpers._load_local_secret` (env var, then
  `config.json` in the data folder, then a gitignored legacy file). They never appear in code, logs, `/api/*` responses or the
  frontend — `/api/runtime-status` and `/api/keys` expose booleans only, and the
  Settings inputs are never prefilled and are emptied as soon as they are sent.
  Only `keys.py` writes `config.json`.
- No new outbound hosts, CDNs, analytics or telemetry without asking (today:
  Yahoo Finance, Finnhub, NVIDIA NIM, KaTeX CDN, and GitHub Releases for the
  one-time model download — `model_fetch.py` — and the daily reference pack —
  `reference_pack.py`, §4; the latter can be switched off in Settings).
- No `eval`/`exec`, `shell=True`, `pickle`/`joblib.load` of anything downloaded,
  or `yaml.load` without `SafeLoader`. Anything downloaded at runtime (model
  artifact — `model_fetch.MODEL_SHA256`, checked before the archive is opened)
  is verified against a SHA-256 pinned in code — except the daily reference
  pack, which cannot be pinned (next bullet).
  Release assets are public the moment they are uploaded: creating one needs
  the user's OK, and an existing tag's asset is never replaced.
- **The one exception: the rolling `reference-pack` release.** Its three
  assets (`manifest.json`, `anchor.json.gz`, `history.json.gz`) are replaced
  by `reference-pack.yml` every weekday with `gh release upload --clobber`.
  The never-replace rule protects **code** that old installs pin by hash (the
  model); the pack is **data** that changes daily and cannot be pinned. What
  protects the app instead: HTTPS to this repo's releases only, the manifest's
  SHA-256 + size per file, hard size caps (download and decompressed), JSON
  only (never pickle, never executed), an allow-list schema (tickers, dates,
  numbers — no text field can load) and the model version; anything that fails
  is logged and ignored. It holds ticker, date and derived scores only —
  never headline, summary or URL — and nothing from users is ever uploaded.
  Creating the release is the owner's call (pre-release, not latest: the
  installers install `/releases/latest`); the workflow never creates it.
- Dependencies: only well-known packages, declared in both manifests (§4
  envcheck rule). Review Dependabot PRs like any other change; never auto-merge.
  Version updates are configured in `.github/dependabot.yml` (pip + github-actions, weekly).

### GitHub Actions
- Workflow default `permissions: contents: read`; grant more per job only when
  needed. Third-party actions pinned to a **commit SHA** with the tag in a
  comment. Never `pull_request_target`, never echo secrets, never write
  secrets into artifacts or caches. Downloaded tools are checksum-verified.
- Collector/scheduled workflows (today: `reference-pack.yml`, roadmap Phase 8
  — in this repo, not a separate one) read keys from Actions secrets only, in
  the one step that needs them; only the publishing job gets `contents:
  write`; they publish derived data to a release asset — never raw licensed
  article text — and **never commit** to the repo. `tests/test_workflows.py`
  checks these rules.

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
`~/Library/Logs/Convexity.log`). `src/convexity/migrate.py` moves old files on first
launch (delete after 2027-06-30).
v1.14 then moved all of it out of the checkout into the data folder (§2, §4
"Where user data lives"); the same module does that, copy → verify → remove.
