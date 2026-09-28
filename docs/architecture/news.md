# News & Sentiment

[← CLAUDE.md](../../CLAUDE.md) · part of the engineering notes; `§N` references name the original CLAUDE.md sections (map in CLAUDE.md).

From §4 (Backend): the two engines (News read, Market read), their fetch/cache layer, the model artifact and release procedure, divergence, history and the Track record. Settings → API keys (`keys.py`) is in [security.md](security.md#settings--api-keys-keyspy).

## 4. News & Sentiment (part of Backend)

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
  `.nvidia_key` found by walking up). Read at import, and **re-read without a
  restart** whenever Settings → API keys saves or clears one:
  `keys.reload_all()` calls `news_sentiment.reload_keys()` (also drops the
  cached OpenAI client, which captured the old key, and — when the NVIDIA key
  changed — clears the persisted LLM status, so a "key rejected" stops
  short-circuiting) and `finnhub_adapter.reload_keys()` (drops `_FH_CACHE` on
  a change), and re-arms both one-time "malformed config.json" warnings.
  **Any new module that caches a key needs a `reload_keys()` hooked in there.**

- *`keys.py` (Settings → API keys, v1.15): see [security.md](security.md#settings--api-keys-keyspy).*

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
  `src/convexity/envcheck.py` is the single runtime-dependency manifest,
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

*Reference pack (roadmap Phase 8, v2.0.0).* A new install has < 200 Market
reads of its own, so without help its percentile sits on the 2023 knots and
its Track record says "too early" for months. The reference pack is the
same model run daily over the S&P 500, built in CI and downloaded by the app.
- **Format** (`src/convexity/reference_pack.py`, the one definition, schema
  version 1): `manifest.json` (date, generated_at, model version, universe,
  row counts, per-source counts, `anchor_basis`, and `{sha256, bytes}` of the
  two data files), `anchor.json.gz` (`[[date, symbol, score], …]`, the last
  90 days) and `history.json.gz` (one record per ticker-day: `market_score,
  market_sar, market_z, market_pct, market_tier, market_model, n_articles,
  price, beta, fwd_1d, fwd_5d`). The history validator is an **allow-list**:
  any other key (a headline, a URL) makes the whole pack invalid, so article
  text can neither be published nor loaded. Deterministic gzip (mtime 0).
- **Builder** (`reference_build.py`, `convexity build-reference-pack --out DIR
  [--limit N] [--previous DIR] [--returns-only]`), run by
  `.github/workflows/reference-pack.yml` (ci.md). Parity by construction: the
  same commit as the app, news through `news_sentiment.collect_company_news`
  (Finnhub + yfinance, dedup, `window_sample` — the uncached core of
  `fetch_company_news`, split out for this) under the app's limiters, the
  Market read through `ml_sentiment.market_read`, forward returns through
  `news_diagnostics.forward_idio` (the Track record's own definition). Known,
  deliberate differences: the company name for the relevance heuristic comes
  from `sp500.json` (CI has no symbol DB; `market_read(company_name=…)`),
  beta is a 1-year daily OLS vs SPY (the local history stores the quote's
  beta), and Finnhub gets the dot spelling of class shares (BRK.B).
- **No look-ahead.** Each run's tiers are anchored on the *previous* pack's
  last-90-day scores only (rows dated today are dropped first, so a same-day
  rerun does not rank against itself); forward returns are joined only once
  the closes exist and are never recomputed.
- **Key and safety.** The builder reads `FINNHUB_API_KEY` from the
  environment only — never `config.json` or a legacy key file — so a run
  from a checkout cannot spend the owner's key. A run with fewer than half
  the names scored, or Finnhub answering for fewer than half, writes nothing
  (exit 1); a previous pack that fails verification aborts (exit 1) unless it
  is merely another model's (then the history starts fresh).
  `CONVEXITY_FINNHUB_BASE` (loopback http only) points it at a local stub for
  verification runs.
- **App side.** `reference_pack.start()` (boot warm-up and the start of each
  news refresh; daemon thread, single-flight) checks at most once a day
  (`manifest.json` mtime; an unchanged manifest downloads nothing and is only
  touched) from `BASE_URL` (`CONVEXITY_REFERENCE_URL` overrides, http for
  loopback only; redirects re-checked). Caps: manifest 64 KB, each file 32 MB
  (Content-Length and streamed), 256 MB decompressed (zlib `max_length`; a
  truncated stream fails on EOF). Then hash + size against the manifest,
  schema + model version, staging dir, `os.replace` with the manifest last.
  Any failure: one `[reference_pack]` line, `status()["state"] = "failed"`
  with the reason (Settings shows it, "— ignored"), previous copy kept. A copy
  older than `MAX_AGE_DAYS = 14` or for another model version is not used.
- **The switch** (Settings → Models & Data → Reference data;
  `<data>/state/reference_pack.json`, `CONVEXITY_REFERENCE_PACK=0` forces it
  off, `tests/conftest.py` sets that) turns off the download **and** its use.
  Privacy note in the UI: GitHub sees that a copy is running, never holdings.
- **Anchor.** `calibrate(score, cal, history, reference, reference_date)`:
  own reads ≥ 200 → `live`; else the pack's scores (≥ 200) → `reference`
  (`n_reference`, `reference_date`; `n_history` stays the app's own count);
  else `training`. `_market_read` asks for the pack only while the app's own
  history is short, excluding the (today, symbol) being scored. The News tab
  says "vs 500 S&P names, last 90 days (reference data, N days old)".
- **Track record "Model (500 names)".** `GET /api/news-diagnostics?source=reference`
  → `compute(history, forward_from_records=True)` (no price download), cached
  per pack in `server._REF_DIAG_CACHE`. Market read only — the News read is an
  LLM on the user's keys and is never built in CI; the card says so. Without a
  usable pack the payload carries `unavailable` with the reason.
