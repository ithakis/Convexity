# News Tab Redesign — Implementation Plan

**Status: IMPLEMENTED (v1) on branch `claude/portfolio-news-tab-redesign-013545`.**
User decisions applied: self-consistency default ON; quantile cuts 10/20/40/20/10
kept; KAPPA set from the sensitivity study in `scripts/kappa_sensitivity.py`
(results JSON beside it). All §7 phases (A-D) implemented. This document is kept as the design record.

**Validation results (2026-07-08):**
- PhraseBank benchmark (`scripts/benchmark_sentiment_prompt.py`, 300
  stratified sentences from Sentences_66Agree): **accuracy 0.900,
  macro-F1 0.901** vs FinBERT reference ~0.86. Zero positive↔negative
  confusions — all errors are neutral-boundary, the benign direction.
- Kappa study (S&P 100, n=99): production **KAPPA=0.2** (see §2.4).

This document is self-contained: it is written to be handed to a fresh
Claude Code session for implementation. Read CLAUDE.md first (§4 backend,
§5 frontend, §9 conventions). Everything here was decided jointly with the
user after research into (a) the current codebase, (b) the Bloomberg News
style guide ("The Bloomberg Way", Winkler), and (c) ~110 academic papers on
news sentiment analysis.

---

## 0. Goal

Rework the News tab so it does two things well:

1. **Bloomberg-style news summaries** — terse, number-led, specific;
   per-stock briefs plus a compact market-risk line. Tone and length follow
   the Bloomberg Way rules codified in §3.
2. **Quantitatively robust sentiment** — per-article scoring, deterministic
   aggregation, a systematic/idiosyncratic decomposition
   (`s_total = β·s_mkt + s_idio`), quantile-calibrated tiers, a local
   Loughran-McDonald dictionary cross-check, and built-in validation
   (rank IC vs forward idiosyncratic returns + score-distribution
   monitoring) so "uses the full range" and "correlates with market moves"
   are measured, not assumed.

Decisions already made by the user (do not re-litigate):
- News sources: **Finnhub + yfinance `tk.news`, merged and deduplicated**.
- Sentiment engine: **per-article LLM scoring (NVIDIA NIM, existing model)
  + deterministic aggregation + quantile calibration + Loughran-McDonald
  lexicon baseline**. No FinBERT / no torch dependency.
- Validation: **both** an in-app diagnostics panel and a one-time offline
  Financial PhraseBank benchmark script.
- Tab layout leads with a **flash-headline tape** and a **Movers & What to
  Watch** section; per-stock Bloomberg briefs live in the constituent
  breakdown; market summary stays compact (systematic-risk context, not a
  long wrap).
- Refresh: **manual only, staged** (market first, then constituents by
  portfolio weight), loading-chip progress. No auto-refresh.

## 1. Current state (what you're changing)

- `portfolio_tracker/news_sentiment.py` — Finnhub `/company-news` (7 days,
  ≤20 articles, headline+summary only) → ONE NIM call per ticker over ≤15
  headlines → single `{tier, score, summary}`; market = 10 headlines → 1
  sentence. 30-day disk cache in `.portfolio_tracker_news.json`, debounced
  persist, token-bucket limiters (Finnhub 55/min, NIM 60/min), circuit
  breakers, `/no_think` control token, 30s timeout. **Keep all of this
  plumbing** — limiters, cache/persist layer, key loading, retry logic are
  battle-tested. You are changing what is fetched, how it is scored, and
  what is rendered.
- `server.py` routes: `/api/news-sentiment`, `/api/news-market`,
  `/api/news-articles`, `/api/news-refresh` (POST).
- `static/app.js` ~4984–5185: News panel (3 cards + constituent table),
  `nsDot` NS column, `warmNewsSentiment()`.
- `static/index.html` ~113–137: `#news-panel` markup.
- yfinance news already parsed in `fetcher.py:603–631` (detail modal) —
  reuse that normalisation logic for the merged feed.
- Row data available per symbol in `DATA` /view rows: `pct_1d, pct_1w,
  pct_1m, pct_ytd, delta_ath, market_cap, sector, next_earnings` (detail),
  and analytics provides per-symbol `beta_spy` and weights. The frontend
  should POST this context to the backend at refresh time (see §4.3) so
  prompts are quantitative.

## 2. Architecture of the new sentiment engine

### 2.1 Ingestion (per ticker)

1. Fetch Finnhub `/company-news` (keep 7-day window, raise cap to 30 raw).
2. Fetch yfinance `tk.news` (~10 articles). Wrap in try/except; yfinance
   news is flaky — degrade silently to Finnhub-only.
3. Normalise both to the existing article dict shape
   (`headline, summary, source, datetime, url`).
4. **Deduplicate / novelty-weight**: cluster by normalised-title similarity
   (rapidfuzz `token_set_ratio ≥ 85` — rapidfuzz is already an optional
   dep via symbol_db; fall back to exact lowercase-title match if absent).
   Keep the earliest article of a cluster as canonical; record
   `n_duplicates` (syndication count is itself a salience signal). Stale
   repeats get down-weighted, not re-scored (Tetlock 2011: markets
   overreact to reprinted news).
5. Cap at 15 canonical articles by recency for the LLM call.

### 2.2 Per-article LLM scoring (one NIM call per ticker — same budget as today)

One call per ticker, but the response is a **JSON array**: for each
article `{id, score, event, relevance}` where
- `score ∈ [-1, +1]` — idiosyncratic sentiment only (prompt instructs:
  strip the market-wide component; macro news about the whole market gets
  `relevance: low`),
- `event ∈ {earnings, guidance, ma, analyst, legal_regulatory, product,
  insider, macro, other}` (event-level FSA, EFSA 2024),
- `relevance ∈ {high, med, low}` — is the article actually about this
  company (Finnhub's `related` field is noisy).

Plus one `brief` field: the Bloomberg-style per-stock summary (§3). The
prompt includes the quant context block (§4.3). Validate the array
strictly (same philosophy as `_validate_sentiment`); on malformed output
retry once, then fall back to scoring the batch as a single blob (today's
behaviour) so the pipeline never returns less than the current version.

Self-consistency (optional, config flag default ON for refresh): run the
scoring call twice at temperature 0.1 and average per-article scores;
disagreement > 0.5 on any article lowers `confidence`. Cost: 2 NIM
calls/ticker — still well inside 60/min for typical 20–40 ticker
portfolios (staged refresh spreads them anyway).

### 2.3 Deterministic aggregation (auditable math, not vibes)

```
w_a = w_recency(a) × w_source(a) × w_novelty(a) × w_relevance(a)
  w_recency  = exp(-Δdays / τ),  τ = 3 days (half-life ≈ 2.1d)
  w_source   = 1.0 tier-1 (Reuters, Bloomberg, WSJ, FT, AP);
               0.8 tier-2 (CNBC, Barron's, MarketWatch, Yahoo Finance);
               0.5 wires/promotional (PR Newswire, GlobeNewswire,
               Business Wire, Motley Fool, Zacks, SeekingAlpha);
               0.7 unknown
  w_novelty  = 1 / (1 + 0.5·log1p(n_duplicates))   (canonical article)
  w_relevance= {high:1.0, med:0.6, low:0.2}

s_idio = Σ w_a·score_a / Σ w_a          (∈ [-1,1])
confidence = f(Σ w_a, dispersion, self-consistency agreement) ∈ [0,1]
```

Weights live in a module-level dict with a docstring citing the rationale
so they're tunable in one place.

### 2.4 Systematic/idiosyncratic decomposition

- `s_mkt` — market sentiment scored per-article the same way from the
  merged general-news feed (Finnhub `/news` + yfinance news for SPY),
  aggregated with the same math.
- Per stock: `s_sys,i = β_i × s_mkt` (β = `beta_spy` from analytics; the
  frontend already has it after `requestAnalytics`; fall back to β=1).
- **Total signal: `s_total,i = clip(κ·s_sys,i + s_idio,i, -1, +1)`** with
  `κ = 0.2` (module constant; systematic tilt should color, not dominate,
  the stock signal — user's framing: "even though the stocks are doing
  well, they are impacted by S&P sentiment"). κ was fixed empirically by
  an S&P 100 sensitivity study (scripts/kappa_sensitivity.py, 2026-07-08,
  n=99): idio scores are compressed (σ≈0.12), so κ=0.5 let β·s_mkt reach
  1σ on an ordinary day (19% sign flips, rank-corr 0.82→0.45 under
  stress); κ=0.2 keeps flips ≤11% and rank-corr ≥0.92 on typical days
  while still mattering (~16% flips) at |s_mkt|=0.6.
- Portfolio aggregate: `S_pf = Σ w_i·s_total,i = κ·β_pf·s_mkt + Σ w_i·s_idio,i`
  using active weights (equal/cap/preset — read from current mode).
  Render the decomposition explicitly: systematic bar + idiosyncratic bar.

### 2.5 Loughran-McDonald lexicon baseline (free cross-check)

New small module `portfolio_tracker/lexicon.py`:
- Ship a trimmed LM word list (positive + negative + uncertainty stems,
  ~4k words) as a data file `portfolio_tracker/data/lm_lexicon.json`
  (LM lists are free for non-commercial use; generate once at build time —
  the implementation session should download from
  https://sraf.nd.edu/loughranmcdonald-master-dictionary/ or vendor the
  word lists from a public mirror, and note the license in the file
  header).
- `lm_score(text) -> float` in [-1,1]: `(pos - neg) / (pos + neg + ε)`
  over headline+summary tokens, with simple negation flip on
  {not, no, never, without} within 3 tokens.
- Computed per canonical article at ingestion (pure Python, ~µs); aggregate
  with the same weights → `s_lm`.
- **Disagreement flag**: `|s_idio - s_lm| > 0.6` AND both |·|>0.2 with
  opposite signs → flag the ticker in the UI ("LLM/lexicon disagree") and
  cap confidence at 0.5. This is the guardrail against LLM hallucinated
  polarity.

### 2.6 Quantile calibration → tiers (guarantees full range usage)

Do NOT trust the model's self-declared tier. Keep raw `s_total` continuous,
then map to the 5 tiers by percentile against the trailing score history
(all symbols pooled, last 90 days, min 100 observations; before that,
fall back to fixed thresholds ±0.15/±0.5):

```
p < 10%          → very_bearish
10% ≤ p < 30%    → bearish
30% ≤ p ≤ 70%    → neutral
70% < p ≤ 90%    → bullish
p > 90%          → very_bullish
```

By construction the tier distribution over time is 10/20/40/20/10 — the
"full range" requirement is satisfied structurally and verified visually
by the histogram in the diagnostics panel.

### 2.7 Sentiment history persistence (prerequisite for validation)

New file `.portfolio_tracker_sentiment_history.json` (gitignore it):
append-only records `{date, symbol, s_idio, s_lm, s_mkt, s_total, tier,
confidence, n_articles, price}` written once per refresh per symbol
(overwrite same-day entry on re-refresh). Cap: 400 days. Written through
the same debounced-persist pattern as the news cache.

### 2.8 Validation

**In-app diagnostics panel** (collapsible section at the bottom of the
News tab, "Model diagnostics"):
- **Rank IC**: Spearman correlation between `s_idio` at t and forward 1d /
  5d **idiosyncratic** return `r_i - β_i·r_SPY` (closes from the existing
  `_BULK_CLOSE_CACHE` / sparkline data; only entries with ≥1d elapsed).
  Show IC, n, and a naive t-stat. Label honestly: "n < 200 — indicative
  only" until the panel has enough history.
- **Tier monotonicity**: mean forward 1d return per tier — should be
  monotone increasing from very_bearish to very_bullish.
- **Score histogram**: distribution of raw `s_total` (last 90d) with tier
  cut lines — shows range usage at a glance.
- Backend: `GET /api/news-diagnostics` computes all three from the history
  file + cached closes. No new network calls.

**Offline benchmark** `scripts/benchmark_sentiment_prompt.py`:
- Downloads Financial PhraseBank (Malo et al. 2014, free), runs the
  per-article scoring prompt through NIM on the "sentences_66agree" split
  (~4k sentences; ~70 batched calls), reports accuracy / macro-F1 /
  confusion matrix vs the labels, and prints FinBERT's published ~0.86
  accuracy as the reference bar. Run manually, not in CI.
- Purpose: certify the prompt + model before shipping; re-run when the
  prompt or model changes.

## 3. Bloomberg Way editorial spec (the prompt contract)

Codified from "The Bloomberg Way" (Winkler): headline-first discipline,
the four-paragraph lead (theme → authority → details → what's-at-stake),
"prefer the short to the long, the familiar to the fancy, the specific to
the abstract", show-don't-tell, numbers with context.

**Per-stock brief** (`brief` field, rendered in constituent breakdown):
- 2–4 sentences, ≤ 60 words. Compressed four-graf lead:
  S1 *theme*: what happened + magnitude + why ("Nvidia fell 3.2% this week
  after Washington widened chip-export curbs").
  S2 *details*: the numbers vs expectations (consensus, guidance, targets —
  use the analyst-target context provided).
  S3 *stake* (optional): what it means for the holding ("shares trade 18%
  below their high with earnings due May 22").
- Ticker-first, present/past simple, active voice. Banned: "exciting",
  "concerning", "impressive", "notably", "significantly", "continues to",
  hedge words, exclamation marks. Every claim traceable to a provided
  article or the quant context block. If coverage is thin: one sentence
  stating that, no padding.

**Headline for the tape**: keep the raw article headline (do not rewrite
20×N headlines through the LLM — cost) but prefix ticker + Δ where known.

**Market line** (compact, per user decision — systematic risk context, not
a wrap): 1–2 sentences, may reference SPY/QQQ/^TNX moves (fetched free via
yfinance closes, injected into the prompt) — e.g. "S&P 500 down 1.1% as
yields rose; risk appetite soft, a headwind for high-beta holdings."

**Portfolio aggregate line**: 1 sentence template rendered from the math
(not the LLM): "Portfolio signal +0.18 (bullish tilt): market +0.05·β 1.12,
stock-specific +0.12; 3 of 24 holdings flagged bearish."

## 4. Backend changes

### 4.1 `news_sentiment.py` (main rework)
- `fetch_company_news` → merged Finnhub+yfinance, dedup/novelty (§2.1).
- New `_SENTIMENT_ARRAY_PROMPT` implementing §2.2 + §3 (few-shot with 2–3
  example articles→JSON). Keep `/no_think`, keep validation-with-fallback.
- New pure functions: `aggregate_scores(articles_scored) -> (s_idio,
  confidence)`, `combine_signal(s_idio, beta, s_mkt) -> s_total`,
  `calibrate_tier(s_total, history) -> tier`. Unit-test these (they're
  deterministic — extend `tests/test_news_sentiment.py`).
- History append/read helpers (§2.7).
- Sentiment dict shape gains keys: `s_idio, s_lm, s_sys, s_total,
  confidence, n_articles, events (list), disagreement (bool), brief`.
  Keep `tier`/`score`/`summary` keys populated (score=s_total,
  summary=brief) so the NS column dot and any old consumers keep working.
- Bump the persisted-cache `version` and migrate/ignore v1 entries.

### 4.2 `lexicon.py` (new)  — §2.5. Stdlib-only scoring.

### 4.3 Routes (`server.py`)
- `POST /api/news-refresh` body gains optional `context`: `{beta_pf,
  weights: {sym: w}, rows: {sym: {pct_1d, pct_1w, pct_ytd, delta_ath,
  next_earnings, target_upside}}}` — the frontend already has all of this;
  the backend injects it into prompts and the decomposition.
- Staged semantics: refresh market first, then symbols sorted by weight
  descending. Stream progress: convert the response to NDJSON
  (`type: market`, then `type: symbol` per ticker, then `done`) mirroring
  the `/api/quotes-stream` pattern so the tape/table fill progressively.
- `GET /api/news-diagnostics` (§2.8).
- `GET /api/news-sentiment` / `/api/news-articles` — return the enriched
  shapes; keep response keys backward compatible.

## 5. Frontend changes (`index.html`, `app.js`, `style.css`)

New `#news-panel` layout, top to bottom:

1. **Header strip** — Portfolio signal gauge: `S_pf` number + tier badge +
   a small two-segment horizontal bar showing systematic vs idiosyncratic
   contribution; the rendered portfolio aggregate line (§3); market line
   with market tier badge; "As of" stamp; staged Refresh button with
   loading-chip progress ("market ✓ · 7/24 stocks").
2. **Movers & What to Watch** (two cards side by side) —
   *Movers*: top 3–5 |pct_1d| holdings, each: ticker, move, and its
   highest-|score| high-relevance headline (attribution). Pure frontend
   join of `DATA` + articles.
   *What to Watch*: holdings with earnings in the next 14 days
   (`next_earnings`), sorted by date; plus any ticker flagged
   `disagreement` or very_bearish.
3. **Flash tape** — chronological merged headline feed across all
   holdings: monospace timestamp, ticker chip, headline (linked), source,
   per-article score dot (red/gray/green), event-type chip. Filter row:
   ticker multi-select + event-type chips + min-relevance toggle.
   Virtualise/cap at ~200 rows.
4. **Constituent breakdown** — existing table upgraded: columns Ticker,
   Company, Price, %1D, β, s_idio, s_sys (β·s_mkt), s_total dot+tier,
   confidence (thin bar), events (chips), ⚑ disagreement. Expandable row →
   the Bloomberg brief + that ticker's articles.
5. **Model diagnostics** (collapsed by default) — IC panel, tier
   monotonicity bars, score histogram with tier cuts (§2.8). Inline SVG,
   same charting idiom as analytics panel.

NS column in the main table: dot now colored by calibrated `s_total` tier;
hover shows the brief. No other main-table changes.

## 6. What does NOT change

- Key loading, rate limiters, circuit breakers, `/no_think`, 30s timeout,
  debounced persistence, cache-restore-on-failed-refresh logic.
- Manual-refresh-only policy; 30-day cache TTL semantics.
- `get_cached_sentiment()` O(1) hot-path contract for `fetch_one`.
- Graceful degradation ladder (no Finnhub key → nothing; no NVIDIA key →
  articles + LM lexicon score only — note: with the lexicon module, the
  no-NVIDIA case now still gets a usable quantitative signal).
- Mutual exclusion of News panel with Portfolio panel.

## 7. Implementation order (each phase shippable)

1. **Phase A — ingestion + math core**: merged feed, dedup, lexicon
   module, aggregation/combination/calibration functions + unit tests.
   (No UI change yet; NS dots silently improve.)
2. **Phase B — scoring + history**: array prompt, self-consistency,
   enriched sentiment shape, history file, NDJSON staged refresh route.
3. **Phase C — UI**: new panel layout (header strip, movers/watch, tape,
   upgraded table, expandable briefs).
4. **Phase D — validation**: diagnostics route + panel; PhraseBank
   benchmark script; run it once and record results in `docs/`.

Per CLAUDE.md: syntax-check every touched .py, restart server to verify,
curl the new routes, run pytest. Version bump discussion happens at the
end with the user (standing policy §15).

## 8. Rate-limit budget (worst case, 40-ticker portfolio, cold refresh)

- Finnhub: 40 company-news + 1 general = 41 calls (< 55/min budget; staged
  order spreads them).
- yfinance news: 40 lightweight calls, no hard limit, sequential with the
  existing worker pool.
- NIM: 40 tickers × 2 (self-consistency) + 1 market = 81 calls → ~85s at
  60/min with the limiter sleeping as designed. Acceptable for a manual
  staged refresh with progressive rendering; drop to 1 pass (41 calls,
  ~45s) via the config flag if too slow.

## 9. Open questions for the implementing session (small, decide inline)

- Exact few-shot examples in the array prompt (write 2–3, verify JSON
  stability against the live model before wiring the UI).
- rapidfuzz availability check pattern — mirror `symbol_db.py`'s optional
  import.
- Whether `pct_1d` context at *refresh time* vs *article time* mismatch
  matters for the brief wording (it doesn't for v1; note it).

## 10. Key literature (for docstrings / rationale)

- Tetlock (2007), J. Finance — media tone → returns; negative words carry
  the signal. Tetlock et al. (2008) — firm-specific news → idiosyncratic
  returns.
- Loughran & McDonald (2011), J. Finance — domain lexicon; word lists.
- Tetlock (2011) — stale news; markets overreact to repeats.
- Araci (2019) / Yang et al. (2020) — FinBERT, ~0.86 acc on PhraseBank
  (the benchmark bar).
- Lopez-Lira & Tang (2023) — LLM headline scores predict next-day returns;
  quantile long-short + rank IC as the validation design; small models
  need careful prompting.
- Malo et al. (2014) — Financial PhraseBank dataset.
- EFSA (2024) — event-level financial sentiment.
