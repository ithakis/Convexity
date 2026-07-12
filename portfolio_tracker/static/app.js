"use strict";

/* ===========================================================================
 * Column definitions
 * --------------------------------------------------------------------------- */
const COLS = [
  { key: "logo",        label: "",          w: 22,  align: "center", sortable: false,
    render: (r) => logoImg(r.symbol) },
  { key: "symbol",      label: "Ticker",    w: 58,  align: "left", sortable: true,
    render: (r) => `<span>${r.symbol}</span>`, td_cls: "sym left" },
  { key: "name",        label: "Company",   w: 148, align: "left", sortable: true,
    render: (r) => escapeHtml(r.name || ""), td_cls: "name left" },
  { key: "price",       label: "Price",     w: 90,  align: "right", sortable: true,
    render: (r) => fmtMoney(r.price, r.currency) },
  { key: "market_cap",  label: "Market Cap",w: 86,  align: "right", sortable: true,
    render: (r) => fmtCompactMoney(r.market_cap, r.currency) },
  /* P/S: dynamic blue ramp — cheapest P/S currently on screen is most blue,
     priciest is neutral. n/a renders with no background. */
  { key: "ps_ratio",    label: "P/S",       w: 56,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "low" },
    render: (r) => fmt2(r.ps_ratio) },
  /* P/E: dynamic blue ramp — cheapest P/E currently on screen is most blue. */
  { key: "pe_ratio",    label: "P/E",       w: 56,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "low" },
    render: (r) => fmt2(r.pe_ratio) },
  { key: "pct_ytd",     label: "% YTD",     w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 100 },
    render: (r) => fmtPctSigned(r.pct_ytd) },
  { key: "spark",       label: "Chart 1Y",  w: 100, align: "center", sortable: false,
    bg: (r) => sparkBg(r.pct_1y),
    render: (r) => sparkSvg(r.sparkline, r.pct_1y) },
  { key: "pct_1y",      label: "% 1Y",      w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 100 },
    render: (r) => fmtPctSigned(r.pct_1y) },
  /* Δ Highs: percent below the fast-path 2Y high. 0% = at high, -50% = halved.
     Anchor full-red at -50%. */
  { key: "delta_ath",   label: "Δ Highs",   w: 110, align: "right", sortable: true,
    render: (r) => deltaBar(r.delta_ath) },
  { key: "rs_rank",     label: "RS Rank 1M",w: 92,  align: "center", sortable: false,
    render: (r) => rsBars(r.rs_rank) },
  { key: "earnings_surprise", label: "EPS Surp.", w: 80, align: "center", sortable: true,
    sortValue: (r) => {
      const arr = r.earnings_surprise;
      if (!arr || !arr.length) return null;
      const vals = arr.slice(0,8).map(x => x.surprise_pct).filter(x => x != null);
      return vals.length ? vals.reduce((a,b)=>a+b,0)/vals.length : null;
    },
    render: (r) => epsSurpriseBars(r.earnings_surprise) },
  { key: "above_sma_20",  label: "20MA",    w: 38,  align: "center", sortable: true,
    render: (r) => triangle(r.above_sma_20),
    sortValue: (r) => r.above_sma_20 === null ? null : (r.above_sma_20 ? 1 : 0) },
  { key: "above_sma_50",  label: "50MA",    w: 38,  align: "center", sortable: true,
    render: (r) => triangle(r.above_sma_50),
    sortValue: (r) => r.above_sma_50 === null ? null : (r.above_sma_50 ? 1 : 0) },
  { key: "above_sma_200", label: "200MA",   w: 40,  align: "center", sortable: true,
    render: (r) => triangle(r.above_sma_200),
    sortValue: (r) => r.above_sma_200 === null ? null : (r.above_sma_200 ? 1 : 0) },

  /* Pass D — optional columns (not in Default preset; surfaced via Fundamentals,
     Momentum, or the custom-column picker). */
  { key: "pct_1w",        label: "% 1W",    w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 20 },
    render: (r) => fmtPctSigned(r.pct_1w) },
  { key: "pct_1m",        label: "% 1M",    w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 30 },
    render: (r) => fmtPctSigned(r.pct_1m) },
  { key: "pct_3m",        label: "% 3M",    w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 50 },
    render: (r) => fmtPctSigned(r.pct_3m) },
  { key: "pct_6m",        label: "% 6M",    w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 75 },
    render: (r) => fmtPctSigned(r.pct_6m) },
  { key: "rsi_14",        label: "RSI 14",  w: 64,  align: "right", sortable: true,
    render: (r) => fmt2(r.rsi_14) },
  { key: "macd_hist_pct", label: "MACD %",  w: 72,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 2 },
    render: (r) => fmtPctSigned(r.macd_hist_pct) },
  { key: "bb_pct_b",      label: "%B",      w: 54,  align: "right", sortable: true,
    render: (r) => fmt2(r.bb_pct_b) },
  { key: "beta",          label: "Beta",    w: 58,  align: "right", sortable: true,
    render: (r) => fmt2(r.beta) },
  { key: "sector",        label: "Sector",  w: 130, align: "left",  sortable: true,
    render: (r) => escapeHtml(r.sector || ""), td_cls: "left" },
  { key: "industry",      label: "Industry",w: 160, align: "left",  sortable: true,
    render: (r) => escapeHtml(r.industry || ""), td_cls: "left" },
  /* Fwd P/E, PEG, EV/Rev, EV/EBITDA, D/E: dynamic blue ramp, favor low (cheaper /
     less levered currently on screen = most blue). Op Mgn, Curr Ratio, Div Yield:
     favor high (more profitable / more liquid / more income = most blue). n/a
     always renders with no background. */
  { key: "forward_pe",    label: "Fwd P/E", w: 64,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "low" },
    render: (r) => fmt2(r.forward_pe) },
  { key: "peg",           label: "PEG",     w: 58,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "low" },
    render: (r) => fmt2(r.peg) },
  { key: "ev_revenue",    label: "EV/Rev",  w: 68,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "low" },
    render: (r) => fmt2(r.ev_revenue) },
  { key: "ev_ebitda",     label: "EV/EBITDA", w: 78, align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "low" },
    render: (r) => fmt2(r.ev_ebitda) },
  { key: "operating_margin", label: "Op Mgn", w: 68, align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "high" },
    render: (r) => fmtPctDirect(r.operating_margin) },
  { key: "debt_equity",   label: "D/E",     w: 56,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "low" },
    render: (r) => fmt2(r.debt_equity) },
  { key: "current_ratio", label: "Curr Ratio", w: 78, align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "high" },
    render: (r) => fmt2(r.current_ratio) },
  { key: "dividend_yield", label: "Div Yield", w: 78, align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "high" },
    render: (r) => fmtPctDirect(r.dividend_yield) },
  /* Extended fundamentals — same yo_dyn ramp family as the block above, so
     the per-view Off / Percentile / Min-Max background modes apply. Favor
     "low" for price multiples and payout (cheaper / more sustainable = blue),
     "high" for returns, margins, growth, yield and liquidity. */
  { key: "price_book",     label: "P/B",       w: 56,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "low" },
    render: (r) => fmt2(r.price_book) },
  { key: "roe",            label: "ROE",       w: 64,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "high" },
    render: (r) => fmtPctDirect(r.roe) },
  { key: "roa",            label: "ROA",       w: 64,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "high" },
    render: (r) => fmtPctDirect(r.roa) },
  { key: "gross_margin",   label: "Gross Mgn", w: 74,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "high" },
    render: (r) => fmtPctDirect(r.gross_margin) },
  { key: "profit_margin",  label: "Net Mgn",   w: 68,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "high" },
    render: (r) => fmtPctDirect(r.profit_margin) },
  { key: "fcf_yield",      label: "FCF Yield", w: 74,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "high" },
    render: (r) => fmtPctDirect(r.fcf_yield) },
  { key: "revenue_growth", label: "Rev Grw",   w: 68,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "high" },
    render: (r) => fmtPctDirect(r.revenue_growth) },
  { key: "earnings_growth", label: "EPS Grw",  w: 68,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "high" },
    render: (r) => fmtPctDirect(r.earnings_growth) },
  { key: "quick_ratio",    label: "Quick Ratio", w: 82, align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "high" },
    render: (r) => fmt2(r.quick_ratio) },
  { key: "payout_ratio",   label: "Payout %",  w: 72,  align: "right", sortable: true,
    heat: { kind: "yo_dyn", favor: "low" },
    render: (r) => fmtPctDirect(r.payout_ratio) },
  /* Analyst recommendation mean: 1 = Strong Buy → 5 = Sell. Lower is
     more bullish, so the ramp paints high ratings (sell side) orange. */
  { key: "analyst_rating",label: "Rating",  w: 64,  align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 1, clipMax: 5, naMax: true },
    render: (r) => fmt2(r.recommendation_mean),
    sortValue: (r) => r.recommendation_mean },
  { key: "rec_trend_fh", label: "Rec Δ6M", w: 72, align: "center", sortable: true,
    sortValue: (r) => recTrendScore(r.rec_trend_fh),
    render: (r) => recTrendCell(r.rec_trend_fh) },
  { key: "insider_mspr", label: "MSPR", w: 62, align: "center", sortable: true,
    sortValue: (r) => r.insider_mspr?.mspr ?? null,
    render: (r) => msprBadge(r.insider_mspr) },
  /* Target upside derived client-side from analyst mean target and last price. */
  { key: "target_upside_pct", label: "Target Δ", w: 86, align: "right", sortable: true,
    heat: { kind: "div", anchor: 30 },
    render: (r) => {
      const t = r.target_mean_price, p = r.price;
      if (t == null || p == null || !isFinite(t) || !isFinite(p) || p <= 0) return fmtPctSigned(null);
      return fmtPctSigned((t / p - 1) * 100);
    },
    sortValue: (r) => {
      const t = r.target_mean_price, p = r.price;
      if (t == null || p == null || !isFinite(t) || !isFinite(p) || p <= 0) return null;
      return (t / p - 1) * 100;
    }
  },
  { key: "w52_high",      label: "52W High",w: 90,  align: "right", sortable: true,
    render: (r) => fmtMoney(r.w52_high, r.currency) },
  { key: "w52_low",       label: "52W Low", w: 90,  align: "right", sortable: true,
    render: (r) => fmtMoney(r.w52_low, r.currency) },
  { key: "news_sentiment", label: "NS", w: 36, align: "center", sortable: true,
    sortValue: (r) => r.news_sentiment?.score ?? null,
    render: (r) => nsDot(r.news_sentiment) },
];

/* Short hover descriptions for the column-header info icons.
   Long-form versions with formulas live in the About / Column Guide modal. */
const COL_INFO = {
  symbol:        "Exchange ticker symbol (e.g. AAPL, NVDA, XLK).",
  logo:          "Brand logo for the company or fund (resolved from Yahoo's website field). Purely visual — no data column behind it.",
  name:          "Full company or fund name from Yahoo Finance.",
  price:         "Last available closing price, converted to the selected display currency (see FX selector in the top bar).",
  market_cap:    "Total market value of all outstanding shares (Price × Shares Outstanding).",
  ps_ratio:      "Price-to-Sales: market cap ÷ trailing-12-month revenue. Lower is generally cheaper.",
  pe_ratio:      "Price-to-Earnings: price ÷ trailing-12-month EPS. Lower is generally cheaper; above 50 implies heavy growth pricing.",
  pct_ytd:       "Return from the first trading day of the current calendar year to today.",
  spark:         "Sparkline of the last 252 trading days. Green if 1Y return is positive, red otherwise.",
  pct_1y:        "Total price return over the last 365 calendar days.",
  delta_ath:     "Distance from the highest close in the table row's 2-year history window. 0% = at that high; full bar = 50% below it.",
  rs_rank:       "Relative Strength: 12 monthly bars showing where each month's close ranked within its trailing-12-month price range.",
  earnings_surprise: "EPS Surprise history: up to 8 quarters, most-recent right. Green bar = beat, red = miss. Height = magnitude (capped ±10%). Source: Yahoo Finance.",
  rec_trend_fh:      "Recommendation Trend Δ6M: change in analyst consensus score over the last 6 months. Score = (2×Strong Buy + Buy − Sell − 2×Strong Sell) / total. Requires FINNHUB_API_KEY.",
  insider_mspr:      "MSPR — Monthly Share Purchase Ratio. Finnhub aggregates Form 4 filings into a single score: +100 = all insiders buying, −100 = all selling. Positive = net insider buying signal. Requires FINNHUB_API_KEY.",
  above_sma_20:  "20-day Simple Moving Average flag. ▲ price above SMA (bullish), ▼ below (bearish). ~1 month of trading days.",
  above_sma_50:  "50-day Simple Moving Average flag. ▲ price above SMA (bullish), ▼ below (bearish). ~1 quarter of trading days.",
  above_sma_200: "200-day Simple Moving Average flag. ▲ price above SMA (bullish), ▼ below (bearish). ~1 year of trading days.",
  pct_1w:        "Total price return over the last 7 calendar days.",
  pct_1m:        "Total price return over the last 30 calendar days.",
  pct_3m:        "Total price return over the last 91 calendar days.",
  pct_6m:        "Total price return over the last 182 calendar days.",
  rsi_14:        "14-day Relative Strength Index. Below 30 is oversold, above 70 is overbought.",
  macd_hist_pct: "MACD histogram as a percent of price. Positive means MACD is above its signal line; negative means momentum is fading.",
  bb_pct_b:      "Bollinger %B. 0 = lower band, 0.5 = middle band, 1 = upper band. Above 1 or below 0 means price is outside the bands.",
  beta:          "Yahoo-reported beta versus the market. Around 1 moves with the market; above 1 is more volatile.",
  sector:        "GICS sector (e.g. Technology, Energy) reported by Yahoo Finance.",
  industry:      "GICS sub-industry — narrower than sector.",
  forward_pe:    "Forward Price/Earnings: price ÷ consensus next-12-month EPS. Lower is generally cheaper.",
  peg:           "PEG ratio: P/E divided by expected earnings growth. Lower can mean cheaper growth, though very low values can also reflect weak forecasts.",
  ev_revenue:    "Enterprise Value ÷ Revenue. Useful when earnings are noisy or negative; lower usually means cheaper on sales.",
  ev_ebitda:     "Enterprise Value ÷ EBITDA. Cap-structure-neutral valuation multiple.",
  operating_margin: "Operating margin as a percent of revenue. Higher means more profit retained after core operating costs.",
  debt_equity:   "Debt-to-equity ratio. Higher means more leverage relative to shareholder equity.",
  current_ratio: "Current assets divided by current liabilities. Above 1 usually signals better short-term liquidity.",
  dividend_yield: "Annual cash dividend divided by price. Higher yields can support total return but may also reflect risk.",
  price_book:    "Price-to-Book: market cap ÷ book value of equity. Below 1 can signal value (or distressed assets); less meaningful for asset-light businesses.",
  roe:           "Return on Equity: trailing net income ÷ shareholder equity. Higher means more profit per dollar of equity; can be inflated by leverage.",
  roa:           "Return on Assets: trailing net income ÷ total assets. Leverage-neutral profitability — useful to cross-check a high ROE.",
  gross_margin:  "Gross margin: (revenue − cost of goods) ÷ revenue. Higher means more pricing power and production efficiency.",
  profit_margin: "Net profit margin: trailing net income ÷ revenue. The bottom-line margin after all costs, interest, and tax.",
  fcf_yield:     "Free-cash-flow yield: trailing free cash flow ÷ market cap. A cash-based valuation check — higher means more cash generated per dollar of price.",
  revenue_growth: "Year-over-year revenue growth (most recent quarter vs the same quarter last year).",
  earnings_growth: "Year-over-year earnings growth (most recent quarter vs the same quarter last year). Very large values usually reflect a small base-year number.",
  quick_ratio:   "Quick ratio: (current assets − inventory) ÷ current liabilities. Stricter liquidity test than the current ratio; above 1 covers near-term obligations without selling inventory.",
  payout_ratio:  "Dividend payout ratio: dividends ÷ net income. Lower is more sustainable and leaves room to grow the dividend; above 100% means paying out more than earned.",
  analyst_rating:"Mean analyst recommendation, 1 (Strong Buy) → 5 (Sell). Lower is more bullish.",
  target_upside_pct: "Distance from current price to mean analyst price target, signed (positive = upside).",
  w52_high:      "Highest closing price over the trailing 52 weeks.",
  w52_low:       "Lowest closing price over the trailing 52 weeks.",
  news_sentiment: "News sentiment signal over the selected analysis window (News tab → Window, default 7 days): per-article AI scores aggregated with recency/source/novelty/relevance weights, plus a β·market systematic tilt. Dot: green = bullish, gray = neutral, red = bearish. Hover for the Bloomberg-style brief. Finnhub + Yahoo news, NVIDIA NIM scoring.",
};

/* Short (~40-55 char) inline descriptions for the Customize Columns modal —
   distinct from COL_INFO's full hover-tooltip text used elsewhere. */
const COL_INFO_SHORT = {
  symbol: "Exchange ticker symbol",
  logo: "Company/fund logo",
  name: "Full company or fund name",
  price: "Last price, in display currency",
  market_cap: "Price × shares outstanding",
  ps_ratio: "Price ÷ trailing sales",
  pe_ratio: "Price ÷ trailing EPS",
  pct_ytd: "Return since Jan 1",
  spark: "252-day price sparkline",
  pct_1y: "Total return, last 365 days",
  delta_ath: "% below 2Y high",
  rs_rank: "12-month relative strength",
  earnings_surprise: "EPS beat/miss, last 8 quarters",
  rec_trend_fh: "Analyst consensus Δ, 6 months",
  insider_mspr: "Insider buy/sell ratio (Form 4)",
  above_sma_20: "Price vs. 20-day average",
  above_sma_50: "Price vs. 50-day average",
  above_sma_200: "Price vs. 200-day average",
  pct_1w: "Return, last 7 days",
  pct_1m: "Return, last 30 days",
  pct_3m: "Return, last 91 days",
  pct_6m: "Return, last 182 days",
  rsi_14: "14-day Relative Strength Index",
  macd_hist_pct: "MACD histogram, % of price",
  bb_pct_b: "Position within Bollinger Bands",
  beta: "Volatility vs. the market",
  sector: "GICS sector",
  industry: "GICS sub-industry",
  forward_pe: "Price ÷ forward EPS estimate",
  peg: "P/E ÷ expected earnings growth",
  ev_revenue: "Enterprise value ÷ revenue",
  ev_ebitda: "Enterprise value ÷ EBITDA",
  operating_margin: "Operating profit ÷ revenue",
  debt_equity: "Leverage vs. equity",
  current_ratio: "Current assets ÷ liabilities",
  dividend_yield: "Annual dividend ÷ price",
  price_book: "Price ÷ book value of equity",
  roe: "Net income ÷ shareholder equity",
  roa: "Net income ÷ total assets",
  gross_margin: "Gross profit ÷ revenue",
  profit_margin: "Net income ÷ revenue",
  fcf_yield: "Free cash flow ÷ market cap",
  revenue_growth: "Revenue growth, YoY",
  earnings_growth: "Earnings growth, YoY",
  quick_ratio: "Liquid assets ÷ liabilities",
  payout_ratio: "Dividends ÷ net income",
  analyst_rating: "Mean analyst rating, 1–5",
  target_upside_pct: "Upside to mean price target",
  w52_high: "Highest close, trailing 52 weeks",
  w52_low: "Lowest close, trailing 52 weeks",
  news_sentiment: "AI-assessed news sentiment",
};

/* Category grouping for the Customize Columns modal — purely a display
   grouping, has no effect on rendering order in the actual table. */
const COL_GROUP = {
  logo: "Core", symbol: "Core", name: "Core", price: "Core", market_cap: "Core",
  sector: "Core", industry: "Core",
  ps_ratio: "Valuation", pe_ratio: "Valuation", forward_pe: "Valuation",
  peg: "Valuation", ev_revenue: "Valuation", ev_ebitda: "Valuation",
  price_book: "Valuation", fcf_yield: "Valuation",
  operating_margin: "Profitability & Leverage", debt_equity: "Profitability & Leverage",
  current_ratio: "Profitability & Leverage", dividend_yield: "Profitability & Leverage",
  roe: "Profitability & Leverage", roa: "Profitability & Leverage",
  gross_margin: "Profitability & Leverage", profit_margin: "Profitability & Leverage",
  quick_ratio: "Profitability & Leverage", payout_ratio: "Profitability & Leverage",
  revenue_growth: "Profitability & Leverage", earnings_growth: "Profitability & Leverage",
  pct_ytd: "Returns", pct_1y: "Returns", pct_1w: "Returns", pct_1m: "Returns",
  pct_3m: "Returns", pct_6m: "Returns", target_upside_pct: "Returns",
  spark: "Technical & Momentum", delta_ath: "Technical & Momentum", rs_rank: "Technical & Momentum",
  above_sma_20: "Technical & Momentum", above_sma_50: "Technical & Momentum", above_sma_200: "Technical & Momentum",
  rsi_14: "Technical & Momentum", macd_hist_pct: "Technical & Momentum", bb_pct_b: "Technical & Momentum",
  beta: "Technical & Momentum", w52_high: "Technical & Momentum", w52_low: "Technical & Momentum",
  analyst_rating: "Analyst & Sentiment", earnings_surprise: "Analyst & Sentiment",
  rec_trend_fh: "Analyst & Sentiment", insider_mspr: "Analyst & Sentiment", news_sentiment: "Analyst & Sentiment",
};
const COL_GROUP_ORDER = ["Core", "Valuation", "Profitability & Leverage", "Returns", "Technical & Momentum", "Analyst & Sentiment"];

let DATA = [];
let SORT = { key: "pct_ytd", dir: -1 };

/* ===========================================================================
 * Column views (Pass D)
 * ---------------------------------------------------------------------------
 * COLS is the registry of every available column. BUILTIN_VIEWS holds the
 * three preset column lists and BUILTIN_VIEW_HEAT their factory color modes.
 * Built-in views are editable in place: STATE.builtinOverrides layers per-view
 * column/color deltas on top of the factory definition (custom views carry
 * their own columns + color map). All rendering goes through getActiveColumns()
 * / getViewHeat() so consumers never need to know whether the active view is
 * built-in-with-override or custom.
 *
 * To add a new column: append a registry entry to COLS above (with key,
 * label, render, optional heat/align/sortable/sortValue), add a COL_INFO
 * tooltip, and — if it belongs in a preset — add its key to BUILTIN_VIEWS
 * here. The registry is the single source of truth; no other code path
 * should hardcode column keys outside that table.
 * --------------------------------------------------------------------------- */
const BUILTIN_VIEW_ALIASES = {
  "IB View": "Fundamentals",
  "Trader View": "Momentum",
};
const BUILTIN_VIEWS = {
  "Default":      ["logo","symbol","name","price","market_cap","pe_ratio","pct_ytd","spark","pct_1y","delta_ath","rs_rank","above_sma_20","above_sma_50","above_sma_200","earnings_surprise","rec_trend_fh","insider_mspr","news_sentiment"],
  "Fundamentals": ["symbol","price","market_cap","sector","industry","ps_ratio","pe_ratio","forward_pe","peg","ev_revenue","ev_ebitda","operating_margin","debt_equity","current_ratio","dividend_yield","earnings_surprise","rec_trend_fh","insider_mspr"],
  "Momentum":     ["symbol","price","pct_1w","pct_1m","pct_3m","pct_6m","pct_ytd","rsi_14","macd_hist_pct","bb_pct_b","beta","spark","pct_1y","delta_ath","rs_rank","earnings_surprise","rec_trend_fh","insider_mspr","above_sma_20","above_sma_50","above_sma_200","news_sentiment"],
};
const BUILTIN_ORDER = ["Default", "Fundamentals", "Momentum"];
/* Factory per-view color-coding ("heat") defaults. Only columns whose default
 * mode should DEVIATE from the column's intrinsic default (minmax for yo_dyn,
 * on for div) need an entry here — everything else falls back automatically.
 * This is what makes the color type a property of the view: EV/EBITDA is
 * percentile-ranked in Fundamentals but min-max wherever else it appears. */
const BUILTIN_VIEW_HEAT = {
  "Fundamentals": { ev_ebitda: "percentile" },
};
const COLS_BY_KEY = Object.fromEntries(COLS.map(c => [c.key, c]));
const DESC_DEFAULT_KEYS = new Set(["pct_ytd","pct_1y","pct_1w","pct_1m","pct_3m","pct_6m","delta_ath","market_cap","price","target_upside_pct"]);

function normalizeBuiltinViewName(name) {
  return BUILTIN_VIEW_ALIASES[name] || name;
}

/* ---- Effective view resolution ----------------------------------------
 * A view (built-in or custom) resolves to an effective {columns, heat}.
 * Built-in views layer a persisted override (STATE.builtinOverrides) on top
 * of the factory definition; custom views carry their own columns + heat.
 * All rendering + the customize modal read through these so no consumer needs
 * to know which flavour of view is active. */
function factoryColumnsFor(name) {
  return (BUILTIN_VIEWS[normalizeBuiltinViewName(name)] || BUILTIN_VIEWS["Default"]).slice();
}
function factoryHeatFor(name) {
  return Object.assign({}, BUILTIN_VIEW_HEAT[normalizeBuiltinViewName(name)] || {});
}
function getViewColumns(name) {
  name = normalizeBuiltinViewName(name);
  const S = (typeof STATE !== "undefined" && STATE) || {};
  if (BUILTIN_VIEWS[name]) {
    const ov = (S.builtinOverrides || {})[name];
    if (ov && Array.isArray(ov.columns) && ov.columns.length) return ov.columns.slice();
    return factoryColumnsFor(name);
  }
  const cv = (S.customViews || {})[name];
  return cv && Array.isArray(cv.columns) ? cv.columns.slice() : factoryColumnsFor("Default");
}
/* Effective per-view color-mode map. For built-ins the factory map is the
 * base and the persisted override wins on a per-column basis; for customs the
 * stored map is authoritative. The override stores only explicit user deltas
 * (never the factory values) so a future factory change still reaches views
 * the user hasn't overridden for that column. */
function getViewHeat(name) {
  name = normalizeBuiltinViewName(name);
  const S = (typeof STATE !== "undefined" && STATE) || {};
  if (BUILTIN_VIEWS[name]) {
    const ov = (S.builtinOverrides || {})[name];
    return Object.assign(factoryHeatFor(name), (ov && ov.heat) || {});
  }
  const cv = (S.customViews || {})[name];
  return Object.assign({}, (cv && cv.heat) || {});
}
function getActiveViewKeys() {
  return getViewColumns((typeof STATE !== "undefined" && STATE && STATE.activeViewName) || "Default");
}
function getActiveColumns() {
  return getActiveViewKeys().map(k => COLS_BY_KEY[k]).filter(Boolean);
}
function defaultSortDirFor(key) {
  return DESC_DEFAULT_KEYS.has(key) ? -1 : 1;
}
function ensureSortKey() {
  /* If active view doesn't include the current sort column, fall back to
     a sensible default (pct_ytd if visible, else first sortable). */
  const active = getActiveColumns();
  const stillThere = active.find(c => c.key === SORT.key && c.sortable);
  if (stillThere) return;
  const ytd = active.find(c => c.key === "pct_ytd" && c.sortable);
  if (ytd) { SORT.key = "pct_ytd"; SORT.dir = -1; return; }
  const firstSortable = active.find(c => c.sortable);
  SORT.key = firstSortable ? firstSortable.key : null;
  SORT.dir = SORT.key ? defaultSortDirFor(SORT.key) : -1;
}

/* ===========================================================================
 * Theme
 * --------------------------------------------------------------------------- */
const THEME_NAMES = ["light", "dark", "bloomberg"];
function getTheme() { return document.documentElement.dataset.theme || "light"; }
/* Bloomberg is a dark-canvas theme — heatmap/spark/return-colour math that
   branches on "is this a dark background" must treat it like dark. */
function isDarkTheme(t) { t = t || getTheme(); return t === "dark" || t === "bloomberg"; }
function setTheme(name) {
  document.documentElement.dataset.theme = name;
  localStorage.setItem("theme", name);
  const track = document.getElementById("ts-track");
  if (track) track.classList.toggle("on", name !== "light");
  const btn = document.getElementById("theme-switch");
  if (btn) btn.classList.toggle("bbg", name === "bloomberg");
  if (DATA.length) render();
}
function readTheme() {
  const saved = localStorage.getItem("theme");
  if (THEME_NAMES.includes(saved)) return saved;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function readFitColumnsPreference() {
  // Default ON: the table must fit the viewport out of the box on any monitor.
  // Only an explicit user opt-out ("0") turns it off (restoring horizontal
  // scroll); an unset preference means the user has never toggled it → fit.
  try {
    const v = localStorage.getItem("fit_columns");
    return v === null ? true : v === "1";
  }
  catch (e) { return true; }
}

function persistFitColumnsPreference(on) {
  try { localStorage.setItem("fit_columns", on ? "1" : "0"); }
  catch (e) {}
}

/* Color-coding ("heat") mode is now a property of the active view (see
   getViewHeat) — EV/EBITDA can be percentile in Fundamentals but min-max in
   Default. The legacy global localStorage map is retained only as a fallback
   default for any (view, column) with no explicit per-view mode, so a user's
   pre-existing global tweaks still apply as a baseline; new toggles write to
   the view, not the global. */
function readHeatPrefs() {
  try {
    const raw = localStorage.getItem("heat_prefs");
    return raw ? JSON.parse(raw) : {};
  } catch (e) { return {}; }
}
function getHeatMode(key) {
  const col = COLS_BY_KEY[key];
  if (!col || !col.heat) return null;
  const viewHeat = getViewHeat(STATE.activeViewName);
  let mode = viewHeat[key];
  if (mode == null) mode = (STATE.heatPrefs || {})[key]; // legacy global fallback
  if (col.heat.kind === "yo_dyn") {
    return (mode === "off" || mode === "percentile" || mode === "minmax") ? mode : "minmax";
  }
  if (col.heat.kind === "div") return mode === "off" ? "off" : "on";
  return null; // "yo" (analyst_rating) — uncontrolled, always legacy-on
}
function setHeatMode(key, mode) {
  const name = normalizeBuiltinViewName(STATE.activeViewName);
  if (isBuiltinView(name)) {
    const ov = STATE.builtinOverrides[name] || {};
    ov.heat = Object.assign({}, ov.heat || {});
    ov.heat[key] = mode;
    STATE.builtinOverrides[name] = ov;
    render();
    persistBuiltinHeat(name, key, mode);
  } else if (STATE.customViews[name]) {
    const cv = STATE.customViews[name];
    cv.heat = Object.assign({}, cv.heat || {});
    cv.heat[key] = mode;
    render();
    debouncedSaveCustom(name, cv.columns, cv.heat);
  } else {
    render();
  }
}

/* Heat-map endpoint colors per theme. */
const THEME_COLORS = {
  light: {
    bg:   [255, 255, 255],
    pos:  [31, 136, 61],     /* #1f883d  github success.emphasis */
    neg:  [207, 34, 46],     /* #cf222e  github danger.emphasis  */
    warn: [249, 115, 22],    /* #f97316  vivid orange (Tailwind orange-500) */
    blue: [37, 99, 235],     /* #2563eb  Tailwind blue-600 */
  },
  dark: {
    bg:   [13, 17, 23],      /* #0d1117 */
    pos:  [63, 185, 80],     /* #3fb950 */
    neg:  [248, 81, 73],     /* #f85149 */
    warn: [251, 146, 60],    /* #fb923c  orange-400, lighter on dark bg */
    blue: [96, 165, 250],    /* #60a5fa  Tailwind blue-400, lighter on dark bg */
  },
  bloomberg: {
    bg:   [0, 0, 0],         /* #000000  pure-black terminal canvas */
    pos:  [51, 209, 122],    /* #33d17a  up/green */
    neg:  [255, 67, 61],     /* #ff433d  official Bloomberg down/red */
    warn: [245, 179, 1],     /* #f5b301  gold */
    blue: [77, 199, 249],    /* #4dc7f9  Bloomberg cyan */
  },
};

/* ===========================================================================
 * Utility helpers
 * --------------------------------------------------------------------------- */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
function escapeHtml(s) {
  return String(s).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[c]));
}
function lerp(a, b, t) { return a + (b - a) * t; }
function clamp(x, a, b) { return Math.max(a, Math.min(b, x)); }
/* sortedArr must be ascending. Linear-interpolation percentile (numpy default). */
function percentileOf(sortedArr, p) {
  if (!sortedArr.length) return null;
  if (sortedArr.length === 1) return sortedArr[0];
  const idx = p * (sortedArr.length - 1);
  const lo = Math.floor(idx), hi = Math.ceil(idx);
  if (lo === hi) return sortedArr[lo];
  return sortedArr[lo] + (sortedArr[hi] - sortedArr[lo]) * (idx - lo);
}
/* Mid-rank percentile of `value` within sortedArr (ascending), ties averaged. */
function percentileRankOf(sortedArr, value) {
  if (sortedArr.length < 2) return null;
  let below = 0, equal = 0;
  for (const v of sortedArr) { if (v < value) below++; else if (v === value) equal++; }
  return (below + (equal - 1) / 2) / (sortedArr.length - 1);
}

/* ---------------------------------------------------------------------------
 * Unified tooltip positioner — single helper used by every hover-tip in the
 * app. Replaces the per-system CSS pseudo-element pattern that could not be
 * repositioned by JS and routinely clipped off the viewport.
 *
 * placeTip(tipEl, anchorRect, opts)
 *   - anchorRect:  {left, top, right, bottom, width, height} in CSS pixels
 *                  (a DOMRect, or anything quacking like one)
 *   - opts.preferred: "below" (default) | "above"
 *   - opts.offset:    distance from anchor edge to tip edge (default 8)
 *   - opts.gap:       minimum margin to keep from viewport edge (default 8)
 *   - opts.allowFlip: switch to the other side if the preferred overflows (default true)
 * The tip is set to position:fixed with left/top in CSS pixels. The chosen
 * side is written to data-side on the tip so an arrow indicator (if any)
 * can react via CSS.
 * --------------------------------------------------------------------------- */
function placeTip(tipEl, anchorRect, opts = {}) {
  if (!tipEl) return "below";
  const preferred = opts.preferred || "below";
  const offset = opts.offset == null ? 8 : opts.offset;
  const gap = opts.gap == null ? 8 : opts.gap;
  const allowFlip = opts.allowFlip !== false;
  // Make the tip measurable without flashing it in the wrong place — render
  // hidden first, measure, then move + reveal.
  const prevVis = tipEl.style.visibility;
  tipEl.style.visibility = "hidden";
  tipEl.style.left = "0px";
  tipEl.style.top = "0px";
  // Force a clean measurement.
  const rect = tipEl.getBoundingClientRect();
  const tw = rect.width, th = rect.height;
  const vw = window.innerWidth, vh = window.innerHeight;
  // Anchor center for horizontal placement.
  const cx = anchorRect.left + (anchorRect.width || 0) / 2;
  let side = preferred;
  let top;
  if (side === "below") {
    top = anchorRect.bottom + offset;
    if (allowFlip && top + th > vh - gap) {
      const tryAbove = anchorRect.top - offset - th;
      if (tryAbove >= gap) { side = "above"; top = tryAbove; }
    }
  } else {
    top = anchorRect.top - offset - th;
    if (allowFlip && top < gap) {
      const tryBelow = anchorRect.bottom + offset;
      if (tryBelow + th <= vh - gap) { side = "below"; top = tryBelow; }
    }
  }
  // Final vertical clamp (when neither side fits cleanly, prefer the
  // preferred side and clamp into the viewport).
  top = Math.max(gap, Math.min(vh - gap - th, top));
  // Horizontal: center on the anchor, then clamp to viewport.
  let left = cx - tw / 2;
  left = Math.max(gap, Math.min(vw - gap - tw, left));
  tipEl.style.left = left + "px";
  tipEl.style.top = top + "px";
  tipEl.setAttribute("data-side", side);
  // Arrow position (if the tip uses one) — point at the anchor center.
  const arrow = tipEl.querySelector(".app-tip-arrow");
  if (arrow) {
    const ax = Math.max(8, Math.min(tw - 8, cx - left));
    arrow.style.left = (ax - 5) + "px";
  }
  tipEl.style.visibility = prevVis || "";
  return side;
}

/* ---------------------------------------------------------------------------
 * Shared [data-tip] tooltip: one <div> at body level, populated and
 * positioned on hover by the delegated handler below. Works for every
 * [data-tip] in the DOM (topbar, column-view bar, MPT controls, overlay
 * pills, table headers, …).
 * --------------------------------------------------------------------------- */
const _APP_TIP = (() => {
  let el = null, currentTarget = null;
  function ensure() {
    if (el && document.body.contains(el)) return el;
    el = document.createElement("div");
    el.className = "app-tip";
    el.setAttribute("role", "tooltip");
    const arrow = document.createElement("div");
    arrow.className = "app-tip-arrow";
    el.appendChild(arrow);
    const body = document.createElement("div");
    body.className = "app-tip-body";
    el.appendChild(body);
    document.body.appendChild(el);
    return el;
  }
  function show(target) {
    const text = target.getAttribute && target.getAttribute("data-tip");
    if (!text) return;
    const tip = ensure();
    currentTarget = target;
    tip.querySelector(".app-tip-body").textContent = text;
    const r = target.getBoundingClientRect();
    // Default to below (matches the legacy CSS placement).
    placeTip(tip, r, {preferred: "below", offset: 8, gap: 8});
    tip.classList.add("show");
  }
  function hide(target) {
    // Only hide if we're hiding from the same element we showed for, so
    // back-to-back hovers don't fight each other.
    if (target && currentTarget && target !== currentTarget) return;
    if (el) el.classList.remove("show");
    currentTarget = null;
  }
  return {show, hide, ensure};
})();

document.addEventListener("mouseover", (ev) => {
  const t = ev.target.closest && ev.target.closest("[data-tip]");
  if (!t) return;
  // Skip if the element opted out (e.g. an editor input).
  if (t.getAttribute("data-tip-off") === "1") return;
  _APP_TIP.show(t);
}, true);
document.addEventListener("mouseout", (ev) => {
  const t = ev.target.closest && ev.target.closest("[data-tip]");
  if (!t) return;
  // Don't hide if cursor moved into a descendant.
  const next = ev.relatedTarget;
  if (next && t.contains(next)) return;
  _APP_TIP.hide(t);
}, true);
document.addEventListener("scroll", () => _APP_TIP.hide(), true);
window.addEventListener("resize", () => _APP_TIP.hide());
// Dismiss tooltip on any user click — pseudo-element ::after tooltips used
// to disappear naturally because the clicked element lost :hover; with a
// detached shared tip we need to hide it explicitly when the user takes any
// real action (e.g. opening a modal that covers the trigger).
document.addEventListener("mousedown", () => _APP_TIP.hide(), true);
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") _APP_TIP.hide();
}, true);

/* ---------------------------------------------------------------------------
 * .pf-metric-tip is a richer hover popover (LaTeX + description). Keep its
 * existing CSS-driven show/hide but route position through placeTip so it
 * stops clipping near viewport edges.
 * --------------------------------------------------------------------------- */
document.addEventListener("mouseenter", (ev) => {
  const t = ev.target && ev.target.nodeType === 1 && ev.target.closest
    ? ev.target.closest("[data-info]") : null;
  if (!t) return;
  const tip = t.querySelector(":scope > .pf-metric-tip, :scope .pf-metric-tip");
  if (!tip) return;
  // Render into fixed positioning so it can escape clipping ancestors and
  // get clamped by placeTip. Remember the original inline styles so we can
  // restore them on leave (avoids permanent style mutations).
  if (!tip.dataset._origPos) {
    tip.dataset._origPos = tip.style.position || "";
    tip.dataset._origLeft = tip.style.left || "";
    tip.dataset._origTop = tip.style.top || "";
    tip.dataset._origRight = tip.style.right || "";
  }
  tip.style.position = "fixed";
  tip.style.right = "auto";
  // Force display so it can be measured; CSS :hover rule will also keep it shown.
  const prevDisplay = tip.style.display;
  tip.style.display = "block";
  const r = t.getBoundingClientRect();
  placeTip(tip, r, {preferred: "below", offset: 8, gap: 10});
  // Restore inline display so the CSS :hover rule keeps owning visibility.
  tip.style.display = prevDisplay || "";
}, true);
document.addEventListener("mouseleave", (ev) => {
  const t = ev.target && ev.target.nodeType === 1 && ev.target.closest
    ? ev.target.closest("[data-info]") : null;
  if (!t) return;
  const tip = t.querySelector(":scope > .pf-metric-tip, :scope .pf-metric-tip");
  if (!tip) return;
  // Restore original inline styles so the CSS-positioned rule reapplies on
  // the next hover if placeTip isn't reached (e.g. tip rendered after the
  // hover already started).
  if (tip.dataset._origPos != null) {
    tip.style.position = tip.dataset._origPos;
    tip.style.left = tip.dataset._origLeft;
    tip.style.top = tip.dataset._origTop;
    tip.style.right = tip.dataset._origRight;
    delete tip.dataset._origPos; delete tip.dataset._origLeft;
    delete tip.dataset._origTop; delete tip.dataset._origRight;
  }
}, true);

// The wrap's clientWidth INCLUDES its horizontal padding, but the table is
// laid out inside that padding — so the true space available to the table is
// clientWidth minus padding-left/right. Ignoring it (the old bug) left the
// table overflowing by ~padding-left px, clipping the rightmost column.
function tableContentWidth(wrap) {
  const cs = getComputedStyle(wrap);
  const padX = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
  return wrap.clientWidth - padX;
}

function fitScaleForColumns(columns = getActiveColumns()) {
  if (!STATE.fitColumns) return 1;
  const wrap = document.querySelector(".table-wrap");
  if (!wrap || !columns.length) return 1;
  const available = Math.max(320, tableContentWidth(wrap) - 4);
  /* Floor at 36px, not 56px: 56 absorbed the narrow SMA-flag columns entirely
     (46/46/50 all floored to 56), so shrinking their labels/widths had no
     effect on the computed scale. 36 matches news_sentiment (the narrowest
     non-SMA/non-logo column), so it only relaxes the floor for the columns
     intentionally narrowed here, not for anything else. */
  const required = columns.reduce((sum, c) => sum + Math.max(36, c.w || 80), 0);
  if (!required) return 1;
  return clamp(available / required, 0.4, 1);
}

// Pass 2 — after the scaled DOM has painted, measure the table's actual
// scrollWidth and, if it still overflows the wrap, apply a CSS transform
// to guarantee zero horizontal overflow. The font/padding pass keeps text
// crisp at normal scales; the transform only kicks in as a last resort.
function applyTableTransformFit() {
  const wrap = document.querySelector(".table-wrap");
  const table = document.getElementById("tbl");
  if (!wrap || !table) return;
  if (!STATE.fitColumns) {
    table.style.transform = "";
    table.style.transformOrigin = "";
    table.style.width = "";
    wrap.style.height = "";
    return;
  }
  // Reset any prior transform before measuring so scrollWidth is the
  // intrinsic post-scaling width, not a post-transform width.
  table.style.transform = "";
  table.style.width = "";
  wrap.style.height = "";
  const need = table.scrollWidth;
  const have = tableContentWidth(wrap);   // exclude wrap padding — see fitScaleForColumns
  if (need > have + 0.5) {
    const extra = have / need;
    // Expand the pre-transform table so the post-transform render fills
    // the wrap edge-to-edge. clientHeight needs to shrink with the
    // transform too, otherwise the wrap reserves the pre-scale height.
    table.style.transformOrigin = "top left";
    table.style.transform = `scale(${extra.toFixed(4)})`;
    table.style.width = `${(100 / extra).toFixed(3)}%`;
    const postHeight = table.getBoundingClientRect().height;
    if (postHeight > 0) wrap.style.height = `${Math.ceil(postHeight + 4)}px`;
    // Self-correct: the measurement above can occasionally land a few
    // pixels short of the table's true rendered height (e.g. a stale
    // rAF from an earlier render() resolving after a newer one during a
    // streaming build). Any shortfall turns .table-wrap into its own
    // vertically-scrollable region — a "scroll trapped inside the
    // table" bug distinct from the page's normal scroll. The wrap must
    // NEVER be shorter than its content, so widen it to match if so.
    if (wrap.scrollHeight > wrap.clientHeight) {
      wrap.style.height = `${wrap.scrollHeight}px`;
    }
  }
}

// Renders during a streaming build call render() (and therefore this
// function) once per incoming row, each scheduling its own async
// double-rAF re-measurement below. Without a generation guard, an older
// render's rAF pair can resolve after a newer one and clobber the wrap's
// height with a stale (too-short) measurement — see applyTableTransformFit's
// self-correction comment for what that breaks. Bumping _fitGen per call and
// having each scheduled pass bail out once superseded makes only the latest
// render's measurement ever take effect.
let _fitGen = 0;
function applyTableFitMode(columns = getActiveColumns()) {
  const wrap = document.querySelector(".table-wrap");
  if (!wrap) return 1;
  const scale = fitScaleForColumns(columns);
  wrap.classList.toggle("fit-columns", !!STATE.fitColumns);
  wrap.style.setProperty("--table-scale", scale.toFixed(3));
  // Schedule the post-layout measurement after the browser has applied
  // the new --table-scale variable. rAF is enough; double-rAF guards
  // against fonts/images settling on a second tick.
  const myGen = ++_fitGen;
  requestAnimationFrame(() => {
    if (myGen !== _fitGen) return;
    applyTableTransformFit();
    requestAnimationFrame(() => {
      if (myGen !== _fitGen) return;
      applyTableTransformFit();
    });
  });
  return scale;
}

function toggleFitColumns() {
  STATE.fitColumns = !STATE.fitColumns;
  persistFitColumnsPreference(STATE.fitColumns);
  render();
}
function rgb(r, g, b) { return `rgb(${r|0},${g|0},${b|0})`; }
function rgbMix(c1, c2, t) {
  return rgb(lerp(c1[0], c2[0], t), lerp(c1[1], c2[1], t), lerp(c1[2], c2[2], t));
}

/* ───────────────────────────── Loading chip ─────────────────────────────
 * lcHtml(text, opts)  → returns chip markup (use inside renderers)
 * lcShow(target, text, opts) → mounts/updates a chip inside `target`
 * lcHide(target) → removes the chip
 * opts: { bar: bool, meta: string }
 * --------------------------------------------------------------------- */
function lcHtml(text, opts) {
  opts = opts || {};
  const bar = opts.bar ? `<span class="lc-bar"></span>` : "";
  const meta = (opts.meta || opts.meta === 0) ? `<span class="lc-meta">${escapeHtml(opts.meta)}</span>` : "";
  const txt = text ? `<span class="lc-text">${escapeHtml(text)}</span>` : "";
  return `<span class="lc">${bar}${txt}${meta}</span>`;
}
function _lcTarget(t) { return typeof t === "string" ? document.querySelector(t) : t; }
function lcShow(target, text, opts) {
  const el = _lcTarget(target); if (!el) return;
  let anchor = el.querySelector(":scope > .lc-anchor");
  if (!anchor) {
    anchor = document.createElement("span");
    anchor.className = "lc-anchor";
    el.appendChild(anchor);
  }
  anchor.innerHTML = lcHtml(text, opts);
}
function lcHide(target) {
  const el = _lcTarget(target); if (!el) return;
  const anchor = el.querySelector(":scope > .lc-anchor");
  if (anchor) anchor.remove();
}

/* ===========================================================================
 * FX (denomination) module
 * --------------------------------------------------------------------------- */
const FX_SUPPORTED = ["USD","EUR","GBP","JPY","CHF","CAD","AUD","NZD","CNY","ZAR","MXN","SGD","HKD","INR"];
const FX_NAMES = {
  USD: "US Dollar",       EUR: "Euro",
  GBP: "British Pound",   JPY: "Japanese Yen",
  CHF: "Swiss Franc",     CAD: "Canadian Dollar",
  AUD: "Australian Dollar", NZD: "New Zealand Dollar",
  CNY: "Chinese Yuan",    ZAR: "South African Rand",
  MXN: "Mexican Peso",    SGD: "Singapore Dollar",
  HKD: "Hong Kong Dollar", INR: "Indian Rupee",
};
const FX_SYMBOL = {
  USD: "$",   EUR: "€",   GBP: "£",   JPY: "¥",   CHF: "Fr",
  CAD: "C$",  AUD: "A$",  NZD: "NZ$", CNY: "CN¥", ZAR: "R",
  MXN: "Mex$", SGD: "S$", HKD: "HK$", INR: "₹",
};
/* Number of decimals to show for the displayed currency. JPY/HKD/CNY trade
   in much larger nominal units, so .00 looks silly. */
const FX_DECIMALS = { JPY: 0, HKD: 1, CNY: 2 };
/* USD-based: rates[ccy] = how many `ccy` per 1 USD. Populated on load. */
let FX_RATES = { USD: 1.0 };
let FX_QUOTE = "USD";       // user-selected display currency
let FX_INDEX_CACHE = {};    // { ccy: [[ts,val],...] }
let FX_INDEX_INFLIGHT = {}; // { ccy: Promise }
let FX_HOVER_REQ_ID = 0;
let FX_HOVER_CCY = null;

function fxLoadPref() {
  try {
    const saved = localStorage.getItem("fx_quote");
    if (saved && FX_SUPPORTED.indexOf(saved) >= 0) FX_QUOTE = saved;
  } catch (e) {}
}

async function fxLoadRates() {
  try {
    const r = await fetch("/api/fx-rates?base=USD");
    if (!r.ok) return;
    const j = await r.json();
    if (j && j.rates) FX_RATES = j.rates;
  } catch (e) {}
}

/* Convert `amount` from `fromCcy` to the active display currency.
   Handles Yahoo's pence/cents subunits (LSE → GBp, JSE → ZAc). */
function fxConvert(amount, fromCcy) {
  if (amount == null || !isFinite(amount)) return amount;
  let from = fromCcy || "USD";
  let scale = 1;
  if (from === "GBp" || from === "GBX") { from = "GBP"; scale = 0.01; }
  else if (from === "ZAc") { from = "ZAR"; scale = 0.01; }
  from = String(from).toUpperCase();
  const base = amount * scale;
  const to = FX_QUOTE;
  if (from === to) return base;
  const rFrom = FX_RATES[from];   // from per USD
  const rTo = FX_RATES[to];       // to per USD
  if (!rFrom || !rTo) return base;  // graceful: no conversion data
  return base * (rTo / rFrom);
}

function fxDecimals(ccy) {
  const d = FX_DECIMALS[ccy || FX_QUOTE];
  return d == null ? 2 : d;
}

function fmtMoney(v, ccy) {
  if (v == null || !isFinite(v)) return na();
  const converted = fxConvert(v, ccy);
  const sym = FX_SYMBOL[FX_QUOTE] || (FX_QUOTE + " ");
  const dec = fxDecimals(FX_QUOTE);
  return sym + Number(converted).toLocaleString(undefined, {minimumFractionDigits: dec, maximumFractionDigits: dec});
}
function fmtCompactMoney(v, ccy) {
  if (v == null || !isFinite(v) || v === 0) return na();
  const converted = fxConvert(v, ccy);
  const sym = FX_SYMBOL[FX_QUOTE] || (FX_QUOTE + " ");
  const a = Math.abs(converted);
  let unit, scaled;
  if (a >= 1e12) { unit = "T"; scaled = converted/1e12; }
  else if (a >= 1e9) { unit = "B"; scaled = converted/1e9; }
  else if (a >= 1e6) { unit = "M"; scaled = converted/1e6; }
  else if (a >= 1e3) { unit = "K"; scaled = converted/1e3; }
  else { return sym + converted.toFixed(2); }
  return sym + scaled.toFixed(1) + unit;
}
function fmt2(v) { return (v == null || !isFinite(v)) ? na() : Number(v).toFixed(2); }

/* Canonical user-visible date format across the app: "Jul 7, 2026".
   Accepts a Date, epoch ms, or ISO string; bare YYYY-MM-DD strings are
   parsed as local-calendar dates (not UTC) so they never shift a day. */
function fmtDateMDY(d) {
  let dt;
  if (d instanceof Date) dt = d;
  else if (typeof d === "string" && /^\d{4}-\d{2}-\d{2}$/.test(d)) {
    const [y, m, dd] = d.split("-").map(Number);
    dt = new Date(y, m - 1, dd);
  } else dt = new Date(d);
  if (isNaN(dt)) return d == null ? "—" : String(d);
  return dt.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}
/* Same, without the year — for compact stamps like the flash tape. */
function fmtDateMD(d) {
  const dt = (d instanceof Date) ? d : new Date(d);
  if (isNaN(dt)) return "—";
  return dt.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}
/* Day-Month-Year, e.g. "09 Jul 2026" — used for the app version tag only
   (conventional release-note date format, distinct from fmtDateMDY above). */
function fmtDateDMY(d) {
  let dt;
  if (d instanceof Date) dt = d;
  else if (typeof d === "string" && /^\d{4}-\d{2}-\d{2}$/.test(d)) {
    const [y, m, dd] = d.split("-").map(Number);
    dt = new Date(y, m - 1, dd);
  } else dt = new Date(d);
  if (isNaN(dt)) return d == null ? "—" : String(d);
  const day = String(dt.getDate()).padStart(2, "0");
  const month = dt.toLocaleDateString("en-US", { month: "short" });
  return `${day} ${month} ${dt.getFullYear()}`;
}
function fmtPctSigned(v) {
  if (v == null || !isFinite(v)) return na();
  const sign = v > 0 ? "+" : (v < 0 ? "" : "+");
  return sign + v.toFixed(2) + "%";
}
function fmtPctDirect(v) {
  if (v == null || !isFinite(v)) return na();
  return (Number(v) * 100).toFixed(2) + "%";
}
function na() { return '<span class="na">n/a</span>'; }

/* ===========================================================================
 * Heat-map scales
 * --------------------------------------------------------------------------- */
function colorYO(t, theme) {
  /* yellow → orange.  Tints the background colour toward the "warn" endpoint
     so it works in both themes. */
  t = clamp(t, 0, 1);
  const C = THEME_COLORS[theme];
  /* Use ~92% of the way to warn at full saturation so text stays readable. */
  return rgbMix(C.bg, C.warn, t * 0.92);
}
function colorBlue(t, theme) {
  /* background → blue.  Same mix ratio as colorYO for consistent readability. */
  t = clamp(t, 0, 1);
  const C = THEME_COLORS[theme];
  return rgbMix(C.bg, C.blue, t * 0.92);
}
function colorDiverging(t, theme) {
  /* t in [-1, 1]; 0 → background (white in light, near-black in dark). */
  t = clamp(t, -1, 1);
  const C = THEME_COLORS[theme];
  const tgt = t >= 0 ? C.pos : C.neg;
  return rgbMix(C.bg, tgt, Math.abs(t) * 0.9);
}
function textOnHeat(t, theme) {
  /* Switch to white text once tint is deep enough that the standard fg
     would lose contrast.  Pick threshold per theme. */
  const mag = Math.abs(t);
  if (isDarkTheme(theme)) return mag > 0.65 ? "#ffffff" : "var(--text)";
  return mag > 0.55 ? "#ffffff" : "var(--text)";
}
function returnColor(pct, anchor = 6) {
  /* Magnitude-scaled TEXT color for a signed return: muted near 0, deepening
     to full green/red as |pct| approaches `anchor`. Sibling of colorDiverging
     (which tints a background); this returns a readable foreground color.
     Reused by the News Movers list and the Market·Systematic tape. */
  if (pct == null || !isFinite(pct)) return "";
  const theme = getTheme();
  const C = THEME_COLORS[theme];
  const neutral = isDarkTheme(theme) ? [125, 133, 144] : [110, 118, 129]; /* --muted */
  const t = clamp(pct / anchor, -1, 1);
  return rgbMix(neutral, pct >= 0 ? C.pos : C.neg, Math.abs(t));
}

/* ===========================================================================
 * Visual primitives
 * --------------------------------------------------------------------------- */
function logoImg(ticker) {
  const t = encodeURIComponent(ticker);
  return `<img class="logo" loading="lazy" alt=""
    src="https://financialmodelingprep.com/image-stock/${t}.png"
    onerror="logoFallback(this, '${t.replace(/'/g,"\\'")}')">`;
}
window.logoFallback = function(img, ticker) {
  if (img.dataset.tried === "parqet") {
    img.onerror = null;
    const span = document.createElement("span");
    span.className = "logo-fallback";
    const sym = String(ticker).replace(/^\^/, "").replace(/[^A-Za-z0-9]/g, "");
    span.textContent = sym.slice(0, 2) || "?";
    img.replaceWith(span);
    return;
  }
  img.dataset.tried = "parqet";
  img.src = `https://assets.parqet.com/logos/symbol/${ticker}?format=png`;
};

function triangle(v) {
  if (v === null || v === undefined) return '<span class="na">—</span>';
  return v ? '<span class="tri-up">▲</span>' : '<span class="tri-down">▼</span>';
}

function sparkSvg(pts, pct1y) {
  if (!pts || pts.length < 2) return na();
  const w = 96, h = 22, pad = 1;
  const lo = Math.min(...pts), hi = Math.max(...pts);
  const rng = (hi - lo) || 1;
  const step = (w - 2 * pad) / (pts.length - 1);
  let d = "";
  for (let i = 0; i < pts.length; i++) {
    const x = pad + i * step;
    const y = h - pad - ((pts[i] - lo) / rng) * (h - 2 * pad);
    d += (i === 0 ? "M" : "L") + x.toFixed(1) + "," + y.toFixed(1) + " ";
  }
  const up = (pct1y != null ? pct1y : (pts[pts.length-1] - pts[0])) >= 0;
  const stroke = up
    ? getComputedStyle(document.documentElement).getPropertyValue("--pos").trim() || "#1f883d"
    : getComputedStyle(document.documentElement).getPropertyValue("--neg").trim() || "#cf222e";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.2"/>
  </svg>`;
}
function sparkBg(pct1y) {
  /* light tint for the chart cell so positive/negative reads at a glance. */
  if (pct1y == null || !isFinite(pct1y)) return "";
  const theme = getTheme();
  const C = THEME_COLORS[theme];
  const tgt = pct1y >= 0 ? C.pos : C.neg;
  return `background:${rgbMix(C.bg, tgt, 0.18)};`;
}

function rsBars(arr) {
  if (!arr || !arr.length) return na();
  const w = 86, h = 22, n = arr.length, gap = 1;
  const bw = (w - (n - 1) * gap) / n;
  const theme = getTheme();
  const C = THEME_COLORS[theme];
  let svg = "";
  for (let i = 0; i < n; i++) {
    const v = clamp(arr[i] || 0, 0, 1);
    const bh = Math.max(1, v * (h - 2));
    const x = i * (bw + gap);
    const y = h - bh;
    /* Brighter green for higher rank — tint pos endpoint into bg. */
    const c = rgbMix(C.bg, C.pos, 0.35 + v * 0.6);
    svg += `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${bw.toFixed(2)}" height="${bh.toFixed(2)}" fill="${c}" rx="0.5"/>`;
  }
  return `<svg class="rs" viewBox="0 0 ${w} ${h}">${svg}</svg>`;
}

function deltaBar(v) {
  if (v == null || !isFinite(v)) return na();
  /* v ≤ 0 normally (% below ATH). 0 = at high (good), large neg = bad. */
  const mag = Math.abs(v);
  const MAX = 50;  // anchor: 50% drawdown = full bar
  const width = clamp(mag / MAX, 0, 1) * 100;
  const theme = getTheme();
  const C = THEME_COLORS[theme];
  /* Bar tint: at high → green, getting darker red as drawdown grows. */
  let intensity;
  let tgt;
  if (mag < 3) { tgt = C.pos; intensity = 0.30; }
  else if (mag < 15) { tgt = C.warn; intensity = 0.55; }
  else { tgt = C.neg; intensity = clamp(0.55 + (mag - 15) / 50, 0.55, 0.95); }
  const bg = rgbMix(C.bg, tgt, intensity);
  const label = (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  const txt = intensity > 0.6 ? "#fff" : "var(--text)";
  return `<div class="bar-cell">
    <div class="bar" style="width:${width.toFixed(1)}%; background:${bg}"></div>
    <span class="bar-label" style="color:${txt}">${label}</span>
  </div>`;
}

/* ===========================================================================
 * Finnhub-powered cells (EPS surprise, recommendation trend, insider MSPR).
 * All three degrade to na() when the row field is null (no FINNHUB_API_KEY
 * or non-US ticker with empty data).
 * --------------------------------------------------------------------------- */

/* "YYYY-MM-DD" → "Q3 2024" for the EPS-surprise tooltip. */
function fhQuarter(period) {
  if (!period) return "?";
  const parts = String(period).split("-");
  const y = parts[0] || "?";
  const m = parseInt(parts[1], 10);
  const q = isFinite(m) ? Math.ceil(m / 3) : "?";
  return "Q" + q + " " + y;
}

/* 8 diverging bars anchored at a midline: beats grow up (green), misses
   grow down (red). arr is most-recent first; we render oldest→newest L→R
   and pad missing quarters on the left. */
function epsSurpriseBars(arr) {
  if (!arr || !arr.length) return na();
  if (arr.every(e => !e || e.surprise_pct == null)) return na();
  const W = 80, H = 18, mid = 9, gap = 1, n = 8;
  const bw = (W - (n - 1) * gap) / n;
  const slots = arr.slice(0, n).reverse();          // oldest..newest
  while (slots.length < n) slots.unshift(null);      // left-pad to 8
  let svg = "";
  for (let i = 0; i < n; i++) {
    const e = slots[i];
    const x = (i * (bw + gap)).toFixed(2);
    if (!e) {
      svg += `<rect x="${x}" y="${(mid - 0.5).toFixed(2)}" width="${bw.toFixed(2)}" height="1" fill="var(--muted)" opacity="0.45"/>`;
      continue;
    }
    const p = e.surprise_pct;
    if (p == null || p === 0) {
      svg += `<rect x="${x}" y="${(mid - 1).toFixed(2)}" width="${bw.toFixed(2)}" height="2" fill="var(--muted)"/>`;
      continue;
    }
    const bh = Math.max(2, Math.min(Math.abs(p), 10) / 10 * 14);
    const y = p > 0 ? mid - bh : mid;                // beat up, miss down
    const color = p > 0 ? "var(--pos)" : "var(--neg)";
    svg += `<rect x="${x}" y="${y.toFixed(2)}" width="${bw.toFixed(2)}" height="${bh.toFixed(2)}" fill="${color}" rx="0.5"/>`;
  }
  const tip = arr.slice(0, n).map(e => {
    const v = e.surprise_pct == null ? "n/a" : (e.surprise_pct > 0 ? "+" : "") + e.surprise_pct.toFixed(1) + "%";
    return `${fhQuarter(e.period)}: ${v}`;
  }).join("  ");
  return `<span data-tip="${tip}"><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" style="vertical-align:middle">${svg}</svg></span>`;
}

/* Insider Monthly Share Purchase Ratio badge. */
function msprBadge(o) {
  if (!o || o.mspr == null) return na();
  const v = o.mspr;
  const color = v > 5 ? "var(--pos)" : (v < -5 ? "var(--neg)" : "var(--muted)");
  const label = (v >= 0 ? "+" : "") + v.toFixed(1);
  const tip = `MSPR ${label} — Monthly Share Purchase Ratio. +100 = all insiders buying, −100 = all selling. ${o.month}/${o.year}`;
  return `<span style="color:${color}" data-tip="${tip}">${label}</span>`;
}

const NS_COLORS = {
  /* Deep saturated endpoints for the "very" tiers vs pale plain tiers, so the
     two steps read clearly apart on the timeline, dots, and badges. */
  very_bullish: "#16a34a",
  bullish:      "#86efac",
  neutral:      "#94a3b8",
  bearish:      "#fca5a5",
  very_bearish: "#dc2626",
};
const NS_LABELS = {
  very_bullish: "Very Bullish",
  bullish:      "Bullish",
  neutral:      "Neutral",
  bearish:      "Bearish",
  very_bearish: "Very Bearish",
};

function nsDot(ns) {
  if (!ns || !ns.tier) return `<span class="ns-dot ns-empty" data-tip="News sentiment not yet loaded"></span>`;
  const color = NS_COLORS[ns.tier] || NS_COLORS.neutral;
  const label = NS_LABELS[ns.tier] || "Neutral";
  const tip = `${label} (${ns.score >= 0 ? "+" : ""}${ns.score.toFixed(2)}) — ${ns.summary || ""}`;
  return `<span class="ns-dot" style="background:${color}" data-tip="${escapeHtml(tip)}"></span>`;
}

/* Per-month analyst consensus score; null if no votes that month. */
function fhConsensus(m) {
  if (!m) return null;
  const total = (m.strongBuy || 0) + (m.buy || 0) + (m.hold || 0) + (m.sell || 0) + (m.strongSell || 0);
  if (!total) return null;
  return (2 * (m.strongBuy || 0) + (m.buy || 0) - (m.sell || 0) - 2 * (m.strongSell || 0)) / total;
}

/* Δ between newest and oldest monthly consensus score; number or null. */
function recTrendScore(arr) {
  if (!arr || arr.length < 2) return null;
  const newest = fhConsensus(arr[0]);
  const oldest = fhConsensus(arr[arr.length - 1]);
  if (newest == null || oldest == null) return null;
  return newest - oldest;
}

function recTrendCell(arr) {
  if (!arr || arr.length < 2) return na();
  const newest = fhConsensus(arr[0]);
  const oldest = fhConsensus(arr[arr.length - 1]);
  if (newest == null || oldest == null) return na();
  const delta = newest - oldest;
  const n = arr.length;
  let glyph, color;
  if (delta > 0.02) { glyph = "▲"; color = "var(--pos)"; }
  else if (delta < -0.02) { glyph = "▼"; color = "var(--neg)"; }
  else { glyph = "—"; color = "var(--muted)"; }
  const sign = delta > 0 ? "+" : (delta < 0 ? "−" : "");
  const label = `${glyph} ${sign}${Math.abs(delta).toFixed(2)}`;
  const tip = `Analyst consensus trend over ${n} months. Score = (2×SB+B−S−2×SS)/total. Current: ${newest.toFixed(2)}, ${n}M ago: ${oldest.toFixed(2)}, Δ = ${delta.toFixed(2)}`;
  return `<span style="color:${color}" data-tip="${tip}">${label}</span>`;
}

/* ===========================================================================
 * Heat-map cell styles
 * --------------------------------------------------------------------------- */
function cellStyleHeat(col, value, theme, ctx) {
  const h = col.heat;
  if (!h) return "";
  if (h.kind === "yo") {
    /* Fixed-ceiling orange ramp. n/a → max if h.naMax.
       h.invert flips the ramp so lower values get more saturated colour
       (used for analyst-rating, where 1 = strong buy, 5 = sell). */
    let t;
    if (value == null || !isFinite(value)) {
      if (!h.naMax) return "";
      t = 1;
    } else {
      const lo = h.clipMin, hi = h.clipMax;
      t = (hi === lo) ? 0.5 : clamp((value - lo) / (hi - lo), 0, 1);
      if (h.invert) t = 1 - t;
    }
    return `background:${colorYO(t, theme)};`;
  }
  if (h.kind === "yo_dyn") {
    /* Dynamic per-column, per-render blue ramp: the most business-favorable
       value currently on screen (per h.favor) is most blue, least-favorable
       is neutral. n/a always renders neutral. ctx carries the active color
       mode ("minmax" = percentile-clipped 10th/90th, or "percentile" = pure
       rank), computed once per render — never a hardcoded clip constant, and
       never derailed by a single outlier the way a raw min/max would be. */
    if (value == null || !isFinite(value)) return "";
    if (!ctx) return "";
    let t;
    if (ctx.mode === "percentile") {
      t = percentileRankOf(ctx.sorted, value);
      if (t == null) return "";
    } else {
      const { lo, hi } = ctx;
      if (lo == null || hi == null) return "";
      t = (hi === lo) ? 0 : clamp((value - lo) / (hi - lo), 0, 1);
    }
    if (h.favor === "low") t = 1 - t;
    return `background:${colorBlue(t, theme)};`;
  }
  if (h.kind === "div") {
    if (value == null || !isFinite(value)) return "";
    const a = h.anchor || 100;
    const t = clamp(value / a, -1, 1);
    return `background:${colorDiverging(t, theme)}; color:${textOnHeat(t, theme)};`;
  }
  return "";
}

/* ===========================================================================
 * Column-view bar, customize modal, and header drag-and-drop (Pass D)
 * ---------------------------------------------------------------------------
 * UI surface: a slim row above the table with built-in preset chips,
 * a "+ Custom" dropdown listing user-saved views, and a Customize button
 * that opens the modal. STATE.activeViewName drives which set of column
 * keys getActiveColumns() returns. Built-in views are now editable in place:
 * edits persist as a per-view override (STATE.builtinOverrides, mirroring the
 * server's builtin_overrides map) and a "Reset to default" pill appears
 * whenever the active built-in differs from its factory definition.
 * --------------------------------------------------------------------------- */

const CV_CUSTOM_KEY = "__cv_custom__";

function isBuiltinView(name) { return Object.prototype.hasOwnProperty.call(BUILTIN_VIEWS, normalizeBuiltinViewName(name)); }

function sameColumnKeys(left, right) {
  if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
  for (let i = 0; i < left.length; i++) {
    if (left[i] !== right[i]) return false;
  }
  return true;
}

/* Does the active built-in differ from its factory definition (columns OR an
   explicit color override)? Drives the "Reset to default" pill. */
function builtinHasOverride(name) {
  name = normalizeBuiltinViewName(name);
  if (!isBuiltinView(name)) return false;
  const ov = (STATE.builtinOverrides || {})[name];
  if (!ov) return false;
  if (Array.isArray(ov.columns) && ov.columns.length && !sameColumnKeys(ov.columns, factoryColumnsFor(name))) return true;
  return !!(ov.heat && Object.keys(ov.heat).length);
}

function currentActiveKeys() {
  /* Returns the live list of effective column keys for the active view. */
  return getViewColumns(STATE.activeViewName);
}

async function loadColumnViews() {
  try {
    const r = await fetch("/api/column-views");
    if (!r.ok) return;
    const j = await r.json();
    STATE.customViews = j.custom || {};
    STATE.builtinOverrides = j.builtin_overrides || {};
    const desired = normalizeBuiltinViewName(j.active || "Default");
    if (isBuiltinView(desired) || STATE.customViews[desired]) {
      STATE.activeViewName = desired;
    } else {
      STATE.activeViewName = "Default";
    }
    render();
  } catch (e) { /* persistence is best-effort */ }
}

async function persistActiveView(name) {
  try {
    await fetch("/api/column-views/active", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name}),
    });
  } catch (e) { /* best-effort */ }
}

/* Persist a view definition (columns + heat). A built-in name is routed
   server-side to its override store; any other name is a custom view. On
   success we refresh both maps from the response. */
async function persistColumnView(name, columns, heat) {
  try {
    const r = await fetch("/api/column-views", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name, columns, heat: heat || {}}),
    });
    if (r.ok) {
      const j = await r.json();
      STATE.customViews = j.custom || STATE.customViews;
      STATE.builtinOverrides = j.builtin_overrides || STATE.builtinOverrides;
    }
  } catch (e) {}
}

/* Heat-only toggle on a built-in — kept separate so it never freezes the
   view's columns to a factory-equal override. */
async function persistBuiltinHeat(name, key, mode) {
  try {
    const r = await fetch("/api/column-views/builtin-heat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: normalizeBuiltinViewName(name), key, mode}),
    });
    if (r.ok) {
      const j = await r.json();
      STATE.builtinOverrides = j.builtin_overrides || STATE.builtinOverrides;
    }
  } catch (e) {}
}

let _cvSaveTimer = null;
function debouncedSaveCustom(name, columns, heat) {
  clearTimeout(_cvSaveTimer);
  _cvSaveTimer = setTimeout(() => { persistColumnView(name, columns, heat); }, 250);
}

function setActiveView(name, {persist = true} = {}) {
  name = normalizeBuiltinViewName(name);
  if (!isBuiltinView(name) && !STATE.customViews[name]) return;
  STATE.activeViewName = name;
  render();
  if (persist) persistActiveView(name);
}

function renderColumnViewBar() {
  const seg = document.getElementById("cv-builtins");
  if (!seg) return;
  seg.innerHTML = "";
  const activeName = normalizeBuiltinViewName(STATE.activeViewName || "Default");
  for (const name of BUILTIN_ORDER) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = name;
    b.setAttribute("role", "tab");
    if (name === activeName) b.classList.add("active");
    b.onclick = () => setActiveView(name);
    seg.appendChild(b);
  }
  /* Custom-view dropdown */
  const wrap = document.getElementById("cv-custom-wrap");
  wrap.innerHTML = "";
  const customNames = Object.keys(STATE.customViews || {}).sort();
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "cv-custom-btn";
  const isCustomActive = !isBuiltinView(activeName);
  if (isCustomActive) btn.classList.add("active");
  btn.innerHTML = `<span>${isCustomActive ? escapeHtml(activeName) : "Custom"}</span><span class="cv-caret">▾</span>`;
  const dd = document.createElement("div");
  dd.className = "cv-custom-dropdown";
  if (!customNames.length) {
    const e = document.createElement("div");
    e.className = "cv-cv-empty";
    e.textContent = "No saved views yet.";
    dd.appendChild(e);
  } else {
    for (const name of customNames) {
      const row = document.createElement("div");
      row.className = "cv-cv-row" + (name === activeName ? " active" : "");
      const lbl = document.createElement("span");
      lbl.textContent = name;
      lbl.style.flex = "1";
      lbl.onclick = () => { dd.classList.remove("open"); setActiveView(name); };
      const del = document.createElement("button");
      del.className = "cv-cv-del"; del.type = "button"; del.textContent = "×";
      del.title = "Delete view";
      del.onclick = async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete view "${name}"?`)) return;
        try {
          const r = await fetch(`/api/column-views/${encodeURIComponent(name)}`, {method: "DELETE"});
          if (r.ok) {
            const j = await r.json();
            STATE.customViews = j.custom || {};
            STATE.builtinOverrides = j.builtin_overrides || STATE.builtinOverrides;
            if (STATE.activeViewName === name) STATE.activeViewName = j.active || "Default";
            render();
          }
        } catch (err) {}
      };
      row.appendChild(lbl); row.appendChild(del);
      dd.appendChild(row);
    }
  }
  btn.onclick = (e) => {
    e.stopPropagation();
    dd.classList.toggle("open");
  };
  document.addEventListener("click", () => dd.classList.remove("open"), {once: true});
  wrap.appendChild(btn); wrap.appendChild(dd);
  /* "Modified" pill — shown when the active built-in differs from factory,
     exposing Save-as-new and Reset-to-default. Custom views have no factory
     to diverge from, so no pill. */
  const dirty = document.getElementById("cv-dirty");
  if (dirty) dirty.hidden = !builtinHasOverride(activeName);
  const fitBtn = document.getElementById("cv-fit-toggle");
  if (fitBtn) {
    fitBtn.classList.toggle("active", !!STATE.fitColumns);
    fitBtn.setAttribute("aria-pressed", STATE.fitColumns ? "true" : "false");
  }
}

/* Reset the active built-in to its factory definition by dropping its
   server-side override (columns + all color overrides). */
async function resetViewOverride() {
  const name = normalizeBuiltinViewName(STATE.activeViewName);
  if (!isBuiltinView(name) || !builtinHasOverride(name)) return;
  try {
    const r = await fetch(`/api/column-views/${encodeURIComponent(name)}`, {method: "DELETE"});
    if (r.ok) {
      const j = await r.json();
      STATE.builtinOverrides = j.builtin_overrides || {};
      STATE.customViews = j.custom || STATE.customViews;
    }
  } catch (e) {}
  render();
}

async function promptAndSaveCurrentAsNew() {
  const suggested = isBuiltinView(STATE.activeViewName)
    ? `${STATE.activeViewName} (custom)` : `${STATE.activeViewName} copy`;
  const name = (prompt("Save current column layout as:", suggested) || "").trim();
  if (!name) return;
  if (isBuiltinView(name)) { alert(`"${name}" is a built-in name; pick another.`); return; }
  const columns = currentActiveKeys();
  const heat = getViewHeat(STATE.activeViewName);  // snapshot the live color config
  try {
    const r = await fetch("/api/column-views", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name, columns, heat}),
    });
    if (!r.ok) { const j = await r.json().catch(()=>({})); alert(j.error || "Save failed."); return; }
    const j = await r.json();
    STATE.customViews = j.custom || STATE.customViews;
    STATE.builtinOverrides = j.builtin_overrides || STATE.builtinOverrides;
    setActiveView(name);
  } catch (e) { alert("Save failed."); }
}

/* ----- Customize modal ----- */
let CV_MODAL_STATE = null;  // {selected: Set<key>, order: string[]}

function openColumnPicker() {
  const bg = document.getElementById("cv-modal-bg");
  const modal = document.getElementById("cv-modal");
  if (!bg || !modal) return;
  const activeKeys = currentActiveKeys();
  const activeSet = new Set(activeKeys);
  /* Build a working order: active keys first (in current order), then
     remaining registry keys appended at the end — then stable-sort the whole
     thing by category (COL_GROUP_ORDER) so the modal's section headers are
     contiguous rather than repeating/interleaved. This does change the
     column order that a bare "Save"/"Update" (with no manual reordering)
     would persist, from the view's original order to a group-bucketed one —
     an accepted, intentional side effect (grouped columns read better in the
     table too), not an oversight. */
  const remaining = COLS.map(c => c.key).filter(k => !activeSet.has(k));
  const combined = activeKeys.concat(remaining);
  const groupRank = (k) => {
    const idx = COL_GROUP_ORDER.indexOf(COL_GROUP[k]);
    return idx === -1 ? COL_GROUP_ORDER.length : idx;
  };
  const order = combined
    .map((k, i) => ({ k, i }))
    .sort((a, b) => (groupRank(a.k) - groupRank(b.k)) || (a.i - b.i))
    .map(x => x.k);
  CV_MODAL_STATE = {
    selected: new Set(activeKeys),
    order,
  };
  /* Every active view — built-in or custom — is now editable in place, so the
     modal always offers "Update <active>" plus "Save as new". */
  const activeName = normalizeBuiltinViewName(STATE.activeViewName);
  const isBuiltinActive = isBuiltinView(activeName);
  modal.innerHTML = `
    <h2>Customize Columns</h2>
    <div class="cv-modal-sub">Toggle which columns appear and drag to reorder. Color-coding controls apply immediately. Editing <b>${escapeHtml(activeName)}</b>${isBuiltinActive ? " (a built-in view — Reset restores its defaults)" : ""}.</div>
    <ul class="cv-list" id="cv-modal-list"></ul>
    <div class="cv-modal-foot">
      <input type="text" class="cv-name-input" id="cv-name-input" placeholder="New name (optional)" value="" />
      <button id="cv-modal-cancel">Cancel</button>
      <button id="cv-modal-update" class="primary">Update "${escapeHtml(activeName)}"</button>
      <button id="cv-modal-save" class="primary">Save as new</button>
    </div>
  `;
  renderColumnPickerList();
  bg.hidden = false;
  bg.classList.add("show");
  document.getElementById("cv-modal-cancel").onclick = closeColumnPicker;
  bg.onclick = (e) => { if (e.target === bg) closeColumnPicker(); };
  document.getElementById("cv-modal-save").onclick = () => saveColumnPicker({asNew: true});
  const upd = document.getElementById("cv-modal-update");
  if (upd) upd.onclick = () => saveColumnPicker({asNew: false});
}

function closeColumnPicker() {
  const bg = document.getElementById("cv-modal-bg");
  if (bg) {
    bg.classList.remove("show");
    bg.hidden = true;
  }
  CV_MODAL_STATE = null;
}

function buildHeatControl(key) {
  const c = COLS_BY_KEY[key];
  if (!c || !c.heat || c.heat.kind === "yo") return null; // no control: no-heat columns + analyst_rating
  if (c.heat.kind === "div") {
    const sw = document.createElement("button");
    sw.type = "button";
    const mode = getHeatMode(key);
    sw.className = "cv-switch" + (mode === "on" ? " on" : "");
    sw.setAttribute("role", "switch");
    sw.setAttribute("aria-checked", mode === "on" ? "true" : "false");
    sw.setAttribute("aria-label", "Color coding");
    sw.onclick = () => {
      const next = getHeatMode(key) === "on" ? "off" : "on";
      setHeatMode(key, next);
      sw.classList.toggle("on", next === "on");
      sw.setAttribute("aria-checked", next === "on" ? "true" : "false");
    };
    return sw;
  }
  if (c.heat.kind === "yo_dyn") {
    const seg = document.createElement("span"); seg.className = "cv-seg";
    const current = getHeatMode(key);
    for (const [val, label] of [["off", "Off"], ["percentile", "Percentile"], ["minmax", "Min-Max"]]) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cv-seg-btn" + (current === val ? " active" : "");
      btn.textContent = label;
      btn.onclick = () => {
        setHeatMode(key, val);
        for (const sib of seg.children) sib.classList.remove("active");
        btn.classList.add("active");
      };
      seg.appendChild(btn);
    }
    return seg;
  }
  return null;
}

function renderColumnPickerList() {
  const ul = document.getElementById("cv-modal-list");
  if (!ul || !CV_MODAL_STATE) return;
  ul.innerHTML = "";
  let lastGroup = null;
  for (const key of CV_MODAL_STATE.order) {
    const c = COLS_BY_KEY[key];
    if (!c) continue;
    const group = COL_GROUP[key] || "Other";
    if (group !== lastGroup) {
      const hdr = document.createElement("li");
      hdr.className = "cv-group-header";
      hdr.textContent = group;
      ul.appendChild(hdr);
      lastGroup = group;
    }
    const li = document.createElement("li");
    li.draggable = true;
    li.dataset.key = key;
    const grip = document.createElement("span"); grip.className = "cv-grip"; grip.textContent = "⋮⋮";
    const cb = document.createElement("input"); cb.type = "checkbox";
    cb.checked = CV_MODAL_STATE.selected.has(key);
    cb.onchange = () => {
      if (cb.checked) CV_MODAL_STATE.selected.add(key);
      else CV_MODAL_STATE.selected.delete(key);
    };
    const lbl = document.createElement("span"); lbl.className = "cv-li-label";
    lbl.textContent = c.label || key;
    const desc = document.createElement("span"); desc.className = "cv-li-desc";
    desc.textContent = COL_INFO_SHORT[key] || "";
    const spacer = document.createElement("span"); spacer.className = "cv-li-spacer";
    const slot = document.createElement("span"); slot.className = "cv-li-ctrl-slot";
    const ctrl = buildHeatControl(key);
    if (ctrl) slot.appendChild(ctrl);
    li.appendChild(grip); li.appendChild(cb); li.appendChild(lbl); li.appendChild(desc);
    li.appendChild(spacer); li.appendChild(slot);
    /* DnD */
    li.addEventListener("dragstart", (ev) => {
      li.classList.add("cv-li-drag");
      ev.dataTransfer.setData("text/plain", key);
      ev.dataTransfer.effectAllowed = "move";
    });
    li.addEventListener("dragend", () => li.classList.remove("cv-li-drag"));
    li.addEventListener("dragover", (ev) => { ev.preventDefault(); li.classList.add("cv-li-over"); });
    li.addEventListener("dragleave", () => li.classList.remove("cv-li-over"));
    li.addEventListener("drop", (ev) => {
      ev.preventDefault();
      li.classList.remove("cv-li-over");
      const fromKey = ev.dataTransfer.getData("text/plain");
      if (!fromKey || fromKey === key) return;
      const arr = CV_MODAL_STATE.order;
      const fromIdx = arr.indexOf(fromKey);
      const toIdx = arr.indexOf(key);
      if (fromIdx < 0 || toIdx < 0) return;
      arr.splice(fromIdx, 1);
      arr.splice(toIdx, 0, fromKey);
      renderColumnPickerList();
    });
    ul.appendChild(li);
  }
}

async function saveColumnPicker({asNew}) {
  if (!CV_MODAL_STATE) return;
  const cols = CV_MODAL_STATE.order.filter(k => CV_MODAL_STATE.selected.has(k));
  if (!cols.length) { alert("Select at least one column."); return; }
  if (!cols.includes("symbol")) {
    if (!confirm("This view doesn't include the Ticker column. Save anyway?")) return;
  }
  let name, heat;
  const inputVal = (document.getElementById("cv-name-input").value || "").trim();
  if (asNew) {
    name = inputVal;
    if (!name) { alert("Enter a name for the new view."); return; }
    if (isBuiltinView(name)) { alert(`"${name}" is a built-in name; pick another.`); return; }
    /* A brand-new custom view captures the full live color config (it has no
       factory to inherit from). */
    heat = getViewHeat(STATE.activeViewName);
  } else {
    name = normalizeBuiltinViewName(STATE.activeViewName);
    /* Updating an existing view persists the column change while preserving
       the view's already-saved explicit color overrides (color controls apply
       immediately, so they're saved before this runs). */
    heat = isBuiltinView(name)
      ? ((STATE.builtinOverrides[name] || {}).heat || {})
      : ((STATE.customViews[name] || {}).heat || {});
  }
  try {
    const r = await fetch("/api/column-views", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name, columns: cols, heat}),
    });
    if (!r.ok) { const j = await r.json().catch(()=>({})); alert(j.error || "Save failed."); return; }
    const j = await r.json();
    STATE.customViews = j.custom || STATE.customViews;
    STATE.builtinOverrides = j.builtin_overrides || STATE.builtinOverrides;
    closeColumnPicker();
    setActiveView(name);
  } catch (e) { alert("Save failed."); }
}

function wireColumnViewBar() {
  const cb = document.getElementById("cv-customize");
  if (cb && !cb._wired) { cb.onclick = openColumnPicker; cb._wired = true; }
  const fit = document.getElementById("cv-fit-toggle");
  if (fit && !fit._wired) { fit.onclick = toggleFitColumns; fit._wired = true; }
  const ds = document.getElementById("cv-dirty-save");
  if (ds && !ds._wired) { ds.onclick = promptAndSaveCurrentAsNew; ds._wired = true; }
  const dr = document.getElementById("cv-dirty-reset");
  if (dr && !dr._wired) { dr.onclick = resetViewOverride; dr._wired = true; }
}

/* ----- Header drag-and-drop reordering ----- */
function wireHeaderDnD(tr) {
  const ths = Array.from(tr.querySelectorAll("th"));
  for (const th of ths) {
    th.draggable = true;
    th.addEventListener("dragstart", (ev) => {
      th._dragStartX = ev.clientX;
      th.classList.add("cv-th-drag");
      ev.dataTransfer.setData("text/plain", th.dataset.colKey);
      ev.dataTransfer.effectAllowed = "move";
    });
    th.addEventListener("dragend", () => {
      th.classList.remove("cv-th-drag");
      /* Suppress the click that fires when a drag ends on the same th. */
      th._suppressClick = true;
      setTimeout(() => { th._suppressClick = false; }, 0);
    });
    th.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      th.classList.add("cv-th-over");
    });
    th.addEventListener("dragleave", () => th.classList.remove("cv-th-over"));
    th.addEventListener("drop", (ev) => {
      ev.preventDefault();
      th.classList.remove("cv-th-over");
      const fromKey = ev.dataTransfer.getData("text/plain");
      const toKey = th.dataset.colKey;
      if (!fromKey || !toKey || fromKey === toKey) return;
      reorderActiveColumns(fromKey, toKey);
    });
  }
}

function reorderActiveColumns(fromKey, toKey) {
  const cur = currentActiveKeys();
  const fromIdx = cur.indexOf(fromKey);
  const toIdx = cur.indexOf(toKey);
  if (fromIdx < 0 || toIdx < 0) return;
  cur.splice(fromIdx, 1);
  cur.splice(toIdx, 0, fromKey);
  const name = normalizeBuiltinViewName(STATE.activeViewName);
  if (isBuiltinView(name)) {
    /* Built-in views are now editable in place: persist the new order as a
       per-view override immediately (preserving any explicit color overrides). */
    const ov = STATE.builtinOverrides[name] || {};
    ov.columns = cur;
    STATE.builtinOverrides[name] = ov;
    render();
    persistColumnView(name, cur, ov.heat || {});
  } else {
    /* Custom view: persist new order immediately. */
    if (STATE.customViews[name]) {
      STATE.customViews[name].columns = cur;
    }
    render();
    debouncedSaveCustom(name, cur, (STATE.customViews[name] || {}).heat || {});
  }
}


/* Sort menu removed — column-header click handles sorting. */

/* ===========================================================================
 * Render
 * --------------------------------------------------------------------------- */
function renderHeader(scale = 1) {
  const tr = $("#thead"); tr.innerHTML = "";
  const cols = getActiveColumns();
  for (const c of cols) {
    const th = document.createElement("th");
    th.textContent = c.label || "";
    const width = Math.round((c.w || 80) * scale);
    th.style.minWidth = width + "px";
    th.style.width = width + "px";
    th.dataset.colKey = c.key;
    if (!c.sortable) th.classList.add("no-sort");
    if (c.sortable) {
      th.onclick = (ev) => {
        /* Suppress click that fires at the end of a drag-reorder gesture. */
        if (th._suppressClick) { th._suppressClick = false; return; }
        if (SORT.key === c.key) SORT.dir *= -1;
        else { SORT.key = c.key; SORT.dir = defaultSortDirFor(c.key); }
        render();
      };
      if (SORT.key === c.key) {
        const a = document.createElement("span"); a.className = "arrow";
        a.textContent = SORT.dir > 0 ? "▲" : "▼"; th.appendChild(a);
      }
    }
    if (COL_INFO[c.key]) {
      th.setAttribute("data-tip", COL_INFO[c.key]);
      th.setAttribute("aria-label", (c.label || c.key) + ": " + COL_INFO[c.key]);
    }
    tr.appendChild(th);
  }
  if (typeof wireHeaderDnD === "function") wireHeaderDnD(tr);
}

function render() {
  ensureSortKey();
  const cols = getActiveColumns();
  const scale = applyTableFitMode(cols);
  renderHeader(scale);
  if (typeof renderColumnViewBar === "function") renderColumnViewBar();
  const tbody = $("#tbody"); tbody.innerHTML = "";
  if (!DATA.length) {
    tbody.innerHTML = `<tr><td colspan="${cols.length}" style="padding:30px; text-align:center; color:var(--muted);">Press <b>Build Dashboard</b> above to load your portfolio.</td></tr>`;
    return;
  }
  let rows = DATA.slice();
  if (SORT.key) {
    const col = COLS_BY_KEY[SORT.key];
    const getter = col && col.sortValue ? col.sortValue : (r) => r[SORT.key];
    rows.sort((a, b) => {
      const av = getter(a), bv = getter(b);
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * SORT.dir;
      return String(av).localeCompare(String(bv)) * SORT.dir;
    });
  }
  const theme = getTheme();
  /* Per-column heat context, computed once per render across the live rows
     currently being shown — NOT a fixed clip range, so the ramp always
     reflects what's actually on screen right now. Mode ("off" / "percentile"
     / "minmax") comes from the user's per-column preference (getHeatMode);
     ctx === null means "don't color this column at all" (Off, or a "div"
     column the user switched off), which the per-cell loop below skips. */
  const heatCtx = {};
  for (const c of cols) {
    if (!c.heat) continue;
    const mode = getHeatMode(c.key);
    if (mode === "off") { heatCtx[c.key] = null; continue; }
    if (c.heat.kind === "yo_dyn") {
      const vals = rows.map(r => r[c.key]).filter(v => v != null && isFinite(v));
      if (vals.length < 2) { heatCtx[c.key] = null; continue; }
      const sorted = vals.slice().sort((a, b) => a - b);
      heatCtx[c.key] = (mode === "percentile")
        ? { mode: "percentile", sorted }
        : { mode: "minmax", lo: percentileOf(sorted, 0.1), hi: percentileOf(sorted, 0.9) };
    } else {
      heatCtx[c.key] = {}; // "div" (on) or "yo" (analyst_rating, uncontrolled) — proceed as-is
    }
  }
  for (const r of rows) {
    const tr = document.createElement("tr");
    for (const c of cols) {
      const td = document.createElement("td");
      if (c.align === "left") td.classList.add("left");
      else if (c.align === "center") td.classList.add("center");
      if (c.td_cls) td.className = c.td_cls;
      let styles = "";
      const ctx = c.heat ? heatCtx[c.key] : undefined;
      if (c.heat && ctx !== null) styles += cellStyleHeat(c, r[c.key], theme, ctx);
      if (c.bg)   styles += c.bg(r);
      /* Mark cells that should follow the alt-row stripe (no heat / no custom bg). */
      if ((!c.heat || ctx === null) && !c.bg) td.classList.add("alt-stripe");
      if (styles) td.style.cssText = styles;
      if (r.error && c.key !== "symbol" && c.key !== "name" && c.key !== "logo") {
        td.innerHTML = c.key === "price"
          ? `<span style="color:var(--neg)">${escapeHtml(r.error)}</span>` : "";
      } else {
        td.innerHTML = c.render(r);
      }
      tr.appendChild(td);
    }
    tr.onclick = () => openModal(r);
    tbody.appendChild(tr);
  }
}

/* ===========================================================================
 * Modal detail view
 * --------------------------------------------------------------------------- */
/* ----- Detail modal state ----- */
const DETAIL = {
  data: null,           // detail payload from /api/detail
  row: null,            // original row data (fallback while loading)
  range: "1Y",          // active range tab
  showSP: false,        // overlay S&P 500
  showSector: false,    // overlay sector ETF
  showVol: true,        // volume bars
  // chart geometry — rebuilt every render
  geom: null,
};
const RANGES = ["1M","3M","6M","YTD","1Y","5Y","MAX"];

function openModal(r) {
  if (r.error) return;
  DETAIL.data = null; DETAIL.row = r;
  DETAIL.range = "1Y"; DETAIL.showSP = false; DETAIL.showSector = false; DETAIL.showVol = true;
  renderModalSkeleton();
  $("#modal-bg").classList.add("show");
  fetch("/api/detail?symbol=" + encodeURIComponent(r.symbol))
    .then(res => res.json())
    .then(d => {
      if (d && !d.error) {
        DETAIL.data = d;
        renderModalFull();
      } else {
        $("#m-loading").textContent = "Failed to load detail: " + (d.error || "unknown");
      }
    })
    .catch(e => { $("#m-loading").textContent = "Network error: " + e.message; });
}
function closeModal() { $("#modal-bg").classList.remove("show"); DETAIL.data = null; }
$("#modal-bg").addEventListener("click", (e) => { if (e.target.id === "modal-bg") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeInfo(); closeNsProgress(); } });

function renderModalSkeleton() {
  const r = DETAIL.row;
  const pc = r.pct_1d;
  const chgCls = (pc != null && pc >= 0) ? "pos" : "neg";
  const sign = (pc != null && pc >= 0) ? "▲" : "▼";
  const change = (r.change_abs_1d != null)
    ? (r.change_abs_1d >= 0 ? "+" : "−") + fmtMoney(Math.abs(r.change_abs_1d), r.currency)
    : "—";
  $("#modal").innerHTML = `
    <div class="m-head">
      ${logoImg(r.symbol)}
      <div class="m-title">
        <h2><span class="ticker">${r.symbol}</span> — ${escapeHtml(r.name || "")}</h2>
        <div class="meta">
          ${[r.exchange, r.sector, r.industry, r.currency].filter(Boolean).map(escapeHtml).join(" · ")}
          ${r.website ? ` · <a href="${escapeHtml(r.website)}" target="_blank" rel="noopener">website ↗</a>` : ""}
        </div>
      </div>
      <div class="m-price-block">
        <div class="p-now">${fmtMoney(r.price, r.currency)}</div>
        <div class="p-chg ${chgCls}">${sign} ${fmtPctSigned(pc)} <span style="opacity:0.7">(${change})</span></div>
      </div>
      <button class="m-close" onclick="closeModal()" title="Close">×</button>
    </div>
    <div class="m-chart-wrap">
      <div class="m-chart-toolbar">
        <div class="m-range-tabs" id="m-range-tabs">
          ${RANGES.map(rg => `<button data-range="${rg}" class="${rg === DETAIL.range ? "active" : ""}">${rg}</button>`).join("")}
        </div>
        <span class="m-toolbar-spacer"></span>
        <button class="m-toolbar-btn" id="m-toggle-sp" title="Compare to S&P 500"><span class="dot sp"></span>S&amp;P 500</button>
        <button class="m-toolbar-btn" id="m-toggle-sec" title="Compare to sector ETF"><span class="dot sec"></span>Sector</button>
        <button class="m-toolbar-btn active" id="m-toggle-vol" title="Toggle volume bars">Volume</button>
      </div>
      <div class="m-chart" id="m-chart">
        <div id="m-loading" style="position:absolute; inset:0; display:flex; align-items:center; justify-content:center;">${lcHtml("fetching detail", {bar: true})}</div>
      </div>
      <div class="m-range-info" id="m-range-info" style="display:none;"></div>
    </div>
    <div class="m-sections" id="m-sections"></div>
  `;
  $("#m-range-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    DETAIL.range = b.dataset.range;
    renderModalFull();
  });
  $("#m-toggle-sp").onclick = () => { DETAIL.showSP = !DETAIL.showSP; renderModalFull(); };
  $("#m-toggle-sec").onclick = () => { DETAIL.showSector = !DETAIL.showSector; renderModalFull(); };
  $("#m-toggle-vol").onclick = () => { DETAIL.showVol = !DETAIL.showVol; renderModalFull(); };
}

function renderModalFull() {
  if (!DETAIL.data) return;
  // Update toolbar active states
  document.querySelectorAll("#m-range-tabs button").forEach(b => {
    b.classList.toggle("active", b.dataset.range === DETAIL.range);
  });
  $("#m-toggle-sp")?.classList.toggle("active", DETAIL.showSP);
  $("#m-toggle-sec")?.classList.toggle("active", DETAIL.showSector);
  $("#m-toggle-vol")?.classList.toggle("active", DETAIL.showVol);
  // Hide sector toggle if no sector ETF data
  if (!DETAIL.data.benchmark_sector || !DETAIL.data.benchmark_sector.length) {
    $("#m-toggle-sec").style.display = "none";
    DETAIL.showSector = false;
  } else {
    $("#m-toggle-sec").title = "Compare to " + (DETAIL.data.sector_etf || "sector ETF");
  }
  renderChart();
  renderSections();
}

/* ---- Chart rendering with crosshair + selection ---- */
function sliceHistory(pts, range) {
  if (!pts || !pts.length) return [];
  if (range === "MAX") return pts;
  const last = pts[pts.length - 1][0];
  const d = new Date(last);
  let cutoff;
  if (range === "YTD") cutoff = new Date(d.getFullYear(), 0, 1).getTime();
  else {
    const months = { "1M": 1, "3M": 3, "6M": 6, "1Y": 12, "5Y": 60 }[range] || 12;
    const c = new Date(d); c.setMonth(c.getMonth() - months); cutoff = c.getTime();
  }
  return pts.filter(p => p[0] >= cutoff);
}

function normalizedTo(pts, startVal) {
  if (!pts.length) return [];
  const base = pts[0][1];
  return pts.map(p => [p[0], (p[1] / base) * startVal]);
}

function renderChart() {
  const d = DETAIL.data;
  const wrap = $("#m-chart");
  const range = DETAIL.range;
  const stock = sliceHistory(d.history, range);
  if (stock.length < 2) {
    wrap.innerHTML = `<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);">No data for ${range}.</div>`;
    return;
  }

  // Slice volume + benchmarks aligned to stock's window
  const t0 = stock[0][0], t1 = stock[stock.length - 1][0];
  const vol = (d.volume || []).filter(p => p[0] >= t0 && p[0] <= t1);
  let spy = null, sec = null;
  if (DETAIL.showSP && d.benchmark_spy) {
    const s = d.benchmark_spy.filter(p => p[0] >= t0 && p[0] <= t1);
    if (s.length >= 2) spy = normalizedTo(s, stock[0][1]);
  }
  if (DETAIL.showSector && d.benchmark_sector) {
    const s = d.benchmark_sector.filter(p => p[0] >= t0 && p[0] <= t1);
    if (s.length >= 2) sec = normalizedTo(s, stock[0][1]);
  }

  // Geometry
  const W = wrap.clientWidth || 800;
  const H = 320;
  const padL = 48, padR = 10, padT = 12, padB = DETAIL.showVol && vol.length ? 60 : 22;
  const chartH = H - padT - padB;
  const xScale = (t) => padL + ((t - t0) / Math.max(1, t1 - t0)) * (W - padL - padR);

  // y range from stock + visible benchmarks
  let lo = Infinity, hi = -Infinity;
  for (const p of stock) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (spy) for (const p of spy) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (sec) for (const p of sec) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  const rng = (hi - lo) || 1;
  const padPct = 0.05;
  lo -= rng * padPct; hi += rng * padPct;
  const yScale = (v) => padT + (1 - (v - lo) / (hi - lo)) * chartH;

  // Volume scale
  let volMax = 1;
  if (vol.length) for (const p of vol) if (p[1] > volMax) volMax = p[1];
  const volH = 32;
  const volTop = H - padB + 18;
  const volScale = (v) => (v / volMax) * volH;

  // Main path
  const buildPath = (pts) => {
    let s = "";
    for (let i = 0; i < pts.length; i++) {
      const x = xScale(pts[i][0]).toFixed(1);
      const y = yScale(pts[i][1]).toFixed(1);
      s += (i === 0 ? "M" : "L") + x + "," + y + " ";
    }
    return s;
  };
  const stockPath = buildPath(stock);
  const stockUp = stock[stock.length - 1][1] >= stock[0][1];
  const css = getComputedStyle(document.documentElement);
  const posCol = css.getPropertyValue("--pos").trim();
  const negCol = css.getPropertyValue("--neg").trim();
  const posRgb = css.getPropertyValue("--pos-rgb").trim();
  const negRgb = css.getPropertyValue("--neg-rgb").trim();
  const stroke = stockUp ? posCol : negCol;
  const fillRgb = stockUp ? posRgb : negRgb;
  const baseY = yScale(lo);
  const area = stockPath + ` L ${xScale(t1).toFixed(1)},${baseY.toFixed(1)} L ${xScale(t0).toFixed(1)},${baseY.toFixed(1)} Z`;

  // Y-axis ticks (4)
  const ticks = [];
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * (i / 4);
    ticks.push({ v, y: yScale(v) });
  }
  // X-axis ticks (4-6 evenly spaced)
  const xTicks = [];
  const N_XT = 5;
  for (let i = 0; i <= N_XT; i++) {
    const t = t0 + (t1 - t0) * (i / N_XT);
    xTicks.push({ t, x: xScale(t) });
  }
  const fmtTickDate = (ts) => {
    const dt = new Date(ts);
    if (range === "1M" || range === "3M") return dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    if (range === "6M" || range === "YTD" || range === "1Y") return dt.toLocaleDateString(undefined, { month: "short", year: "2-digit" });
    return dt.toLocaleDateString(undefined, { year: "numeric" });
  };
  const fmtTickVal = (v) => {
    if (v >= 1000) return v.toFixed(0);
    if (v >= 100) return v.toFixed(1);
    return v.toFixed(2);
  };

  // Volume bars
  let volSvg = "";
  if (DETAIL.showVol && vol.length) {
    const bw = Math.max(1, (W - padL - padR) / Math.max(vol.length, 1) - 0.5);
    volSvg = vol.map(p => {
      const x = xScale(p[0]) - bw/2;
      const h = volScale(p[1]);
      return `<rect x="${x.toFixed(1)}" y="${(volTop + volH - h).toFixed(1)}" width="${bw.toFixed(2)}" height="${h.toFixed(1)}" fill="rgba(${fillRgb},0.35)"/>`;
    }).join("");
  }

  const spyPath = spy ? buildPath(spy) : null;
  const secPath = sec ? buildPath(sec) : null;

  wrap.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" id="m-svg">
      <defs>
        <linearGradient id="g-area" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="rgba(${fillRgb},0.28)"/>
          <stop offset="100%" stop-color="rgba(${fillRgb},0.02)"/>
        </linearGradient>
      </defs>
      ${ticks.map(t => `<line x1="${padL}" y1="${t.y.toFixed(1)}" x2="${W-padR}" y2="${t.y.toFixed(1)}" stroke="var(--border)" stroke-width="0.5" stroke-dasharray="2 3"/>`).join("")}
      ${ticks.map(t => `<text x="${padL-6}" y="${t.y+3}" font-size="10" fill="var(--muted)" text-anchor="end">${fmtTickVal(t.v)}</text>`).join("")}
      ${xTicks.map(t => `<text x="${t.x}" y="${H-padB+12}" font-size="10" fill="var(--muted)" text-anchor="middle">${fmtTickDate(t.t)}</text>`).join("")}
      <path d="${area}" fill="url(#g-area)"/>
      ${spyPath ? `<path d="${spyPath}" fill="none" stroke="#8b5cf6" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
      ${secPath ? `<path d="${secPath}" fill="none" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
      <path d="${stockPath}" fill="none" stroke="${stroke}" stroke-width="1.8"/>
      ${volSvg}
      <rect class="sel-rect" id="m-sel" x="0" y="0" width="0" height="${chartH}" style="display:none"/>
      <line class="crosshair-line" id="m-cross-v" x1="0" y1="${padT}" x2="0" y2="${padT+chartH}"/>
      <line class="crosshair-line" id="m-cross-h" x1="${padL}" y1="0" x2="${W-padR}" y2="0"/>
      <circle class="crosshair-dot" id="m-dot" r="4" cx="0" cy="0"/>
      ${spyPath ? `<circle class="crosshair-dot sp" id="m-dot-sp" r="3.5" cx="0" cy="0"/>` : ""}
      ${secPath ? `<circle class="crosshair-dot sec" id="m-dot-sec" r="3.5" cx="0" cy="0"/>` : ""}
      <rect id="m-overlay" x="${padL}" y="${padT}" width="${W-padL-padR}" height="${chartH}" fill="transparent" style="cursor:crosshair"/>
    </svg>
    <div class="m-tooltip" id="m-tt"></div>
  `;

  // Save geometry + data for interaction
  DETAIL.geom = { W, H, padL, padR, padT, padB, chartH, xScale, yScale, stock, spy, sec, t0, t1, fillRgb };

  // Range return summary (when not dragging)
  const sPct = ((stock[stock.length-1][1] / stock[0][1] - 1) * 100);
  const sCls = sPct >= 0 ? "pos" : "neg";
  let parts = [`<span><b class="${sCls}">${(sPct>=0?"+":"")+sPct.toFixed(2)}%</b> · ${range} (${DETAIL.data.symbol})</span>`];
  if (spy) {
    const p = (spy[spy.length-1][1] / spy[0][1] - 1) * 100;
    parts.push(`<span><b class="${p>=0?"pos":"neg"}">${(p>=0?"+":"")+p.toFixed(2)}%</b> · S&amp;P 500</span>`);
  }
  if (sec) {
    const p = (sec[sec.length-1][1] / sec[0][1] - 1) * 100;
    parts.push(`<span><b class="${p>=0?"pos":"neg"}">${(p>=0?"+":"")+p.toFixed(2)}%</b> · ${DETAIL.data.sector_etf || "Sector"}</span>`);
  }
  parts.push(`<span style="margin-left:auto">${fmtDateMDY(t0)} → ${fmtDateMDY(t1)}</span>`);
  const info = $("#m-range-info");
  info.innerHTML = parts.join("");
  info.style.display = "flex";
  info.dataset.default = "1";

  attachChartInteraction();
}

function attachChartInteraction() {
  const svg = $("#m-svg");
  const overlay = $("#m-overlay");
  const tt = $("#m-tt");
  const cv = $("#m-cross-v"), ch = $("#m-cross-h"), dot = $("#m-dot");
  const dotSp = $("#m-dot-sp"), dotSec = $("#m-dot-sec");
  const sel = $("#m-sel");
  const wrap = $("#m-chart");
  const info = $("#m-range-info");
  const g = DETAIL.geom;

  function pxToData(px) {
    // px is in svg viewBox units → matches our coords
    const tx = g.t0 + (px - g.padL) / (g.W - g.padL - g.padR) * (g.t1 - g.t0);
    return tx;
  }
  function nearestIdx(pts, t) {
    if (!pts.length) return -1;
    let lo = 0, hi = pts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (pts[mid][0] < t) lo = mid + 1; else hi = mid;
    }
    if (lo > 0 && Math.abs(pts[lo - 1][0] - t) < Math.abs(pts[lo][0] - t)) return lo - 1;
    return lo;
  }
  function clientToSvgX(clientX) {
    const r = svg.getBoundingClientRect();
    return (clientX - r.left) * (g.W / r.width);
  }

  let dragging = false, dragStartT = null;

  overlay.addEventListener("mousemove", (e) => {
    const svgX = clientToSvgX(e.clientX);
    const t = pxToData(Math.max(g.padL, Math.min(g.W - g.padR, svgX)));
    const i = nearestIdx(g.stock, t);
    if (i < 0) return;
    const sp = g.stock[i];
    const x = g.xScale(sp[0]);
    const y = g.yScale(sp[1]);
    cv.setAttribute("x1", x); cv.setAttribute("x2", x); cv.style.opacity = 1;
    ch.setAttribute("y1", y); ch.setAttribute("y2", y); ch.style.opacity = 1;
    dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.style.opacity = 1;
    let extra = "";
    if (dotSp && g.spy) {
      const j = nearestIdx(g.spy, sp[0]);
      if (j >= 0) {
        const px = g.xScale(g.spy[j][0]); const py = g.yScale(g.spy[j][1]);
        dotSp.setAttribute("cx", px); dotSp.setAttribute("cy", py); dotSp.style.opacity = 1;
        const pct = (g.spy[j][1] / g.spy[0][1] - 1) * 100;
        extra += `<div class="tt-row"><span class="tt-label"><span class="dot sp" style="width:8px;height:8px;background:#8b5cf6;border-radius:50%;display:inline-block"></span>S&amp;P</span><b class="${pct>=0?'pos':'neg'}" style="color:${pct>=0?'var(--pos)':'var(--neg)'}">${(pct>=0?'+':'')+pct.toFixed(2)}%</b></div>`;
      }
    }
    if (dotSec && g.sec) {
      const j = nearestIdx(g.sec, sp[0]);
      if (j >= 0) {
        const px = g.xScale(g.sec[j][0]); const py = g.yScale(g.sec[j][1]);
        dotSec.setAttribute("cx", px); dotSec.setAttribute("cy", py); dotSec.style.opacity = 1;
        const pct = (g.sec[j][1] / g.sec[0][1] - 1) * 100;
        extra += `<div class="tt-row"><span class="tt-label"><span style="width:8px;height:8px;background:#f59e0b;border-radius:50%;display:inline-block"></span>${DETAIL.data.sector_etf||'Sector'}</span><b style="color:${pct>=0?'var(--pos)':'var(--neg)'}">${(pct>=0?'+':'')+pct.toFixed(2)}%</b></div>`;
      }
    }
    const stockPct = (sp[1] / g.stock[0][1] - 1) * 100;
    const dt = new Date(sp[0]);
    tt.innerHTML = `
      <div class="tt-date">${fmtDateMDY(dt)}</div>
      <div class="tt-row"><span class="tt-label">Price</span><b>${fmtMoney(sp[1], DETAIL.data && DETAIL.data.currency)}</b></div>
      <div class="tt-row"><span class="tt-label">${DETAIL.data.symbol}</span><b style="color:${stockPct>=0?'var(--pos)':'var(--neg)'}">${(stockPct>=0?'+':'')+stockPct.toFixed(2)}%</b></div>
      ${extra}
    `;
    tt.classList.add("show");
    // Position tooltip near cursor
    const wrapRect = wrap.getBoundingClientRect();
    const lx = e.clientX - wrapRect.left + 12;
    const ly = e.clientY - wrapRect.top - 8;
    const ttRect = tt.getBoundingClientRect();
    const maxX = wrap.clientWidth - ttRect.width - 6;
    tt.style.left = Math.min(lx, Math.max(6, maxX)) + "px";
    tt.style.top = Math.max(6, ly) + "px";

    if (dragging && dragStartT != null) {
      const a = Math.min(dragStartT, sp[0]), b = Math.max(dragStartT, sp[0]);
      const ax = g.xScale(a), bx = g.xScale(b);
      sel.style.display = "";
      sel.setAttribute("x", ax);
      sel.setAttribute("y", g.padT);
      sel.setAttribute("width", Math.max(1, bx - ax));
      // Update range-info to show drag return
      const iA = nearestIdx(g.stock, a), iB = nearestIdx(g.stock, b);
      if (iA >= 0 && iB >= 0 && iA !== iB) {
        const va = g.stock[iA][1], vb = g.stock[iB][1];
        const pct = (vb/va - 1) * 100;
        const cls = pct >= 0 ? "pos" : "neg";
        let drag = `<span><b class="${cls}">${(pct>=0?"+":"")+pct.toFixed(2)}%</b> · selection</span>`;
        if (g.spy) {
          const jA = nearestIdx(g.spy, a), jB = nearestIdx(g.spy, b);
          if (jA >= 0 && jB >= 0 && jA !== jB) {
            const p = (g.spy[jB][1]/g.spy[jA][1] - 1) * 100;
            drag += `<span><b class="${p>=0?'pos':'neg'}">${(p>=0?'+':'')+p.toFixed(2)}%</b> · S&amp;P</span>`;
          }
        }
        drag += `<span style="margin-left:auto">${fmtDateMDY(g.stock[iA][0])} → ${fmtDateMDY(g.stock[iB][0])}</span>`;
        info.innerHTML = drag;
      }
    }
  });
  overlay.addEventListener("mouseleave", () => {
    cv.style.opacity = 0; ch.style.opacity = 0; dot.style.opacity = 0;
    if (dotSp) dotSp.style.opacity = 0;
    if (dotSec) dotSec.style.opacity = 0;
    tt.classList.remove("show");
  });
  overlay.addEventListener("mousedown", (e) => {
    dragging = true;
    const svgX = clientToSvgX(e.clientX);
    dragStartT = pxToData(Math.max(g.padL, Math.min(g.W - g.padR, svgX)));
    sel.style.display = "";
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false; dragStartT = null;
    // Keep selection visible briefly, then revert range-info to defaults
    setTimeout(() => {
      sel.style.display = "none";
      // Restore range summary
      renderChart();
    }, 1800);
  });
}

/* ---- Information sections ---- */
function fmtPctRaw(v, digits) {
  if (v == null || !isFinite(v)) return "—";
  const s = v >= 0 ? "+" : "";
  return s + Number(v).toFixed(digits == null ? 2 : digits) + "%";
}
function pctCell(v) {
  if (v == null || !isFinite(v)) return `<td class="na">—</td>`;
  const cls = v >= 0 ? "pos" : "neg";
  return `<td class="${cls}">${(v>=0?"+":"")+v.toFixed(2)}%</td>`;
}
const DETAIL_METRIC_INFO = {
  "Volume": {
    formula: String.raw`\text{shares traded today}`,
    desc: "Total shares traded in the latest session. It is a liquidity read, not a valuation signal.",
    range: "Compare it with Avg Volume. A large spike often means news, earnings, rebalancing, or stress."
  },
  "Avg Volume": {
    formula: String.raw`\overline{V} = \dfrac{1}{n}\sum_{t=1}^{n} V_t`,
    desc: "Average daily trading volume over Yahoo's lookback window, used as the baseline liquidity reference.",
    range: "If current volume is far above this level, participation is unusual and the move may be more informative."
  },
  "52W High": {
    formula: String.raw`\max(P_t),\; t \in \text{last 52 weeks}`,
    desc: "Highest price reached in the trailing 52-week window.",
    range: "Names near the high are often in strong trends; deep gaps below it indicate a prior drawdown."
  },
  "52W Low": {
    formula: String.raw`\min(P_t),\; t \in \text{last 52 weeks}`,
    desc: "Lowest price reached in the trailing 52-week window.",
    range: "Useful for judging whether a stock is still washed out or already recovering from its low."
  },
  "ATH": {
    formula: String.raw`\max(P_t),\; t \in \text{available history}`,
    desc: "Highest price in the full available price history, not just the trailing year.",
    range: "The gap between spot and ATH is a quick read on how much prior optimism has been unwound."
  },
  "Market Cap": {
    formula: String.raw`MC = P \times \text{Shares Outstanding}`,
    desc: "Total equity value of the company at the current price.",
    range: "Useful for sizing the business and understanding whether you are buying a mega-cap, mid-cap, or micro-cap risk profile."
  },
  "Shares Out": {
    formula: String.raw`\text{issued shares currently outstanding}`,
    desc: "Number of shares currently outstanding. It moves with buybacks, issuance, stock-based comp, and corporate actions.",
    range: "Shrinking share counts support per-share growth; rising counts can dilute existing holders."
  },
  "Beta": {
    formula: String.raw`\beta = \dfrac{\mathrm{Cov}(r_i, r_m)}{\mathrm{Var}(r_m)}`,
    desc: "Yahoo-reported market beta. It estimates how sensitively the stock tends to move versus the market benchmark.",
    range: "Around 1 behaves like the market. Below 1 is more defensive; above 1.3 is usually high-beta growth or cyclicality."
  },
  "Revenue (TTM)": {
    formula: String.raw`\text{sales over the trailing 12 months}`,
    desc: "Trailing-12-month revenue. This is the current annualised top-line scale of the business.",
    range: "Best used with growth and margin metrics. Sales alone say nothing about quality or profitability."
  },
  "Revenue Growth": {
    formula: String.raw`g = \dfrac{\text{Revenue}_{TTM} - \text{Revenue}_{prior}}{\text{Revenue}_{prior}}`,
    desc: "Year-over-year revenue growth rate.",
    range: "Mid-single digits is mature; teens are healthy; 30%+ usually implies high-growth expectations and tougher comps ahead."
  },
  "Free Cash Flow": {
    formula: String.raw`FCF = CFO - CapEx`,
    desc: "Cash left after operating cash flow covers capital expenditures. It is the cleanest internal funding source for buybacks, dividends, and debt paydown.",
    range: "Positive and rising is a quality signal. Negative FCF can be fine in investment-heavy businesses, but it raises the financing burden."
  },
  "FCF Yield": {
    formula: String.raw`FCF\ Yield = \dfrac{FCF}{\text{Market Cap}}`,
    desc: "Free cash flow scaled by equity value. It is the cash-flow analogue of an earnings yield.",
    range: "Low single digits is common for quality growth. High single digits can mean cheap cash generation or market skepticism."
  },
  "Fwd P/E": {
    formula: String.raw`\text{Fwd P/E} = \dfrac{P}{\text{EPS}_{NTM}}`,
    desc: "Price divided by consensus next-12-month earnings per share.",
    range: "Lower than trailing P/E can indicate expected earnings growth; higher can mean analysts see an earnings dip ahead."
  },
  "P/E (TTM)": {
    formula: String.raw`P/E = \dfrac{P}{\text{EPS}_{TTM}}`,
    desc: "Trailing price-to-earnings multiple using the last 12 months of earnings.",
    range: "Broad-market quality names often live in the high teens to mid-20s. Very high multiples imply strong growth expectations."
  },
  "EV/EBITDA": {
    formula: String.raw`\dfrac{EV}{EBITDA}`,
    desc: "Enterprise value divided by EBITDA. It compares the total business value to an operating cash-earnings proxy.",
    range: "Useful across peers with different debt loads. Higher values usually mean better growth, higher quality, or richer pricing."
  },
  "EV/Revenue": {
    formula: String.raw`\dfrac{EV}{Revenue}`,
    desc: "Enterprise value divided by revenue. Often more informative than earnings multiples when margins are still immature.",
    range: "Best used for software, platforms, and cyclical turnarounds where earnings are temporarily noisy."
  },
  "Operating Mgn": {
    formula: String.raw`\text{Operating Margin} = \dfrac{\text{Operating Income}}{Revenue}`,
    desc: "Share of revenue left after core operating costs, before interest and taxes.",
    range: "Higher usually means stronger business quality, pricing power, or scale. Compare within the same industry, not across all sectors."
  },
  "Gross Margin": {
    formula: String.raw`\text{Gross Margin} = \dfrac{Revenue - COGS}{Revenue}`,
    desc: "Share of revenue left after direct production or delivery costs.",
    range: "High gross margins often signal pricing power or software-like economics, but they need operating discipline to turn into profits."
  },
  "Profit Margin": {
    formula: String.raw`\text{Profit Margin} = \dfrac{\text{Net Income}}{Revenue}`,
    desc: "Net income as a share of revenue after all expenses.",
    range: "A compact measure of business efficiency, but it can swing with tax effects, interest costs, and one-off items."
  },
  "ROE": {
    formula: String.raw`ROE = \dfrac{\text{Net Income}}{\text{Shareholders' Equity}}`,
    desc: "Return on equity measures how efficiently management converts book equity into earnings.",
    range: "Higher is usually better, but leverage can inflate ROE, so read it together with D/E."
  },
  "D/E": {
    formula: String.raw`D/E = \dfrac{\text{Total Debt}}{\text{Shareholders' Equity}}`,
    desc: "Debt-to-equity ratio. It shows how much leverage sits on top of the equity base.",
    range: "Low values imply balance-sheet flexibility. High values can amplify returns in good times and pain in bad times."
  },
};
function metricTipHtml(label, info) {
  if (!info) return "";
  return `
    <div class="pf-metric-tip" role="tooltip">
      <div class="mt-name">${escapeHtml(label)}</div>
      <div class="mt-formula">$$${info.formula}$$</div>
      <div class="mt-desc">${escapeHtml(info.desc)}</div>
      <div class="mt-range">${escapeHtml(info.range)}</div>
    </div>`;
}
function detailMetricCellHtml(item, idx) {
  const label = Array.isArray(item) ? item[0] : item.label;
  const value = Array.isArray(item) ? item[1] : item.value;
  const info = (Array.isArray(item) ? null : item.info) || DETAIL_METRIC_INFO[label];
  const dataAttr = info ? ` data-info="1" data-metric="${escapeHtml(label)}"` : "";
  const sideCls = idx % 2 ? " tip-right" : "";
  return `<div class="metric-tip-host${sideCls}"${dataAttr}><div class="k">${label}</div><div class="v">${value}</div>${metricTipHtml(label, info)}</div>`;
}
function renderDetailMetricGrid(items) {
  return `<div class="m-kv">${items.map((item, idx) => detailMetricCellHtml(item, idx)).join("")}</div>`;
}
function renderSections() {
  const d = DETAIL.data;
  const sec = $("#m-sections");
  const fcfYield = (d.free_cashflow != null && d.market_cap != null && isFinite(d.free_cashflow) && isFinite(d.market_cap) && d.market_cap !== 0)
    ? d.free_cashflow / d.market_cap
    : null;

  // ---- Snapshot
  const snap = [
    ["Volume", fmtCompactNum(d.day_volume)],
    ["Avg Volume", fmtCompactNum(d.avg_volume)],
    ["52W High", fmtMoney(d.w52_high, d.currency)],
    ["52W Low", fmtMoney(d.w52_low, d.currency)],
    ["ATH", fmtMoney(d.ath, d.currency)],
    ["Market Cap", fmtCompactMoney(d.market_cap, d.currency)],
    ["Shares Out", fmtCompactNum(d.shares)],
    ["Beta", fmt2(d.beta)],
  ];

  // ---- Valuation
  const val = [
    ["Revenue (TTM)", fmtCompactMoney(d.total_revenue, d.currency)],
    ["Revenue Growth", fmtPctFrac(d.revenue_growth)],
    ["Free Cash Flow", fmtCompactMoney(d.free_cashflow, d.currency)],
    ["FCF Yield", fmtPctFrac(fcfYield)],
    ["Fwd P/E", fmt2(d.forward_pe)],
    ["P/E (TTM)", fmt2(d.pe)],
    ["EV/EBITDA", fmt2(d.ev_ebitda)],
    ["EV/Revenue", fmt2(d.ev_revenue)],
    ["Operating Mgn", fmtPctFrac(d.operating_margin)],
    ["Gross Margin", fmtPctFrac(d.gross_margin)],
    ["Profit Margin", fmtPctFrac(d.profit_margin)],
    ["ROE", fmtPctFrac(d.roe)],
    ["D/E", fmt2(d.debt_equity)],
  ];

  // ---- Performance table
  const perf = d.performance || {};
  const labels = [["1d","1 Day"],["1w","1 Week"],["1m","1 Month"],["3m","3 Months"],["6m","6 Months"],["ytd","YTD"],["1y","1 Year"],["5y","5 Years"]];
  const sectorETF = d.sector_etf || "Sector";
  const perfRows = labels.map(([k,lbl]) => {
    const row = perf[k] || {};
    const s = row.stock, b = row.spy, c = row.sector;
    const dvs = (s != null && b != null) ? (s - b) : null;
    return `<tr>
      <td>${lbl}</td>
      ${pctCell(s)}${pctCell(b)}${pctCell(c)}
      ${dvs == null ? `<td class="na">—</td>` : `<td class="${dvs>=0?'pos':'neg'}">${(dvs>=0?'+':'')+dvs.toFixed(2)}</td>`}
    </tr>`;
  }).join("");

  const betaC = d.beta_computed, corrC = d.correlation_spy;
  const betaSec = d.beta_sector, corrSec = d.correlation_sector;

  // ---- Analyst
  let analystHtml = "";
  if (d.recommendation_mean != null || d.target_mean != null || (d.recommendations_trend && d.recommendations_trend.length)) {
    const recMean = d.recommendation_mean; // 1=Strong Buy ... 5=Strong Sell
    const recKey = (d.recommendation_key || "").replace("_", " ");
    const keyCls = recKey.includes("buy") ? "buy" : recKey.includes("sell") ? "sell" : "hold";
    // Needle position: 1 → left, 5 → right
    const needlePct = recMean != null ? Math.max(0, Math.min(100, ((recMean - 1) / 4) * 100)) : null;
    // Target bar: position current price between low and high
    let tbHtml = "";
    if (d.target_low != null && d.target_high != null && d.price != null) {
      const lo = Math.min(d.target_low, d.price), hi = Math.max(d.target_high, d.price);
      const pct = v => 6 + ((v - lo) / Math.max(0.0001, (hi - lo))) * 88;
      const upside = d.target_mean != null ? ((d.target_mean / d.price - 1) * 100) : null;
      tbHtml = `
        <div class="m-target-bar">
          <div class="tb-track"></div>
          <span class="tb-low">${fmtMoney(d.target_low, d.currency)}</span>
          <div class="tb-mark current" style="left:${pct(d.price).toFixed(1)}%" title="Current"></div>
          ${d.target_mean != null ? `<div class="tb-mark target" style="left:${pct(d.target_mean).toFixed(1)}%" title="Mean target"></div>` : ""}
          <span class="tb-high">${fmtMoney(d.target_high, d.currency)}</span>
        </div>
        <div class="m-target-labels">
          <span>Current <b>${fmtMoney(d.price, d.currency)}</b></span>
          <span>Mean target <b>${fmtMoney(d.target_mean, d.currency)}</b>
            ${upside != null ? `<span class="upside ${upside>=0?'pos':'neg'}">(${(upside>=0?'+':'')+upside.toFixed(1)}%)</span>` : ""}
          </span>
        </div>
      `;
    }
    // Latest recommendation distribution
    let recBars = "";
    if (d.recommendations_trend && d.recommendations_trend.length) {
      const r = d.recommendations_trend[0];
      const tot = (r.strongBuy||0) + (r.buy||0) + (r.hold||0) + (r.sell||0) + (r.strongSell||0);
      if (tot > 0) {
        const pct = (n) => ((n||0) / tot * 100).toFixed(1) + "%";
        recBars = `
          <div class="m-rec-bars">
            <div class="sb" style="flex:${r.strongBuy||0}" title="Strong Buy: ${r.strongBuy}">${r.strongBuy||""}</div>
            <div class="b"  style="flex:${r.buy||0}" title="Buy: ${r.buy}">${r.buy||""}</div>
            <div class="h"  style="flex:${r.hold||0}" title="Hold: ${r.hold}">${r.hold||""}</div>
            <div class="s"  style="flex:${r.sell||0}" title="Sell: ${r.sell}">${r.sell||""}</div>
            <div class="ss" style="flex:${r.strongSell||0}" title="Strong Sell: ${r.strongSell}">${r.strongSell||""}</div>
          </div>
          <div class="m-rec-legend">
            <span><i style="background:#15803d"></i>Strong Buy</span>
            <span><i style="background:#22c55e"></i>Buy</span>
            <span><i style="background:#eab308"></i>Hold</span>
            <span><i style="background:#f97316"></i>Sell</span>
            <span><i style="background:#dc2626"></i>Strong Sell</span>
          </div>`;
      }
    }
    analystHtml = `
      <div class="m-sec full">
        <h3>Analyst Coverage</h3>
        <div class="m-analyst-grid">
          <div>
            <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
              ${recKey ? `<span class="m-rec-key ${keyCls}">${escapeHtml(recKey)}</span>` : ""}
              ${recMean != null ? `<span style="font-size:12px;color:var(--muted);">Mean rating <b style="color:var(--text)">${recMean.toFixed(2)}</b> / 5</span>` : ""}
              ${d.num_analysts ? `<span style="font-size:12px;color:var(--muted);">from <b style="color:var(--text)">${d.num_analysts}</b> analysts</span>` : ""}
            </div>
            ${needlePct != null ? `
              <div class="m-gauge"><div class="needle" style="left:${needlePct.toFixed(1)}%"></div></div>
              <div class="m-gauge-labels"><span>Strong Buy</span><span>Buy</span><span>Hold</span><span>Sell</span><span>Strong Sell</span></div>
            ` : ""}
            ${recBars}
          </div>
          <div>
            ${tbHtml || `<div class="m-summary">No price targets available.</div>`}
          </div>
        </div>
      </div>`;
  }

  // ---- Fundamentals
  const funda = [
    ["Revenue (TTM)", fmtCompactMoney(d.total_revenue, d.currency)],
    ["Revenue Growth", fmtPctFrac(d.revenue_growth)],
    ["Earnings Growth", fmtPctFrac(d.earnings_growth)],
    ["Free Cash Flow", fmtCompactMoney(d.free_cashflow, d.currency)],
    ["ROA", fmtPctFrac(d.roa)],
    ["Current Ratio", fmt2(d.current_ratio)],
  ];

  // ---- Dividend (only if any data)
  let divHtml = "";
  if (d.dividend_yield != null || d.dividend_rate != null || d.ex_div_date) {
    const divItems = [
      ["Yield", fmtPctDirect(d.dividend_yield)],
      ["Rate", fmtMoney(d.dividend_rate, d.currency)],
      ["Payout", fmtPctFrac(d.payout_ratio)],
      ["Ex-Div Date", d.ex_div_date || "—"],
    ];
    divHtml = `
      <div class="m-sec">
        <h3>Dividend</h3>
        <div class="m-kv">${divItems.map(([k,v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
      </div>`;
  }

  // ---- News
  let newsHtml = "";
  if (d.news && d.news.length) {
    newsHtml = `
      <div class="m-sec full">
        <h3>Latest News</h3>
        <ul class="m-news-list">
          ${d.news.map(n => `
            <li>
              ${n.link ? `<a href="${escapeHtml(n.link)}" target="_blank" rel="noopener">${escapeHtml(n.title)}</a>` : `<span>${escapeHtml(n.title)}</span>`}
              <div class="src">${escapeHtml(n.publisher || "")}${n.time ? " · " + relTime(n.time) : ""}</div>
            </li>`).join("")}
        </ul>
      </div>`;
  }

  // ---- About
  let aboutHtml = "";
  if (d.summary) {
    aboutHtml = `
      <div class="m-sec full">
        <h3>About${d.next_earnings ? ` · Next earnings: <b style="color:var(--text); font-weight:700">${fmtDateMDY(d.next_earnings)}</b>` : ""}</h3>
        <div class="m-summary collapsed" id="m-summary">${escapeHtml(d.summary)}</div>
        <button class="m-summary-toggle" onclick="this.previousElementSibling.classList.toggle('collapsed'); this.textContent = this.previousElementSibling.classList.contains('collapsed') ? 'Show more' : 'Show less';">Show more</button>
      </div>`;
  } else if (d.next_earnings) {
    aboutHtml = `<div class="m-sec full"><h3>Upcoming</h3><div class="m-summary">Next earnings: <b style="color:var(--text)">${fmtDateMDY(d.next_earnings)}</b></div></div>`;
  }

  sec.innerHTML = `
    <div class="m-sec">
      <h3>Snapshot</h3>
      ${renderDetailMetricGrid(snap)}
    </div>
    <div class="m-sec">
      <h3>Valuation &amp; Profitability</h3>
      ${renderDetailMetricGrid(val)}
    </div>
    <div class="m-sec full">
      <h3>Performance vs Benchmarks
        ${betaC != null ? ` · β(SPY) <b style="color:var(--text); font-weight:700">${betaC.toFixed(2)}</b>` : ""}
        ${corrC != null ? ` · ρ(SPY) <b style="color:var(--text); font-weight:700">${corrC.toFixed(2)}</b>` : ""}
        ${betaSec != null ? ` · β(${sectorETF}) <b style="color:var(--text); font-weight:700">${betaSec.toFixed(2)}</b>` : ""}
      </h3>
      <table class="m-perf">
        <thead><tr>
          <th>Window</th>
          <th><span class="legend" style="background:${getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()}"></span>${d.symbol}</th>
          <th><span class="legend" style="background:#8b5cf6"></span>S&amp;P 500</th>
          <th><span class="legend" style="background:#f59e0b"></span>${sectorETF}</th>
          <th>vs SPY</th>
        </tr></thead>
        <tbody>${perfRows}</tbody>
      </table>
    </div>
    ${analystHtml}
    <div class="m-sec">
      <h3>Fundamentals</h3>
      <div class="m-kv">${funda.map(([k,v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
    </div>
    ${divHtml || `<div class="m-sec"><h3>Profile</h3><div class="m-kv">
      <div><div class="k">Country</div><div class="v">${escapeHtml(d.country || "—")}</div></div>
      <div><div class="k">Employees</div><div class="v">${fmtCompactNum(d.employees)}</div></div>
      <div><div class="k">Exchange</div><div class="v">${escapeHtml(d.exchange || "—")}</div></div>
      <div><div class="k">Currency</div><div class="v">${escapeHtml(d.currency || "—")}</div></div>
    </div></div>`}
    ${newsHtml}
    ${aboutHtml}
  `;
  renderStatTipsKatex("#m-sections");
}

function fmtCompactNum(v) {
  if (v == null || !isFinite(v)) return "—";
  v = Number(v);
  const abs = Math.abs(v);
  if (abs >= 1e12) return (v/1e12).toFixed(2) + "T";
  if (abs >= 1e9)  return (v/1e9).toFixed(2) + "B";
  if (abs >= 1e6)  return (v/1e6).toFixed(2) + "M";
  if (abs >= 1e3)  return (v/1e3).toFixed(2) + "K";
  return v.toFixed(0);
}
function fmtPctFrac(v) {
  if (v == null || !isFinite(v)) return "—";
  return (Number(v) * 100).toFixed(2) + "%";
}
function relTime(iso) {
  try {
    const t = new Date(iso).getTime();
    const diff = (Date.now() - t) / 1000;
    if (diff < 3600) return Math.max(1, Math.floor(diff/60)) + "m ago";
    if (diff < 86400) return Math.floor(diff/3600) + "h ago";
    if (diff < 86400*30) return Math.floor(diff/86400) + "d ago";
    return fmtDateMDY(iso);
  } catch { return ""; }
}

/* ===========================================================================
 * Build / fetch (NDJSON streaming with progress)
 * --------------------------------------------------------------------------- */
function showProgress(pct) {
  $("#progress-wrap").classList.add("show");
  $("#progress-bar").style.width = clamp(pct, 0, 100) + "%";
}
function hideProgress() {
  setTimeout(() => {
    $("#progress-wrap").classList.remove("show");
    $("#progress-bar").style.width = "0%";
  }, 400);
}

/* ===========================================================================
 * Portfolio state — per-portfolio views, tabs, and analytics
 * --------------------------------------------------------------------------- */
const AD_HOC_KEY = "__current__";
let STATE = {
  activeView: null,        // current view name (or AD_HOC_KEY for ad-hoc input, or null = none yet)
  mode: "cap",             // "equal" | "cap" | "custom" | "preset:<name>"
  customWeights: null,     // {symbol: fraction}, set after user applies the popup
  period: "1Y",
  showSpy: true,
  showNdx: false,
  showSec: false,
  showDd: true,
  analytics: null,
  analyticsLoading: false,
  // {tabName: {"<mode>|<period>|<ccy>": result}} — persisted per tab so
  // switching back to a previously-visited tab paints from memory.
  analyticsByTab: {},
  // Named weight presets for the active portfolio. Loaded from the server
  // each time the active tab changes; saved/edited via the weights popup.
  weightPresets: [],       // [{name, weights, saved_at}]
  // Pass D — column views. activeViewName is global (one selection
  // across all portfolios). customViews and builtinOverrides both mirror
  // the server's .portfolio_tracker_column_views.json — built-in views are
  // editable in place, their per-view deltas (columns + color modes) living
  // in builtinOverrides and persisting immediately (no transient state).
  activeViewName: "Default",
  customViews: {},
  builtinOverrides: {},   // per-view overrides for built-in views (mirrors server)
  fitColumns: readFitColumnsPreference(),
  // Legacy global color-mode map — now only a fallback default under any
  // per-view color setting (see getHeatMode); new toggles write to the view.
  heatPrefs: readHeatPrefs(),
};

/* ===========================================================================
 * Weight-mode helpers (Equal / Cap / named presets / ad-hoc custom)
 * --------------------------------------------------------------------------- */
function presetByName(name) {
  if (!name || !STATE.weightPresets) return null;
  return STATE.weightPresets.find(p => p && p.name === name) || null;
}
function activePresetName() {
  if (typeof STATE.mode !== "string") return null;
  return STATE.mode.startsWith("preset:") ? STATE.mode.slice(7) : null;
}
function modeId(name) { return "preset:" + name; }
function weightsForMode(mode) {
  // Returns a {symbol: fraction} dict appropriate for `mode`, normalised over
  // the currently-loaded DATA symbols. Falls back to cap-weight on unknown modes.
  const rows = DATA.filter(r => r && r.symbol);
  if (mode === "equal") return equalWeightsOf(rows);
  if (mode === "cap")   return capWeightsOf(rows);
  if (mode === "custom") return STATE.customWeights || capWeightsOf(rows);
  if (typeof mode === "string" && mode.startsWith("preset:")) {
    const p = presetByName(mode.slice(7));
    if (!p) return capWeightsOf(rows);
    // Project preset weights onto current symbols and renormalise. Missing
    // symbols silently default to 0.
    const out = {}; let total = 0;
    for (const r of rows) { const w = Math.max(0, Number(p.weights[r.symbol] || 0)); out[r.symbol] = w; total += w; }
    if (total <= 0) return capWeightsOf(rows);
    for (const k of Object.keys(out)) out[k] /= total;
    return out;
  }
  return capWeightsOf(rows);
}
async function loadPresetsForView(name) {
  // Anonymous / unsaved tabs don't have presets — keep the list empty.
  if (!name || name === AD_HOC_KEY) {
    STATE.weightPresets = [];
    return;
  }
  try {
    const r = await fetch(`/api/weight-presets?view=${encodeURIComponent(name)}`);
    const d = await r.json();
    STATE.weightPresets = Array.isArray(d.presets) ? d.presets : [];
    // Honor the server-side active selection on initial load so the user's
    // last choice is restored across sessions. We only apply it if the
    // current mode is the safe default ("cap").
    if (d.active && STATE.mode === "cap" && presetByName(d.active)) {
      STATE.mode = modeId(d.active);
    }
  } catch (_) {
    STATE.weightPresets = [];
  }
}
async function persistActivePreset() {
  // Tell the server which preset (or none) is currently active for the
  // active view so it can be restored next launch.
  const view = STATE.activeView;
  if (!view || view === AD_HOC_KEY) return;
  const name = activePresetName();
  try {
    await fetch("/api/weight-presets/active", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({view, name}),
    });
  } catch (_) {}
}
async function savePresetServer(name, weights, opts) {
  opts = opts || {};
  const view = STATE.activeView;
  if (!view || view === AD_HOC_KEY) throw new Error("Save the portfolio first.");
  const body = {view, name, weights, set_active: opts.setActive !== false};
  if (opts.renameFrom) body.rename_from = opts.renameFrom;
  const r = await fetch("/api/weight-presets", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "save failed");
  STATE.weightPresets = Array.isArray(d.presets) ? d.presets : STATE.weightPresets;
  return d;
}
async function deletePresetServer(name) {
  const view = STATE.activeView;
  if (!view || view === AD_HOC_KEY) return;
  const url = `/api/weight-presets?view=${encodeURIComponent(view)}&name=${encodeURIComponent(name)}`;
  const r = await fetch(url, {method: "DELETE"});
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "delete failed");
  STATE.weightPresets = Array.isArray(d.presets) ? d.presets : [];
}

function analyticsMapForTab(name) {
  const key = name || AD_HOC_KEY;
  let m = STATE.analyticsByTab[key];
  if (!m) { m = {}; STATE.analyticsByTab[key] = m; }
  return m;
}
function currentAnalyticsMap() { return analyticsMapForTab(STATE.activeView); }
function invalidateAnalyticsForTab(name) {
  delete STATE.analyticsByTab[name || AD_HOC_KEY];
  // Cascade to the disk cache so the analyst panel doesn't paint stale
  // numbers after a quotes refresh. Fire-and-forget — a network blip here
  // only means we keep showing the old cached payload, which is fine.
  if (name && name !== AD_HOC_KEY) {
    fetch(`/api/analytics-cache?view=${encodeURIComponent(name)}`, {method: "DELETE"}).catch(() => {});
  }
}

// Pull persisted analytics off disk and seed the in-memory map for this tab.
// Called once on tab activation (same place we load the weight presets), so
// reopening a saved portfolio paints the Rating Distribution from cache —
// no spinner, no yfinance round-trip.
async function loadAnalyticsCacheForView(name) {
  if (!name || name === AD_HOC_KEY) return;
  try {
    const r = await fetch(`/api/analytics-cache?view=${encodeURIComponent(name)}`);
    const d = await r.json();
    const cache = d && d.cache;
    if (!cache || typeof cache !== "object") return;
    const tabMap = analyticsMapForTab(name);
    for (const k of Object.keys(cache)) {
      const rec = cache[k];
      if (rec && rec.payload && !tabMap[k]) tabMap[k] = rec.payload;
    }
  } catch (_) { /* ignore — fall back to in-memory + fresh fetch */ }
}

// Slim a full analytics payload to the static fields worth persisting.
// `series` is large and only used by the live time-series chart, which is
// always re-fetched on demand — no point storing it on disk.
function _slimAnalyticsForCache(a) {
  if (!a || typeof a !== "object" || a.error) return null;
  const out = {};
  const keep = ["period", "display_ccy", "weights_applied", "active_symbols",
                "missing_symbols", "stats", "spy_stats", "nasdaq_stats",
                "weighted", "contribution", "analyst", "exposure",
                "concentration", "warnings"];
  for (const k of keep) if (k in a) out[k] = a[k];
  return out;
}

function persistAnalyticsToCache(viewName, key, payload) {
  if (!viewName || viewName === AD_HOC_KEY || !key) return;
  const slim = _slimAnalyticsForCache(payload);
  if (!slim) return;
  fetch("/api/analytics-cache", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({view: viewName, key, payload: slim}),
  }).catch(() => {});
}
let WATCHLISTS = {};        // name -> entries-string (saved watchlists)
let VIEWS = {};             // name -> {entries, saved_at, row_count, stale}
let LAST_VIEW = null;

function viewIsAdhoc(name) { return !name || name === AD_HOC_KEY; }
function entriesArr(raw) { return String(raw || "").split(/[\n,]+/).map(s => s.trim()).filter(Boolean); }

async function build(opts) {
  opts = opts || {};
  const raw = $("#tickers").value.trim();
  if (!raw) { toast("Enter at least one ticker or company name."); return; }
  const entries = entriesArr(raw);
  $("#build").disabled = true; $("#refresh").disabled = true;
  $("#status").innerHTML = lcHtml("resolving symbols", {bar: true, meta: `0·${entries.length}`});
  showProgress(2);
  DATA = [];
  render();

  let total = entries.length;
  let done = 0;
  try {
    const r = await fetch("/api/quotes-stream", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({entries}),
    });
    if (!r.ok || !r.body) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || ("HTTP " + r.status));
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done: rDone, value } = await reader.read();
      if (rDone) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        let msg;
        try { msg = JSON.parse(line); } catch { continue; }
        if (msg.type === "start") {
          total = msg.total || total;
          showProgress(3);
        } else if (msg.type === "row") {
          done = msg.done || (done + 1);
          DATA.push(msg.row);
          render();
          showProgress((done / Math.max(1, total)) * 100);
          $("#status").innerHTML = lcHtml("streaming quotes", {bar: true, meta: `${done}·${total}`});
        } else if (msg.type === "done") {
          showProgress(100);
        }
      }
    }
    {
      const _nm = STATE.activeView || AD_HOC_KEY;
      $("#status").innerHTML = `<span class="status-name">${escapeHtml(viewLabel(_nm))}</span><span class="status-meta">updated ${escapeHtml(new Date().toLocaleTimeString())}</span>`;
    }
    // Persist this build as a view under the active tab name (or ad-hoc).
    const targetName = STATE.activeView || AD_HOC_KEY;
    await persistView(targetName, raw, DATA);
    STATE.customWeights = null;  // new build — drop stale custom weights
    if (STATE.mode === "custom") STATE.mode = "cap";
    // Refresh per-portfolio presets — a brand-new build may have just
    // promoted the ad-hoc tab into a real named view.
    await loadPresetsForView(targetName);
    invalidateAnalyticsForTab(targetName);  // rebuild invalidates this tab only
    renderModeBar();
    requestAnalytics();
    // Background sentiment warm-up — fills NS dots ~10-15s after build.
    setTimeout(() => warmNewsSentiment(), 2000);
  } catch (e) {
    toast("Error: " + e.message);
    $("#status").textContent = "Error.";
  } finally {
    hideProgress();
    $("#build").disabled = false; $("#refresh").disabled = false;
  }
}

async function persistView(name, entries, rows) {
  const targetName = name || AD_HOC_KEY;
  try {
    const r = await fetch(`/api/views/${encodeURIComponent(targetName)}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({entries, rows, set_last: true}),
    });
    if (r.ok) {
      const d = await r.json();
      VIEWS[targetName] = {
        entries: d.view.entries,
        saved_at: d.view.saved_at,
        row_count: (d.view.rows || []).length,
        stale: false,
      };
      LAST_VIEW = targetName;
      STATE.activeView = targetName;
      renderTabs();
      renderEditorMeta();
    }
  } catch (e) { /* best-effort */ }
}

async function loadAllAtStartup() {
  $("#status").innerHTML = lcHtml("indexing portfolios", {bar: true});
  await Promise.all([loadWatchlists(), loadViews(), loadColumnViews()]);
  $("#status").textContent = "Idle";
  wireColumnViewBar();
  renderTabs();
  // Restore the last open view if any.
  if (LAST_VIEW && VIEWS[LAST_VIEW]) {
    await activateTab(LAST_VIEW, {silent: true});
  } else {
    // No prior view — show the editor in ad-hoc mode but keep the panel closed.
    STATE.activeView = null;
    renderEditorMeta();
  }
  // Background warm-up — sequenced so we don't saturate yfinance with parallel
  // requests. Portfolio warm-up first (one bulk download per tab is heavier
  // and benefits more from being warm), then FX hover charts.
  setTimeout(() => {
    warmRecentTabs().then(() => {
      setTimeout(() => preloadFxIndexes(), 1500);
    });
  }, 1200);
}

async function warmNewsSentiment() {
  const symbols = DATA.map(r => r.symbol).filter(Boolean);
  if (!symbols.length) return;
  try {
    const resp = await fetch(`/api/news-sentiment?symbols=${encodeURIComponent(symbols.join(","))}`);
    if (!resp.ok) return;
    const data = await resp.json();
    patchRowSentiment(data.sentiment || {});
  } catch {}
}

async function preloadFxIndexes() {
  // Bias toward the most common reserve / display currencies so the first
  // hover lands on a warm cache. The remaining ones are still loaded
  // lazily on demand by fxFetchIndex().
  const priority = ["USD","EUR","GBP","JPY","CHF","CAD","AUD"].filter(c => FX_SUPPORTED.indexOf(c) >= 0);
  const rest = (FX_SUPPORTED || []).filter(c => priority.indexOf(c) < 0);
  for (const batch of [priority, rest]) {
    if (!batch.length) continue;
    try {
      const r = await fetch(`/api/fx-indexes-bulk?ccys=${encodeURIComponent(batch.join(","))}`);
      if (!r.ok) continue;
      const d = await r.json();
      const indexes = d.indexes || {};
      for (const c of Object.keys(indexes)) {
        if (Array.isArray(indexes[c]) && indexes[c].length) FX_INDEX_CACHE[c] = indexes[c];
      }
    } catch (e) { /* best-effort */ }
    // Brief gap before the second batch.
    await new Promise(r => setTimeout(r, 800));
  }
}

async function warmRecentTabs(maxN) {
  const limit = (typeof maxN === "number" && maxN > 0) ? maxN : 3;
  // Skip the currently active view (it's been rendered) and pick the most-recent others.
  const others = Object.entries(VIEWS)
    .filter(([n, v]) => n !== STATE.activeView && v && v.row_count > 0 && n !== AD_HOC_KEY)
    .sort((a, b) => String(b[1].saved_at || "").localeCompare(String(a[1].saved_at || "")))
    .slice(0, limit);
  // Sequential — back-to-back analytics POSTs would compete for the same
  // yfinance session and Yahoo would start returning empty close-price frames.
  for (const [name, _meta] of others) {
    try {
      const r = await fetch(`/api/views/${encodeURIComponent(name)}`);
      if (!r.ok) continue;
      const d = await r.json();
      const rows = (d.view && Array.isArray(d.view.rows)) ? d.view.rows : [];
      if (!rows.length) continue;
      const eq = equalWeightsOf(rows);
      const cap = capWeightsOf(rows);
      const wsets = {equal: eq, cap: cap};
      const res = await fetch("/api/portfolio-analytics-multi", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({rows, weight_sets: wsets, period: STATE.period, display_ccy: FX_QUOTE}),
      });
      if (!res.ok) continue;
      const dr = await res.json();
      const results = dr.results || {};
      const tabMap = analyticsMapForTab(name);
      for (const k of Object.keys(results)) {
        tabMap[analyticsCacheKey(k, STATE.period)] = results[k];
      }
      if (results.cap && !tabMap[analyticsCacheKey("custom", STATE.period)]) {
        tabMap[analyticsCacheKey("custom", STATE.period)] = results.cap;
      }
      // Brief pause so we don't saturate Yahoo's rate limiter.
      await new Promise(r => setTimeout(r, 500));
    } catch (e) { /* skip this tab silently */ }
  }
}

async function loadViews() {
  try {
    const r = await fetch("/api/views");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    VIEWS = d.views || {};
    LAST_VIEW = d.last_view || null;
  } catch (e) {
    VIEWS = {}; LAST_VIEW = null;
  }
}

async function activateTab(name, opts) {
  opts = opts || {};
  STATE.activeView = name;
  STATE.customWeights = null;
  // Keep STATE.analyticsByTab[*] across switches — re-visiting paints from memory.
  if (STATE.mode === "custom") STATE.mode = "cap";
  // Switching tabs swaps the per-portfolio preset list. loadPresetsForView
  // may also restore the active preset (only if the current mode is the safe
  // default "cap"), so the user's last selection persists across sessions.
  await loadPresetsForView(name);
  // Seed the in-memory analytics map from disk so reopening a portfolio
  // hydrates the Rating Distribution panel without re-fetching from
  // yfinance. requestAnalytics() will still kick off in the cached-rows
  // branch below, but its cache-hit path will take the disk seed.
  await loadAnalyticsCacheForView(name);
  renderModeBar();
  renderTabs();
  renderEditorMeta();
  // Set the textarea to the watchlist entries (or stored view entries if ad-hoc).
  const view = VIEWS[name];
  const wlEntries = WATCHLISTS[name];
  $("#tickers").value = wlEntries != null ? wlEntries : (view ? view.entries : "");
  // Try to load cached rows for the view.
  if (view && view.row_count > 0) {
    try {
      const r = await fetch(`/api/views/${encodeURIComponent(name)}`);
      const d = await r.json();
      const rows = (d.view && Array.isArray(d.view.rows)) ? d.view.rows : [];
      if (rows.length) {
        DATA = rows;
        render();
        // DATA just landed — refresh the mode bar so the [+] pill appears
        // and the active preset paints with the correct active state.
        renderModeBar();
        const savedAt = view.saved_at ? relTime(view.saved_at) : "previously";
        $("#status").innerHTML = `<span class="status-name">${escapeHtml(viewLabel(name))}</span><span class="status-meta">cached ${escapeHtml(savedAt)}</span>${view.stale ? `<span class="status-stale">stale</span>` : ""}`;
        const needsColumnRefresh = DATA.length > 0 && DATA.some(r => r && !r.error && r.rating_dist === undefined);
        if (view.stale || needsColumnRefresh) {
          if (!opts.silent) toast(view.stale ? `Constituents changed — refreshing ${viewLabel(name)}…` : `Refreshing ${viewLabel(name)} — new data columns available…`);
          await build({keepPanelOpen: true});
        } else {
          // Saved rows do not carry forward every transient news field, but the
          // backend news cache now survives restarts. Rehydrate NS dots from the
          // cache as soon as a portfolio tab is reopened so the user does not
          // need to manually refresh or open the News panel first.
          warmNewsSentiment();
          requestAnalytics();
        }
        await fetch("/api/last-view", {method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({name})});
        return;
      }
    } catch (e) { /* fall through */ }
  }
  // No cached rows yet — clear the table, prompt user.
  DATA = []; render();
  $("#pf-analytics-body").innerHTML = `<div class="pf-empty">Press <b>Build Dashboard</b> to load this portfolio.</div>`;
  $("#status").innerHTML = `<span class="status-name">${escapeHtml(viewLabel(name))}</span>`;
  await fetch("/api/last-view", {method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({name})});
}

function viewLabel(name) {
  if (!name) return "Ad-hoc";
  if (name === AD_HOC_KEY) return "Ad-hoc (unsaved)";
  return name;
}

function renderTabs() {
  const wrap = $("#pf-tabs"); wrap.innerHTML = "";
  // Tab order: saved watchlists alphabetically, then ad-hoc (if present), then "+ New".
  const wlNames = Object.keys(WATCHLISTS).sort((a, b) => a.localeCompare(b));
  const hasAdhoc = !!VIEWS[AD_HOC_KEY];
  const tabNames = [...wlNames];
  if (hasAdhoc) tabNames.push(AD_HOC_KEY);

  for (const name of tabNames) {
    const tab = document.createElement("div");
    tab.className = "pf-tab" + (STATE.activeView === name ? " active" : "") + (name === AD_HOC_KEY ? " pf-tab-newadhoc" : "");
    tab.setAttribute("role", "tab");
    tab.setAttribute("data-name", name);

    const view = VIEWS[name];
    const staleDot = view && view.stale ? `<span class="pf-tab-stale" title="Constituents changed — refresh to update"></span>` : "";
    const closeBtn = `<span class="pf-tab-close" title="Delete">✕</span>`;
    tab.innerHTML = `${staleDot}<span class="pf-tab-label" title="Double-click to rename">${escapeHtml(viewLabel(name))}</span>${closeBtn}`;
    tab.addEventListener("click", (e) => {
      if (e.target.closest(".pf-tab-close")) return;
      if (e.target.closest(".pf-tab-rename-input")) return;
      activateTab(name);
    });
    tab.querySelector(".pf-tab-close").addEventListener("click", async (e) => {
      e.stopPropagation();
      await deletePortfolio(name);
    });
    // Double-click the label → inline rename (saved portfolios only, not ad-hoc).
    if (name !== AD_HOC_KEY) {
      tab.querySelector(".pf-tab-label").addEventListener("dblclick", (e) => {
        e.stopPropagation();
        e.preventDefault();
        beginTabRename(tab, name);
      });
    }
    wrap.appendChild(tab);
  }

  // "+ New" tab — always present.
  const add = document.createElement("button");
  add.className = "pf-tab-add";
  add.textContent = "＋ New portfolio";
  add.addEventListener("click", () => createNewTab());
  wrap.appendChild(add);
}

function beginTabRename(tab, oldName) {
  const labelEl = tab.querySelector(".pf-tab-label");
  if (!labelEl || tab.querySelector(".pf-tab-rename-input")) return;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "pf-tab-rename-input";
  input.value = oldName;
  input.spellcheck = false;
  input.size = Math.max(8, oldName.length + 2);
  labelEl.style.display = "none";
  labelEl.insertAdjacentElement("afterend", input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return; done = true;
    const next = input.value.trim();
    input.remove();
    labelEl.style.display = "";
    if (!commit || !next || next === oldName) return;
    await renamePortfolio(oldName, next);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("dblclick", (e) => e.stopPropagation());
}

async function renamePortfolio(oldName, newName) {
  try {
    const r = await fetch("/api/portfolio/rename", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({old: oldName, new: newName}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Rename failed.");
    // Update local maps so the UI reflects the new key without a full reload.
    WATCHLISTS = d.watchlists || WATCHLISTS;
    if (VIEWS[oldName]) { VIEWS[newName] = VIEWS[oldName]; delete VIEWS[oldName]; }
    if (STATE.analyticsByTab && STATE.analyticsByTab[oldName]) {
      STATE.analyticsByTab[newName] = STATE.analyticsByTab[oldName];
      delete STATE.analyticsByTab[oldName];
    }
    if (STATE.activeView === oldName) STATE.activeView = newName;
    renderTabs(); renderEditorMeta();
    toast(`Renamed to "${newName}".`);
  } catch (err) {
    toast(err.message || "Rename failed.");
  }
}

function renderEditorMeta() {
  // Meta line was removed — name lives in the topbar status now.
  if (typeof updatePrimaryButtonLabels === "function") updatePrimaryButtonLabels();
}

async function createNewTab() {
  // Open the panel in fresh ad-hoc state.
  STATE.activeView = AD_HOC_KEY;
  STATE.customWeights = null;
  $("#tickers").value = "";
  DATA = []; render();
  $("#input-panel").classList.remove("hidden");
  $("#pf-analytics-body").innerHTML = `<div class="pf-empty">Paste tickers and press <b>Build Dashboard</b>.</div>`;
  renderTabs(); renderEditorMeta();
  $("#tickers").focus();
}

function showConfirm({title, body, okLabel}) {
  return new Promise((resolve) => {
    const bg = $("#confirm-bg");
    $("#confirm-title").textContent = title || "Are you sure?";
    $("#confirm-body").innerHTML = body || "";
    $("#confirm-ok").textContent = okLabel || "Delete";
    bg.classList.add("show");
    const cleanup = (val) => {
      bg.classList.remove("show");
      $("#confirm-ok").onclick = null;
      $("#confirm-cancel").onclick = null;
      bg.onclick = null;
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onKey = (e) => {
      if (e.key === "Escape") cleanup(false);
      else if (e.key === "Enter") cleanup(true);
    };
    $("#confirm-ok").onclick = () => cleanup(true);
    $("#confirm-cancel").onclick = () => cleanup(false);
    bg.onclick = (e) => { if (e.target.id === "confirm-bg") cleanup(false); };
    document.addEventListener("keydown", onKey);
    setTimeout(() => $("#confirm-ok").focus(), 30);
  });
}

async function deletePortfolio(name) {
  if (!name) return;
  if (name === AD_HOC_KEY) {
    const ok = await showConfirm({
      title: "Discard unsaved portfolio?",
      body: "Your unsaved ad-hoc portfolio will be removed.",
      okLabel: "Discard",
    });
    if (!ok) return;
    try { await fetch(`/api/views/${encodeURIComponent(name)}`, {method: "DELETE"}); } catch(e) {}
    delete VIEWS[name];
    if (STATE.activeView === name) STATE.activeView = null;
    renderTabs(); renderEditorMeta();
    return;
  }
  const ok = await showConfirm({
    title: "Delete portfolio?",
    body: `<b>${escapeHtml(name)}</b> and its cached results will be removed. This can't be undone.`,
    okLabel: "Delete",
  });
  if (!ok) return;
  try {
    const r = await fetch(`/api/watchlists?name=${encodeURIComponent(name)}`, {method: "DELETE"});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Delete failed.");
    WATCHLISTS = d.watchlists || {};
    delete VIEWS[name];
    if (STATE.activeView === name) {
      STATE.activeView = null;
      DATA = []; render();
    }
    renderTabs(); renderEditorMeta();
    toast(`Deleted "${name}".`);
  } catch (err) {
    toast(err.message || "Delete failed.");
  }
}

async function saveAsNewWatchlist() {
  const raw = $("#tickers").value.trim();
  if (!raw) return toast("Enter tickers first.");
  const name = prompt("Save this portfolio as…", STATE.activeView && STATE.activeView !== AD_HOC_KEY ? STATE.activeView : "");
  if (!name || !name.trim()) return;
  const clean = name.trim();
  try {
    const r = await fetch("/api/watchlists", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: clean, entries: raw}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Save failed.");
    WATCHLISTS = d.watchlists || {};
    // If we were on ad-hoc and have data, move that view's rows under the new name.
    if (STATE.activeView === AD_HOC_KEY && DATA.length) {
      await persistView(clean, raw, DATA);
      try { await fetch(`/api/views/${encodeURIComponent(AD_HOC_KEY)}`, {method: "DELETE"}); } catch(e) {}
      delete VIEWS[AD_HOC_KEY];
    }
    STATE.activeView = clean;
    renderTabs(); renderEditorMeta();
    toast(`Saved "${clean}".`);
    // If the server reported the entries changed and we have rows, auto-rebuild.
    if (d.entries_changed && DATA.length) {
      await build({keepPanelOpen: true});
    }
  } catch (err) {
    toast(err.message || "Save failed.");
  }
}

// "Save" toolbar button — overwrite current named tab, or prompt if ad-hoc.
async function saveWatchlist() {
  const name = STATE.activeView;
  if (!name || name === AD_HOC_KEY) return saveAsNewWatchlist();
  const raw = $("#tickers").value.trim();
  if (!raw) return toast("Enter tickers first.");
  try {
    const r = await fetch("/api/watchlists", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({name, entries: raw}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Save failed.");
    WATCHLISTS = d.watchlists || {};
    renderTabs(); renderEditorMeta();
    toast(`Updated "${name}".`);
    if (d.entries_changed) {
      // Constituents changed → auto-refresh data (user preference).
      await build({keepPanelOpen: true});
    }
  } catch (err) {
    toast(err.message || "Save failed.");
  }
}

async function loadWatchlists() {
  try {
    const res = await fetch("/api/watchlists");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to load watchlists.");
    WATCHLISTS = data.watchlists || {};
  } catch (err) {
    WATCHLISTS = {};
    toast(err.message || "Failed to load watchlists.");
  }
}

/**
 * Export every saved portfolio to an .xlsx (one sheet per portfolio).
 * Server-side build — fetches analyst recs in parallel + computes the
 * analytics block from cached rows, so the download is "everything you
 * can see in the app." See xlsx_export.py and the inline comment by the
 * topbar Export button for the maintenance contract.
 *
 * UX: this can take a few seconds (the analyst-rec fetch is the slow
 * piece). We swap the button label for a loading chip while the request
 * is in flight, then restore it.
 */
async function exportXlsx() {
  const btn = $("#export");
  if (!btn) return;
  const origHtml = btn.innerHTML;
  const origDisabled = btn.disabled;
  btn.disabled = true;
  btn.innerHTML = lcHtml("exporting", {bar: true});
  try {
    const r = await fetch("/api/export-xlsx");
    if (!r.ok) {
      let msg = "Export failed (HTTP " + r.status + ").";
      try { const j = await r.json(); if (j && j.error) msg = j.error; } catch {}
      throw new Error(msg);
    }
    const blob = await r.blob();
    // Pull the filename out of Content-Disposition (server picks the timestamp).
    let fname = "portfolio_tracker_export.xlsx";
    const cd = r.headers.get("content-disposition") || "";
    const m = cd.match(/filename="?([^";]+)"?/i);
    if (m) fname = m[1];
    const a = document.createElement("a");
    const url = URL.createObjectURL(blob);
    a.href = url;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast(`Exported ${fname}`);
  } catch (err) {
    toast(err.message || "Export failed.");
  } finally {
    btn.innerHTML = origHtml;
    btn.disabled = origDisabled;
  }
}

/* ===========================================================================
 * Portfolio analytics — weights, request, render, chart
 * --------------------------------------------------------------------------- */
function computeWeights() {
  const rows = DATA.filter(r => r && r.symbol);
  if (!rows.length) return {};
  if (STATE.mode === "equal") {
    const w = 1 / rows.length;
    return Object.fromEntries(rows.map(r => [r.symbol, w]));
  }
  if (STATE.mode === "custom" && STATE.customWeights) {
    // Validate and renormalize over the current row set.
    const raw = {};
    let total = 0;
    for (const r of rows) {
      const v = Number(STATE.customWeights[r.symbol] || 0);
      raw[r.symbol] = isFinite(v) && v >= 0 ? v : 0;
      total += raw[r.symbol];
    }
    if (total <= 0) {
      const w = 1 / rows.length;
      return Object.fromEntries(rows.map(r => [r.symbol, w]));
    }
    return Object.fromEntries(rows.map(r => [r.symbol, raw[r.symbol] / total]));
  }
  // Cap-weighted (default).
  const caps = rows.map(r => Math.max(0, Number(r.market_cap) || 0));
  const total = caps.reduce((a, b) => a + b, 0);
  if (total > 0) return Object.fromEntries(rows.map((r, i) => [r.symbol, caps[i] / total]));
  // Fallback to equal if no caps.
  const w = 1 / rows.length;
  return Object.fromEntries(rows.map(r => [r.symbol, w]));
}

function capWeightsFromData() { return capWeightsOf(DATA.filter(r => r && r.symbol)); }
function equalWeightsFromData() { return equalWeightsOf(DATA.filter(r => r && r.symbol)); }
function analyticsCacheKey(mode, period) { return mode + "|" + period + "|" + FX_QUOTE; }

let _analyticsReqId = 0;
async function requestAnalytics(opts) {
  opts = opts || {};
  if (!DATA.length) {
    STATE.analytics = null;
    renderAnalyticsBody();
    return;
  }
  const period = STATE.period;
  const mode = STATE.mode;
  const key = analyticsCacheKey(mode, period);
  const tabMap = currentAnalyticsMap();

  // Cache hit → instant.
  const cached = tabMap[key];
  if (cached && !opts.force) {
    STATE.analytics = cached;
    STATE.analyticsLoading = false;
    renderAnalyticsBody();
    return;
  }

  const reqId = ++_analyticsReqId;
  STATE.analyticsLoading = true;
  renderAnalyticsBody();

  // Build the weight_sets we'll request. Always include the *active* mode;
  // also opportunistically include any other modes we don't already have
  // cached for this period (the backend shares the slow yfinance + analyst
  // fetches across every weight set in a single call). The set name we send
  // matches the mode id so analyticsCacheKey lines up server↔client.
  const wsets = {};
  function maybeRequest(modeKey) {
    if (!tabMap[analyticsCacheKey(modeKey, period)]) {
      wsets[modeKey] = weightsForMode(modeKey);
    }
  }
  wsets[mode] = weightsForMode(mode);
  maybeRequest("equal");
  maybeRequest("cap");

  try {
    const r = await fetch("/api/portfolio-analytics-multi", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({rows: DATA, weight_sets: wsets, period, display_ccy: FX_QUOTE}),
    });
    const d = await r.json();
    if (reqId !== _analyticsReqId) return;
    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
    if (d.error) throw new Error(d.error);
    const results = d.results || {};
    for (const k of Object.keys(results)) {
      const cacheK = analyticsCacheKey(k, period);
      tabMap[cacheK] = results[k];
      // Mirror to disk so the next reload hydrates the panel instantly.
      persistAnalyticsToCache(STATE.activeView, cacheK, results[k]);
    }
    STATE.analytics = tabMap[key] || results[mode] || null;
  } catch (e) {
    STATE.analytics = {error: e.message};
  } finally {
    if (reqId === _analyticsReqId) STATE.analyticsLoading = false;
    renderAnalyticsBody();
  }
}

function renderAnalyticsBody() {
  const body = $("#pf-analytics-body");
  if (!DATA.length) {
    body.innerHTML = `<div class="pf-empty">Build the dashboard to compute portfolio analytics.</div>`;
    renderAnalystDashboard({});
    return;
  }
  if (STATE.analyticsLoading && !STATE.analytics) {
    body.innerHTML = `<div class="pf-empty lc-block">${lcHtml("computing analytics", {bar: true})}</div>`;
    renderAnalystDashboard(quickAnalystPreview() || {});
    return;
  }
  const a = STATE.analytics;
  if (!a) { body.innerHTML = `<div class="pf-empty">No analytics yet.</div>`; renderAnalystDashboard({}); return; }
  if (a.error) { body.innerHTML = `<div class="pf-empty" style="color:var(--neg)">Analytics error: ${escapeHtml(a.error)}</div>`; renderAnalystDashboard({}); return; }

  // Build cards.
  const banner = staleBannerHtml();
  body.innerHTML = `
    ${banner}
    <div class="pf-grid">
      <div class="pf-card">
        <h4>Portfolio chart <span class="sub">${escapeHtml(STATE.period)} · ${labelForMode(STATE.mode)} · ${escapeHtml(a.display_ccy || FX_QUOTE)}${STATE.analyticsLoading ? '<span class="pf-loading"> refreshing…</span>' : ''}</span></h4>
        <div class="pf-chart-wrap" id="pf-chart-host"></div>
        <div class="pf-chart-legend" id="pf-chart-legend"></div>
      </div>
      <div class="pf-card">
        <h4>Risk &amp; return <span class="sub">vs SPY · ${escapeHtml(a.display_ccy || FX_QUOTE)}</span></h4>
        ${renderStatsHtml(a)}
      </div>
      <div class="pf-card">
        <h4>Valuation &amp; analyst <span class="sub">weighted</span></h4>
        ${renderValuationAnalystHtml(a)}
      </div>
      <div class="pf-card">
        <h4>Concentration</h4>
        ${renderConcentrationHtml(a)}
      </div>
      <div class="pf-card">
        <h4>Sector exposure</h4>
        ${renderBarsHtml(a.exposure && a.exposure.by_sector)}
      </div>
      <div class="pf-card">
        <h4>Market-cap buckets</h4>
        ${renderBarsHtml(a.exposure && a.exposure.by_bucket)}
      </div>
      <div class="pf-card" style="grid-column: 1 / -1;">
        <h4>Contribution to ${escapeHtml(STATE.period)} return <span class="sub">${escapeHtml(a.display_ccy || FX_QUOTE)}</span></h4>
        ${renderContribHtml(a)}
      </div>
    </div>
  `;
  drawPortfolioChart(a, $("#pf-chart-host"), $("#pf-chart-legend"));
  renderStatTipsKatex();
  renderAnalystDashboard(a);
}

function staleBannerHtml() {
  const name = STATE.activeView;
  if (!name || !VIEWS[name] || !VIEWS[name].stale) return "";
  return `<div class="pf-stale-banner">Constituents changed since last build — analytics may be stale.
    <button onclick="build({keepPanelOpen:true})">Refresh now</button></div>`;
}

function labelForMode(m) {
  if (m === "equal") return "Equal-weight";
  if (m === "custom") return "Custom";
  if (m === "cap") return "Cap-weighted";
  if (typeof m === "string" && m.startsWith("preset:")) return m.slice(7);
  return "Cap-weighted";
}

function fmtPctSigned(v, d) {
  if (v == null || !isFinite(v)) return "—";
  d = d == null ? 2 : d;
  const sign = v > 0 ? "+" : "";
  return sign + Number(v).toFixed(d) + "%";
}
function fmtPctPlain(v, d) {
  if (v == null || !isFinite(v)) return "—";
  d = d == null ? 2 : d;
  return Number(v).toFixed(d) + "%";
}
function fmtNumOr(v, d, suffix) {
  if (v == null || !isFinite(v)) return "—";
  d = d == null ? 2 : d;
  return Number(v).toFixed(d) + (suffix || "");
}
function fmtCapBig(v) {
  if (v == null || !isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e12) return (v/1e12).toFixed(2) + "T";
  if (abs >= 1e9)  return (v/1e9).toFixed(2) + "B";
  if (abs >= 1e6)  return (v/1e6).toFixed(2) + "M";
  if (abs >= 1e3)  return (v/1e3).toFixed(2) + "K";
  return v.toFixed(0);
}

const METRIC_INFO = {
  "Period return": {
    formula: String.raw`R = \dfrac{V_T}{V_0} - 1`,
    desc: "Total return of the portfolio over the chosen period, FX-adjusted to the display currency.",
    range: "Compare to SPY in the same period. A positive read with low volatility is the cleanest win."
  },
  "Ann. return": {
    formula: String.raw`R_{ann} = \left(\dfrac{V_T}{V_0}\right)^{\frac{1}{y}} - 1`,
    desc: "Compound annual growth rate. Normalises returns across periods of different length so 3M, 1Y and 5Y are comparable.",
    range: "Above 10% sustained is strong; above the risk-free rate (~5%) is the bar for active equity."
  },
  "Ann. vol": {
    formula: String.raw`\sigma_{ann} = \sigma_{daily} \cdot \sqrt{252}`,
    desc: "Annualised standard deviation of daily returns — the headline risk measure.",
    range: "Equities cluster 15–25%. Below 12% is unusually smooth, above 35% is a high-octane book."
  },
  "Sharpe": {
    formula: String.raw`S = \dfrac{\overline{R} - R_f}{\sigma}`,
    desc: "Excess return per unit of total volatility. Risk-free rate treated as 0 here, so it’s a pure return/vol ratio.",
    range: "0.5 mediocre · 1.0 good · 2.0 excellent · >3.0 suspect (curve-fit or short sample)."
  },
  "Sortino": {
    formula: String.raw`S_o = \dfrac{\overline{R} - R_f}{\sigma_{down}}`,
    desc: "Like Sharpe but penalises only downside volatility — closer to how an investor actually feels risk.",
    range: "Usually higher than Sharpe. >1.0 is good, >2.0 is excellent. The Sortino/Sharpe gap reveals upside-skew."
  },
  "Max drawdown": {
    formula: String.raw`\text{DD}_{max} = \min_{t}\!\left(\dfrac{V_t}{\max_{s\le t} V_s} - 1\right)`,
    desc: "Worst peak-to-trough loss over the period. Drives the emotional pain of holding the strategy.",
    range: "Equity portfolios routinely see −20 to −35% in bear markets. Above −50% suggests concentrated risk."
  },
  "Calmar": {
    formula: String.raw`C = \dfrac{R_{ann}}{|\text{DD}_{max}|}`,
    desc: "Annualised return divided by max drawdown — return per unit of worst-case pain.",
    range: ">0.5 acceptable · >1.0 good · >2.0 exceptional. Penalises managers who run wild during crashes."
  },
  "Beta (SPY)": {
    formula: String.raw`\beta = \dfrac{\mathrm{Cov}(r_p, r_m)}{\mathrm{Var}(r_m)}`,
    desc: "Sensitivity to SPY moves. β=1 means it moves with the market; β=1.3 means 30% more responsive.",
    range: "0.6–0.8 defensive · 0.9–1.1 market-like · >1.3 high-beta growth. Negative is rare and means inverse exposure."
  },
  "R² (SPY)": {
    formula: String.raw`R^2 = \mathrm{Corr}(r_p, r_m)^2`,
    desc: "Share of portfolio variance explained by SPY. Tells you whether beta is a meaningful description of behaviour.",
    range: ">0.85 → portfolio is essentially SPY+leverage. <0.4 → diversification/idiosyncratic exposure. <0.1 → unrelated."
  },
  "Tracking err": {
    formula: String.raw`\mathrm{TE} = \sqrt{252}\cdot\sigma\!\left(r_p - r_m\right)`,
    desc: "Annualised standard deviation of the portfolio’s return *minus* SPY’s — how far you wander from the benchmark.",
    range: "Index funds <2%. Active managers 4–8% typical. Concentrated stock picks 10–20%+. Pair with information ratio."
  },
  /* --- Valuation & analyst (weighted) --- */
  "P/E (wtd)": {
    formula: String.raw`PE_{port} = \dfrac{\sum_i w_i \cdot PE_i}{\sum_i w_i \;:\; PE_i \text{ defined}}`,
    desc: "Portfolio-weighted trailing price-to-earnings. Names without an earnings figure (loss-makers, missing data) drop out of both numerator and denominator.",
    range: "S&P 500 average sits around 20–25. Above 30 is growth-tilt; below 15 is value-tilt. Heavily skewed by megacaps when cap-weighted."
  },
  "P/S (wtd)": {
    formula: String.raw`PS_{port} = \dfrac{\sum_i w_i \cdot PS_i}{\sum_i w_i \;:\; PS_i \text{ defined}}`,
    desc: "Portfolio-weighted price-to-sales. Useful when earnings are noisy or negative — sales are more stable across the cycle.",
    range: "Broad market ~2–3×. Tech / high-margin software often 8–15×. Above 20× is rare outside hyper-growth."
  },
  "EV/EBITDA (wtd)": {
    formula: String.raw`\dfrac{\sum_i w_i \cdot (EV/EBITDA)_i}{\sum_i w_i \;:\; EV/EBITDA_i \text{ defined}}`,
    desc: "Portfolio-weighted average EV/EBITDA across covered names. This is a weighted average of constituent multiples, not a reconstructed aggregate enterprise-value-to-aggregate-EBITDA ratio.",
    range: "Mature businesses 8–14×. Quality compounders 15–25×. Above 25× requires sustained growth to justify."
  },
  "Div yield (wtd)": {
    formula: String.raw`y_{port} = \dfrac{\sum_i w_i \cdot y_i}{\sum_i w_i \;:\; y_i \text{ defined}}`,
    desc: "Forward indicated dividend yield across covered names, weighted by portfolio share. Names without a usable dividend figure drop out of both numerator and denominator. Tax-unadjusted and cash-dividend-only.",
    range: "S&P 500 ~1.3–1.8%. Income-tilted books 3–5%. Above 6% often signals stress or capital return at the expense of growth."
  },
  "Market cap (wtd avg)": {
    formula: String.raw`MC_{port} = \sum_i w_i \cdot MC_i`,
    desc: "Portfolio-weighted average market capitalisation. Useful as a quick read on how mega-cap-heavy a book really is.",
    range: "Equal-weighted S&P sits in the low tens of billions; cap-weighted is dragged into the hundreds of billions by the top 7 names."
  },
  "Analyst rating (1=SB, 5=SS)": {
    formula: String.raw`R_{port} = \dfrac{\sum_i w_i \cdot R_i}{\sum_i w_i \;:\; R_i \text{ defined}}`,
    desc: "Mean sell-side analyst rating across covered names. Yahoo's 1–5 scale: 1 Strong Buy → 5 Strong Sell.",
    range: "Most large caps cluster 1.8–2.4 (Buy). Below 1.5 is unusually bullish; above 3.0 leans bearish."
  },
  "Weighted target upside": {
    formula: String.raw`U_{port} = \dfrac{\sum_i w_i \cdot \left(\dfrac{TP_i}{P_i} - 1\right)}{\sum_i w_i \;:\; TP_i, P_i \text{ defined}}`,
    desc: "Coverage-weighted average of analysts' 12-month price-target upside versus current price. Computed in each holding's local currency before weighting; uncovered names drop out of the denominator.",
    range: "Single-digit positive is typical. >20% upside often reflects beaten-down names or aggressive growth assumptions."
  },
  "Analysts covering (sum)": {
    formula: String.raw`N = \sum_i n_i`,
    desc: "Total count of unique analyst opinions across all covered holdings. A coverage-density gauge — high numbers mean the consensus is well-sampled.",
    range: "Megacap names alone often have 30–50 analysts. A diverse 20-name book commonly clears 300+."
  },
  /* --- Concentration --- */
  "Top-5 weight": {
    formula: String.raw`T_5 = \sum_{i \in \text{top 5}} w_i`,
    desc: "Combined weight of the five largest holdings. Direct gauge of concentration risk — how much of the portfolio rides on a handful of names.",
    range: "Diversified funds 15–25%. Active concentrated books 40–60%. Above 70% means a few names dominate the P&L."
  },
  "Herfindahl (HHI)": {
    formula: String.raw`H = \sum_i w_i^2`,
    desc: "Sum of squared weights. The textbook measure of concentration — small when weight is spread out, approaches 1 when one name dominates.",
    range: "An equal-weighted N-stock book has HHI = 1/N. <0.10 well-spread · 0.10–0.20 moderate · >0.25 concentrated."
  },
  "Effective # of names": {
    formula: String.raw`N_{eff} = \dfrac{1}{H} = \dfrac{1}{\sum_i w_i^2}`,
    desc: "Reciprocal of HHI. Reads as 'this portfolio behaves like N equally-weighted names.' Falls below the raw count whenever weights are uneven.",
    range: "Equal-weight: N_eff equals the holding count. Cap-weighted megacap books often have N_eff of 3–6 even with 20+ holdings."
  },
  "Active holdings": {
    formula: String.raw`|\{i : w_i > 0\}|`,
    desc: "Number of positions with non-zero weight that have usable price history (i.e. survived the analytics download).",
    range: "Anything below your input count means some symbols were dropped — see 'Dropped (no history)' for the list."
  },
  "Dropped (no history)": {
    formula: String.raw`\text{symbols}\notin\text{price history}`,
    desc: "Constituents that returned no usable price series for the chosen period (rate-limited fetch, new listings, delisted, or bad ticker).",
    range: "Empty is ideal. If recurring, hit Build again — yfinance's rate-limiter sometimes drops a couple of symbols on the first pass."
  },
};

function renderStatsHtml(a) {
  const s = a.stats || {};
  const sp = a.spy_stats || {};
  const cls = (v) => v == null ? "" : (v >= 0 ? "pos" : "neg");
  const rows = [
    ["Period return", fmtPctSigned(s.total_return), cls(s.total_return), fmtPctSigned(sp.total_return)],
    ["Ann. return", fmtPctSigned(s.ann_return), cls(s.ann_return), fmtPctSigned(sp.ann_return)],
    ["Ann. vol", fmtPctPlain(s.ann_vol), "", fmtPctPlain(sp.ann_vol)],
    ["Sharpe", fmtNumOr(s.sharpe, 2), "", fmtNumOr(sp.sharpe, 2)],
    ["Sortino", fmtNumOr(s.sortino, 2), "", fmtNumOr(sp.sortino, 2)],
    ["Max drawdown", fmtPctSigned(s.max_dd), cls(s.max_dd), fmtPctSigned(sp.max_dd)],
    ["Calmar", fmtNumOr(s.calmar, 2), "", fmtNumOr(sp.calmar, 2)],
    ["Beta (SPY)", fmtNumOr(s.beta_spy, 2), "", fmtNumOr(sp.beta_spy, 2)],
    ["R² (SPY)", fmtNumOr(s.r2_spy, 2), "", fmtNumOr(sp.r2_spy, 2)],
    ["Tracking err", fmtPctPlain(s.te_spy), "", fmtPctPlain(sp.te_spy)],
  ];
  const html = rows.map(r => {
    const info = METRIC_INFO[r[0]];
    const dataAttr = info ? ` data-info="1" data-metric="${escapeHtml(r[0])}"` : "";
    const tip = metricTipHtml(r[0], info);
    return `
    <div class="pf-stat-row"${dataAttr} title="${info ? '' : 'Portfolio vs SPY'}">
      <span class="l">${r[0]}</span>
      <span class="v ${r[2]}">${r[1]} <span style="color:var(--muted); font-weight:400">/ ${r[3]}</span></span>
      ${tip}
    </div>`;
  }).join("");
  return `<div class="pf-stats">${html}</div>`;
}

/* ---------------------------- Analyst sentiment dashboard ---------------------------- */
const _RATING_BUCKETS = [
  { max: 1.5, key: "strong-buy",  label: "Strong Buy" },
  { max: 2.5, key: "buy",         label: "Buy" },
  { max: 3.5, key: "hold",        label: "Hold" },
  { max: 4.5, key: "sell",        label: "Underperform" },
  { max: Infinity, key: "strong-sell", label: "Sell" },
];
function ratingBucket(mr) {
  if (mr == null || !isFinite(mr)) return null;
  for (const b of _RATING_BUCKETS) if (mr <= b.max) return b;
  return _RATING_BUCKETS[_RATING_BUCKETS.length - 1];
}
function recKeyToClass(k) {
  if (!k) return "none";
  k = String(k).toLowerCase().replace(/_/g, "-");
  if (k === "strongbuy") return "strong-buy";
  if (k === "strongsell" || k === "underperform") return "strong-sell";
  return k;
}
function recKeyLabel(k) {
  if (!k) return "—";
  return String(k).replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}
function quickAnalystPreview() {
  const rows = (DATA || []).filter(r => r && r.symbol);
  if (!rows.length) return null;
  const weights = weightsForMode(STATE.mode);
  let ratingNum = 0, ratingW = 0, upsideNum = 0, upsideW = 0;
  let nAnalystsTotal = 0;
  const distSum = {strongBuy: 0, buy: 0, hold: 0, sell: 0, strongSell: 0};
  let distW = 0;
  const holdings = [];
  const notCovered = [];
  for (const row of rows) {
    const symbol = row.symbol;
    const weight = Number(weights[symbol] || 0);
    const meanRating = row.recommendation_mean;
    const price = row.price;
    const targetMean = row.target_mean_price;
    const upside = (targetMean != null && price != null && isFinite(targetMean) && isFinite(price) && price > 0)
      ? ((targetMean / price - 1) * 100)
      : null;
    if (meanRating != null && isFinite(meanRating)) {
      ratingNum += meanRating * weight;
      ratingW += weight;
    }
    if (upside != null && isFinite(upside)) {
      upsideNum += upside * weight;
      upsideW += weight;
    }
    if (row.n_analysts != null && isFinite(row.n_analysts)) nAnalystsTotal += Math.trunc(Number(row.n_analysts));
    const rowDist = row.rating_dist || null;
    if (rowDist && typeof rowDist === "object") {
      const totVotes = (rowDist.strongBuy||0) + (rowDist.buy||0) + (rowDist.hold||0) + (rowDist.sell||0) + (rowDist.strongSell||0);
      if (totVotes > 0) {
        for (const k of Object.keys(distSum)) distSum[k] += (rowDist[k] || 0) * weight;
        distW += weight;
      }
    }
    const hasCoverage = (meanRating != null && isFinite(meanRating)) || (upside != null && isFinite(upside));
    const payload = {
      symbol,
      name: row.name || symbol,
      currency: row.currency || FX_QUOTE,
      weight,
      price,
      target_mean: targetMean,
      target_median: null,
      target_low: null,
      target_high: null,
      upside_pct: upside,
      mean_rating: meanRating,
      rec_key: row.rec_key || null,
      n_analysts: row.n_analysts != null ? row.n_analysts : null,
      dist: rowDist,
    };
    if (hasCoverage) holdings.push(payload);
    else notCovered.push({symbol, name: row.name || symbol, weight});
  }
  let distributionPct = null;
  if (distW > 0) {
    const distTotalW = Object.values(distSum).reduce((a, b) => a + b, 0);
    if (distTotalW > 0) {
      distributionPct = {};
      for (const k of Object.keys(distSum)) distributionPct[k] = (distSum[k] / distTotalW) * 100;
    }
  }
  holdings.sort((a, b) => (b.weight || 0) - (a.weight || 0) || String(a.symbol).localeCompare(String(b.symbol)));
  return {
    display_ccy: FX_QUOTE,
    analyst: {
      mean_rating: ratingW > 0 ? (ratingNum / ratingW) : null,
      rating_coverage_weight: ratingW,
      weighted_target_upside_pct: upsideW > 0 ? (upsideNum / upsideW) : null,
      target_coverage_weight: upsideW,
      n_analysts_total: nAnalystsTotal || null,
      distribution_pct: distributionPct,
      holdings,
      not_covered: notCovered,
      covered_count: holdings.length,
      active_count: rows.length,
    },
  };
}
let _AN_SORT = { key: "weight", dir: -1 };
function renderAnalystDashboard(a) {
  const host = document.getElementById("analyst-dashboard");
  if (!host) return;
  const an = (a && a.analyst) || {};
  const holdings = Array.isArray(an.holdings) ? an.holdings.slice() : [];
  const notCovered = Array.isArray(an.not_covered) ? an.not_covered : [];
  const active = an.active_count || 0;
  const covered = an.covered_count || 0;
  const coverageWeight = an.target_coverage_weight || 0;
  const dist = an.distribution_pct;  // {strongBuy, buy, hold, sell, strongSell} as %
  const wRating = an.mean_rating;
  const wUpside = an.weighted_target_upside_pct;
  const nAnalysts = an.n_analysts_total || 0;

  if (STATE.analyticsLoading && !holdings.length && !notCovered.length) {
    host.innerHTML = `
      <div class="an-head">
        <span class="an-title">Analyst Sentiment</span>
        <span class="an-sub">Portfolio-weighted analyst consensus from yfinance.</span>
      </div>
      <div class="an-grid"><div class="an-card"><div class="lc-block">${lcHtml("loading analyst sentiment", {bar: true})}</div></div></div>
      <div class="an-coverage-foot"><span></span><span class="an-version">${versionLabel()}</span></div>
    `;
    return;
  }

  // Empty-state placeholder
  if (!holdings.length && !notCovered.length) {
    host.innerHTML = `
      <div class="an-head">
        <span class="an-title">Analyst Sentiment</span>
        <span class="an-sub">Build a portfolio to see weighted analyst consensus, rating distribution and price-target upside.</span>
      </div>
      <div class="an-grid"><div class="an-card"><div style="color:var(--muted);font-size:12px;padding:6px 0">No analyst data yet.</div></div></div>
      <div class="an-coverage-foot"><span></span><span class="an-version">${versionLabel()}</span></div>
    `;
    return;
  }

  const bucket = ratingBucket(wRating);
  const needlePct = wRating != null ? Math.max(0, Math.min(100, ((wRating - 1) / 4) * 100)) : 50;

  const card1 = `
    <div class="an-card">
      <h4>Weighted rating <span class="an-sub">${escapeHtml(labelForMode(STATE.mode))}</span></h4>
      <div>
        <span class="an-rating-num">${wRating != null ? wRating.toFixed(2) : "—"}</span>
        ${bucket ? `<span class="an-rating-key ${bucket.key}">${bucket.label}</span>` : ""}
        <span style="color:var(--muted);font-size:11px;margin-left:6px;">/ 5</span>
      </div>
      <div class="an-gauge"><div class="needle" style="left:${needlePct.toFixed(1)}%"></div></div>
      <div class="an-gauge-labels"><span>Strong Buy</span><span>Buy</span><span>Hold</span><span>Underperform</span><span>Sell</span></div>
      <div class="an-rating-meta">
        <span>Coverage <b>${covered}/${active}</b> positions${an.rating_coverage_weight != null ? ` · <b>${(an.rating_coverage_weight*100).toFixed(0)}%</b> of weight` : ""}</span>
        <span>Total analyst opinions: <b>${nAnalysts}</b></span>
      </div>
    </div>`;

  const card2 = (() => {
    if (!dist) {
      return `<div class="an-card"><h4>Rating distribution</h4>
        <div style="color:var(--muted);font-size:12px;padding:6px 0">No distribution data — coverage names lack a recent recommendations row.</div></div>`;
    }
    const total = (dist.strongBuy || 0) + (dist.buy || 0) + (dist.hold || 0) + (dist.sell || 0) + (dist.strongSell || 0);
    const norm = total > 0 ? total : 1;
    const seg = (v) => Math.max(0, (v || 0) / norm * 100);
    const lbl = (k) => seg(dist[k]) >= 6 ? Math.round(seg(dist[k])) + "%" : "";
    return `
    <div class="an-card">
      <h4>Rating distribution <span class="an-sub">weighted</span></h4>
      <div class="an-dist-bar">
        <div class="sb" style="flex:${seg(dist.strongBuy).toFixed(2)}" title="Strong Buy ${seg(dist.strongBuy).toFixed(1)}%">${lbl("strongBuy")}</div>
        <div class="b"  style="flex:${seg(dist.buy).toFixed(2)}"        title="Buy ${seg(dist.buy).toFixed(1)}%">${lbl("buy")}</div>
        <div class="h"  style="flex:${seg(dist.hold).toFixed(2)}"       title="Hold ${seg(dist.hold).toFixed(1)}%">${lbl("hold")}</div>
        <div class="s"  style="flex:${seg(dist.sell).toFixed(2)}"       title="Sell ${seg(dist.sell).toFixed(1)}%">${lbl("sell")}</div>
        <div class="ss" style="flex:${seg(dist.strongSell).toFixed(2)}" title="Strong Sell ${seg(dist.strongSell).toFixed(1)}%">${lbl("strongSell")}</div>
      </div>
      <div class="an-dist-legend">
        <span><i style="background:#15803d"></i>Strong Buy ${seg(dist.strongBuy).toFixed(1)}%</span>
        <span><i style="background:#22c55e"></i>Buy ${seg(dist.buy).toFixed(1)}%</span>
        <span><i style="background:#eab308"></i>Hold ${seg(dist.hold).toFixed(1)}%</span>
        <span><i style="background:#f97316"></i>Sell ${seg(dist.sell).toFixed(1)}%</span>
        <span><i style="background:#dc2626"></i>Strong Sell ${seg(dist.strongSell).toFixed(1)}%</span>
      </div>
    </div>`;
  })();

  const card3 = (() => {
    const upCls = wUpside == null ? "" : (wUpside >= 0 ? "pos" : "neg");
    const upSign = wUpside == null ? "" : (wUpside >= 0 ? "+" : "");
    const covPct = an.target_coverage_weight != null ? (an.target_coverage_weight * 100) : 0;
    return `
    <div class="an-card">
      <h4>Consensus price target upside <span class="an-sub">vs current</span></h4>
      <div>
        <span class="an-upside-num ${upCls}">${wUpside == null ? "—" : (upSign + wUpside.toFixed(2) + "%")}</span>
        <span style="color:var(--muted);font-size:11px;margin-left:8px;">12-month consensus</span>
      </div>
      <div class="an-upside-meta">
        <span>Computed in each position's local currency, then weighted by portfolio share.</span>
        <span>Coverage: <b>${covPct.toFixed(0)}%</b> of portfolio weight has price-target data.</span>
      </div>
      <div class="an-coverage-bar" title="Share of portfolio weight with analyst price targets"><div class="fill" style="width:${covPct.toFixed(1)}%"></div></div>
    </div>`;
  })();

  // Per-holding table
  const sortKey = _AN_SORT.key, sortDir = _AN_SORT.dir;
  holdings.sort((x, y) => {
    let av = x[sortKey], bv = y[sortKey];
    if (sortKey === "rec_key") { av = x.mean_rating; bv = y.mean_rating; }
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "number") return (av - bv) * sortDir;
    return String(av).localeCompare(String(bv)) * sortDir;
  });
  const arrow = (k) => sortKey === k ? `<span class="arrow">${sortDir > 0 ? "▲" : "▼"}</span>` : "";
  const fmtUp = (v) => v == null ? "—" : ((v >= 0 ? "+" : "") + v.toFixed(2) + "%");

  const rowsHtml = holdings.map(h => {
    const upCls = h.upside_pct == null ? "" : (h.upside_pct >= 0 ? "pos" : "neg");
    const cls = recKeyToClass(h.rec_key || (ratingBucket(h.mean_rating) || {}).key);
    const lbl = h.rec_key ? recKeyLabel(h.rec_key) : ((ratingBucket(h.mean_rating) || {}).label || "—");
    let mini = "";
    if (h.dist) {
      const tot = (h.dist.strongBuy||0)+(h.dist.buy||0)+(h.dist.hold||0)+(h.dist.sell||0)+(h.dist.strongSell||0);
      const s = (v) => tot > 0 ? (v || 0) / tot * 100 : 0;
      mini = `<span class="an-mini-dist" title="SB ${h.dist.strongBuy||0} · B ${h.dist.buy||0} · H ${h.dist.hold||0} · S ${h.dist.sell||0} · SS ${h.dist.strongSell||0}">
        <div class="sb" style="width:${s(h.dist.strongBuy).toFixed(2)}%"></div>
        <div class="b"  style="width:${s(h.dist.buy).toFixed(2)}%"></div>
        <div class="h"  style="width:${s(h.dist.hold).toFixed(2)}%"></div>
        <div class="s"  style="width:${s(h.dist.sell).toFixed(2)}%"></div>
        <div class="ss" style="width:${s(h.dist.strongSell).toFixed(2)}%"></div>
      </span>`;
    } else {
      mini = `<span style="color:var(--muted);font-size:10.5px">—</span>`;
    }
    return `<tr>
      <td class="sym">${escapeHtml(h.symbol)}<span class="muted">${escapeHtml((h.name || "").length > 22 ? h.name.slice(0, 22) + "…" : (h.name || ""))}</span></td>
      <td>${(h.weight*100).toFixed(2)}%</td>
      <td><span class="rk ${cls}">${escapeHtml(lbl)}</span></td>
      <td>${h.mean_rating != null ? h.mean_rating.toFixed(2) : "—"}</td>
      <td>${h.n_analysts != null ? h.n_analysts : "—"}</td>
      <td>${h.price != null ? fmtMoney(h.price, h.currency) : "—"}</td>
      <td>${h.target_mean != null ? fmtMoney(h.target_mean, h.currency) : "—"}</td>
      <td class="${upCls}">${fmtUp(h.upside_pct)}</td>
      <td>${mini}</td>
    </tr>`;
  }).join("");

  const tableHtml = holdings.length ? `
    <div class="an-card" style="grid-column: 1 / -1;">
      <h4>Per-holding consensus <span class="an-sub">click a column to sort</span></h4>
      <div class="an-table-wrap">
        <table class="an-table" id="an-table">
          <thead><tr>
            <th data-k="symbol">Symbol${arrow("symbol")}</th>
            <th data-k="weight">Weight${arrow("weight")}</th>
            <th data-k="rec_key">Consensus${arrow("rec_key")}</th>
            <th data-k="mean_rating">Rating${arrow("mean_rating")}</th>
            <th data-k="n_analysts"># Analysts${arrow("n_analysts")}</th>
            <th data-k="price">Price${arrow("price")}</th>
            <th data-k="target_mean">Target (mean)${arrow("target_mean")}</th>
            <th data-k="upside_pct">Upside${arrow("upside_pct")}</th>
            <th>Distribution</th>
          </tr></thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
    </div>` : "";

  const notCoveredHtml = notCovered.length ? (() => {
    const totalW = notCovered.reduce((a, n) => a + (n.weight || 0), 0);
    const names = notCovered.slice(0, 6).map(n => `${escapeHtml(n.symbol)}${n.weight ? ` (${(n.weight*100).toFixed(1)}%)` : ""}`).join(", ");
    return `<span class="an-not-covered">${notCovered.length} position${notCovered.length>1?"s":""} without analyst coverage — <b>${(totalW*100).toFixed(1)}%</b> of weight: ${names}${notCovered.length > 6 ? "…" : ""}</span>`;
  })() : "";

  host.innerHTML = `
    <div class="an-head">
      <span class="an-title">Analyst Sentiment</span>
      <span class="an-sub">Portfolio-weighted analyst consensus from yfinance · positions in ${escapeHtml(a.display_ccy || FX_QUOTE)}.${STATE.analyticsLoading ? ` ${lcHtml("refreshing", {bar: true})}` : ""}</span>
      <span class="an-mode-note">Aggregated by
        <button type="button" class="an-mode-btn" id="an-mode-btn" aria-haspopup="listbox" aria-expanded="false">
          <span id="an-mode-label">${escapeHtml(labelForMode(STATE.mode))}</span>
          <span class="an-caret">▾</span>
        </button>
        <div class="an-mode-menu" id="an-mode-menu" role="listbox" aria-label="Aggregation method">
          <div class="an-mode-opt" data-mode="equal"  role="option" data-selected="${STATE.mode === 'equal' ? '1' : '0'}">Equal-weight</div>
          <div class="an-mode-opt" data-mode="cap"    role="option" data-selected="${STATE.mode === 'cap' ? '1' : '0'}">Cap-weighted</div>
          ${(STATE.weightPresets || []).map(p => `<div class="an-mode-opt" data-mode="${escapeHtml(modeId(p.name))}" role="option" data-selected="${STATE.mode === modeId(p.name) ? '1' : '0'}">${escapeHtml(p.name)}</div>`).join("")}
          ${STATE.mode === 'custom' ? `<div class="an-mode-opt" data-mode="custom" role="option" data-selected="1">Custom (unsaved)</div>` : ""}
        </div>
      </span>
    </div>
    <div class="an-grid">${card1}${card2}${card3}${tableHtml}</div>
    <div class="an-coverage-foot">
      <span>${notCoveredHtml}</span>
      <span class="an-version">${versionLabel()}</span>
    </div>
  `;

  // Wire up the aggregation-mode dropdown in the header.
  const btn = document.getElementById("an-mode-btn");
  const menu = document.getElementById("an-mode-menu");
  if (btn && menu) {
    const closeMenu = () => {
      menu.classList.remove("show");
      btn.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
      document.removeEventListener("mousedown", onOutside, true);
      document.removeEventListener("keydown", onEsc, true);
    };
    function onOutside(e) {
      if (e.target.closest("#an-mode-btn") || e.target.closest("#an-mode-menu")) return;
      closeMenu();
    }
    function onEsc(e) { if (e.key === "Escape") closeMenu(); }
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = menu.classList.contains("show");
      if (isOpen) { closeMenu(); return; }
      menu.classList.add("show");
      btn.classList.add("open");
      btn.setAttribute("aria-expanded", "true");
      document.addEventListener("mousedown", onOutside, true);
      document.addEventListener("keydown", onEsc, true);
    });
    menu.addEventListener("click", (e) => {
      const opt = e.target.closest(".an-mode-opt");
      if (!opt) return;
      const mode = opt.dataset.mode;
      closeMenu();
      if (!mode) return;
      if (mode === "custom" && STATE.mode !== "custom") { openWeightsPopup({intent: "adhoc"}); return; }
      selectMode(mode);
    });
  }

  // Wire up column sorting on the per-holding table.
  const tbl = document.getElementById("an-table");
  if (tbl) {
    tbl.querySelectorAll("th[data-k]").forEach(th => {
      th.addEventListener("click", () => {
        const k = th.dataset.k;
        if (_AN_SORT.key === k) _AN_SORT.dir = -_AN_SORT.dir;
        else { _AN_SORT.key = k; _AN_SORT.dir = (k === "symbol" ? 1 : -1); }
        renderAnalystDashboard(STATE.analytics);
      });
    });
  }
}

function renderStatTipsKatex(rootOrSelector = "#pf-analytics-body") {
  if (!window.renderMathInElement) return;
  const root = typeof rootOrSelector === "string"
    ? document.querySelector(rootOrSelector)
    : rootOrSelector;
  if (!root) return;
  root.querySelectorAll(".pf-metric-tip").forEach(el => {
    if (el.dataset.katex === "1") return;
    try {
      renderMathInElement(el, { delimiters: [{ left: "$$", right: "$$", display: true }], throwOnError: false });
      el.dataset.katex = "1";
    } catch (_) {}
  });
}

function statRowHtml(label, value, valueCls) {
  const info = METRIC_INFO[label];
  const dataAttr = info ? ` data-info="1" data-metric="${escapeHtml(label)}"` : "";
  const tip = metricTipHtml(label, info);
  return `<div class="pf-stat-row"${dataAttr}>
    <span class="l">${label}</span>
    <span class="v ${valueCls || ""}">${value}</span>
    ${tip}
  </div>`;
}

function renderValuationAnalystHtml(a) {
  const w = a.weighted || {};
  const an = a.analyst || {};
  const rows = [
    ["P/E (wtd)", fmtNumOr(w.pe, 2)],
    ["P/S (wtd)", fmtNumOr(w.ps, 2)],
    ["EV/EBITDA (wtd)", fmtNumOr(w.ev_ebitda, 2)],
    ["Div yield (wtd)", w.div_yield == null ? "—" : fmtPctFrac(w.div_yield)],
    ["Market cap (wtd avg)", fmtCapBig(w.market_cap)],
    ["Analyst rating (1=SB, 5=SS)", fmtNumOr(an.mean_rating, 2)],
    ["Weighted target upside", fmtPctSigned(an.weighted_target_upside_pct), an.weighted_target_upside_pct == null ? "" : (an.weighted_target_upside_pct >= 0 ? "pos" : "neg")],
    ["Analysts covering (sum)", an.n_analysts_total != null ? an.n_analysts_total : "—"],
  ];
  return `<div class="pf-stats">${rows.map(r => statRowHtml(r[0], r[1], r[2])).join("")}</div>`;
}

function renderConcentrationHtml(a) {
  const c = a.concentration || {};
  const rows = [
    ["Top-5 weight", fmtPctPlain((c.top5 || 0) * 100, 1)],
    ["Herfindahl (HHI)", fmtNumOr(c.herfindahl, 3)],
    ["Effective # of names", fmtNumOr(c.effective_n, 1)],
    ["Active holdings", (a.active_symbols || []).length],
    ["Dropped (no history)", (a.missing_symbols || []).join(", ") || "—"],
  ];
  return `<div class="pf-stats">${rows.map(r => statRowHtml(r[0], r[1])).join("")}</div>`;
}

function renderBarsHtml(map) {
  if (!map) return `<div class="pf-empty" style="padding:6px 0;">—</div>`;
  const entries = Object.entries(map).filter(([k, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return `<div class="pf-empty" style="padding:6px 0;">No data</div>`;
  const max = entries[0][1];
  return `<div class="pf-bars">${entries.map(([k, v]) => `
    <div class="pf-bar">
      <span class="pf-bar-name" title="${escapeHtml(k)}">${escapeHtml(k)}</span>
      <div class="pf-bar-track"><div class="pf-bar-fill" style="width:${((v/max)*100).toFixed(1)}%"></div></div>
      <span class="pf-bar-val">${(v*100).toFixed(1)}%</span>
    </div>`).join("")}</div>`;
}

function renderContribHtml(a) {
  const list = a.contribution || [];
  if (!list.length) return `<div class="pf-empty" style="padding:6px 0;">No contribution data</div>`;
  return `<table class="pf-contrib-table">
    <thead><tr><th>Symbol</th><th>Weight</th><th>${escapeHtml(STATE.period)} return</th><th>Contribution</th></tr></thead>
    <tbody>${list.map(c => `<tr>
      <td title="${escapeHtml(c.name || c.symbol)}">${escapeHtml(c.symbol)} <span style="color:var(--muted)">${escapeHtml(c.sector || "")}</span></td>
      <td>${(c.weight*100).toFixed(2)}%</td>
      <td class="${c.period_return >= 0 ? 'pos':'neg'}">${fmtPctSigned(c.period_return)}</td>
      <td class="${c.contribution >= 0 ? 'pos':'neg'}">${fmtPctSigned(c.contribution)}</td>
    </tr>`).join("")}</tbody>
  </table>`;
}

function drawPortfolioChart(a, hostEl, legendEl) {
  const series = a.series || {};
  const port = series.portfolio || [];
  if (port.length < 2) { hostEl.innerHTML = `<div class="pf-empty">Not enough data to plot.</div>`; return; }
  const showSpy = STATE.showSpy && series.spy && series.spy.length;
  const showNdx = STATE.showNdx && series.nasdaq && series.nasdaq.length;
  const showSec = STATE.showSec && series.sector_mix && series.sector_mix.length;
  const showDd  = STATE.showDd && series.drawdown && series.drawdown.length;

  const W = 720, H = 220;
  const padL = 36, padR = 12, padT = 8, padB = 22;
  const t0 = port[0][0], t1 = port[port.length-1][0];
  const xScale = (t) => padL + ((t - t0) / Math.max(1, (t1 - t0))) * (W - padL - padR);
  let lo = Infinity, hi = -Infinity;
  for (const p of port) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (showSpy) for (const p of series.spy) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (showNdx) for (const p of series.nasdaq) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (showSec) for (const p of series.sector_mix) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  const pad = (hi - lo) * 0.06 || 1;
  lo -= pad; hi += pad;
  const yScale = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);
  const path = (pts) => {
    let s = "";
    for (let i = 0; i < pts.length; i++) {
      const x = xScale(pts[i][0]).toFixed(1), y = yScale(pts[i][1]).toFixed(1);
      s += (i === 0 ? "M" : "L") + x + "," + y + " ";
    }
    return s;
  };
  const ticks = [];
  for (let i = 0; i <= 4; i++) { const v = lo + (hi - lo) * (i/4); ticks.push({v, y: yScale(v)}); }
  const N_XT = 5; const xt = [];
  for (let i = 0; i <= N_XT; i++) { const t = t0 + (t1-t0)*(i/N_XT); xt.push({t, x: xScale(t)}); }
  const fmtT = (ts) => {
    const d = new Date(ts);
    if (STATE.period === "3M" || STATE.period === "6M") return d.toLocaleDateString(undefined, {month:"short", day:"numeric"});
    if (STATE.period === "YTD" || STATE.period === "1Y") return d.toLocaleDateString(undefined, {month:"short", year:"2-digit"});
    return d.toLocaleDateString(undefined, {year:"numeric"});
  };
  const accent = "var(--accent)";
  const svg = `<svg id="pf-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    ${ticks.map(t => `<line x1="${padL}" y1="${t.y.toFixed(1)}" x2="${W-padR}" y2="${t.y.toFixed(1)}" stroke="var(--border)" stroke-width="0.5" stroke-dasharray="2 3"/>`).join("")}
    ${ticks.map(t => `<text x="${padL-6}" y="${(t.y+3).toFixed(1)}" font-size="10" fill="var(--muted)" text-anchor="end">${t.v.toFixed(0)}</text>`).join("")}
    ${xt.map(t => `<text x="${t.x.toFixed(1)}" y="${(H-padB+12).toFixed(0)}" font-size="10" fill="var(--muted)" text-anchor="middle">${fmtT(t.t)}</text>`).join("")}
    ${showSec ? `<path d="${path(series.sector_mix)}" fill="none" stroke="#f59e0b" stroke-width="1.4" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
    ${showNdx ? `<path d="${path(series.nasdaq)}" fill="none" stroke="#06b6d4" stroke-width="1.4" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
    ${showSpy ? `<path d="${path(series.spy)}" fill="none" stroke="#8b5cf6" stroke-width="1.4" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
    <path d="${path(port)}" fill="none" stroke="${accent}" stroke-width="2"/>
    <rect class="pf-sel" id="pf-sel" x="0" y="${padT}" width="0" height="${H-padT-padB}" style="display:none"/>
    <line class="pf-cross" id="pf-cv" x1="0" x2="0" y1="${padT}" y2="${H-padB}"/>
    <line class="pf-cross" id="pf-ch" y1="0" y2="0" x1="${padL}" x2="${W-padR}"/>
    <circle class="pf-dot" id="pf-dot" r="4" cx="0" cy="0"/>
    ${showSpy ? `<circle class="pf-dot spy" id="pf-dot-spy" r="3.5" cx="0" cy="0"/>` : ""}
    ${showNdx ? `<circle class="pf-dot ndx" id="pf-dot-ndx" r="3.5" cx="0" cy="0"/>` : ""}
    ${showSec ? `<circle class="pf-dot sec" id="pf-dot-sec" r="3.5" cx="0" cy="0"/>` : ""}
    <rect id="pf-overlay" x="${padL}" y="${padT}" width="${W-padL-padR}" height="${H-padT-padB}" fill="transparent" style="cursor:crosshair"/>
  </svg>`;
  let ddSvg = "";
  if (showDd) {
    const dd = series.drawdown;
    const W2 = W, H2 = 80, padT2 = 4, padB2 = 12;
    const minDd = Math.min(...dd.map(p => p[1])); const maxDd = 0;
    const yS2 = (v) => padT2 + (1 - (v - minDd) / Math.max(1e-9, (maxDd - minDd))) * (H2 - padT2 - padB2);
    const path2 = (pts) => {
      let s = "";
      for (let i = 0; i < pts.length; i++) {
        const x = xScale(pts[i][0]).toFixed(1), y = yS2(pts[i][1]).toFixed(1);
        s += (i === 0 ? "M" : "L") + x + "," + y + " ";
      }
      return s + ` L ${xScale(dd[dd.length-1][0]).toFixed(1)},${yS2(0).toFixed(1)} L ${xScale(dd[0][0]).toFixed(1)},${yS2(0).toFixed(1)} Z`;
    };
    ddSvg = `<svg class="pf-dd-svg" viewBox="0 0 ${W2} ${H2}" preserveAspectRatio="none">
      <line x1="${padL}" y1="${yS2(0).toFixed(1)}" x2="${W-padR}" y2="${yS2(0).toFixed(1)}" stroke="var(--border)" stroke-width="0.5"/>
      <path d="${path2(dd)}" fill="rgba(248,81,73,0.18)" stroke="#f85149" stroke-width="1.3"/>
      <text x="${padL-6}" y="${(yS2(minDd)+3).toFixed(1)}" font-size="10" fill="var(--muted)" text-anchor="end">${minDd.toFixed(0)}%</text>
      <text x="${padL-6}" y="${(yS2(0)+3).toFixed(1)}" font-size="10" fill="var(--muted)" text-anchor="end">0</text>
    </svg>`;
  }
  hostEl.innerHTML = svg + ddSvg + `<div class="pf-tt" id="pf-tt"></div>` + `<div class="pf-chart-info" id="pf-chart-info"></div>`;
  const legend = [];
  legend.push(`<span><i style="background:#2f81f7"></i> Portfolio</span>`);
  if (showSpy) legend.push(`<span><i style="background:#8b5cf6"></i> SPY</span>`);
  if (showNdx) legend.push(`<span><i style="background:#06b6d4"></i> NASDAQ</span>`);
  if (showSec) legend.push(`<span><i style="background:#f59e0b"></i> Sector mix</span>`);
  if (showDd)  legend.push(`<span><i style="background:#f85149"></i> Drawdown</span>`);
  legendEl.innerHTML = legend.join("");

  attachPortfolioChartInteraction({
    W, H, padL, padR, padT, padB,
    t0, t1, xScale, yScale,
    port, spy: showSpy ? series.spy : null,
    ndx: showNdx ? series.nasdaq : null,
    sec: showSec ? series.sector_mix : null,
    hostEl,
  });
}

function attachPortfolioChartInteraction(g) {
  const svg = document.getElementById("pf-svg");
  const overlay = document.getElementById("pf-overlay");
  if (!svg || !overlay) return;
  const tt = document.getElementById("pf-tt");
  const cv = document.getElementById("pf-cv");
  const ch = document.getElementById("pf-ch");
  const dot = document.getElementById("pf-dot");
  const dotSpy = document.getElementById("pf-dot-spy");
  const dotNdx = document.getElementById("pf-dot-ndx");
  const dotSec = document.getElementById("pf-dot-sec");
  const sel = document.getElementById("pf-sel");
  const info = document.getElementById("pf-chart-info");
  const wrap = g.hostEl;

  const defaultInfo = () => {
    const port = g.port;
    if (!port.length) return "";
    const pct = (port[port.length-1][1] / port[0][1] - 1) * 100;
    const cls = pct >= 0 ? "pos" : "neg";
    const parts = [
      `<span><b class="${cls}">${(pct>=0?"+":"")+pct.toFixed(2)}%</b> · Portfolio</span>`,
    ];
    if (g.spy) {
      const p = (g.spy[g.spy.length-1][1] / g.spy[0][1] - 1) * 100;
      parts.push(`<span><b class="${p>=0?'pos':'neg'}">${(p>=0?"+":"")+p.toFixed(2)}%</b> · SPY</span>`);
    }
    if (g.ndx) {
      const p = (g.ndx[g.ndx.length-1][1] / g.ndx[0][1] - 1) * 100;
      parts.push(`<span><b class="${p>=0?'pos':'neg'}">${(p>=0?"+":"")+p.toFixed(2)}%</b> · NASDAQ</span>`);
    }
    if (g.sec) {
      const p = (g.sec[g.sec.length-1][1] / g.sec[0][1] - 1) * 100;
      parts.push(`<span><b class="${p>=0?'pos':'neg'}">${(p>=0?"+":"")+p.toFixed(2)}%</b> · Sector mix</span>`);
    }
    parts.push(`<span class="selection-hint">drag on chart to measure a sub-period →</span>`);
    return parts.join("");
  };
  info.innerHTML = defaultInfo();

  const nearestIdx = (pts, t) => {
    if (!pts || !pts.length) return -1;
    let lo = 0, hi = pts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (pts[mid][0] < t) lo = mid + 1; else hi = mid;
    }
    if (lo > 0 && Math.abs(pts[lo - 1][0] - t) < Math.abs(pts[lo][0] - t)) return lo - 1;
    return lo;
  };
  const clientToSvgX = (clientX) => {
    const r = svg.getBoundingClientRect();
    return (clientX - r.left) * (g.W / r.width);
  };
  const pxToData = (px) => g.t0 + (px - g.padL) / (g.W - g.padL - g.padR) * (g.t1 - g.t0);

  let dragging = false, dragStartT = null;

  overlay.addEventListener("mousemove", (e) => {
    const svgX = clientToSvgX(e.clientX);
    const t = pxToData(Math.max(g.padL, Math.min(g.W - g.padR, svgX)));
    const i = nearestIdx(g.port, t);
    if (i < 0) return;
    const sp = g.port[i];
    const x = g.xScale(sp[0]);
    const y = g.yScale(sp[1]);
    cv.setAttribute("x1", x); cv.setAttribute("x2", x); cv.style.opacity = 1;
    ch.setAttribute("y1", y); ch.setAttribute("y2", y); ch.style.opacity = 1;
    dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.style.opacity = 1;
    const pct = (sp[1] / g.port[0][1] - 1) * 100;
    let extra = "";
    if (dotSpy && g.spy) {
      const j = nearestIdx(g.spy, sp[0]);
      if (j >= 0) {
        const px = g.xScale(g.spy[j][0]); const py = g.yScale(g.spy[j][1]);
        dotSpy.setAttribute("cx", px); dotSpy.setAttribute("cy", py); dotSpy.style.opacity = 1;
        const sPct = (g.spy[j][1] / g.spy[0][1] - 1) * 100;
        extra += `<div class="tt-row"><span>SPY</span><b class="${sPct>=0?'pos':'neg'}">${(sPct>=0?'+':'')+sPct.toFixed(2)}%</b></div>`;
      }
    }
    if (dotNdx && g.ndx) {
      const j = nearestIdx(g.ndx, sp[0]);
      if (j >= 0) {
        const px = g.xScale(g.ndx[j][0]); const py = g.yScale(g.ndx[j][1]);
        dotNdx.setAttribute("cx", px); dotNdx.setAttribute("cy", py); dotNdx.style.opacity = 1;
        const nPct = (g.ndx[j][1] / g.ndx[0][1] - 1) * 100;
        extra += `<div class="tt-row"><span>NASDAQ</span><b class="${nPct>=0?'pos':'neg'}">${(nPct>=0?'+':'')+nPct.toFixed(2)}%</b></div>`;
      }
    }
    if (dotSec && g.sec) {
      const j = nearestIdx(g.sec, sp[0]);
      if (j >= 0) {
        const px = g.xScale(g.sec[j][0]); const py = g.yScale(g.sec[j][1]);
        dotSec.setAttribute("cx", px); dotSec.setAttribute("cy", py); dotSec.style.opacity = 1;
        const sPct = (g.sec[j][1] / g.sec[0][1] - 1) * 100;
        extra += `<div class="tt-row"><span>Sector mix</span><b class="${sPct>=0?'pos':'neg'}">${(sPct>=0?'+':'')+sPct.toFixed(2)}%</b></div>`;
      }
    }
    const dt = new Date(sp[0]);
    tt.innerHTML = `
      <div class="tt-date">${fmtDateMDY(dt)}</div>
      <div class="tt-row"><span>Portfolio</span><b class="${pct>=0?'pos':'neg'}">${(pct>=0?'+':'')+pct.toFixed(2)}%</b></div>
      ${extra}
    `;
    tt.classList.add("show");
    const wrapRect = wrap.getBoundingClientRect();
    const lx = e.clientX - wrapRect.left + 12;
    const ly = e.clientY - wrapRect.top - 8;
    const ttRect = tt.getBoundingClientRect();
    const maxX = wrap.clientWidth - ttRect.width - 6;
    tt.style.left = Math.min(lx, Math.max(6, maxX)) + "px";
    tt.style.top = Math.max(6, ly) + "px";

    if (dragging && dragStartT != null) {
      const a = Math.min(dragStartT, sp[0]), b = Math.max(dragStartT, sp[0]);
      const ax = g.xScale(a), bx = g.xScale(b);
      sel.style.display = "";
      sel.setAttribute("x", ax);
      sel.setAttribute("width", Math.max(1, bx - ax));
      const iA = nearestIdx(g.port, a), iB = nearestIdx(g.port, b);
      if (iA >= 0 && iB >= 0 && iA !== iB) {
        const va = g.port[iA][1], vb = g.port[iB][1];
        const pPct = (vb/va - 1) * 100;
        let drag = `<span><b class="${pPct>=0?'pos':'neg'}">${(pPct>=0?'+':'')+pPct.toFixed(2)}%</b> · Portfolio (selection)</span>`;
        if (g.spy) {
          const jA = nearestIdx(g.spy, a), jB = nearestIdx(g.spy, b);
          if (jA >= 0 && jB >= 0 && jA !== jB) {
            const sp2 = (g.spy[jB][1]/g.spy[jA][1] - 1) * 100;
            drag += `<span><b class="${sp2>=0?'pos':'neg'}">${(sp2>=0?'+':'')+sp2.toFixed(2)}%</b> · SPY</span>`;
          }
        }
        if (g.ndx) {
          const jA = nearestIdx(g.ndx, a), jB = nearestIdx(g.ndx, b);
          if (jA >= 0 && jB >= 0 && jA !== jB) {
            const nx = (g.ndx[jB][1]/g.ndx[jA][1] - 1) * 100;
            drag += `<span><b class="${nx>=0?'pos':'neg'}">${(nx>=0?'+':'')+nx.toFixed(2)}%</b> · NASDAQ</span>`;
          }
        }
        if (g.sec) {
          const jA = nearestIdx(g.sec, a), jB = nearestIdx(g.sec, b);
          if (jA >= 0 && jB >= 0 && jA !== jB) {
            const sc = (g.sec[jB][1]/g.sec[jA][1] - 1) * 100;
            drag += `<span><b class="${sc>=0?'pos':'neg'}">${(sc>=0?'+':'')+sc.toFixed(2)}%</b> · Sector mix</span>`;
          }
        }
        drag += `<span class="selection-hint">${fmtDateMDY(g.port[iA][0])} → ${fmtDateMDY(g.port[iB][0])}</span>`;
        info.innerHTML = drag;
      }
    }
  });
  overlay.addEventListener("mouseleave", () => {
    cv.style.opacity = 0; ch.style.opacity = 0; dot.style.opacity = 0;
    if (dotSpy) dotSpy.style.opacity = 0;
    if (dotNdx) dotNdx.style.opacity = 0;
    if (dotSec) dotSec.style.opacity = 0;
    tt.classList.remove("show");
  });
  overlay.addEventListener("mousedown", (e) => {
    dragging = true;
    const svgX = clientToSvgX(e.clientX);
    dragStartT = pxToData(Math.max(g.padL, Math.min(g.W - g.padR, svgX)));
    sel.style.display = "";
    sel.setAttribute("x", g.xScale(dragStartT));
    sel.setAttribute("width", 1);
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false; dragStartT = null;
    // Keep selection visible briefly, then fade back to defaults
    setTimeout(() => {
      sel.style.display = "none";
      info.innerHTML = defaultInfo();
    }, 1800);
  });
}

/* ===========================================================================
 * Custom weights popup
 * --------------------------------------------------------------------------- */
let WEIGHTS_DRAFT = null;  // [{symbol, name, weight (0-1), locked}]

function capWeightsOf(rows) {
  const caps = rows.map(r => Math.max(0, Number(r.market_cap) || 0));
  const total = caps.reduce((a, b) => a + b, 0);
  if (total > 0) return Object.fromEntries(rows.map((r, i) => [r.symbol, caps[i] / total]));
  return Object.fromEntries(rows.map(r => [r.symbol, 1 / Math.max(1, rows.length)]));
}
function equalWeightsOf(rows) {
  if (!rows.length) return {};
  const w = 1 / rows.length;
  return Object.fromEntries(rows.map(r => [r.symbol, w]));
}

// Tracks what the modal is currently editing: "adhoc" (ad-hoc custom, no save),
// "new" (saving a brand-new preset), or "edit" (editing an existing one).
// `editingName` is the original name of the preset under edit so we can rename.
let WEIGHTS_INTENT = "adhoc";
let WEIGHTS_EDITING_NAME = null;

function openWeightsPopup(opts) {
  if (!DATA.length) return toast("Build a portfolio first.");
  opts = opts || {};
  const rows = DATA.filter(r => r && r.symbol);
  // Seed the draft from the most appropriate source:
  //   intent=edit → the preset's saved weights
  //   intent=new  → current resolved mode weights (capture what user sees)
  //   intent=adhoc (default) → existing customWeights or active mode
  let init;
  if (opts.intent === "edit" && opts.presetName) {
    const p = presetByName(opts.presetName);
    init = (p && p.weights) ? p.weights : weightsForMode(STATE.mode);
    WEIGHTS_INTENT = "edit"; WEIGHTS_EDITING_NAME = opts.presetName;
  } else if (opts.intent === "new") {
    init = weightsForMode(STATE.mode);
    WEIGHTS_INTENT = "new"; WEIGHTS_EDITING_NAME = null;
  } else {
    init = STATE.customWeights || weightsForMode(STATE.mode);
    WEIGHTS_INTENT = "adhoc"; WEIGHTS_EDITING_NAME = null;
  }
  WEIGHTS_DRAFT = rows.map(r => ({
    symbol: r.symbol,
    name: r.name || r.symbol,
    weight: Number(init[r.symbol] || 0),
    locked: false,
  }));
  // Wire UI to reflect intent (title, prefilled name, delete-button visibility).
  const titleEl = document.getElementById("pf-weights-title");
  const nameEl = document.getElementById("pf-weights-name");
  const delBtn = document.getElementById("pf-weights-delete");
  const statusEl = document.getElementById("pf-weights-status");
  const saved = STATE.activeView && STATE.activeView !== AD_HOC_KEY;
  if (titleEl) titleEl.textContent =
      WEIGHTS_INTENT === "edit" ? `Edit preset · ${opts.presetName}` :
      WEIGHTS_INTENT === "new"  ? "New weight preset" :
                                  "Custom weights";
  if (nameEl) nameEl.value = WEIGHTS_INTENT === "edit" ? opts.presetName : "";
  if (delBtn) delBtn.style.display = WEIGHTS_INTENT === "edit" ? "" : "none";
  if (statusEl) statusEl.textContent = saved ? "" : "Save the portfolio to enable named presets.";
  // Disable save buttons when there's no active saved portfolio to attach to.
  ["pf-weights-save", "pf-weights-saveas"].forEach(id => {
    const b = document.getElementById(id); if (b) b.disabled = !saved;
  });
  renderWeightsRows();
  $("#pf-weights-bg").classList.add("show");
}

function closeWeightsPopup() {
  $("#pf-weights-bg").classList.remove("show");
  hideInlinePrompt();
}

function renderWeightsRows() {
  const body = $("#pf-weights-body"); body.innerHTML = "";
  WEIGHTS_DRAFT.forEach((row, i) => {
    const r = document.createElement("div");
    r.className = "pf-w-row" + (row.locked ? " locked" : "");
    r.innerHTML = `
      <span class="pf-w-sym" title="${escapeHtml(row.name)}">${escapeHtml(row.symbol)}</span>
      <input class="pf-w-slider" type="range" min="0" max="1" step="0.001" value="${row.weight.toFixed(3)}" ${row.locked ? "disabled" : ""}/>
      <input class="pf-w-num" type="number" min="0" max="100" step="0.1" value="${(row.weight*100).toFixed(2)}"/>
      <button class="pf-w-lock${row.locked ? " on" : ""}" title="Lock">${row.locked ? "🔒" : "🔓"}</button>
    `;
    const slider = r.querySelector(".pf-w-slider");
    const num = r.querySelector(".pf-w-num");
    const lock = r.querySelector(".pf-w-lock");
    slider.addEventListener("input", () => onWeightInput(i, Number(slider.value)));
    num.addEventListener("input", () => onWeightInput(i, Math.max(0, Math.min(100, Number(num.value))) / 100));
    lock.addEventListener("click", () => { row.locked = !row.locked; renderWeightsRows(); updateWeightsSum(); });
    body.appendChild(r);
  });
  updateWeightsSum();
}

function onWeightInput(i, newVal) {
  const row = WEIGHTS_DRAFT[i];
  if (row.locked) return;
  newVal = Math.max(0, Math.min(1, newVal));
  // Distribute the delta proportionally over the other unlocked rows so total stays at 1.
  const lockedTotal = WEIGHTS_DRAFT.filter(r => r.locked).reduce((a, r) => a + r.weight, 0);
  const otherUnlocked = WEIGHTS_DRAFT.filter((r, j) => !r.locked && j !== i);
  const otherCurrent = otherUnlocked.reduce((a, r) => a + r.weight, 0);
  const remaining = Math.max(0, 1 - lockedTotal - newVal);
  if (otherUnlocked.length === 0) {
    row.weight = newVal;
  } else if (otherCurrent <= 0) {
    const share = remaining / otherUnlocked.length;
    for (const r of otherUnlocked) r.weight = share;
    row.weight = newVal;
  } else {
    const factor = remaining / otherCurrent;
    for (const r of otherUnlocked) r.weight = r.weight * factor;
    row.weight = newVal;
  }
  // Sync sliders without rebuilding the DOM (preserve focus).
  syncWeightsInputs();
  updateWeightsSum();
}

function syncWeightsInputs() {
  const rows = $$(".pf-w-row");
  rows.forEach((el, i) => {
    const w = WEIGHTS_DRAFT[i];
    if (!w) return;
    const s = el.querySelector(".pf-w-slider"); const n = el.querySelector(".pf-w-num");
    if (document.activeElement !== s) s.value = w.weight.toFixed(3);
    if (document.activeElement !== n) n.value = (w.weight*100).toFixed(2);
    el.classList.toggle("locked", w.locked);
  });
}

function updateWeightsSum() {
  const total = WEIGHTS_DRAFT.reduce((a, r) => a + r.weight, 0);
  const el = $("#pf-weights-sum");
  const pct = total * 100;
  el.textContent = pct.toFixed(2) + "%";
  el.className = "pf-weights-sum" + (Math.abs(pct - 100) < 0.5 ? " good" : " bad");
}

function normalizeDraft() {
  const total = WEIGHTS_DRAFT.reduce((a, r) => a + r.weight, 0);
  if (total <= 0) return;
  for (const r of WEIGHTS_DRAFT) r.weight = r.weight / total;
  syncWeightsInputs(); updateWeightsSum();
}

function resetDraftToEqual() {
  if (!WEIGHTS_DRAFT || !WEIGHTS_DRAFT.length) return;
  const w = 1 / WEIGHTS_DRAFT.length;
  WEIGHTS_DRAFT.forEach((row) => { row.weight = w; row.locked = false; });
  renderWeightsRows();
}

function resetDraftToCap() {
  // Re-derive cap weights from DATA.
  const caps = DATA.map(r => Math.max(0, Number(r.market_cap) || 0));
  const total = caps.reduce((a, b) => a + b, 0);
  const w = total > 0 ? DATA.map((r, i) => caps[i] / total) : DATA.map(() => 1 / DATA.length);
  WEIGHTS_DRAFT.forEach((row, i) => {
    row.weight = w[i] || 0;
    row.locked = false;
  });
  renderWeightsRows();
}

function _normalizedDraft() {
  const total = WEIGHTS_DRAFT.reduce((a, r) => a + r.weight, 0);
  if (total <= 0) return null;
  const out = {};
  for (const r of WEIGHTS_DRAFT) out[r.symbol] = r.weight / total;
  return out;
}

function applyWeightsDraft() {
  // Apply alone (no Save) = legacy ad-hoc "custom" path. Updates STATE.mode to
  // "custom" and stores the normalized vector. Saved presets use savePresetClick
  // / saveAsPresetClick instead, which also call selectMode("preset:<name>").
  const w = _normalizedDraft();
  if (!w) { toast("Weights must sum to a positive value."); return; }
  STATE.customWeights = w;
  STATE.mode = "custom";
  const tabMap = currentAnalyticsMap();
  for (const k of Object.keys(tabMap)) {
    if (k.startsWith("custom|")) delete tabMap[k];
  }
  closeWeightsPopup();
  renderModeBar();
  persistActivePreset();
  requestAnalytics({force: true});
}

function _invalidateModeCache(mode) {
  // Drop any cached analytics for a specific mode across all periods so the
  // next request hits the backend with the updated weights.
  const tabMap = currentAnalyticsMap();
  for (const k of Object.keys(tabMap)) {
    if (k.startsWith(mode + "|")) delete tabMap[k];
  }
}

async function savePresetClick() {
  // "Save" — if intent=edit, update in place (rename if name field changed);
  // otherwise treat as Save-as so the user is forced to name it.
  const nameInput = document.getElementById("pf-weights-name");
  const name = (nameInput && nameInput.value || "").trim();
  if (!name) { saveAsPresetClick(); return; }
  const w = _normalizedDraft();
  if (!w) return toast("Weights must sum to a positive value.");
  try {
    const renameFrom = WEIGHTS_INTENT === "edit" && WEIGHTS_EDITING_NAME && WEIGHTS_EDITING_NAME !== name
                        ? WEIGHTS_EDITING_NAME : null;
    await savePresetServer(name, w, {renameFrom, setActive: true});
    _invalidateModeCache(modeId(name));
    if (renameFrom) _invalidateModeCache(modeId(renameFrom));
    STATE.mode = modeId(name);
    STATE.customWeights = null;
    closeWeightsPopup();
    renderModeBar();
    persistActivePreset();
    requestAnalytics({force: true});
    toast(`Preset "${name}" saved.`);
  } catch (e) {
    toast("Save failed: " + (e.message || e));
  }
}

function saveAsPresetClick() {
  const existing = (STATE.weightPresets || []).map(p => p.name);
  showInlinePrompt({
    title: "Save preset as…",
    initial: "",
    placeholder: "e.g. Growth Tilt",
    validate(name) {
      if (!name) return "Name required.";
      if (existing.includes(name)) return `"${name}" already exists. Pick a different name.`;
      return null;
    },
    onOk: async (name) => {
      const w = _normalizedDraft();
      if (!w) return toast("Weights must sum to a positive value.");
      try {
        await savePresetServer(name, w, {setActive: true});
        STATE.mode = modeId(name);
        STATE.customWeights = null;
        _invalidateModeCache(modeId(name));
        closeWeightsPopup();
        renderModeBar();
        persistActivePreset();
        requestAnalytics({force: true});
        toast(`Preset "${name}" saved.`);
      } catch (e) {
        toast("Save failed: " + (e.message || e));
      }
    },
  });
}

async function deletePresetClick() {
  if (WEIGHTS_INTENT !== "edit" || !WEIGHTS_EDITING_NAME) return;
  const name = WEIGHTS_EDITING_NAME;
  try {
    await deletePresetServer(name);
    if (STATE.mode === modeId(name)) STATE.mode = "cap";
    _invalidateModeCache(modeId(name));
    closeWeightsPopup();
    renderModeBar();
    persistActivePreset();
    requestAnalytics({force: true});
    toast(`Preset "${name}" deleted.`);
  } catch (e) {
    toast("Delete failed: " + (e.message || e));
  }
}

/* Inline name prompt — used by Save-as (and by MPT "Save as preset"). Keeps
   us off the native prompt() so the UI stays consistent. */
let _ipState = null;
function showInlinePrompt(opts) {
  _ipState = opts || {};
  const host = document.getElementById("pf-name-prompt");
  const titleEl = document.getElementById("pf-name-prompt-title");
  const descEl = document.getElementById("pf-name-prompt-desc");
  const input = document.getElementById("pf-name-prompt-input");
  const err = document.getElementById("pf-name-prompt-err");
  if (!host || !input) return;
  if (titleEl) titleEl.textContent = _ipState.title || "Name";
  if (descEl) {
    const desc = _ipState.description || "";
    if (desc) { descEl.textContent = desc; descEl.style.display = ""; }
    else { descEl.textContent = ""; descEl.style.display = "none"; }
  }
  input.placeholder = _ipState.placeholder || "";
  input.value = _ipState.initial || "";
  if (err) err.textContent = "";
  host.classList.add("show");
  setTimeout(() => { input.focus(); input.select(); }, 30);
}
function hideInlinePrompt() {
  const host = document.getElementById("pf-name-prompt");
  if (host) host.classList.remove("show");
  _ipState = null;
}
function _ipSubmit() {
  if (!_ipState) return;
  const input = document.getElementById("pf-name-prompt-input");
  const err = document.getElementById("pf-name-prompt-err");
  const v = (input.value || "").trim();
  const msg = _ipState.validate ? _ipState.validate(v) : null;
  if (msg) { if (err) err.textContent = msg; return; }
  const onOk = _ipState.onOk;
  hideInlinePrompt();
  if (onOk) onOk(v);
}

function updateModeButtons() { renderModeBar(); }

function renderModeBar() {
  // Rebuilds the pill bar: Equal, Cap, each saved preset, then [+] (and [✎]
  // when a preset is currently active so the user can jump straight to edit).
  const host = document.getElementById("pf-mode-toggle");
  if (!host) return;
  const active = STATE.mode;
  const pills = [];
  function pill(mode, label, extra) {
    const cls = "pf-mode-pill" + (active === mode ? " active" : "") + (extra ? " " + extra : "");
    return `<span class="${cls}" role="tab" data-mode="${escapeHtml(mode)}" tabindex="0">${escapeHtml(label)}</span>`;
  }
  pills.push(pill("equal", "Equal"));
  pills.push(pill("cap", "Cap"));
  for (const p of (STATE.weightPresets || [])) {
    if (!p || !p.name) continue;
    pills.push(pill(modeId(p.name), p.name, "preset"));
  }
  // Trailing controls (only meaningful for saved portfolios)
  const canAdd = !!STATE.activeView && STATE.activeView !== AD_HOC_KEY && DATA.length > 0;
  if (canAdd) {
    pills.push(`<span class="pf-mode-pill icon" id="pf-mode-add" title="New preset from current weights">＋</span>`);
  }
  const activeName = activePresetName();
  if (activeName) {
    pills.push(`<span class="pf-mode-pill icon" id="pf-mode-edit" title="Edit '${escapeHtml(activeName)}'">✎</span>`);
  }
  host.innerHTML = pills.join("");
  host.querySelectorAll(".pf-mode-pill[data-mode]").forEach(el => {
    el.addEventListener("click", () => selectMode(el.dataset.mode));
    el.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectMode(el.dataset.mode); }});
  });
  const addBtn = document.getElementById("pf-mode-add");
  if (addBtn) addBtn.onclick = () => openWeightsPopup({intent: "new"});
  const editBtn = document.getElementById("pf-mode-edit");
  if (editBtn) editBtn.onclick = () => openWeightsPopup({intent: "edit", presetName: activeName});
}

function selectMode(mode) {
  if (!mode || mode === STATE.mode) return;
  STATE.mode = mode;
  // Drop ad-hoc custom weights when switching away from "custom" — preset modes
  // resolve through STATE.weightPresets, not customWeights.
  if (mode !== "custom") STATE.customWeights = null;
  renderModeBar();
  persistActivePreset();
  requestAnalytics();
}
function updatePeriodButtons() {
  $$("#pf-period-tabs button").forEach(b => b.classList.toggle("active", b.dataset.p === STATE.period));
}

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(window._toastT); window._toastT = setTimeout(() => t.classList.remove("show"), 2400);
}

/* ===========================================================================
 * Info panel
 * --------------------------------------------------------------------------- */
let _katexRendered = false;
function openInfo() {
  $("#info-bg").classList.add("show");
  if (!_katexRendered && window.renderMathInElement) {
    renderMathInElement(document.getElementById("info-bg"), {
      delimiters: [{ left: "$$", right: "$$", display: true }],
      throwOnError: false,
    });
    _katexRendered = true;
  }
}
function closeInfo() { $("#info-bg").classList.remove("show"); }
$("#info-bg").addEventListener("click", (e) => { if (e.target.id === "info-bg") closeInfo(); });

// About-MPT modal — layered on top of the MPT overlay (z-index 95 vs 90).
// Mirrors openInfo()/closeInfo() so KaTeX renders math the same way.
let _mptKatexRendered = false;
function openMptInfo() {
  const bg = document.getElementById("pf-mpt-info-bg");
  if (!bg) return;
  bg.classList.add("show");
  if (!_mptKatexRendered && window.renderMathInElement) {
    renderMathInElement(bg, {
      delimiters: [{ left: "$$", right: "$$", display: true }],
      throwOnError: false,
    });
    _mptKatexRendered = true;
  }
}
function closeMptInfo() {
  const bg = document.getElementById("pf-mpt-info-bg");
  if (bg) bg.classList.remove("show");
}

/* ===========================================================================
 * Wire up
 * --------------------------------------------------------------------------- */
// Primary button mode: "build" when no saved portfolio is loaded, "update" when one is.
// In "update" mode, #build becomes "Update Portfolio" (saves entries + refreshes), and
// #save-as becomes "Save as New Portfolio".
function primaryButtonMode() {
  const name = STATE.activeView;
  const namedLoaded = name && name !== AD_HOC_KEY;
  return namedLoaded ? "update" : "build";
}
function updatePrimaryButtonLabels() {
  const mode = primaryButtonMode();
  const buildBtn = $("#build");
  const saveAsBtn = $("#save-as");
  if (!buildBtn || !saveAsBtn) return;
  if (mode === "update") {
    buildBtn.textContent = "Update Portfolio";
    saveAsBtn.textContent = "Save as New Portfolio";
  } else {
    buildBtn.textContent = "Build Dashboard";
    saveAsBtn.textContent = "＋ Save as new";
  }
}
function runPrimary() {
  if (primaryButtonMode() === "update") {
    // saveWatchlist persists current entries and auto-rebuilds if they changed.
    // If they didn't change, fall back to a plain refresh so the button always "does something".
    const name = STATE.activeView;
    const savedView = name && VIEWS[name];
    const currentEntries = $("#tickers").value.trim();
    const savedEntries = (savedView && savedView.entries || "").trim();
    if (currentEntries && currentEntries !== savedEntries) {
      saveWatchlist();
    } else {
      build({keepPanelOpen: true});
    }
  } else {
    build({keepPanelOpen: true});
  }
}
$("#build").onclick = runPrimary;
$("#refresh").onclick = () => build({keepPanelOpen: $("#input-panel").classList.contains("hidden") ? false : true});
$("#save-as").onclick = saveAsNewWatchlist;
$("#export").onclick = exportXlsx;
// Keep labels in sync whenever the active view or the textarea changes.
$("#tickers").addEventListener("input", updatePrimaryButtonLabels);
$("#edit-btn").onclick = () => {
  const panel = $("#input-panel");
  const willOpen = panel.classList.contains("hidden");
  if (willOpen) {
    // Mutual exclusion with the News panel — opening Portfolio closes News.
    $("#news-panel").classList.add("hidden");
    $("#news-btn").classList.remove("active");
  }
  panel.classList.toggle("hidden");
  $("#edit-btn").classList.toggle("active", willOpen);
  if (willOpen) {
    // If we open the panel without an active view yet, start an ad-hoc tab.
    if (!STATE.activeView) STATE.activeView = AD_HOC_KEY;
    renderTabs(); renderEditorMeta();
    if (DATA.length && (!STATE.analytics || STATE.analytics.error)) requestAnalytics();
  }
};
$("#info-btn").onclick = openInfo;
/* Theme switch: a short click toggles light↔dark (bloomberg counts as
   non-light, so it exits to light); a long-press (≥500ms) reveals the hidden
   Bloomberg terminal theme. Pointer events cover mouse + touch in one path. */
(function setupThemeSwitch() {
  const btn = $("#theme-switch");
  if (!btn) return;
  const LONG_MS = 500;
  let timer = null, longFired = false;
  const startPress = () => {
    longFired = false;
    clearTimeout(timer);
    timer = setTimeout(() => {
      longFired = true;
      if (getTheme() !== "bloomberg") { setTheme("bloomberg"); toast("Bloomberg terminal theme"); }
    }, LONG_MS);
  };
  const cancelPress = () => { clearTimeout(timer); timer = null; };
  btn.addEventListener("pointerdown", startPress);
  btn.addEventListener("pointerup", cancelPress);
  btn.addEventListener("pointerleave", cancelPress);
  btn.addEventListener("pointercancel", cancelPress);
  btn.addEventListener("click", (e) => {
    // Swallow the click that terminates a long-press so it doesn't also toggle.
    if (longFired) { longFired = false; e.preventDefault(); e.stopPropagation(); return; }
    setTheme(getTheme() === "light" ? "dark" : "light");
  });
})();
$("#tickers").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); runPrimary(); }
});
window.addEventListener("scroll", () => {
  $("#topbar").classList.toggle("scrolled", window.scrollY > 4);
});
window.addEventListener("resize", () => {
  if (STATE.fitColumns) render();
});

/* --- News & Sentiment panel --- */
$("#news-btn").onclick = () => {
  const panel = $("#news-panel");
  const willOpen = panel.classList.contains("hidden");
  if (willOpen) {
    $("#input-panel").classList.add("hidden");
    $("#edit-btn").classList.remove("active");
  }
  panel.classList.toggle("hidden");
  $("#news-btn").classList.toggle("active", willOpen);
  if (willOpen) loadNewsSentiment();
};
$("#ns-refresh").onclick = () => refreshNewsSentiment();
$("#ns-diag-toggle").onclick = (e) => {
  // The (i) button lives inside the toggle header — let it handle its own
  // click without also collapsing/expanding the diagnostics body.
  if (e.target.closest("#ns-diag-info")) return;
  toggleNsDiagnostics();
};
$("#ns-diag-info").onclick = (e) => { e.stopPropagation(); toggleNsDiagAbout(); };

/* Panel-local state: sentiment dicts are the enriched backend shape
 * (s_idio / s_sys / s_total / confidence / events / disagreement + legacy
 * tier/score/summary). Articles come from the cache-only tape route and
 * carry per-article score/event/relevance after an array-scored refresh. */
const NS = {
  market: null,
  sentiment: {},
  articles: [],
  status: null,
  filterSym: "",
  filterEvent: "",
  tapeTiers: new Set(),  // sentiment-tier filter for the flash tape (empty = all)
  sortKey: "total",   // constituent-table sort column
  sortDir: -1,        // 1 asc, -1 desc
  expanded: null,     // symbol whose brief row is expanded in the table
  diagLoaded: false,
  // Analysis window (item 3): how far back the news fetch/scoring reaches.
  // Applied on the next Refresh; persisted per user.
  lookbackDays: (() => {
    const v = parseInt(localStorage.getItem("ns_lookback") || "7", 10);
    return [3, 7, 14, 30].includes(v) ? v : 7;
  })(),
  tlSym: "",         // timeline ticker filter ("" = all)
  tlPeriod: "1W",    // timeline window
};

/* Lookback control wiring — active pill + persistence. The new window only
 * takes effect on Refresh (cache-bust path), which the tooltip explains. */
(() => {
  const box = $("#ns-lookback");
  if (!box) return;
  const sync = () => box.querySelectorAll("button[data-days]").forEach(b =>
    b.classList.toggle("active", parseInt(b.dataset.days, 10) === NS.lookbackDays));
  box.querySelectorAll("button[data-days]").forEach(b => {
    b.onclick = () => {
      NS.lookbackDays = parseInt(b.dataset.days, 10);
      localStorage.setItem("ns_lookback", String(NS.lookbackDays));
      sync();
      renderNsTimeline();
    };
  });
  sync();
})();

function nsSymbols() { return DATA.map(r => r.symbol).filter(Boolean); }

async function loadNewsSentiment() {
  const symbols = nsSymbols();
  $("#ns-market-body").innerHTML = '<div class="ns-loading">Loading market sentiment...</div>';
  $("#ns-portfolio-body").innerHTML = '<div class="ns-loading">Loading constituent sentiment...</div>';

  const mktP = fetch("/api/news-market").then(r => r.json());
  const pfP = symbols.length
    ? fetch(`/api/news-sentiment?symbols=${encodeURIComponent(symbols.join(","))}`).then(r => r.json())
    : Promise.resolve(null);
  const tapeP = symbols.length
    ? fetch(`/api/news-tape?symbols=${encodeURIComponent(symbols.join(","))}`).then(r => r.json())
    : Promise.resolve(null);
  const [mktRes, pfRes, tapeRes] = await Promise.allSettled([mktP, pfP, tapeP]);

  if (mktRes.status === "fulfilled") {
    NS.market = mktRes.value.sentiment || null;
    NS.status = mktRes.value.status || NS.status;
  }
  if (pfRes.status === "fulfilled" && pfRes.value) {
    NS.sentiment = pfRes.value.sentiment || {};
    NS.status = pfRes.value.status || NS.status;
  }
  if (tapeRes.status === "fulfilled" && tapeRes.value) {
    NS.articles = tapeRes.value.articles || [];
  }
  renderNewsPanel(symbols);
  if (Object.keys(NS.sentiment).length) patchRowSentiment(NS.sentiment);
}

function nsRefreshContext(symbols) {
  /* Quant context shipped to the backend: betas + row numbers feed the
   * decomposition and the Bloomberg briefs; weights drive staged order
   * and let the server know the active weighting. */
  const weights = weightsForMode(STATE.mode) || {};
  const betas = {}, rows = {};
  for (const r of DATA) {
    if (!r.symbol) continue;
    if (r.beta != null) betas[r.symbol] = r.beta;
    rows[r.symbol] = {
      name: r.name ?? null,
      price: r.price ?? null,
      pct_1d: r.pct_1d ?? null,
      pct_1w: r.pct_1w ?? null,
      pct_ytd: r.pct_ytd ?? null,
      delta_ath: r.delta_ath ?? null,
    };
  }
  return {weights, betas, rows, lookback_days: NS.lookbackDays};
}

/* ---- News refresh progress modal ---------------------------------------
 * One job row per constituent + the market. Each bar fills through the
 * backend stage events (start → fetch → score1 → score2 → aggregate → done);
 * two rows animate at once because the refresh runs a 2-worker pool. */
const NS_PROG_STAGE = {
  queued:    { frac: 0.00, txt: "queued" },
  start:     { frac: 0.05, txt: "starting…" },
  fetch:     { frac: 0.35, txt: "fetched news…" },
  score1:    { frac: 0.60, txt: "AI pass 1…" },
  score2:    { frac: 0.85, txt: "AI pass 2…" },
  aggregate: { frac: 0.95, txt: "aggregating…" },
};

function updateNsProgCount() {
  const c = $("#ns-prog-count");
  if (c && NS.prog) c.textContent = `${NS.prog.done}/${NS.prog.total}`;
}

function openNsProgress(plan) {
  const bg = $("#ns-prog-bg");
  const list = $("#ns-prog-jobs");
  if (!bg || !list) return;
  // Cancel any pending auto-close from a previous refresh — otherwise its
  // 900ms timer could fire and hide the modal this new refresh just opened
  // (the Refresh button re-enables before that timer elapses).
  clearTimeout(NS.progCloseTimer);
  NS.prog = { jobs: new Map(), total: 0, done: 0, error: false };
  list.innerHTML = "";
  const add = (id, label) => {
    const row = document.createElement("div");
    row.className = "ns-prog-job";
    row.innerHTML =
      `<span class="ns-prog-sym"></span>` +
      `<span class="ns-prog-track"><span class="ns-prog-fill"></span></span>` +
      `<span class="ns-prog-status">queued</span>`;
    row.querySelector(".ns-prog-sym").textContent = label;
    list.appendChild(row);
    NS.prog.jobs.set(id, {
      label, frac: 0, done: false, el: row,
      fill: row.querySelector(".ns-prog-fill"),
      status: row.querySelector(".ns-prog-status"),
    });
  };
  if (plan.market) add("__market__", "Market");
  (plan.symbols || []).forEach(s => add(s, s));
  NS.prog.total = NS.prog.jobs.size;
  updateNsProgCount();
  const foot = $("#ns-prog-foot");
  if (foot) {
    // Clear a previous run's error styling — otherwise a failed refresh
    // permanently reddens every subsequent refresh's footer.
    foot.classList.remove("ns-prog-err");
    foot.textContent = `Window ${plan.days || NS.lookbackDays}d · 2 workers in parallel`;
  }
  bg.hidden = false;
  bg.classList.add("show");
  if (!bg.dataset.wired) {  // backdrop-click close, attached once
    bg.addEventListener("click", e => { if (e.target === bg) closeNsProgress(); });
    bg.dataset.wired = "1";
  }
}

function updateNsProgressJob(id, patch) {
  if (!NS.prog) return;
  const job = NS.prog.jobs.get(id);
  if (!job) return;
  if (patch.stage) {
    const st = NS_PROG_STAGE[patch.stage];
    if (st) {
      job.frac = Math.max(job.frac, patch.frac != null ? patch.frac : st.frac);
      job.status.textContent = st.txt;
    }
    if (!job.done) job.el.classList.add("active");
  }
  if (patch.done && !job.done) {
    job.done = true;
    job.frac = 1;
    job.el.classList.remove("active");
    job.el.classList.add("done");
    const s = patch.sentiment;
    if (s && (s.tier || s.score != null)) {
      const tier = s.tier || "neutral";
      job.fill.style.background = NS_COLORS[tier] || "var(--accent)";
      const lbl = NS_LABELS[tier] || "done";
      job.status.textContent = s.score != null ? `${lbl} ${fmtSig(s.score)}` : lbl;
      job.status.style.color = NS_COLORS[tier] || "";
    } else {
      job.el.classList.add("empty");
      job.status.textContent = "no news";
    }
    NS.prog.done += 1;
    updateNsProgCount();
  }
  job.fill.style.width = (job.frac * 100).toFixed(1) + "%";
}

function nsProgressError(message) {
  if (!NS.prog) return;
  NS.prog.error = true;
  const foot = $("#ns-prog-foot");
  if (foot) {
    foot.textContent = `Refresh failed: ${message}`;
    foot.classList.add("ns-prog-err");
  }
}

function closeNsProgress() {
  const bg = $("#ns-prog-bg");
  if (!bg) return;
  bg.classList.remove("show");
  bg.hidden = true;
}

async function refreshNewsSentiment() {
  const btn = $("#ns-refresh");
  btn.disabled = true;
  const symbols = nsSymbols();
  let done = 0;
  btn.textContent = "Refreshing…";

  try {
    const resp = await fetch("/api/news-refresh", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({symbols, context: nsRefreshContext(symbols)}),
    });
    if (!resp.ok || !resp.body) {
      const j = await resp.json().catch(() => ({}));
      throw new Error(j.error || ("HTTP " + resp.status));
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done: rDone, value } = await reader.read();
      if (rDone) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        let msg;
        try { msg = JSON.parse(line); } catch { continue; }
        if (msg.type === "plan") {
          openNsProgress(msg);
        } else if (msg.type === "market_stage") {
          updateNsProgressJob("__market__", { stage: msg.stage, frac: msg.frac });
        } else if (msg.type === "symbol_stage") {
          updateNsProgressJob(msg.symbol, { stage: msg.stage, frac: msg.frac });
        } else if (msg.type === "market") {
          NS.market = msg.sentiment || null;
          renderMarketSentiment();
          updateNsProgressJob("__market__", { done: true, sentiment: msg.sentiment });
          btn.textContent = `Refreshing… market ✓ 0/${symbols.length}`;
        } else if (msg.type === "symbol") {
          done += 1;
          if (msg.sentiment) NS.sentiment[msg.symbol] = msg.sentiment;
          renderNsGauge(symbols);
          renderPortfolioSentiment(symbols);
          updateNsProgressJob(msg.symbol, { done: true, sentiment: msg.sentiment });
          btn.textContent = `Refreshing… ${done}/${symbols.length}`;
        } else if (msg.type === "done") {
          NS.market = msg.market || NS.market;
          NS.sentiment = msg.portfolio || NS.sentiment;
          NS.status = msg.status || NS.status;
        } else if (msg.type === "error") {
          throw new Error(msg.error || "refresh failed");
        }
      }
    }
    // Tape articles now carry fresh per-article scores — reload them.
    try {
      const tape = await fetch(`/api/news-tape?symbols=${encodeURIComponent(symbols.join(","))}`).then(r => r.json());
      NS.articles = tape.articles || [];
    } catch (e) { /* tape is decorative — keep the stale one */ }
    renderNewsPanel(symbols);
    patchRowSentiment(NS.sentiment);
    // Brief hold so the last bar's fill is visible, then dismiss. Tracked so a
    // subsequent refresh can cancel it (see openNsProgress).
    NS.progCloseTimer = setTimeout(closeNsProgress, 900);
  } catch (e) {
    const detail = escapeHtml(String(e.message || e));
    $("#ns-market-body").innerHTML = `<div class="ns-panel-empty">Refresh failed. ${detail}</div>`;
    // Keep the modal open showing the error (closeable via backdrop/Esc)
    // rather than silently vanishing on failure.
    nsProgressError(detail);
  }
  btn.disabled = false;
  btn.textContent = "↻ Refresh";
}

function renderNewsPanel(symbols) {
  symbols = symbols || nsSymbols();
  renderNsGauge(symbols);
  renderMarketSentiment();
  renderNsMovers();
  renderNsWatch(symbols);
  renderNsTimeline();
  renderNsTape();
  renderPortfolioSentiment(symbols);
  setNewsUpdatedLabel(NS.market, NS.sentiment);
}

function latestNewsAssessment(marketSentiment, portfolioSentiment) {
  const stamps = [];
  if (marketSentiment && marketSentiment.assessed_at) stamps.push(marketSentiment.assessed_at);
  if (portfolioSentiment) {
    for (const value of Object.values(portfolioSentiment)) {
      if (value && value.assessed_at) stamps.push(value.assessed_at);
    }
  }
  if (!stamps.length) return null;
  stamps.sort();
  return stamps[stamps.length - 1];
}

function setNewsUpdatedLabel(marketSentiment, portfolioSentiment) {
  const updated = $("#ns-updated");
  if (!updated) return;
  const stamp = latestNewsAssessment(marketSentiment, portfolioSentiment);
  if (!stamp) {
    updated.textContent = "";
    return;
  }
  const dt = new Date(stamp);
  const time = dt.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  updated.textContent = `As of ${fmtDateMDY(dt)}, ${time}`;
}

function nsKeyDiagnostic(status) {
  if (!status) return "Check API keys.";
  if (!status.finnhub_key) return "Finnhub key missing — add .finnhub_key beside dashboard.py.";
  if (!status.nvidia_key) return "NVIDIA key missing — add .nvidia_key beside dashboard.py.";
  if (status.finnhub_backoff_s > 0) {
    const used = status.finnhub_calls_used ?? "?";
    const limit = status.finnhub_calls_limit ?? "?";
    return `Finnhub rate-limited — ${used}/${limit} calls used, retry in ~${status.finnhub_backoff_s}s.`;
  }
  if (status.nvidia_backoff_s > 0) {
    const used = status.nvidia_calls_used ?? "?";
    const limit = status.nvidia_calls_limit ?? "?";
    return `NVIDIA rate-limited — ${used}/${limit} calls used, retry in ~${status.nvidia_backoff_s}s.`;
  }
  return "Rate-limited or temporarily unavailable — try again in ~60s.";
}

function fmtSig(v, digits = 2) {
  if (v == null || !isFinite(v)) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}`;
}

function nsScoreDot(score) {
  /* Per-article / per-signal dot colored by sign+magnitude. */
  if (score == null || !isFinite(score)) return '<span class="ns-dot ns-empty"></span>';
  let color = "#94a3b8";
  if (score > 0.15) color = score > 0.5 ? "#16a34a" : "#86efac";
  else if (score < -0.15) color = score < -0.5 ? "#dc2626" : "#fca5a5";
  return `<span class="ns-dot" style="background:${color}" data-tip="${fmtSig(score)}"></span>`;
}

function nsTierFromScore(s) {
  /* Client-side label for aggregate numbers (portfolio gauge). Fixed
   * thresholds — per-stock tiers come calibrated from the server. */
  if (s <= -0.5) return "very_bearish";
  if (s <= -0.15) return "bearish";
  if (s < 0.15) return "neutral";
  if (s < 0.5) return "bullish";
  return "very_bullish";
}

const NS_EVENT_LABELS = {
  earnings: "Earnings", guidance: "Guidance", ma: "M&A", analyst: "Analyst",
  legal_regulatory: "Legal/Reg", product: "Product", insider: "Insider",
  macro: "Macro", other: "Other",
};

function renderNsGauge(symbols) {
  const body = $("#ns-gauge-body");
  const weights = weightsForMode(STATE.mode) || {};
  let W = 0, sTot = 0, sSys = 0, sIdio = 0, nAssessed = 0, nBear = 0, nFlag = 0;
  for (const sym of symbols) {
    const s = NS.sentiment[sym];
    if (!s || s.s_total == null) continue;
    const wi = weights[sym] != null ? weights[sym] : 1 / Math.max(1, symbols.length);
    W += wi;
    sTot += wi * s.s_total;
    sSys += wi * (s.s_sys || 0);
    sIdio += wi * (s.s_idio || 0);
    nAssessed++;
    if (s.tier === "bearish" || s.tier === "very_bearish") nBear++;
    if (s.disagreement) nFlag++;
  }
  if (!W || !nAssessed) {
    body.innerHTML = `<div class="ns-panel-empty">No assessed holdings yet. ${escapeHtml(nsKeyDiagnostic(NS.status))}</div>`;
    return;
  }
  sTot /= W; sSys /= W; sIdio /= W;
  const tier = nsTierFromScore(sTot);
  const tierLabel = NS_LABELS[tier] || "Neutral";
  const barSeg = (v, cls) => {
    const pct = Math.min(100, Math.abs(v) * 100);
    return `<div class="ns-decomp-row">
      <span class="ns-decomp-label">${cls === "sys" ? "Systematic (β·mkt)" : "Stock-specific"}</span>
      <span class="ns-decomp-track"><span class="ns-decomp-fill ${cls} ${v >= 0 ? "pos" : "neg"}" style="width:${pct}%"></span></span>
      <span class="ns-decomp-val">${fmtSig(v)}</span>
    </div>`;
  };
  const flagNote = nFlag ? ` · ${nFlag} flagged ⚑` : "";
  // Analysis-window transparency (item 3): surface the lookback + recency tau
  // the assessed signals were actually computed with.
  const anyS = symbols.map(s => NS.sentiment[s]).find(s => s && s.lookback_days);
  const windowNote = anyS
    ? ` · window ${anyS.lookback_days}d (recency τ ${anyS.recency_tau_days ?? "3"}d)` : "";
  body.innerHTML = `
    <div class="ns-gauge-top">
      <span class="ns-gauge-num">${fmtSig(sTot)}</span>
      <span class="ns-tier-badge ${tier}">${escapeHtml(tierLabel)}</span>
    </div>
    ${barSeg(sSys, "sys")}
    ${barSeg(sIdio, "idio")}
    <div class="ns-gauge-foot">${nAssessed} of ${symbols.length} holdings assessed · ${nBear} bearish${flagNote}${windowNote}</div>
  `;
}

function renderMarketSentiment() {
  const body = $("#ns-market-body");
  const s = NS.market;
  if (!s) {
    body.innerHTML = `<div class="ns-panel-empty">No market sentiment available. ${escapeHtml(nsKeyDiagnostic(NS.status))}</div>`;
    return;
  }
  const tierLabel = NS_LABELS[s.tier] || "Neutral";
  const tierCls = NS_COLORS[s.tier] ? s.tier : "neutral";
  // Structured cross-asset tape: label muted, return magnitude-colored. Falls
  // back to the legacy pre-formatted string when tape_items is absent.
  let tape = "";
  if (Array.isArray(s.tape_items) && s.tape_items.length) {
    const items = s.tape_items.map(it =>
      `<span class="ns-mkt-item"><span class="ns-mkt-lbl">${escapeHtml(it.label)}</span> ` +
      `<span class="ns-mkt-val" style="color:${returnColor(it.pct, 2.5)}">${fmtPctSigned(it.pct)} 1d</span></span>`
    ).join('<span class="ns-mkt-sep">·</span>');
    tape = `<div class="ns-mkt-tape">${items}</div>`;
  } else if (s.tape) {
    tape = `<div class="ns-mkt-tape">${escapeHtml(s.tape)}</div>`;
  }
  const conf = s.confidence != null ? ` · conf ${(s.confidence * 100).toFixed(0)}%` : "";
  body.innerHTML = `
    <div class="ns-market-summary">${escapeHtml(s.summary)}</div>
    ${tape}
    <span class="ns-tier-badge ${tierCls}">${escapeHtml(tierLabel)}</span>
    <span class="ns-score">${fmtSig(s.score)} | ${s.article_count || 0} articles${conf}</span>
  `;
}

function nsBestArticle(sym) {
  /* Attribution pick for Movers: the highest-|score| high/med-relevance
   * article, else the most recent one. */
  const arts = NS.articles.filter(a => a.symbol === sym);
  if (!arts.length) return null;
  const scored = arts.filter(a => a.score != null && a.relevance !== "low");
  if (scored.length) {
    scored.sort((a, b) => Math.abs(b.score) - Math.abs(a.score));
    return scored[0];
  }
  return arts[0];
}

function renderNsMovers() {
  const body = $("#ns-movers-body");
  const movers = DATA.filter(r => r.symbol && r.pct_1d != null)
    .sort((a, b) => Math.abs(b.pct_1d) - Math.abs(a.pct_1d))
    .slice(0, 5);
  if (!movers.length) {
    body.innerHTML = '<div class="ns-panel-empty">Build the dashboard to see movers.</div>';
    return;
  }
  const rows = movers.map(r => {
    const art = nsBestArticle(r.symbol);
    const head = art
      ? `<a href="${escapeHtml(art.url || "#")}" target="_blank" rel="noopener">${escapeHtml(art.headline || "")}</a>` +
        (art.source ? ` <span class="ns-src">${escapeHtml(art.source)}</span>` : "")
      : '<span class="ns-src">no recent coverage</span>';
    return `<div class="ns-mover-row">
      <span class="ns-tape-sym">${r.symbol}</span>
      <span class="ns-mover-pct" style="color:${returnColor(r.pct_1d, 6)}">${fmtPctSigned(r.pct_1d)}</span>
      <span class="ns-mover-head">${head}</span>
    </div>`;
  });
  body.innerHTML = rows.join("");
}

function renderNsWatch(symbols) {
  const body = $("#ns-watch-body");
  const items = [];
  for (const sym of symbols) {
    const s = NS.sentiment[sym];
    if (!s) continue;
    if (s.disagreement) {
      items.push(`<div class="ns-watch-row">⚑ <b>${sym}</b> — AI and dictionary sentiment disagree; treat the signal with caution.</div>`);
    }
    if (s.tier === "very_bearish" || s.tier === "very_bullish") {
      const lbl = NS_LABELS[s.tier] || s.tier;
      const color = NS_COLORS[s.tier] || "var(--muted)";
      items.push(`<div class="ns-watch-row"><span style="color:${color};font-weight:600">●</span> <b>${sym}</b> — ${lbl}: ${escapeHtml(s.summary || "")}</div>`);
    }
  }
  const dataBySymbol = new Map(DATA.map(d => [d.symbol, d]));
  for (const sym of symbols) {
    const ne = (dataBySymbol.get(sym) || {}).next_earnings;
    if (ne) items.push(`<div class="ns-watch-row">📅 <b>${sym}</b> — earnings ${escapeHtml(fmtDateMDY(ne))}</div>`);
  }
  body.innerHTML = items.length ? items.slice(0, 10).join("")
    : '<div class="ns-panel-empty">Nothing flagged — no strong signals, disagreements, or imminent catalysts.</div>';
}

function renderNsTape() {
  const filtBody = $("#ns-tape-filters");
  const body = $("#ns-tape-body");
  if (!NS.articles.length) {
    filtBody.innerHTML = "";
    body.innerHTML = '<div class="ns-panel-empty">No cached articles yet — refresh to fill the tape.</div>';
    return;
  }
  const syms = [...new Set(NS.articles.map(a => a.symbol))].sort();
  const events = [...new Set(NS.articles.map(a => a.event).filter(Boolean))];
  const symOpts = ['<option value="">All tickers</option>']
    .concat(syms.map(s => `<option value="${s}" ${NS.filterSym === s ? "selected" : ""}>${s}</option>`)).join("");
  // Sentiment-tier filter (multi-select). Empty set = All. Any combination of
  // the five tiers can be active at once (item 5): click "All" to clear, or
  // toggle individual tiers to build e.g. {very_bearish, bearish}.
  const tierChipDefs = [
    ["very_bullish", "Very Bullish"], ["bullish", "Bullish"], ["neutral", "Neutral"],
    ["bearish", "Bearish"], ["very_bearish", "Very Bearish"],
  ];
  const allOn = NS.tapeTiers.size === 0;
  const tierChips =
    `<button class="ns-tier-chip ${allOn ? "on" : ""}" data-tier="__all__">All</button>` +
    tierChipDefs.map(([t, lbl]) =>
      `<button class="ns-tier-chip t-${t} ${NS.tapeTiers.has(t) ? "on" : ""}" data-tier="${t}">${lbl}</button>`).join("");
  const evChips = events.map(ev =>
    `<button class="ns-ev-chip ${NS.filterEvent === ev ? "on" : ""}" data-ev="${ev}">${NS_EVENT_LABELS[ev] || ev}</button>`).join("");
  filtBody.innerHTML =
    `<select id="ns-tape-sym-filter" class="ns-tape-select">${symOpts}</select>` +
    `<span class="ns-tape-sep"></span><span class="ns-tape-grp">${tierChips}</span>` +
    (evChips ? `<span class="ns-tape-sep"></span><span class="ns-tape-grp">${evChips}</span>` : "");
  const sel = $("#ns-tape-sym-filter");
  if (sel) sel.onchange = () => { NS.filterSym = sel.value; renderNsTape(); };
  filtBody.querySelectorAll(".ns-tier-chip").forEach(btn => {
    btn.onclick = () => {
      const t = btn.dataset.tier;
      if (t === "__all__") { NS.tapeTiers.clear(); }
      else if (NS.tapeTiers.has(t)) { NS.tapeTiers.delete(t); }
      else { NS.tapeTiers.add(t); }
      renderNsTape();
    };
  });
  filtBody.querySelectorAll(".ns-ev-chip").forEach(btn => {
    btn.onclick = () => {
      NS.filterEvent = NS.filterEvent === btn.dataset.ev ? "" : btn.dataset.ev;
      renderNsTape();
    };
  });

  let arts = NS.articles;
  if (NS.filterSym) arts = arts.filter(a => a.symbol === NS.filterSym);
  if (NS.filterEvent) arts = arts.filter(a => a.event === NS.filterEvent);
  if (NS.tapeTiers.size) arts = arts.filter(a =>
    a.score != null && isFinite(a.score) && NS.tapeTiers.has(nsTierFromScore(a.score)));
  arts = arts.slice(0, 120);
  if (!arts.length) {
    body.innerHTML = '<div class="ns-panel-empty">No articles match the filter.</div>';
    return;
  }
  const rows = arts.map(a => {
    const dt = a.datetime ? new Date(a.datetime * 1000) : null;
    const stamp = dt ? `${fmtDateMD(dt)} ${String(dt.getHours()).padStart(2, "0")}:${String(dt.getMinutes()).padStart(2, "0")}` : "—";
    const ev = a.event ? `<span class="ns-ev-tag">${NS_EVENT_LABELS[a.event] || a.event}</span>` : "";
    const dup = a.n_duplicates ? `<span class="ns-dup" data-tip="${a.n_duplicates} syndicated copies collapsed">×${a.n_duplicates + 1}</span>` : "";
    return `<div class="ns-tape-row">
      <span class="ns-tape-time">${stamp}</span>
      <span class="ns-tape-sym">${escapeHtml(a.symbol || "")}</span>
      ${nsScoreDot(a.score)}
      <span class="ns-tape-head"><a href="${escapeHtml(a.url || "#")}" target="_blank" rel="noopener">${escapeHtml(a.headline || "")}</a></span>
      ${ev}${dup}
      <span class="ns-src">${escapeHtml(a.source || "")}</span>
    </div>`;
  });
  body.innerHTML = rows.join("");
}

/* --- News Timeline (item 4) ---------------------------------------------
 * x = time, one bar per cached article. Bar height encodes signal strength
 * (strong = very bullish/bearish, medium = bullish/bearish, short gray =
 * neutral/unscored); color encodes direction. Period tabs mirror the price
 * charts; the ticker select mirrors the Flash Tape filter. Pure HTML/CSS
 * bars (no canvas) so the shared [data-tip] tooltip system works per bar. */
const NS_TL_PERIODS = [["1D", 1], ["3D", 3], ["1W", 7], ["2W", 14], ["1M", 30]];

function nsTlLevel(score) {
  /* 3-level magnitude ramp. "very" tiers use deep saturated green/red
     (#16a34a / #dc2626); plain tiers use pale (#86efac / #fca5a5) so the two
     are unmistakable on the timeline (was #22c55e vs #4ade80 — too close). */
  if (score == null || !isFinite(score)) return { h: 22, color: "#94a3b8", lbl: "neutral" };
  const a = Math.abs(score);
  if (a > 0.5) return { h: 92, color: score > 0 ? "#16a34a" : "#dc2626", lbl: score > 0 ? "very bullish" : "very bearish" };
  if (a > 0.15) return { h: 58, color: score > 0 ? "#86efac" : "#fca5a5", lbl: score > 0 ? "bullish" : "bearish" };
  return { h: 22, color: "#94a3b8", lbl: "neutral" };
}

function renderNsTimeline() {
  const ctl = $("#ns-tl-controls");
  const body = $("#ns-tl-body");
  if (!ctl || !body) return;
  if (!NS.articles.length) {
    ctl.innerHTML = "";
    body.innerHTML = '<div class="ns-panel-empty">No cached articles yet — refresh to fill the timeline.</div>';
    return;
  }
  // Controls: ticker select (like the tape) + period tabs (like price charts).
  const syms = [...new Set(NS.articles.map(a => a.symbol))].sort();
  const symOpts = ['<option value="">All tickers</option>']
    .concat(syms.map(s => `<option value="${s}" ${NS.tlSym === s ? "selected" : ""}>${s}</option>`)).join("");
  const periodBtns = NS_TL_PERIODS.map(([p]) =>
    `<button class="ns-tl-p ${NS.tlPeriod === p ? "active" : ""}" data-p="${p}" type="button">${p}</button>`).join("");
  ctl.innerHTML =
    `<select id="ns-tl-sym" class="ns-tape-select">${symOpts}</select>` +
    `<span class="ns-tl-periods">${periodBtns}</span>` +
    `<span class="ns-tl-legend">` +
    `<span class="ns-tl-lg"><i style="background:#16a34a;height:10px"></i>strong</span>` +
    `<span class="ns-tl-lg"><i style="background:#86efac;height:7px"></i>moderate</span>` +
    `<span class="ns-tl-lg"><i style="background:#94a3b8;height:4px"></i>neutral</span>` +
    `<span class="ns-tl-lg">green bullish · red bearish</span></span>`;
  $("#ns-tl-sym").onchange = (e) => { NS.tlSym = e.target.value; renderNsTimeline(); };
  ctl.querySelectorAll(".ns-tl-p").forEach(b => {
    b.onclick = () => { NS.tlPeriod = b.dataset.p; renderNsTimeline(); };
  });

  const days = (NS_TL_PERIODS.find(([p]) => p === NS.tlPeriod) || ["1W", 7])[1];
  const now = Date.now();
  const t0 = now - days * 86400e3;
  let arts = NS.articles.filter(a => a.datetime && a.datetime * 1000 >= t0);
  if (NS.tlSym) arts = arts.filter(a => a.symbol === NS.tlSym);
  if (!arts.length) {
    body.innerHTML = '<div class="ns-panel-empty">No articles in this window — widen the period or the analysis window, then refresh.</div>';
    return;
  }
  // Bars, oldest→newest so later (newer) bars paint on top when overlapping.
  arts = arts.slice().sort((a, b) => a.datetime - b.datetime);
  const bars = arts.map(a => {
    const x = ((a.datetime * 1000 - t0) / (now - t0)) * 100;
    const lv = nsTlLevel(a.score);
    const dt = new Date(a.datetime * 1000);
    const stamp = `${fmtDateMD(dt)} ${String(dt.getHours()).padStart(2, "0")}:${String(dt.getMinutes()).padStart(2, "0")}`;
    const sc = (a.score != null && isFinite(a.score)) ? ` ${fmtSig(a.score)}` : "";
    const head = (a.headline || "").slice(0, 110);
    const tip = `${stamp} · ${a.symbol || ""}${sc} (${lv.lbl}) — ${head}`;
    return `<a class="ns-tl-bar" href="${escapeHtml(a.url || "#")}" target="_blank" rel="noopener"
      style="left:${x.toFixed(3)}%;height:${lv.h}%;background:${lv.color}"
      data-tip="${escapeHtml(tip)}"></a>`;
  }).join("");
  // Time axis: ~5 evenly spaced ticks with subtle grid lines.
  const nTicks = 5;
  let ticks = "";
  for (let i = 0; i <= nTicks; i++) {
    const frac = i / nTicks;
    const t = t0 + frac * (now - t0);
    const d = new Date(t);
    const lbl = days <= 3
      ? `${fmtDateMD(d)} ${String(d.getHours()).padStart(2, "0")}:00`
      : fmtDateMD(d);
    ticks += `<span class="ns-tl-tick" style="left:${(frac * 100).toFixed(2)}%"><i></i>${lbl}</span>`;
  }
  body.innerHTML = `<div class="ns-tl-plot">${ticks}${bars}<div class="ns-tl-baseline"></div></div>`;
}

/* --- Resizable news quad (item 1) ----------------------------------------
 * The vertical gutter re-splits the two columns (fr units — intrinsically
 * zero-sum), the horizontal gutter re-splits the two rows (pixel heights,
 * frozen from the live layout on first drag so total height is conserved).
 * Cards reflow/auto-scale live during the drag because only grid tracks
 * change. Double-click a gutter to reset; splits persist per user. */
(() => {
  const quad = $("#ns-quad");
  const gv = $("#ns-qgut-v"), gh = $("#ns-qgut-h");
  if (!quad || !gv || !gh) return;
  const KEY = "ns_quad_split";
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch { saved = {}; }
  const apply = (s) => {
    if (s.c1 != null) {
      quad.style.setProperty("--nsq-c1", `${s.c1}fr`);
      quad.style.setProperty("--nsq-c2", `${1 - s.c1}fr`);
    }
    if (s.r1 != null && s.r2 != null) {
      quad.style.setProperty("--nsq-r1", `${s.r1}px`);
      quad.style.setProperty("--nsq-r2", `${s.r2}px`);
      quad.classList.add("rows-fixed");
    }
  };
  apply(saved);
  const persist = () => localStorage.setItem(KEY, JSON.stringify(saved));

  gv.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    try { gv.setPointerCapture(e.pointerId); } catch { /* capture is best-effort */ }
    const rect = quad.getBoundingClientRect();
    const gut = gv.getBoundingClientRect().width;
    const move = (ev) => {
      const frac = Math.min(0.8, Math.max(0.2, (ev.clientX - rect.left) / (rect.width - gut)));
      saved.c1 = Math.round(frac * 1000) / 1000;
      apply(saved);
    };
    const up = () => {
      gv.removeEventListener("pointermove", move);
      gv.removeEventListener("pointerup", up);
      persist();
    };
    gv.addEventListener("pointermove", move);
    gv.addEventListener("pointerup", up);
  });

  gh.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    try { gh.setPointerCapture(e.pointerId); } catch { /* capture is best-effort */ }
    // Freeze the current auto row heights so the drag is zero-sum in px.
    const h1 = $("#ns-gauge-card").getBoundingClientRect().height;
    const h2 = $("#ns-movers-card").getBoundingClientRect().height;
    const total = h1 + h2;
    const y0 = e.clientY;
    const move = (ev) => {
      if (total < 130) return; // too small to split meaningfully
      const dy = ev.clientY - y0;
      const min = Math.min(60, total / 2); // keep clamp bounds ordered even for short cards
      const r1 = Math.min(total - min, Math.max(min, h1 + dy));
      saved.r1 = Math.round(r1);
      saved.r2 = Math.round(total - r1);
      apply(saved);
    };
    const up = () => {
      gh.removeEventListener("pointermove", move);
      gh.removeEventListener("pointerup", up);
      persist();
    };
    gh.addEventListener("pointermove", move);
    gh.addEventListener("pointerup", up);
  });

  const reset = (which) => {
    if (which === "v") { delete saved.c1; quad.style.removeProperty("--nsq-c1"); quad.style.removeProperty("--nsq-c2"); }
    else {
      delete saved.r1; delete saved.r2;
      quad.style.removeProperty("--nsq-r1"); quad.style.removeProperty("--nsq-r2");
      quad.classList.remove("rows-fixed");
    }
    persist();
  };
  gv.addEventListener("dblclick", () => reset("v"));
  gh.addEventListener("dblclick", () => reset("h"));
})();

function renderPortfolioSentiment(symbols, _status) {
  const pBody = $("#ns-portfolio-body");
  symbols = symbols || nsSymbols();
  if (!symbols.length) {
    pBody.innerHTML = '<div class="ns-panel-empty">Build the dashboard first to see per-stock sentiment.</div>';
    return;
  }
  const dataBySymbol = new Map(DATA.map(d => [d.symbol, d]));
  // Sortable column registry. `sv(sym)` returns the sort value (numbers for
  // numeric columns, lowercased strings for text). Events is intentionally
  // absent — sorting a set of event tags is meaningless (item 3).
  const nsCols = [
    { key: "ticker",  label: "Ticker",  cls: "",  align: "l", sv: (r) => (r.symbol || "").toLowerCase() },
    { key: "company", label: "Company", cls: "",  align: "l", sv: (r) => (r.name || "").toLowerCase() },
    { key: "price",   label: "Price",   cls: "r", align: "r", sv: (r) => r.price },
    { key: "pct_1d",  label: "% 1D",    cls: "r", align: "r", sv: (r) => r.pct_1d },
    { key: "beta",    label: "β",       cls: "r", align: "r", sv: (r) => r.beta },
    { key: "idio",    label: "Idio",    cls: "r", align: "r", tip: "Idiosyncratic news score — company-specific signal only", sv: (r, s) => s ? s.s_idio : null },
    { key: "sys",     label: "Sys",     cls: "r", align: "r", tip: "Systematic tilt: κ·β·market sentiment", sv: (r, s) => s ? s.s_sys : null },
    { key: "total",   label: "Total",   cls: "c", align: "c", tip: "Total signal = clip(κ·β·mkt + idio), tier calibrated vs trailing distribution", sv: (r, s) => s ? s.s_total : null },
    { key: "conf",    label: "Conf",    cls: "c", align: "c", tip: "Confidence: evidence mass, article agreement, self-consistency", sv: (r, s) => s ? s.confidence : null },
    { key: "events",  label: "Events",  cls: "",  align: "l", sortable: false },
    { key: "flag",    label: "⚑",       cls: "c", align: "c", tip: "AI vs Loughran-McDonald dictionary disagreement flag", sv: (r, s) => (s && s.disagreement) ? 1 : 0 },
  ];
  const colByKey = Object.fromEntries(nsCols.map(c => [c.key, c]));
  // Sort the symbol order. Missing values always sink to the bottom regardless
  // of direction, so unassessed holdings never crowd the top.
  const sortCol = colByKey[NS.sortKey] && colByKey[NS.sortKey].sortable !== false
    ? colByKey[NS.sortKey] : colByKey.total;
  const dir = NS.sortDir;
  const orderedSyms = symbols.slice().sort((sa, sb) => {
    const va = sortCol.sv(dataBySymbol.get(sa) || {}, NS.sentiment[sa]);
    const vb = sortCol.sv(dataBySymbol.get(sb) || {}, NS.sentiment[sb]);
    const na = va == null || (typeof va === "number" && !isFinite(va));
    const nb = vb == null || (typeof vb === "number" && !isFinite(vb));
    if (na && nb) return 0;
    if (na) return 1;
    if (nb) return -1;
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return 0;
  });
  const th = (c) => {
    const arrow = (c.sortable !== false && NS.sortKey === c.key)
      ? `<span class="ns-sort-arr">${NS.sortDir < 0 ? "▾" : "▴"}</span>` : "";
    const cls = [c.cls, c.sortable !== false ? "ns-sortable" : ""].filter(Boolean).join(" ");
    const tip = c.tip ? ` data-tip="${c.tip}"` : "";
    const dataAttr = c.sortable !== false ? ` data-sortkey="${c.key}"` : "";
    return `<th class="${cls}"${tip}${dataAttr}>${c.label}${arrow}</th>`;
  };
  let html = `<table class="ns-table">
    <thead><tr><th></th>${nsCols.map(th).join("")}</tr></thead><tbody>`;
  for (const sym of orderedSyms) {
    const r = dataBySymbol.get(sym) || {};
    const s = NS.sentiment[sym];
    const tierLabel = s ? (NS_LABELS[s.tier] || s.tier) : "—";
    const tierColor = s ? (NS_COLORS[s.tier] || "#94a3b8") : "var(--muted)";
    const events = s && s.events
      ? Object.entries(s.events).map(([ev, n]) =>
          `<span class="ns-ev-tag">${NS_EVENT_LABELS[ev] || ev}${n > 1 ? " ×" + n : ""}</span>`).join(" ")
      : "";
    const conf = s && s.confidence != null
      ? `<span class="ns-conf-track"><span class="ns-conf-fill" style="width:${(s.confidence * 100).toFixed(0)}%"></span></span>`
      : "—";
    const isOpen = NS.expanded === sym;
    html += `<tr class="ns-row" data-sym="${sym}">
      <td class="c ns-expander">${s ? (isOpen ? "▾" : "▸") : ""}</td>
      <td class="sym">${sym}</td>
      <td class="name">${escapeHtml(r.name || "")}</td>
      <td class="r">${r.price != null ? fmtMoney(r.price, r.currency) : "—"}</td>
      <td class="r">${r.pct_1d != null ? fmtPctSigned(r.pct_1d) : "—"}</td>
      <td class="r">${r.beta != null ? r.beta.toFixed(2) : "—"}</td>
      <td class="r">${s ? fmtSig(s.s_idio) : "—"}</td>
      <td class="r">${s ? fmtSig(s.s_sys) : "—"}</td>
      <td class="c" style="color:${tierColor};font-weight:600;font-size:11px">${s ? nsDot(s) + " " + tierLabel : "—"}</td>
      <td class="c">${conf}</td>
      <td>${events}</td>
      <td class="c">${s && s.disagreement ? '<span data-tip="AI and LM dictionary disagree on polarity">⚑</span>' : ""}</td>
    </tr>`;
    if (isOpen && s) {
      // Order by |score| desc so the most material (bullish OR bearish) news
      // leads; neutral/unscored articles sink to the bottom (item 9).
      const absScore = a => (a.score != null && isFinite(a.score)) ? Math.abs(a.score) : -1;
      const arts = NS.articles.filter(a => a.symbol === sym)
        .slice()
        .sort((a, b) => absScore(b) - absScore(a))
        .slice(0, 6).map(a =>
        `<div class="ns-brief-art">${nsScoreDot(a.score)} <a href="${escapeHtml(a.url || "#")}" target="_blank" rel="noopener">${escapeHtml(a.headline || "")}</a> <span class="ns-src">${escapeHtml(a.source || "")}</span></div>`).join("");
      const lm = s.s_lm != null ? ` · LM dictionary ${fmtSig(s.s_lm)}` : "";
      const fb = s.fallback ? " · single-call fallback" : "";
      html += `<tr class="ns-brief-row"><td colspan="12">
        <div class="ns-brief">${escapeHtml(s.summary || "")}</div>
        <div class="ns-brief-meta">${s.article_count || 0} articles${lm}${fb}</div>
        ${arts}
      </td></tr>`;
    }
  }
  html += "</tbody></table>";
  pBody.innerHTML = html;
  pBody.querySelectorAll("th.ns-sortable").forEach(thEl => {
    thEl.onclick = () => {
      const key = thEl.dataset.sortkey;
      if (NS.sortKey === key) {
        NS.sortDir = -NS.sortDir;         // same column → flip direction
      } else {
        NS.sortKey = key;
        // Text columns default ascending (A→Z); numeric default descending.
        NS.sortDir = (key === "ticker" || key === "company") ? 1 : -1;
      }
      renderPortfolioSentiment(symbols);
    };
  });
  pBody.querySelectorAll("tr.ns-row").forEach(tr => {
    tr.onclick = (e) => {
      if (e.target.closest("a")) return;
      const sym = tr.dataset.sym;
      NS.expanded = NS.expanded === sym ? null : sym;
      renderPortfolioSentiment(symbols);
    };
  });
}

const NS_DIAG_ABOUT_HTML = `
  <p><b>What this panel is.</b> Model Diagnostics is the evidence that the sentiment
  engine is calibrated and actually predictive — not just plausible-looking. It is
  computed from <code>.portfolio_tracker_sentiment_history.json</code>, the append-only
  log of every score the model has ever produced, joined against realized returns.</p>
  <p><b>Rank IC — score vs forward idiosyncratic return.</b> For each past score we take
  the stock's <i>idiosyncratic</i> forward return (its move with the market component
  <code>β·r_SPY</code> stripped out) over the next 1 and 5 trading days, then compute the
  <b>Spearman rank correlation</b> between score and that return. A positive IC means
  higher scores preceded higher stock-specific returns — the signal has predictive
  content. The t-stat flags whether it is distinguishable from zero; <code>n</code> is
  the number of scored observations with a realized forward return available. Small
  samples are labelled indicative — the number firms up as history accumulates.</p>
  <p><b>Mean forward 1d idio return by tier.</b> A monotonicity check: average realized
  next-day idiosyncratic return within each tier, from Very Bearish to Very Bullish. If
  the model is well-ordered these bars should rise left-to-right. A tier that is out of
  order is a miscalibration you can see at a glance.</p>
  <p><b>Score distribution (90d).</b> Histogram of <code>s_total</code> over the trailing
  90 days. It shows the engine is using the full range rather than clustering at neutral,
  and whether quantile tier-calibration is active yet (it switches on once ≥100
  observations exist; until then fixed thresholds are used).</p>
  <p class="ns-diag-about-foot">Methodology: per-article LLM scoring → recency×source×novelty×relevance
  weighted aggregation → <code>s_total = clip(κ·β·s_mkt + s_idio)</code> with κ=0.2, tiers
  calibrated to rolling score quantiles. Loughran-McDonald dictionary runs in parallel as
  a disagreement guardrail. See the ⚑ flag in the constituent table.</p>`;

function toggleNsDiagAbout() {
  const about = $("#ns-diag-about");
  const btn = $("#ns-diag-info");
  const willOpen = about.classList.contains("hidden");
  if (willOpen && !about.innerHTML) about.innerHTML = NS_DIAG_ABOUT_HTML;
  about.classList.toggle("hidden");
  if (btn) btn.classList.toggle("on", willOpen);
}

async function toggleNsDiagnostics() {
  const body = $("#ns-diag-body");
  const arrow = $("#ns-diag-arrow");
  const willOpen = body.classList.contains("hidden");
  body.classList.toggle("hidden");
  arrow.innerHTML = willOpen ? "&#9662;" : "&#9656;";
  if (!willOpen) return;
  body.innerHTML = '<div class="ns-loading">Computing diagnostics…</div>';
  try {
    const d = await fetch("/api/news-diagnostics").then(r => r.json());
    renderNsDiagnostics(d);
  } catch (e) {
    body.innerHTML = '<div class="ns-panel-empty">Diagnostics unavailable.</div>';
  }
}

function renderNsDiagnostics(d) {
  const body = $("#ns-diag-body");
  if (!d || d.error) {
    body.innerHTML = '<div class="ns-panel-empty">Diagnostics unavailable.</div>';
    return;
  }
  const parts = [];
  // Rank IC — the "does the score correlate with subsequent idiosyncratic
  // moves" evidence (Spearman; small n is labelled as indicative only).
  if (d.ic && (d.ic["1d"] || d.ic["5d"])) {
    const row = (label, o) => o
      ? `<tr><td>${label}</td><td class="r">${o.ic != null ? fmtSig(o.ic, 3) : "—"}</td><td class="r">${o.n}</td><td class="r">${o.t_stat != null ? o.t_stat : "—"}</td></tr>`
      : "";
    const smallN = Math.max((d.ic["1d"] || {}).n || 0, (d.ic["5d"] || {}).n || 0) < 200;
    parts.push(`<div class="ns-diag-sec">
      <h5>Rank IC — score vs forward idiosyncratic return</h5>
      <table class="ns-table ns-diag-table"><thead><tr><th>Horizon</th><th class="r">Spearman IC</th><th class="r">n</th><th class="r">t-stat</th></tr></thead>
      <tbody>${row("1 day", d.ic["1d"])}${row("5 days", d.ic["5d"])}</tbody></table>
      ${smallN ? '<div class="ns-diag-note">n &lt; 200 — indicative only; evidence accumulates with each refresh.</div>' : ""}
    </div>`);
  } else {
    parts.push('<div class="ns-diag-sec"><h5>Rank IC</h5><div class="ns-panel-empty">Not enough history yet — refresh over a few days to accumulate observations.</div></div>');
  }
  // Tier monotonicity — mean forward return should rise from very_bearish
  // to very_bullish.
  const tierOrder = ["very_bearish", "bearish", "neutral", "bullish", "very_bullish"];
  if (d.tiers && Object.keys(d.tiers).length) {
    const maxAbs = Math.max(0.1, ...tierOrder.map(t => Math.abs((d.tiers[t] || {}).mean_fwd_1d_pct || 0)));
    const bars = tierOrder.filter(t => d.tiers[t]).map(t => {
      const o = d.tiers[t];
      const w = Math.abs(o.mean_fwd_1d_pct) / maxAbs * 100;
      const color = NS_COLORS[t] || "#94a3b8";
      return `<div class="ns-diag-tier-row">
        <span class="ns-diag-tier-label" style="color:${color}">${NS_LABELS[t] || t}</span>
        <span class="ns-decomp-track"><span class="ns-decomp-fill ${o.mean_fwd_1d_pct >= 0 ? "pos" : "neg"}" style="width:${w}%"></span></span>
        <span class="ns-decomp-val">${fmtSig(o.mean_fwd_1d_pct, 2)}% (n=${o.n})</span>
      </div>`;
    }).join("");
    parts.push(`<div class="ns-diag-sec"><h5>Mean forward 1d idio return by tier</h5>${bars}</div>`);
  }
  // Score distribution — range-usage evidence.
  if (d.histogram && d.histogram.n > 0) {
    const maxC = Math.max(...d.histogram.counts, 1);
    const cols = d.histogram.counts.map((c, i) => {
      const h = Math.round(c / maxC * 48);
      const mid = -1 + (i + 0.5) * 0.1;
      const color = mid > 0.15 ? "#4ade80" : (mid < -0.15 ? "#f87171" : "#94a3b8");
      return `<div class="ns-hist-col" data-tip="[${(-1 + i * 0.1).toFixed(1)}, ${(-0.9 + i * 0.1).toFixed(1)}): ${c}" style="height:${Math.max(2, h)}px;background:${color}"></div>`;
    }).join("");
    const calNote = d.calibration_active
      ? `quantile calibration active (${d.calibration_n} obs)`
      : `fixed thresholds until ${100} obs (${d.calibration_n} so far)`;
    parts.push(`<div class="ns-diag-sec"><h5>Score distribution (90d, s_total)</h5>
      <div class="ns-hist">${cols}</div>
      <div class="ns-hist-axis"><span>-1</span><span>0</span><span>+1</span></div>
      <div class="ns-diag-note">${calNote}</div></div>`);
  }
  body.innerHTML = parts.join("") || '<div class="ns-panel-empty">No diagnostics data yet.</div>';
}

function patchRowSentiment(sentiment) {
  for (const row of DATA) {
    const s = sentiment[row.symbol];
    if (s) row.news_sentiment = s;
  }
  render();
}

/* --- Analytics controls --- */
$("#pf-mode-toggle").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-mode]");
  if (!btn) return;
  const mode = btn.dataset.mode;
  if (mode === "custom") { openWeightsPopup(); return; }
  STATE.mode = mode;
  updateModeButtons();
  requestAnalytics();
});
$("#pf-period-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-p]");
  if (!btn) return;
  STATE.period = btn.dataset.p;
  updatePeriodButtons();
  requestAnalytics();
});
function wireOverlayPill(id, stateKey) {
  const el = $(id);
  if (!el) return;
  const toggle = () => {
    STATE[stateKey] = !STATE[stateKey];
    el.dataset.on = STATE[stateKey] ? "1" : "0";
    el.setAttribute("aria-checked", STATE[stateKey] ? "true" : "false");
    renderAnalyticsBody();
  };
  el.addEventListener("click", (e) => {
    // Clicking the info icon shouldn't toggle the overlay.
    if (e.target.closest(".ovl-info")) return;
    toggle();
  });
  el.addEventListener("keydown", (e) => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); }
  });
  // Initial sync from STATE.
  el.dataset.on = STATE[stateKey] ? "1" : "0";
  el.setAttribute("aria-checked", STATE[stateKey] ? "true" : "false");
}
wireOverlayPill("#pf-show-spy", "showSpy");
wireOverlayPill("#pf-show-ndx", "showNdx");
wireOverlayPill("#pf-show-sec", "showSec");
wireOverlayPill("#pf-show-dd", "showDd");

/* --- Weights popup wiring --- */
$("#pf-weights-bg").addEventListener("click", (e) => { if (e.target.id === "pf-weights-bg") closeWeightsPopup(); });
$("#pf-weights-close").addEventListener("click", closeWeightsPopup);
$("#pf-weights-cancel").addEventListener("click", closeWeightsPopup);
$("#pf-weights-equal").addEventListener("click", resetDraftToEqual);
$("#pf-weights-reset").addEventListener("click", resetDraftToCap);
$("#pf-weights-apply").addEventListener("click", applyWeightsDraft);
$("#pf-weights-save").addEventListener("click", savePresetClick);
$("#pf-weights-saveas").addEventListener("click", saveAsPresetClick);
$("#pf-weights-delete").addEventListener("click", deletePresetClick);
$("#pf-name-prompt-ok").addEventListener("click", _ipSubmit);
$("#pf-name-prompt-cancel").addEventListener("click", hideInlinePrompt);
$("#pf-name-prompt-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); _ipSubmit(); }
  else if (e.key === "Escape") { e.preventDefault(); hideInlinePrompt(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const ip = document.getElementById("pf-name-prompt");
  if (ip && ip.classList.contains("show")) { hideInlinePrompt(); return; }
  if ($("#pf-weights-bg").classList.contains("show")) closeWeightsPopup();
});
// Initial paint — empty until DATA is built but renders the [+] / Equal/Cap baseline.
renderModeBar();

/* ===========================================================================
 * MPT — Portfolio Optimization overlay
 * --------------------------------------------------------------------------
 * The Optimize button opens a full-screen workspace that computes the
 * long-only efficient frontier from the active portfolio, lets the user pick
 * a point along it with a slider, and apply / save the resulting weights as
 * a named preset. Math lives in mpt.py (Critical Line Algorithm); this code
 * just drives the UI and renders the inline SVG chart.
 * --------------------------------------------------------------------------- */
const MPT = {
  result: null,        // latest /api/efficient-frontier response
  selectedIdx: 0,      // index into result.frontier for the slider marker
  cvarIdx: 0,          // index into result.cvar_frontier for the CVaR slider
  hoverIdx: null,      // index of point under cursor (cloud or frontier)
  activeLine: "frontier", // which curve drives sidebar/apply: "frontier" | "cvar"
  pulseUntil: 0,       // performance.now() time at which the current pulse ends
  pulseKind: null,     // legend key being pulsed (frontier|cvar|tangency|equal|cap|current)
  pulseRaf: 0,         // rAF id for the active pulse animation loop
  view: null,          // portfolio name this run is bound to
  runs: [],            // saved runs metadata for the active view
  busy: false,
};

function openMptOverlay() {
  if (!DATA.length) return toast("Build a portfolio first.");
  if (!STATE.activeView || STATE.activeView === AD_HOC_KEY) {
    // Ad-hoc tabs still get to optimize, but the runs sidebar + save flows
    // need a named view to attach to. We surface this with a status line.
    document.getElementById("pf-mpt-sub").textContent =
      "Save the portfolio first to keep runs / presets.";
  } else {
    document.getElementById("pf-mpt-sub").textContent =
      "Markowitz long-only · Critical Line Algorithm · " + STATE.activeView;
  }
  MPT.view = STATE.activeView;
  document.getElementById("pf-mpt-bg").classList.add("show");
  // Lock body scroll while the overlay is open so the page underneath
  // can't move. Inner sidebar still scrolls via its own overflow-y.
  document.body.dataset.mptPrevOverflow = document.body.style.overflow || "";
  document.body.style.overflow = "hidden";
  mptLoadRuns();
}
function closeMptOverlay() {
  document.getElementById("pf-mpt-bg").classList.remove("show");
  document.body.style.overflow = document.body.dataset.mptPrevOverflow || "";
  delete document.body.dataset.mptPrevOverflow;
  // Cancel any in-flight progress animation so a half-filled bar doesn't
  // linger if the user reopens the overlay before the next run.
  mptProgressStop();
}

// Budget label table — also drives the deterministic progress bar duration
// (#9) and the legend metadata strip.
const MPT_BUDGET_LABEL = {
  fast: "Fast (~2s)",
  standard: "Standard (~5s)",
  thorough: "Thorough (~15s)",
  exhaustive: "Exhaustive (~60s)",
};
const MPT_BUDGET_SECONDS = { fast: 2, standard: 5, thorough: 15, exhaustive: 60 };

function mptSetBudget(value) {
  const root = document.getElementById("pf-mpt-budget");
  if (!root) return;
  const v = MPT_BUDGET_LABEL[value] ? value : "standard";
  root.dataset.value = v;
  const label = root.querySelector(".pf-mpt-select-label");
  if (label) label.textContent = MPT_BUDGET_LABEL[v];
  root.querySelectorAll(".pf-mpt-select-menu li").forEach(li => {
    li.setAttribute("aria-selected", li.dataset.value === v ? "true" : "false");
  });
}

function mptGetParams() {
  const lookback = document.querySelector("#pf-mpt-lookback .active")?.dataset.v || "3Y";
  const frequency = document.querySelector("#pf-mpt-freq .active")?.dataset.v || "weekly";
  const rf = (Number(document.getElementById("pf-mpt-rf").value) || 0) / 100;
  const budget = document.getElementById("pf-mpt-budget").dataset.value || "standard";
  const mode = document.querySelector("#pf-mpt-mode .active")?.dataset.v || "sparse";
  const diversified = mode === "diversified";
  return {lookback, frequency, rf, budget, diversified};
}

/* --- Deterministic progress bar --- */
let _mptProgressTimer = null;
function mptProgressStart(budget) {
  const chart = document.getElementById("pf-mpt-chart");
  const status = document.getElementById("pf-mpt-status");
  if (!chart || !status) return;
  const dur = MPT_BUDGET_SECONDS[budget] || 5;
  status.innerHTML = `
    <div class="mpt-progress" id="mpt-progress">
      <div class="mpt-progress-track"><div class="mpt-progress-fill"></div></div>
      <div class="mpt-progress-label">
        <span class="mpt-progress-text">Optimizing portfolio…</span>
        <span class="mpt-progress-timer">0.0s / ~${dur}s</span>
      </div>
    </div>`;
  const root = status.querySelector("#mpt-progress");
  const fill = root.querySelector(".mpt-progress-fill");
  const timer = root.querySelector(".mpt-progress-timer");
  const text = root.querySelector(".mpt-progress-text");
  // Force layout, then start the CSS width transition over the budget window.
  void fill.offsetWidth;
  fill.style.transition = `width ${dur}s linear`;
  fill.style.width = "100%";
  const start = performance.now();
  _mptProgressTimer = setInterval(() => {
    const elapsed = (performance.now() - start) / 1000;
    if (elapsed >= dur) {
      text.textContent = "Finalizing…";
      timer.textContent = `${elapsed.toFixed(1)}s / ~${dur}s`;
    } else {
      timer.textContent = `${elapsed.toFixed(1)}s / ~${dur}s`;
    }
  }, 100);
}
function mptProgressStop(state) {
  if (_mptProgressTimer) { clearInterval(_mptProgressTimer); _mptProgressTimer = null; }
  if (state === "done" || state === "fail") {
    const root = document.querySelector("#mpt-progress");
    if (root) {
      root.classList.add(state);
      const fill = root.querySelector(".mpt-progress-fill");
      if (fill) fill.style.width = "100%";
    }
  }
}

async function mptRun() {
  if (!DATA.length) return;
  const btn = document.getElementById("pf-mpt-run");
  const status = document.getElementById("pf-mpt-status");
  // Clear any previous frame so the cloud/frontier disappear during compute.
  mptClearChart();
  const params = mptGetParams();
  mptProgressStart(params.budget);
  btn.disabled = true; MPT.busy = true;
  try {
    const r = await fetch("/api/efficient-frontier", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        rows: DATA, display_ccy: FX_QUOTE,
        ...params,
        current_weights: weightsForMode(STATE.mode),
      }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || ("HTTP " + r.status));
    MPT.result = d;
    // Default selection = tangency (max Sharpe), if available, else mid-frontier
    if (d.tangency) {
      let best = 0, bestSh = -Infinity;
      d.frontier.forEach((p, i) => {
        const sh = p.vol > 1e-9 ? (p.ret - params.rf) / p.vol : -Infinity;
        if (sh > bestSh) { bestSh = sh; best = i; }
      });
      MPT.selectedIdx = best;
    } else {
      MPT.selectedIdx = Math.floor(d.frontier.length / 2);
    }
    document.getElementById("pf-mpt-slider").disabled = false;
    document.getElementById("pf-mpt-slider").max = String(Math.max(0, d.frontier.length - 1));
    document.getElementById("pf-mpt-slider").value = String(MPT.selectedIdx);
    // CVaR slider: index 0 = strictest tail (CVaR99), index N-1 = CVaR50.
    // Default selection = midpoint of the curve so neither extreme dominates.
    const cv = d.cvar_frontier || [];
    const cvarSlider = document.getElementById("pf-mpt-cvar-slider");
    if (cv.length) {
      MPT.cvarIdx = Math.floor(cv.length / 2);
      cvarSlider.disabled = false;
      cvarSlider.max = String(Math.max(0, cv.length - 1));
      cvarSlider.value = String(MPT.cvarIdx);
    } else {
      MPT.cvarIdx = 0;
      cvarSlider.disabled = true;
      cvarSlider.value = "0";
    }
    MPT.activeLine = "frontier";
    mptProgressStop("done");
    if (status) status.innerHTML = "";
    mptRender();
    // Auto-save the run. Server-side eviction (mpt save_mpt_run) drops any
    // stale runs whose portfolio composition or risk-free rate differs from
    // this one, keeping the Recent-runs list relevant without UI prompts.
    mptSaveRun({silent: true}).catch(() => {});
  } catch (e) {
    mptProgressStop("fail");
    if (status) {
      status.innerHTML = `<div class="pf-mpt-error">Optimization failed: ${escapeHtml(e.message || String(e))}</div>`;
    }
  } finally {
    btn.disabled = false; MPT.busy = false;
  }
}

function mptClearChart() {
  const base = document.getElementById("pf-mpt-base");
  if (base) { const ctx = base.getContext("2d"); ctx && ctx.clearRect(0, 0, base.width, base.height); }
  const ov = document.getElementById("pf-mpt-overlay");
  if (ov) { const ctx = ov.getContext("2d"); ctx && ctx.clearRect(0, 0, ov.width, ov.height); }
  const leg = document.getElementById("pf-mpt-legend"); if (leg) leg.innerHTML = "";
}

function mptRender() {
  const d = MPT.result; if (!d) return;
  mptRenderChart();
  mptRenderSide();
}

// MPT._proj is the shared projection used by every chart draw call and the
// hit-test. Single source of truth — both canvases agree on every coord by
// construction, eliminating the SVG/canvas drift the old 3-layer chart had.
function mptCurrentScale() { return MPT._proj; }

// Resize both canvases identically through one helper so their backing
// stores cannot drift apart. CSS controls the *display* size (inset:0 +
// width/height:100%); we touch ONLY the backing store. Setting inline
// width/height previously left the canvas stuck at its first measurement
// even when the modal reflowed, causing axis labels to render below the
// chart's visual box.
function mptSizeCanvases(host) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const rect = host.getBoundingClientRect();
  const cssW = Math.max(360, Math.floor(rect.width));
  const cssH = Math.max(280, Math.floor(rect.height));
  const W = Math.floor(cssW * dpr), H = Math.floor(cssH * dpr);
  for (const id of ["pf-mpt-base", "pf-mpt-overlay"]) {
    const cv = document.getElementById(id);
    if (!cv) continue;
    cv.width = W; cv.height = H;
  }
  return {cssW, cssH, dpr};
}

// Tick generation — nice rounding by powers of 10.
function mptTicks(lo, hi, n) {
  const span = hi - lo; if (span <= 0) return [lo];
  const step0 = Math.pow(10, Math.floor(Math.log10(span / n)));
  const norm = span / (n * step0);
  const step = step0 * (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1);
  const start = Math.ceil(lo / step) * step;
  const out = []; for (let v = start; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

function mptRenderChart() {
  const d = MPT.result; if (!d) return;
  const host = document.getElementById("pf-mpt-chart");
  // Atomic size for both canvases.
  const {cssW, cssH, dpr} = mptSizeCanvases(host);
  const pad = {l: 56, r: 18, t: 18, b: 38};

  // Domain from cloud + frontier + anchors.
  let xMax = 0, xMin = Infinity, yMax = -Infinity, yMin = Infinity;
  const consume = (v, r) => { if (v < xMin) xMin = v; if (v > xMax) xMax = v; if (r < yMin) yMin = r; if (r > yMax) yMax = r; };
  (d.cloud || []).forEach(p => consume(p[0], p[1]));
  (d.frontier || []).forEach(p => consume(p.vol, p.ret));
  (d.cvar_frontier || []).forEach(p => consume(p.vol, p.ret));
  Object.values(d.anchors || {}).forEach(a => a && consume(a.vol, a.ret));
  if (!isFinite(xMin)) { xMin = 0; xMax = 0.3; yMin = 0; yMax = 0.2; }
  const params = mptGetParams();
  yMin = Math.min(yMin, params.rf);
  const dx = (xMax - xMin) * 0.06 || 0.01;
  const dy = (yMax - yMin) * 0.08 || 0.01;
  xMin = Math.max(0, xMin - dx); xMax += dx; yMin -= dy; yMax += dy;

  // Projection in CSS pixels (toPx); fromPx for hit-testing.
  const xToPx = v => pad.l + (v - xMin) / (xMax - xMin) * (cssW - pad.l - pad.r);
  const yToPx = r => cssH - pad.b - (r - yMin) / (yMax - yMin) * (cssH - pad.t - pad.b);
  const proj = {xMin, xMax, yMin, yMax, pad, cssW, cssH, dpr,
                X: xToPx, Y: yToPx, toPx: (v, r) => [xToPx(v), yToPx(r)]};
  MPT._proj = proj;

  // Sharpe-by-color setup.
  const sharpeOf = p => (p[0] > 1e-9 ? (p[1] - params.rf) / p[0] : 0);
  let sMin = Infinity, sMax = -Infinity;
  (d.cloud || []).forEach(p => { const s = sharpeOf(p); if (s < sMin) sMin = s; if (s > sMax) sMax = s; });
  if (!isFinite(sMin)) { sMin = 0; sMax = 1; }
  function sharpeColor(s) {
    const t = Math.max(0, Math.min(1, (s - sMin) / Math.max(1e-9, sMax - sMin)));
    const stops = [[94,40,120],[33,144,141],[253,231,37]];
    const i = t * 2, j = Math.floor(i), f = i - j;
    const a = stops[j], b = stops[Math.min(2, j + 1)];
    return `rgb(${Math.round(a[0]+(b[0]-a[0])*f)},${Math.round(a[1]+(b[1]-a[1])*f)},${Math.round(a[2]+(b[2]-a[2])*f)})`;
  }

  // --- Base canvas: axes + cloud + frontier + anchors + tangency line ---
  const baseCv = document.getElementById("pf-mpt-base");
  const ctx = baseCv.getContext("2d");
  // Reset transform to identity, then scale so every subsequent call is in
  // CSS pixels — the same units as the projection.
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  drawAxes(ctx, proj, d, params);
  drawCloud(ctx, d.cloud || [], proj, sharpeOf, sharpeColor);
  drawFrontierAndAnchors(ctx, d, proj, params);

  // --- Overlay canvas: selection + hover ghost ---
  drawOverlay();

  // --- Wire interaction on the overlay canvas (top of stack) ---
  const ov = document.getElementById("pf-mpt-overlay");
  // Replace listeners by cloning so we never stack handlers on re-render.
  const fresh = ov.cloneNode(false);
  ov.parentNode.replaceChild(fresh, ov);
  // Hit-test against BOTH the MV frontier and the CVaR curve. Returns
  // {line, idx} for the nearest point; line drives which slider moves.
  function _hitPoint(ev) {
    const r = fresh.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    const px = (ev.clientX - r.left) * (cssW / r.width);
    const py = (ev.clientY - r.top) * (cssH / r.height);
    let bestLine = "frontier", best = 0, bestD = Infinity;
    (d.frontier || []).forEach((p, i) => {
      const dxp = xToPx(p.vol) - px, dyp = yToPx(p.ret) - py;
      const dd = dxp * dxp + dyp * dyp;
      if (dd < bestD) { bestD = dd; best = i; bestLine = "frontier"; }
    });
    (d.cvar_frontier || []).forEach((p, i) => {
      const dxp = xToPx(p.vol) - px, dyp = yToPx(p.ret) - py;
      const dd = dxp * dxp + dyp * dyp;
      if (dd < bestD) { bestD = dd; best = i; bestLine = "cvar"; }
    });
    return {line: bestLine, idx: best};
  }
  fresh.addEventListener("click", (ev) => {
    const hit = _hitPoint(ev);
    if (!hit) return;
    if (hit.line === "cvar") {
      MPT.cvarIdx = hit.idx;
      MPT.activeLine = "cvar";
      document.getElementById("pf-mpt-cvar-slider").value = String(hit.idx);
    } else {
      MPT.selectedIdx = hit.idx;
      MPT.activeLine = "frontier";
      document.getElementById("pf-mpt-slider").value = String(hit.idx);
    }
    MPT.hoverIdx = null;
    mptHideFrontierTip();
    mptUpdateSelection();
    mptRenderSide();
    // Flash the Apply button so the user knows the point is selected and ready.
    const applyBtn = document.getElementById("pf-mpt-apply");
    if (applyBtn) {
      applyBtn.classList.add("pf-mpt-apply-flash");
      setTimeout(() => applyBtn.classList.remove("pf-mpt-apply-flash"), 600);
    }
  });
  fresh.addEventListener("mousemove", (ev) => {
    const hit = _hitPoint(ev);
    if (!hit || hit.line !== "frontier") {
      MPT.hoverIdx = null;
      mptUpdateSelection();
      mptHideFrontierTip();
      return;
    }
    MPT.hoverIdx = hit.idx;
    mptUpdateSelection();
    mptShowFrontierTip(ev, hit.idx);
  });
  fresh.addEventListener("mouseleave", () => {
    MPT.hoverIdx = null;
    mptUpdateSelection();
    mptHideFrontierTip();
  });

  // --- Legend with marker-shaped swatches. Each entry is clickable — see
  //     mptLegendClick below for the kind → action mapping. ---
  const legend = document.getElementById("pf-mpt-legend");
  const hasCvar = (d.cvar_frontier || []).length > 0;
  legend.innerHTML = `
    <span data-legend="frontier" title="Click to switch to the efficient-frontier slider and highlight the line.">${legendSwatch("frontier")}Efficient frontier</span>
    ${hasCvar ? `<span data-legend="cvar" title="Click to switch to the CVaR slider and highlight the curve.">${legendSwatch("cvar")}CVaR-optimal (α=99↔50)</span>` : ""}
    <span data-legend="tangency" title="Click to jump to the tangency portfolio.">${legendSwatch("tangency")}Tangency (max Sharpe)</span>
    <span data-legend="equal" title="Click to jump to the equal-weight portfolio on the frontier.">${legendSwatch("equal")}Equal-weight</span>
    <span data-legend="cap" title="Click to jump to the cap-weight portfolio on the frontier.">${legendSwatch("cap")}Cap-weight</span>
    <span data-legend="current" title="Click to jump to the current portfolio on the frontier.">${legendSwatch("current")}Current</span>
    <span class="no-click">${legendSwatch("selected")}Selected</span>
    <span class="no-click" style="margin-left:auto">${(d.meta?.n_samples_actual || (d.cloud || []).length).toLocaleString()} Monte-Carlo portfolios · ${d.meta?.n_obs || "?"} ${d.params?.frequency || "?"} obs · ${d.meta?.total_ms || "?"}ms</span>
  `;
  legend.querySelectorAll("span[data-legend]").forEach(el => {
    el.addEventListener("click", () => mptLegendClick(el.dataset.legend));
  });
}

// Snap the MV-frontier slider to the index nearest (vol, ret) — used by
// the legend's anchor entries so clicking "Equal-weight" jumps the
// frontier marker to the closest feasible point on the curve.
function _nearestFrontierIdx(vol, ret) {
  const d = MPT.result; if (!d || !(d.frontier || []).length) return 0;
  let best = 0, bestD = Infinity;
  d.frontier.forEach((p, i) => {
    const dvx = (p.vol - vol), dvy = (p.ret - ret);
    const dd = dvx * dvx + dvy * dvy;
    if (dd < bestD) { bestD = dd; best = i; }
  });
  return best;
}

// Legend click handler — kind → (select point + pulse). For the two line
// entries we switch activeLine; for anchor markers we snap the MV-frontier
// slider to the closest point on the curve.
function mptLegendClick(kind) {
  const d = MPT.result; if (!d) return;
  switch (kind) {
    case "frontier":
      MPT.activeLine = "frontier";
      break;
    case "cvar":
      if ((d.cvar_frontier || []).length) MPT.activeLine = "cvar";
      break;
    case "tangency":
      if (d.tangency) {
        MPT.selectedIdx = _nearestFrontierIdx(d.tangency.vol, d.tangency.ret);
        MPT.activeLine = "frontier";
        document.getElementById("pf-mpt-slider").value = String(MPT.selectedIdx);
      }
      break;
    case "equal":
    case "cap":
    case "current": {
      const a = (d.anchors || {})[kind];
      if (a) {
        MPT.selectedIdx = _nearestFrontierIdx(a.vol, a.ret);
        MPT.activeLine = "frontier";
        document.getElementById("pf-mpt-slider").value = String(MPT.selectedIdx);
      }
      break;
    }
  }
  mptPulse(kind);
  mptUpdateSelection();
  mptRenderSide();
}

function legendSwatch(kind) {
  const c = 'class="lg-swatch"';
  switch (kind) {
    case "frontier":
      return `<svg ${c} viewBox="0 0 14 12"><path d="M1 9 Q7 1 13 4" fill="none" stroke="var(--accent)" stroke-width="2.2"/></svg>`;
    case "cvar":
      return `<svg ${c} viewBox="0 0 14 12"><path d="M1 9 Q7 1 13 4" fill="none" stroke="#ea580c" stroke-width="2" stroke-dasharray="3 2"/></svg>`;
    case "tangency":
      return `<svg ${c} viewBox="0 0 14 12"><polygon points="${star(7,6,5,2.4,5)}" fill="#fbbf24" stroke="#7c2d12" stroke-width="0.6"/></svg>`;
    case "equal":
      return `<svg ${c} viewBox="0 0 14 12"><polygon points="7,1 12,6 7,11 2,6" fill="#8b5cf6"/></svg>`;
    case "cap":
      return `<svg ${c} viewBox="0 0 14 12"><polygon points="7,1 12,11 2,11" fill="#06b6d4"/></svg>`;
    case "current":
      return `<svg ${c} viewBox="0 0 14 12"><path d="M2 2 L12 10 M12 2 L2 10" stroke="#f59e0b" stroke-width="2.2"/></svg>`;
    case "selected":
      return `<svg ${c} viewBox="0 0 14 12"><circle cx="7" cy="6" r="4.5" fill="none" stroke="var(--accent)" stroke-width="1.8"/><circle cx="7" cy="6" r="1.6" fill="var(--accent)"/></svg>`;
    default:
      return `<span class="lg-dot" style="background:var(--muted)"></span>`;
  }
}

// CSS-variable color resolver: canvas can't read `var(--accent)` directly,
// so look it up against :root once and cache for the current render.
function _mptCssColor(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch (_e) { return fallback; }
}

function drawAxes(ctx, proj, d, params) {
  const {pad, cssW, cssH, xMin, xMax, yMin, yMax, X, Y} = proj;
  const accent = _mptCssColor("--accent", "#0969da");
  const border = _mptCssColor("--border", "#d0d7de");
  const muted  = _mptCssColor("--muted",  "#6e7781");
  const bgCv   = _mptCssColor("--bg-canvas", "#ffffff");

  // Plot box background (subtle) + border.
  ctx.fillStyle = bgCv;
  ctx.globalAlpha = 0.04;
  ctx.fillRect(pad.l, pad.t, cssW - pad.l - pad.r, cssH - pad.t - pad.b);
  ctx.globalAlpha = 1;
  ctx.strokeStyle = border; ctx.lineWidth = 1;
  ctx.strokeRect(pad.l + 0.5, pad.t + 0.5, cssW - pad.l - pad.r - 1, cssH - pad.t - pad.b - 1);

  const xt = mptTicks(xMin, xMax, 5);
  const yt = mptTicks(yMin, yMax, 5);
  const fmtPct = v => (v * 100).toFixed(v < 0.1 ? 1 : 0) + "%";

  ctx.save();
  ctx.strokeStyle = border; ctx.globalAlpha = 0.45;
  ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
  for (const v of xt) {
    const x = Math.round(X(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, cssH - pad.b); ctx.stroke();
  }
  for (const v of yt) {
    const y = Math.round(Y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(cssW - pad.r, y); ctx.stroke();
  }
  ctx.restore();

  ctx.fillStyle = muted;
  ctx.font = "10px ui-sans-serif, -apple-system, system-ui, sans-serif";
  ctx.textBaseline = "middle";
  ctx.textAlign = "center";
  for (const v of xt) ctx.fillText(fmtPct(v), X(v), cssH - pad.b + 14);
  ctx.textAlign = "end";
  for (const v of yt) ctx.fillText(fmtPct(v), pad.l - 6, Y(v));

  // Axis titles.
  ctx.font = "11px ui-sans-serif, -apple-system, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText("Volatility (annualized)", (cssW - pad.r + pad.l) / 2, cssH - 6);
  ctx.save();
  ctx.translate(14, (cssH - pad.b + pad.t) / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("Return (annualized)", 0, 0);
  ctx.restore();

  // Tangency dashed line from rf marker → tangency, extended past it.
  if (d.tangency) {
    const tx = X(d.tangency.vol), ty = Y(d.tangency.ret);
    const rfX = X(0), rfY = Y(params.rf);
    const dxp = tx - rfX, dyp = ty - rfY;
    const k = 1.6;
    const ex = rfX + dxp * k, ey = rfY + dyp * k;
    ctx.save();
    ctx.strokeStyle = accent; ctx.lineWidth = 1.2;
    ctx.setLineDash([5, 4]); ctx.globalAlpha = 0.85;
    ctx.beginPath(); ctx.moveTo(rfX, rfY); ctx.lineTo(ex, ey); ctx.stroke();
    ctx.restore();
    ctx.fillStyle = accent; ctx.globalAlpha = 0.8;
    ctx.beginPath(); ctx.arc(rfX, rfY, 3, 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha = 1;
    ctx.fillStyle = accent;
    ctx.textAlign = "start"; ctx.textBaseline = "alphabetic";
    ctx.font = "10px ui-sans-serif, -apple-system, system-ui, sans-serif";
    ctx.fillText(`rf ${(params.rf * 100).toFixed(2)}%`, rfX + 8, rfY - 6);
  }
}

// Cloud paint on the base canvas — single fillRect per point. The caller
// already set ctx transform so we're in CSS pixels; no manual dpr math.
function drawCloud(ctx, cloud, proj, sharpeOf, sharpeColor) {
  if (!cloud || !cloud.length) return;
  const {X, Y, pad, cssW, cssH, dpr} = proj;
  ctx.save();
  // Clip to the plot box so cloud dots never escape onto the axes.
  ctx.beginPath();
  ctx.rect(pad.l, pad.t, cssW - pad.l - pad.r, cssH - pad.t - pad.b);
  ctx.clip();
  ctx.globalAlpha = 0.85;
  // Dot side ~1.7 CSS px; bumped slightly on hi-dpi so dots stay visible.
  const r = Math.max(1.1, 1.7 * Math.min(dpr, 1.5));
  const step = cloud.length > 1_200_000 ? Math.ceil(cloud.length / 1_200_000) : 1;
  for (let i = 0; i < cloud.length; i += step) {
    const p = cloud[i];
    const x = X(p[0]), y = Y(p[1]);
    ctx.fillStyle = sharpeColor(sharpeOf(p));
    ctx.fillRect(x - r * 0.5, y - r * 0.5, r, r);
  }
  ctx.restore();
}

function drawFrontierAndAnchors(ctx, d, proj, params) {
  const {X, Y} = proj;
  const accent = _mptCssColor("--accent", "#0969da");
  const cvarColor = "#ea580c";
  if (d.frontier && d.frontier.length) {
    ctx.save();
    ctx.strokeStyle = accent; ctx.lineWidth = 2.2;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.beginPath();
    d.frontier.forEach((p, i) => {
      const x = X(p.vol), y = Y(p.ret);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.restore();
  }
  // CVaR curve: dashed orange polyline distinct from the MV frontier so the
  // two families of optima don't blur together.
  if (d.cvar_frontier && d.cvar_frontier.length) {
    ctx.save();
    ctx.strokeStyle = cvarColor; ctx.lineWidth = 2.0;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    d.cvar_frontier.forEach((p, i) => {
      const x = X(p.vol), y = Y(p.ret);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.restore();
  }
  if (d.min_vol) drawMarker(ctx, "endpoint", X(d.min_vol.vol), Y(d.min_vol.ret));
  if (d.max_ret) drawMarker(ctx, "endpoint", X(d.max_ret.vol), Y(d.max_ret.ret));
  const an = d.anchors || {};
  if (an.equal)   drawMarker(ctx, "equal",   X(an.equal.vol),   Y(an.equal.ret));
  if (an.cap)     drawMarker(ctx, "cap",     X(an.cap.vol),     Y(an.cap.ret));
  if (an.current) drawMarker(ctx, "current", X(an.current.vol), Y(an.current.ret));
  if (d.tangency) drawMarker(ctx, "tangency", X(d.tangency.vol), Y(d.tangency.ret));
}

function drawMarker(ctx, kind, x, y) {
  ctx.save();
  switch (kind) {
    case "endpoint":
      ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.stroke();
      break;
    case "equal":
      ctx.fillStyle = "#8b5cf6"; ctx.globalAlpha = 0.95;
      ctx.beginPath();
      ctx.moveTo(x, y - 6); ctx.lineTo(x + 6, y); ctx.lineTo(x, y + 6); ctx.lineTo(x - 6, y); ctx.closePath();
      ctx.fill();
      break;
    case "cap":
      ctx.fillStyle = "#06b6d4"; ctx.globalAlpha = 0.95;
      ctx.beginPath();
      ctx.moveTo(x, y - 6); ctx.lineTo(x + 6, y + 5); ctx.lineTo(x - 6, y + 5); ctx.closePath();
      ctx.fill();
      break;
    case "current":
      ctx.strokeStyle = "#f59e0b"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(x - 5, y - 5); ctx.lineTo(x + 5, y + 5);
      ctx.moveTo(x + 5, y - 5); ctx.lineTo(x - 5, y + 5); ctx.stroke();
      break;
    case "tangency": {
      const R = 8, r = 4, n = 5;
      ctx.fillStyle = "#fbbf24"; ctx.strokeStyle = "#7c2d12"; ctx.lineWidth = 0.8;
      ctx.beginPath();
      for (let i = 0; i < 2 * n; i++) {
        const ang = -Math.PI / 2 + i * Math.PI / n;
        const rad = i % 2 === 0 ? R : r;
        const px = x + rad * Math.cos(ang), py = y + rad * Math.sin(ang);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.closePath(); ctx.fill(); ctx.stroke();
      break;
    }
  }
  ctx.restore();
}

// Paint just the overlay canvas (selection ring + hover ghost + pulse).
// Cheap — called on slider input, mousemove, and the pulse animation loop;
// the base canvas is untouched.
function drawOverlay() {
  const d = MPT.result; if (!d) return;
  const proj = MPT._proj; if (!proj) return;
  const cv = document.getElementById("pf-mpt-overlay");
  if (!cv) return;
  const {cssW, cssH, dpr, X, Y} = proj;
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  const accent = _mptCssColor("--accent", "#0969da");
  const cvarColor = "#ea580c";

  // Hover ghost (only when it isn't the same point as the selection).
  if (MPT.hoverIdx != null && MPT.hoverIdx !== MPT.selectedIdx) {
    const hp = d.frontier[MPT.hoverIdx];
    if (hp) {
      const hx = X(hp.vol), hy = Y(hp.ret);
      ctx.save();
      ctx.strokeStyle = accent; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.6;
      ctx.beginPath(); ctx.arc(hx, hy, 6, 0, Math.PI * 2); ctx.stroke();
      ctx.fillStyle = accent; ctx.globalAlpha = 0.8;
      ctx.beginPath(); ctx.arc(hx, hy, 2.2, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
    }
  }

  // Inactive (muted) ring on the curve that ISN'T currently driving the
  // sidebar — keeps both sliders' positions visible at a glance.
  const inactiveIsCvar = MPT.activeLine !== "cvar";
  const cv_arr = d.cvar_frontier || [];
  const muted = inactiveIsCvar ? cv_arr[MPT.cvarIdx] : d.frontier[MPT.selectedIdx];
  if (muted) {
    const mx = X(muted.vol), my = Y(muted.ret);
    const mcol = inactiveIsCvar ? cvarColor : accent;
    ctx.save();
    ctx.strokeStyle = mcol; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.55;
    ctx.beginPath(); ctx.arc(mx, my, 7, 0, Math.PI * 2); ctx.stroke();
    ctx.restore();
  }

  // Active selection ring on the currently-driven curve.
  const activeIsCvar = MPT.activeLine === "cvar";
  const sel = activeIsCvar ? cv_arr[MPT.cvarIdx] : d.frontier[MPT.selectedIdx];
  if (sel) {
    const x = X(sel.vol), y = Y(sel.ret);
    const col = activeIsCvar ? cvarColor : accent;
    ctx.save();
    ctx.strokeStyle = col; ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.stroke();
    ctx.fillStyle = col;
    ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill();
    ctx.restore();
  }

  // Pulse layer: fades over the pulse window.
  drawPulse(ctx, d, proj, accent, cvarColor);
}

// Pulse animation — fades a highlight ring (or thicker line stroke for the
// frontier/cvar entries) over a 700 ms window. Uses MPT.pulseUntil + MPT.pulseKind.
function drawPulse(ctx, d, proj, accent, cvarColor) {
  const now = performance.now();
  if (!MPT.pulseKind || now >= MPT.pulseUntil) return;
  const PULSE_MS = 700;
  const t = Math.max(0, Math.min(1, (MPT.pulseUntil - now) / PULSE_MS));
  // Ease-out: alpha fades 0.85 → 0; radius grows 6 → 22.
  const alpha = 0.85 * t;
  const radius = 6 + (1 - t) * 16;
  const {X, Y} = proj;
  const kind = MPT.pulseKind;

  const ringAt = (vol, ret, color) => {
    if (vol == null || ret == null) return;
    ctx.save();
    ctx.strokeStyle = color; ctx.lineWidth = 3; ctx.globalAlpha = alpha;
    ctx.beginPath(); ctx.arc(X(vol), Y(ret), radius, 0, Math.PI * 2); ctx.stroke();
    ctx.restore();
  };
  const lineWith = (pts, color, lw) => {
    if (!pts || !pts.length) return;
    ctx.save();
    ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.globalAlpha = alpha;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.beginPath();
    pts.forEach((p, i) => { const x = X(p.vol), y = Y(p.ret); if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); });
    ctx.stroke();
    ctx.restore();
  };

  switch (kind) {
    case "frontier":
      lineWith(d.frontier, accent, 5);
      break;
    case "cvar":
      lineWith(d.cvar_frontier, cvarColor, 5);
      break;
    case "tangency":
      if (d.tangency) ringAt(d.tangency.vol, d.tangency.ret, "#fbbf24");
      break;
    case "equal":
      if (d.anchors?.equal) ringAt(d.anchors.equal.vol, d.anchors.equal.ret, "#8b5cf6");
      break;
    case "cap":
      if (d.anchors?.cap) ringAt(d.anchors.cap.vol, d.anchors.cap.ret, "#06b6d4");
      break;
    case "current":
      if (d.anchors?.current) ringAt(d.anchors.current.vol, d.anchors.current.ret, "#f59e0b");
      break;
  }

  // Keep the animation running while the pulse hasn't expired.
  if (MPT.pulseRaf) cancelAnimationFrame(MPT.pulseRaf);
  MPT.pulseRaf = requestAnimationFrame(() => { MPT.pulseRaf = 0; drawOverlay(); });
}

// Trigger a pulse on the named legend entry. Called by the legend click
// handler (and by chart clicks if we want visual reinforcement).
function mptPulse(kind) {
  MPT.pulseKind = kind;
  MPT.pulseUntil = performance.now() + 700;
  drawOverlay();
}

// Public name preserved so existing slider/click handlers keep working.
function mptUpdateSelection() { drawOverlay(); }

// Floating tooltip anchored to the cursor while hovering the chart. Shows
// (vol, ret, sharpe) of the nearest frontier point plus its top-3 weights.
// Positioning routes through the unified placeTip() so it never clips off
// the viewport.
function mptShowFrontierTip(ev, idx) {
  const d = MPT.result; if (!d) return;
  const p = d.frontier[idx]; if (!p) return;
  const params = mptGetParams();
  const sharpe = p.vol > 1e-9 ? (p.ret - params.rf) / p.vol : NaN;
  const top = Object.entries(p.weights || {})
    .filter(([_, w]) => w > 1e-4)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);
  let tip = document.getElementById("pf-mpt-frontier-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "pf-mpt-frontier-tip";
    tip.className = "pf-mpt-frontier-tip";
    document.body.appendChild(tip);
  }
  tip.innerHTML = `
    <div class="tip-title">Frontier point #${idx + 1} / ${d.frontier.length} <span style="font-weight:400;color:var(--muted);font-size:10px">· click to select</span></div>
    <div class="tip-row"><span class="k">Return</span><span class="v">${(p.ret * 100).toFixed(2)}%</span></div>
    <div class="tip-row"><span class="k">Vol</span><span class="v">${(p.vol * 100).toFixed(2)}%</span></div>
    <div class="tip-row"><span class="k">Sharpe</span><span class="v">${isFinite(sharpe) ? sharpe.toFixed(3) : "—"}</span></div>
    ${top.length ? `<div class="tip-sub">Top weights</div>` + top.map(([s, w]) =>
      `<div class="tip-row"><span class="k">${escapeHtml(s)}</span><span class="v">${(w * 100).toFixed(1)}%</span></div>`
    ).join("") : ""}
  `;
  tip.classList.add("show");
  // Anchor on the cursor and let placeTip pick a side / clamp to viewport.
  if (typeof placeTip === "function") {
    placeTip(tip, {left: ev.clientX, top: ev.clientY, right: ev.clientX, bottom: ev.clientY,
                   width: 0, height: 0}, {preferred: "above", offset: 14, gap: 8});
  } else {
    tip.style.left = (ev.clientX + 14) + "px";
    tip.style.top = (ev.clientY + 14) + "px";
  }
}
function mptHideFrontierTip() {
  const tip = document.getElementById("pf-mpt-frontier-tip");
  if (tip) tip.classList.remove("show");
}

function star(cx, cy, R, r, n) {
  // Render a star polygon centered at (cx, cy)
  const pts = [];
  for (let i = 0; i < 2 * n; i++) {
    const ang = -Math.PI / 2 + i * Math.PI / n;
    const rad = i % 2 === 0 ? R : r;
    pts.push(`${(cx + rad * Math.cos(ang)).toFixed(1)},${(cy + rad * Math.sin(ang)).toFixed(1)}`);
  }
  return pts.join(" ");
}

// Pull the currently-selected portfolio (frontier or CVaR curve) — single
// source of truth for the sidebar, apply button, and save flow.
function mptActiveSel() {
  const d = MPT.result; if (!d) return null;
  if (MPT.activeLine === "cvar") {
    const cv = d.cvar_frontier || [];
    return cv[MPT.cvarIdx] || null;
  }
  return (d.frontier || [])[MPT.selectedIdx] || null;
}

function mptRenderSide() {
  const d = MPT.result; if (!d) return;
  const sel = mptActiveSel(); if (!sel) return;
  const params = mptGetParams();
  const sharpe = sel.vol > 1e-9 ? (sel.ret - params.rf) / sel.vol : NaN;
  const stats = document.getElementById("pf-mpt-stats");
  const isCvar = MPT.activeLine === "cvar";
  // Sidebar readouts under each slider track current selection along its curve.
  const sliderReadout = document.getElementById("pf-mpt-slider-readout");
  if (sliderReadout) {
    const fp = (d.frontier || [])[MPT.selectedIdx];
    sliderReadout.textContent = fp
      ? `${(fp.ret * 100).toFixed(1)}% / ${(fp.vol * 100).toFixed(1)}%`
      : "—";
  }
  const cvarReadout = document.getElementById("pf-mpt-cvar-readout");
  if (cvarReadout) {
    const cv = (d.cvar_frontier || [])[MPT.cvarIdx];
    cvarReadout.textContent = cv ? `α=${Math.round(cv.conf * 100)}%` : "—";
  }
  // Stats block. CVaR/VaR are returned in per-period units (matches the
  // returns frequency used for optimisation). Multiply by 100 for %.
  let extraRows = "";
  if (isCvar && sel.cvar != null) {
    const freq = d.params?.frequency || "weekly";
    const conf = Math.round((sel.conf || 0) * 100);
    extraRows = `
      <span class="k">CVaR (α=${conf}%, ${freq})</span><span class="v neg">${(sel.cvar * 100).toFixed(2)}%</span>
      <span class="k">VaR (α=${conf}%, ${freq})</span><span class="v neg">${(sel.var * 100).toFixed(2)}%</span>`;
  }
  stats.innerHTML = `
    <span class="k">Source</span><span class="v">${isCvar ? "Min-CVaR" : "Efficient frontier"}</span>
    <span class="k">Annualised return</span><span class="v ${sel.ret >= 0 ? "pos" : "neg"}">${(sel.ret * 100).toFixed(2)}%</span>
    <span class="k">Annualised vol</span><span class="v">${(sel.vol * 100).toFixed(2)}%</span>
    <span class="k">Sharpe (rf ${(params.rf*100).toFixed(2)}%)</span><span class="v ${sharpe >= 0 ? "pos" : "neg"}">${isFinite(sharpe) ? sharpe.toFixed(3) : "—"}</span>
    ${extraRows}
    <span class="k">Active assets</span><span class="v">${(d.symbols || []).length}${(d.missing || []).length ? ` <span style="color:var(--muted);font-weight:400">(${(d.missing||[]).length} dropped)</span>` : ""}</span>
  `;
  // Weights bars (sorted descending; zero-weight rows hidden for clarity)
  const wlist = document.getElementById("pf-mpt-wlist");
  const ws = Object.entries(sel.weights || {})
    .filter(([_, w]) => w > 1e-4)
    .sort((a, b) => b[1] - a[1]);
  if (!ws.length) {
    wlist.innerHTML = `<span class="pf-mpt-status">No weights at this point.</span>`;
  } else {
    const maxW = ws[0][1];
    wlist.innerHTML = ws.map(([sym, w]) => `
      <div class="pf-mpt-wrow">
        <span title="${escapeHtml(sym)}">${escapeHtml(sym)}</span>
        <div class="pf-mpt-track"><div class="pf-mpt-fill" style="width:${(w/maxW*100).toFixed(1)}%"></div></div>
        <span class="pf-mpt-val">${(w * 100).toFixed(2)}%</span>
      </div>
    `).join("");
  }
  // Slider-row highlighting follows activeLine.
  document.querySelectorAll(".pf-mpt-slider-row").forEach(r => {
    r.classList.toggle("active-line", r.dataset.line === MPT.activeLine);
  });
}

function mptApplyToPortfolio() {
  const d = MPT.result; if (!d) return;
  const sel = mptActiveSel(); if (!sel) return;
  const w = sel.weights || {};
  if (!Object.keys(w).length) { toast("No weights at this point."); return; }
  // Invalidate any stale custom-mode analytics cache before switching.
  const tabMap = currentAnalyticsMap();
  for (const k of Object.keys(tabMap)) {
    if (k.startsWith("custom|")) delete tabMap[k];
  }
  STATE.customWeights = {...w};
  STATE.mode = "custom";
  closeMptOverlay();
  renderModeBar();
  persistActivePreset();
  requestAnalytics({force: true});
  if (MPT.activeLine === "cvar") {
    const conf = Math.round((sel.conf || 0) * 100);
    toast(`Applied min-CVaR α=${conf}% — ${(sel.ret * 100).toFixed(1)}% ret, ${(sel.vol * 100).toFixed(1)}% vol.`);
  } else {
    const pt = MPT.selectedIdx + 1;
    const total = (d.frontier || []).length;
    toast(`Applied MPT point ${pt}/${total} — ${(sel.ret * 100).toFixed(1)}% ret, ${(sel.vol * 100).toFixed(1)}% vol.`);
  }
}

function mptSaveAsPreset() {
  const d = MPT.result; if (!d) return;
  const sel = mptActiveSel(); if (!sel) return;
  if (!STATE.activeView || STATE.activeView === AD_HOC_KEY) {
    return toast("Save the portfolio first to keep custom weights.");
  }
  const existing = (STATE.weightPresets || []).map(p => p.name);
  const suggested = MPT.activeLine === "cvar"
    ? `CVaR α=${Math.round((sel.conf || 0) * 100)}% ${d.params.lookback} ${d.params.frequency}`
    : `MPT ${d.params.lookback} ${d.params.frequency}`;
  showInlinePrompt({
    title: "Save as Custom Weights",
    description:
      "Saves the currently selected portfolio (the point on the efficient frontier under the slider) as a named Custom-Weights configuration under this portfolio. " +
      "Switch between custom configurations from the mode bar to compare strategies side-by-side against Equal-weight and Cap-weight.",
    initial: suggested,
    placeholder: "e.g. Tangency 3Y Weekly",
    validate(name) {
      if (!name) return "Name required.";
      if (existing.includes(name)) return `"${name}" already exists.`;
      return null;
    },
    onOk: async (name) => {
      try {
        await savePresetServer(name, sel.weights, {setActive: true});
        STATE.mode = modeId(name);
        STATE.customWeights = null;
        closeMptOverlay();
        renderModeBar();
        persistActivePreset();
        requestAnalytics({force: true});
        toast(`Custom weights "${name}" saved.`);
      } catch (e) { toast("Save failed: " + (e.message || e)); }
    },
  });
}

async function mptSaveRun({silent} = {silent: false}) {
  const d = MPT.result; if (!d) return;
  if (!STATE.activeView || STATE.activeView === AD_HOC_KEY) {
    if (!silent) toast("Save the portfolio first.");
    return;
  }
  try {
    // Strip the (heavy) cloud before persisting; it can be re-sampled cheaply.
    const compact = {
      params: d.params, symbols: d.symbols, missing: d.missing,
      frontier: d.frontier, cvar_frontier: d.cvar_frontier || [],
      tangency: d.tangency,
      min_vol: d.min_vol, max_ret: d.max_ret, anchors: d.anchors,
      meta: d.meta,
    };
    const r = await fetch("/api/mpt-runs", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({view: STATE.activeView, run: compact}),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "save failed");
    await mptLoadRuns();
    if (!silent) toast("Run saved.");
  } catch (e) { if (!silent) toast("Save failed: " + (e.message || e)); }
}

async function mptLoadRuns() {
  const host = document.getElementById("pf-mpt-runs");
  if (!STATE.activeView || STATE.activeView === AD_HOC_KEY) {
    host.innerHTML = `<span class="pf-mpt-status">Saved runs are per-portfolio.</span>`;
    MPT.runs = []; return;
  }
  try {
    const r = await fetch(`/api/mpt-runs?view=${encodeURIComponent(STATE.activeView)}`);
    const j = await r.json();
    MPT.runs = j.runs || [];
  } catch (_) { MPT.runs = []; }
  if (!MPT.runs.length) {
    host.innerHTML = `<span class="pf-mpt-status">No saved runs yet.</span>`;
    return;
  }
  host.innerHTML = MPT.runs.map(r => {
    const p = r.params || {};
    const t = r.tangency || {};
    return `<div class="pf-mpt-run-row" data-id="${escapeHtml(r.id)}">
      <div style="flex:1">
        <div><b>${escapeHtml(p.lookback || "")} ${escapeHtml(p.frequency || "")}</b> · ${(p.display_ccy || "USD")}</div>
        <div class="meta">${r.saved_at ? escapeHtml(fmtDateMDY(r.saved_at) + " " + String(r.saved_at).slice(11, 16)) : ""} · tangent Sharpe ${t.sharpe != null ? Number(t.sharpe).toFixed(2) : "—"}</div>
      </div>
      <button class="del" title="Delete">✕</button>
    </div>`;
  }).join("");
  host.querySelectorAll(".pf-mpt-run-row").forEach(row => {
    const id = row.dataset.id;
    row.addEventListener("click", (e) => {
      if (e.target.classList.contains("del")) return;
      mptLoadRun(id);
    });
    row.querySelector(".del").addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await fetch(`/api/mpt-runs/${encodeURIComponent(id)}?view=${encodeURIComponent(STATE.activeView)}`, {method: "DELETE"});
        mptLoadRuns();
      } catch (_) {}
    });
  });
}

async function mptLoadRun(id) {
  try {
    const r = await fetch(`/api/mpt-runs/${encodeURIComponent(id)}?view=${encodeURIComponent(STATE.activeView)}`);
    const j = await r.json();
    if (!r.ok || !j.run) throw new Error(j.error || "not found");
    // Saved runs are persisted without the cloud — show an empty cloud so
    // the chart still renders.
    MPT.result = {...j.run, cloud: j.run.cloud || []};
    // Reflect run params in controls
    if (j.run.params) {
      const p = j.run.params;
      document.querySelectorAll("#pf-mpt-lookback button").forEach(b => b.classList.toggle("active", b.dataset.v === p.lookback));
      document.querySelectorAll("#pf-mpt-freq button").forEach(b => b.classList.toggle("active", b.dataset.v === p.frequency));
      if (p.rf != null) document.getElementById("pf-mpt-rf").value = (p.rf * 100).toFixed(2);
      if (p.budget) mptSetBudget(p.budget);
      const savedMode = p.diversified ? "diversified" : "sparse";
      document.querySelectorAll("#pf-mpt-mode button").forEach(b => b.classList.toggle("active", b.dataset.v === savedMode));
    }
    MPT.selectedIdx = Math.floor((MPT.result.frontier || []).length / 2);
    document.getElementById("pf-mpt-slider").max = String(Math.max(0, (MPT.result.frontier || []).length - 1));
    document.getElementById("pf-mpt-slider").value = String(MPT.selectedIdx);
    document.getElementById("pf-mpt-slider").disabled = false;
    // CVaR slider — pre-existing saved runs (before this feature) won't have
    // cvar_frontier, so disable the slider gracefully in that case.
    const cv = MPT.result.cvar_frontier || [];
    const cvarSlider = document.getElementById("pf-mpt-cvar-slider");
    if (cv.length) {
      MPT.cvarIdx = Math.floor(cv.length / 2);
      cvarSlider.max = String(Math.max(0, cv.length - 1));
      cvarSlider.value = String(MPT.cvarIdx);
      cvarSlider.disabled = false;
    } else {
      MPT.cvarIdx = 0;
      cvarSlider.value = "0";
      cvarSlider.disabled = true;
    }
    MPT.activeLine = "frontier";
    mptRender();
  } catch (e) {
    toast("Load failed: " + (e.message || e));
  }
}

/* --- MPT overlay wiring --- */
$("#optimize").addEventListener("click", openMptOverlay);
$("#pf-mpt-close").addEventListener("click", closeMptOverlay);
$("#pf-mpt-bg").addEventListener("click", (e) => { if (e.target.id === "pf-mpt-bg") closeMptOverlay(); });
$("#pf-mpt-run").addEventListener("click", mptRun);
$("#pf-mpt-apply").addEventListener("click", mptApplyToPortfolio);
$("#pf-mpt-save").addEventListener("click", mptSaveAsPreset);
$("#pf-mpt-info")?.addEventListener("click", openMptInfo);
$("#pf-mpt-info-close")?.addEventListener("click", closeMptInfo);
$("#pf-mpt-info-bg")?.addEventListener("click", (e) => { if (e.target.id === "pf-mpt-info-bg") closeMptInfo(); });

// Slider — coalesce rapid input events through requestAnimationFrame so we
// repaint the dynamic marker + sidebar at display rate, never more. The
// static cloud/frontier layers are NOT touched here, so this stays fast
// even with 1M cloud points.
let _mptSliderFrame = 0;
$("#pf-mpt-slider").addEventListener("input", (e) => {
  MPT.selectedIdx = Number(e.target.value) || 0;
  MPT.activeLine = "frontier";
  MPT.hoverIdx = null;
  if (_mptSliderFrame) return;
  _mptSliderFrame = requestAnimationFrame(() => {
    _mptSliderFrame = 0;
    mptUpdateSelection();
    mptRenderSide();
  });
});
let _mptCvarSliderFrame = 0;
$("#pf-mpt-cvar-slider").addEventListener("input", (e) => {
  MPT.cvarIdx = Number(e.target.value) || 0;
  MPT.activeLine = "cvar";
  MPT.hoverIdx = null;
  if (_mptCvarSliderFrame) return;
  _mptCvarSliderFrame = requestAnimationFrame(() => {
    _mptCvarSliderFrame = 0;
    mptUpdateSelection();
    mptRenderSide();
  });
});

// Segmented-control click handlers (lookback + frequency)
document.querySelectorAll("#pf-mpt-lookback button").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#pf-mpt-lookback button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
  });
});
document.querySelectorAll("#pf-mpt-freq button").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#pf-mpt-freq button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
  });
});
document.querySelectorAll("#pf-mpt-mode button").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#pf-mpt-mode button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
  });
});

// --- Custom Compute Budget dropdown ---
(function wireMptBudget() {
  const root = document.getElementById("pf-mpt-budget");
  if (!root) return;
  const menu = root.querySelector(".pf-mpt-select-menu");
  const open = () => { menu.hidden = false; root.classList.add("open"); root.setAttribute("aria-expanded", "true"); };
  const close = () => { menu.hidden = true; root.classList.remove("open"); root.setAttribute("aria-expanded", "false"); };
  root.addEventListener("click", (e) => {
    if (e.target.tagName === "LI") {
      mptSetBudget(e.target.dataset.value);
      close();
      return;
    }
    menu.hidden ? open() : close();
  });
  root.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); menu.hidden ? open() : close(); }
    else if (e.key === "Escape") { close(); }
  });
  document.addEventListener("click", (e) => {
    if (!root.contains(e.target)) close();
  });
})();

// --- Risk-free "Auto" pill + inline sparkline ---
// Holds the most-recent fetched series so the hover crosshair can re-derive
// per-pixel data without refetching.
const _RF_SPARK = { series: null, meta: null, lookback: null };
async function mptFetchRfAuto({silent} = {silent: false}) {
  const btn = document.getElementById("pf-mpt-rf-auto");
  const input = document.getElementById("pf-mpt-rf");
  const spark = document.getElementById("pf-mpt-rf-spark");
  if (!btn || !input || !spark) return;
  const lookback = document.querySelector("#pf-mpt-lookback .active")?.dataset.v || "3Y";
  btn.classList.add("busy");
  try {
    const r = await fetch(`/api/risk-free-history?ccy=${encodeURIComponent(FX_QUOTE)}&lookback=${encodeURIComponent(lookback)}`);
    const j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error || ("HTTP " + r.status));
    if (j.mean_pct != null && isFinite(j.mean_pct)) {
      input.value = Number(j.mean_pct).toFixed(2);
    }
    const series = (j.series || []).filter(p => isFinite(p[1]));
    if (series.length >= 2) {
      _RF_SPARK.series = series; _RF_SPARK.meta = j; _RF_SPARK.lookback = lookback;
      mptDrawRfSpark();
      // Tooltip content is rendered live by the hover handler from _RF_SPARK.
    } else {
      _RF_SPARK.series = null; _RF_SPARK.meta = null;
      spark.classList.remove("show");
      spark.innerHTML = "";
    }
    if (!silent && j.mean_pct == null) toast("No risk-free data available for " + FX_QUOTE + ".");
  } catch (e) {
    if (!silent) toast("Risk-free fetch failed: " + (e.message || e));
  } finally {
    btn.classList.remove("busy");
  }
}

// Draws the polished sparkline: filled area + line + dashed mean baseline +
// latest-value dot. The numeric label lives in the input + hover tooltip.
function mptDrawRfSpark() {
  const spark = document.getElementById("pf-mpt-rf-spark");
  if (!spark || !_RF_SPARK.series) return;
  const series = _RF_SPARK.series;
  const meta = _RF_SPARK.meta || {};
  const vals = series.map(p => p[1]);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = Math.max(1e-9, hi - lo);
  // viewBox = 200 x 36, with 3px top/bottom padding and a tiny right margin
  // for the latest-value dot — no in-SVG percent label anymore.
  const W = 200, H = 36, padY = 3, padR = 4;
  const usableW = W - padR - 2;
  const usableH = H - 2 * padY;
  const xs = series.map((_, i) => 2 + (i / (series.length - 1)) * usableW);
  const ys = series.map(p => H - padY - ((p[1] - lo) / span) * usableH);
  const linePts = xs.map((x, i) => `${x.toFixed(2)},${ys[i].toFixed(2)}`).join(" ");
  const areaPts = `${xs[0].toFixed(2)},${(H - padY).toFixed(2)} ${linePts} ${xs[xs.length-1].toFixed(2)},${(H - padY).toFixed(2)}`;
  const mean = meta.mean_pct != null ? meta.mean_pct : (vals.reduce((a, b) => a + b, 0) / vals.length);
  const meanY = H - padY - ((mean - lo) / span) * usableH;
  const lastX = xs[xs.length - 1], lastY = ys[ys.length - 1];
  spark.innerHTML = `
    <polygon class="rfs-area" points="${areaPts}"/>
    <line class="rfs-base" x1="2" y1="${meanY.toFixed(2)}" x2="${(W - padR).toFixed(2)}" y2="${meanY.toFixed(2)}"/>
    <polyline class="rfs-line" points="${linePts}"/>
    <line class="rfs-cross" id="rfs-cross-line" x1="0" y1="${padY}" x2="0" y2="${H - padY}"/>
    <circle class="rfs-dot" cx="${lastX.toFixed(2)}" cy="${lastY.toFixed(2)}" r="2.4"/>
  `;
  spark.classList.add("show");
}

function _rfLookbackLabel(lb) {
  if (!lb) return "lookback";
  const m = String(lb).match(/^(\d+)\s*Y/i);
  return m ? `${m[1]} Y` : String(lb);
}

// Hover crosshair + rich card. The card matches the data-tip aesthetic but
// supports multi-line content and follows the cursor.
(function wireRfSparkHover() {
  const wrap = document.getElementById("pf-mpt-rf-spark-wrap");
  const spark = document.getElementById("pf-mpt-rf-spark");
  const tip = document.getElementById("pf-mpt-rf-spark-tip");
  if (!wrap || !spark || !tip) return;
  wrap.addEventListener("mousemove", (e) => {
    if (!_RF_SPARK.series || _RF_SPARK.series.length < 2) return;
    const series = _RF_SPARK.series;
    const meta = _RF_SPARK.meta || {};
    const rect = wrap.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const frac = Math.max(0, Math.min(1, px / rect.width));
    const idx = Math.round(frac * (series.length - 1));
    const pt = series[idx];
    const date = (pt[0] || "").slice(0, 10);
    const val = Number(pt[1]);
    const vals = series.map(p => p[1]);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const mean = meta.mean_pct != null ? meta.mean_pct : (vals.reduce((a, b) => a + b, 0) / vals.length);
    const cur = meta.current_pct != null ? meta.current_pct : vals[vals.length - 1];
    const ticker = meta.ticker || "rf";
    const lbLabel = _rfLookbackLabel(_RF_SPARK.lookback);
    const note = meta.source_note ? `<div class="tip-note">${escapeHtml(meta.source_note)}</div>` : "";
    tip.innerHTML = `
      <div class="tip-title">${escapeHtml(ticker)} · risk-free proxy</div>
      <div class="tip-sub">Annualized yield used as the Sharpe / tangency baseline.</div>
      <div class="tip-row"><span class="k">Hover ${escapeHtml(date)}</span><span class="v">${val.toFixed(2)}%</span></div>
      <div class="tip-row"><span class="k">Current</span><span class="v">${cur != null ? cur.toFixed(2) + "%" : "—"}</span></div>
      <div class="tip-row"><span class="k">Mean (${escapeHtml(lbLabel)})</span><span class="v">${mean != null ? mean.toFixed(2) + "%" : "—"}</span></div>
      <div class="tip-row"><span class="k">Range</span><span class="v">${lo.toFixed(2)} – ${hi.toFixed(2)}%</span></div>
      ${note}
    `;
    tip.style.left = px + "px";
    tip.style.top = wrap.clientHeight + "px";
    tip.classList.add("show");
    // Move crosshair on the SVG (viewBox coords)
    const line = document.getElementById("rfs-cross-line");
    if (line) {
      const W = 200, padR = 4;
      const x = 2 + frac * (W - padR - 2);
      line.setAttribute("x1", x.toFixed(2));
      line.setAttribute("x2", x.toFixed(2));
      line.classList.add("show");
    }
  });
  wrap.addEventListener("mouseleave", () => {
    tip.classList.remove("show");
    const line = document.getElementById("rfs-cross-line");
    if (line) line.classList.remove("show");
  });
})();

document.getElementById("pf-mpt-rf-auto")?.addEventListener("click", () => mptFetchRfAuto({silent: false}));

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  // About-MPT modal takes precedence (it sits on top of the MPT overlay)
  const infoBg = document.getElementById("pf-mpt-info-bg");
  if (infoBg && infoBg.classList.contains("show")) { closeMptInfo(); return; }
  if (document.getElementById("pf-mpt-bg").classList.contains("show")) closeMptOverlay();
});
// Re-draw chart on window resize while overlay is open.
window.addEventListener("resize", () => {
  if (document.getElementById("pf-mpt-bg").classList.contains("show") && MPT.result) mptRender();
});

/* ===========================================================================
 * FX selector wiring (topbar dropdown + hover currency-index chart)
 * --------------------------------------------------------------------------- */
function fxLabelFor(ccy) {
  const sym = FX_SYMBOL[ccy];
  return sym ? `${ccy} ${sym}` : ccy;
}
function fxRenderDropdown() {
  const dd = $("#fx-dropdown");
  if (!dd) return;
  dd.innerHTML = FX_SUPPORTED.map(ccy => `
    <div class="fx-opt ${ccy === FX_QUOTE ? 'selected' : ''}" data-ccy="${ccy}" role="option">
      <span class="fx-opt-code">${ccy} ${FX_SYMBOL[ccy] || ''}</span>
      <span class="fx-opt-name">${FX_NAMES[ccy] || ''}</span>
    </div>
  `).join("");
}
function fxUpdateButton() {
  const lbl = $("#fx-btn-label");
  if (lbl) lbl.textContent = fxLabelFor(FX_QUOTE);
}
function fxOpen() {
  fxRenderDropdown();
  $("#fx-dropdown").classList.add("show");
  $("#fx-btn").classList.add("open");
}
function fxClose() {
  $("#fx-dropdown").classList.remove("show");
  $("#fx-btn").classList.remove("open");
  fxHoverHide();
}
function fxToggle() {
  $("#fx-dropdown").classList.contains("show") ? fxClose() : fxOpen();
}
function fxSelect(ccy) {
  if (!ccy || FX_SUPPORTED.indexOf(ccy) < 0) return;
  const prev = FX_QUOTE;
  FX_QUOTE = ccy;
  try { localStorage.setItem("fx_quote", ccy); } catch (e) {}
  fxUpdateButton();
  fxRenderDropdown();
  if (DATA && DATA.length) {
    if (prev !== ccy) {
      // Invalidate every tab's analytics — returns must be recomputed in the new currency.
      STATE.analyticsByTab = {};
      STATE.analytics = null;
    }
    render();
    if (prev !== ccy && DATA.length) requestAnalytics();
  }
  if (DETAIL && DETAIL.data) {
    const det = $("#modal");
    if (det && det.classList && document.getElementById("modal-bg").classList.contains("show")) {
      try { renderSections(); } catch (e) {}
    }
  }
}

/* --- hover currency-index chart ---
 *
 * Cache discipline: ONLY cache non-empty results. An empty array means the
 * upstream call failed (likely yfinance rate-limit on the basket pairs).
 * Caching `[]` made the "no data" message stick on every subsequent hover
 * even after the rate-limit window passed — so we now keep failures
 * uncached and retry on the next hover. Inflight-dedup still prevents
 * burst-fetching when the user wiggles the cursor. */
async function fxFetchIndex(ccy) {
  if (FX_INDEX_CACHE[ccy] && FX_INDEX_CACHE[ccy].length >= 2) {
    return FX_INDEX_CACHE[ccy];
  }
  if (FX_INDEX_INFLIGHT[ccy]) return FX_INDEX_INFLIGHT[ccy];
  const p = (async () => {
    try {
      let pts = [];
      for (let attempt = 0; attempt < 2; attempt++) {
        const r = await fetch(`/api/fx-index?ccy=${encodeURIComponent(ccy)}`);
        if (!r.ok) continue;
        const j = await r.json();
        pts = Array.isArray(j.index) ? j.index : [];
        if (pts.length >= 2) break;
      }
      // Cache only real data; transient failures (empty) stay uncached so
      // the next hover re-attempts the fetch.
      if (pts.length >= 2) FX_INDEX_CACHE[ccy] = pts;
      return pts;
    } catch (e) { return []; }
    finally { delete FX_INDEX_INFLIGHT[ccy]; }
  })();
  FX_INDEX_INFLIGHT[ccy] = p;
  return p;
}
function fxDrawHoverChart(pts) {
  const svg = $("#fx-hover-svg");
  if (!svg) return;
  if (!pts || pts.length < 2) {
    svg.innerHTML = `<text x="110" y="44" text-anchor="middle" fill="var(--muted)" font-size="11">no data</text>`;
    return;
  }
  const w = 220, h = 80, padL = 4, padR = 4, padT = 6, padB = 12;
  const xs = pts.map(p => p[0]);
  const ys = pts.map(p => p[1]);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const ymin = Math.min(...ys), ymax = Math.max(...ys);
  const xrng = (xmax - xmin) || 1;
  const yrng = (ymax - ymin) || 1;
  const sx = t => padL + ((t - xmin) / xrng) * (w - padL - padR);
  const sy = v => padT + (1 - (v - ymin) / yrng) * (h - padT - padB);
  let d = "";
  for (let i = 0; i < pts.length; i++) {
    const x = sx(pts[i][0]).toFixed(1);
    const y = sy(pts[i][1]).toFixed(1);
    d += (i === 0 ? "M" : "L") + x + "," + y + " ";
  }
  const last = ys[ys.length - 1];
  const first = ys[0];
  const up = last >= first;
  const stroke = up
    ? getComputedStyle(document.documentElement).getPropertyValue("--pos").trim() || "#1f883d"
    : getComputedStyle(document.documentElement).getPropertyValue("--neg").trim() || "#cf222e";
  const baseY = sy(100).toFixed(1);
  svg.innerHTML = `
    <line x1="${padL}" x2="${w-padR}" y1="${baseY}" y2="${baseY}" stroke="var(--border)" stroke-dasharray="2 3" stroke-width="1"/>
    <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.4"/>
  `;
}
async function fxHoverShow(ccy, anchorEl) {
  const box = $("#fx-hover");
  if (!box) return;
  if (FX_HOVER_CCY === ccy && box.classList.contains("show")) return;
  const reqId = ++FX_HOVER_REQ_ID;
  FX_HOVER_CCY = ccy;
  $("#fx-hover-title").textContent = `${ccy} basket index (1Y)`;
  $("#fx-hover-foot").innerHTML = lcHtml("fetching index", {bar: true});
  const svg = $("#fx-hover-svg");
  if (svg) svg.innerHTML = "";
  box.classList.add("show");
  // position next to dropdown (anchored to the right side of the topbar);
  // CSS already places it with right:180px,top:36px.
  const pts = await fxFetchIndex(ccy);
  if (!box.classList.contains("show") || reqId !== FX_HOVER_REQ_ID || FX_HOVER_CCY !== ccy) return;
  fxDrawHoverChart(pts);
  if (pts && pts.length >= 2) {
    const ret = (pts[pts.length-1][1] / pts[0][1] - 1) * 100;
    const cls = ret >= 0 ? "pos" : "neg";
    const sign = ret >= 0 ? "+" : "";
    $("#fx-hover-foot").innerHTML = `vs 6-major basket · 1Y <span class="${cls}">${sign}${ret.toFixed(2)}%</span>`;
  } else {
    $("#fx-hover-foot").textContent = "no data";
  }
}
function fxHoverHide() {
  const box = $("#fx-hover");
  FX_HOVER_REQ_ID += 1;
  FX_HOVER_CCY = null;
  if (box) box.classList.remove("show");
}

function fxInit() {
  fxLoadPref();
  fxUpdateButton();
  fxRenderDropdown();
  fxLoadRates().then(() => { if (DATA && DATA.length) render(); });
  const btn = $("#fx-btn");
  if (btn) btn.addEventListener("click", (e) => { e.stopPropagation(); fxToggle(); });
  const dd = $("#fx-dropdown");
  if (dd) {
    dd.addEventListener("click", (e) => {
      const opt = e.target.closest(".fx-opt");
      if (!opt) return;
      fxSelect(opt.dataset.ccy);
      fxClose();
    });
    dd.addEventListener("mouseover", (e) => {
      const opt = e.target.closest(".fx-opt");
      if (!opt) return;
      fxHoverShow(opt.dataset.ccy, opt);
    });
    dd.addEventListener("mouseleave", fxHoverHide);
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#fx-menu")) fxClose();
  });
}

// Loaded once at startup, then read by renderAnalystDashboard()'s coverage-foot
// row (the version tag lives there now — see .an-version — instead of the old
// fixed-position overlay). Any `.an-version` element already in the DOM when
// this resolves is patched directly so a slow /api/health response still lands.
let APP_VERSION = null; // {version, date} — date is a raw ISO "yyyy-mm-dd"

function versionLabel() {
  return APP_VERSION ? `v${APP_VERSION.version} (${fmtDateDMY(APP_VERSION.date)})` : "";
}

function loadAppVersion() {
  fetch("/api/health").then(r => r.json()).then(d => {
    if (!d || !d.version) return;
    APP_VERSION = { version: d.version, date: d.version_date };
    document.querySelectorAll(".an-version").forEach(el => { el.textContent = versionLabel(); });
  }).catch(() => {});
}

setTheme(readTheme());
fxInit();
renderHeader();
loadAllAtStartup();
loadAppVersion();
