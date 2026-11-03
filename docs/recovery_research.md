# How long does a fall take to recover?

Research behind the Time to recover card in My Investments (2026-11-02/03). The
owner proposed peaks-over-threshold (POT) with a generalized Pareto tail, with
benchmarks from Brownian motion. This note records the test and what shipped, and the
later change to the histogram and the volatility scale (below). Tables after "What
shipped" are from the first study, on 1,520 falls and before those changes.

`scripts/study_recovery.py` recomputes every shipped constant from public tickers,
and was checked to reproduce them exactly. The panel is 40 large caps (AAPL, MSFT,
NVDA, SPY, JPM, ...), Yahoo max history from 1962, total return: 1,528 falls of 10%
or more from a high, 31 still running. The study also ran on one private book;
those figures are not recorded here beyond the summary below.

## What shipped

| Piece | Model | Why |
|---|---|---|
| The histogram | Every stretch below the running all-time high, 30 bars from 2 days to 30 years, with a lognormal truncated below 2 days, leaning on the panel by 100 dips | The owner's definition. A holding has dozens to hundreds, where it has about a dozen falls of 10% or more. |
| Falls of 10% or more | Inverse Gaussian, shape pooled (φ .30, μ 269 days at 30% volatility), scale from the 3-year volatility before each fall (slope −1.02), adjusted by its own falls over 25 | Beats the old estimate after every date tested. One holding's falls can't carry two parameters. |
| Falls of each depth | Pooled Kaplan-Meier table, median days 59 / 150 / 384 / 1223 for 10-20 / 20-30 / 30-50 / 50%+ | A table held up out of time. Curves conditioned on depth did not. |
| A fall still running | ln(days left) = 5.165 + 1.300 ln a + 0.309 ln t − 0.597 ln w, σ 1.205 | Knows how far the price still has to go. Nothing else came close. |
| Data | `long_run.repair_splits`, then `long_run.usable_start` | The existing glitch filter removed nothing on max history. |

No GPD anywhere.

## Every dip, not only the big ones

Falls of 10% or more come about 0.7 times a year, so 30 bars over a holding's dozen falls
stayed mostly empty, and more history can't fix that: the panel's median of 48 years and 39
falls still fills about half.
The owner's definition is the plain one: keep a running all-time high, count the days each
stretch below it took to get back, and plot that. On the panel that is a median of 298 dips
per stock, half back within 5 days, 9 in 10 within 55. The 10%+ falls are the solid part
of each bar.

Six two-parameter curves (lognormal, log-logistic, inverse Gaussian, inverse gamma, Lomax,
Weibull) score the same out of sample, within .01 per dip. A lognormal truncated below two
days holds the tail (share past 1 year: 2.8% seen, 2.8% curve; past 3 years: .9%, 1.0%). Its
parameters (μ −5.08, σ 3.66) mean little alone: it is a bending power law, close to the first
return of a random walk. One that begins at 2 days instead halves that tail. A holding's
own later dips are told better by leaning on the panel, so its curve is blended with the
panel's by n / (n + 100). The blend is of the curves: their parameters trade off along a ridge.

## Volatility sets a fall's scale

A holding's falls are about a dozen numbers, its volatility thousands. Out of sample the
volatility over the three years before a fall tells more about that fall than the holding's
earlier falls do. A calm share takes longer, since 10% is further for it.

| fit on falls finished by, scored later | slope | log score | without volatility | gain |
|---|---|---|---|---|
| 1995, 896 falls | −1.26 | −1.562 | −1.567 | +.006 |
| 2005, 521 falls | −1.13 | −1.426 | −1.507 | +.080 |
| 2015, 349 falls | −1.01 | −1.397 | −1.451 | +.054 |

Separately by period the slope is −1.13 (to 1999), −.70 (2000-12) and −.69 (2013 on): always
well below zero, but flatter lately, so the size of the effect is less sure than its sign. It
needs one number per fall: a single volatility over a stock's whole history against all its
falls dilutes the slope and, after 2005, adds nothing. Clamped to the panel's 1st to 99th
percentile (14% to 73%). A block bootstrap of each holding's own returns scored clearly
worse than the shrunk curve (−.08 and −.04), whichever drift it used. Falls from any recent
high, not only the all-time one, give four times as many but make the estimate no better,
nor do 5% falls or drift. Volatility adds nothing the running-fall formula has not
got already (+.005 and +.010 on the panel, 0 on the private book). The curves' comparison, the
bootstrap and these alternatives were one-off runs, not in the script.

## The owner's benchmarks

Verified by simulation at daily closes with a censored likelihood.

- **Zero log drift, ξ = 2: correct** (fitted 2.01). The drift is the log drift,
  m − σ²/2, so a share with +4.5% arithmetic drift at 30% vol is this case.
- **Positive drift, ξ = 0: only asymptotically.** The exponential tail sets in past
  σ²/ν² (7.6 years at the panel's median, 30 years at 10% drift and 30% vol). Fitted
  at the panel's median drift and vol: .87 at 3 months, .56 at 1 year, .35 at 3 years,
  .15 at 10. So a bare ξ can't say "GBM-like": it has to be read against a simulated
  twin with the same drift, vol and length.

## ξ against each ticker's GBM twin

| | threshold | ξ | profile 90% | two-way bootstrap | GBM twin |
|---|---|---|---|---|---|
| log depth | 10% | .63 | .56, .71 | .50, .75 | .45 [.39, .51] |
| log depth | 30% | .25 | .13, .40 | .04, .51 | .29 [.17, .41] |
| duration | 6 months | .72 | .60, .86 | .41, .97 | .72 [.62, .83] |
| duration | 1 year | .54 | .39, .72 | .23, .84 | .57 [.43, .71] |
| duration | 3 years | .15 | −.04, .39 | −.35, .48 | .39 [.18, .64] |

Duration matches GBM from 6 months to 2 years and is thinner past 3 (survivors or
real reversion; this data can't separate them). ξ falls steadily with the threshold,
from .78 at 3 months to .02 at 6 years, so there is no one number. A GPD is defensible
only from about 3 years (118 falls), and there it is close to exponential, scale about
1,000 days: past 3 years, about 20% of what is left recovers per year.

## Depth

T scales with depth to the power 1.80 [1.65, 1.94], but a GBM twin gives 2.04: this is
mostly Brownian scaling, not an empirical discovery. Given depth, the tail index is
about 0 (−.03 to .06). **The heavy tail is a mixture over depth**, so once depth is
known a GPD has no job. Simple depth is bounded at 100%, so its ξ is negative by
construction; use log depth.

## Out of sample

Each fall scored from other tickers' constants and the holding's own earlier falls.
Depth unknown, because a fall's final depth is unknown while it matters.

| Model | Δ log score vs chosen (se by ticker) | within median / 90% (.50 / .90) | P > 5y (seen .046) |
|---|---|---|---|
| **Inverse Gaussian refit** | 0 | .514 / .907 | .037 |
| Inverse Gaussian + depth slope | +.011 (.005) | .467 / .895 | .035 |
| Lognormal + GPD, u = 1y | +.008 (.006) | .487 / .895 | .040 |
| Lognormal + depth slope | +.013 (.005) | .472 / .895 | .036 |
| Pooled Kaplan-Meier | −.031 (.011) | .490 / .904 | .051 |

The GPD's gain doesn't clear its noise, and the card claims only the median and 90th
percentile, where it is less calibrated. With u = 3 years it fails out of time (P > 3y
.133 against .085 seen). The depth-slope models look much better (+.57) only when the
final depth is known, and their calibration falls to .28 / .69 when fitted before 2005.
On one real book the GPD's gain was larger (+.034) and every model understated the tail:
"9 of 10" reads about 87% there.

## A running fall

Every fall at 12 checkpoints from 3 months to 10 years, 2,812 rows, on held-out
tickers. MAE is the gap to Kaplan-Meier's share still not back.

| Model | Conditional log score | MAE |
|---|---|---|
| Ratio of the inverse Gaussian, S(t+h)/S(t) | −.961 | .100 |
| Ratio of lognormal + GPD | −.936 | .086 |
| Lognormal + depth, depth so far plugged in | −1.332 | .125 |
| **The formula above** | **−.684** | **.034** |

Share still not back, worst fall 50% or more, 2.4 years in (86 falls):

| | 1 year | 3 years | 5 years |
|---|---|---|---|
| Seen | .84 | .50 | .30 |
| Formula | .74 | .48 | .34 |
| Ratio of the curve | .63 | .30 | .17 |

The ratio tells a holding 50% down right now that 6 in 10 are still out after a year,
when 96% were. The formula errs the other way past 3 years on a thin sample (27 falls),
and it moves with the price (median 22% a month), so the card rounds it.

## Data

The panel's longest falls are real (GE 2000-2024, CSCO 2000-2021). What isn't: MCD's two
unadjusted 2:1 splits of 1968-69, and on secondary and thin lines, a big merger's share
exchange, ×40 jumps, multi-year holes, and whipsaws that read as a fall running for decades.

A fall to an exact split ratio among calm days, more than five years ago (within 3 points of
½, ⅓, ¼, ⅕, neighbours under 5%), is repaired by rescaling what came before. On the panel that
finds MCD's two splits and nothing else: AAPL's −52% in 2000 had a loud day beside it.

The gate then cuts history after the last of: a hole over 30 days, a year with over half its
returns zero, or three moves over 30% undone within 10 rows in a year. More than five years
ago it also cuts a day over +100% or under −75%. A bad sign in a holding's own young line
doesn't cost the stand-in's clean years before it: they are read as a stretch of their own.

The repair has a cost: a real −50% day among calm days, years ago, reads as a split. Yahoo's
split list can't tell them apart, since a split it knows is already in its closes.

The five-year limit exists because a recent crash is what the card is for, and cutting it
would call that holding unfallen. Should a record and the fresher ten-year figures disagree
about whether a fall is running, the ten years win (`investments._long_record`).

False cuts on the panel: 3 of 40, all before 1984. Constants barely move with the window
(from 1990: φ .314, μ 267), except from 2000 (φ .288, μ 343, one lost decade), so all of it
is used.

## What it can't say

Yahoo keeps no delisted names: a share that never came back is absent, every recovery
figure is flattered, and the size of that can't be measured. The card says so. C at −98%
since 2006 is the panel's closest case.
