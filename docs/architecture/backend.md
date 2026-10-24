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
- **`_VIEWS_LOCK`, `_WATCHLISTS_LOCK`** — threading locks around the
  persistence JSON files. Always acquire before read-modify-write.

### Where user data lives (`paths.py`)
Nothing is ever written next to the code (an installed wheel once wrote the
user's watchlists into `site-packages/convexity/`, where the next upgrade
deletes them). `src/convexity/paths.py` is the only place
a data path is built: `data_dir()`, `state_file(name)`, `models_dir()`,
`logs_dir()`, `config_file()`, `symbol_db_file()`. Stdlib only, no import-time
side effects, never creates a directory — writers `mkdir` their own parent.

- **`CONVEXITY_HOME` is mandatory for every test and manual/dev run.**
  `tests/conftest.py` sets it at import time (modules bind paths at import)
  plus per test. A dev run is
  `CONVEXITY_HOME=$(mktemp -d) uv run convexity`.
- **No migration code.** The v1.13 → v1.14 move into the data folder (and the
  key-file / `~/.convexity/ml_model` fallbacks that came with it) was removed in
  1.27.2: only the owner ever had the old layout, and it was already migrated.
  Keys resolve env → `config.json`; the model is `MLSENT_MODEL_DIR` →
  `models/<ver>`. A malformed `config.json` is logged once
  (`[config] ignoring …`), never silently treated as "no key".
- `/api/health` carries `data_dir` (Settings → About shows it).

### Persistence
Two separate JSON files (in `<data>/state/`) because they evolve independently:
- **`watchlists.json`** — `{name: entries_string}` —
  the source of truth for constituents.
- **`views.json`** — `{views: {name: {entries, rows,
  saved_at, stale}}, last_view: name}` — cached row payloads + stale
  flag + which view to restore on next launch. The `__current__` key is
  the ad-hoc / unsaved tab.

**`prefs.json`** (`persistence.load_prefs` / `save_prefs`, 2026-10-11) holds
the settings that must outlive the page: `default_ccy` (Settings → General,
the currency the app opens in) and `inv_benchmark` (My Investments' "vs"
menu, a key of `investments.INV_BENCHMARKS`). The browser's storage is per
origin (port), and the desktop window's was wiped on every launch (desktop.md),
so neither could live there. Allow-listed keys and values only; anything else
in the file or a request is dropped, a malformed file reads as `{}`, and a key
sent as null is removed.

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

### Symbol resolution pipeline (`resolver.resolve_symbol`)
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
top of the file), and `xlsx_export.py`'s `HOLDINGS_PRIMARY_COLS` auto-picks up
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
call/symbol, warm build = zero.

**Finnhub supplemental columns (`finnhub_adapter.py`):** `fetch_one`
adds two optional row fields — `insider_mspr` (Form-4 Monthly Share
Purchase Ratio) and `rec_trend_fh` (6-month analyst recommendation
trend). They come from the Finnhub free API via
`from convexity import finnhub_adapter as _fh` in `fetcher.py`
(import at the top). The key is resolved by `helpers._load_local_secret`, shared
with `news_sentiment`: the `FINNHUB_API_KEY` env var first, then
`finnhub_api_key` in the data folder's `config.json` (Settings → API keys).
When neither is present, `_fh` returns `None` for every call and the three
fields are written as `None`, so the columns render `—` and the server log
stays clean.

**Key stays local — never commit it.** `.env.local`, `*.secret` and the old
`.finnhub_key` / `.nvidia_key` names are gitignored, and a pre-commit guard at
`.claude/hooks/check-secrets.sh` (wired as the first PreToolUse hook in
`.claude/settings.json`) blocks any `git commit` that either stages one
of those files or leaks the key value into the staged diff. The key lives in
`config.json` in the data folder (outside the repo), so it never travels
between checkouts; export `FINNHUB_API_KEY` to override it. The adapter is stdlib-only (`urllib.request`), self-caches with
TTLs (3600/1800/3600s), and each call is individually try/excepted so a
failing endpoint never breaks a row. Cold build = up to 2 calls/symbol
(within Finnhub's 60/min free budget at 5 workers); warm build = zero.
Only HTTP 429 logs one line.
The columns are `Rec Δ6M` and `MSPR` (Fundamentals preset). The third
Finnhub-style column, `EPS Surp.`, is now yfinance-sourced (see above).

**Finnhub's free plan covers US listings only (checked 2026-10-11).** Every
other ticker (REP.MC, TTE.PA, CCC3.DE…) answers HTTP 403 on recommendation,
insider-sentiment and company-news alike, while the same company's US line
answers: its home (KO for CCC3.DE), an ADR (TTE, NVO, SAP) or an OTC ADR
(REPYY, BNPQY). So every Finnhub call is made for `symbol_db.us_line(symbol)`
(through `finnhub_adapter.finnhub_symbol`, which also spells BRK-B as
BRK.B): the company group's US listings in the symbol pack, home first, then
exchange lines before OTC, then the busiest. It reads only the pack, so it is
safe in `fetch_one`. A company the pack has no US line for (SCGLY, Société
Générale's ADR, is missing from Yahoo's screener) is looked up by name with
Yahoo's Lookup by `symbol_db.discover_us_line`, a network call made only from
the news refresh; the answer, a miss too, is kept for the process and the
table uses it from the next build. With no US line Finnhub is not asked at
all, and a 403 is negative-cached like an empty answer (it used to be
retried on every build: three wasted calls per European row).
- **Rec:** Finnhub answers an ADR with the company's worldwide consensus (TTE
  comes back as TTE.PA). With no US line, or no answer, the trend is Yahoo's
  for the row's own listing — the `tk.recommendations` frame `fetch_one`
  already reads for `rating_dist`, so no extra request (PHIL.MI). The row
  carries `rec_src` ({source, via}) and `us_line` for the tooltips. Both
  sources now return four months, so the column's Δ spans three.
- **MSPR** is built from SEC Form 4, which only US companies file: a US
  company's German line gets it through its home (CCC3.DE via KO), a foreign
  company never does, ADR or not (EU insider dealings are published regulator
  by regulator; there is no free feed). The n/a cell says why.

**One analyst target, app-wide (`analyst_targets.py`, 2026-10-22).** The grid,
the per-holding consensus table, the optimizer's views, the Excel export and
My Investments read the row's `target_mean_price` (and `target_median` /
`target_low` / `target_high`); the stock window's coverage card reads
`fetch_detail`'s `target_*`; σ/μ reads `options_density._analysts`. All three
now come from one cached function, `analyst_targets.consensus(symbol)`
(6 h; a summary fallback or an empty answer only 30 min, since Yahoo answers
a burst with empty frames). Before, the row carried Yahoo's summary
(`targetMeanPrice`) while the stock window carried the firms' mean: CRWD read
$246.65 in the table and $285.02 in the window. The firms' list itself was
wrong too: **CRWD split 4:1 on 2026-07-02 and Yahoo never restates a firm's
target**, so a June target of $800 counted as $800 against a $240 price (the
4x band let most through). The rules, each with a test in
`tests/test_analyst_targets.py`:
- each firm's **latest** action; one without a target drops the firm, unless
  it only maintained/reiterated an unchanged rating (then its previous target
  stands). The old code dropped blank rows *before* taking the latest, so a
  firm that moved to Neutral without a target kept its old Buy target;
- **split-adjusted** by every split after the action's date (`fetch_one` passes
  the dates from its own history, `actions=True`, so no extra request);
- ≤ 365 days old, flagged stale past 182, within 4x of the price;
- mean, median, low, high and n from **that one list** (the card's range used
  to be Yahoo's while its mean was the firms');
- no firm list (most non-US listings) → Yahoo's summary, `source: "summary"`.
The row carries `target_n_firms` and `target_source`. A row saved before this
(no `target_source`) still holds the summary: `analytics._analyst_for` swaps
in the shared block for it, so the consensus table and the optimizer agree
with the window at once; the grid's Target Δ follows on the next Refresh.
A research line's targets are that line's block, scaled (`_research_row` runs
the same `_apply_targets`; σ/μ uses `analyst_targets.scaled`).

**What analyst targets predict, and how the app uses them (2026-10-22/23).**
Two point-in-time studies rebuilt, at each month-end, the consensus from the
targets known that day (each firm's latest, split-adjusted) and compared it
with the price 12 months later:
- 80 US large caps, 2021–25 (4,534 stock-months): the plain mean target
  ranked returns with IC 0.023 (t 0.5); "return-preserving" upside (each
  firm's upside when published, applied to today's price) 0.085 (t 1.6), with
  90-day recency weights 0.080 (t 1.9); rolling a target forward by its age or
  annualising its remaining horizon was worse. Mean ln(T/P) 0.116 a year
  against 0.091 realised.
- S&P 500, 2015–25 (~63k stock-months, IC by month, Newey-West lag 11):
  return-preserving upside 0.035 (t 1.6), but it loads on volatility (+0.29)
  and trailing volatility alone scored 0.061 on today's (survivor) index;
  volatility-neutral it fell to 0.013 (t 0.9), negative in 2015–19. Momentum,
  revision breadth and earnings surprise: ≈ 0. No composite passed the gates
  (IC > 0 in every sub-period, t ≥ 2.5, monotone deciles).
Conclusion: a target is the Street's expectation, not a return forecast. So
(1) the optimizer's views are the equilibrium plus a small relative tilt
(mpt.md, Views), (2) the stock window and the consensus table show what a
target rests on (firms, median age, stale count, 90-day raises/cuts, spread)
and say what it is (`targetFacts`, `TARGET_NOTE`), (3) σ/μ opens on the
market's μ, the analysts' curve opt-in at 50% trust by default, and
(4) the app keeps its own dated record, `state/target_history.jsonl` (one line
per day and symbol the first time a block is computed: date, price, mean,
median, n, source, median age; market data only), scored by
`analyst_targets.track_record` (weekly cross-sections of ≥ 8 names, Spearman
IC of relative ln(T/P) vs the realised 12-month log return, the close within
10 days of the horizon or the name is skipped, NW t lag 51, a verdict after
52 weeks) at `GET /api/target-track-record` and in Track record → Analyst
targets. Once it shows evidence (52 weeks, t ≥ 2, IC > 0) `view_ic` hands its
IC to the optimizer instead of the prior; it reads the cached evaluation only,
so the optimizer never fetches for it. This record has no survivorship bias,
which is why it, not a backtest, is what can raise the IC.

**The research line: analysts for a secondary listing (2026-10-18).** Yahoo
keeps a company's analysts, and much of its fundamentals, on its home or US
line only. Checked live: NOV.DE and CCC3.DE (and every other Xetra or Vienna
line of a foreign company tried) had no target, rating or analyst count (and
no options), while NOVO-B.CO had 24 analysts, NVO 12 and KO 23. `fetcher.research_line(symbol)` picks, among
`symbol_db.research_candidates` (the pack group's home line, then its US
line), the one with the most `numberOfAnalystOpinions`. NOV.DE gets NOVO-B.CO
over NVO.
- **Caching.** The choice is kept a week in `state/research_lines.json` (a
  miss a day; written atomically). A probe Yahoo didn't answer is not
  recorded.
- **What it fills.** `_apply_research` runs in `fetch_one` only when the row
  has no analysts, or no sector or USD market cap. Its fields are read by the
  shared `_row_fields` (exactly as a listing's own), cached
  `research_info|line` for 30 minutes.
  - Analysts (only when the row has none): targets × own price / research
    price, which absorbs FX, pence and ADR share ratios without a rate; the
    upside is the research line's. Rating, key and count are copied, and the
    ratings trend comes from the research line's `recommendations`.
  - Gaps only (never replaced): sector, industry, multiples, margins,
    `market_cap_usd` / `market_cap`, EPS surprises.
  - The row carries `research_symbol`, `research_ratio`, `research_n` and
    `research_analysts` (tooltips only, the owner's choice; skipped in the
    Excel export).
- **Cost.** Measured: +1–4 s per secondary listing the first time, warm
  unchanged (~0.7 s).
- **Other consumers:**
  - `fetch_detail` (`_apply_research_detail`, before the sector ETF is
    picked).
  - `fetch_events` (earnings dates, `earnings_via`).
  - `options_density._history` (`_research_analysts`: firms' targets scaled
    the same way).
  - `analytics._analyst_for`, which skips the own-symbol trio fetch for a
    borrowed row; the consensus table's `via`.
  - The frontend logo falls back to the research line's (`LOGO_ALIAS`:
    a young Xetra line borrows its US line's).
- **OID lines.** `_us_lines` also counts OID-board lines whose `region` is
  empty (1,029 OTC ADRs such as NSRGY, ISNPY and SCGLY were invisible to it).

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

**Bars for the chart types (v1.19).** The stock payloads (`/api/detail`,
`/api/history`) carry `ohlc: [[ts, open, high, low]]` on the same timestamps as
`history` (the close is not repeated; `helpers._ohlc_to_points`). The portfolio
has no traded bar, so `series.ohlc` / `series.volume` are **approximate**,
built by `analytics._portfolio_bars` from the OHLCV that `_bulk_close` already
downloads and now keeps beside the close (`cache._BULK_BARS_CACHE`, filled by
`_put_bars`, never fetched on its own; duplicate dates dropped there). The
bar is the exact form for the day's start-of-day weights w_i,t and trading
cost c_t: `X_t = P(t-1)·(1−c_t)·Σ w_i,t (1+r_i,t)·X_i,t/C_i,t` with
`P(t-1) = P(t)/((1−c_t)·Σ w_i,t(1+r_i,t))`:
X/C in the listing's own currency, r in the display currency (FX at the close
rate). It is an upper/lower bound on the true basket high/low (the holdings
need not peak together) and always contains the close. Wicks beyond
max(5× the median daily range, 3%) are capped (`_wick_cap`; Yahoo has bad
prints such as VOD.L 2007-10-09, high 357 on a 176 close; the client's
`clipBadWicks` applies the same rule to single stocks). Prices go out at 6
significant figures and volume at 4: these payloads are saved in
`views.json`'s analytics cache. Volume is Σ w·shares·close in the display
currency. If any weighted holding has no bars the keys are **empty lists**
(always present, so the client's backfill can tell a current payload from one
saved before v1.19) and the client draws a line — never a band from part of
the book. A malformed cached frame returns empty rather than 500ing the
analytics. The caveat lives in Settings → About (the user's call: not on the
chart). Returns and statistics never read these bars.
`tests/test_chart_bars.py` holds the invariants.

The conventions (audit of 2026-09-29, `docs/METRICS_AUDIT.md`):
- **Portfolio** = the weights set on the period's first day, left to drift
  with prices and reset on the rebalancing calendar (`rebalance`: D/W/M/Q/S/Y,
  or N = buy and hold), each reset paying `tc_bps` on what it buys and sells:
  r_p,t = (1 − c_t)(1 + Σ w_i,t r_i,t) − 1 (`_rebalance_days`, `_drift`;
  DeMiguel-Garlappi-Uppal net return). One shared setting in prefs.json
  (`persistence.TRADING_DEFAULTS`: monthly, 10 bps), sent by the client,
  defaulting to the prefs in the route and the Excel export, and part of both
  cache keys. Daily at 0 bps reproduces the old constant-mix index exactly
  (tested). A holding starting (or stopping) to trade between rebalances
  trades only itself: bought at its target weight, the others scaled pro rata
  (`_drift`'s `entry`), so "Never" stays buy-and-hold for what was owned; the
  first purchase is free. Request values go through
  `persistence.clean_trading` (unknown calendar or a non-finite cost, which
  JSON's NaN slips past `float()`, falls back to the defaults; 0–200 bps).
  `trading{rebalances, turnover_ann (one-way), cost_drag_ann}` goes out with
  the stats, and the client's disk cache keeps it (a cached payload without it
  is refetched once). `rebalances` counts calendar reset days that traded; an
  entry trade is in turnover and cost but not in the count (a late listing
  made a monthly year read 13). The Excel export's Read me states the
  setting its returns assume (`xlsx_export._trading_line`).
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
- **Contribution** — Carino log-linked from the day's weights, each holding
  paying for its own trades ((1−c_t)·w_i,t·r_i,t − tc·|Δw_i,t|), so the
  column adds up to `stats.total_return` exactly.

The `analyst.holdings` list feeds the Excel export's analyst columns; the
row fields are the fallback, so the export never calls `Ticker.info` itself.
`fetch_one` stores the target range (`target_low/high/median`) from the
`.info` it already has, so `_analyst_for` fetches it only for rows cached
before that (and never for a row with no mean target: no coverage, a fund).

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

### HTTP routes (`Handler` in `server.py`)

*(The request-origin guard that runs before every route below is in [security.md](security.md#request-origin-guard).)*

**GET**
- `/`                              — serves `static/index.html`
- `/api/views`                     — list of saved views (metadata only)
- `/api/views/<name>`              — full view payload (rows included)
- `/api/watchlists`                — `{name: entries_string}` map
- `/api/investments`               — My Investments page payload (`investments.book(ccy)`, `?ccy=` the display currency; the benchmark comes from `prefs.json`), computed by replaying the ledger. Never writes the book file. See "My Investments" below
- `/api/prefs`                     — the saved preferences (`persistence.load_prefs`), allow-listed
- `/api/investments/symbols`       — open positions only, no network (the Portfolio tab's pinned book pill)
- `/api/investments/lookup?q=…`    — company typeahead for the Add popover (symbol pack)
- `/api/investments/price?symbol=…&date=…` — that day's close as traded (Yahoo's split adjustment undone), for the price auto-fill
- `/api/fx-rates?base=USD`         — spot rates
- `/api/fx-index?ccy=…`            — one synthetic basket index
- `/api/fx-indexes-bulk?ccys=…`    — many in one call (sequential server-side)
- `/api/detail?symbol=…`           — heavy per-symbol payload for the modal
- `/api/export-xlsx?ccy=`          — Excel export of every saved portfolio and the book
- `/api/export-progress`           — `{step, done, total}` of the running export (the button's label)

**POST**
- `/api/quotes-stream` (preferred) — NDJSON streaming, the fast path
- `/api/search`                    — company search `{q}` or `{query}` (an edited chip set), `{offset}` — §6
- `/api/watchlists`                — upsert `{name, entries}`
- `/api/prefs`                     — merge `{default_ccy?, inv_benchmark?}` into `prefs.json` (null removes a key); answers what is saved
- `/api/investments/entries`, `…/entries/update`, `…/entries/delete`, `…/undo`, `…/redo` — change the real book; body carries `base_rev`. Answer the new page payload, or `{error, message}`: 400 bad input, 409 stale rev or a damaged file, 422 a change the ledger refuses
- `/api/investments/import`         — read `{text, files: [{name, type, data(base64)}]}` into rows to review (`importer.read`); writes nothing. 409 when there is no NVIDIA key, 503 when NIM didn't answer
- `/api/investments/import/check`   — dry run `{entries}` against the book: `{problems: {row: {kind, message, short?}}}`; writes nothing
- `/api/investments/import/apply`   — `{base_rev, entries, aliases}`: one journal item (one Undo), names the owner corrected remembered
- `/api/views/<name>`              — save view body `{entries, rows, set_last?}`
- `/api/last-view`                 — set the restore-on-launch target
- `/api/portfolio/rename`          — `{old, new}` — atomic rename of both files
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

### My Investments: the ledger and the book file (`ledger.py`, `investments.py`)
Built in nine phases (issue #1); what is left of the roadmap is the decisions and
the rules below, which code comments cite as "engine rules" and "alert research".

**Decisions agreed with the owner (2026-10-05)**

| Topic | Decision |
|---|---|
| Placement | Its own page, the first topbar button, left of Portfolio. Self-contained: no links into the research tabs, and hypothetical weightings are untouched. |
| Layout | Split: performance and holdings on the left (~70%), the attention rail on the right (~30%). |
| Book | One book, no accounts. A future "race against friends" would compare TWR %, so the TWR index is stored cleanly. |
| Headline | Value · Today $ and % · P&L (unrealised · realised · dividends) · TWR against the S&P 500 in pp · MWR p.a. with the timing effect (MWR − TWR). |
| Ledger | Buy, sell, dividend, deposit, withdrawal, fee, split. A quick-added holding is one synthetic buy. Every change can be undone. |
| Cost basis | Average cost, which matches what brokers show. |
| Import UX | A composer that takes text and any file, then a review step with confidence marks and inline questions, then Apply as one batch. |
| Alerts | X-ray rules, events, plan levels and plain price alerts. In-app, plus macOS notifications for high-severity items and a weekly recap. No email until accounts exist. |
| Providers | NVIDIA NIM only. |

**Engine rules ("engine rules" in code comments)**
- **Value the book with closes that are not dividend-adjusted.** `_bulk_close`
  uses `auto_adjust=True`; valuing with it while also booking dividends as cash
  counts every dividend twice. Use `adjusted=False` with Yahoo's dividend and
  split actions. The SPY benchmark stays total return.
- **Cash** is one pool in `base_ccy`. A trade settles at the trade-date FX rate,
  or at the entry's own `fx_rate`. A buy or fee that would push cash below 0
  becomes an *implied deposit*, derived on every replay and never stored; that
  is what makes a book of quick-adds work.
- **Splits:** quantities are kept in today's share count. Each entry records
  `qty_basis`: `trade` (as on the contract note) or `current` (what the broker
  shows today).
- **TWR:** r_t = (V_t + W_t)/(V_{t−1} + D_t) − 1. Inflows count at the start of
  the day, outflows at the end.
- **MWR:** XIRR (`scipy.optimize.brentq`), or None when the cash flows never
  change sign. Under one year, compare period figures, not annualised ones.
- **Contribution:** daily P&L over the start-of-day capital, linked with Carino
  (`analytics._linked_contribution`), plus a "Cash and fees" bucket; it adds up
  exactly to the TWR.
- **Risk share:** RC_i = w_i(Σw)_i / σ², with Σ from `mpt.annualized_cov(…, "ledoit")`.
- **FX split:** the price part is (P·q − C_loc)·FX_now; the FX part is
  C_loc·FX_now − C_disp. Prices go through `helpers.price_in_major` (LSE quotes
  are in pence).
- **Out of scope:** shorts (an oversell is refused), spin-offs, mergers and
  ticker changes. A holding with no price history is valued at its last trade
  price, with a warning.
- **Logs** record the operation, the entry id and the symbol only, never
  amounts: users paste logs into public issues.

*Acceptance test* (synthetic AAA in USD, numbers checked independently; it lives
in `tests/test_ledger.py`):

| Date | Entries | AAA close |
|---|---|---|
| 2025-01-02 | deposit 10,000; buy 40 @ 100, fee 10 | 100 |
| 2025-04-01 | deposit 5,000; buy 40 @ 125, fee 10 | 125 |
| 2025-07-01 | sell 30 @ 130, fee 10 | 130 |
| 2025-10-01 | dividend 50; fee 20 | 120 |
| 2026-01-02 | withdrawal 1,000 | 140 |

Expected: average cost **112.75**, realised **507.50**, quantity **50**,
unrealised **1,362.50**; dividends 50, fees 20, total P&L **1,900** (= value
15,900 − net deposits 14,000); **TWR 12.629086%**, **MWR 13.848000%**, timing
effect **+1.2189 pp**; contributions add up to the TWR (1e-9) and risk shares to 1.
Companions: trade basis vs current basis across a 2:1 split agree; GBp with FX
splits 975 USD into 650 price + 325 FX; quick-adds only gives TWR = price
return; an oversell is refused; an edit that breaks a later sell is refused.

**Alert research ("alert research" in code comments)**
- *Alert for Alerts* (Chen, Liu & Wen, SSRN 4466498): price-tracking alerts cut
  retail returns by about 1% over 6 months, through extra trading. Hence
  plan-based alerts, a calm rail, plain price alerts as `info` only, and only
  high-severity macOS notifications.
- Feedback on their own behaviour measurably reduces the disposition effect (the
  basis of the planned behavioural coach).
- Trading 212 reversed a card-based redesign within 10 days because power users
  wanted dense lists, and users complained when numbers did not match a hand
  calculation. Capitally separates FX from capital gains; Ghostfolio's X-ray has
  editable static rules; Kubera's import lets you review and correct, and
  remembers the corrections.
- Import review follows the agentic-UX rules: show what will happen before
  acting, signal confidence, keep an audit trail with undo, and ask when unsure.
- **`ledger.py` is pure** (no I/O): `replay(entries, base_ccy, fx_at, splits)`
  returns the whole book from the first entry, every time. Nothing derived is
  stored, so editing a 2023 trade can never leave a stale number. Average
  cost (trade fees in the cost), one cash pool in the base currency, and cash
  never below zero: a shortfall is an *implied deposit*. Quantities are in
  today's shares (`qty_basis` `trade` entries are scaled by the splits after
  their date; Yahoo dates a split on its ex-date, so "after" is strict).
  Same-day order: deposit, dividend, sell, buy, fee, withdrawal, so a buy
  and a sell of the same share on one day books the sell first and is refused
  if nothing was held. TWR is `twr_index` (a running index) and MWR is
  `period_mwr` (XIRR solved per period length); the acceptance test and the
  page's daily points both use them (below).
- **`investments.py` owns `state/investments.json`.** One lock around every
  read-modify-write; an atomic write after copying the old file to `.bak`; a
  malformed or newer-schema file is **never overwritten** (409, the page
  shows the message); every change carries `base_rev` (409 when stale) and
  replays the candidate ledger before saving, so the file never holds a book
  the engine refuses. Undo/redo are journal items of inverse ops
  (`{"put": entry, "at": i}` / `{"del": id}`, last 100). Logs carry the
  operation, entry id and symbol only, never amounts.
- **Units:** entry prices and amounts are in the major unit of the entry's
  `ccy` (GBP, not pence); the page converts to the quote unit for display.
  `price_on` undoes Yahoo's split adjustment (`auto_adjust=False` only drops
  the dividend one): NVDA on 1 May 2024 fills ~830, not 83.
- **A failed split fetch is not "no splits"** (Phase 4, 2026-10-11).
  yfinance swallows a failed download into an empty frame, and the old code
  cached that as "no splits" for 12 h: a pre-split `qty_basis: "trade"` buy
  replayed at face value, the ledger oversold and the page said "broken".
  `_fetch_splits` now returns None on an empty or failed history and `[]`
  only on a real answer. `market_splits` keeps the last good list per ticker
  (`_SPLITS_GOOD`, also in `state/splits.json`, market data only, so a
  restart during an outage still has it) and retries a failure after 60 s.
  A ticker Yahoo doesn't carry (delisted) never gets a list either, and
  yfinance reports it exactly like an outage ("no timezone"):
  `_not_on_yahoo` uses the page's cached price download to tell them apart
  (the benchmark fund has prices and the ticker has none = not on Yahoo: its
  trades count as entered, with a warning, asked again in 12 h).
  A trade-basis ticker with no good list at all is **pending**
  (`splits_unknown`): `book()` replays without its entries (as if never
  bought, cash flows included), lists it in `splits_pending` and as a
  figure-less last holdings row, and the page reloads itself every minute
  until it loads. A write that touches a pending ticker is refused ("try
  again in a minute"); the import dry run marks its rows `splits_pending`;
  `price_on` returns no price rather than one off by a split factor.
- **Validation** (`_clean`, `_load`): numbers must be finite, not booleans,
  and at most 1e12 (JSON has no infinity); `ccy` is three letters; editing an
  entry to another company drops the old currency and FX rate. A book file
  whose entries lack a date, a known type or a symbol is unreadable (409),
  never a 500.
- Network calls sit in small functions (`_closes`, `_bench_closes`,
  `_dividends`, `_fx_mult`, `_fx_now`, `_fetch_splits`,
  `_fetch_quote_ccy`) that the tests stub.

### My Investments performance (Phase 3: `investments._performance`)
- **Value the book with as-traded closes**: `_bulk_close(symbols, period,
  adjusted=False)` (one path with the adjusted closes, its own cache key). The book books dividends
  as cash, so a dividend-adjusted close would count each one twice. Yahoo's
  `auto_adjust=False` close is still split-adjusted, which matches the
  ledger's quantities in today's shares. The raw closes have their own cache
  key (`|raw`) and store no bars. The dividends that come with them (per
  current share, quote unit: pence for LSE) go to `cache._DIVIDENDS_CACHE`
  (`analytics.dividends_cached`). The default `_bulk_close` path is unchanged
  (a test pins its exact `yf.download` call). **The S&P 500 benchmark stays
  total return** (SPY, the adjusted path).
- **The daily series.** The calendar is every weekday from the one before
  the first entry (a zero day, so ALL includes day one) to today, plus any
  weekend entry dates. Closes are forward-filled. Before a share's first
  close, its last trade price is used (`ledger.trade_prices`, per current
  share). FX is `fx.fx_multipliers` (Yahoo's `ccyUSD=X` history crossed
  through USD, first rate before the history starts; today's spot with a
  warning when there is none). It is the only FX path: the replays' `fx_at`
  (`_make_fx_at`) reads it once per currency over the ledger's own dates. TWR follows the engine rules, r = (V + W)/(V₋₁ + D)
  − 1, as a running index (`ledger.twr_index`). Every period figure is a
  ratio of two of its points, at the server's `period_starts` base.
- **Two replays, one currency.** The page asks for its display currency
  (`book(ccy)`, `?ccy=` / a POST's `ccy`; unknown codes fall back to the
  base, and so does a display currency Yahoo has no rates for, with a
  warning: the book isn't broken). The replay in the book's base currency gives cash, deposits and
  withdrawals, converted at each day's rate, so uninvested cash moves with
  its currency. A replay in the display currency (entries' own `fx_rate`
  carried over by `_rebase_fx`) gives each holding's cost, realised profit,
  dividends and price/FX split at its own days' rates. Their difference is
  `headline.cash_fx` ("Currency on cash" in the profit card); it is 0 when
  the display currency is the base.
- **Opening holdings** (`ledger.is_opening`: a quick-add, `source: "quick"`,
  or an imported current holding, `opening: true`; also `source: "import"`
  with `qty_basis: "current"`, because Apply dropped the flag until
  2026-10-11 — `_clean` copies only `_FIELDS`, so `opening` is read from the
  page's fields) start their performance
  at that day's market value. `Day.openings` lets the series swap cost for
  value in the day's inflow, so the gap from the average price stays in
  lifetime Total profit and never becomes a one-day return.
- **Estimated dividends** (`ledger.estimate_dividends`). Each Yahoo payout
  with no recorded dividend of that share from 5 days before to 45 days
  after its ex-date is estimated as shares held at the previous close × the
  amount (gross). Matching is one to one, nearest first, preferring a
  recorded dividend after the ex-date, so a monthly payer is matched payout
  by payout and a recorded dividend always replaces its estimate. Estimates
  count **as paid out**: an outflow on the ex-date for TWR and MWR, a
  "Dividends, estimated" row in Total profit, never Value or Cash. The rail
  says when there are any.
- **The benchmark** (owner, 2026-10-11: as on Portfolios, plus MSCI World)
  is `INV_BENCHMARKS[bench_key()]`: SPY, QQQ, SXRT.DE (Euro Stoxx 50),
  1321.T (Nikkei 225), 069500.KS (KOSPI 200) or URTH (MSCI World). Each is a
  **fund**, so its adjusted close carries the dividends; Yahoo's ^STOXX50E,
  ^N225 and ^KS11 are price indices that would flatter the book by their
  yield. It is priced in its own currency, converted day by day
  (`mult[bench ccy]`), and `bench_vol` is its volatility on the same trading
  days (the Volatility card). The pick is read from `prefs.json` by
  `book()`, so every route that answers the payload uses it. The book file's
  own `settings.benchmark` is unused.
- **The benchmark line, one per period** (owner, 2026-10-11: the two lines meet
  at the left edge of every pill). `bench_value[p]` starts with your value on
  the period's base day, buys the benchmark (total return, in the display currency)
  with each later performance inflow at the previous close (an opening
  holding's value at the day's close, as it enters your value) and sells for
  every outflow at the day's close, **estimated dividends included**: they
  leave your value, so the S&P pays the same out (a book that is only SPY
  sits exactly on its line). `bench_index` is SPY's own index, so the
  Return card's S&P figure compares like with like.
- **Risk** (`_risk`): volatility, Sharpe, Sortino and max drawdown of the TWR
  index inside each period, with the Portfolio tab's formulas
  (`analytics._stats`), over trading days only (a weekday with no close for
  SPY or any holding is a holiday, dropped so it can't shrink the
  volatility). Excess returns over ^IRX (`_rf_closes`) in a USD view, over
  0 in any other currency, as `analytics._daily_rf`. Ten daily returns at
  least, else null.
- **Payload.** `series{dates, value, invested, bench_value{period},
  twr_index, bench_index, period_start}`; per period `twr`, `bench_twr`,
  `twr_ann`, `mwr` (the period's own money-weighted return), `mwr_ann`,
  `years`, `since_start` (the book is younger than the pill),
  `value_change` (value now − value at the base), `net_in` (money put in less
  taken out after the base), `vol`, `sharpe`, `sortino`, `max_dd`,
  `bench_vol`. Top level: `benchmark{key, label}` and `benchmarks[{key, label}]`.
  `month_abs` / `month_pct` are the 1M window's gain and TWR (the hero's
  "past month"; the holdings' Month column uses the same base). Returns are cumulative.
  The page shows Your money per year when `years` ≥ 1, else the period
  figure. `ledger.period_mwr` solves per period length, so a short period
  can't overflow the annualising. With no closes at all there is no
  `series`, and every period figure is null (a dash).
- **Refresh** on the page runs `jobs._run_book_phase` (the `__investments__`
  sink, jobs.md): `investments.refresh_market` drops the book's raw closes,
  dividends, splits, quote currencies, the FX histories and the benchmark, then fetches
  them again four symbols at a time. It never writes the book file.

### My Investments holdings depth (Phase 4)
- **Contribution** (`_contrib_daily`, `_contrib_linked`): each holding's day
  P&L (value change, less the money put into it from `ledger.Day.flows` with
  fees left out, plus its dividends, recorded or estimated; an opening
  holding enters at that day's value) over the TWR's own denominator
  V_{t-1} + D_t. The rest of each day's return is `CASH_BUCKET` ("Cash and
  fees": fees, and in another display currency what the currency did to
  cash), so the row sums are the TWR's daily returns exactly. Per period,
  `analytics._carino_link` makes them add up to the period's TWR
  (`headline.contrib[period][symbol]`, points; closed holdings included).
- **Risk share** (`_risk_share`): RC_i = w_i(Σw)_i / w'Σw at today's
  weights, Σ = `mpt.annualized_cov(…, "daily", "ledoit")` of the last year's
  trading-day returns in the display currency. Cash has no variance, so the
  holdings' shares add up to 1 whether or not cash is in w. A listing with
  fewer than 20 returns in the window (an IPO) is left out alone (None);
  fewer than 20 shared days for the rest gives {}. Contributions are None,
  not a 500, when a day loses everything (Carino has no log link at -100%).
- **Yield on cost**: the last 12 months of Yahoo payouts per share x shares
  / the holding's cost, gross, at today's FX. **News / Market read and
  analyst upside** come from caches only (`_research_extras`: the sentiment
  cache, and the `__book__` rows, else any saved portfolio's row for the
  share), never a fetch here. The export writes text that starts with "="
  as text, never a formula.
- **Closed** holdings (`closed`) and **suggested dividends**
  (`suggested_dividends`, every estimate with its ex-date, shares and per
  share amount) ride in the payload; `add_dividends` books picked ones as one
  journal item, source `suggested`.
- **Export**: with a book, `build_workbook(book=…)` adds "Positions" (as the
  page, plus cash, total and closed) and "Ledger" (oldest first, each entry
  in its own currency, with its cash effect) after Overview, in the page's
  display currency (`/api/export-xlsx?ccy=`). A book alone is enough to
  export.

### My Investments page speed (2026-10-12)
- **`investments.page(ccy)`** is what the route, the export and every change
  call: one computation per display currency at a time (a lock per
  currency; a second caller waits for the first's answer), kept a minute
  (`PAGE_MEMO_TTL`, keyed by rev, currency, benchmark, day and the book's
  path; `refresh_market` clears it). `book()` stays the plain computation
  (tests call it with different stubs at one rev).
- **The last good page on disk**: a healthy page (series, nothing pending,
  broken or waiting) is written to `state/book_cache.json`. `GET
  /api/investments?cached=1` returns it with `stale: true, computed_at` only
  when rev, currency and benchmark still match, else `{"nocache": true}`; a
  change bumps the rev, so it can never stand in for an edited book.
- **Warm-up**: `server.start_server` runs `investments.warm(prefs ccy)` and
  the mpt/numba import on daemon threads (the desktop app never runs
  `main()`, so numba used to compile on My Investments' first request).
- **`_prefetch`** (top of `book()`): the holdings' closes, then the stand-ins
  already known (`analytics.known_proxies`, `retry=False`), then the
  benchmark, in one worker (one `yf.download` at a time: concurrent
  downloads share yfinance's module state), beside every currency's
  history in parallel (`fx.prefetch_usd_series`, `Ticker.history`, the same
  dates and closes as the download path). The ordered steps after it read
  the same caches.
- **Stand-ins**: `_proxy_candidates` keeps found ones a week in
  `state/proxies.json` (market data only); candidate closes are fetched with
  `_bulk_close(retry=False)`, so a dead candidate costs nothing beyond the
  batch (the retries sleep up to ~5 s each).
- `_research_extras` reads the saved portfolios' analyst rows once per
  `views.json` change (`_analyst_rows`, keyed by path and mtime).

### My Investments Attention rail (Phase 7: `attention.py`)
- **Metrics with the page, rules at response time.** `book()` adds each
  holding's `xray` (`investments._xray`, total return: as-traded closes with
  each payout added back on the day its drop shows (`_ex_dates`: Yahoo dates
  some a day late and lists some twice), on the share's own trading days; the
  last day's move and its sigma against the 252 trading days before it, the
  5-day move against sigma x sqrt(5), the highest point since the holding
  was bought). `_xray_clean` drops Yahoo glitches first (a close more than x4
  off the median of the 10 closes before it and of the 10 after: a run of bad
  days differs from both sides, a crash that stays matches the side after;
  at either end only a pence/pounds flip, about x100, so a real -96% last
  day still shows), and sigma is never below `XRAY_SIGMA_FLOOR` (0.2% a day: a
  money-market line's ticks would read as dozens of sigmas). The xray rides
  in the memo and `book_cache.json`. `investments.with_attention(out)` adds
  `attention` (`attention.evaluate`, pure, about 0.06 ms) to every answer,
  fresh, memo or cached, as a copy: the memo and the file never hold the
  rail, so a threshold change or a dismiss shows at once without a
  recompute. `sector` and `fund` come from the symbol pack at that moment
  (`_pack_kind`, local SQLite; a listing the pack lacks is a fund by its
  name), not at compute time: a first launch computes before the pack has
  downloaded, and that page is the one saved.
- **Rules** (`attention.RULES`, one registry for Settings, validation and
  the rail; owner, 2026-10-13): one holding above 15%, one sector above 35%
  (two names or more), one currency other than the display currency above
  60% (uninvested cash counts in the base currency), down from cost by 20%,
  a day move of 3 sigma (shown while it is the last trading day's, up to 4
  calendar days) are "Needs a look" (`high`); down from its peak by 25% and
  the News read divergence (bullish while the week fell more than 1 sigma, or
  bearish while it rose; cache only, a read older than its window + 1 day or
  flagged stale is ignored) are "For your information" (`info`). Every
  threshold and switch is in Settings -> Attention.
- **Dismiss and unread** (`state/attention.json`: `rules`, `dismissed`,
  `seen`, each `{key: level}`; a preference file, malformed reads as the
  defaults). An item's level is how far past its limit it is; a dismissed or
  seen item counts again once it is a full `step` worse (5 pts for weights,
  10 for drawdowns). Dated items (a day's move, a read, an event) carry the
  date in their key and never come back under it. `prune(firing, checked)`
  runs on a fresh, healthy page only, and forgets a key only when the page
  checked it (`attention.scope`: the rule on, and the holding priced, with
  an xray or a read) and it no longer fires. A rule switched off, a holding
  on its last trade price, or a currency item seen in another display
  currency keeps its dismissal. Currency and down-from-cost keys carry the
  view (`|in:GBP`): both figures depend on it. Both lists keep at most
  `MAX_KEPT` (2,000) keys, the oldest dropped; a dated key over 30 days old
  is dropped whatever was checked.
- **Events** (`state/events.json`, a cache): Yahoo `Ticker.calendar` per
  held share (next earnings, ex-dividend), a day at a time. Filled in a
  background thread after a fresh page (`refresh_events_soon`) and by the
  topbar Refresh (`refresh_market`, forced, also in the background: one call
  per share must not hold the job at 100%); held shares only; single
  flight, and a forced refresh asked for while another runs is fetched by
  that one before it ends (`_FORCE_AGAIN`). A failed call keeps the last good dates and retries after 15
  minutes (never "no events"). yfinance hides HTTP errors (a 429 comes back
  as an empty calendar), so an empty answer for a share whose cached date is
  still ahead counts as a failure; a fund's empty calendar is an answer.
- **Routes**: `GET /api/investments/attention?ccy=` (the badge; from the memo,
  the last page computed even if unhealthy (`_LAST_PAGE`), or the cached
  page, never a compute; with none, the rules alone with `page: false`, which
  the rail reads as "keep what you show", and `pending: true` when a book
  exists, so the badge asks again every 10 s for two minutes) and JSON POSTs
  `/api/investments/attention/{dismiss,seen,rules}`; each answers the rail as
  it now reads and never touches the book file.

### My Investments plans and price alerts (Phase 8: `attention.py`)
- **`state/plans.json`** `{version, plans: {symbol: {target, review,
  max_weight, why, set_on, set_price, levels_on, alerts: [{id, level, side,
  set_on, fired_on, fired_price}]}}}` is the owner's input: a damaged file
  is never overwritten (`PlansUnreadable`, 409), a newer `version` is
  refused, and a write puts back untouched holdings verbatim. It is not in
  the book file: a plan edit would bump the rev and recompute the book.
- **Levels are in the quote unit** (pence for LSE), compared with the page
  position's `price`; no fetch. A split since a level was set rescales it
  (`split_adjusted` in memory on every answer, `apply_splits` saved once on
  a fresh page, from `market_splits(fetch=False)`).
- **Items**: `target|SYM|level` and `review|SYM|level` (high; review steps
  10 pts), `holding|SYM|maxN` for a plan's own max weight (it replaces the
  book limit, funds included, switch or not), `price|SYM|id` (info, one
  shot: `record_fired` on a fresh page keeps it until removed). The side of
  an alert and "set at" come from the server's page (`investments.price_now`),
  never the request.
- **Snooze** (`state/attention.json` `snoozed: {key: day}`) hides an item
  until that day; `prune` drops past ones. Routes: JSON POSTs
  `/api/investments/attention/{snooze,plan,plan/clear,alert,alert/remove}`;
  set and add need a held symbol, clear and remove work on any plan.

### My Investments notifications and the weekly recap (Phase 9: `notify.py`)
- **What is sent is the rail's answer**, never a second rule set:
  `attention.should_notify(att)` = a `high` item (so not dismissed or
  snoozed) marked `unread` by `evaluate` and past `notified` (a map in
  `state/attention.json`, same `_over` step, cap and `prune` as `seen`).
  Plain price alerts are `info`: in-app only (alert research). `armed: false`
  (first run, or after the switch was off) makes the next pass a baseline:
  what is in the rail then is recorded, not sent.
- **The scheduler** (`notify.start()` from `start_server`, both modes; off
  with `CONVEXITY_NOTIFY=0`, which the tests set) ticks every minute:
  quiet-hours end, the recap, then `due()`: every 30 min while a held
  listing's market is open (suffix table `_MARKETS`, US hours otherwise),
  and once 20 min after each close; nothing at night or weekends. A pass is
  a **background job** (`phases=["watch"]`, jobs.md): `notify.watch` runs
  `investments.page(fresh=True)` inside `cache.prefer_cached(held +
  benchmark + ^IRX)`, so only held shares' expired closes are fetched (a
  sold share's stale entry is served; a context variable, so the page's own
  requests are untouched). **A degraded page** (not `_healthy`, no rail, or
  >20% of holdings without a price) sends and records nothing.
- **Quiet hours** (default 22:00-08:00, local): nothing sent or recorded,
  the items stay unread; at their end a no-fetch pass from
  `investments.computed_page` sends one "n items came up overnight". The
  recap waits too. **Cap**: three items per pass, the rest one "+n more".
- **Delivery**: `send()` puts each message in an in-memory outbox (`seq`,
  `GET /api/notifications?since=`, `native` true when the desktop sink is
  registered) and calls the sinks (desktop.md). Logs name rule and ticker,
  never an amount.
- **The weekly recap** (`build_recap`) is for the last Friday whose 16:30
  New York time has passed, from a page already computed (memo, last page,
  `book_cache.json`), never a fetch. Return from `series.twr_index`, the
  benchmark from `bench_index`, gain = value change less `series.flows`
  (cumulative money in less estimated dividends paid out: the month
  figure's formula), best and worst from `xray.week_pct` only when the
  holding's `day_date` is that Friday (else a dash), dividends from the
  ledger's `cash`, next week from `state/events.json`. Built once per week, only from a page that passes `degraded()`
  and was computed after that Friday's 16:30 New York close (`computed_at`,
  stamped by `investments.page`), into `state/recaps.json` (derived, last 12, malformed = {}); a launch
  after a missed Friday catches it up, but the notification goes out only
  until Monday's US close (`RECAP_NOTIFY_DAYS`). `GET
  /api/investments/recap` builds it without notifying.
- **Settings** (`prefs.json`, typed keys `notify_on`, `quiet_on`,
  `quiet_from`, `quiet_to` "HH:MM"): `POST /api/prefs` calls
  `notify.on_prefs` (off cancels a running pass and disarms).
  `POST /api/notifications/test` sends a test; `/check` runs a pass now
  (202, or 409 while a refresh runs or when off).

### My Investments import (`importer.py`)
Roadmap Phases 5–6 (run together, ahead of Performance, at the owner's
request): the composer is the way into the page.
- **Two NIM stages**, both on `news_sentiment`'s client, limiter and circuit
  breaker. Screenshots and scanned PDF pages are *transcribed to plain text*
  by `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` with thinking off (~7 s;
  every field right on the gold set; Llama 3.2 90B misread dates and a
  currency and took 75 s; probed 2026-10-06). All text (pasted, transcripts,
  CSV/Excel rendered as CSV, a PDF's text layer) then goes to the text model
  under a strict schema of ledger rows (`_nvidia_call(..., max_tokens=8000)`,
  `record=False` so an import failure never flips the News banner).
  **The text stage asks the careful model first** (`importer._CAREFUL_MODEL`,
  nemotron-3-ultra-550b, 90 s; then the News read's chain head), since
  2026-10-22: with lightning-30b alone the eval fell to 8/11 (pence read as
  pounds, an exchange's currency dropped); careful-first is 11/11, slowest
  case 81 s inside the 150 s stage deadline.
- **Full resolution matters.** A phone screenshot shrunk to a third read
  405.00 as 405.05. The gold images are rendered at an iPhone's 1179 px.
- **The model may suggest, never decide** (the `search.py` rule). Code then:
  verifies each ticker in the symbol pack and picks the listing by the row's
  currency (a £ price makes "Shell" SHEL.L; the pack's home listing for the
  name is the NYSE line); counts a ticker as *read* only if it literally
  appears in the source, else marks it guessed; turns GBX/GBp into pounds;
  checks qty × price against the row's total (tolerance: half a cent a share
  plus a cent) and offers the price the total implies; drops a "holding"
  with no share count (a model over-reading a portfolio total); and asks at
  most three questions (since when undated holdings were held; which listing
  when the currency can't tell).
- **Share counts:** an imported trade keeps the statement's count
  (`qty_basis: "trade"`: a Feb 2024 NVIDIA sale at $674 is pre-split); a
  holding from a holdings list is today's count (`current`). A dry run
  (`investments.check_rows`) flags a sale the book can't cover before Apply,
  in the statement's shares, with the shortfall the page offers to add.
- **Reconciling a holdings list** against the book (`import_context().held`):
  the same count is left out by default ("Already in your book"); a larger
  count offers to add only the difference. Never applied silently.
- **Name memory:** a listing the owner picks in the review is saved on Apply
  in `settings.aliases` (`alias_key`: lower-case words → ticker, only for
  tickers that batch booked, at most 300) and wins outright next time.
- **Inputs:** PNG/JPEG/WebP, PDF (`pypdfium2`: text pages as text, pages
  with < 40 characters rendered and sent as images; PNG encoded with the
  standard library, no imaging dependency), CSV/TSV/TXT, XLSX (old `.xls`
  and HEIC are refused with a plain fix). At most 8 files, 8 MB each, 20 MB
  together, 20 PDF pages, 60,000 characters of text.
- **Deadlines:** every NIM wait (limiter, circuit breaker, call) is bounded
  at 150 s per stage; an eval call once sat 46 minutes in the shared queue.
- **Chunks:** text goes to the model 30 rows at a time (each with its
  header row), three chunks at once. Each row comes back as about 16 JSON
  fields, and 90-row chunks overran the 8,000-token reply cap, so every
  reply was cut-off JSON. Blocks of 20 lines or fewer are read twice and the
  fuller reading wins. A block whose left-out list shows doubt is read once
  more.
- **Problems are checked by the page**, not at read time: the review sends
  the rows exactly as Apply will (`invRowEntry`) to `/import/check`, and
  Apply waits while a check is pending.
- **Logs** carry counts and timings only, never amounts, text or files.
- **Eval:** `scripts/eval_import.py` against `tests/data/import/gold.json`
  (synthetic: phone screenshots, three broker CSV styles, a text PDF, a
  scanned PDF, typed text, a broker positions table with exchange codes and
  pence). Rebuild its images with `scripts/build_import_gold.py`. Run it live
  after any prompt, rule or model change; all cases must pass (11 since
  2026-10-10).

**The exchange shown decides the listing** (owner's report, 2026-10-09).
Euro rows shown on Paris and Madrid were booked on Xetra: currency alone
picked the first euro line (`_CCY_SUFFIX` order, `.DE` first). Now:
- **Plain code reads the exchange**, never the model:
  `shown_exchange(ticker, block)` takes the word right after the ticker in
  its cell ("AIR SBF"), or a cell of that line that is an exchange code or
  name ("AIR | SBF", a CSV Exchange column). An `exchange` field in the
  model's schema made it leave out rows that showed none (4 of 10 reads to
  8 of 10 on "typed holdings"), so it was taken out again.
- **`_VENUES`** maps Interactive Brokers' codes (SBF, IBIS, BM, BVME, AEB,
  BUX…), ISO MICs (XPAR, XETR…), Yahoo's own codes and plain names to
  (Yahoo exchange codes, ticker suffix, currency). No key names two venues
  (tested).
- **`match_listing(..., shown)` → `_on_venue`**:
  1. its own line: `base + suffix` in the pack, else the company by name on
     that exchange, else Yahoo's live Lookup (`symbol_db.yahoo_lookup`).
     Note: "On Euronext Paris, as shown."
  2. else a proxy, the same company (pack group, `symbol_db.listings`, or
     Lookup), in the venue's currency first. Note: "Yahoo has no Milan line
     for this, so it is priced from VOW3.DE (Xetra): the same company in the
     same currency."
  3. else its home listing, in another currency ("…values convert at each
     day's rate").
  Proxies are marked guessed (✦); the review shows the note under the
  row, and a proxy's note is saved in the entry's `note`. The row's currency,
  when not shown, is the venue's.
- **A name that is only the ticker and its exchange code** ("GLE SBF", a
  positions screen's instrument cell copied as the name) is no name:
  `_company_words` hands `match_listing` the bare ticker. Checked against
  "GLE SBF", Société Générale failed `same_company`, and every row of a
  correctly read screenshot was marked as the AI's guess (2026-10-10).
- **No exchange shown** (typed text, "40 Richter Gedeon at 9640 HUF"): when
  the pack has nothing for the name, or nothing in the row's currency,
  `match_listing` asks Yahoo's live Lookup by the name and by its first word
  (Yahoo finds RICHT.BD for "Richter", not for "Richter Gedeon"); only the
  same company counts, and the currency rule then picks among them. Each row
  carries `listing_ccy` and each alternative its `ccy`, for the review's
  listing menu (2026-10-10).
- **Units against the market** (`_units`, after matching): the listing's
  last close (`_last_closes`: `_bulk_close(..., "1mo", adjusted=False)` and
  the quote currency) decides pence. ULVR on LSE read as "GBP 4380.50" with a
  close of £45.12 is 4,380.50 p = £43.81: converted, marked, with a link to
  keep the other reading. Any price 25x off today's gets a check line.
- **Average vs last price by arithmetic** (`_avg_from_pnl`): a positions
  row shows two prices that look alike, and the model took today's for the
  average (or none) on a synthetic IBKR screen, while the vision step read
  that table as a phone list and dropped its headings whatever the prompt
  said. (last − average) × shares = the signed unrealised P&L on the row
  (or a hundredth of it: pence prices, P&L in pounds) proves which is
  which; exactly one fitting pair, or nothing changes. Numbers are read in
  either convention ("1.234,56", "171,40"; percentages skipped), and only a
  reading equal to the row's last price (or none) is replaced, so a format
  misread can never overwrite a good one.
- **Ambiguous codes:** "TSE" is Toronto at Interactive Brokers but Tokyo on
  many screens; a numeric ticker (7203) says Tokyo. A currency taken from
  the exchange is marked guessed. ZAc and ILA rows become ZAR and ILS.
- **Tickers are whole words** (`_word`): "T" is not inside "T-Mobile" or
  "AT&T".
- **One buy date per row, no "Since when" question** (owner, 2026-10-10:
  with a date per row it made no sense; performance counts from the oldest
  date in the book). `_default_dates` gives an undated holding (and a
  snapshot's cash) today with `date_default`, which the review shows quietly
  until the owner picks that row's date; an undated trade stays "needs you".

**Clear** (`investments.clear`, `POST /api/investments/clear`, Settings → My
Investments): every entry out in one journal item ("Cleared N entries"), so
Undo restores them in place. Settings and import aliases stay; a dated copy
`state/investments.before-clear-<UTC stamp>.json` is written first (newest
three kept).

### Short listings' history (`analytics.extend_history`, 2026-10-09)
A listing younger than its company used to cut a whole basket to its own
start (`dropna(how="any")`): a US company's young Xetra line starts a few
months ago, and Gedeon Richter's Budapest ticker was renamed RICHTER.BD → RICHT.BD
on 2026-10-09 (Yahoo then answers only "1d"/"5d" for it).
- **Splice:** before a short line's first close, the same company's other
  listing stands in (`_proxy_candidates`: pack group, else Yahoo Lookup by
  the bare ticker and name; exchange lines before OTC and order-book lines),
  converted into the short line's currency at each day's rate
  (`fx.fx_multipliers`) and scaled to meet it at that close. Minor units on
  either side are a constant the scale absorbs.
- **Only clean, trading data stands in** (`_clean_tail`): Yahoo's old data
  has glitches (Frankfurt's Richter lines are *negative* when
  dividend-adjusted before June 2026; Unilever's London line jumps x52 in
  1993). A close ≤ 0 or a day more than tripling or losing two thirds starts
  the usable stretch after it; a stand-in must show a new price on half its
  days (the London order-book line moved six times in five years). When the
  adjusted history is broken, the as-traded one is tried.
- **Short** means: starting after `need[symbol]` (My Investments: the day the
  book first holds it), else later than most of the frame's lines (MAX must
  not splice ADRs onto a 2000 start next to Unilever's 1988).
- **A hole counts as a late start** (`_after_gap`, `_GAP_ROWS` = 60, added
  2026-10-10): Yahoo has no NOT.DE (Novartis, Xetra) close from Sep 2019 to
  Jan 2025, and junk before it; ffill drew five flat years. A line with a
  hole of more than 60 rows starts, for splicing, after it: the part before
  is blanked and the stand-in fills it. With no stand-in the line's own data
  is restored untouched (a real suspension stays as it was). A month's gap
  is not a hole. A stand-in's own hole (over 92 days) starts its usable
  stretch in `_clean_tail`.
- **Ragged baskets:** a holding with no stand-in no longer truncates the
  others. Before its first close the basket is the other holdings, their
  weights scaled back to 100% (`_basket_weights`; contributions still add
  up to the total); the frame starts once half the holdings have a price;
  impossible one-day moves (over eight-fold, or losing more than 94%: unit
  changes, not markets) are counted as no move; no OHLC band when a
  holding starts inside the period shown (the line is drawn).
- **Dividends merge** (`cache._dividends_put`): a short as-traded fetch (an
  import's one-month price check) used to replace years of cached payouts,
  and the book's estimated dividends vanished until the cache expired. Baskets whose holdings start together are unchanged (tested).
- **Notes:** every splice, late start or ignored glitch inside the period is
  a warning, shown folded under the Portfolio chart ("About these prices")
  and in My Investments' rail.

### Real-world price distribution (`options_density.py`, `GET /api/density`)

The stock window's σ/μ. For each stock, the probability distribution of its
price in about 3 and 12 months from its listed options: readable in five seconds
by a non-quant (a price axis, a curve, ranges, "chance of" numbers), and
defensible to a derivatives quant (every identifying assumption on screen, no
method the literature has rejected, a quality gate that hides bad chains).

**Why a two-moment tilt.** Options give only the risk-neutral density q; the
real-world density is p = q/(M·R_f), and options cannot identify the pricing
kernel M, so every method is an identifying assumption. A preference tilt
p ∝ q·S^γ is a robust benchmark (Bliss & Panigirtzoglou 2004) but leaves the
variance risk premium in, so the curve stays too wide. Ross recovery is not used:
it is misspecified (Borovička, Hansen & Scheinkman 2016) and fails empirically
(Jackwerth & Menner 2020). Chosen: p(x) ∝ q(x)·exp(θ₁x + θ₂x²), x = ln(S_T/F),
with E_P[S_T] = S₀·exp((r + β·ERP)·T) and Var_P[x] = (κσ_Q)²T. θ₂ = 0 is the
power tilt, the fallback when the solve fails. κ (physical/implied vol) is
anchored to implied vol on purpose: a historical-vol forecast made NVDA's 12M
curve *wider* than the options (10y vol includes 2022), and trailing vol in a
calm market made SPY's crash risk vanish. κ is a placeholder (0.92 stocks, 0.80
indices) until Phase 2 estimates it. Non-US listings have no chains: the US line
or ADR is used (`symbol_db.us_line`) or the answer is "No listed options". Yahoo
has no option history, so anything needing history is built forward.

- **Pipeline** per horizon. `_options_rn` tries, in order, and the first fit
  that passes the gate wins:
  1. A liquid expiry within 10% of 91 or 365 days (`ANCHOR_BAND`): its own
     SVI, then the SSVI surface at it.
  2. Else the SSVI surface read at **exactly** the horizon, between the most
     liquid expiry on each side within [½, 2]× the horizon (`_liquidity`:
     open interest × quotes / median relative spread).
  3. Else the old nearest-expiry tries inside 45–182 or 270–550 days.
  4. With none there, the surface extrapolated when the chain reaches half the
     horizon, else `no_expiry` (small caps listing nothing past 5 months open
     on 3M).

  Each expiry then runs: chain → forward from parity with the rate from **^IRX**
  (never backed out of parity: Yahoo's US chains are American-style and gave
  −4.8%) → two-sided **OTM-only** quotes, minus any that break static
  no-arbitrage by more than the spread (`_monotone`) → raw SVI with **Lee's
  wing cap** → Breeden–Litzenberger on a 4001-point log-strike grid
  (±10 ATM sd) with **GEV tails** → the minimum-entropy two-moment tilt
  (Approach B; mean r + β·ERP, variance (κσ_Q)²T), with the power tilt (A)
  as the fallback.
- **GEV tails** (Figlewski 2009, `_tails`): the SVI is trusted only where it
  was fitted. Past the 5% / 95% points, or the last quoted strike when the
  quotes stop short of them, a GEV takes over: its cdf and density equal the
  body's at the attachment point, and its shape ξ matches the density 3 cdf
  points further in. The cdf there comes from the call slope,
  1 + e^{rT}·∂C/∂K, which reads the smile at that strike only. Integrating
  the density from the left instead counts the extrapolated wing: SPY 12M's
  SVI bent up past K = 995 into a false hump with negative density after it,
  and the mean came out 2.1% above F. The right tail's ξ is capped at 0.45 so
  E[S] and Var[S] stay finite. The payload's `quality.tails` gives where each
  tail attaches and its ξ; it is `null` when the quotes are too narrow to place
  them, and the gate then judges the SVI's own wings.
- **Robust chains (2026-10-18)**, checked on a Sunday against Friday's chains:
  - **The session clock.** Maturities count from the quotes' session
    (`_session`: the New York date of the chain's latest trade), never the
    calendar. A weekend read prices the same T as Friday's close; `T` used to
    run two days short.
  - **Hygiene** (`_live`, `_slice`). A quote whose mid is under
    `MIN_PREMIUM` ($0.05, or 0.5% of spot) is dropped, and so is one more
    than `MAX_K_SD` (3.5) ATM sd from the forward. Two minimum-tick
    deep-OTM calls (NVO's K=90 January at 0.03/0.04 on a $39 stock) put a
    14.7 vol-pt outlier in NVO's surface; the rule took it from 2.2 to 1.1
    vol pts and SPY's from 4.7 to 0.6.
  - **Dead strikes.** One untraded for 14 days with no open interest is
    dropped. The trade-recency filter of the owner's Heston notebook alone
    did not help: the wings were live min-tick quotes, not stale ones.
  - **Weights.** SSVI residuals are weighted 1 / (1 + spread/mid / 0.2),
    tight quotes up. Not 1/spread², which favours exactly the min-tick wings.
  - **The anchored horizon.** NVO 12M now passes at 365 days between
    June-27 and the Jan-28 LEAP; the thin September nearest the horizon had
    failed it.
  - **Snapshots.** A chain that passes is kept as
    `state/chains/<line>.json` (strikes, quotes, open interest, session,
    spot; numbers only, once per session). When Yahoo serves one with under
    30% two-sided quotes, a snapshot up to 4 days old stands in, its forward
    moved to today's spot (same moneyness).
  - **The payload** carries `quality.asof`, `quality.snapshot` and
    `quality.anchors`. The footnote says "Options as of the Fri 16 Oct
    close", and names the anchors.
- **Stale quotes**: walking out from the forward, a put or call further OTM
  can't bid above the previous one's ask. DELL 12M had a K=165 put bid 12.15
  next to a K=170 ask of 4.40. The SVI bent into a Lee-slope kink to chase it,
  giving a smile fit of 1.5 vol pts and a mean 9% below F.
- **Analysts' μ (before 1.29, replaced by `capm.py`)**: one B-tilt per firm (latest target ≤ 12 months old, from
  yfinance `upgrades_downgrades`; Finnhub's targets are 403 on the free plan),
  blended equally, the common variance shrunk by the between-firm variance so
  the blend's total is still (κσ_Q)²T. **Trust** w keeps that share of each
  target's excess log return over CAPM, `lr = lc + w(lr − lc)`. Every payload
  ships w = 1 (`curves.analysts`) and w = 1/2 (`curves.analysts_t50`); w = 0
  is `curves.market` (every firm on CAPM, one tilt), so the page's trust
  control never refetches and trust is not part of the cache key. The
  analysts' block is copied before `at_expiry` is written: `consensus`
  returns its cached dict, shared by both horizons, and writing into it let
  the 3M computation move a cached 12M payload's diamond (fixed 1.28.8). Any horizon (3M too since
  2026-10-22, the owner's call): a target's log return is scaled by T up to a
  year and carried on at CAPM after it, so on 3M each 12-month target is
  moved to the 3-month expiry as S0·(T/S0)^(t/1y). The expiry is rarely
  exactly the horizon (AAPL 335 days, ARKK 461) for the same reason. The
  targets are `analyst_targets.consensus`'s (below): the listing's own block,
  put in the chain's units when the chain is another line's
  (`_own_analysts`), so σ/μ shows the row's number.
- **No NaN reaches the page**: `json.dumps` writes NaN, which `JSON.parse`
  rejects. The gate fails a non-finite fit (NaN passes every `<` test), a
  missing last price is `error`, and `downsample` refuses a non-finite curve.
- **Gate**: quotes ≥ 30, SVI RMSE < 1 vol pt, |E_Q/F − 1| < 1%. A failure is
  `{"ok": false, "reason": "thin"}` and is never drawn. It runs after the
  tails. Before them, long chains failed often: on 2026-10-17 SPY was +2.1%,
  QQQ +3.1% and DELL had a 1.5 vol-pt smile fit and −9.1%. With the tails and
  the stale-quote filter, all three pass (+0.2%, −0.1% and +0.1%). When a
  12M chain still fails, the page opens on 3M and says why.
- **Non-US listings** have no chains (Yahoo carries no Eurex or Euronext
  derivatives): every US line of the company is probed (`symbol_db.us_lines`
  plus `discover_us_line`), and when several list options the one with the
  most open interest at the ~3M expiry wins (an ADR over its pink sheet); the
  choice is cached a day (`density-line|SYM`). Every price is scaled by
  local/US last price (`ratio`). A chain that exists but fails (a European bank's ADR:
  quotes 50–150% of mid wide on 2026-10-17) falls back to history, the
  fallback naming that chain (`DensityError.chain`).
- **Maturities no expiry has** (`surface_at`, `EXTRAP_MIN` 0.5): the SSVI's
  θ is linear in T between fitted expiries and grows at the last expiry's
  average rate past it (flat vol), the carry is the nearest expiry's, and that
  slice's quotes, rescaled to the same ATM sd, place the GEV tails. Same
  surface gate; `quality.extrapolated = {from_days, beyond}` and a synthetic
  expiry of today + horizon.
- **Vols for the readouts** (`vol`): `implied` is the ATM (k = 0) implied vol
  of the fitted smile at the horizon's expiry; `hist` is the last year's daily
  log returns, sample sd × √252, of the same listing (`_hist_vol`), so both
  are per-year vols of one instrument in one currency. A history fallback
  keeps the thin chain's implied (`implied_symbol`) when it had one. Analyst targets stay in the chain's currency
  in the payload; the page multiplies by `ratio`.
- **Expiry choice and sparse chains** (`_options_rn`): every expiry from 20
  days to 3 years is fetched once in parallel and cached (`_chains`, 15 min,
  shared by both horizons). Up to 4 expiries in the window are tried, nearest
  the horizon first. Each tries SVI when the slice has ≥ 30 quotes, then an
  **SSVI surface** (Gatheral–Jacquier 2014, power-law φ, `fit_surface`) over
  all expiries; the first fit that passes the gate wins. PG, PEP, JNJ, MCD,
  VZ and XOM list 7–29 usable strikes per long expiry: per-slice SVI
  overfits them (PEP 12M missed its forward by 2.8% on 14 quotes) while
  their surfaces hold 90–280 quotes; all pass on the surface.
  The SSVI has no static arbitrage by construction (η(1+|ρ|) ≤ 2, γ ≤ ½, θ
  non-decreasing). Its gate: ≥ 60 quotes over ≥ 3 expiries, surface fit
  < 1.5 vol pts, ≥ 8 quotes at the expiry, and quotes reaching the 15% / 85%
  points; then the usual smile fit and martingale checks.
- **FX** (`_fx`, `affine_mix`): a US line shown in another currency carries
  that currency's risk. ln S_shown = (1 + b)·x + e, with ρ and b from two
  years of weekly returns, the FX vol from the currency ETF's options (FXE,
  FXB, …; the longest expiry when none reaches the horizon) else history,
  and no expected FX move. Applied to every curve before downsampling; the
  payload's `fx` says pair, σ, ρ and source.
- **History estimate** (`_history`, `source: "history"`): when options fail
  (no chain, no expiry, thin) the stock's own record stands in, the options'
  reason kept in `fallback`. x = β_m·y + e: y the market proxy's options
  curve (`_PROXY` by listing suffix: SPY in the US; FEZ for Europe, EEM for
  EM funds, whose chains are too thin today, so their market part is
  history too), scaled to κ_index × its vol in the stock's currency; β_m and
  e from 3 years of weekly returns (weekly: European and US closes are hours
  apart), winsorised at 8 robust sd, e's variance reverting from the last 13
  weeks to the 3-year level with a 3-month half-life. A listing with under
  130 weeks or gaps uses the company's US line in its currency (a Xetra
  secondary listing had 76 weeks and a phantom +19% week). Mean: CAPM in the stock's currency.
  No risk-neutral curve; earnings dates without a jump.
- **Local rates** (`_local_rate`): USD from ^IRX; EUR, GBP, CHF, JPY, CAD,
  AUD from two quarterly CME futures (ln F_far/F_near over their gap is
  r_USD − r_local: both settle together, unlike Yahoo's spot, which implied a
  euro rate above the dollar's); other European currencies take the euro's.
- **Earnings**: next date from `attention.fetch_events`; jump variance
  σ_e² = w_after − w_before·T_after/T_before from the ATM total variance either
  side; later reports estimated every 91 days.
- **Payload**: curves (`rn`, `market`, `analysts`) as ~400 points of the
  density of x = ln(K/F); the page maps them by `forward` (already in the
  listing's currency) and runs the Monte Carlo bridges itself (seeded), since
  hover needs the paths.
- **Cache**: `cache._cache_put`, 15 min, successes only, keyed by symbol,
  horizon, β and the settings (a settings change is a new key). One
  computation per key at a time. Never called from `fetch_one`.
- **Settings** (prefs.json, bounded in `persistence._clean_prefs`):
  `dist_erp`, `dist_kappa_stock`, `dist_kappa_index`, `dist_measure` (P|Q);
  the model's own keys are in "The model price" below (1.29 dropped
  `dist_trust`).
- **Phase 2 (not built):** a snapshot collector (the reference-pack workflow
  appends daily 3M/12M density snapshots for the S&P 500: tickers, dates and
  numbers only, under the pack's allow-list rules); a Track-record-style page
  with PIT histograms of realised prices against the stored curves; κ estimated
  from those snapshots, then a Beta calibration map; and paths pinned to both
  expiries on the 12M view with a Gaussian copula ρ = √(T₁/T₂).
- **Sources:** Bliss & Panigirtzoglou (2004); Liu, Shackleton, Taylor & Xu (2007);
  Shackleton, Taylor & Yu (2010); Christoffersen, Heston & Jacobs (2013);
  Metaxoglou & Smith (2016); Martin (2017); Martin & Wagner (2019); Ross (2015);
  Borovička, Hansen & Scheinkman (2016); Jackwerth & Menner (2020); Cuesdeanu &
  Jackwerth (2018); Gatheral & Jacquier (2014); Figlewski (2009);
  Vincent-Humphreys & Noss (2012).

### The model price (`capm.py`, 1.29)

One expected-return model for every asset, replacing Yahoo's beta, the US-bill
CAPM and the per-firm analyst mixture with its trust control:

    mu = rf_local + beta·ERP + alpha,   model price at T = S0·e^(mu T)

- **Beta** (`capm.estimate`, `ensure`): the listing's home index in local
  currency (`HOME` by Yahoo suffix: ^GSPC, ^FCHI, ^GDAXI, ^IBEX, FTSEMIB.MI,
  ^AEX, ^BFX, ^OMXH25, ^ATX, ^SSMI, ^OMX, ^OMXC25, ^FTAS, 1306.T for TOPIX; HK,
  AX, TO, KS, NS, SA by the same rule, not backtested), else STOXX 600 for an
  unmapped European exchange, else the S&P 500 with the stock in USD. Welch's
  BSWA on the last 3Y of daily returns on joint days (stock clipped to
  [−2 r_m, 4 r_m], weights exp(−2·age/252), weighted OLS slope; |log r| > 1
  is a glitch, zeroed, and so is a one-bar bad print: a move over 15% the
  next bar takes back, both bars — an S&P 500 ETF on London had a +30%/−28%
  Friday pair that cut its weekly R² with the S&P to 0.18), 1Y OLS with 151–251 joint days, beta 1 "prior" below.
  Shrunk 1 + w(b − 1), w = 0.9. Backtest (164 names, 2008–2025, research branch
  `scripts/backtest_beta.py`): MSE vs the realised beta −51% at 12M and −57% at
  3M against Yahoo's. Cached per symbol and window per day in
  `state/betas.json` (raw beta, so a shrink change needs no recompute); a
  download that returned nothing is the prior, uncached.
- **Whose beta** (`beta_line`, 2026-10-24, from the owner's real book): a
  secondary listing of a stock is measured on the company's home line (the
  symbol pack's group: NVD.DE → NVDA, APC.DE → AAPL, CCC3.DE → KO, NSRGY →
  NESN.SW, NOV.DE → NOVO-B.CO); on its venue's index NVIDIA read as a German
  stock. A listing the pack lacks learns its home line by name
  (`learn_line`: Yahoo Lookup, accepted only when the pack knows the hit under
  the same name; a US company's line on Borsa Italiana's global segment), batch path only, a found line
  kept a week in `state/beta_lines.json`; a lookup with no answer is asked
  again, never cached as a miss, and until the line is known (`unresolved`)
  the listing's own-line beta is shown but not cached (a timed-out lookup
  after a restart put a US company's Milan line on the FTSE MIB for the
  day, 2026-10-24). Funds keep
  their own line. `beta_meta.via` names the line; the window, the grid tip and
  the export say "via MU". More home indices, all probed live: Oslo
  (OSEBX.OL), Dublin, Lisbon, Singapore, Taiwan, NZ, Jakarta, KL, Mexico,
  Tel Aviv, Johannesburg (Equinor read −0.52 vs STOXX 600, 1.82 vs OSEBX). An
  exchange with no home index here uses the S&P 500 on **weekly** USD
  returns (`method: "weekly"`): daily returns across a closing-time gap read
  a beta near zero (DBS 0.14). Every home index answers on Yahoo, so a beta
  on a fallback proxy means a failed download: shown, never cached.
- **Funds** (`is_fund` from the symbol pack's type, or an ETC/ETN/physical-metal
  name Yahoo types "stock" — never a name ending in a company suffix; an ETC
  also keeps its own line, never its group's; `fund_candidates`,
  2026-10-24): a fund is measured against the candidate with the highest
  weekly R² (`weekly_r2`) among its home chain, ^GSPC, ^STOXX, ACWI and EEM
  (USD ETFs standing in for MSCI ACWI and EM), when that R² is at least
  `FUND_MIN_R2` = 0.3 (`beta_meta.fit_r2`). On its venue's index an S&P 500
  tracker on London read 0.47 (FTSE) and a world fund on Xetra 0.59 (DAX).
  Below 0.3 (gold, bonds; `fit_best_r2`) it stays on its home market with
  plain daily OLS (`method: "ols"`): BSWA's [−2 r_m, 4 r_m] band pulls a
  weakly correlated asset toward 1 (GLD 0.74 BSWA, 0.28 OLS). Weekly
  returns (`_weekly`: any proxy outside the listing's session) use one lag,
  Dimson 1979 (`dimson`): plain weekly still read a London S&P tracker 0.86,
  0.99 with the lag. A fund fitted without every candidate's download is
  shown, not cached. The cache key carries `|f`, so betas from before the
  fund rule recompute. Weekly estimates always read at least two years
  (W104): a 1Y window has ~52 weeks, under the 53 needed. Concurrent callers
  compute once (`_COMPUTE`: a page load asked from three places and
  downloaded the same 27 names three times).
- **rf_local** is `options_density._local_rate` (a home-index beta is a local
  CAPM). The CME spread is read on the two contracts' joint days, median of
  the last five: the thin far contract's last close was a day older than the
  near one's and EUR read 1.48% instead of ~2.4% (2026-10-24). **alpha** is `analyst_targets.relative_alpha` on the name's total
  upside (mean target / price − 1 + dividend yield, like the optimizer's
  views), its reference the long-run universe mean blended 10:1 with the name's
  own; IC = `model_ic` or `view_ic()` (measured, else the 0.02 prior), z capped
  at `model_z_cap`. Zero without targets.
- **One number everywhere.** `capm.model` builds the block
  (`{mu, mu_capm, rf, rf_source, beta, beta_meta, erp, alpha, alpha_meta,
  capm_price, model_price}`); `/api/density` returns it at the options' expiry,
  `POST /api/model-prices` (`capm.for_rows`) at 12 months for the grid, and the
  export calls the same. The window's alpha uses the listing's own or its
  research line's target only (`_own_analysts` returns whether the block is
  the listing's); the chain line's block is still shown as the Street
  reference but sets no alpha, since the row has none. `analyst_targets.consensus` returns None for a
  listing with no target summary of its own: Yahoo hands such a secondary
  line the parent's firm list in the parent's currency (CCC3.DE got Coca-Cola's
  $93.73 as euros, ~11% high, in the window, the coverage card and old rows),
  so every caller falls through to the research line, scaled. The window's dividend
  yield is computed with the row's rule, currencies included (HSBA.L read
  0.04% with a pence price).
- **Portfolio beta**: `stats.beta` is the weighted sum of the holdings' app
  betas (`beta_coverage` says how much weight had one); `benchmarks[*].rel.beta`
  stays the basket's regression on the chosen benchmark.
- **News and the reference pack**: the news context's betas are the app's
  (the job batches the missing ones); the pack's CI beta is the same estimator
  against SPY on 5Y of closes (its universe is the S&P 500). Neither is a
  Market read feature: only the forward idiosyncratic return reads it.
- **Settings** (prefs.json, bounded in `persistence._clean_prefs`):
  `model_beta_shrink` 0–1, `model_beta_years` 1|2|3, `model_ic` 0–0.1 (unset =
  auto), `model_z_cap` 0.5–5, `dist_mu` (the window's μ toggle, default on);
  `dist_trust` / `dist_haircut` are dropped on read. All are in the density's
  cache key; the beta window and shrink are in the analytics'.
- **Known limits** (live check 2026-10-23): the model price grows the price at
  the total-return mu (dividends not subtracted); an FX pair gets a CAPM mu
  (EURUSD β 0.09), not carry; a defensive name can have a negative beta when the
  last year's correlation was negative (DUK −0.10: 1Y OLS −0.32 vs the S&P).

### Earnings and dividends for the chart (`fetcher.fetch_events`, `GET /api/events`)

The stock window's E/D flags and its dividend yield. Earnings from
`get_earnings_dates(limit=60)` (EPS estimate, reported, surprise), one row per
date (Yahoo repeats some). Timestamps come in New York time and are read in
the exchange's zone (`fast_info.timezone`): at or after 16:00 local the
reaction is the next session (`react`), earlier the same one; midnight New
York means no time given (`timing: ""`). Dividends per share by ex-date in
the quote's unit; `div_ttm` / `div_yield_ttm` = the last 365 days over the last
price. Cached 6 h, only when something came back. Reactions are computed by
the page on the detail's daily (adjusted) closes.

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
- **Reading a request (learned live, 2026-10).** Size words (big, large,
  largest, giant) rank by market cap; vague amounts are moderate, editable
  filters (`_QUAL`: "small leverage" is D/E < 0.5, never lowest-D/E-first,
  which returned companies with no debt at all); exchanges are their own
  filter (`EXCHANGES`: "nasdaq" is NMS/NGM/NCM, not Region US); an exact
  industry absorbs a generic word after it ("uranium miners" is Uranium, not
  every mining industry). The model gets worked examples, and `_sane_rank`
  enforces the two rules it broke: a size word ranks by market cap, and a field
  the user only constrains is not the ranking unless they asked for an extreme
  ("best/top" may rank highest-first). A ROE/ROA ranking requires a profit at
  the screener (a loss over negative equity is a "high" ROE on Yahoo).
  **After any prompt, rule or model change run `scripts/eval_search.py`** (22
  live cases, the user's NVIDIA key via env; all must pass).
  **The AI step is a hedged request** (`search._hedged_call`, 2026-10-22):
  the careful model (nemotron-3-ultra, 3–33 s) and the fast one (the News
  read's chain head, lightning-30b, ~2 s) are asked at once; the careful
  reading wins if it lands within `_CAREFUL_WAIT_S` (18 s), else the fast
  one, else whichever answers before the 25 s deadline. Lightning alone was
  18–19/22 ("best" with nothing to rank by, "european" dropped, an empty
  cybersecurity theme); hedged 21/22 and 22/22 on two runs, the one miss a
  case where the careful model was slow. Free strings and lists in the schema
  are capped (`maxLength`/`maxItems`) for the same loop the News read had.
- **Which listing a card shows.** The most traded real listing of the company
  (`symbol_db.top`: NVO for Novo Nordisk, 7203.T for Toyota) — and on an
  exchange screen the listing there (SKHY for SK hynix on Nasdaq); a typed
  ticker stays as typed. Region, sector and size stay the company's own.
  "Also listed on" (`alternates`) shows only listings someone would buy: no
  re-listing venues (`symbol_db.relisting`: German regional, order books, OTC,
  BDRs — the builder uses the same rule) and none trading under 5% of the top
  listing (a CEDEAR). Every card carries the pack's USD market cap and the
  1-year change from `.info`.
- **Pasted lists** — two or more comma/semicolon/newline parts are `kind:
  "list"` at any name length, unless the text has an operator (<, >, =) or
  every part is only category words ("semiconductors, banks": a screen). A
  part that is only "Inc." or "Class A" joins the name before it; lists are
  not cut at the screen's 300 characters. `_run_list` resolves the parts in
  parallel: an exact or exchange-prefixed ticker (`LSE:HSPX` via the
  resolver's map) keeps that listing; a name is matched with and without its
  share-class words and `_pick` re-ranks the near-ties (within 6 points) on
  what the name says beyond the company — UCITS (implied by Acc/Dist) wants a
  non-US line, Acc/Dist (Accumulation/Distribution) the matching class (an
  unmarked UCITS line counts as the distributing one), Class B a -B ticker; a
  qualifier nobody typed (Swap, ESG, Hedged, a currency class) costs, and so
  does UCITS when the US fund of that name is in the running ("SPDR S&P 500
  ETF" is SPY). A short name of a giant is the giant ("Taiwan Semiconductor"
  is TSMC, not the $0.8B namesake). All on one page, in the order pasted,
  misses named.
  **Review pass (2026-10-19).** Tabs split too (an Excel row; the page turns
  pasted line breaks into commas, since a one-line box makes them spaces).
  Each list card carries `match` ("exact" a typed ticker in some form, "hit" a
  sure match, "guess" a name under 85 or tied within a point) and `query` (the
  part as typed); the page pre-picks exact and hit only and labels a guess
  "Closest match to …". Formats read like the Add box: broker pairs (`NOV
  IBIS`, `GLE SBF` via `resolver._broker_pair`), class tickers (BRK.B, BF/B →
  -B), Bloomberg `X CC Equity` (`_BBG_SUFFIX`; the resolver's "GR" is
  Greece), `Name (TICKER)` when it is the same company, ISINs (yfinance
  Search), bare European ETF tickers (VWCE → VWCE.DE: the main venues, the
  most traded line; initials alone are a guess). A fund line trading under 5%
  of the liquid one is dropped and liquidity breaks ties (CSPX.L, not a
  Colombian line). Legal forms ("S.A.", "N.V.", "AB") join the name before
  them (`sdb._SUFFIX`); weights, amounts and bare numbers after a ticker are
  stripped. A paste where fewer than half the parts are sure matches is not
  a list ("AI, robotics, automation" is a theme). A header word as a whole
  part ("Ticker", "Name", "Weight (%)": `_HEADER`) is dropped; lower-case class
  tickers dash too (`brk.b` → BRK-B). A name that lands on the company of a
  ticker typed elsewhere in the paste (a spreadsheet row "NOV IBIS, Novo
  Nordisk") is dropped for that listing, unless it names a share class
  ("GOOGL, Alphabet Class C" stays two). The whole list runs under
  one 20 s `Deadline`; truncation (50 names, 4,000 characters), duplicates
  and unfinished parts are warnings, never silent.
- **`run`** — names and pure sector/region screens offline on the pack;
  criteria through `yf.screen`, deduped to home listings, regions checked on
  the home listing (LLY.DE is not a European company); `Ticker.info` for the
  cards shown. Results cached 10 min per query; `limit` (5 inline, 25 per page
  in the full-screen table, at most 50) pages through them. A chip
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

### Names the pack lacks (`symbol_db.yahoo_lookup`)
Yahoo's screener misses some listings entirely: no Gedeon Richter line, on
any venue, is in the pack (2026-10-04). Its live Lookup endpoint knows them.
`yahoo_lookup(query)` wraps `yf.Lookup(q).get_all()` (equities and ETFs),
keeps lines with a close in the last three months, orders home venues first,
borrows the fullest name among the company's lines ("RICHTER" on Budapest),
and caches a day (an empty answer ten minutes). Group `YAHOO_GROUP` (-1).
Used only when the pack can't place a name: company search (best pack score
under 85; a Yahoo name containing the typed words ranks first), AI picks the
pack lacks, the Add popover, an import row, and a history stand-in.
`same_company(a, b)`: equal names without legal forms, the shorter (two
words or more) inside the longer, or the same first two words when neither
is generic (`_GENERIC`: Bank of America is not Bank of Ireland, China
Resources Beverage not China Resources Land); one shared word is never
enough (Apple vs Apple Hospitality REIT). An AI pick or a pasted name that
only Yahoo knows must match by ticker or name, never Lookup's first hit.

### Wiring
`resolver._symbol_db_lookup()` uses `lookup()`. With no pack yet (first
minute of a fresh install, or the switch off before any download) lookups
return nothing and the resolver falls through to `yf.Search`; search says the
list is downloading. Switching off stops the download; a copy already on disk
keeps working.

## 7. `xlsx_export.py`

### Design contract
**"Everything you can see in the app, in one file", readable by a newcomer
and usable by a quant or an AI as context.** When new columns / analytics /
fields land in the dashboard, extend `xlsx_export.py`. The contract lives in
the module docstring and the HTML comment by the topbar `#export` button
(`index.html`); keep them in sync.

### Sheets (2026-10-13 redesign, "story first" — the owner's pick)
1. **Read me** — what each sheet is, one plain sentence per portfolio
   (`headline()`: return vs the S&P 500, worst fall, top-5 share), how to
   read periods, currency, units and colours.
2. **Overview** — every portfolio, 1Y and 5Y side by side.
3. **Positions / Ledger** — My Investments, when there is a book.
4. **One sheet per portfolio**: headline → Performance (each period's
   Portfolio / S&P 500 / Nasdaq-100 next to each other, plus "What it
   means") → Valuation (look-through multiples with coverage) → Analysts →
   Where the money is (sector, size, industry) → Concentration → What drove
   the return (Carino contributions, they sum to the total) → Notes (the
   analytics' warnings: stand-ins, missing history) → Holdings as an Excel
   table. Weighting = the view's active preset (`_resolve_active_weights`),
   else by market value.
5. **Data** — every portfolio figure as a long table (portfolio, period,
   section, key, label, series, value, unit), built by `_Data` while the
   sheets are written.
6. **Definitions** — `definition_rows()`; each header's hover comment too.

**Units:** percent figures stay in percent points (as the app computes
them) with an escaped `0.0\%` format; fractions use Excel's `0.0%`. Only
formats every viewer reads alike: macOS Quick Look scales a quoted `"%"`
by 100 and ignores `[Color10]`, so losses are `[Red]` in the format and
gains turn green by one conditional-format rule per sheet (`_colour_gains`).

### Performance contract (measured 2026-10-13, 10 portfolios, 138 tickers)
- Before: each portfolio × period (20 runs) downloaded its own prices,
  re-downloaded FX pairs uncached, retried dead tickers one after another
  (~5 s each) and called `Ticker.info` per holding twice. Cold: **366 s**.
- Now: `analytics.prefetch_closes` downloads every holding, benchmark,
  sector ETF and `^IRX` once over the longest warm-up window (10y) and cuts
  the 2y cache from it; beside that download (Ticker calls keep per-object
  state; `yf.download` does not, so never two downloads at once) it fills
  the FX histories and any missing analyst target ranges. FX conversion
  reads the per-currency cache (`fx.prefetch_usd_series`), missing-symbol
  retries run four at a time. The runs then read caches (~5 s for all 20).
- Cold is Yahoo's floor: ~45–90 s for the one 10-year download depending on
  Yahoo's latency; 123 s end to end on a slow-Yahoo day. Warm: **< 1 s**.
- The analytics run in the display currency, so tabs already computed in
  the app are cache hits. `ANALYTICS_BUDGET_S` (120 s) bounds the loop: a
  run past it is written as "not computed in time" instead of hanging.
- The button polls `/api/export-progress` (downloading prices → analysing
  n/N → writing) and gives up after five minutes.

### Adding a new column to the holdings table
- If it's a row-payload key, it appears automatically via the "extras"
  pass (any key not in `HOLDINGS_PRIMARY_COLS` or `HOLDINGS_SKIP_EXTRAS`
  gets appended).
- If it's an analyst column, add a `(key, label)` tuple to `ANALYST_COLS`
  and its source keys to `_analyst_cell`.
