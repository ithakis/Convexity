# Real-world price distribution (3M / 12M) — plan

Status: Phase 1 built on `feature/real-world-density` (17 Oct 2026): the backend
(`options_density.py`, `GET /api/density`), the stock window's σ/μ, Settings → Price
distribution, tests. Phase 2 below is the follow-up. Prototype script:
`scripts/density_prototype.py`. The full research note (interactive mockup, 109
sources) is kept privately by the owner.

## Goal

For each stock, show the probability distribution of its price in about 3 and
12 months, built from that stock's listed options. It must be:

1. **Readable in five seconds** by a non-quant: a price axis, a curve, ranges and
   "chance of" numbers.
2. **Defensible to a derivatives quant**: every identifying assumption on screen,
   no method the literature has rejected, a quality gate that hides bad chains.

## The decision and why

Option prices give the risk-neutral density `q` only. The real-world density is
`p = q / (M · R_f)`, and options alone cannot identify the pricing kernel `M`.
Every method is an identifying assumption. Equivalently: P is the measure whose
numeraire is the growth-optimal portfolio, and choosing a method means choosing that numeraire.

| Method | Numeraire behind P | Verdict |
|---|---|---|
| 1 · Preference tilt `p ∝ q·S^γ` | market portfolio (γ = 1 is the share measure) | robust benchmark (Bliss & Panigirtzoglou 2004; not rejected by Jackwerth & Menner 2020), but leaves the variance risk premium in, so the curve stays too wide |
| 2 · Empirical dP/dQ (entropy tilt, calibration map) | whatever the data imply | best when inputs/history exist |
| 3 · Ross recovery | long zero-coupon bond (the long-forward measure) | **do not use**: misspecified (Borovička, Hansen & Scheinkman 2016), fails empirically (Jackwerth & Menner 2020) |

**Chosen: Approach B, a minimum-entropy two-moment tilt, with the variance target
anchored to implied volatility.**

```
p(x) ∝ q(x) · exp(θ1·x + θ2·x²),   x = ln(S_T / F)
E_P[S_T]   = S0 · exp((r + β·ERP)·T)          # CAPM-consistent mean
Var_P[x]   = (κ · σ_Q)² · T                   # κ = physical / implied vol
```

- θ2 = 0 is Approach A (power tilt). Use it as the fallback when the two-moment solve
  fails or θ2 leaves its bounds.
- κ: placeholders 0.80 for indices and 0.92 for single stocks. These come from my rough
  reading of the variance-risk-premium literature and have not been checked against the
  sources. Estimate them properly from the snapshot collector (Phase 2). Anchoring to
  implied vol rather than a historical-vol model is deliberate:
  the prototype's historical forecast made NVDA's real-world curve *wider* than the
  options at 12M (10y vol incl. 2022), and trailing vol in a calm market made SPY's
  crash risk vanish. κ × σ_Q can never exceed the options' width and reacts at
  once when the market reprices risk.
- Approach C (Beta calibration on past PIT values) is the long-term validation, not
  the method: Yahoo has no option history, so it needs years of our own snapshots.

Expected effect (prototype, 17 Oct 2026):

| | Q: P(≤ −20%) | P (B): P(≤ −20%) | Q: P(up) | P (B): P(up) |
|---|---|---|---|---|
| AAPL 3M | 5.5% | 3.6% | 56% | 60% |
| AAPL 12M | 18.3% | 12.6% | 55% | 63% |
| SPY 3M | 2.3% | 1.0% | 64% | 70% |

## Numerical recipe (as in `scripts/density_prototype.py`)

1. **Expiry**: the listed expiry nearest 91 and 365 days. Show the real date, never
   interpolate a maturity.
2. **Rate**: from `^IRX`. **Never** back it out of put–call parity. Yahoo's US chains
   are American-style, and parity gave −4.8% at 3M.
3. **Forward**: median of `K + (C − P)/DF` over strikes within ±12% of spot.
4. **Quotes**: two-sided only, OTM only (puts below F, calls above), bid-ask < 60% of
   mid, 0.3 < K/F < 2.5.
5. **Smile**: raw SVI on implied total variance, vega-like weights on the body,
   wing slope capped by Lee's bound `b(1+|ρ|) ≤ 2`. Uncapped, SPY 12M's mean came out
   11% above the forward.
6. **Density**: Breeden–Litzenberger on a 4001-point log-strike grid (±10 ATM sd;
   ±5 before the tails), clip negatives, **GEV tails** past the 5% / 95% points or
   the last quoted strike, renormalise. See "Explicit tails" below.
7. **Tilt**: solve the two equations above, fall back to A.
8. **Quality gate**: quotes ≥ 30, SVI RMSE < ~1 vol pt, |E_Q[S_T]/F − 1| < 1%.
   Fail → show "Thin data" or hide the horizon. It runs after the tails. SPY 12M
   failed without them (+2.1%: strikes stop at 540–995).

## Data realities (tested)

- US listings: 38–148 usable OTM quotes per expiry for AAPL, NVDA, SPY, ASML.
- Non-US listings have **no chains** (TTE.PA: zero expiries). Use the company's US
  line or ADR (`symbol_db.us_line`) and map the price axis by ADR ratio and FX, or
  show "No listed options".
- No historical chains on Yahoo. Anything needing history is built forward.

## Where it sits in Convexity

- `src/convexity/options_density.py`: chain fetch, SVI, Breeden–Litzenberger, tilt,
  quality gate. numpy/scipy are already dependencies. **Never** in `fetch_one`'s
  hot path.
- `GET /api/density?symbol=…&h=91|365` returns curve points on a price axis plus
  quantiles, probabilities, inputs and quality. TTL cache ~15 min, successes only
  (§9 rule).
- β from existing analytics; ERP default 5% as an owner setting in `prefs.json`.
- Methodology (the three methods, two paragraphs) in Settings → About. Wording on the
  chart: "Priced by options" / "Real-world estimate", "chance", never "prediction".
- Phase 2: the reference-pack workflow appends daily 3M/12M density snapshots for the
  S&P 500 (tickers, dates, numbers only: the pack's allow-list rules). A Track-record
  style page shows PIT histograms; κ and later a Beta map are fitted from them.

## Look and feel (agreed with the owner through mockups v1–v7, 17 Oct 2026)

- **Where**: inside the stock window's existing chart (every chart type, volume kept). The
  window is wider: `min(1440px, 96vw)`, with a lighter backdrop.
- **Toggles**: **σ** and **μ** after Volume, both on whenever a window opens.
  - σ+μ: σ from the options, μ from the analysts.
  - σ: the market's μ (CAPM).
  - μ: the analysts alone, as a line per firm to its target plus where the targets sit.
  - Neither: the old chart.
  - The Q / risk-neutral view is a setting, not a chart mode. Only one curve is shown
    at a time.
- **Layout**: history to Today (~58%), a fixed-width future zone with its own linear time
  to the real expiry, then the density turned sideways, then the right-axis price tags.
  The y axis is log and spans the history plus the end curve's 3%..97%. Horizon chips:
  `12M · <expiry>` (default) and `3M · <expiry>`, plus Replay, top right.
- **Paths**: ~140 drawn of 1,000, mono accent, with a 50/90% cone and a dashed median.
  Each path is a bridge to the drawn curve with jumps on earnings dates (an "E" marker).
  The animation runs 750 ms of paths, then 300 ms of the density growing. There is no
  histogram stage.
- **Density**: green above today's price, red below; dashed outline under risk-neutral.
- **Analysts** (12M, μ on): an amber Low–High bar on the density's spine with Consensus
  (the median of firms) as a diamond, and chip labels to its left. Hidden on 3M.
- **Hover**: a date × price crosshair. The box shows the chance above or below on that
  date, the chance of touching first, and the percentile. On the density it snaps to the
  analyst marks. The box always sits left of the density.
- **Readouts** (a rounded box under the chart): chance up; expected price; median and
  50% range; 90% range; chance ≤ −20% and of touching it; chance ≥ consensus; earnings
  date and implied move; σ (κ × implied). The footnote gives the assumptions and an
  "Assumptions ⚙" link to Settings → Price distribution: ERP, analyst haircut, measure,
  and κ under Advanced.
- **Empty states**: "No listed options"; "No options expiry near 12 months" (small caps
  list nothing past 5–7 months); "Thin options data (n quotes, why)". When 12M fails and
  3M passes, the window opens on 3M and says why.

## Coverage beyond dense US chains (built 17 Oct 2026)

A test over 27 European and US listings (large caps, UCITS ETFs, a small cap)
found 4 that drew. 13 had a US chain too sparse for the gate, and 10 had no chain
on Yahoo. Three changes, each labelled on the page:

1. **Sparse chains: an SSVI surface.** Long expiries of many large caps list 7–29
   usable strikes, and a 5-parameter SVI overfits them: a low smile-fit error, yet
   PEP 12M missed its forward by 2.8%. The surface (Gatheral–Jacquier 2014,
   power-law φ, arbitrage-free by construction) fits all expiries at once with 3
   shared parameters. It has its own gate: ≥ 60 quotes over ≥ 3 expiries, a fit
   < 1.5 vol pts, ≥ 8 quotes at the expiry, and quotes reaching the 15% / 85%
   points. Up to 4 expiries in the window are tried, nearest first. PG, PEP, JNJ,
   MCD, T, VZ and XOM now pass at 12M.
2. **FX.** A US line shown in euros carries EUR/USD risk:
   ln S_EUR = (1 + b)·x + e. ρ and b come from 2 years of weekly returns, the FX
   vol from FXE options (5.8% today, correlations near 0 to ±0.25), and no
   expected FX move is assumed. The 12M σ rises by 0–1 vol pt.
3. **History estimate where options fail.** x = β·(market) + own: β and the
   residual come from 3 years of weekly returns against an index ETF (SPY in the
   US, FEZ for Europe, EEM for EM funds). The residual variance reverts from the
   last quarter to the 3-year level. The market part uses the ETF's options when
   they pass the gate (κ_index × implied, in local currency). Today only SPY's do:
   FEZ, EZU, VGK, IEUR, EWG and EWU are too thin or stop at 5–7 months, so
   Europe's market part is history too. The mean is CAPM at the local rate, read
   from two quarterly CME futures (EUR 2.5%, GBP 4.0%, CHF −0.4%). It is a
   real-world curve only, with no risk-neutral one.

After the change, all 27 drew at both horizons. At 12M, 8 came from options (3 of
them on the surface) and 19 from history; at 3M, 11 from options (7 on the
surface) and 16 from history. Each says which, and why.

Remaining limits:
- No European options source on Yahoo. Eurex end-of-day settlements would fix
  it, but they need a new outbound host and a terms-of-use check.
- κ is still a placeholder.
- History misses forward events and underrates illiquid small caps, whose stale
  prices look calm.

## Owner feedback, 17 Oct 2026 (built, v1.27.1)

- Replay removed; today's price is a gauge (dashed level across the plot, tag
  on the left axis); the right tag gutter is gone, so the density reaches the
  edge. Pill `3M | 12M`, the date under the density as **12M** · 17 Oct 27.
- Readouts: Pr[S_T > S_0]; the mean named by its source ("expected" claimed
  too much); "Falls 20% or more: x% by the end · y% at some point" (the old
  "Chance ≤ −20% · touch it" read as noise); Dividend Yield (trailing 12
  months) in place of the earnings cell; Hist. / Imp. Volatility, both
  annualised vols of log returns on the same listing and currency.
- European chains: Yahoo carries none. Every US line is now probed and the
  one with the most open interest used; a horizon past the longest expiry is
  read off the SSVI surface when the chain reaches half of it. a European bank's ADR
  chain exists but was 50–150% wide on 17 Oct (a Saturday), so it stays on
  history. Euronext's own delayed option prices would need a new outbound
  host: not added without the owner's OK.
- E/D flags on the history with a card per event (EPS vs estimate, the
  price's reaction and the S&P 500's; a dividend against a year earlier and
  its ex-date move).

## Phase 2 — follow-up (not built)

- **Snapshot collector**: the reference-pack workflow appends daily 3M/12M density
  snapshots for the S&P 500 (tickers, dates and numbers only: the pack's allow-list rules).
- **PIT validation**: a Track-record-style page with PIT histograms of realised prices
  against the stored curves.
- **κ estimated** from those snapshots, replacing the 0.92 / 0.80 placeholders; later a
  Beta calibration map (Approach C).
- **Paths**: pin them to both expiries on the 12M view, with a Gaussian copula
  ρ = √(T₁/T₂), so the cone at 3M also matches the 3M chain.

## Explicit tails (built 17 Oct 2026)

Long chains often failed the martingale gate. Two causes, both fixed in
`fit_density` without loosening the gate:

- **The SVI's wing past the last strike.** It is extrapolation, and SPY 12M's bent
  up past K = 995 into a false hump with negative density beyond it. GEV tails
  (Figlewski 2009) now replace the density past the 5% / 95% points, or past the last
  quoted strike when the quotes stop short of them. Each tail matches the body's cdf
  and density at the attachment point and its decay 3 cdf points further in. The
  attachment cdf comes from the call slope 1 + e^{rT}·∂C/∂K, never from integrating
  the contaminated density. The right ξ is capped at 0.45, so E[S] and Var[S] stay
  finite. The grid widened from ±5 to ±10 ATM sd: a stressed chain's left tail runs
  well past 5 sd of its ATM vol.
- **A stale quote.** DELL's K=165 put bid 12.15 while the K=170 put asked 4.40. Quotes
  that break static no-arbitrage by more than the spread are dropped.

Before and after, live with a temp `CONVEXITY_HOME`, 17 Oct 2026, 12M:

| | expiry | quotes | smile fit | E_Q/F − 1 before | after | gate |
|---|---|---|---|---|---|---|
| SPY | 30 Sep 2027 | 142 | 0.10 vol pt | +2.10% | +0.20% | thin → ok |
| DELL | 17 Sep 2027 | 70 → 69 | 1.50 → 0.26 | −9.07% | +0.09% | thin → ok |
| QQQ | 30 Sep 2027 | 87 | 0.14 | +3.09% | −0.09% | thin → ok |
| ARKK | 21 Jan 2028 | 37 → 35 | 3.38 → 0.92 | +0.21% | +0.16% | thin → ok |
| AAPL | 17 Sep 2027 | 60 | 0.32 | +0.24% | −0.06% | ok → ok |
| NVDA | 17 Sep 2027 | 62 | 0.33 | +0.47% | +0.04% | ok → ok |

DELL and ARKK passed thanks to the stale-quote filter; SPY and QQQ thanks to the
tails. 3M still passes (SPY +0.10%, DELL 0.00%, AAPL −0.03%). σ_Q rises where the
old grid cut the left tail: SPY 19.8% → 21.7% and NVDA 41.1% → 43.1%. That is
the left-skewed mass beyond 5 ATM sd now being counted.
In the tests, a lognormal-mixture chain (a 10% crash regime) quoted only within ±15%
reads +1.7% with the SVI's own wings and +0.4% with the tails.

## Acceptance for the prototype

- `options_density.py` reproduces the script's numbers for AAPL/SPY within rounding.
- Endpoint answers for any US ticker with options; a clear empty state for no
  chain or a failed quality gate; non-US names via their US line.
- UI matches the owner's spec above, in light and dark ("Ink"), within the
  two-box rule and classic green/red/blue data colours.
- Tests: unit tests on a synthetic Black–Scholes chain (B with κ = 1 and zero premium
  must return q; A and B must hit their target mean and variance; the gate must
  trip on a truncated chain). `ruff check` and `ruff format` clean.
- Verified live against a temp `CONVEXITY_HOME` (never the owner's data).

## Key sources

Bliss & Panigirtzoglou (2004) *Option-implied risk aversion estimates* ·
Liu, Shackleton, Taylor & Xu (2007) *Closed-form transformations from risk-neutral
to real-world distributions* · Shackleton, Taylor & Yu (2010) *A multi-horizon
comparison of density forecasts for the S&P 500* · Christoffersen, Heston & Jacobs
(2013) *Capturing option anomalies with a variance-dependent pricing kernel* ·
Metaxoglou & Smith (2016) *Option-implied equity premium predictions via entropic
tilting* · Martin (2017) *What is the expected return on the market?* · Martin &
Wagner (2019) *What is the expected return on a stock?* · Ross (2015) *The recovery
theorem* · Borovička, Hansen & Scheinkman (2016) *Misspecified recovery* ·
Jackwerth & Menner (2020) *Does the Ross recovery theorem work empirically?* ·
Cuesdeanu & Jackwerth (2018) *The pricing kernel puzzle: survey and outlook* ·
Gatheral & Jacquier (2014) *Arbitrage-free SVI volatility surfaces* · Figlewski
(2009) *Estimating the implied risk neutral density* · Vincent-Humphreys & Noss
(2012) *Estimating probability distributions of future asset prices*.

## Next-session prompt (Phase 1, done)

> Build a working prototype of the real-world price distribution feature in
> Convexity. Read `CLAUDE.md` first, then `docs/plans/real-world-density.md` (the
> spec: method, numerical recipe, data rules, placement, acceptance) and
> `scripts/density_prototype.py` (the reference implementation, already validated
> on live AAPL/NVDA/SPY chains). The method is decided, so don't re-research it:
> Approach B (minimum-entropy two-moment tilt, variance target κ × implied vol,
> fallback to the power tilt), with the quality gate and the `^IRX` / OTM-only /
> Lee-bound rules exactly as written. Follow the owner's "Look and feel" section
> for the UI. If anything there is unclear, show a mockup and ask before building
> it (the owner signs off each visual step). Work on a feature branch off
> `distribution`. Put the backend in `src/convexity/options_density.py` with
> `GET /api/density`, keep it out of `fetch_one`, and add the tests listed under
> Acceptance. Verify live with a temp `CONVEXITY_HOME` and never against the owner's
> data or the app on 8765. Before any version bump, reason about it and ask (§15).
> Leave Phase 2 (snapshot collector, PIT validation, κ estimation) as a follow-up
> in the plan file.
