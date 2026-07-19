# MLNews — ML News-Sentiment Design Record (mlsent-v1)

Frozen decisions + measured results for the return-supervised news-sentiment
model. Companion documents: `docs/ml_sentiment_lit_review.md` (60-paper
grounding), plan at `.claude/plans/` (approved 2026-07-13). Pipeline code:
`ml/` (training) + `portfolio_tracker/{relevance,ml_features,ml_sentiment}.py`
(shared/production).

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
- **Beta convention: daily (analytics.py style), NOT kappa_sensitivity's
  weekly** — the hedge ratio must match the label frequency and be
  computable point-in-time from the FNSPID panel itself. The two conventions
  coexist deliberately; kappa's weekly beta serves its own study.

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
≥0.65 · 9 live shadow vs LLM (11_shadow_compare_llm.py, 2–4 weeks, ML IC ≥
LLM IC − noise before UI surfacing).

Results (fill after run): see `ml/data/reports/validation_report.json`.

## 9. Production

`portfolio_tracker/ml_sentiment.py`: lazy singleton loads
`~/.portfolio_tracker/ml_model/mlsent-v1/` (env `MLSENT_MODEL_DIR`), schema
gate, per-article predict ~1–3 ms, aggregate = recency(τ=3d) × source-tier ×
novelty × continuous relevance weights → `ml_sar`, `ml_score=tanh(sar/2)`,
`ml_tier` (frozen cuts), `ml_confidence`, `ml_n`. Wired additively into
`get_news_sentiment()` and `_history_append` (fields `ml_sar`, `ml_tier`) —
absent artifact ⇒ fields absent, LLM path untouched. Deploy = `python
ml/scripts/10_export_artifact.py --deploy`. Optional deps `lightgbm`,
`scikit-learn` documented in requirements.txt.

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

## 11. Shadow → primary promotion (v1.6.1, 2026-07-16)

Promoted the ML model from shadow to the **primary displayed signal** in the
News tab. `get_news_sentiment` now sets the canonical `tier`/`score` to the ML
values when available (LLM preserved as `llm_tier`/`llm_score`, `disp_source`
flag), writes per-article ML scores onto the article feed, and persists
`ml_score`/`ml_confidence` to history. `compute_diagnostics` gained four live
panels (rolling IC, calibration curve, ML–LLM agreement grid, coverage &
confidence); the frozen backtest write-up moved into a new in-app **Methodology**
article. Graceful LLM fallback is unchanged (missing artifact ⇒ `disp_source=
"llm"`, LLM drives the UI). No model/artifact/featurizer change — train/serve
parity and all `tests/test_ml_*` remain intact. Live ML performance continues to
accrue in Model Diagnostics; quarterly tier-cut recalibration still applies.
