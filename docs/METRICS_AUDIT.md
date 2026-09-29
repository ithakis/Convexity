# Metrics Audit Report

## Audit of 2026-09-29 — every formula, unit and explanation

A line-by-line review of the math in `fetcher.py`, `helpers.py`, `fx.py`,
`analytics.py`, `frontier.py`, `mpt.py`, `news_diagnostics.py`,
`xlsx_export.py` and every formula shown in the app (tooltips, the Guide, the
optimizer Guide, the Methodology). Yahoo's units were checked against live
data (AAPL, KO, TSM, TM, 7203.T, SHEL.L, BABA, NVO, 005930.KS, ASML.AS).
Regression tests: `tests/test_finance_math.py`.

### Wrong numbers, fixed

| Area | What was wrong | Fix |
|---|---|---|
| D/E | Yahoo's `debtToEquity` is in percent (78.4 = 0.78×); the app showed "78.40" under the formula Debt/Equity, which gives 0.78 | Shown as "78.4%", formula × 100%, description says 100% = 1.0× |
| Dividend yield | yfinance ≥ 1.0 reports `dividendYield` in percent; the `> 1 ⇒ /100` guess read every yield under 1% as a fraction (0.32 → 32%). LSE names divided a pound rate by a pence price (SHEL.L 0.03% instead of 3.2%). ADRs used a home-currency trailing rate over a USD price | Raw yield always / 100; minor-unit prices scaled; trailing fields only in one currency |
| ADR / cross-currency multiples | Yahoo's EV/EBITDA, EV/Revenue, P/S, P/B mix a USD cap with TWD/JPY/CNY statements (TM 6.3 and 15.3 vs 11.9 and 0.91 on 7203.T; TSM 5.2 and 93 vs ~23 and ~12) | Rebuilt in the statement currency with spot FX (`fetcher._currency_consistent`); None without a rate |
| FCF yield | FCF (statement ccy) ÷ market cap (trading ccy): TSM 31%, TM −1614% | Same currency before dividing |
| Cap weights | Raw caps in mixed currencies: a Tokyo name (¥34T) took ~87% of a book with AAPL | USD caps everywhere (app, analytics buckets, Excel, BL prior) |
| Market cap display | LSE caps (GBP) formatted as pence: SHEL.L £2.1B instead of £208B; ADR revenue/FCF labelled $ while in yen | `majorCcy()`, statement currency for statement figures |
| Size buckets / wtd market cap | Local-currency caps against dollar thresholds; average of mixed currencies | USD caps; average returned in the display currency |
| YTD (table, detail, analytics) and every period return | Base = first close of the window, dropping the first session's move (YTD missed the year's first trading day) | Base = last close on or before the window start |
| P/E column | `trailingPE or forwardPE`: loss-makers showed a forward multiple under "trailing" | Trailing only; negative multiples n/a |
| Negative multiples | Negative P/E, EV/EBITDA, PEG, P/B coloured as the "cheapest" on screen | Not meaningful → n/a |
| Sharpe / Sortino | Risk-free rate 0 (overstated Sharpe by ~rf/σ ≈ 0.25) while the formula showed R_f | Excess over the daily T-bill (^IRX) in USD; 0 elsewhere, stated |
| Weighted P/E, P/S, EV/EBITDA | Arithmetic mean of multiples (overweights expensive names) | Weighted harmonic mean — the look-through multiple |
| Rating distribution | Raw vote counts × weight (50-analyst name outvoted a 5-analyst one 10:1) | Each holding's vote shares × weight |
| Contribution to return | w × holding's buy-and-hold return; did not add up to the (daily-rebalanced) portfolio return | Carino log-linked daily contributions: sum = period return |
| Ledoit-Wolf | Intensity left out ρ (the target's own noise), overstating shrinkage | Full LW (2004) estimator, tested against `covCor.m` |
| BL views | q = target/price − 1 − rf: price return vs a total-return prior | + forward dividend yield |
| Hit-rate CIs | Wilson interval assumed independent ticker-days | Date-clustered effective sample (design effect) |
| FX basket index | Arithmetic mean of price relatives (biased up) | Geometric mean |
| ROE / D/E fallback (detail) | Annual balance sheet before quarterly; one quarter's profit used as "TTM"; negative equity gave a positive ROE | mrq first; TTM needs 4 quarters; negative equity → n/a |
| RSI | A flat series returned 100 (overbought) | 50 |
| Excel | "Change ($)" on non-USD rows; Forward P/E / EV/EBITDA / PEG / Beta / Div Yield blank whenever analytics ran; raw keys with no units | Labels with units, percent formats, row values |

### Explanations corrected

Forward P/E is on the next fiscal year's EPS estimate (not NTM); revenue and
earnings growth are the latest quarter year-over-year (not TTM); Yahoo's beta is
5-year monthly (market sensitivity, not volatility); the 52-week range and ATH are
dividend-adjusted daily closes; returns are total returns; average volume is 30
sessions; "Analysts covering" is not unique analysts; a BL asset without a view
is not "left at the prior" (it moves through Σ); CVaR30 at 95% is the average loss
in the worst 5% of months; the Guide's P/S and P/E "heat anchors" no longer
existed; the Methodology omitted the reference-pack anchor.

### Checked and correct

Annualised vol, CAGR, max drawdown, beta / R² / tracking error, HHI and
effective N, Bollinger %B (population σ, as Bollinger), MACD 12/26/9, Wilder RSI,
SMA, EPS surprise, target upside, the Rec Δ6M score, MSPR, FX conversion of
closes, the BL posterior (He-Litterman form), δ calibration, the Rockafellar-
Uryasev LP, overlapping 10-day CVaR and its √3 display scaling, CDaR, the
cloud/frontier feasible set, date-clustered IC with Newey-West t, the mid-rank
percentile and tier cut-points of the Market read, the News read weights.

### Deliberate conventions (documented, not errors)

- Portfolios are rebalanced daily to their weights.
- Risk-free is 0 for non-USD display currencies (Yahoo has no short-rate series).
- Empirical CVaR is the mean of the ⌈(1−α)T⌉ worst scenarios everywhere; the
  LP's value differs by a fraction of one scenario when (1−α)T is not whole.
- Calmar uses the selected period, not 36 months; CAGR extrapolates short windows.

---

## Earlier audit (pre-package)

> Historical document: written when the whole backend was one `dashboard.py`.
> Its `dashboard.py:<line>` references predate the package split — the
> functions now live in `src/convexity/` (`analytics._stats` / `_relative`,
> `helpers._normalize_dividend_yield`, the KaTeX formulas in `static/app.js`).

Comprehensive validation of every quantitative metric and formula across
`mpt.py`, `dashboard.py`, and `xlsx_export.py` against standard references.

---

## 1. xlsx_export.py — metric extraction

`xlsx_export.py` does **not** recalculate any risk/return metrics. It is a
pure reader/formatter that pulls pre-computed values from the analytics
payload produced by `analyze_portfolios_multi` in `dashboard.py`.

### Metrics read from analytics payload (STATS_KEYS, xlsx_export.py:113-124)

| Key | Label | Source computation |
|---|---|---|
| `total_return` | Total Return (%) | `dashboard.py:2507` |
| `ann_return` | Annualised Return (%) | `dashboard.py:2508` |
| `ann_vol` | Annualised Volatility (%) | `dashboard.py:2509` |
| `sharpe` | Sharpe Ratio | `dashboard.py:2510` |
| `sortino` | Sortino Ratio | `dashboard.py:2511-2512` |
| `calmar` | Calmar Ratio | `dashboard.py:2515` |
| `max_dd` | Max Drawdown (%) | `dashboard.py:2514` |
| `beta_spy` | Beta vs SPY | `dashboard.py:2615` |
| `r2_spy` | R² vs SPY | `dashboard.py:2617` |
| `te_spy` | Tracking Error vs SPY (%) | `dashboard.py:2619` |

### Concentration metrics (xlsx_export.py:139-144)

| Key | Formula | Code location | Verdict |
|---|---|---|---|
| `top5` | `sum(weights_sorted[:5])` | `dashboard.py:2737` | ✓ matches KaTeX `T_5 = sum_{i in top 5} w_i` |
| `herfindahl` | `sum(w² for w in weights)` | `dashboard.py:2738` | ✓ matches KaTeX `H = sum(w_i²)` |
| `effective_n` | `1 / herfindahl` | `dashboard.py:2739` | ✓ matches KaTeX `N_eff = 1/H` |

### `_normalize_dividend_yield` — duplicate in xlsx_export.py

`xlsx_export.py:238-258` contains a **local copy** of `_normalize_dividend_yield`
(uses `_maybe_num` instead of `_safe_num`). Logic is identical to `dashboard.py:1748-1775`.
Risk: if one copy diverges, the export and UI will show different dividend yields.
Both currently produce the same results. ✓ (but consider deduplicating in future.)

### Analyst aggregates (xlsx_export.py:129-137)

| Key | Formula | Code location | Verdict |
|---|---|---|---|
| `mean_rating` | Coverage-weighted mean sell-side rating | `dashboard.py:2707` | ✓ |
| `weighted_target_upside_pct` | `upside_num / upside_w` | `dashboard.py:2709` | ✓ |
| `n_analysts_total` | `sum(n_analysts per holding)` | `dashboard.py:2711` | ✓ |

`upside_pct` per holding: `(target_mean / price - 1) * 100` (`dashboard.py:2660`).
KaTeX: `U_port = sum(w_i * (TP_i/P_i - 1)) / sum(w_i)`. ✓

---

## 2. KaTeX UI tooltip ↔ Python implementation cross-check

All KaTeX formulas are in `dashboard.py:8580-8696`. Each is compared against the
corresponding Python line(s) in `dashboard.py` or `mpt.py`.

| Metric | KaTeX formula | Python code | Verdict |
|---|---|---|---|
| **Ann Vol** | `σ_ann = σ_daily · √252` | `ret.std() * sqrt(252) * 100` (line 2509) | ✓ |
| **Sharpe** | `S = (R̄ - R_f) / σ` (R_f=0) | `(mean*252) / (std*√252)` (line 2510) | ✓ |
| **Sortino** (post-fix) | `S_o = (R̄ - R_f) / σ_down` | `(mean*252) / (sqrt(mean(clip(r,0)²))*√252)` (line 2512) | ✓ |
| **Max Drawdown** | `DD_max = min_t(V_t / max_{s≤t}(V_s) - 1)` | `(val / val.cummax() - 1).min() * 100` (line 2514) | ✓ |
| **Calmar** | `C = R_ann / \|DD_max\|` | `ann_return / abs(max_dd)` (line 2515) | ✓ |
| **Beta** | `β = Cov(r_p, r_m) / Var(r_m)` | `rp.cov(rb) / rb.var()` (line 2615) | ✓ |
| **R²** | `R² = Corr(r_p, r_m)²` | `corr * corr` (line 2617) | ✓ |
| **Tracking Error** | `TE = √252 · σ(r_p - r_m)` | `(rp-rb).std() * √252 * 100` (line 2619) | ✓ |
| **P/E (wtd)** | `PE_port = sum(w_i·PE_i) / sum(w_i : PE_i defined)` | `_w_avg(pe_vals)` (line 2632) | ✓ |
| **P/S (wtd)** | `PS_port = sum(w_i·PS_i) / ...` | `_w_avg(ps_vals)` (line 2633) | ✓ |
| **EV/EBITDA (wtd)** | weighted avg of constituent multiples | `_w_avg(ev_vals)` (line 2634) | ✓ |
| **Div yield (wtd)** | `y_port = sum(w_i·y_i) / sum(w_i : y_i defined)` | `_w_avg(div_vals)` (line 2635) | ✓ |
| **Top-5 weight** | `T_5 = sum_{i in top 5} w_i` | `sum(weights_sorted[:5])` (line 2737) | ✓ |
| **HHI** | `H = sum(w_i²)` | `sum(w*w for w in weights.values())` (line 2738) | ✓ |
| **Effective N** | `N_eff = 1/H` | `1.0 / herfindahl` (line 2739) | ✓ |
| **Weighted target upside** | `U_port = sum(w_i·(TP_i/P_i-1)) / sum(w_i)` | `upside_num / upside_w` (line 2709) | ✓ |
| **MPT Sharpe** | `(ret - rf) / vol` on annualised values | `mpt.py:430` | ✓ |
| **CLA frontier** | piecewise-linear exact frontier | Bailey & López de Prado 2013 | ✓ |
| **Ledoit-Wolf** | constant-correlation target + analytical α | `mpt.py:71-100` | ✓ |

---

## 3. Confirmed bug and fix

### Sortino Ratio — `dashboard.py:2511-2512`

**Standard definition** (Sortino & Price 1994):
```
σ_down = sqrt( mean( min(r_i, 0)² ) )   — average over ALL N periods
```
Days where `r ≥ 0` contribute `0` (not dropped); the MAR (minimum acceptable
return) is 0.

**Bug (pre-fix):**
```python
downside = ret[ret < 0].std()
```
Divides by `N_neg − 1` (only negative days), not `N` (all days). Subtracts
`mean_neg` instead of `0` as the threshold. Result: Sortino **overstated by
~30–60%** for typical equity return distributions.

**Fix applied:**
```python
downside = math.sqrt((ret.clip(upper=0) ** 2).mean())
```

---

## 4. Annualisation convention summary

| Context | Factor | Reason |
|---|---|---|
| Vol (analytics daily returns) | `√252` | trading days/year |
| Sharpe numerator | `× 252` | annualise daily mean |
| Sortino numerator | `× 252` | same |
| Tracking Error | `× √252` | annualise daily diff std |
| CAGR | `/ (calendar_days / 365.25)` | compounding over real time |
| MPT (weekly) | `× 52` | periods/year for mean & cov |
| MPT (daily) | `× 252` | periods/year for mean & cov |
| MPT (monthly) | `× 12` | periods/year for mean & cov |

---

## 5. All metrics confirmed correct (post-fix)

After the Sortino fix, **all 19 quantitative metrics** across all three files
match their standard reference definitions. The test suite in
`tests/test_metrics.py` provides 26 closed-form regression tests.
