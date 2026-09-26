# Literature Review — ML News-Sentiment Quantification (MLNews)

Compiled 2026-07-13 for the MLNews pipeline (see `docs/ml_sentiment_design.md`).
~60 papers collected via multi-source academic search (Crossref, OpenAlex, arXiv,
Semantic Scholar), organized by the **design decision each cluster informs**.
Every locked pipeline choice below cites the evidence that motivated it.

## How the literature maps to pipeline decisions

| Pipeline decision | Grounding (sections) |
|---|---|
| Label = next open/close window return after release | §A (Tetlock, Chan, Heston-Sinha), §C (timing) |
| Beta-adjust vs index, then standardize by trailing vol (SAR) | §C (BMP 1991, Blume shrinkage) |
| Overnight vs intraday window split by release time | §C (Lou-Polk-Skouras), §A (Heston-Sinha) |
| Weak labels from realized returns (no human annotation) | §B (Ke-Kelly-Xiu SESTM — the direct ancestor) |
| Relevance heuristic + novelty/duplicate discounting | §D (Tetlock 2011, Boudoukh et al.) |
| LM lexicon + hashed TF-IDF features, no embeddings | §E (Loughran-McDonald, Weinberger hashing) |
| LightGBM via FLAML, strict time-ordered CV | §F, §G (backtest-overfitting literature) |
| Group-level IC evaluation, walk-forward stability gates | §G |
| Quantile tier cuts on the predicted-return distribution | §A (García regime-dependence), §G (calibration) |

---

## A. News and stock returns — the core evidence

1. **Tetlock (2007), "Giving Content to Investor Sentiment: The Role of Media in the
   Stock Market," *J. Finance* 62(3).** doi:10.1111/j.1540-6261.2007.01232.x.
   High media pessimism predicts downward pressure then reversion — the founding
   result that newspaper text carries priced information.
2. **Tetlock, Saar-Tsechansky & Macskassy (2008), "More Than Words: Quantifying
   Language to Measure Firms' Fundamentals," *J. Finance* 63(3).**
   doi:10.1111/j.1540-6261.2008.01362.x. Negative word fraction in firm-specific
   news predicts earnings and returns; market response concentrates within a day —
   supports our tight (next open/close) label window.
3. **Chan (2003), "Stock price reaction to news and no-news: drift and reversal
   after headlines," *JFE* 70(2).** doi:10.1016/s0304-405x(03)00146-6. Drift after
   public news, reversal after no-news moves — a same-magnitude return means
   different things with vs without news, which is exactly why the model conditions
   on text rather than the return alone.
4. **Antweiler & Frank (2004), "Is All That Talk Just Noise? The Information Content
   of Internet Stock Message Boards," *J. Finance* 59(3).**
   doi:10.1111/j.1540-6261.2004.00662.x. Early large-scale (1.5M messages) NLP;
   effect sizes are small but statistically robust — set expectations: single-digit
   % R² / low IC is a *good* outcome in this domain.
5. **Boudoukh, Feldman, Kogan & Richardson (2013), "Which News Moves Stock Prices?
   A Textual Analysis," NBER w18725.** doi:10.3386/w18725. Only ~50% of "news days"
   contain identifiably relevant firm news; conditioning on *relevance* roughly
   doubles explained variance — the single strongest justification for our
   deterministic relevance score.
6. **Heston & Sinha (2017), "News vs. Sentiment: Predicting Stock Returns from News
   Stories," *FAJ* 73(3).** doi:10.2469/faj.v73.n3.3 (also FEDS 2016-048). Daily
   news predicts returns for 1–2 days; **weekly aggregation of sentiment predicts
   for a quarter**; and crucially, response depends on *time-of-day* of publication
   — direct support for the session-classified label windows.
7. **Fang & Peress (2009), "Media Coverage and the Cross-Section of Stock Returns,"
   *J. Finance* 64(5).** doi:10.1111/j.1540-6261.2009.01493.x. No-coverage stocks
   earn a premium; coverage intensity is itself priced — media *presence* (our
   article_count / novelty features) is informative beyond tone.
8. **Fedyk (2024), "Front-Page News: The Effect of News Positioning on Financial
   Markets," *J. Finance*.** doi:10.1111/jofi.13287. Positioning/prominence causes
   trading independent of content — motivates publisher-tier and salience features.
9. **Baker & Wurgler (2006), "Investor Sentiment and the Cross-Section of Stock
   Returns," *J. Finance* 61(4).** doi:10.2139/ssrn.464843. Sentiment effects are
   conditional on firm characteristics (size, volatility) — motivates the
   dollar-volume slice tests in validation.
10. **Barber & Odean (2008), "All That Glitters: The Effect of Attention and News on
    the Buying Behavior of Individual and Institutional Investors," *RFS* 21(2).**
    doi:10.1093/rfs/hhm079. Attention-grabbing news triggers net retail buying
    regardless of sign — a mechanism for why *high-relevance* news moves prices
    more than tone alone implies.
11. **Hirshleifer, Lim & Teoh (2009), "Driven to Distraction: Extraneous Events and
    Underreaction to Earnings News," *J. Finance* 64(5).**
    doi:10.1111/j.1540-6261.2009.01501.x. Competing-news days show weaker immediate
    response and more drift — motivates keeping same-day market-wide news volume
    out of scope for v1 but flags it as a future feature.
12. **Jeon, McCurdy & Zhao (2022), "News as sources of jumps in stock returns:
    Evidence from 21 million news articles for 9000 companies," *JFE*.**
    doi:10.1016/j.jfineco.2021.08.002. At FNSPID-like scale, news maps to return
    *jumps*; fat-tailed responses justify SAR winsorization at ±5σ rather than
    dropping tails.
13. **García (2013), "Sentiment during Recessions," *J. Finance* 68(3).**
    doi:10.1111/jofi.12027. News-return sensitivity is state-dependent (stronger in
    recessions) — motivates the year-by-year walk-forward IC gate and calendar
    features.
14. **Glasserman & Mamaysky (2019), "Does Unusual News Forecast Market Stress?"
    *JFQA*.** doi:10.1017/s0022109019000127. "Unusualness" of news interacts with
    sentiment — novelty is not just a duplicate discount, it's signal.
15. **Daniel, Hirshleifer & Subrahmanyam (1998), "Investor Psychology and Security
    Market Under- and Overreactions," *J. Finance* 53(6).**
    doi:10.1111/0022-1082.00077. Theoretical under/overreaction framework behind
    both immediate-reaction labels and drift horizons.

## B. Return-supervised sentiment & LLM baselines — the method's ancestry

16. **Ke, Kelly & Xiu (2019), "Predicting Returns with Text Data," NBER w26186.**
    doi:10.3386/w26186. **SESTM — the closest methodological ancestor of this
    pipeline**: learn a sentiment dictionary *supervised by realized returns*
    instead of human labels. Validates the entire weak-label framing; their
    screening-for-relevant-words step parallels our TF-IDF + LightGBM feature
    selection.
17. **Lopez-Lira & Tang (2023), "Can ChatGPT Forecast Stock Price Movements? Return
    Predictability and Large Language Models," SSRN 4412788 / arXiv 2304.07619.**
    LLM headline scores predict next-day returns (long-short Sharpe > 3 in-sample
    window); the ancestor of the app's News read (the NIM LLM engine), which now
    runs as a peer of the return-supervised Market read rather than its baseline.
18. **Araci (2019), "FinBERT: Financial Sentiment Analysis with Pre-trained Language
    Models," arXiv 1908.10063.** Domain-adapted transformer baseline; PhraseBank
    accuracy ~0.86 — context for the News read's 0.94 PhraseBank direction
    accuracy and the 0.65 polarity sanity floor for the return-supervised model
    (different task, lower bar).
19. **Huang, Wang & Yang (2023), "FinBERT: A Large Language Model for Extracting
    Information from Financial Text," *Contemporary Accounting Research*.**
    Demonstrates fine-tuned finance LMs beat dictionaries on classification — but
    require labeled data; our return-label approach sidesteps annotation entirely.
20. **"Evaluation of Sentiment Analysis in Finance: From Lexicons to Transformers,"
    *IEEE Access* (2020).** doi:10.1109/access.2020.3009626. Systematic comparison:
    lexicons are surprisingly competitive on short financial text — supports LM
    lexicon features remaining in the model rather than TF-IDF alone.
21. **Wu et al. (2023), "BloombergGPT: A Large Language Model for Finance," arXiv
    2303.17564.** Frontier of finance-domain LMs; context for why a hosted general
    model with a strict per-headline schema (not a finance-specific LM) is the
    News read's production choice.
22. **Malo, Sinha, Korhonen, Wallenius & Takala (2014), "Good Debt or Bad Debt:
    Detecting Semantic Orientations in Economic Texts," *JASIST* 65(4).** The
    Financial PhraseBank — wired into `scripts/benchmark_news_read.py` (News
    read certification) and reused as the human-label polarity anchor for the
    ML model.
23. **Dong, Yan, Zhao et al. (2024), "FNSPID: A Comprehensive Financial News Dataset
    in Time Series," KDD 2024.** doi:10.1145/3637528.3671629 / arXiv 2402.06698.
    The training corpus: 15.7M articles, 4,775 tickers, 1999–2023. Their own
    experiments show news sentiment improves price-prediction accuracy at scale;
    known caveats (mixed timestamp resolution, publisher-dependent coverage) drive
    our Phase-3 audit-first design.
24. **"Open-Source Edge LLMs for Forecasting Stock Returns via News Headline
    Sentiment," SSRN 6608038 (2026).** Contemporary evidence that small local
    models score headlines usefully — parallel to our NIM choice.
25. **Shapiro, Sudhof & Wilson (2019-22), "Daily market news sentiment and stock
    prices" (*Applied Economics* version, 2019).** doi:10.1080/00036846.2018.1564115.
    Daily aggregate news sentiment indices lead prices — grounds the market-wide
    (`s_mkt`) component kept from the existing pipeline.

## C. Event-study label design — SAR, timing, beta

26. **Boehmer, Musumeci & Poulsen (1991), "Event-study methodology under conditions
    of event-induced variance," *JFE* 30.** doi:10.1016/0304-405x(91)90032-f.
    **The canonical case for standardized abnormal returns**: dividing by
    pre-event volatility restores test power when events change variance — the
    direct justification for SAR as the label (label method #1).
27. **Lou, Polk & Skouras (2019), "A tug of war: Overnight versus intraday expected
    returns," *JFE* 134.** doi:10.1016/j.jfineco.2019.03.011. Overnight and intraday
    returns have *different* factor exposures and dynamics — the reason we compute
    window-type-specific σ (σ_co vs σ_oc) instead of scaling daily σ by √fraction.
28. **Blume (1975), "Betas and Their Regression Tendencies," *J. Finance* 30(3).**
    doi:10.1111/j.1540-6261.1975.tb01850.x — and **Blume (1979), "…Some Further
    Evidence," *J. Finance* 34.** doi:10.1111/j.1540-6261.1979.tb02088.x. Estimated
    betas mean-revert toward 1; the 0.67/0.33 shrinkage we apply to `beta_252` is
    Blume's adjustment.
29. **Lally (1998), "An Examination of Blume and Vasicek Betas," *Financial Review*
    33.** doi:10.1111/j.1540-6288.1998.tb01390.x. Blume vs Vasicek shrinkage
    comparison; Blume is adequate for our purpose (hedge ratio, not pricing).
30. **Bernard & Thomas (1989), "Post-Earnings-Announcement Drift: Delayed Price
    Response or Risk Premium?" *J. Accounting Research* 27.** doi:10.2307/2491062.
    PEAD — the reason multi-day CAR labels (ranked #3) capture more total signal
    but with contamination; kept as a documented alternative, not the target.
31. **"Warp speed price moves: Jumps after earnings announcements," *JFE* (2025).**
    doi:10.1016/j.jfineco.2025.104010. Modern markets impound earnings news in
    seconds-to-minutes — the next open/close window captures essentially all of the
    immediate reaction; nothing material is lost by not having tick data.
32. **Michaely, Rubin & Vedrashko (2016), "Market (in)attention and the strategic
    scheduling and timing of earnings announcements," *JAE* 61.**
    doi:10.1016/j.jacceco.2015.03.003. BMO/AMC timing conventions and their
    strategic use — the basis for our earnings-headline timestamp cross-check in
    the Phase-3 timezone audit.
33. **"Variable trading hours and market reactions to earnings announcements,"
    *Economics Letters* (2023).** doi:10.1016/j.econlet.2023.111199 — and
    **"The Impact of Earnings Announcements Before and After Regular Market Hours
    on Asset Price Dynamics," SSRN 5071234.** After-hours releases resolve mostly
    in the overnight gap — validates close→open as the after-close label window.
34. **"After-Hours Market Reactions and Media Coverage of Firms' Earnings
    Announcements," SSRN 4935328.** Media coverage amplifies after-hours reactions —
    interaction of timing and coverage features.
35. **Campbell, Lo & MacKinlay (1997), *The Econometrics of Financial Markets*,
    ch. 4 (event studies).** Standard reference for the market-model abnormal
    return `r − β·m` that `abn` implements.

## D. Stale news, duplicates, and relevance

36. **Tetlock (2011), "All the News That's Fit to Reprint: Do Investors React to
    Stale Information?" *RFS* 24(5).** doi:10.1093/rfs/hhq141. Individual investors
    trade on *reprinted* (stale) news; the price impact of stale news reverses.
    Directly motivates: (i) dedup before labeling, (ii) `n_duplicates` as a
    salience-with-reversal-risk feature, (iii) group-level labels so syndication
    doesn't manufacture training rows.
37. **Boudoukh et al. (2013)** (see #5) — relevance conditioning doubles explained
    variance; our `relevance_score` is the heuristic stand-in for their
    identified-relevance classifier.
38. **"KU Sentiment, Novelty, and Relevance," in *Knightian Uncertainty* (CUP,
    2022).** doi:10.1017/9781108974899.010. Commercial news-analytics practice
    (RavenPack-style) scores every article on sentiment × novelty × relevance —
    the exact triple our aggregation weights implement.
39. **"News Analytics: From Market Attention and Sentiment to Trading" (CRC
    Handbook chapter, 2019).** doi:10.1201/9780429183942-7. Survey of production
    news-analytics pipelines; confirms industry practice of decaying event windows
    and source tiers.
40. **"Media Co-mention Structure, Attention Efficiency, and Cross-firm
    Predictability," SSRN 5130201.** Articles mentioning many firms transmit
    weaker firm-specific signal — the basis for the co-mention penalty
    `1/(1+0.4(n−1))` in the relevance heuristic.
41. **"Mention and Entity Description Co-Attention for Entity Disambiguation,"
    AAAI 2018.** doi:10.1609/aaai.v32i1.12043. Entity-disambiguation context: our
    company-name matching (symbol_db + fuzzy position-weighted match) is a
    lightweight, deterministic version of this problem.
42. **"News, Sentiment and Trading," SSRN 4966869.** Recent practitioner evidence
    on sentiment-signal decay horizons — informs the recency τ used at aggregation.

## E. Text features — dictionaries, hashing, representations

43. **Loughran & McDonald (2011), "When Is a Liability Not a Liability? Textual
    Analysis, Dictionaries, and 10-Ks," *J. Finance* 66(1).**
    doi:10.1111/j.1540-6261.2010.01625.x. Finance-specific word lists (the LM
    lexicon already vendored in `convexity/data/lm_lexicon.json`);
    general-purpose dictionaries misclassify financial text.
44. **Loughran & McDonald (2016), "Textual Analysis in Accounting and Finance: A
    Survey," *J. Accounting Research* 54(4).** doi:10.1111/1475-679x.12123.
    Methodology survey; warns about document-length effects and term weighting —
    motivates L2 normalization and headline/summary truncation consistency.
45. **Loughran & McDonald (2020), "Textual Analysis in Finance," *Annual Review of
    Financial Economics*.** doi:10.1146/annurev-financial-012820-032249. Updated
    survey including ML methods; bag-of-words remains competitive with embeddings
    on short financial text — supports the no-embeddings decision.
46. **Loughran & McDonald (2015), "The Use of Word Lists in Textual Analysis,"
    *J. Behavioral Finance* 16.** doi:10.1080/15427560.2015.1000335. Practical
    guidance on lexicon pitfalls (negation, context) — our lexicon.py already
    implements a 3-token negation window.
47. **Kearney & Liu (2014), "Textual sentiment in finance: A survey of methods and
    models," *Int. Rev. Financial Analysis* 33.** doi:10.1016/j.irfa.2014.02.006.
    Taxonomy of sentiment sources/methods; headline-only vs full-text tradeoffs.
48. **Gentzkow, Kelly & Taddy (2019), "Text as Data," *J. Economic Literature*
    57(3).** doi:10.1257/jel.20181020. The canonical text-to-econometrics
    reference: sparse counts + regularized linear/tree models as the default
    before deep methods; exactly the TF-IDF + GBM design.
49. **Weinberger, Dasgupta, Langford, Smola & Attenberg (2009), "Feature Hashing
    for Large Scale Multitask Learning," ICML.** doi:10.1145/1553374.1553516 /
    arXiv 0902.2206. Hashing-trick collision analysis: at 2^18 dims for a
    ~100k-vocab corpus, collision-induced error is negligible — and the
    transform is stateless, killing train/serve vocabulary skew by construction.
50. **Ash & Hansen (2023), "Text Algorithms in Economics," *Annual Review of
    Economics*.** doi:10.1146/annurev-economics-082222-074352. Modern review
    bridging dictionaries → embeddings; supports interpretability as a legitimate
    selection criterion for production financial models.

## F. Model class and AutoML

51. **Ke, Meng, Finley et al. (2017), "LightGBM: A Highly Efficient Gradient
    Boosting Decision Tree," NeurIPS 30.** Histogram-based GBDT with native sparse
    support — why 3M×262k sparse rows train inside an 8 GB budget at all.
52. **Wang, Wu, Weimer & Zhu (2021), "FLAML: A Fast and Lightweight AutoML
    Library," MLSys / arXiv 1911.04706.** Cost-frugal hyperparameter search under
    an explicit time budget — matches the 4-hour constraint exactly; its
    `split_type="time"` implements ordered expanding-window CV.
53. **"Table 1: Technical comparison of seven AutoML frameworks," *PeerJ CS*
    (2025).** doi:10.7717/peerj-cs.3497/table-1. Independent benchmark placing
    FLAML competitive at fixed small budgets — supports the single-library choice.

## G. Evaluation, overfitting, calibration

54. **López de Prado (2018), *Advances in Financial Machine Learning*, Wiley —
    chs. 7 (purged K-fold, embargo) and 11–14 (backtest statistics).** Random
    K-fold leaks overlapping-window information in financial panels; our
    time-ordered expanding CV + 6-month untouched holdout + group-level labels is
    the purged-CV prescription adapted to event data.
55. **Bailey, Borwein, López de Prado & Zhu (2015), "Statistical Overfitting and
    Backtest Performance," SSRN 2507040** — and **"Backtest Overfitting
    Demonstration Tool," SSRN 2597421.** Multiple-testing inflates apparent
    performance; why the tier-cut grid search uses bootstrap stability constraints
    rather than picking the best-looking cut.
56. **"Backtest Overfitting in the Machine Learning Era: A Comparison of
    Out-of-Sample Testing Methods," SSRN 4778909 (2024).** Modern comparison of
    OOS protocols for ML strategies; walk-forward yearly evaluation (our test #4)
    ranks among the most robust.
57. **"An Empirical Evaluation of Cross-Sectional Equity Signals Under Backtest
    Overfitting Diagnostics," SSRN 6078546 (2026).** Contemporary practice for
    cross-sectional signal validation — deflated performance metrics context.
58. **Grinold (1989), "The Fundamental Law of Active Management," *JPM* 15(3)** —
    and **Ding (2016), "The Fundamental Law of Active Management: Redux," SSRN
    2730434.** IR ≈ IC·√breadth: why a small per-event IC (0.02–0.05) at
    FNSPID breadth is economically meaningful, and why IC (not R²) is the primary
    quality metric.
59. **Niculescu-Mizil & Caruana (2005), "Predicting Good Probabilities with
    Supervised Learning," ICML.** Tree ensembles need output calibration —
    analogous motivation for calibrating tier cuts on out-of-fold predictions
    rather than trusting raw prediction magnitudes.
60. **Hüllermeier & Waegeman (2021), "Aleatoric and epistemic uncertainty in
    machine learning," *Machine Learning* 110.** doi:10.1007/s10994-021-05946-3.
    Frames the `ml_confidence` field: dispersion across articles (aleatoric) vs
    thin-coverage tickers (epistemic) should be surfaced differently.

---

## Synthesis — what the literature changed or confirmed

1. **Confirmed SAR label (BMP 1991 + Lou-Polk-Skouras 2019).** Standardization by
   *window-type-specific* pre-event vol is not just variance stabilization — it is
   required for valid cross-sectional comparison because overnight and intraday
   returns are different processes.
2. **Relevance is the highest-leverage input (Boudoukh et al. 2013).** Roughly half
   of firm-tagged news is not firm-relevant; conditioning on relevance ~doubles
   explained variance. The heuristic must be good, not an afterthought.
3. **Dedup before labeling is mandatory (Tetlock 2011).** Stale reprints move
   prices *and then reverse* — training on duplicates as independent rows would
   teach the model reversal noise. Group-level labels + 1/n weights implement this.
4. **Expect small ICs and design for breadth (Antweiler-Frank 2004, Grinold 1989).**
   Per-event R² will be ~1%; the value comes from breadth (millions of events).
   Gates are set on IC and monotonicity, not R² thresholds.
5. **Time-of-day matters (Heston-Sinha 2017, earnings-timing literature).** The
   session-classified window assignment is empirically grounded, and the
   date-only fallback (close→close next day) is conservative w.r.t. reverse
   causality at the cost of attenuation.
6. **Regime dependence is real (García 2013).** Year-by-year walk-forward gates and
   the "retrain on 2016+ and compare" decision rule directly address it.
7. **Bag-of-words + GBM is a defensible frontier for short financial text
   (LM 2020, Gentzkow et al. 2019, IEEE Access 2020).** Embeddings buy little on
   headlines and cost train/serve complexity; hashing (Weinberger 2009) makes the
   featurizer stateless and skew-proof.
8. **Guard against multiple-testing in the tier-cut search (Bailey et al.).** The
   bootstrap-stability constraints on the quantile grid are the deflation
   mechanism; picking the max-spread cut without them would be textbook overfit.
