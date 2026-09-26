# MLNews — ML News-Sentiment Design Record (mlsent-v1 → v1.1)

Frozen decisions + measured results for the return-supervised news-sentiment
model. In the app it is the **Market read** — one of two peer engines on the
News tab, beside the LLM **News read** (CLAUDE.md §4); the two are never
blended. Sections 1–10 are the mlsent-v1 build (2026-07); §11 is the 2026-09
recalibration (mlsent-v1.1, shipped) and the v2 retrain (not shipped).
Companion document: `docs/ml_sentiment_lit_review.md` (60-paper grounding).
Pipeline code: `ml/` (training) + `portfolio_tracker/{relevance,ml_features,
ml_sentiment}.py` (shared/production).

## 1. Problem

The LLM sentiment tiers (news_sentiment.py) lack a return-grounded
calibration: tiers sat on fixed thresholds until 100 observations accumulate,
and the 5 categories had no standardized meaning. This pipeline grounds them
in realized market reaction: label historical news with the stock's
subsequent open/close-window return (market-adjusted, vol-standardized),
train text -> expected-return, define tiers as robust quantile bands of the
predicted-return distribution.

## 2. Label — the decision the user selected (method #1 of 5 ranked)

**SAR: vol-standardized, beta-adjusted, timing-aware abnormal return.**

```
abn = r_win − β*·m_win          β* = Blume-shrunk (0.67·β̂ + 0.33) rolling
SAR = abn / σ_win_type               252d daily beta vs SPY, clip [0,3]
```

- **Windows (session-classified, ET):** after-close (≥16:00) or non-trading
  timed → `overnight` close(D0)→open(D1) with σ_co; pre-open (<09:30) →
  `preopen_oc` open(D)→close(D) with σ_oc; intraday → `intraday_cc`
  close(D)→close(D1) with σ_cc (starts 16:00 ≥ publication — contamination-
  free, captures AH + next-day reaction); date-only → `dateonly_cc`
  close(D0)→close(D1), the only window guaranteed to start after any possible
  publication time (reverse-causality guard). Weekend/holiday: D0 = previous
  trading day (market closed between D0 close and publication → price at
  publication = close(D0); no contamination).
- σ = trailing 60-obs std of the SAME window-type return (min 40), shift(1).
  Window-type-specific σ per Lou-Polk-Skouras (overnight ≠ intraday process).
- All trailing stats point-in-time: value at D uses only ≤ D−1 (unit-tested
  with a synthetic jump fixture, tests/test_ml_labels.py; spot-checked vs raw
  prices in 08_validate test 3).
- Winsorized ±5σ. Raw `abn` (%) and `r_cc_baseline` stored alongside.
- **Beta convention: daily (analytics.py style)** — the hedge ratio must
  match the label frequency and be computable point-in-time from the FNSPID
  panel itself.

## 3. Data (FNSPID) — audit results that shaped the build

| Finding | Number | Consequence |
|---|---|---|
| Raw news rows | 15,550,328 | |
| Rows with a ticker tag | 5,745,701 | the labelable corpus (rest is market-wide) |
| Date-only timestamps | **98.5%** | `dateonly_cc` window dominates; timed classes kept as features + high-precision strata |
| Timed-row timezone | **ET-naive** despite literal " UTC" suffix | Benzinga pre-market peak at 06h proves ET (06 UTC = 01 ET is impossible news flow). ~27k publisher-NULL 2023 rows look truly UTC — left on the ET rule; misreading only swaps between windows that both start at close(D). |
| Symbols missing prices | 35.8% by symbol, **14.6% row-weighted** | under the 15% gate; dropped + logged |
| Coverage | 2009-02 → 2023-12, 9,469 symbols | holdout = Jul–Dec 2023 |
| Labeled rows (post filters + dedup) | **4,332,774** | dedup removed 7.65% syndicated near-dups |
| SAR distribution | mean −0.018, std 1.14, p01/p99 ±3.7 | bell-shaped as designed; slight left skew |

Prices: FNSPID full_history (7,693 tickers incl. delisted — survivorship-safe;
SPY present 1993–2023 so the trading calendar needs no yfinance).

## 4. Relevance heuristic (deterministic — NO parameters fitted on FNSPID)

`portfolio_tracker/relevance.py`. Multiplicative: mention position (title
1.0 / lead-150-chars 0.6 / body 0.35 / tagged-only 0.2, ticker regex OR
symbol_db company-name fuzzy ≥88) × co-mention penalty 1/(1+0.4(n−1)) ×
boilerplate 0.45 (listicle/roundup regexes) × long-headline 0.85 ×
publisher-tier (0.75–1.0). Clip [0.05, 1]. Corpus distribution: median 0.19,
p75 0.66 — consistent with Boudoukh et al.'s "~half of tagged news is not
firm-relevant". Co-mention count at train time = rows sharing the story URL
on the same window day; at serve time = Finnhub `related` field.
**Hand-set by construction** so the user's future ML relevance model can be
trained on this same corpus without circularity.

## 5. Features (shared featurizer, `portfolio_tracker/ml_features.py`)

- Hashed TF-IDF: HashingVectorizer 2^18, uni+bigrams, alternate_sign=False,
  title + first 600 summary chars (memory contract), × train-fitted idf,
  L2. Stateless hashing ⇒ no vocabulary skew between train and serve.
- Dense (35): LM lexicon score + missing flag + uncertainty ratio (reusing
  lexicon.py), 16 event regex flags, publisher tier, relevance,
  log1p(dups), log1p(co-mentions), session one-hot, day-of-week, month,
  title/summary token counts, trailing-only market context (SPY 1d/5d ret,
  20d vol, ticker 5d ret as of D0).
- **The same-window index return is in the LABEL (β-adjustment), not the
  features** — it is unknowable at prediction time; leaking it would train a
  model that can't be scored live.
- Train/serve parity enforced by tests/test_ml_sentiment.py (same article →
  identical vector through both paths) and a feature_schema.json match gate
  at artifact load.

## 6. Training

Stratified 3.0M-row training sample (year × session_class, proportional with
20k floor, deterministic md5 ordering) from the 3.73M-row pre-holdout pool;
holdout = ALL 601,633 rows with d1 ≥ 2023-07-01, never passed to fit.
FLAML 2.3.5, estimator lgbm only, metric mse, 4h budget, n_jobs 8,
eval_method cv + split_type time + 3 splits (ordered expanding folds — rows
pre-sorted by d1, asserted monotonic), retrain_full, mem_thres 3GB.
Observed matrix: ~50 nnz/row → ~2 GB CSR (comfortable on 8 GB).

**Results (run of 2026-07-14, 366 min wall incl. bounded retrain):**
best_config = 56 trees, 17 leaves, lr 0.054, min_child 15, reg_lambda 3.19,
max_bin 31 — small and heavily regularized, the expected winner under honest
time-ordered CV on noisy financial text. Holdout (601,633 rows, Jul–Dec 2023):
MSE 1.111, MAE 0.715, R² −0.004 (per-event R²≈0 is the domain norm),
**group Spearman IC = 0.0263** (in-sample 0.058 — healthy shrinkage).
Top-vs-bottom predicted decile realized SAR spread +0.031σ; bullish side
ranks cleanly, bearish side noisier.

**First training attempt post-mortem (2026-07-13/14, killed after 16h):**
LightGBM's sklearn API re-bins the sparse Dataset per trial × fold and FLAML
varies `log_max_bin`; at 262k columns the multi-val bin construction consumed
~100% of runtime (two stack samples 4h apart: all `PushDataToMultiValBin`,
zero tree frames). Fixes now baked into 06/07: df-pruning column mask (top
2^16 buckets = 85.1% of term mass, df cutoff ≈366, ships in artifact),
`log_max_bin` fixed at 5 (Dataset construct: 4s measured), and probe-sized
caps n_estimators≤300 / num_leaves≤256 bounding the worst trial ≈1.3h.

## 7. Tier cuts (09_tier_cuts.py)

Calibration = chronological refit predictions on Jan–Jun 2023 (OOS, holdout
untouched). Grid of asymmetric quantile 4-tuples (p1 ∈ {3,5,8,10}%, p2 ∈
{30,35,40,45}%, skew-shifted mirrors). Acceptance at group level: neutral
band |Spearman| < 0.02 with bootstrap-95% CI covering 0; strict tier-mean
monotonicity in ≥95% of 1,000 group bootstraps; very_* separated from
neighbors (non-overlapping 80% CIs); outer mass ≥3%; sub-period (2-month)
monotone ends. Objective: outer spread − 0.5×cut instability (bootstrap IQR).
Frozen as SCORE THRESHOLDS in tier_cuts.json; holdout verification is
report-only.

**Results (2026-07-14):** survivor found at base tolerance. Calibration:
masses 5/25/46/19/5, tier means −0.262/−0.048/−0.031/−0.006/+0.197 (strictly
monotone), neutral IC 0.0014, bootstrap mono rate 0.963, outer spread 0.46σ.
Holdout verification: tier means −0.086/−0.006/+0.003/+0.008/**+0.459** —
strictly monotone across all five tiers. Caveat: holdout MASSES shift
(8.9/63.4/24.4/1.6/1.6%) because the deployed booster's prediction
distribution differs from the calibration refit's — tier MEANING holds,
population shares don't match the design shares. Recalibrate cuts (rerun
09 against fresh OOS predictions) quarterly or whenever the live tier mix
looks degenerate.

## 8. Validation battery (08_validate.py) — gates

1 shuffled-label null (real IC > null mean + 4σ) · 2 time-shift ±5d (+5 IC <
0.3× real; −5 recorded, must not exceed real) · 3 point-in-time spot-check
(50 random stats recomputed from raw prices) · 4 walk-forward yearly IC
2015–2023 (≥7/9 positive, no year >40% of IC mass) · 5 baselines (≥1.3×
dense-only ridge; SAR label ≥ raw-label model on SAR IC) · 6 slices
(session/dollar-vol/relevance terciles; relevance top > bottom) ·
7 calibration deciles (monotone ends, CIs) · 8 PhraseBank sign accuracy
≥0.65 · 9 live evidence: the app's Track record (date-clustered daily IC,
long-short, hit rates — `portfolio_tracker/news_diagnostics.py`).

Results (fill after run): see `ml/data/reports/validation_report.json`.

## 9. Production (mlsent-v1.1)

`portfolio_tracker/ml_sentiment.py` loads `~/.portfolio_tracker/ml_model/
mlsent-v1.1/` (env `MLSENT_MODEL_DIR`) behind a schema gate. Per refresh, per
ticker: the 7-day window's articles, capped with `relevance.window_sample(60,
15)`, go through the v1 encoder (one batched predict); the score is
`ml_features.weighted_sar` — the recency (τ=3d) × source-tier × novelty ×
relevance weighted mean of the per-article predictions; `calibrate()` turns it
into z, a percentile, a tier and the band's expected next-day SAR. The raw
score, z, pct, tier and `market_model` are appended to the sentiment history,
which is both the live-anchoring reference (§11) and the Track record's input.
Missing artifact / dependency / schema ⇒ `available()` False, every call
`None`, the reason in `runtime_status()`; the News read is unaffected. Deploy
= `python ml/scripts/10_export_artifact.py --deploy`.

## 10. Engineering notes (what actually bit)

- DuckDB in-memory connections can't spill without `PRAGMA temp_directory`;
  even then, one global ORDER BY over 4.7M wide-text rows OOMs at 3 GB —
  stage C processes per-year (group key includes d1 ⇒ groups never span
  years).
- Per-group `pd.concat` dedup got SIGKILL'd by macOS memory pressure;
  vectorized rewrite (singleton groups bypass Python, numpy-indexed rapidfuzz
  only for multi-groups) does the full corpus in 6 minutes.
- pandas NaN is truthy: every text field needs `isinstance(x, str)` guards,
  not `x or ""` (bit three separate call sites).
- FNSPID's `Stock_price/full_history.zip` is 0.59 GB (not the planned 7.6)
  and half its members are `__MACOSX/._*` junk; news CSV is 23.23 GB with
  9.8M untagged rows.
- Python's builtin `hash()` is process-salted — artifact schema hashes must
  use hashlib.
- Training dependencies are not app dependencies and stay out of `envcheck`
  and the manifests: DuckDB (all v2 parquet IO; present in `pt`), pyarrow
  (stages A–C only — deliberately not in `pt`, because installing it switches
  pandas 3's string backing), FLAML. `tests/test_ml_labels.py` skips without
  pyarrow for that reason.
- DuckDB's `greatest`/`least` ignore NULLs: stage D's winsorisation turned
  every missing label into +5 until it became `CASE WHEN isfinite(raw) …`.

## 11. Recalibration and the v2 retrain (2026-09, v1.12)

**Why.** Live, the v1 tiers labelled ~93% of holding-days "bearish" (208 of
223 records; 15/15 in one portfolio). The frozen score thresholds of §7 had
drifted with the booster and the news mix. Two steps (plan D4): recalibrate
v1 (v1.1), and retrain a ticker-day model (v2); ship v2 only if it passes.

**Unit change.** Both now score a *ticker-day*, not an article: the 7-day
window of articles as of day D, capped exactly like the app's retained set,
labelled with the next 1-day / 5-day SAR from close(D). Price context is as of
D−1 (the last completed session). Labels: `05_build_labels.py` stage D,
15.14M rows (14.83M with a 1-day SAR, 14.80M with a 5-day one). FNSPID
timestamps are 98.5% date-only, so live articles are encoded as
`dateonly_cc` too. Encoder predictions for the panel are **cross-fitted by
year** (each year scored by a booster that never saw it) to keep label
leakage out of the window features; leakage spot-check 0 of 40.

**v2 (not shipped).** A second model over 17 ticker-day features (encoder
mean/max/min/weighted mean, lexicon, uncertainty, attention shock vs the
ticker's 60-day baseline, source mix, recycled-news share, freshness, ticker
and SPY return/vol context). Train [2011, 2023), select Jan–Jun 2023, holdout
Jul–Dec 2023; 5.27M panel rows. LightGBM rankers and regressors never beat a
dense ridge on the selection window (lambdarank@50 IC 0.0046 at 1d; negative
at 5d), so the winner was the ridge:

| Horizon | Holdout daily IC | t (NW) | Days | v1.1 on same holdout | Walk-forward years positive |
|---|---|---|---|---|---|
| 1 day | 0.0095 | 1.42 | 122 | 0.0072 | 9 / 9 (0.001–0.014) |
| 5 days | 0.0065 | 0.68 | 120 | 0.0022 | 7 / 9 |

Gates: IC ≥ 0.03 with t ≥ 3 — **fail** at both horizons; beat the dense
ridge — fail (it *is* the ridge); 5-day decile ends monotone — fail. It beats
v1.1 on the same days, but by less than the noise. Not shipped; the training
and validation code (`07 --stage window`, `08`) stays so the evidence can be
re-run, but there is no export path for it.

**v1.1 (shipped).** Score = the v1 encoder's weighted mean over the window
(panel column `enc_wmean`, the same `weighted_sar` the app runs). Calibrating
its percentile knots was the hard part — a weak regressor's output *level*
moves:

| Calibrate on → verify on | Tier mass (vbear/bear/no edge/bull/vbull) | Why it failed |
|---|---|---|
| Jan–Jun 2023, deployed booster → Jul–Dec | 2 / 13 / 81 / 1 / 2 % | deployed booster is in-sample on Jan–Jun (~5× more dispersed than live) |
| Jan–Jun 2023, cross-fitted booster → Jul–Dec | 3 / 26 / 68 / 1 / 2 % | a different booster; its output level differs |
| Jul–Sep 2023, deployed booster → Oct–Dec | 12.7 / 10.7 / 65.7 / 7.0 / 4.0 % | shipped knots; still 7.6 pp off design |

On Oct–Dec 2023 the shipped score has daily IC 0.0157 (t_NW 2.2, 59 days),
but the tails realized the wrong sign (very bearish +0.037σ, very bullish
+0.067σ next day). **No out-of-sample edge has been shown**; the app says so
in the Methodology and the Track record.

**Live anchoring (the user's decision, 2026-09-25).** Because the level
drifts, the percentile is computed against the app's own recent Market reads
once there are `MIN_LIVE_HISTORY = 200` of them in the last 90 days (same
model version, excluding the ticker-day being scored); until then against the
Jul–Sep 2023 knots. Tiers are by percentile: ≤5 very bearish, ≤15 bearish,
15–85 **no edge**, ≥85 bullish, ≥95 very bullish. The dict carries `anchor`
("live" / "training") and `n_history`, and the UI names which one is in use.
First live run (Hyper Scalers, training anchor): 13 of 15 no edge, 2 bullish,
0 bearish — the live score sits above the 2023 mean, exactly the drift the
live anchor corrects once it has history.

**Superseded:** the v1.6.1 "ML is the primary displayed signal" promotion and
its ML-vs-LLM diagnostics panels. The engines are now peers with different
questions, and live evidence is the Track record (date-clustered statistics:
daily cross-sectional Spearman IC, plain t at 1 day and Newey-West at longer
horizons, Wilson CIs on hit rates, "Too early" under 40 trading days).
