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
