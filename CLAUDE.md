# Portfolio Tracker — Engineering Notes for AI Agents

This file is the source of truth for AI agents working on this codebase.
It captures the architecture, conventions, gotchas, and design contracts
that aren't obvious from the code alone. Update it whenever you add a
new system or change an established pattern.

---

## 1. What this app is

Single-user, local-only portfolio dashboard. Runs as a Python HTTP server
on `127.0.0.1:8765`, opens itself in the user's browser. No accounts, no
network calls except to yfinance (Yahoo Finance), no build step. The
entire frontend is one embedded HTML string in `dashboard.py`.

User profile: quantitative-finance background, power user, runs the app
locally on macOS, expects complete autonomous delivery on requests, fast
iteration, no hand-holding. When in doubt about scope, ship a working
slice and document follow-ups rather than asking permission for each
sub-decision.

---

## 2. File layout

```
.
├── dashboard.py                ← ~9000 lines. Server + embedded frontend. THE app.
├── symbol_db.py                ← Local fuzzy ticker DB (provider-agnostic schema)
├── build_symbol_db.py          ← CLI to (re)build symbol_db.sqlite from public sources
├── xlsx_export.py              ← One-sheet-per-portfolio Excel export
├── mpt.py                      ← Modern Portfolio Theory primitives (CLA, Ledoit-Wolf, MC cloud)
├── Launch Dashboard.command    ← macOS launcher (activates QF12 conda env, restarts cleanly)
├── requirements.txt
├── README.md
├── TODO.txt                    ← User's product wishlist (read for context, don't edit)
├── COLUMN_CUSTOMIZATION_PROMPT.md ← Original Pass D spec from the user
├── PASS_D_PROMPT.md            ← Fresh self-contained Pass D prompt (use this for a clean handoff)
├── CLAUDE.md                   ← This file
├── icon.png
├── symbol_db.sqlite            ← Built from `python build_symbol_db.py` (gitignored, *.sqlite)
├── .portfolio_tracker_views.json     ← Per-portfolio cached rows + metadata + weight presets (gitignored)
├── .portfolio_tracker_watchlists.json ← Per-portfolio entries strings (gitignored)
├── .portfolio_tracker_mpt.json       ← Saved MPT efficient-frontier runs per portfolio (gitignored)
└── .portfolio_tracker_session.json   ← Legacy single-session file (auto-migrated, gone after first run)
```

`__pycache__/` and `*.sqlite` are gitignored. Don't add gitignore entries
for the JSON state files — they're already covered.

---

## 3. How to run / restart

```bash
# Recommended (uses the conda env the user actually has):
~/miniforge3/envs/QF12/bin/python dashboard.py

# Or use the launcher (handles existing-pid cleanup, conda activation):
./"Launch Dashboard.command"
```

**Critical gotchas when restarting:**

1. **System Python is missing yfinance** — always use the QF12 env path
   above. `python dashboard.py` without env activation will crash on
   import.
2. **TIME_WAIT on port 8765** — after a kill, 8765 sometimes stays in
   TIME_WAIT for ~30s. `_pick_port()` falls through to 8766, 8767, 8768,
   then any free port. Check `lsof -nP -iTCP -p <PID> | grep LISTEN` to
   find which port the new server actually bound to.
3. **Existing process check** — always `pkill -f "python.*dashboard.py"`
   before spawning a new one. The launcher does this via `.dashboard.pid`
   but ad-hoc spawns from the assistant don't.
4. **Python module caching** — restarting the server is the ONLY way to
   pick up changes to `symbol_db.py` or `xlsx_export.py`. Same for
   `dashboard.py` itself. No autoreload.
5. **Background warm-up** — the server runs `warmRecentTabs()` +
   `preloadFxIndexes()` ~1.2s after startup. Those write yfinance
   deprecation warnings to stdout — they look scary but are harmless.

---

## 4. Backend (dashboard.py — server side)

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

### Persistence
Two separate JSON files because they evolve independently:
- **`.portfolio_tracker_watchlists.json`** — `{name: entries_string}` —
  the source of truth for constituents.
- **`.portfolio_tracker_views.json`** — `{views: {name: {entries, rows,
  saved_at, stale}}, last_view: name}` — cached row payloads + stale
  flag + which view to restore on next launch. The `__current__` key is
  the ad-hoc / unsaved tab.

Renaming a portfolio touches BOTH files (`rename_watchlist` +
`rename_view`) — see `/api/portfolio/rename` handler. Failure on the
view side rolls back the watchlist rename.

### Symbol resolution pipeline (`resolve_symbol`, line ~559)
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

Adding a new field: extend `fetch_one`, add to the column registry (Pass
D will introduce one — for now, edit `COLS` in `dashboard.py` line
~3506), and `xlsx_export.py`'s `HOLDINGS_PRIMARY_COLS` auto-picks up
extras through the "extras pass" mechanism.

**Dividend-yield contract:** normalise Yahoo dividend yields to a
fraction at ingestion (`0.0315` = `3.15%`). Yahoo sometimes returns
`dividendYield` as a fraction and sometimes as a percent-like number
(e.g. `11.15` for `11.15%`); `_normalize_dividend_yield(...)` is the
shared guardrail. Prefer `dividendRate / price` when available, then
fall back to the raw yield fields with a `> 1 => divide by 100` fixup.
UI formatters and analytics should assume the normalized fractional
value, not re-detect units downstream.

### Analytics (`/api/portfolio-analytics-multi`)
`analyze_portfolios_multi(rows, weight_sets, period, display_ccy)`.
Returns either `{set_name: analytics_dict | {"error": ...}}` OR
`{"results": {...}}` OR `{"error": ...}` — handle all three shapes.

Per-set analytics_dict has these top-level keys (all important — none
flat strings except metadata):
```
period, display_ccy,           ← scalar metadata
weights_applied, active_symbols, missing_symbols,
series,                        ← time-series (skip in exports)
stats {total_return, ann_return, ann_vol, sharpe, sortino, calmar,
       max_dd, beta_spy, r2_spy, te_spy},
spy_stats {…same keys, vs SPY benchmark…},
nasdaq_stats {…same keys, vs NASDAQ benchmark…},
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

### HTTP routes (Handler at line ~7000)
**GET**
- `/`                              — INDEX_HTML
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
- `/api/efficient-frontier`        — compute long-only MPT frontier `{rows, lookback, frequency, rf, budget, current_weights, display_ccy}`
- `/api/mpt-runs`                  — save `{view, run}` MPT run (id auto-generated)

**DELETE**
- `/api/views/<name>`              — drop a view (cascades to MPT runs)
- `/api/watchlists?name=…`         — drop a watchlist (cascades to view delete)
- `/api/column-views/<name>`       — drop a custom column view (built-ins rejected with 400)
- `/api/weight-presets?view=…&name=…` — drop a per-portfolio weight preset
- `/api/mpt-runs/<id>?view=…`      — drop a saved MPT run

**GET (also)**
- `/api/column-views`              — `{builtins, custom, active}`
- `/api/weight-presets?view=…`     — `{presets: [...], active: name|null}` for a portfolio
- `/api/mpt-runs?view=…`           — saved MPT runs (metadata only) for a portfolio
- `/api/mpt-runs/<id>?view=…`      — full saved MPT run payload

---

## 5. Frontend (dashboard.py — embedded `INDEX_HTML`)

Single template literal starting around line ~2200, ending ~5500.
No frameworks, no build step. KaTeX is the only external dependency
(loaded from CDN, used only for column-guide formulas).

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
`heat`, `bg`, `sortValue`, `td_cls`. `BUILTIN_VIEWS` (just below COLS)
maps each preset (`Default`, `Fundamentals`, `Momentum`) to an ordered
list of column keys. Legacy names (`IB View`, `Trader View`) are still
accepted and normalised through the alias helpers so saved state migrates
without user intervention. `COLS_BY_KEY` is the lookup table; rendering goes
through `getActiveColumns()` which resolves the active view from
`STATE.activeViewName` / `STATE.customViews` / `STATE.activeColumnOverride`
(set when the user drags headers on a built-in preset — kept
in-memory until they Save-as-new or Reset). User-defined views persist
to `.portfolio_tracker_column_views.json` via `/api/column-views`.

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
- **Inline tab rename**: double-click a `.pf-tab-label` →
  `beginTabRename()` swaps the span for an input; Enter commits, Esc
  cancels, blur commits. Calls `/api/portfolio/rename`.
- **Loading chip** (`lc-anchor`, `lc-spin`, `lcHtml`, `lcShow`,
  `lcHide`): a small terminal-flavoured indicator. Braille spinner cycles
  ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ via CSS keyframes on `content`. Optional shimmer bar +
  tabular-numerics counter (e.g. `21·47`). Used in: streaming status,
  startup, analytics empty state, modal detail load, FX hover load,
  Excel export button. CSS at line ~2395, JS at line ~3740.
- **Export → Excel** (`exportXlsx`): client just hits
  `/api/export-xlsx`; server handles everything. Button shows the
  loading chip during the ~40s cold-cache export. The maintenance
  contract for "what goes in the file" lives both in the inline HTML
  comment next to the button AND in `xlsx_export.py`'s module docstring.
- **Topbar + column-bar hover copy**: actionable hover text on
  `Portfolio`, `Refresh`, `Export`, `Sort`, and `Fit to screen` uses the
  custom `data-tip` pseudo-element pattern, not native `title`, so the
  help text is consistently visible in-browser.
- **Fit to screen toggle** (`#cv-fit-toggle`): optional table compaction
  mode for dense presets. `applyTableFitMode()` computes a scale from the
  active columns' declared widths versus `.table-wrap` width and applies
  it through the `--table-scale` CSS variable. The active view should
  remain readable, but the explicit goal is "keep the current preset on
  screen before falling back to horizontal overflow."

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
`dashboard.py:_symbol_db_lookup()` is the bridge. Both `symbol_db.py`
and `symbol_db.sqlite` are OPTIONAL — if either is missing, the dashboard
still works, the lookup just gracefully returns `None` and the existing
`yf.Search` fallback runs.

---

## 7. `xlsx_export.py`

### Design contract (mirror in dashboard.py too)
**"Everything you can see in the app, in one file."** Each saved
portfolio → one sheet. When new columns / analytics / fields land in the
dashboard, extend `xlsx_export.py` so the export stays comprehensive.

Two places have the contract documented; keep them in sync:
1. Inline HTML comment next to the topbar `#export` button (dashboard.py)
2. Module docstring at the top of `xlsx_export.py`

### Per-sheet structure
1. **Overview sheet** (first) — every portfolio with headline metrics
   side-by-side.
2. **One sheet per portfolio**:
   - Meta block (name, constituents, cached-at, holdings count)
   - **Portfolio Metrics** — 4-column table Portfolio / SPY / NASDAQ
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
- **Single-file ethos** — dashboard.py is intentionally monolithic.
  Only split out into a module when the new code is genuinely
  independent (symbol_db, xlsx_export). Don't fragment for its own sake.
- **Top-of-block docstrings** that explain WHY, not WHAT. The user
  reads them; they're part of the deliverable.
- **Comments around tricky behaviour** — every non-obvious gate or
  retry has a comment explaining the failure mode it's defending against
  (see FX hover cache, sequential FX pair fallback, view rename rollback).
- **Performance-aware code paths** are explicitly labelled (e.g.
  "fast streaming path" vs "heavy detail path"). When in doubt, ask:
  "could this end up in the per-row hot loop?"

### Workflow
- After non-trivial edits: `python -c "import ast; ast.parse(open('dashboard.py').read())"` to syntax-check.
- After backend edits that change behaviour: restart the server.
  `pkill -f "python.*dashboard.py"; <conda-python> dashboard.py &`.
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

### Pass D — Column views (NEXT)
See `PASS_D_PROMPT.md`. The big spec piece still pending:
- Refactor `COLS` into a registry + active-selection model
- Add ~12 curated optional columns (% 1W/1M/3M/6M, Sector, Industry,
  Fwd P/E, EV/EBITDA, Analyst Rating, Target Upside, 52W High/Low)
- Three built-in presets: Default, IB View, Trader View
- Custom-preset save/load/delete with separate JSON file
  (`.portfolio_tracker_column_views.json`)
- Sort menu and `xlsx_export.py` HOLDINGS_PRIMARY_COLS should reflect
  the active view's columns at point of use

### Open items from user's TODO.txt
- Natural-language search for companies (would build on `symbol_db.py`)
- News + sentiment view per company
- Per-metric tooltips on hover (more analytics now have these; extend further)
- Improvements to the "contribution by 3y returns" table

### Done / archived (don't redo)
- ★ Save Watchlist button removal
- CSV → Excel export (Pass B)
- FX hover stale-cache fix
- Robust ticker input (Pass C) — colon syntax, prefix reversal, fuzzy DB
- Tab rename via double-click
- Loading chip system
- Square icon removal from Portfolio button
- NASDAQ blurb removal

---

## 11. Quick reference — current line landmarks in dashboard.py

(Approximate. Use `grep -n` to confirm before editing.)

| What | Where |
|---|---|
| Cache helpers | ~59–80 |
| Views/watchlists persistence | ~88–340 |
| Exchange prefix table + parser | ~344–460 |
| Google-colon normaliser | ~536 |
| `resolve_symbol` pipeline | ~559 |
| `fetch_one` (per-symbol row) | ~670 |
| FX layer (rates, history, basket index) | ~821–1000 |
| Analytics (`analyze_portfolios_multi`) | ~1815 |
| `fetch_detail` (modal payload) | ~1278 |
| Embedded HTML/CSS/JS begins | ~2200 |
| Loading-chip CSS | ~2395 |
| Topbar HTML | ~3290 |
| Loading-chip JS helpers | ~3740 |
| Modal skeleton | ~4162 |
| Build / stream wiring | ~4859 |
| Tab rendering + rename | ~4992–5290 |
| `exportXlsx` | ~5451 |
| Analytics request / render | ~5510 |
| FX hover cache + draw | ~6730–6810 |
| HTTP Handler (GET/POST/DELETE) | ~8500+ |
| `_pick_port` + `main()` | end of file |
| Weight-preset persistence | ~200–340 |
| MPT runs persistence | ~340–400 |
| `compute_efficient_frontier` | ~2690 |
| MPT overlay HTML | ~4790 |
| Mode pill bar (`renderModeBar`) | ~8970 |
| Weights popup + save/save-as/delete | ~8870–9050 |
| Inline name prompt | top-level overlay, JS ~9070 |
| MPT overlay JS (chart + slider + sidebar) | ~9260 |

---

## 12. `mpt.py` — Modern Portfolio Theory module

Standalone module so the dashboard stays dependency-light and the math is
testable in isolation. Implements **long-only, sum=1 Markowitz** with a
clean public surface used by `compute_efficient_frontier` in dashboard.py.

### Public API

```python
mpt.compute_returns(closes_df, freq) -> returns_df
mpt.annualize(returns_df, freq) -> (mu_series, cov_df)         # annualized
mpt.ledoit_wolf_shrink(cov, returns=None) -> cov_df            # toward constant-corr target
mpt.critical_line(mu, cov) -> [_TurningPoint]                  # Markowitz CLA
mpt.frontier_curve(turning, mu, cov, n_samples=100) -> [{ret, vol, weights}]
mpt.tangency_portfolio(curve, rf=0) -> {ret, vol, weights, sharpe}
mpt.monte_carlo_cloud(mu, cov, n_samples=25000, rf=0, seed=42) -> [(vol, ret, sharpe)]
mpt.portfolio_stats(weights, mu, cov, rf=0) -> {ret, vol, sharpe}
```

### Algorithm choices (and why)

- **Critical Line Algorithm (CLA)** for the frontier — produces the exact
  piecewise-linear path from max-return corner to min-vol corner in *one
  pass* (~5–50 ms for 50 assets). Avoids the naive
  `scipy.optimize.minimize` loop over target returns (1–5 s).
- **Ledoit-Wolf shrinkage** toward constant-correlation target. Pure
  NumPy, ~20 lines. Stabilises the covariance matrix for portfolios with
  ~15–60 assets where the sample covariance is noisy.
- **Monte-Carlo cloud is visual only** — Dirichlet-sampled long-only
  vectors, vectorised. 100k samples × 50 assets ≈ 200 ms. Mixes two
  concentrations (α=0.3 and α=1.0) so the cloud fills the feasible set
  uniformly rather than clustering at the centroid.

### `_PERIOD_YF` mapping for MPT lookbacks (in dashboard.py)
```python
_MPT_LOOKBACK_YF = {"1Y": "1y", "3Y": "3y", "5Y": "5y", "10Y": "10y"}
_MPT_BUDGETS = {
    "fast":     {"cloud":   4_000, "frontier": 40,  "label": "Fast (~2s)"},
    "standard": {"cloud":  25_000, "frontier": 120, "label": "Standard (~5s)"},
    "thorough": {"cloud": 100_000, "frontier": 250, "label": "Thorough (~15s)"},
}
```

### Frequency contract
- `daily` → 252 periods/yr, no resample.
- `weekly` → 52 periods/yr, resampled to W-FRI. **Default.** Best
  signal-to-noise for multi-asset, 1Y–5Y lookbacks; reduces cross-exchange
  holiday misalignment.
- `monthly` → 12 periods/yr, resampled to month-end.

### Performance contract
- Data fetch dominates: cold ~3–6 s for 20 symbols / 3Y weekly; warm ~50 ms
  (per-symbol `_BULK_CLOSE_CACHE` shared with analytics).
- CLA optimisation: <100 ms even for 50 assets.
- Monte-Carlo at "Standard" (25k): ~400 ms.
- End-to-end warm: <1 s. Cold "Standard": <8 s. "Thorough": <20 s.

### Reuse from dashboard.py
- `_bulk_close()` — price history with the same yfinance cache analytics uses.
- `_apply_fx_to_closes()` — currency normalisation when `display_ccy != "USD"`.
- `_PERIOD_YF` neighbour — kept separately as `_MPT_LOOKBACK_YF` because
  MPT only supports a subset of analytics periods.

### Extending
- **Black-Litterman / views**: extend `annualize` to accept prior views
  and a confidence matrix; CLA stays as-is.
- **Constraints** (sector caps, max-position): switch from CLA to
  scipy.optimize.minimize (SLSQP) with constraint dicts. Add `scipy` to
  requirements when this lands.
- **Risk parity / minimum-CVaR**: separate solver in `mpt.py`; the
  `/api/efficient-frontier` payload shape can absorb additional special
  portfolios alongside `tangency`/`min_vol`/`max_ret`.

### Saved MPT runs (`.portfolio_tracker_mpt.json`)
```jsonc
{ "runs": {
    "<portfolio name>": [
      { "id": "run_20260518T0901234567", "saved_at": "...",
        "params": {lookback, frequency, rf, budget, display_ccy},
        "symbols": [...], "missing": [...],
        "frontier": [{ret, vol, weights}, ...],
        "tangency": {ret, vol, sharpe, weights},
        "min_vol":  {...}, "max_ret": {...},
        "anchors":  {equal: {...}, cap: {...}, current: {...}},
        "meta":     {n_obs, fetch_ms, optimize_ms, total_ms, ...}
      }
    ]
  }
}
```
Capped at 30 newest runs per portfolio. Cascades on view rename/delete.

---

## 13. CI / Quality gates

### GitHub Actions (`.github/workflows/ci.yml`)

Runs on every push to `main` or `claude/**` branches and on every PR to `main`.
Three jobs (all non-blocking on CI for now; tighten when false-positive rate
is measured):

| Step | Tool | What it checks |
|---|---|---|
| Syntax check | `ast.parse` | Every `.py` file — catches grammar errors before server start |
| Static analysis | `pyflakes` | `symbol_db.py`, `mpt.py`, `xlsx_export.py`, `build_symbol_db.py` — undefined names, unused imports |
| Import smoke | `ast.parse` | `dashboard.py` parseable; `mpt.py` importable without full numba stack |

`dashboard.py`'s pyflakes step is `|| true` (non-blocking) because the
embedded HTML/JS strings generate false positives. Remove when a scoped
ignore strategy is in place.

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
