# Backend

[← CLAUDE.md](../../CLAUDE.md) · part of the engineering notes; `§N` references name the original CLAUDE.md sections (map in CLAUDE.md).

Sections: §4 (caching, data folder, persistence, symbol resolution, the streaming row build, analytics, FX, HTTP routes), §6 (symbol DB) and §7 (Excel export). The News & Sentiment part of §4 is in [news.md](news.md); the request-origin guard and `keys.py` are in [security.md](security.md).

## 4. Backend (`src/convexity/` package — server side)

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
where the next upgrade deletes them. Now `src/convexity/paths.py` is the only place
a data path is built: `data_dir()`, `state_file(name)`, `models_dir()`,
`logs_dir()`, `config_file()`, `symbol_db_file()`. Stdlib only, no import-time
side effects, never creates a directory — writers `mkdir` their own parent.
`_repo_root()` survives only as `paths.legacy_root()` for migration/fallbacks.

- **`CONVEXITY_HOME` is mandatory for every test and manual/dev run.**
  `tests/conftest.py` sets it at import time (modules bind paths at import,
  and `__init__` runs the migration) plus per test. A dev run is
  `CONVEXITY_HOME=$(mktemp -d) uv run convexity`.
- **Migration** (`migrate.migrate_to_data_dir`, from `__init__`, **only when
  the process is the app** — `convexity._launched_as_app()`: the `convexity` /
  `convexity-app` scripts, `-m convexity[.server|.desktop]`).
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
  (`MLSENT_MODEL_DIR` → `models/<ver>` → `~/.convexity/ml_model/<ver>`).
  Remove them after 1.14. (The symbol DB's fallback went with the old DB in
  2.0: the file is the downloaded symbol pack, §6.) A malformed `config.json` is logged once (`[config] ignoring …`),
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
7. **`_symbol_db_lookup`** — the symbol pack via `symbol_db.lookup()` (§6).
   Handles `"microsoft"`, typos like `"Microsft"`, initials like `"tsmc"`,
   company names like `"DaVita"`; returns the home listing. Threshold 72.
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
market_cap, market_cap_usd, currency, financial_currency,
exchange, sector, industry, quote_type, website,
ps_ratio, pe_ratio, forward_pe, peg, ev_ebitda, ev_revenue, price_book,
dividend_yield, debt_equity, fcf_yield, roe, roa, … (fundamentals),
error  (only on failure)
```

Adding a new field: extend `fetch_one` (`src/convexity/fetcher.py`), add
it to the column registry (`COLS` in `src/convexity/static/app.js`,
line ~6), and `xlsx_export.py`'s `HOLDINGS_PRIMARY_COLS` auto-picks up
extras through the "extras pass" mechanism.

**Yahoo's units and currencies — the contract (audit of 2026-09-29).**
Every value is stored in ONE unit, decided here, and the frontend formats it
without re-detecting anything:

| Field | Yahoo gives | Stored as |
|---|---|---|
| `debtToEquity` → `debt_equity` | percent (AAPL 78.4 = 0.78×) | percent, shown "78.4%" (`fmtPctNum`) |
| `dividendYield` | percent since yfinance 0.2.54 (AAPL 0.32 = 0.32%) | fraction, via `_normalize_dividend_yield` |
| margins, ROE/ROA, growth, payout, `trailingAnnualDividendYield` | fraction | fraction |
| `marketCap`, `dividendRate`, statement figures | MAJOR unit (GBP for an LSE name) | major unit — format with `majorCcy()` |
| price, targets, 52-week range | QUOTE unit (GBp / ZAc / ILA pence) | quote unit — format with the row currency |
| statement figures (revenue, FCF, debt, EBITDA) | `financialCurrency` | `financial_currency` on the row |

- `_normalize_dividend_yield`: `dividendRate / price` first (the price scaled
  from pence to pounds for minor-unit listings — SHEL.L: 1.16 / 36.545 =
  3.2%, not 0.03%), then `dividendYield / 100` **always** (the old `> 1`
  guess read every yield under 1% as a fraction: 0.32 → 32%), then the
  trailing fields only when the statements share the listing currency (an
  ADR's trailing rate is in the home currency: TSM 26 TWD over a USD price).
- **ADRs and cross-currency listings** (TSM/TWD, TM/JPY, BABA/CNY, NVO/DKK,
  SHEL.L reporting in USD): Yahoo's own EV/EBITDA, EV/Revenue, P/S and P/B
  divide a trading-currency market cap by home-currency statements (TM: 6.3 and
  15.3 against 11.9 and 0.91 on the Tokyo line). `fetcher._currency_consistent`
  rebuilds them in the statement currency — EV = MC·fx + Total Debt − Total
  Cash, P/B = MC·fx / (Total Debt / (D/E/100)) — and sets them to None when no
  FX rate exists. Trailing/forward P/E and PEG are per-share ratios Yahoo gets
  right and are kept. It also writes `fcf_yield`, `market_cap_usd` (the one
  comparable size: cap-weighting, buckets, the BL prior, the Excel cap
  weights all use it via `analytics._market_cap_usd`) and `financial_currency`.
- **Not meaningful → None** (`fetcher._positive`): negative P/E, forward
  P/E, PEG, EV/EBITDA, P/B. A heat ramp that favours low values would
  otherwise paint a loss-maker as the cheapest name on screen.
- `pe_ratio` is trailing only (it used to fall back to `forwardPE`, putting a
  forward multiple in the trailing column for exactly the loss-makers).
- The 52-week range and ATH are the max/min of **dividend-adjusted daily
  closes** (`auto_adjust=True`), not Yahoo's intraday `fiftyTwoWeekHigh`
  (which is 0.0 for SHEL.L's low — unusable). Returns (`pct_*`) are
  therefore total returns; YTD is measured from the previous year's last
  close (`helpers._ytd_change`).

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
up matters: the key lives at the repo root, not inside `src/convexity/`,
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
exposure {by_sector, by_industry, by_bucket},
concentration {top5, herfindahl, effective_n},
warnings
```

**One wide fetch, then slice.** `_bulk_close` pulls holdings + every
benchmark + the sector ETFs (+ `^IRX` in USD) over `_WARMUP_YF[period]`
(≥200 trading days before the period start), FX-converts all of it
(`_apply_fx_to_closes` — the indices quote in EUR/JPY/KRW, and a USD display
still converts non-USD holdings), then `_period_slice()` keeps the window from
the **last close on or before** `_period_start()` — the base every period
return is measured from (YTD: the previous year's last close). Only the SMAs use
the warm-up, which is why SMA 200 spans the whole chart; `min_periods=1` means
an average starts on fewer bars only where nothing earlier exists (MAX, a
young holding). The calendar is days *a holding* traded — foreign indices are
ffilled onto it, never allowed to add their own holidays as zero-return days.
Same-day beta vs Nikkei/KOSPI is understated (they close before the US
opens); the Beta tooltip says so. `_stats` / `_relative` are module-level and
tested directly in `tests/test_metrics.py` and `tests/test_finance_math.py`.

The conventions (audit of 2026-09-29, `docs/METRICS_AUDIT.md`):
- **Portfolio** = daily-rebalanced to the weights (Σ w_i r_i,t, compounded).
- **Sharpe / Sortino** on excess returns r_t − rf_t: in USD rf_t is the
  13-week T-bill (`^IRX`, (1+y/100)^(1/252) − 1 per day); other display
  currencies have no Yahoo short-rate series and use 0. `stats.rf_source`
  and `stats.rf_ann` say which. Mean ×252 over sd ×√252; Sortino's
  downside deviation is the RMS of min(x, 0) over all days.
- `rel.ir` — information ratio: mean active return ×252 / TE.
- **Weighted P/E, P/S, EV/EBITDA** — weighted HARMONIC means over positive
  multiples (the look-through multiple); `weighted.coverage` gives the
  weight share behind each. Dividend yield stays an arithmetic mean.
- **Weighted market cap** — averaged in USD, returned in the display
  currency (`weighted.market_cap_ccy`); the size buckets use USD caps.
- **Rating distribution** — each holding's share of votes × its weight
  (raw vote counts let a 50-analyst name outvote a 5-analyst one 10:1).
- **Contribution** — Carino log-linked from the daily-rebalanced portfolio,
  so the column adds up to `stats.total_return` exactly.

The `analyst.holdings` list is a HUGE win for Excel export — it means we
don't need a separate parallel `Ticker.info` fetch when analytics
succeeded. `xlsx_export.build_workbook` does exactly this.

### FX
- **`fx_rates(base)`** — spot rates against the 6 majors. 30-min cache.
- **`usd_per_unit(ccy)` / `convert_amount(x, a, b)`** — spot conversion of
  single amounts (market caps, statement figures): the `fx_rates("USD")`
  cache first, one `ccyUSD=X` download for currencies outside it (TWD, KRW,
  DKK…), cached for the FX TTL (a miss for 10 min). Minor units map to
  their major currency (`helpers.major_ccy`: GBp→GBP, ZAc→ZAR, ILA→ILS).
- **`fx_index_history(base, period)`** — synthetic equal-weight index
  level (base vs the basket of other majors), the GEOMETRIC mean of the
  price relatives (as currency indices are), normalised to 100 at
  window start. 4-hour cache. Has a "bulk download → if empty, retry
  sequentially with jittered sleep + per-pair retry" hardening for
  yfinance rate limits.
- The frontend FX hover cache (`FX_INDEX_CACHE`) only stores successful
  results (`length >= 2`). Empty arrays are NEVER cached — fixes the
  old "no data sticks forever" bug.

### HTTP routes (`Handler` in `server.py`, ~line 92)

*(The request-origin guard that runs before every route below is in [security.md](security.md#request-origin-guard).)*

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
- `/api/quotes-stream` (preferred) — NDJSON streaming, the fast path
- `/api/search`                    — company search `{q}` or `{query}` (an edited chip set), `{offset}` — §6
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
- `/api/keys`                      — `{provider, action: set|clear, key?}` → key booleans; 400 bad key, 409 malformed config.json (§4 keys.py)
- `/api/keys/test`                 — `{provider}` → `{status, http, ms}`, one cheap provider call
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
- `/api/keys`                      — `{config_ok, config_path, finnhub|nvidia: {set, source, in_config, env_overrides}}` — booleans only (§4 keys.py)
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

## 6. Symbol pack + company search (`symbol_db.py`, `symbol_build.py`, `search.py`)

### Why
Finding a company should not need its ticker (issue #6). Until 2.0 the
symbol DB was a US-only NASDAQ/SEC table that only the `build-symbols` CLI
created, so fresh installs had none and a typo became a literal ticker row.

### The symbol pack (`symbol_db.py`, built by `symbol_build.py`)
`convexity build-symbols --out DIR` (run weekly by
`.github/workflows/symbol-pack.yml`, ~50 min) sweeps Yahoo's screener for
every listing: equities in all 59 regions (~236k), the same universe once per
industry (~145 sweeps — screener quotes carry no sector/industry, this is how
rows get them), ETFs (~57k, minus `^…-IV` indicative values), mutual funds
(~338k, minus Nasdaq test funds) and `data/indices.json` (the screener has no
indices). Two files go to the rolling `reference-pack` release:
`symbols-manifest.json` and `symbols.ndjson.gz` (a `{schema, columns}` line,
then one JSON array per listing).

**Yahoo serves at most 10,000 results per query** — past offset 9,750 every
page repeats the last one. `sweep()` therefore counts first and splits any
larger query into price decades (every listing has `intradayprice`), halving
a band until it fits; a band that cannot be split (1,000+ funds at $1.00) is
read ascending and descending by ticker. A first version paged naively and
turned 630k listings into 22k unique ones without an error.

Columns: ticker, name, type (stock/etf/fund/index), exchange, region, sector,
industry, mcap_usd, group, home. **Rules learned from the data:**
- A quote's `region` echoes the request; the listing's region comes from its
  exchange code (yfinance's region → exchange maps, unique per exchange).
- `marketCap` is in the listing's currency (OTP in HUF); converted to USD once
  at build time through `fx.usd_per_unit`. ETFs/funds/indices carry no size.
- **A company = every listing with the same cleaned name**, whatever Yahoo
  calls its type (it files Canadian depositary receipts like NOVO.TO as ETFs).
  Sector, industry and size fill in from whichever listing has them.
- **Re-listings** (`symbol_build._secondary`): German regional exchanges,
  Cboe/Aquis/LSE international boards (incl. `0XXX.L` order-book lines), OTC,
  Mexico/Santiago foreign boards, Brazilian BDRs (`XXXX3[1-9].SA`) and
  Argentine CEDEARs (a `.BA` line whose base ticker trades in the US). A group
  with **no primary listing is dropped** (MOH.SG for LVMH, SPY.BA under SPY's
  old name); Morningstar fund records (`0P…`) and indices have no venue.
- **Dropped too:** stock groups with no size and no industry anywhere —
  warrants, CBBCs and re-listings (~5.5k Hong Kong rows, ~1.4k Vienna).
- **Home listing:** among primary listings, quoted in the reporting currency
  **unless that is USD** (Shell, Zurich, Genmab report in USD but list at home
  elsewhere) **and only if liquid** (≥10% of the group's top traded value —
  Alibaba's RMB counter 89988.HK is "local" but thin), then the highest
  3-month traded value in USD. Gold set: NVO→NOVO-B.CO, TSM→2330.TW, LLY.F→LLY,
  ZURVY→ZURN.SW, 89988.HK→9988.HK (`test_symbol_build.py`).
- Result (2026-10): 472,558 listings → 378,606 searchable homes (43k
  companies, 18k ETFs, 317k funds, 40 indices), 8.5 MB gzipped, ~50 min.
- Never published: a sweep under 95% of Yahoo's own total, or a pack >20%
  smaller than the previous one.

App side: `symbol_db.start()` at boot (and with the reference-pack switch /
"Check now") downloads when the local copy is older than 7 days, verifies the
manifest hash + size + allow-list (names are the one free-text field:
printable, ≤160 chars, only displayed or fuzzy-matched) **while streaming**
the NDJSON into a new SQLite file (parsing it whole took ~550 MB; streamed it
peaks under 90 MB), checks the row count against the manifest and unique
tickers via the primary key, then `os.replace`s it in (stale `-wal`/`-shm`
removed first; retried on Windows). `PRAGMA user_version = 2`; an older file
reads as empty and is downloaded again. **Bump `DB_VERSION` whenever
`name_key()` or `acronym()` change** — the file stores their output. The `symbols`
table keeps `ticker`/`name` because `relevance.load_company_names()` reads it
for the Market read (its cache is cleared on install).

`lookup(q)`: exact ticker (any listing) or `ALIASES` (google → GOOGL) first;
then **candidates from SQLite** — an FTS5 trigram index over the home
listings' cleaned names (legal forms dropped: "Novo Nordisk A/S" → "novo
nordisk"), top 300 by bm25, plus initials ("tsmc", "ibm") from a partial
index — scored with rapidfuzz WRatio and re-ranked exact > prefix > whole
word > fuzzy, shorter-is-better, then size (+≤6) and type (stock > index >
ETF > fund). Memoised per file version. An in-memory index of ~450k names
cost ~300 MB and ~1 s per WRatio scan; this costs ~7 MB and a few hundred ms
at worst. `category()` lists the largest
home listings for sectors/industries/regions; `alternates(group)` and
`home(group)` serve the cards.

### Company search (`search.py`, `POST /api/search`)
One query object, two parsers, one executor — the query is the audit trail:
the page shows it as chips, every search prints one `[search]` line.
- **`FIELDS`** — the registry (~20 screener fields), one row each: label,
  unit, screener field + factor, `.info` key + factor, aliases. **Units are
  verified, not assumed:** the screener stores D/E, ROE, margins, growth in
  percent; `.info` has D/E and dividend yield in percent but ROE/margins as
  fractions; growth has no matching `.info` figure (cards show a tick).
  Market cap never goes to the screener (local currency there): it is filtered
  and ranked in USD from the pack.
- **`parse_rules`** — metric/operator/number, sector/industry/region/type
  words; leftovers become `ignored` (a struck-through chip + a warning).
- **`parse_llm`** — only when words are left over, the query is a theme, or
  "best" needs a metric, and only with the NVIDIA key. Strict JSON schema whose
  enums are the registry and the screener vocabulary; output re-validated.
  Every AI pick must exist in the pack and is shown as its home listing; a
  theme's picks outside the sectors/industries the model itself set are
  dropped and named in a warning (a live test offered Philip Morris as a GLP-1
  maker). The rules' placeholder rank (market cap for "best") is left out of
  the hint, or the model copies it back instead of choosing a metric.
  Reuses `news_sentiment._nvidia_call(..., record=False, tag="search")`, so a
  search failure never touches the News LLM banner.
- **Pasted lists** — two or more comma/newline parts with no criteria are
  `kind: "list"` (`_run_list`, rules only): an exact ticker keeps that listing,
  anything else is its best name match; all on one page, misses named.
- **`run`** — names and pure sector/region screens offline on the pack;
  criteria through `yf.screen`, deduped to home listings, regions checked on
  the home listing (LLY.DE is not a European company); `Ticker.info` for the
  five cards shown. Results cached 10 min per query for "Show next 5". A chip
  edit posts `{query}` back: validated, never sent to the LLM.
- **Screener rules learned live**: yfinance shares one cookie + crumb across
  threads and any 4xx (a delisted ticker in a concurrent quote fetch) flips its
  cookie strategy and wipes the cookie, so for as long as the startup warm-up
  lasts (~35 s) every screen through it got a 401, retries included. Screens
  use their own session instead (`_yahoo_screen`: cookie from fc.yahoo.com,
  crumb from getcrumb, re-minted on 401/403), falling back to `yf.screen`. Its
  body is raw UTF-8 like yfinance's: Yahoo does not decode `\u2014`, so a
  JSON-escaped "Banks—Regional" matched nothing. A ratio ranking without a market-cap filter is among companies above
  $1B in USD (`_size_floor`, 4 pages fetched; a note says so), because over
  every listing it is led by microcaps with tiny equity. A lowest-first
  ranking adds `field >= 0` at the screener: negative D/E or P/E is negative
  equity or a loss, which Yahoo would sort to the top.

### Wiring
`resolver._symbol_db_lookup()` uses `lookup()`. With no pack yet (first
minute of a fresh install, or the switch off before any download) lookups
return nothing and the resolver falls through to `yf.Search`; search says the
list is downloading. Switching off stops the download; a copy already on disk
keeps working.

## 7. `xlsx_export.py`

### Design contract (mirror in app.js too)
**"Everything you can see in the app, in one file."** Each saved
portfolio → one sheet. When new columns / analytics / fields land in the
dashboard, extend `xlsx_export.py` so the export stays comprehensive.

Two places have the contract documented; keep them in sync:
1. Inline HTML comment next to the topbar `#export` button
   (`src/convexity/static/app.js`)
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
