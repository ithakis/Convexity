# Finance & Market Data APIs for a Retail Investor Workflow: Rigorous Comparison vs. yfinance

## PART 1: EXECUTIVE SUMMARY

- **For this specific workflow (forward estimates → ANR → news, US + Europe, sensible cost), the single best provider is Financial Modeling Prep (FMP) on the Premium ($59/mo annual billing) or Ultimate ($149/mo annual billing) tier.** Premium gives 30 years of fundamentals, analyst estimates, price targets, upgrades/downgrades, news, press releases, intraday charts, and UK+Canada exchanges; Ultimate adds full global coverage (Euronext, XETRA, etc.) and earnings call transcripts. No other sub-$200 provider bundles forward estimates AND European prices AND news in one API.
- **Best budget improvement on yfinance: Tiingo Power at $30/mo, or Finnhub Free + EODHD All-World at €19.99/mo.** Tiingo gives clean adjusted EOD prices, dividend/split data, and a real news API (which yfinance lacks entirely). EODHD adds proper European exchange coverage. Both are dramatically more reliable than yfinance for batch jobs.
- **Best low-cost stack (~$60–110/mo combined):** EODHD Fundamentals Data Feed (€59.99/mo) or EODHD All-In-One (€99.99/mo) as the spine for global prices + estimates + news + corporate actions; keep yfinance only as a free convenience layer for ad-hoc Yahoo metadata.
- **Best premium option for retail: FMP Ultimate ($149/mo).** Polygon Stocks Advanced ($199/mo) is superior for US intraday/tick data but has no European stock coverage and weak forward estimates. Intrinio is sales-quoted (Datarade lists "$250 / month to $2,400 / year" range), with international data routed through partner network — overpriced for retail.
- **For news quality specifically: Benzinga (sales-quoted, expensive) or Tiingo News are the strongest stand-alone sources.** EODHD news and FMP news are usable but US-skewed. RavenPack/Refinitiv are enterprise-only and out of scope.
- **yfinance should NOT be the production data source for ANR work.** The Oregon State University Situation Report (March 2025) documents verbatim: "Two weeks ago (the week of 2/17/2025), Yahoo Finance changed their API which broke most versions of yfinance. For most stock tickers, yfinance would return zero results with an error message stating that it was possible the stock had been delisted." Use it only for prototyping or as a cross-check.
- **Hybrid is usually the right answer:** one provider for prices/corporate actions (EODHD or Tiingo), one for forward estimates (FMP, Finnhub Estimates, or Intrinio), one for news (Tiingo or Benzinga). Single-provider stacks at this budget always have a weak leg.
- **Forward-estimate quality below $150/mo is mediocre across the board.** True institutional estimates (Refinitiv I/B/E/S, FactSet, Visible Alpha) are inaccessible to retail. Finnhub, FMP, and Intrinio (Zacks-sourced) are the realistic options; expect estimate counts in the low double digits, not 20+ analysts per name, especially in Europe.
- **ANR (abnormal return) suitability:** Any provider with clean adjusted OHLCV + index/ETF history + reliable timestamps works. EODHD, Tiingo, Polygon, FMP all do this well. yfinance does too — until it breaks. The real ANR weakness in retail APIs is **event-time precision for news**, which only Benzinga, Tiingo News, and (to a lesser extent) FMP/Finnhub provide with sub-minute timestamps.
- **Marketstack and Alpha Vantage are not recommended.** Marketstack documents in its own V2 docs: "Marketstack provides derived data that calculates a real-time reference price for each asset. While this is not a substitute for the TOPS Feed, we believe it will fulfill the needs of 95% of our customer base. We're doing so because as of February 1st, 2025, the IEX Exchange has changed its market data policies." Translation: "intraday" is a synthetic reference price, not actual intraday data. Alpha Vantage's own Premium page documents "our standard API usage limit (25 API requests per day)" with an additional 5 requests/minute cap — too restrictive for any serious workflow.

---

## PART 2: WHAT THE USER ACTUALLY NEEDS

Translating the four priorities into concrete data requirements:

### 2.1 Forward / expected data (Priority 1) — MUST HAVE
- **Mandatory:** Consensus EPS and revenue estimates (next quarter, current year, next year); number of estimating analysts; mean/median/high/low; target price consensus; individual upgrades/downgrades with date and firm; earnings calendar with confirmed/estimated date and BMO/AMC timing; earnings surprises history.
- **Nice-to-have:** EPS revision history (count of raised vs. lowered in last 4 weeks), guidance text, long-term growth estimates, conference call transcripts.
- **Europe-specific challenge:** Most retail APIs source US estimates from Zacks; European estimate coverage (DAX, CAC, AEX, FTSE 100 names) is materially thinner. Confirm before committing.

### 2.2 ANR / abnormal return analysis (Priority 2) — MUST HAVE
- **Mandatory:** Daily OHLCV adjusted for splits AND dividends; benchmark index history (S&P 500, STOXX 600, DAX, CAC 40, FTSE 100); sector ETF history for sector-adjustment; ex-dividend dates and split factors as separate fields (not just adjusted prices); peer-group lookup or industry classification; consistent timestamps in a known timezone with explicit UTC offset; FX series for EUR/USD, GBP/USD, etc.
- **Nice-to-have:** Intraday bars for narrow event windows (1-minute around 8:00 CET earnings releases for European names; 1-minute around 16:00 ET for US); free-float-adjusted market cap history.
- **For Europe ANR specifically:** XETRA, Euronext (Paris/Amsterdam/Brussels/Milan), LSE, SIX, OMX Nordic coverage is mandatory.

### 2.3 News quality (Priority 3) — MUST HAVE
- **Mandatory:** Per-article ticker tagging; timestamp with second precision; source URL; headline + body; ability to query historical archive (>= 2 years back).
- **Nice-to-have:** Significance / sentiment / category metadata; press release vs. analyst comment vs. editorial distinction; European-language coverage (German, French); first-publication-time vs. index-time distinction (critical for ANR).
- **Europe-specific challenge:** Most retail finance APIs are heavily skewed to US/English-language outlets. Real European IR/regulatory news (e.g., AMF Paris filings, BaFin disclosures, RNS announcements on LSE) is rarely first-class.

### 2.4 Cost sensibility (Priority 4)
- Realistic retail budget bands: **near-free**, **~$30/mo**, **~$60–100/mo**, **~$150–200/mo**, and **$200+/mo**.
- Beware enterprise-only features hidden behind sales quotes (Intrinio, Refinitiv, Bloomberg, RavenPack).

---

## PART 3: PROVIDER COMPARISON TABLE

| Provider | Price (2024–2025) | Europe coverage | Historical prices | News | Forward estimates | ANR suitability | Better than yfinance? | Strengths | Weaknesses | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| **yfinance (baseline)** | Free | Medium (Yahoo tickers for LSE/XETRA/Euronext work but inconsistently) | Medium — adjusted OHLCV, but scraped and fragile; Yahoo broke yfinance for "most stock tickers" the week of 2/17/2025 (Oregon State Univ. Situation Report) | Weak — headlines only, no archive, no timestamps with precision | Weak — point-in-time consensus only via `.info`, no revisions, no upgrades/downgrades feed | Medium — fine for backtests if it works; unreliable for production | Baseline | Free, pandas-native, huge community | Unofficial scraper, breaks unpredictably, no SLA, no support, rate-limited / blacklisted on heavy use | Keep ONLY as prototyping aid |
| **Finnhub** | Free (60 calls/min, US only). Paid tiers are **modular**: ~$50/mo per international market for prices; ~$50/mo Fundamentals; ~$75/mo Estimates; $3,500/mo All-In-One enterprise | Strong on paper (LSE, XETRA, Euronext, TSX, ASX) but requires per-market paid add-ons; intl quotes 15-min delayed | Medium — global OHLCV on paid; intraday tick on premium | Strong free company news (US only on free; international on paid); but a verified 1-star SourceForge review from user "Jeroen" reports: "The WebSocket news feed delivers thousands of items — but they are all old (some months or even years old). During US market hours, I receive zero newly published news items." | Strong globally on Estimates plan (~$75/mo): EPS/revenue estimates, surprises history "going back to 2000" (per Finnhub docs), price targets, upgrades/downgrades | Medium — clean candles, but timestamp granularity varies by exchange | Yes, on paid | Best free tier in the industry; broadest global estimate coverage at retail prices; great Python SDK | Modular pricing inflates real cost; international real-time costs ~$50/mo per market; support reports mixed | Strong for budget global estimates; pricier than it looks if you stack markets |
| **Financial Modeling Prep (FMP)** | Free Basic (250 calls/day, US, 5 yrs). **Starter $22/mo, Premium $59/mo, Ultimate $149/mo** — all billed annually per the live FMP pricing page; monthly billing is higher | **Starter = US only; Premium = US + UK + Canada; Ultimate = "Full Global Coverage" including Euronext/XETRA**. European fundamentals are sourced and standardized but quality varies | Strong — "Up to 30 Years of Historical Data" on Premium; 1-min intraday charting on Ultimate; split/div adjusted | Medium — Stock News + Press Releases endpoints; mostly US-skewed | Strong — Financial Estimates, Price Target Consensus, Stock Grades (upgrades/downgrades), Earnings Calendar with EPS estimates/actuals starting at Premium tier | Strong — clean adjusted prices, index data, ETF holdings, peer lookup endpoint | Yes, materially | One-stop shop; only sub-$200/mo retail API combining estimates + news + Europe; transcripts on Ultimate | Estimate analyst counts thinner than institutional; 30-day bandwidth caps (50 GB Premium, 150 GB Ultimate, per footnote on FMP pricing page) | **Top pick for this user** |
| **Tiingo** | Free Starter (1,000 req/day, 500 unique symbols/mo). **Power $30/mo (or $300/yr); Business $50/mo for commercial license** | Weak — Tiingo's own product page lists "32,000 U.S. and Chinese equities" only. No European exchanges in the EOD product | Strong for US — proprietary error-checked EOD back to 1962; dividends/splits; 50+ yrs depth | Strong — per Tiingo's own product description: "Tiingo's News Feed covers 20 million articles and over two decades worth of news... our feed focuses on unstructured information sources, such as art blogs, tech publications, niche farming publications, healthcare trade magazines, and other trade-specific content." Ticker + topic + slang tagging | Weak — no consensus estimates, no upgrades/downgrades feed; basic fundamentals only (in beta) | Strong (US only) — best-in-class adjusted prices and corporate actions | Yes, for US workflow; **No** for Europe | Cleanest US EOD prices; best news tagging at retail prices; very cheap | No European equities, no consensus estimates, fundamentals API still maturing | Best as the **prices+news leg** of a US-only stack |
| **Alpha Vantage** | Free 25 req/day (5/min) per Alpha Vantage's own Premium page. Paid $49.99 / $99.99 / $149.99 / $199.99 / $249.99 per month | Medium — claims 20+ exchanges incl. London/Frankfurt, but coverage and currency adjustments are uneven | Medium — adjusted OHLCV, but `outputsize=full` (full history) is premium-only; free users capped at 100 data points per request | Weak — sentiment endpoint exists but limited archive | Medium — earnings actual vs. estimate, no rich revisions feed | Medium — OK for ANR if you stay on liquid US names | Marginal; only really better than yfinance for compliance (NASDAQ-licensed) | NASDAQ-licensed real-time at retail prices; Excel/Sheets add-ins; MCP server | Free tier too restrictive to be useful (25/day); no rich estimates revisions; thin European fundamentals | Skip unless you specifically need a NASDAQ-licensed feed |
| **Polygon.io** | Stocks: Free (5 calls/min, EOD only). Starter $29/mo, Developer $79/mo, Advanced $199/mo | **None — US equities only** (NYSE/NASDAQ/AMEX/OTC). No LSE/XETRA/Euronext at any tier | Strong — best-in-class for US: tick data 20+ yrs, ~20ms mean latency claim, unlimited calls on premium | Medium — News endpoint included, Benzinga partnership available, archive less deep than Tiingo | Weak — financials and some analyst data on Advanced, but no rich consensus revisions feed | Strong (US only) — gold standard for US event-window analysis | Yes for US ANR | Real-time WebSocket; unlimited calls; clean docs | US-only; weak estimates; $199/mo to be useful | Use only as US intraday leg of a stack; not a fit for Europe-heavy user |
| **EOD Historical Data (EODHD)** | Free (20 calls/day). EOD All-World €19.99/mo. EOD+Intraday Extended €29.99/mo. **Fundamentals Data Feed €59.99/mo**. **All-In-One €99.99/mo** | **Strong — 60+ exchanges via direct contracts: Nasdaq Cloud API for US, LSE for UK, Cboe for European markets** including Amsterdam, Brussels, Frankfurt, Lisbon, Madrid, Paris, plus India and China | Strong — 30+ yrs major US markets, 15–20 yrs most European/Asian. OHLCV adjusted for splits/dividends. 1-min and 5-min intraday | Medium — Stock Market Financial News API included in upper plans; US-skewed but covers global tickers via tagging | Medium-Strong — Earnings Trend API (quarterly + annual estimates, post-v1.1 fix for missing Q4), Calendar API for earnings/IPOs/splits, analyst ratings on Fundamentals tier | Strong — splits/dividends API, index constituents history, market cap history, FX, technical indicators | Yes, materially for Europe | Best Europe coverage at retail prices; bulk downloads; Excel/Sheets/Power BI add-ins | Estimate coverage less detailed than FMP/Finnhub; news depth weaker | **Top pick for Europe-heavy budgets** |
| **Twelve Data** | Free Basic (8 calls/min, 800/day). Grow $29/mo, Pro $99/mo, Pro 610 $149/mo, Ultra $329/mo | Strong on paper — 70+ exchanges; Twelve Data's site explicitly advertises "Now Live: Cboe Europe real-time data for all major European stocks." International data progressively unlocked across Grow → Pro → Ultra | Strong — 100+ exchanges, 1M+ symbols, ISIN/FIGI/CUSIP support on Ultra | Weak — limited news endpoint, no rich archive | Medium — EPS estimates, sales estimates, growth estimates, analyst ratings (US complete, intl lightweight) | Strong for prices/FX; clean structured API | Yes for Europe equities | Modern API; good docs; explicit Cboe Europe coverage | Credits-based pricing confusing; news weak; full estimates need Pro+ | Strong for EU prices; pair with another provider for news |
| **Intrinio** | Sales-quoted; Datarade publishes "Intrinio's APIs and datasets range in cost from $250 / month to $2,400 / year." Retail entry typically ~$250/mo | Weak — primary product is US; international data via partner network (added cost) | Strong for US — historical prices "back to 1996 or the IPO date, with some companies with data back to the 1970s" per Intrinio docs | Medium — news included but US-focus | Strong (US) — Zacks-sourced EPS/sales estimates with mean/median/high/low/raised/lowered, target prices, 20+ years history | Strong (US) | Yes, but expensive | Institutional-grade Zacks estimates; clean licensing | Sales gating, expensive, European data is third-party passthrough | Overkill for retail unless you need Zacks specifically |
| **Marketstack** | Free 100 req/mo. **Basic $9.99/mo (10k); Professional $49.99/mo (100k); Business $149.99/mo (500k)** | Medium — claims 70 exchanges incl. LSE, WSE, Australian; US data sourced from Tiingo per Marketstack FAQ | Medium — EOD with split/div adjustment fields. **"Intraday" is a synthetic derived reference price**, per Marketstack's own V2 docs (verbatim): "Marketstack provides derived data that calculates a real-time reference price for each asset. While this is not a substitute for the TOPS Feed... as of February 1st, 2025, the IEX Exchange has changed its market data policies." | None — no news API | Analyst ratings only on Business tier ($150/mo); 1 call/min rate limit on the endpoint, 15+ years history | Medium — OK for EOD; not real intraday | Marginal — Tiingo via middleman | Cheap headline price | "Intraday" misleading; no news; estimates locked to expensive tier | Skip — Tiingo direct is better |
| **LSEG/Refinitiv, Bloomberg, FactSet, Visible Alpha, RavenPack** | Enterprise only, typically $20k+/yr per seat | Best in industry | Best in industry | Best in industry | Best in industry (I/B/E/S, FactSet Estimates) | Best | N/A — out of reach | Gold standard | Sales-only, redistribution restrictions | Not realistic for retail |
| **Benzinga** | Sales-quoted; retail-accessible via Massive (formerly Polygon partner) or direct. Often quoted $150–$400/mo+ for full API | Weak — US-centric | N/A — news/ratings/calendar specialist | **Strong — purpose-built newswire with significance score (0–5 scale per their API schema), real-time analyst ratings, "Why Is It Moving" feed, press releases, FDA calendar, conference calls, live transcripts** | Strong — real-time analyst ratings WebSocket with firm, price target, action_pt, action_company, accuracy scores | N/A | Yes for news specifically | Best retail-accessible news/ratings API for US; explicit significance metadata for ranking | US-only; expensive; sales gating | Best US news+ratings add-on if budget allows |
| **Nasdaq Data Link (Quandl)** | Free + paid datasets (Sharadar core US Fundamentals & Prices ~$150/mo retail) | Weak | Strong (Sharadar SEP for US, 20+ yrs) | None | Sharadar estimates not core; can subscribe to Zacks via the platform | Strong (US) | Yes for US fundamentals | Clean academic-grade US data; marketplace model | Marketplace fragmentation; mostly US | Niche; skip unless you want a specific Sharadar table |
| **newsapi.ai / GDELT** | Various; ~$20–$200/mo | Strong (multilingual) | N/A | Strong on breadth, weak on financial-specific tagging | None | N/A | Yes for news breadth | Multilingual European coverage | No financial tagging; you must build entity resolution | Optional add-on if Europe news matters more than estimates |

Legend: Strong / Medium / Weak / Unclear.

---

## PART 4: BEST SOLUTIONS ORDERED BY PRICE

### Tier 1 — Near-Free / Ultra Budget (≤ $10/mo)

**Recommended stack:** Finnhub Free + EODHD Free + yfinance (cross-check only).

- **Cost:** $0/mo.
- **What you gain over yfinance alone:** A licensed source (Finnhub) for US real-time + sample analyst estimates + earnings calendar; EODHD free tier (20 calls/day) for sanity-checking European tickers in proper exchange codes (e.g., `BMW.XETRA`).
- **What's still missing:** Reliable Europe equities (rate-limited free tiers), full estimate history, news archive, intraday for Europe.
- **Good enough for:** Europe — no; Forward expectations — partial (US only, sample); ANR — yes for US; News impact — no.

### Tier 2 — Low-Cost (~$20–50/mo)

**Recommended stack A (Europe-first):** **EODHD EOD+Intraday All-World Extended (€29.99/mo)** + yfinance for `.info`-style metadata.

- **Cost:** ~$32/mo USD equivalent.
- **What you gain over yfinance:** Properly licensed European exchange data (LSE, XETRA, Euronext incl. Amsterdam/Paris/Brussels/Lisbon/Madrid/Milan, OMX Nordic, SIX), 30+ yrs US history, intraday bars, working corporate actions, deterministic API.
- **What's still missing:** Real analyst estimates (this tier is prices + intraday, not Fundamentals), real news API.
- **Good enough for:** Europe — yes; Forward expectations — no; ANR — yes; News — no.

**Recommended stack B (US-first):** **Tiingo Power ($30/mo)**.

- **Cost:** $30/mo.
- **What you gain over yfinance:** Cleanest US adjusted-price series available at retail, real news API with 20+ years of history and ticker tagging ("20 million articles and over two decades worth of news" per Tiingo's own description), dividends/splits as separate well-formed events, 100k req/day.
- **What's still missing:** Any European equities, any analyst estimates.
- **Good enough for:** Europe — no; Forward expectations — no; ANR — yes (US); News — yes (US).

**Recommended stack C (Forward-data-first):** **FMP Starter ($22/mo annual) + yfinance**.

- **Cost:** $22/mo.
- **What you gain:** US-only but with full Stock Grades, Price Targets, and the Earnings Calendar — i.e., the first taste of real forward data. Sample analyst estimates included.
- **What's still missing:** Europe coverage entirely (Starter is US-only), full 30-yr history (5 yrs at this tier), transcripts.
- **Good enough for:** Europe — no; Forward — partial; ANR — yes (US); News — partial.

### Tier 3 — Mid-Budget (~$50–200/mo) ★ RECOMMENDED ZONE

**Recommended stack A (best value, single provider):** **FMP Premium ($59/mo annual / higher monthly)**.

- **Cost:** $59–69/mo.
- **What you gain over yfinance:** Full 30-yr fundamentals + financial estimates + price targets + stock grades (upgrades/downgrades) + earnings calendar with EPS estimates/actuals + intraday charts + news + press releases. UK and Canada exchanges enabled (LSE counts here for the European leg). Bandwidth cap 50 GB/30d.
- **What's still missing:** Continental European exchanges (Euronext/XETRA) — those are Ultimate-only. Earnings call transcripts also Ultimate-only.
- **Good enough for:** Europe — partial (UK yes, EUR-zone no); Forward — yes; ANR — yes; News — yes (US-skewed).

**Recommended stack B (Europe-complete, single provider):** **EODHD All-In-One (€99.99/mo)**.

- **Cost:** ~$108/mo.
- **What you gain:** Bundles historical + intraday + real-time delayed + fundamentals + news + calendar + technical indicators + screener across 60+ global exchanges including all major European venues. Earnings Trend API includes quarterly + annual estimates.
- **What's still missing:** Estimate detail is shallower than FMP/Finnhub (fewer fields, smaller analyst panels); transcripts not equivalent.
- **Good enough for:** Europe — yes; Forward — partial; ANR — yes; News — yes.

**Recommended stack C (best forward-estimate + Europe combo):** **FMP Premium ($59/mo) + EODHD EOD All-World (€19.99/mo)**.

- **Cost:** ~$80/mo.
- **What you gain:** FMP handles all the forward-looking data (US-heavy estimates panel is fine), EODHD handles Continental European prices, corporate actions, and intraday. You can map tickers via ISIN.
- **What's still missing:** European analyst estimates remain thin (industry-wide retail limitation). News still US-skewed unless you add a third leg.
- **Good enough for:** Europe — yes (prices) / partial (estimates); Forward — yes; ANR — yes; News — partial.

**Recommended stack D (news-heavy):** **Tiingo Power ($30/mo) + Finnhub Estimates (~$75/mo)**.

- **Cost:** ~$105/mo.
- **What you gain:** Tiingo's superior news + clean US prices; Finnhub gives global estimates including European EPS/revenue revisions and upgrades/downgrades.
- **What's still missing:** European prices unless you add Finnhub's per-market plan (~$50/mo per market) or EODHD.
- **Good enough for:** Europe — partial (estimates yes, prices no without a third leg); Forward — yes; ANR — yes (US); News — yes.

### Tier 4 — Premium ($200+/mo)

**Recommended stack A (single provider):** **FMP Ultimate ($149/mo annual)**.

- **Cost:** $149/mo (annual) or higher monthly.
- **What you gain over yfinance and over Tier 3:** Global Coverage flag turns on (Euronext, XETRA, SIX, Nordics, APAC). Earnings Call Transcripts. 1-min intraday charting. Full historical access on all endpoints. 13F holdings. ETF holdings. 3,000 API calls/min, 150 GB bandwidth.
- **What's still missing:** Native European-language press releases; institutional-grade estimate panels; truly tick-level US data (use Polygon if needed).
- **Good enough for:** Europe — yes; Forward — yes; ANR — yes; News — yes (US-skewed). **This is the single recommendation for the user.**

**Recommended stack B (premium hybrid):** **FMP Ultimate ($149) + Polygon Stocks Starter ($29) or Developer ($79)** + Benzinga news add-on if budget allows.

- **Cost:** $180–$300+/mo depending on Benzinga quote.
- **What you gain:** Tick-precision US intraday for event-window ANR; Benzinga's significance-scored news with sub-second timestamps for US event timing.
- **What's still missing:** Native European real-time tick data (still retail-out-of-reach).
- **Good enough for:** Everything within the retail envelope.

**Recommended stack C (full retail max):** **FMP Ultimate ($149) + EODHD All-In-One (€99.99) + Benzinga news**.

- **Cost:** ~$350+/mo.
- **What you gain:** Best European exchange breadth (EODHD's Cboe-sourced Euronext/XETRA via direct contracts) + best forward-estimate coverage at retail (FMP) + best US news/ratings (Benzinga). Three independent sources allow cross-validation, which is genuinely useful for ANR.
- **Good enough for:** Everything within retail.

---

## PART 5: FINAL RECOMMENDATION

### Single best recommendation for today

**Adopt FMP Ultimate ($149/mo annual billing, $1,788/yr) as the primary data spine, and keep yfinance as a free fallback for ad-hoc Yahoo metadata queries.**

Rationale:
1. It is the only sub-$200/mo retail API that simultaneously satisfies (a) forward estimates with revisions/grades/targets, (b) ANR-grade adjusted prices for US + Europe + Asia, (c) corporate actions, (d) earnings calendar with timing, and (e) a usable news + press-release feed.
2. European stock coverage is real ("Global Coverage" flag on Ultimate per the FMP pricing page), not promised-but-limited as on Premium ($59).
3. The bandwidth cap (150 GB/30d) is generous enough that batch ANR studies on a few hundred names are feasible.
4. The Python integration is documented, stable, and JSON-native.

### Cheapest serious recommendation

**EODHD EOD+Intraday All-World Extended (€29.99/mo) + Tiingo Power ($30/mo).**

Total ≈ $63/mo. EODHD covers European prices, intraday, splits, dividends, FX, and indices; Tiingo provides clean US prices, dividends/splits, and the news API. This stack improves on yfinance on every axis except forward estimates, which neither provider does well. Add `yfinance` for free consensus snapshot as a cross-check.

If forward estimates are truly the #1 priority, then **FMP Premium ($59/mo)** alone is the cheapest "serious" option, accepting that Continental European exchanges are limited to UK-listed names.

### Upgrade path

1. **Start:** EODHD Fundamentals Data Feed (€59.99/mo) — gets you all-asset Europe prices + intraday + Earnings Trend estimates + Calendar + global fundamentals.
2. **Add (~$60/mo more):** Layer FMP Premium ($59/mo) on top to deepen the analyst estimates, price targets, upgrades/downgrades. Total ~$125/mo.
3. **Upgrade:** Replace FMP Premium with FMP Ultimate ($149/mo) once you want transcripts and the rest of global coverage. Drop EODHD if you don't need bulk EU intraday. Total ~$150/mo.
4. **Top of retail:** Add Benzinga news (sales-quoted) for sub-second US event timestamps and significance scoring. Total ~$250–$400/mo.
5. **Stop here.** Institutional providers (Refinitiv, FactSet) are not 5–10× better for a retail workflow; they are 50–100× more expensive.

---

## PART 6: RISKS AND CAVEATS

### Europe-specific data gaps
- **Continental European exchanges (Euronext, XETRA, SIX, OMX) are routinely either gated to top tiers (FMP Ultimate, Twelve Data Pro+, Finnhub per-market add-ons) or sourced via secondary feeds (EODHD via Cboe Europe contracts, Marketstack via Tiingo).** Always verify the specific tickers you need before committing.
- **Real-time European data is rare and expensive.** EODHD calls its European feeds "delayed (15–20 min)" outside of paid add-ons. Twelve Data advertises "Now Live: Cboe Europe real-time data for all major European stocks" as a recent rollout but it requires upper tiers.
- **European analyst estimate coverage is the weakest leg of every retail API.** Even FMP Ultimate and Finnhub Estimates carry fewer analysts per European name than per US name. For DAX/CAC blue chips this is workable; for small caps it's often empty.
- **Press releases / IR feeds in native European languages** (BaFin, AMF, RNS) are not first-class in any of these APIs. If multilingual press releases matter, you'll need a separate news provider (newsapi.ai, GDELT) and build entity resolution.

### News quality limitations
- **"News API" means very different things across providers.** Tiingo and Benzinga are purpose-built newswires. EODHD and FMP news are useful but aggregator-style. Finnhub's WebSocket news has documented reliability issues — a verified SourceForge review by user "Jeroen" states verbatim: "The WebSocket news feed delivers thousands of items — but they are all old (some months or even years old). During US market hours, I receive zero newly published news items." This was on a paid Fundamentals-1 + US Market Data subscription.
- **Timestamps are not equivalent.** For ANR you need first-publication timestamps; some APIs return index-time. Benzinga is explicit about this; most others are not.
- **Archive depth varies wildly.** Tiingo's own description claims "20 million articles and over two decades worth of news"; FMP and Finnhub free tiers limit history; EODHD news is shallow on the cheap tiers.

### Forward-estimate caveats
- **Retail estimate panels are not I/B/E/S.** Expect 3–15 analysts per US name and 1–8 per European name, vs. 20+ from Refinitiv.
- **Revisions data is the weakest field.** Finnhub Estimates and FMP Premium expose revision counts; most others give snapshots only. If your ANR study uses estimate-revision events, validate carefully.
- **Earnings-calendar timing fields** (BMO/AMC, confirmed vs. tentative) are present in FMP, Finnhub, Benzinga; thin or missing elsewhere.

### Premium features hidden behind higher plans
- **FMP:** Europe (XETRA/Euronext) and earnings transcripts are Ultimate-only. Don't buy Premium expecting full global coverage.
- **Finnhub:** Each international market is a separate ~$50/mo add-on. Stacking US Market Data + Fundamentals + Estimates + 2–3 international markets quickly exceeds $250/mo.
- **Alpha Vantage:** `outputsize=full` (full history) is **premium-only**; Alpha Vantage's own page documents the free limit as "25 API requests per day" with 5/min throughput.
- **Marketstack:** "Intraday" below 15 minutes requires Professional; analyst ratings require Business ($149.99/mo) and are explicitly rate-limited to 1 call/min. Their "real-time" intraday is a synthetic derived reference price, not a TOPS feed.
- **Twelve Data:** International coverage progresses across Grow → Pro → Ultra. Mutual funds, advanced fundamentals, and full WebSocket require Pro+ ($99/mo).
- **Polygon:** No European stocks at any price; that's a hard ceiling.
- **Intrinio:** "International market data, ETF data, mutual funds, and analyst estimates" are explicitly described as routed through their **partner network** — meaning extra licensing and not native Intrinio quality.

### Unclear licensing
- **yfinance redistribution is technically prohibited by Yahoo TOS.** Fine for personal research; not fine for client deliverables or commercial products.
- **Tiingo, EODHD, Marketstack, FMP** all distinguish personal vs. commercial licenses; the cheap published prices are personal-use only. If the user ever publishes a research letter or sells signals, prices double or triple.
- **Polygon and Finnhub** display "for personal use" caveats on the cheap tiers.
- **NASDAQ/NYSE-licensed real-time data** has separate exchange fees layered on top of most providers. Alpha Vantage is one of the few that bundles a NASDAQ license at retail prices.

### Weak transcript / call-transcript coverage
- Earnings call transcripts at retail prices are essentially **FMP Ultimate** or nothing. Finnhub has them since 2000 on premium. Benzinga streams them live as a separate product.
- European call transcripts are unreliable across all retail providers.

### Overpaying traps
- **Buying Polygon Advanced ($199) for a Europe-heavy workflow** — Polygon has no European stocks. Common mistake.
- **Buying Finnhub Free thinking you'll get global estimates** — international and Estimates are paid add-ons.
- **Buying Marketstack expecting real-time intraday** — per their own docs, you get a derived reference price, not real intraday, after the IEX TOPS policy change of Feb 1, 2025.
- **Buying Intrinio for international coverage** — it's a partner-network passthrough; you're paying retail markup for data you could get directly.

### Reliability caveats on yfinance specifically
- Yahoo changed its API around 17 Feb 2025 and broke most yfinance versions. Oregon State University's Situation Report (March 2025) documents verbatim: "Two weeks ago (the week of 2/17/2025), Yahoo Finance changed their API which broke most versions of yfinance. For most stock tickers, yfinance would return zero results with an error message stating that it was possible the stock had been delisted."
- Maintainer commentary on the project's own GitHub discussions acknowledges yfinance is a scraper "at the mercy of Yahoo not changing the layout." Recent versions ship workarounds but each is a stopgap.
- For an ANR study that requires reproducibility, yfinance is unsuitable as the primary source. **It can stay in the workflow only as a free fallback or sanity check.**

---

**Bottom line:** Move off yfinance as the primary data source. If you can spend $59/mo, FMP Premium is the single best upgrade. If you can spend $149/mo, FMP Ultimate is the single best fit for this exact workflow. If you must stay below $50/mo, layer EODHD All-World (€19.99) + Tiingo Power ($30) and accept that forward estimates will be the weak leg until you upgrade.