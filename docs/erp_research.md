# The equity premium: historical vs assumed (2026-10-26)

Why `capm.py` offers two equity premia and why **Assumed (5%) is the default**.
The shipped table is `src/convexity/data/erp_history.json`, built by
`scripts/build_erp_history.py` from the Jordà-Schularick-Taylor Macrohistory
Database R6 (CC BY-NC-SA 4.0, not in the repo; the derived file carries the
citation and licence).

## What was reviewed

194 papers on the equity premium were screened by title and abstract (list
below), from 40 targeted searches (OpenAlex, Crossref, Semantic Scholar, CORE,
arXiv). Paywalls and CORE's download block meant full text was rarely
available, so the per-market numbers are **computed** from the JST annual
data rather than transcribed from papers; the papers supply method and
cross-checks. The ones that shaped the decision:

- Dimson, Marsh & Staunton (2006), *The Worldwide Equity Premium: A Smaller
  Puzzle*: world premium 1900-2005 of 4.7% over bills; investors should expect
  about 3-3.5% geometric, 4.5-5% arithmetic.
- Jordà et al. (2019), *The Rate of Return on Everything, 1870-2015*: the data
  used here.
- Goyal & Welch (2008) and Goyal, Welch & Zafirov (2024): predictors rarely
  beat the historical average out of sample; Campbell & Thompson (2008): small
  gains only once sign and coefficient restrictions are imposed.
- Lee, Tu & Ullah (2015), *Forecasting Equity Premium: Global Historical
  Average Versus Local Historical Average and Constraints*: the same
  global-vs-local question tested below on JST (abstract only).
- Welch (2008) consensus of ~400 professors: about 5% (IQR 4-6%); Fernandez's
  surveys (2008-2021): 5-6% in the US, wide dispersion by country; Fernandez
  (2020), 150 textbooks: 3-10%.
- Country studies: Brailsford & Handley (2007, Australia, 6.8% vs bills
  1958-2005); Vivian (2007, UK); Nyberg & Vaihekoski (2013, Finland);
  Battilossi et al. (2025, Spain 1900-2020); Mehra (2006, India, 9.7% post
  1991); Zhu (2019, Greater China, 8-10% forward-looking); de Beer (2008, South
  Africa); Salomons & Grootveld (2003) and Donadelli & Prosperi (2011, emerging
  markets).

## The premium the app needs

The model price S0·e^(μT) is a **mean**, so for the market (β = 1)
e^(rf + ERP) = E[1 + R_eq] and ERP = ln E[(1 + R_eq)/(1 + R_bill)]: an
arithmetic-mean premium. Tables quoting the geometric premium read about 1.5
points lower (US 4.6% geometric vs 6.1% here).

## Per market (JST R6, annual, local currency, full sample)

| Market | ERP | Geometric | ± 1 s.e. | Years |
|---|---|---|---|---|
| Australia | 6.0% | 5.0% | 1.1% | 1870–2020 (148) |
| Belgium | 5.4% | 3.3% | 1.7% | 1870–2020 (147) |
| Denmark | 4.9% | 3.6% | 1.3% | 1875–2020 (146) |
| Finland | 9.5% | 6.5% | 2.3% | 1896–2020 (125) |
| France | 4.3% | 2.5% | 1.7% | 1870–2020 (144) |
| Germany | 7.8% | 5.3% | 2.0% | 1870–2020 (144) |
| Italy | 6.7% | 3.4% | 2.4% | 1870–2020 (131) |
| Japan | 8.6% | 6.1% | 2.0% | 1886–2020 (133) |
| Netherlands | 6.4% | 4.4% | 1.9% | 1900–2020 (121) |
| Norway | 4.9% | 3.2% | 1.5% | 1881–2020 (140) |
| Portugal | 3.8% | 1.0% | 2.0% | 1880–2020 (141) |
| Spain | 6.1% | 4.4% | 1.7% | 1900–2020 (117) |
| Sweden | 6.3% | 4.7% | 1.4% | 1871–2020 (150) |
| Switzerland | 5.7% | 4.2% | 1.6% | 1900–2020 (121) |
| UK | 5.5% | 4.2% | 1.4% | 1871–2020 (150) |
| USA | 6.1% | 4.6% | 1.3% | 1872–2020 (149) |
| 16 developed markets (pooled) | 6.5% | 5.6% | 1.2% | 1900–2020 (121) |
| 13 European markets (pooled) | 6.3% | 5.3% | 1.3% | 1900–2020 (121) |

Canada and Ireland have no equity series in JST; they, Hong Kong, Korea,
India, Brazil and the other non-JST home markets take the 16-market pool.
Survivorship: markets that were wiped out (Russia 1917, China 1949) are not in
JST, so all of these read high.

## Is the historical premium robust enough? No.

Out of sample, 1950-2020, 16 markets, each origin using only data up to then:

| Forecast of the next H years' premium | 10 years | 20 years |
|---|---|---|
| The market's own history | MSE **+14%** vs flat 5% | **+33%** |
| A pooled world history | −2% (not significant) | −5% (not significant) |
| Country-block bootstrap: own history beats 5% | p = 0.03 | p = 0.04 |
| Rank correlation, past vs future premium across markets | −0.12 | −0.16 |

And the differences between markets are noise: Cochran's Q = 8.6 on 15 df,
p = 0.90 (1950-2020 alone: 6.4, p = 0.97), so the implied true dispersion is
zero. Even 120-150 years leave a standard error of 1.2-2.4 points per market.
At a 1-year horizon (the grid's Model 12M) every choice is within 1.5% MSE of
the others: the year-ahead realised premium is dominated by noise.

### Stress tests of that result

| Check | 10 years | 20 years |
|---|---|---|
| Own history vs a flat 4% / 6% (MSE) | +2% / +21% | +8% / +50% |
| P(own history beats 5%), bootstrap over origin decades (keeps the common shocks across markets together) | 0.00 | 0.00 |
| Same, decades × markets | 0.05 | 0.05 |
| Non-overlapping origins only | +10% | +20% |
| History from 1900 only (no 19th century) | +18% | +43% |

Own history loses to any flat premium from 4% to 6%, under every resampling.
One caveat cuts the other way: ex post, a flat 6% scored better than 5% over
1950-2020 (the pooled history is 6.5%), so 5% is mildly conservative for that
period.

### Financial repression inflates the historical premium

34% of country-years had a negative real bill rate. The premium over bills was
**7.1%** in those years and **5.5%** in the others; over a 1%-real bill it is
5.5% pooled. A premium measured against repressed bills overstates what stocks
earn over today's positive-real-rate bills by about 0.6-1.6 points, which is
another reason the unconditional history is not used by default, and the
regime-adjusted 5.5% sits close to the assumed 5%.

### Known limits of the framework

- **rf was the listing currency's rate only where a CME future quotes it**
  (USD, EUR, GBP, CHF, JPY, CAD, AUD); the rest borrowed the euro's or the US
  bill. Fixed the same day: those currencies now use their central bank's
  policy rate (BIS), so the premium over local bills sits on a local bill.
  TWD and SGD have no BIS series and still use the US bill, labelled.
- β is measured against large-cap price indices (CAC 40, OMX Helsinki 25)
  while the premium is the broad market's: a second-order mismatch.
- A secondary listing uses its own currency's rf with the home market's β and
  premium; that is consistent only to the extent uncovered interest parity
  holds (excess returns are roughly currency-invariant, FX risk premia aside).
- Emerging markets are untested (not in JST); they take the pooled premium.

**Decision:** Assumed stays the default. Historical is the second option in
Settings → Model price, for an owner who wants each market's own record.

## Every paper screened

- Amit Goyal et al. (????), *A Comprehensive Look at the Empirical Performance of Equity Premium Prediction II*
- Béatrice Sagna (1970), *Trading volumes in stock markets : Forecasts, Trading Strategies, Market Impact, Equity Premium, and Monetary Policy*, doi:10.70675/f89ef6beza27bz4120zbd69z5c5e866df1a9
- Umesh Kumar Jaiswal (1970), *Implied Cost of Capital as an Improved Measure of Equity Premium: A Deep Learning Approach*, doi:10.12794/metadc2481297
- Rajnish Mehra et al. (1985), *The equity premium: A puzzle*, doi:10.1016/0304-3932(85)90061-3
- n.a. (1988), *THE EQUITY PREMIUM PUZZLE AND THE RISK-FREE RATE PUZZLE*
- Edward C. Prescott et al. (1988), *The Equity Risk Premium: A Solution?*, doi:10.21034/wp.389
- Philippe Weil (1989), *The Equity Premium Puzzle and the Riskfree Rate Puzzle*, doi:10.1016/0304-3932(89)90028-7
- George M. Constantinides (1990), *Habit Formation: A Resolution of the Equity Premium Puzzle*, doi:10.1086/261693
- Simon Benninga et al. (1990), *Leverage, time preference and the ‘equity premium puzzle’*, doi:10.1016/0304-3932(90)90044-5
- Stephen G. Cecchetti et al. (1991), *The Equity Premium and the Risk Free Rate: Matching the Moments*, doi:10.3386/w3752
- Jeremy J. Siegel (1992), *The Equity Premium: Stock and Bond Returns Since 1802*, doi:10.2469/faj.v48.n1.28
- Olivier Jean Blanchard et al. (1993), *Movements in the Equity Premium*, doi:10.2307/2534565
- Shlomo Benartzi et al. (1995), *Myopic Loss Aversion and the Equity Premium Puzzle*, doi:10.2307/2118511
- Mordecai Kurz et al. (1996), *The Equity Premium is No Puzzle*, doi:10.2139/ssrn.1092
- George Constantinidies et al. (1998), *Junior Can't Borrow:  A New Perspective on the Equity Premium Puzzle*, doi:10.3386/w6617
- Aswath Damodaran (1999), *Estimating Equity Risk Premiums*
- Bradford Cornell (1999), *The Equity Risk Premium: The Long-Run Future of the Stock Market*
- John O'Hanlon et al. (2000), *Estimating the Equity Risk Premium Using Accounting Fundamentals*, doi:10.1111/1468-5957.00346
- Robert Hibbard (2000), *The Equity Premium Puzzle: New Zealand Evidence*, doi:10.1108/eb037953
- Jiang Luo (2000), *Demographics and the Equity Premium*
- Valery Polkovnichenko (2001), *Limited Stock Market Participation and the Equity Premium*, doi:10.2139/ssrn.293830
- Ravi Jagannathan et al. (2001), *The Declining U.S. Equity Premium*, doi:10.3386/w8172
- Ravi Bansal et al. (2002), *Market efficiency, asset returns, and the size of the risk premium in global equity markets*, doi:10.1016/s0304-4076(02)00067-2
- Aswath Damodaran (2002), *Equity Risk Premiums*, doi:10.1007/978-1-4615-0999-8_17
- Martin Lally (2002), *Time Varying Market Leverage, the Market Risk Premium and the Cost of Capital*, doi:10.1111/1468-5957.00470
- Amit Goyal et al. (2002), *Predicting the Equity Premium With Dividend Ratios*, doi:10.3386/w8788
- Elroy Dimson et al. (2003), *Global Evidence on the Equity Risk Premium*, doi:10.2139/ssrn.431901
- Rajnish Mehra et al. (2003), *The Equity Premium in Retrospect*, doi:10.3386/w9525
- Rajnish Mehra (2003), *The Equity Premium: Why is it a Puzzle?*, doi:10.3386/w9512
- Yacine Aı̈t-Sahalia et al. (2003), *Luxury Goods and the Equity Premium*, doi:10.2139/ssrn.385243
- Roelof Salomons et al. (2003), *The equity risk premium: emerging vs. developed markets*, doi:10.1016/s1566-0141(03)00024-4
- Paul Bostock (2004), *The Equity Premium*, doi:10.3905/jpm.2004.319936
- Elmer Sterken et al. (2004), *The Determinants of the Long-Run Equity Risk Premium*, doi:10.2139/ssrn.538402
- Roelof Salomons et al. (2004), *The Equity Risk Premium: Emerging Versus Developed Markets*, doi:10.2139/ssrn.535662
- David Schroeder (2004), *The Implied Equity Risk Premium - An Evaluation of Empirical Methods*, doi:10.2139/ssrn.493042
- Lurion De Mello (2004), *An investigation of the equity premium using habit utility and equity returns: Australian evidence*
- Nigel J. Barradale (2004), *A Simple Methodology to Estimate the Prospective Equity Premium*, doi:10.2139/ssrn.522619
- Robert J. Barro (2005), *Rare Events and the Equity Premium*, doi:10.3386/w11310
- John Robert Graham et al. (2005), *The Long-Run Equity Risk Premium*, doi:10.2139/ssrn.795369
- John Campbell et al. (2005), *Predicting the Equity Premium Out of Sample: Can Anything Beat the Historical Average?*, doi:10.3386/w11468
- Yiyu Shen (2005), *Minimum Consumption Level, Limited Stock Market Participation, and Equity Premium*, doi:10.2139/ssrn.746748
- Simon Grant et al. (2005), *What Does the Equity Premium Mean?*, doi:10.2202/1553-3832.1088
- Eric Jacquier A (2005), *Optimal Estimation of the Risk Premium for the Long Run and Asset Allocation: A Case of Compounded Estimation Risk*, doi:10.1093/jjfinec/nbi001
- n.a. (2005), *The equity risk premium*, doi:10.1017/cbo9781139171335.006
- Pablo Daniel Fernandez (2006), *Equity Premium: Historical, Expected, Required and Implied*, doi:10.2139/ssrn.933070
- Rajnish Mehra (2006), *The Equity Premium in India*, doi:10.3386/w12434
- Elroy Dimson et al. (2006), *The Worldwide Equity Premium: A Smaller Puzzle*, doi:10.2139/ssrn.891620
- Simon P. Ville (2006), *The Equity Premium Puzzle: Australia and the United States in Comparative Perspective*
- Martin Lettau A et al. (2007), *The Declining Equity Premium: What Role Does Macroeconomic Risk Play?*, doi:10.1093/rfs/hhm020
- Andrew J. Vivian (2007), *The UK Equity Premium: 1901–2004*, doi:10.1111/j.1468-5957.2007.02065.x
- John Y. Campbell (2007), *Estimating the Equity Premium*, doi:10.3386/w13423
- Timothy John Brailsford et al. (2007), *A Re-Examination of the Historical Equity Risk Premium in Australia*, doi:10.2139/ssrn.982043
- Ivo Welch et al. (2007), *A Comprehensive Look at The Empirical Performance of Equity Premium Prediction*, doi:10.1093/rfs/hhm014
- n.a. (2007), *The Equity Risk Premium: An Annotated Bibliography*, doi:10.2470/rflr.v2.n1.4477
- Ivo Welch (2007), *A Different Way to Estimate the Equity Premium (for CAPM and One-Factor Model Use Only)*, doi:10.2139/ssrn.1077876
- Laurence S. Copeland et al. (2007), *Rare Disasters and the Equity Premium in a Two-Country World*, doi:10.2139/ssrn.968080
- Rajnish Mehra (2007), *The Equity Premium Puzzle: A Review*, doi:10.1561/0500000006
- Emin Baghramyan (2007), *THE ESTIMATION OF EQUITY RISK PREMIUM FOR CANADA*
- Roelof Salomons et al. (2007), *Corporate control rights and the long-run equity risk premium*, doi:10.1016/j.intfin.2007.08.007
- Stephen F. LeRoy (2008), *Discussion: “The Worldwide Equity Premium: A Smaller Puzzle” and “History and the Equity Risk Premium”*, doi:10.1016/b978-044450899-7.50025-7
- Hanno Lustig (2008), *Discussion: Understanding the Equity Risk Premium Puzzle*, doi:10.1016/b978-044450899-7.50017-8
- George M. Constantinides (2008), *Understanding the Equity Risk Premium Puzzle*, doi:10.1016/b978-044450899-7.50016-6
- Lior Menzly (2008), *Discussion: Cash Flow Risk, Discounting Risk, and the Equity Premium Puzzle*, doi:10.1016/b978-044450899-7.50020-8
- John Donaldson et al. (2008), *Risk-Based Explanations of the Equity Premium*, doi:10.1016/b978-044450899-7.50005-1
- Jesse de Beer (2008), *The historically realised equity risk premium as a guide to future expectations in an emerging market: The case of South Africa*, doi:10.4102/jef.v2i1.361
- Aswath Damodaran (2008), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications*, doi:10.2139/ssrn.1274967
- Danyi Bao (2008), *Time-Varying Market Leverage and the Market Risk Premium in New Zealand*, doi:10.26686/wgtn.16945501
- Fabio Panetta et al. (2008), *Is There an Equity Premium Puzzle in Italy? A look at Asset Returns, Consumption and Financial Structure Data over the Last Century*, doi:10.2139/ssrn.1269506
- Jakob Bruechner Madsen et al. (2008), *The equity premium puzzle and theex postbias*, doi:10.1080/09603100701765174
- Ivo Welch (2008), *The Consensus Estimate for the Equity Premium by Academic Financial Economists in December 2007*, doi:10.2139/ssrn.1084918
- Rajnish Mehra et al. (2008), *The Equity Premium: ABCs*, doi:10.1016/b978-044450899-7.50004-x
- Rajnish Mehra et al. (2008), *Non-Risk-based Explanations of the Equity Premium*, doi:10.1016/b978-044450899-7.50006-3
- Pablo Fernandez (2008), *The Equity Premium in July 2008: Survey (in Spanish)*, doi:10.2139/ssrn.1159818
- A. Damodaran (2008), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications - A Post-Crisis Update*, doi:10.2139/SSRN.1492717
- William N. Goetzmann et al. (2008), *History and the Equity Risk Premium*, doi:10.1016/b978-044450899-7.50024-5
- Pablo Daniel Fernandez et al. (2009), *The equity premium puzzle: High required equity premium, undervaluation and self fulfilling prophecy*
- Jie Jiang Zhu (2009), *Estimating Equity Risk Premium: The Case of Great China*, doi:10.2139/ssrn.1314290
- Robert R. Johnson et al. (2009), *Equity Market Risk Premium and Global Integration*, doi:10.7835/jcc-berj-2009-0019
- David E. Rapach et al. (2009), *Out-of-Sample Equity Premium Prediction: Combination Forecasts and Links to the Real Economy*, doi:10.1093/rfs/hhp063
- Harjoat Singh Bhamra et al. (2009), *The Levered Equity Risk Premium and Credit Spreads: A Unified Framework*, doi:10.1093/rfs/hhp082
- Pablo Fernandez (2009), *Market Risk Premium Used in 2008 by Professors: A Survey with 1,400 Answers*, doi:10.2139/ssrn.1344209
- Michael Brian Cohen (2009), *Estimating the Equity Risk Premium for Economies in the Asian Region*, doi:10.5296/ajfa.v1i1.86
- Bradford Cornell et al. (2010), *The Equity Premium Revisited*, doi:10.2139/ssrn.1651196
- Arash Sani (2010), *Equity Market Risk Premium: Emerging and Frontier Markets*
- Casper van Ewijk et al. (2010), *A Meta-Analysis of the Equity Premium*, doi:10.2139/ssrn.1664942
- Pablo Daniel Fernandez et al. (2010), *Market Risk Premium Used in 2010 by Professors: A Survey with 1,500 Answers*, doi:10.2139/ssrn.1606563
- Andrew van Biljon et al. (2010), *The Equity Premium and Risk-Free Rate Puzzles in a Turbulent Economy: Evidence from 105 Years of Data from South Africa*, doi:10.2139/ssrn.1553704
- Karl Walentin X (2010), *Earnings Inequality and the Equity Premium*, doi:10.2202/1935-1690.1939
- Aswath Damodaran (2010), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications - The 2010 Edition*, doi:10.2139/ssrn.1556382
- David J.C. Smant (2011), *Stock Market Fundamental Valuation and the Implied Equity Risk Premium*, doi:10.2139/ssrn.1911349
- Michael Donadelli et al. (2011), *The Equity Risk Premium: Empirical Evidence from Emerging Markets*, doi:10.2139/ssrn.1893378
- Alan Gregory (2011), *The Expected Cost of Equity and the Expected Risk Premium in the UK*, doi:10.1108/19405979201100001
- Alan Gregory (2011), *Expected Cost of Equity and the Expected Risk Premium in the UK*, doi:10.2139/ssrn.1738904
- Christoph Jäckel et al. (2011), *The Equity Risk Premium Across European Markets: An Analysis Using the Implied Cost of Capital*, doi:10.2139/ssrn.1945311
- Patrick Gagliardini et al. (2011), *Time-Varying Risk Premium in Large Cross-Sectional Equity Datasets*, doi:10.2139/ssrn.1786472
- Pablo Fernandez et al. (2011), *Equity Premium Used in 2011 for the USA by Analysts, Companies and Professors: A Survey*, doi:10.2139/ssrn.1898172
- Aswath Damodaran (2011), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications – The 2011 Edition*, doi:10.2139/ssrn.1769064
- Jack Favilukis (2012), *Inequality, stock market participation, and the equity premium*, doi:10.1016/j.jfineco.2012.10.008
- Gueorgui I. Kolev et al. (2012), *Out-of-Sample Equity Premium Predictability and Sample Split Invariant Inference*, doi:10.2139/ssrn.2024573
- Paulo F. Maio (2012), *Cross-Sectional Return Dispersion and the Equity Premium*, doi:10.2139/ssrn.2021346
- Georges Prat (2012), *Equity risk premium and time horizon: what do the U.S. secular data say?*
- Aswath Damodaran (2012), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications – The 2012 Edition*, doi:10.2139/ssrn.2027211
- Marc Oliver Rieger et al. (2013), *International Evidence on the Equity Premium Puzzle and Time Discounting*, doi:10.17578/17-3/4-2
- Peter Mikael Nyberg et al. (2013), *Equity premium in Finland and long-term performance of the Finnish equity and money markets*, doi:10.1007/s11698-013-0101-7
- A. Damodaran (2013), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications – The 2013 Edition*, doi:10.2139/ssrn.2238064
- Efstathios Avdis et al. (2013), *Maximum likelihood estimation of the equity premium*, doi:10.3386/w19684
- Alexander Boukaras et al. (2013), *The effect of Optimism on teh Equity Premium*
- Vegard Mellingen et al. (2013), *The fundamental-derived equity premium*
- Tae‐Hwy Lee et al. (2014), *Forecasting Equity Premium: Global Historical Average Versus Local Historical Average and Constraints*, doi:10.1080/07350015.2014.955174
- Loukia Meligkotsidou et al. (2014), *A Quantile Regression Approach to Equity Premium Prediction*, doi:10.1002/for.2312
- Ruihao Ke et al. (2014), *Estimating the Equity Risk Premium Using Implied Cost of Capital*, doi:10.2139/ssrn.2419832
- Shantaram P. Hegde et al. (2014), *Patented Knowledge Capital and Implied Equity Risk Premium*, doi:10.2139/ssrn.2385062
- n.a. (2014), *Equity Risk Premium*, doi:10.1002/9781118846780.ch08
- SungSup Choi et al. (2014), *A Note on the Interrelation of Volatility Puzzle, Equity Premium Puzzle, and Mean Reversion through State Dependent Preferences*, doi:10.2139/ssrn.2377120
- Pablo Fernández et al. (2014), *Market Risk Premium Used in 88 Countries in 2014: A Survey with 8,228 Answers*, doi:10.2139/ssrn.2450452
- William John Arant (2014), *Determinants of Cross-Country Differences in the Equity Risk Premium: A Cross-Sectional Analysis*
- A. Damodaran (2014), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications – The 2014 Edition*, doi:10.2139/ssrn.2409198
- Anthony M. Diercks (2015), *The Equity Premium, Long-Run Risk, &amp; Optimal Monetary Policy*, doi:10.2139/ssrn.2670122
- Rajni Kant Rajhans (2015), *Equity Risk Premium Puzzle: Case of Indian Stock Market*, doi:10.2139/ssrn.2568966
- Giuliano Curatola et al. (2015), *Matching the BRIC equity premium: A structural approach*, doi:10.1016/j.ememar.2014.12.001
- A. Damodaran (2015), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications – The 2015 Edition*, doi:10.2139/ssrn.2581517
- Rajni Kant Rajhans (2015), *Equity Risk Premium Puzzle: The Case of Indian Stock Market [Dagger]*
- Santhosh Kumar (2015), *Equity risk premium: A cross country analysis on return behaviour*, doi:10.12725/ujbm.32.4
- Sunil Poshakwale et al. (2016), *The impact of monetary policy shocks on the equity risk premium before and after the Quantitative Easing in the United Kingdom*, doi:10.21511/imfi.13(4-1).2016.01
- A. Damodaran (2016), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications – The 2016 Edition*, doi:10.2139/ssrn.2742186
- Mohamed Mehdi Rahoui (2016), *Essays on the Equity Risk Premium*, doi:10.25777/x6cm-5e57
- Jan Antell et al. (2016), *Countercyclical and time-varying reward to risk and the equity premium*, doi:10.2139/ssrn.2753537
- Aleksandar Naumoski et al. (2016), *Expected Equity Risk Premium: Survey Approach in the Case of a Small Emerging Market*
- Christina Christou et al. (2017), *Does inequality help in forecasting equity premium in a panel of G7 countries?*, doi:10.1016/J.NAJEF.2021.101456
- Aswath Damodaran (2017), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications – The 2017 Edition*, doi:10.2139/ssrn.2947861
- Xiaoyuan Wang (2018), *Modified Fama-French Three-Factor Model and the Equity Premium: An Empirical Test in the Chinese Stock Market*, doi:10.1088/1757-899X/466/1/012014
- Fabrice Collard et al. (2018), *Ambiguity and the historical equity premium*, doi:10.3982/qe708
- Aswath Damodaran (2018), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications – The 2018 Edition*, doi:10.2139/ssrn.3140837
- Heleen Goussard (2018), *The relationship between various risk factors and the cost of equity premium implied by analysts' forecasts on the New York Stock Exchange*
- Leandro Gomes et al. (2018), *Term Structure(s) of the Equity Risk Premium*, doi:10.2139/ssrn.3144028
- Sami Toivonen (2018), *Behavior of Equity Risk Premium After a Catastrophic Event*
- Raja Adnan Ali et al. (2019), *Equity Risk Premium in Pakistan: Comparative Estimates from Historical Returns, Dividend and Earnings Models*, doi:10.2139/ssrn.3510621
- A. Damodaran (2019), *Equity Risk Premiums (ERP): Determinants, Estimation and Implications – The 2019 Edition*, doi:10.2139/ssrn.3378246
- Anthony M. Diercks (2019), *The Equity Premium, Long-Run Risks to R-Star, and Asymmetric Optimal Monetary Policy*, doi:10.2139/ssrn.3435372
- Jie Jiang Zhu (2019), *ESTIMATING EQUITY RISK PREMIUM: THE CASE OF GREATER CHINA*, doi:10.21098/bemp.v22i2.1088
- Yuuki Maruyama (2019), *A model of mean reversion in stock prices and the Equity Premium Puzzle*, doi:10.31219/osf.io/6gwjq
- Gregory F Moscato (2019), *Forward Looking Equity Risk Premium: A Normative Long-Term View*, doi:10.4236/tel.2019.98186
- Will Cassidy (2020), *Long-Run Taxation Risk and the Presidential Equity Premium Puzzle*, doi:10.2139/ssrn.3718410
- Christina Christou et al. (2020), *Forecasting equity premium in a panel of OECD countries: The role of economic policy uncertainty*, doi:10.1016/J.QREF.2019.08.001
- Yulia Merkoulova et al. (2020), *Does It Pay to Invest? The Personal Equity Risk Premium and Stock Market Participation*, doi:10.2139/ssrn.3661024
- Aswath Damodaran (2020), *Equity Risk Premiums: Determinants, Estimation and Implications - The 2020 Edition*, doi:10.2139/ssrn.3550293
- Pablo Daniel Fernandez (2020), *The Equity Premium in 150 Textbooks*, doi:10.46671/2521-2486.1009
- Ilias Tsiakas et al. (2020), *Equity premium prediction and the state of the economy*, doi:10.1016/j.jempfin.2020.05.004
- Thiago de Oliveira Souza (2020), *Two Out-of-Sample Forecasting Models of the Equity Premium*, doi:10.2139/ssrn.3718404
- Ömer EREN et al. (2020), *MEAN REVERSION IN TURKISH STOCK MARKET AND TIME-VARYING EQUITY RISK PREMIUM*, doi:10.14780/muiibd.763893
- Hang Bai et al. (2020), *Searching for the Equity Premium*, doi:10.3386/w28001
- Abootaleb Shirvani et al. (2020), *Equity Premium Puzzle or Faulty Economic Modelling?*, doi:10.1007/s11156-020-00928-3
- Antônio Zoratto Sanvicente et al. (2020), *Determinants of the implied equity risk premium in Brazil*, doi:10.12660/rbfin.v18n1.2020.80038
- RAFAELA BIANCA PINI RIZZO (2021), *INTERNATIONAL RESERVES AND THE EQUITY PREMIUM*, doi:10.17771/pucrio.acad.55328
- Sungju Chun (2021), *Forecasting the Equity Risk Premium in the Korean Stock Market: A Factor Analysis Approach*, doi:10.17549/gbfr.2021.26.4.77
- Pablo Fernandez et al. (2021), *Survey: Market Risk Premium and Risk-Free Rate used for 88 countries in 2021*, doi:10.2139/ssrn.3861152
- Jiří Kučera et al. (2021), *Methodology of calculating risk premiums in the environment of the Czech Republic and its comparison with Damodaran*, doi:10.1051/shsconf/20219101045
- Dmitry А. Endovitsky et al. (2022), *Regime shifts in equity risk premium: international evidence*, doi:10.17308/econ.2022.1/3842
- Tyler Beason et al. (2022), *Dissecting the Equity Premium*, doi:10.1086/720396
- Basri Basri et al. (2022), *Fundamental, Stock Market, and Macroeconomic Factors on Equity Premium – Evidence from Indonesia Stock Exchange*, doi:10.32602/jafas.2022.022
- Kuok Sin Un et al. (2022), *Equity premium prediction: Taking into account the role of long, even asymmetric, swings in stock market behavior*, doi:10.1016/j.physa.2022.128285
- Aswath Damodaran (2022), *Equity Risk Premiums (ERP): Determinants, Estimation, and Implications – The 2022 Edition*, doi:10.2139/ssrn.4066060
- Jonathan A. Batten et al. (2022), *Beating the Average: Equity Premium Variations, Uncertainty, and Liquidity*, doi:10.1111/abac.12250
- Patrick Launhardt et al. (2022), *Aggregate Implied Cost of Capital, Option Implied Information and Equity Premium Predictability*, doi:10.2139/ssrn.3405213
- Christoph Kaserer (2022), *Estimating the market risk premium for valuations: arithmetic or geometric mean or something in between?*, doi:10.1007/s11573-022-01104-w
- Ayodeji Michael Obadire (2022), *Empirical Analysis of the Effects of Macroeconomic Variables on the Equity Market Risk Premium in South Africa*, doi:10.7176/rjfa/13-16-03
- T. Eldomiaty et al. (2023), *How Do Stock Market Development and Competitiveness Affect Equity Risk Premium? Implications from World Economies*, doi:10.3390/ijfs11010030
- BN Kausik (2023), *Equity Premium in Efficient Markets*, doi:10.2139/ssrn.4644881
- Yunqi Wang et al. (2023), *Out-of-sample equity premium prediction: The role of option-implied constraints*, doi:10.1016/j.jempfin.2022.12.004
- Xianzheng Zhou et al. (2023), *Forecasting the equity premium: Do deep neural network models work?*, doi:10.61351/mf.v1i1.2
- Deshui Yu et al. (2023), *Time-Varying Predictability of the Long Horizon Equity Premium Based on Semiparametric Regressions*, doi:10.2139/ssrn.4319953
- Anand Manikutty (2023), *Disruptive Dependency Theory and the Equity Premium Puzzle: A New Answer to the Equity Premium Puzzle*, doi:10.2139/ssrn.4387624
- Thomas Maloney (2024), *Honey, the Fed Shrunk the Equity Premium: Asset Allocation in a Higher-Rate World*, doi:10.3905/jpm.2024.1.601
- Hang Cheng et al. (2024), *Multifactor Conditional Equity Premium Model: Evidence from China's Stock Market*, doi:10.1016/j.jbankfin.2024.107117
- Mehran Azimi et al. (2024), *Probability weighting and equity premium prediction: Investing with optimism*, doi:10.1111/fima.12477
- Yee-Peng Chow et al. (2024), *Associations among equity risk premium models in Malaysia*, doi:10.32890/gbmr2024.16.1.3
- Xingfu Xu et al. (2024), *Forecasting the Equity Premium: Can Machine Learning Beat the Historical Average?*, doi:10.2139/ssrn.4781195
- Amit Goyal et al. (2024), *A Comprehensive 2022 Look at the Empirical Performance of Equity Premium Prediction*, doi:10.1093/rfs/hhae044
- J. Austin Murphy et al. (2024), *The Determinants of the Time-Varying Equity Premium*, doi:10.2139/ssrn.4757330
- Stefano Battilossi et al. (2025), *The historical and expected equity risk premium in Spain: a long-run view, 1900–2020*, doi:10.1007/s11698-025-00309-7
- Aditya Saxena (2025), *Revisiting the Equity Premium Puzzle: Time Series Analysis and Forecasting from 1990 to 2012*, doi:10.1109/AIMLA63829.2025.11041424
- Roland Clere (2025), *&lt;b&gt;Expected Equity Risk Premium, Stock Market Performance, and Fundamental Uncertaint&lt;/b&gt;y*, doi:10.2139/ssrn.5292299
- Rajan Raju (2025), *Estimating Equity Risk Premium in India: A Multi-Method Analysis of Historical, Implied, and Cross-Sectional Approaches*, doi:10.2139/ssrn.5217024
- Natasha Macharia (2025), *Back testing Equity Risk Premium in the Kenyan stock market*
- Hollstein, Fabian et al. (2025), *Predicting the equity premium around the globe: Comprehensive evidence from a large sample*, doi:10.15488/20425
- Yanyan Lin et al. (2025), *Equity Risk Premium Prediction: Return Decomposition and Noise Shrinkage*, doi:10.2139/ssrn.5497463
- Xiaobin Liu et al. (2025), *Equity Premium Prediction with Shrinkage Estimation Guided by Economic Theory*, doi:10.2139/ssrn.5577810
- George-Aurelian Ghita et al. (2025), *Modeling the Effects of Sustainable Development on the Equity Risk Premium*, doi:10.65672/jfme.2025.1.4
- Rajan Raju (2026), *Three Perspectives on the Equity Risk Premium: Evidence from India*, doi:10.2139/ssrn.6159926
- A. C. Călin et al. (2026), *Climate transition risk premium in equity markets: Downside asymmetry and regime dependence across Europe, the United States, and the United Kingdom*, doi:10.1016/j.bir.2026.100895
- Gerson Brilhante (2026), *The Non-Existent Equity Risk Premium in Brazil: Why the Stock Market is Not a True Real Asset*, doi:10.2139/ssrn.5989656
- Jennifer Z. Sun et al. (2026), *Explaining the equity premium in Australia: a disaggregated consumption approach*, doi:10.1108/jal-11-2025-0652
- Ratbek Dzhumashev (2026), *Segmented  Asset Markets and the Equity Premium*, doi:10.2139/ssrn.6232265
- Jeonggyu Huh et al. (2026), *Equity premium forecasting with reliability-screened forward-looking signals*, doi:10.1371/journal.pone.0341578
