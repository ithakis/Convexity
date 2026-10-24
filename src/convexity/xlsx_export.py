"""Excel export: every saved portfolio (and My Investments' book) in one
workbook that a newcomer can read and a quant, or a model, can use as a
reference.

Sheets, in order:

  • Read me: what each sheet is, one plain sentence per portfolio, how to
    read the numbers (periods, currency, units, colours).
  • Overview: every portfolio side by side, 1Y and 5Y.
  • Positions / Ledger: My Investments, when there is a book.
  • One sheet per portfolio, story first: headline, Performance (each
    period next to SPY and QQQ, with what each metric means), Valuation,
    Analysts, Where the money is, Concentration, What drove the return,
    Notes (stand-in listings, missing history), then the Holdings table
    (an Excel table: every row field, both news reads, the analyst block).
  • Data: every portfolio figure as one long table with its unit.
  • Definitions: formula, meaning and typical range of every column and
    metric; the same text is on each header as a hover comment, and
    mirrors the app's hover tips (COL_INFO / METRIC_INFO in static/app.js).

Speed: the server passes ``analytics.prefetch_closes``, which downloads
every portfolio's prices once before the per-portfolio runs (they then read
the cache), and the analyst fields come from the rows, never a per-symbol
Yahoo call (10 portfolios: ~6 minutes cold before, 2026-10-13).

Unknown row keys go into the holdings table automatically (the "extras"
pass). A new column also needs a COLUMN_DEFS entry (and a readable
EXTRA_LABELS header if it is an extra): tests/test_xlsx_definitions.py fails
without one.
"""

import io
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

# `_maybe_num` is helpers._safe_num (finite float or None), named for the
# many call sites below.
from convexity.analytics import _market_cap_usd
from convexity.helpers import _safe_num as _maybe_num


# --------------------------------------------------------------------------
# Holdings table — primary columns in display order. Anything else found on
# the row payload gets appended as an "extras" column so additions to
# fetch_one show up automatically.
# --------------------------------------------------------------------------

HOLDINGS_PRIMARY_COLS: list[tuple[str, str]] = [
    # (row_key, header_label)
    ("symbol", "Ticker"),
    ("name", "Company"),
    ("weight", "Weight"),
    ("exchange", "Exchange"),
    ("currency", "Currency"),
    ("price", "Price"),
    ("change_abs_1d", "Change (quote ccy)"),
    ("pct_1d", "% 1D"),
    ("pct_2d", "% 2D"),
    ("pct_1w", "% 1W"),
    ("pct_1m", "% 1M"),
    ("pct_3m", "% 3M"),
    ("pct_6m", "% 6M"),
    ("pct_ytd", "% YTD"),
    ("pct_1y", "% 1Y"),
    ("market_cap", "Market Cap (listing ccy)"),
    ("ps_ratio", "P/S"),
    ("pe_ratio", "P/E"),
    ("beta", "Beta"),
    ("beta_vs", "Beta Measured Against"),
    ("model_mu", "Expected Return μ (12M)"),
    ("model_price", "Model Price (12M)"),
    ("sector", "Sector"),
    ("industry", "Industry"),
    ("w52_high", "52W High"),
    ("w52_low", "52W Low"),
    ("ath", "2Y High"),
    ("delta_ath", "Δ 2Y High (%)"),
    ("above_sma_20", "Above 20SMA"),
    ("above_sma_50", "Above 50SMA"),
    ("above_sma_200", "Above 200SMA"),
    ("above_1m", "Above 1M"),
    ("volume", "Volume"),
    ("quote_type", "Type"),
    ("website", "Website"),
]

# Analyst columns appended to the right of the holdings table.
ANALYST_COLS: list[tuple[str, str]] = [
    ("recommendation_key", "Analyst Rating"),
    ("recommendation_mean", "Rec. Mean (1=Strong Buy, 5=Sell)"),
    ("num_analysts", "# Analysts"),
    ("target_mean", "Target Mean"),
    ("target_high", "Target High"),
    ("target_low", "Target Low"),
    ("target_median", "Target Median"),
    ("target_upside_pct", "Target Upside (%)"),
    ("forward_pe", "Forward P/E"),
    ("ev_ebitda", "EV/EBITDA"),
    ("peg", "PEG"),
    ("dividend_yield", "Dividend Yield"),
]

# Headers for the auto-discovered "extras" row fields whose unit is not
# obvious from the raw key. Fractions (0.42 = 42%) get a percent cell format.
EXTRA_LABELS: dict[str, str] = {
    "debt_equity": "D/E (%)",
    "market_cap_usd": "Market Cap (USD)",
    "financial_currency": "Statement Currency",
    "roe": "ROE",
    "roa": "ROA",
    "gross_margin": "Gross Margin",
    "operating_margin": "Operating Margin",
    "profit_margin": "Net Margin",
    "fcf_yield": "FCF Yield",
    "revenue_growth": "Revenue Growth (YoY, mrq)",
    "earnings_growth": "Earnings Growth (YoY, mrq)",
    "payout_ratio": "Payout Ratio",
    "price_book": "P/B",
    "ev_revenue": "EV/Revenue",
    "rsi_14": "RSI 14",
    "macd_hist_pct": "MACD Hist (% of price)",
    "bb_pct_b": "Bollinger %B",
    "current_ratio": "Current Ratio",
    "quick_ratio": "Quick Ratio",
    "sma_20": "SMA 20",
    "sma_50": "SMA 50",
    "sma_200": "SMA 200",
}
FRACTION_KEYS = {
    "roe",
    "roa",
    "gross_margin",
    "operating_margin",
    "profit_margin",
    "fcf_yield",
    "revenue_growth",
    "earnings_growth",
    "payout_ratio",
    "dividend_yield",
    "model_mu",
}

# Fields explicitly skipped from the holdings "extras" pass — these are
# either large arrays (charts) or already reported elsewhere in the
# analyst block, so we don't want them appearing twice.
HOLDINGS_SKIP_EXTRAS = {
    "sparkline",
    "rs_rank",
    "error",
    # Pass D — these now ride on every row (via fetch_one) but are also
    # rendered in the dedicated ANALYST_COLS block; skip the auto-extras
    # pass so they don't duplicate.
    "forward_pe",
    "ev_ebitda",
    "recommendation_mean",
    "target_mean_price",
    "target_low",
    "target_high",
    "target_median",
    # Rendered as the dedicated SENTIMENT_COLS block below instead of a
    # "[5 keys]" blob.
    "news_sentiment",
    # Also in ANALYST_COLS, which reads the same row fields: exporting them
    # again as extras gave two columns with the same numbers.
    "peg",
    "dividend_yield",
    "n_analysts",
    "rec_key",
    # Nested payloads (rating counts, EPS surprises, Finnhub insider and
    # recommendation history) that could only export as "[n keys]".
    "rating_dist",
    "earnings_surprise",
    "insider_mspr",
    "rec_trend_fh",
    # Where those came from (the company's US line Finnhub was asked for),
    # for the table's tooltips only.
    "rec_src",
    "us_line",
    # Where a secondary listing's analysts came from (fetcher._apply_research):
    # bookkeeping, like us_line; the values themselves are in the row.
    "research_symbol",
    "research_ratio",
    "research_n",
    "research_analysts",
    # The beta's provenance: its value and market are the Beta columns.
    "beta_meta",
}

# News read (LLM, five lenses) + Market read (statistical) — the two engines
# the News tab shows, flattened for the sheet. Keys are resolved from the
# row's `news_sentiment` payload by _sentiment_value().
SENTIMENT_COLS: list[tuple[str, str]] = [
    ("ns:news_tier", "News read"),
    ("ns:lens:financials", "News · Financials (−2..+2)"),
    ("ns:lens:outlook", "News · Outlook (−2..+2)"),
    ("ns:lens:competition", "News · Competition (−2..+2)"),
    ("ns:lens:regulation", "News · Regulation (−2..+2)"),
    ("ns:lens:street", "News · Street view (−2..+2)"),
    ("ns:market_tier", "Market read"),
    ("ns:market_z", "Market read z (σ)"),
    ("ns:divergence", "Divergence"),
]


def _sentiment_value(key: str, s) -> object:
    """One SENTIMENT_COLS cell from a row's two-engine sentiment dict."""
    if not isinstance(s, dict):
        return None
    news, market = s.get("news") or {}, s.get("market") or {}
    if key == "ns:news_tier":
        t = news.get("tier")
        return t.replace("_", " ") + (" (stale)" if news.get("stale") else "") if t else None
    if key.startswith("ns:lens:"):
        lens = (news.get("lenses") or {}).get(key.split(":", 2)[2])
        return lens.get("score") if isinstance(lens, dict) else None
    if key == "ns:market_tier":
        t = market.get("tier")
        return t.replace("_", " ") if t else None
    if key == "ns:market_z":
        return market.get("z")
    if key == "ns:divergence":
        return (s.get("divergence") or {}).get("text")
    return None


# Stats keys (under analytics["stats"]) in display order — these are the
# rows of the prominent "Portfolio Metrics" block.
STATS_KEYS: list[tuple[str, str]] = [
    ("total_return", "Total Return (%)"),
    ("ann_return", "Annualised Return (%)"),
    ("ann_vol", "Annualised Volatility (%)"),
    ("sharpe", "Sharpe Ratio"),
    ("sortino", "Sortino Ratio"),
    ("calmar", "Calmar Ratio"),
    ("max_dd", "Max Drawdown (%)"),
    ("beta", "Beta vs SPY"),
    ("r2", "R² vs SPY"),
    ("te", "Tracking Error vs SPY (%)"),
    ("ir", "Information Ratio vs SPY"),
    ("rf_ann", "Risk-free Rate used (% p.a.)"),
]

EXPORT_STATS_PERIODS: tuple[str, ...] = ("1Y", "5Y")

# Aggregated analyst-coverage keys (under analytics["analyst"]).
ANALYST_AGG_KEYS: list[tuple[str, str]] = [
    ("mean_rating", "Mean Analyst Rating (1=Strong Buy, 5=Sell)"),
    ("rating_coverage_weight", "Rating Coverage (weight)"),
    ("weighted_target_upside_pct", "Weighted Target Upside (%)"),
    ("target_coverage_weight", "Target Coverage (weight)"),
    ("n_analysts_total", "Total Analysts"),
    ("covered_count", "Holdings Covered"),
    ("active_count", "Active Holdings"),
]

# The portfolio's look-through valuation (analytics["weighted"], key minus "w_").
WEIGHTED_KEYS: list[tuple[str, str]] = [
    ("w_pe", "P/E"),
    ("w_ps", "P/S"),
    ("w_ev_ebitda", "EV/EBITDA"),
    ("w_div_yield", "Dividend Yield"),
    ("w_market_cap", "Average Market Cap"),
]

# Concentration keys (under analytics["concentration"]).
CONCENTRATION_KEYS: list[tuple[str, str]] = [
    ("top5", "Top-5 Concentration"),
    ("herfindahl", "Herfindahl Index"),
    ("effective_n", "Effective N (1/HHI)"),
]


# --------------------------------------------------------------------------
# Definitions: the hover comment on every header and the "Definitions" sheet.
# (formula, meaning, typical range) in plain text, because Excel cannot render
# the app's KaTeX. Keyed by column key, not label: `beta` is Yahoo's beta in
# the holdings table but the portfolio's beta vs SPY in the metrics block, so
# holdings columns and portfolio metrics have separate tables.
# --------------------------------------------------------------------------

_RET = "Price return over the window, in %, from dividend-adjusted closes ({} back)."
_LENS = "-2 very negative, 0 neutral, +2 very positive."
COLUMN_DEFS: dict[str, tuple[str, str, str]] = {
    "symbol": (
        "",
        "Yahoo Finance ticker, with the exchange suffix outside the US (e.g. SHEL.L).",
        "",
    ),
    "name": ("", "Company or fund name as Yahoo reports it.", ""),
    "weight": (
        "the portfolio's weight in this holding",
        "Share of the portfolio in this holding, as the app weights it.",
        "All weights add up to 100%.",
    ),
    "exchange": ("", "Listing exchange.", ""),
    "currency": (
        "",
        "Quote currency. GBp / ZAc / ILA are pence, cents and agorot: those prices are in the minor unit.",
        "",
    ),
    "price": ("last close", "Last close in the quote currency.", ""),
    "change_abs_1d": ("price - previous close", "One-day price change in the quote currency.", ""),
    "pct_1d": ("price / previous close - 1", "Return over the last trading session, in %.", ""),
    "pct_2d": (
        "price / close 2 sessions ago - 1",
        "Return over the last two trading sessions, in %. Trading bars, so weekends do not shorten it.",
        "",
    ),
    "pct_1w": ("price / close 7 days ago - 1", _RET.format("7 calendar days"), ""),
    "pct_1m": ("price / close 30 days ago - 1", _RET.format("30 calendar days"), ""),
    "pct_3m": ("price / close 91 days ago - 1", _RET.format("91 calendar days"), ""),
    "pct_6m": ("price / close 182 days ago - 1", _RET.format("182 calendar days"), ""),
    "pct_ytd": ("price / last close of the previous year - 1", "Year-to-date return, in %.", ""),
    "pct_1y": ("price / close 365 days ago - 1", _RET.format("365 calendar days"), ""),
    "market_cap": (
        "price x shares outstanding",
        "Equity market value in the listing's major currency (pounds, not pence).",
        "Compare across listings with Market Cap (USD).",
    ),
    "ps_ratio": (
        "market cap / revenue (TTM)",
        "Price-to-sales, both in the statements' currency.",
        "Lower is generally cheaper; compare within a sector.",
    ),
    "pe_ratio": (
        "price / EPS (TTM)",
        "Trailing price-to-earnings. Empty when earnings are negative.",
        "Lower is generally cheaper; above ~50 prices in heavy growth.",
    ),
    "sector": ("", "GICS sector reported by Yahoo.", ""),
    "industry": ("", "GICS sub-industry, narrower than sector.", ""),
    "w52_high": (
        "max close over 52 weeks",
        "Highest dividend-adjusted close over the trailing 52 weeks (Yahoo's quoted high uses intraday prices).",
        "",
    ),
    "w52_low": (
        "min close over 52 weeks",
        "Lowest dividend-adjusted close over the trailing 52 weeks.",
        "",
    ),
    "ath": (
        "max close over 2 years",
        "Highest dividend-adjusted close in the 2-year history the app downloads.",
        "",
    ),
    "delta_ath": (
        "price / 2Y high - 1",
        "Distance below the 2-year high, in %.",
        "0% = at the high.",
    ),
    "above_sma_20": (
        "price > mean of last 20 closes",
        "True when the price is above its 20-day simple moving average (about a month).",
        "Short-term trend flag.",
    ),
    "above_sma_50": (
        "price > mean of last 50 closes",
        "True when the price is above its 50-day simple moving average.",
        "Medium-term trend flag.",
    ),
    "above_sma_200": (
        "price > mean of last 200 closes",
        "True when the price is above its 200-day simple moving average.",
        "Long-term trend flag.",
    ),
    "above_1m": (
        "price > close 21 sessions ago",
        "True when the price is above where it was one month (21 trading sessions) ago.",
        "",
    ),
    "volume": (
        "shares traded",
        "Shares traded in the latest session.",
        "Compare with the usual volume; spikes often mean news.",
    ),
    "quote_type": ("", "Yahoo instrument type: EQUITY, ETF, MUTUALFUND, INDEX, ...", ""),
    "website": ("", "Company website.", ""),
    # News & Sentiment (SENTIMENT_COLS)
    "ns:news_tier": (
        "tier of the overall News read score",
        "What the news says: the LLM News read of recent headlines across five lenses. "
        "'(stale)' = assessed before the lookback window.",
        "Very bearish ... Very bullish.",
    ),
    "ns:lens:financials": (
        "mean lens score",
        "News read, Financials lens: results, guidance, margins.",
        _LENS,
    ),
    "ns:lens:outlook": (
        "mean lens score",
        "News read, Outlook lens: forward-looking business prospects.",
        _LENS,
    ),
    "ns:lens:competition": (
        "mean lens score",
        "News read, Competition lens: market share, rivals, pricing power.",
        _LENS,
    ),
    "ns:lens:regulation": (
        "mean lens score",
        "News read, Regulation lens: legal, regulatory and political risk.",
        _LENS,
    ),
    "ns:lens:street": (
        "mean lens score",
        "News read, Street view lens: analyst actions and targets in the news.",
        _LENS,
    ),
    "ns:market_tier": (
        "tier from the Market read z",
        "How prices have historically reacted to news like this (statistical model). Only the tails are called.",
        "No edge in the middle; bullish / bearish in the tails.",
    ),
    "ns:market_z": (
        "(score - mean) / sd of recent reads",
        "The Market read score as a z-score against the app's recent reads.",
        "Larger |z| = further into the tail.",
    ),
    "ns:divergence": (
        "",
        "Set when the two reads disagree, or the stock sold off on good news.",
        "",
    ),
    # Extras (EXTRA_LABELS)
    "debt_equity": (
        "total debt / shareholders' equity x 100",
        "Debt-to-equity in percent, most recent quarter. Empty when equity is negative.",
        "Below ~50% conservative; above ~200% heavily levered. Banks, utilities, REITs run higher.",
    ),
    "market_cap_usd": (
        "market cap x FX rate to USD",
        "Market cap in US dollars at spot, for comparing listings in different currencies.",
        "",
    ),
    "financial_currency": (
        "",
        "Currency of the financial statements (differs from the quote currency for ADRs).",
        "",
    ),
    "roe": (
        "net income (TTM) / shareholders' equity",
        "Return on equity.",
        "Higher is better, but leverage inflates it; read with D/E.",
    ),
    "roa": (
        "net income (TTM) / total assets",
        "Return on assets; not inflated by leverage.",
        "Above ~5% solid, 10%+ strong; banks run near 1%.",
    ),
    "gross_margin": (
        "(revenue - cost of goods) / revenue",
        "Gross margin, trailing 12 months.",
        "Higher means more pricing power.",
    ),
    "operating_margin": (
        "operating income / revenue",
        "Operating margin, trailing 12 months.",
        "Higher means more profit after core operating costs.",
    ),
    "profit_margin": ("net income / revenue", "Net margin, trailing 12 months.", ""),
    "fcf_yield": (
        "free cash flow (TTM) / market cap",
        "Free-cash-flow yield, both in the statements' currency.",
        "Higher means more cash generated per unit of price.",
    ),
    "revenue_growth": (
        "revenue(q) / revenue(q-4) - 1",
        "Year-over-year revenue growth of the most recent quarter.",
        "Mid-single digits is mature; 30%+ is high growth.",
    ),
    "earnings_growth": (
        "EPS(q) / EPS(q-4) - 1",
        "Year-over-year earnings growth of the most recent quarter.",
        "Noisy: a small base gives huge values.",
    ),
    "payout_ratio": (
        "dividends / net income",
        "Share of earnings paid out as dividends.",
        "Below ~60% is sustainable; above 100% pays out more than it earns.",
    ),
    "price_book": (
        "market cap / book equity",
        "Price-to-book.",
        "Below 1 can signal value or distress; weak for asset-light businesses.",
    ),
    "ev_revenue": (
        "(market cap + debt - cash) / revenue (TTM)",
        "Enterprise value to sales.",
        "Lower is usually cheaper on sales.",
    ),
    "rsi_14": (
        "100 - 100 / (1 + avg gain / avg loss), 14-day Wilder smoothing",
        "Relative Strength Index.",
        "Below 30 oversold, above 70 overbought.",
    ),
    "macd_hist_pct": (
        "(MACD - signal) / price; MACD = EMA12 - EMA26, signal = EMA9 of MACD",
        "MACD histogram as a percent of price.",
        "Positive = momentum above its signal line.",
    ),
    "bb_pct_b": (
        "(price - lower band) / (upper band - lower band); 20-day, 2 sd",
        "Bollinger %B.",
        "0 lower band, 0.5 middle, 1 upper; outside 0..1 = outside the bands.",
    ),
    "beta": (
        "1 + 0.9 (beta_BSWA - 1); beta_BSWA = weighted OLS slope of daily returns on the home index",
        "The app's beta: 3 years of daily returns against the listing's home index in local "
        "currency (S&P 500, DAX, FTSE All-Share, TOPIX...), slope-winsorised and age-weighted "
        "(Welch 2022), shrunk 10% toward 1. Under a year of history: 1-year OLS; under 151 days: 1.",
        "1 moves with its market; above 1 amplifies it.",
    ),
    "beta_vs": (
        "",
        "The market the beta is measured against, and how (BSWA, 1Y OLS or prior).",
        "",
    ),
    "model_mu": (
        "mu = rf_local + beta x ERP + alpha",
        "The app's expected return: CAPM in the listing's currency plus the analysts' small "
        "relative tilt (alpha = IC x 0.42 x z of relative upside, at most ~2.5%).",
        "A model, not a forecast with an edge.",
    ),
    "model_price": (
        "S0 x exp(mu x 1)",
        "The model price 12 months out: today's price grown at mu. The same number as the "
        "grid and the stock window.",
        "",
    ),
    "current_ratio": (
        "current assets / current liabilities",
        "Short-term liquidity, most recent quarter.",
        "Below 1 can signal a funding squeeze; 1.5-3 is typical.",
    ),
    "quick_ratio": (
        "(current assets - inventory) / current liabilities",
        "A stricter liquidity test than the current ratio.",
        "Above 1 covers near-term obligations without selling inventory.",
    ),
    "sma_20": (
        "mean of last 20 closes",
        "20-day simple moving average, in the quote currency.",
        "",
    ),
    "sma_50": (
        "mean of last 50 closes",
        "50-day simple moving average, in the quote currency.",
        "",
    ),
    "sma_200": (
        "mean of last 200 closes",
        "200-day simple moving average, in the quote currency.",
        "",
    ),
    # Analyst block (ANALYST_COLS)
    "recommendation_key": ("", "Consensus analyst recommendation (strong_buy ... sell).", ""),
    "recommendation_mean": (
        "mean of analyst ratings",
        "Mean sell-side rating on Yahoo's 1-5 scale.",
        "1 Strong Buy, 2 Buy, 3 Hold, 4 Underperform, 5 Sell.",
    ),
    "num_analysts": (
        "",
        "Number of analysts with an opinion.",
        "More analysts = a better-sampled consensus.",
    ),
    "target_mean": (
        "mean of 12-month price targets",
        "Mean analyst price target, in the quote currency.",
        "",
    ),
    "target_high": ("max of 12-month price targets", "Highest analyst price target.", ""),
    "target_low": ("min of 12-month price targets", "Lowest analyst price target.", ""),
    "target_median": ("median of 12-month price targets", "Median analyst price target.", ""),
    "target_upside_pct": (
        "target mean / price - 1",
        "Upside to the mean analyst target, in %. A price return: dividends excluded.",
        "Sell-side targets are optimism-biased.",
    ),
    "forward_pe": (
        "price / next fiscal year's consensus EPS",
        "Forward price-to-earnings. Empty when the estimate is negative.",
        "Lower is generally cheaper.",
    ),
    "ev_ebitda": (
        "(market cap + debt - cash) / EBITDA (TTM)",
        "Enterprise value to EBITDA; capital-structure neutral. Empty when EBITDA is negative.",
        "",
    ),
    "peg": (
        "P/E / expected annual EPS growth (%)",
        "PEG ratio, using Yahoo's 5-year growth estimate.",
        "Around 1 is the classic 'fair' mark.",
    ),
    "dividend_yield": (
        "annual dividend per share / price",
        "Forward dividend yield.",
        "2-4% is typical for mature payers; far above the sector often prices in a cut.",
    ),
}

METRIC_DEFS: dict[str, tuple[str, str, str]] = {
    # STATS_KEYS (Portfolio Metrics); the SPY / NASDAQ columns use the same definitions.
    "total_return": (
        "prod_t (1 - c_t)(1 + sum_i w_i,t r_i,t) - 1",
        "Total return over the period in the display currency, dividend-adjusted; the weights drift between "
        "rebalances (the calendar and trading cost c set under Constraints in the app).",
        "",
    ),
    "ann_return": (
        "(V_T / V_0)^(365.25 / calendar days) - 1",
        "Compound annual growth rate.",
        "On short periods it extrapolates to a year; read with care.",
    ),
    "ann_vol": ("sqrt(252) x sd(daily returns)", "Annualised volatility of daily returns.", ""),
    "sharpe": (
        "252 x mean(r - rf) / (sqrt(252) x sd(r - rf))",
        "Excess return per unit of total volatility, from daily returns.",
        "Above 1 is good; above 2 is rare over long periods.",
    ),
    "sortino": (
        "252 x mean(x) / (sqrt(252) x sqrt(mean(min(x, 0)^2))), x = r - rf",
        "Like Sharpe, but only downside volatility counts.",
        "Above Sharpe when losses are rarer or smaller than gains.",
    ),
    "calmar": (
        "annualised return / |max drawdown|",
        "Return per unit of worst peak-to-trough loss over the period.",
        "Noisy on short periods.",
    ),
    "max_dd": (
        "min_t(V_t / max_(s<=t) V_s - 1)",
        "Worst peak-to-trough loss over the period, in %.",
        "",
    ),
    "beta": (
        "Cov(r_p, r_SPY) / Var(r_SPY)",
        "Sensitivity of the portfolio to SPY, from daily returns.",
        "1 moves with SPY; 1.3 moves 30% more.",
    ),
    "r2": (
        "Corr(r_p, r_SPY)^2",
        "Share of the portfolio's variance explained by SPY.",
        "Low R2 means beta describes the portfolio poorly.",
    ),
    "te": (
        "sqrt(252) x sd(r_p - r_SPY)",
        "Tracking error: how far the portfolio wanders from SPY, annualised.",
        "",
    ),
    "ir": (
        "252 x mean(r_p - r_SPY) / tracking error",
        "Information ratio: active return per unit of tracking error.",
        "Above ~0.5 is good for an active portfolio.",
    ),
    "rf_ann": (
        "prod_t(1 + rf_t)^(1 / years) - 1, daily short-rate returns compounded",
        "Risk-free rate used for Sharpe and Sortino, % a year (13-week T-bill, ^IRX, for USD).",
        "",
    ),
    "excess_vs_spy": (
        "total return - SPY total return",
        "Period return minus SPY's, in percentage points.",
        "",
    ),
    # WEIGHTED_KEYS
    "w_pe": (
        "sum_i w_i / sum_i (w_i / PE_i) over holdings with a P/E (weighted harmonic mean)",
        "The portfolio's price-to-earnings, as if it were one company.",
        "Lower is generally cheaper.",
    ),
    "w_ps": (
        "weighted harmonic mean of P/S",
        "The portfolio's price-to-sales, as if it were one company.",
        "",
    ),
    "w_ev_ebitda": (
        "weighted harmonic mean of EV/EBITDA",
        "The portfolio's enterprise value to EBITDA, as if it were one company.",
        "",
    ),
    "w_div_yield": (
        "sum_i w_i y_i",
        "The portfolio's dividend yield: what it would pay out a year per unit invested.",
        "",
    ),
    "w_market_cap": (
        "sum_i w_i cap_i, in the display currency",
        "Weighted average company size.",
        "",
    ),
    # Contribution
    "period_return": (
        "price_T / price_0 - 1",
        "The holding's own return over the period, in %.",
        "",
    ),
    "contribution": (
        "linked sum_t w_i,t r_i,t (Carino)",
        "Percentage points of the portfolio's return that came from this holding.",
        "The holdings' contributions add up to the portfolio's total return.",
    ),
    # ANALYST_AGG_KEYS
    "mean_rating": (
        "sum_i w_i R_i / sum_i w_i over rated holdings",
        "Weighted mean analyst rating.",
        "1 Strong Buy, 2 Buy, 3 Hold, 4 Underperform, 5 Sell.",
    ),
    "rating_coverage_weight": (
        "sum of weights of holdings with a rating",
        "Share of the portfolio the mean rating is based on.",
        "",
    ),
    "weighted_target_upside_pct": (
        "sum_i w_i (TP_i / P_i - 1) / sum_i w_i over covered holdings",
        "Weighted upside to the analysts' mean price targets, in %.",
        "A price return; sell-side targets are optimism-biased.",
    ),
    "target_coverage_weight": (
        "sum of weights of holdings with a price target",
        "Share of the portfolio the target upside is based on.",
        "",
    ),
    "n_analysts_total": (
        "sum_i n_i",
        "Sum of each holding's analyst count (one analyst covering two holdings counts twice).",
        "",
    ),
    "covered_count": ("", "Holdings with analyst coverage.", ""),
    "active_count": ("", "Holdings with usable price history for the analytics.", ""),
    # CONCENTRATION_KEYS
    "top5": (
        "sum of the five largest weights",
        "Combined weight of the five largest holdings.",
        "",
    ),
    "herfindahl": (
        "sum_i w_i^2",
        "Herfindahl index of concentration.",
        "Near 0 = spread out; 1 = a single holding.",
    ),
    "effective_n": (
        "1 / sum_i w_i^2",
        "The number of equal-weighted holdings that would be as concentrated.",
        "Below the raw count whenever weights are uneven.",
    ),
}

# Overview sheet: header suffix (after the period prefix) → METRIC_DEFS key.
OVERVIEW_METRICS: tuple[tuple[str, str], ...] = (
    ("Return", "total_return"),
    ("vs S&P 500", "excess_vs_spy"),
    ("Return a year", "ann_return"),
    ("Volatility", "ann_vol"),
    ("Sharpe", "sharpe"),
    ("Worst fall", "max_dd"),
)


def _def_text(d: tuple[str, str, str] | None) -> str | None:
    """One definition as the text of a header comment."""
    if not d:
        return None
    formula, meaning, rng = d
    parts = [meaning]
    if formula:
        parts.append(f"Formula: {formula}")
    if rng:
        parts.append(f"Typical: {rng}")
    return "\n".join(parts)


def _add_comment(cell, d: tuple[str, str, str] | None) -> None:
    """Attach a definition to a cell as an Excel hover comment."""
    text = _def_text(d)
    if text:
        c = Comment(text, "Convexity")
        c.width, c.height = 320, 150
        cell.comment = c


# --------------------------------------------------------------------------
# Units. Percent figures stay in percent points, as the app computes them
# (12.5 = 12.5%); fractions (weights, R², margins: 0.125) keep Excel's percent
# format. The Data sheet names the unit of every value, so a reader (or a
# model) never has to guess which of the two a number is.
# --------------------------------------------------------------------------

METRIC_UNITS: dict[str, str] = {
    **dict.fromkeys(["total_return", "ann_return", "ann_vol", "max_dd", "te", "rf_ann"], "%"),
    **dict.fromkeys(["weighted_target_upside_pct", "dist"], "%"),
    **dict.fromkeys(["r2", "rating_coverage_weight", "target_coverage_weight", "top5"], "fraction"),
    **dict.fromkeys(["herfindahl", "w_div_yield", "weight", "coverage"], "fraction"),
    **dict.fromkeys(["n_analysts_total", "covered_count", "active_count"], "count"),
    "excess_vs_spy": "pts",
    "contribution": "pts",
    "w_market_cap": "money",
}
UNIT_TEXT = {
    "%": "percent (12.5 = 12.5%)",
    "pts": "percentage points",
    "fraction": "fraction (0.125 = 12.5%)",
    "ratio": "ratio",
    "count": "count",
}
_SIGNED_KEYS = {"total_return", "ann_return", "excess_vs_spy", "weighted_target_upside_pct",
                "contribution", "period_return"}  # fmt: skip
# Only formats every viewer reads alike: a literal percent sign escaped
# (macOS Quick Look scales a quoted "%" by 100) and [Red] for losses (it
# ignores [Color10]); gains turn green by conditional formatting (_colour_gains).
_FMT = {"%": "0.0\\%", "pts": "0.00", "fraction": "0.0%", "ratio": "0.00", "count": "#,##0",
        "money": "#,##0"}  # fmt: skip
_SIGNED_FMT = "+0.0\\%;[Red]-0.0\\%;0.0\\%"
_SIGNED_PTS = "+0.00;[Red]-0.00;0.00"


def _unit(key: str) -> str:
    return METRIC_UNITS.get(key, "ratio")


def _fmt(key: str) -> str:
    if key in _SIGNED_KEYS:
        return _SIGNED_PTS if _unit(key) == "pts" else _SIGNED_FMT
    return _FMT[_unit(key)]


# Percent-point holdings columns (the others are fractions, money or plain).
_PP_COLS = {"pct_1d", "pct_2d", "pct_1w", "pct_1m", "pct_3m", "pct_6m", "pct_ytd", "pct_1y",
            "delta_ath", "target_upside_pct", "debt_equity"}  # fmt: skip
_HOLDING_FMT = {
    **dict.fromkeys(["price", "change_abs_1d", "w52_high", "w52_low", "ath", "sma_20", "sma_50",
                     "sma_200", "target_mean", "target_high", "target_low", "target_median",
                     "model_price"], "#,##0.00"),
    **dict.fromkeys(["ps_ratio", "pe_ratio", "forward_pe", "ev_ebitda", "peg", "beta",
                     "price_book", "ev_revenue", "current_ratio", "quick_ratio", "rsi_14", "bb_pct_b",
                     "macd_hist_pct", "recommendation_mean"], "0.00"),
    **dict.fromkeys(["market_cap", "market_cap_usd", "volume", "num_analysts"], "#,##0"),
    **dict.fromkeys(_PP_COLS, "0.00\\%"),
    **dict.fromkeys([*FRACTION_KEYS, "weight"], "0.0%"),
}  # fmt: skip


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_INVALID_SHEET_CHARS = re.compile(r"[\\/?*\[\]:]")
_RESERVED_SHEETS = ("Read me", "Overview", "Positions", "Ledger", "Data", "Definitions")
_BENCH_SERIES = (("Portfolio", None), ("S&P 500", "SPY"), ("Nasdaq-100", "QQQ"))
_PERIOD_WORDS = {"1Y": "the last year", "3Y": "the last 3 years", "5Y": "the last 5 years"}
# A cold export downloads every holding once; this bounds what is left (a
# Yahoo stall) so the button always comes back.
ANALYTICS_BUDGET_S = 120.0
# What the export is doing, for the button's label (GET /api/export-progress).
PROGRESS: dict = {"step": "", "done": 0, "total": 0}


def _sanitize_sheet_name(name: str, used: set[str]) -> str:
    """Excel: 31-char max, no `\\ / ? * [ ] :`, must be unique."""
    base = _INVALID_SHEET_CHARS.sub("_", (name or "Portfolio").strip())[:31] or "Portfolio"
    cand = base
    n = 2
    while cand.lower() in used:
        suffix = f"-{n}"
        cand = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(cand.lower())
    return cand


def _flatten_value(v):
    """Coerce a row-payload value to something openpyxl will accept."""
    if v is None:
        return None
    if isinstance(v, (int, float, str, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return f"[{len(v)} items]"
    if isinstance(v, dict):
        return f"[{len(v)} keys]"
    return str(v)


def _analytics_result_for_period(result: dict, set_name: str = "cap") -> dict:
    """Pull the single weight-set we asked for out of analyze_portfolios_multi's
    output. `set_name` is whatever key we passed in weight_sets (the active
    preset name, else "cap"), so the export reflects the portfolio's real
    weighting rather than always assuming cap-weighted."""
    if not isinstance(result, dict):
        return {"error": "invalid analytics payload"}
    if "results" in result:
        return result["results"].get(set_name) or {}
    if "error" in result:
        return {"error": result["error"]}
    return result.get(set_name) or result


def _first_available_analytics(analytics_by_period: dict[str, dict], periods: list[str]) -> dict:
    for period in periods:
        analytics = analytics_by_period.get(period) or {}
        if analytics and not analytics.get("error"):
            return analytics
    return {}


def _bench(analytics: dict, key: str) -> dict:
    """One benchmark block from analytics["benchmarks"], or {}."""
    return (analytics.get("benchmarks") or {}).get(key) or {}


def _excess_vs_spy(analytics: dict) -> float | None:
    if not isinstance(analytics, dict):
        return None
    port_tr = _maybe_num((analytics.get("stats") or {}).get("total_return"))
    spy_tr = _maybe_num(_bench(analytics, "SPY").get("stats", {}).get("total_return"))
    if port_tr is None or spy_tr is None:
        return None
    return port_tr - spy_tr


def _series_stats(analytics: dict, bench: str | None) -> dict:
    """The stats column for the portfolio (with its beta, R², TE, IR vs SPY)
    or for one benchmark."""
    if bench is None:
        return {**(analytics.get("stats") or {}), **(_bench(analytics, "SPY").get("rel") or {}),
                "excess_vs_spy": _excess_vs_spy(analytics)}  # fmt: skip
    return _bench(analytics, bench).get("stats") or {}


def _when(iso) -> str:
    """A saved_at stamp as the reader's local date and time."""
    try:
        return datetime.fromisoformat(str(iso)).astimezone().strftime("%d %b %Y, %H:%M")
    except ValueError:
        return "the last refresh"


def _metric_periods(primary_period: str) -> list[str]:
    periods: list[str] = []
    for candidate in (primary_period, *EXPORT_STATS_PERIODS):
        period = str(candidate or "").upper()
        if period and period not in periods:
            periods.append(period)
    return periods or list(EXPORT_STATS_PERIODS)


def headline(analytics_by_period: dict[str, dict], periods: list[str]) -> str:
    """The portfolio in two or three plain sentences, for the Read me and the
    top of its sheet."""
    out = []
    for p in periods:
        a = analytics_by_period.get(p) or {}
        st = a.get("stats") or {}
        tr = _maybe_num(st.get("total_return"))
        if tr is None:
            continue
        s = f"{'Up' if tr >= 0 else 'Down'} {abs(tr):.1f}% over {_PERIOD_WORDS.get(p, p)}"
        ar = _maybe_num(st.get("ann_return"))
        if p != "1Y" and ar is not None:
            s += f" ({ar:+.1f}% a year)"
        spy = _maybe_num(_series_stats(a, "SPY").get("total_return"))
        out.append(s + (f"; the S&P 500 {spy:+.1f}%." if spy is not None else "."))
    first = _first_available_analytics(analytics_by_period, periods)
    dd = _maybe_num((first.get("stats") or {}).get("max_dd"))
    if dd is not None:
        out.append(
            f"Worst fall from a peak in {_PERIOD_WORDS.get(first.get('period'), 'the period')}: {dd:.1f}%."
        )
    top5 = _maybe_num((first.get("concentration") or {}).get("top5"))
    if top5 is not None:
        out.append(f"The 5 largest holdings are {top5 * 100:.0f}% of it.")
    return " ".join(out) or "No performance figures: Yahoo had no usable price history for it."


# --------------------------------------------------------------------------
# Sheet writers
# --------------------------------------------------------------------------

_HEADER_FILL = PatternFill("solid", fgColor="1F2937")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_SECTION_FONT = Font(bold=True, size=12, color="1F2937")
_TITLE_FONT = Font(bold=True, size=16)
_NOTE_FONT = Font(italic=True, color="6B7280")
_MEANING_FONT = Font(color="6B7280")
_RULE = Border(bottom=Side(style="thin", color="9CA3AF"))
_WRAP = Alignment(wrap_text=True, vertical="top")


def _write_title(ws, title: str, note: str) -> None:
    ws["A1"] = title
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = note
    ws["A2"].font = _NOTE_FONT


def _write_section(ws, r: int, title: str, width: int = 8) -> int:
    """A kicker and a hairline under it, as the app draws sections."""
    ws.cell(row=r, column=1, value=title).font = _SECTION_FONT
    for c in range(1, width + 1):
        ws.cell(row=r, column=c).border = _RULE
    return r + 1


def _write_header_cells(ws, r: int, labels: Iterable[str], defs: Iterable = ()) -> None:
    """One styled table-header row from column 1; `defs`, aligned with
    `labels`, attaches each column's definition as a hover comment."""
    defs = list(defs)
    for col_idx, label in enumerate(labels, 1):
        c = ws.cell(row=r, column=col_idx, value=label)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        if col_idx <= len(defs):
            _add_comment(c, defs[col_idx - 1])


def _put(ws, r: int, c: int, value, fmt: str | None = None):
    cell = ws.cell(row=r, column=c, value=value)
    if fmt and isinstance(value, int | float) and not isinstance(value, bool):
        cell.number_format = fmt
        if fmt in (_SIGNED_FMT, _SIGNED_PTS):
            ws.__dict__.setdefault("_gains", []).append(cell.coordinate)
    return cell


def _colour_gains(wb: Workbook) -> None:
    """One green-for-gains rule per sheet over every signed cell _put wrote."""
    for ws in wb.worksheets:
        if cells := ws.__dict__.get("_gains"):
            ws.conditional_formatting.add(" ".join(cells), CellIsRule(
                operator="greaterThan", formula=["0"], font=Font(color="15803D")))  # fmt: skip


def _write_kv(ws, r: int, label: str, key: str, value, defs=METRIC_DEFS, note: str = "") -> int:
    """Label (with its definition on hover), value, then what it means."""
    a = ws.cell(row=r, column=1, value=label)
    a.font = Font(bold=True)
    _add_comment(a, defs.get(key))
    _put(ws, r, 2, value, _fmt(key))
    meaning = note or (defs.get(key) or ("", "", ""))[1]
    ws.cell(row=r, column=3, value=meaning or None).font = _MEANING_FONT
    return r + 1


class _Data:
    """The long table behind the Data sheet: one row per number."""

    HEADER = ["Portfolio", "Period", "Section", "Key", "Label", "Series", "Value", "Unit"]

    def __init__(self, ccy: str) -> None:
        self.rows: list[list] = []
        self.ccy = ccy

    def add(self, pf, period, section, key, label, value, series="Portfolio", unit=None) -> None:
        value = _maybe_num(value)
        if value is not None:
            u = unit or _unit(key)
            self.rows.append([pf, period, section, key, label, series, value,
                              self.ccy if u == "money" else UNIT_TEXT.get(u, u)])  # fmt: skip


def _write_overview(ws, summaries: list[dict], metric_periods: list[str], ccy: str) -> None:
    _write_title(
        ws,
        "Overview",
        f"One row per portfolio · values in {ccy} · hover a header for its definition",
    )
    headers = ["Portfolio", "Holdings", "Weighting"]
    defs: list = [None, None, None]
    for period in metric_periods:
        for suffix, key in OVERVIEW_METRICS:
            headers.append(f"{period} {suffix}")
            defs.append(METRIC_DEFS[key])
    _write_header_cells(ws, 4, headers, defs)
    for row, s in enumerate(summaries, 5):
        for c, v in enumerate((s["name"], s["rows"], s["weighting"]), 1):
            ws.cell(row=row, column=c, value=v)
        col = 4
        for period in metric_periods:
            stats = _series_stats(s["analytics_by_period"].get(period) or {}, None)
            for _suffix, key in OVERVIEW_METRICS:
                _put(ws, row, col, _maybe_num(stats.get(key)), _fmt(key))
                col += 1
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 13
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["C"].width = 22
    ws.row_dimensions[4].height = 32
    ws.freeze_panes = "B5"


_REBALANCE_WORDS = {"D": "daily", "W": "weekly", "M": "monthly", "Q": "quarterly",
                    "S": "every six months", "Y": "yearly"}  # fmt: skip


def _trading_line(t: dict | None) -> str | None:
    """The rebalancing and cost the returns assume (the app's Constraints),
    from the analytics that made them: a reader comparing numbers with the
    app needs the same setting."""
    if not t or not t.get("rebalance"):
        return None
    bps = f"{float(t.get('tc_bps') or 0):g} bps"
    if t["rebalance"] == "N":
        return (f"Buy and hold: the weights are set on the first day and drift with prices, never "
                f"reset (Constraints in the app); a holding that starts trading later is bought at {bps}.")  # fmt: skip
    how = _REBALANCE_WORDS.get(t["rebalance"], t["rebalance"])
    return (f"Rebalanced {how} at a trading cost of {bps} (Constraints in the app): the weights drift "
            f"with prices between rebalances, and each rebalance pays the cost on what it buys and sells.")  # fmt: skip


def _write_readme(ws, summaries: list[dict], sheets: list[tuple[str, str]], ccy: str,
                  trading: dict | None = None) -> None:  # fmt: skip
    created = datetime.now().astimezone().strftime("%d %b %Y, %H:%M %Z")
    _write_title(
        ws, "Convexity export", f"Created {created} · values in {ccy} · {len(summaries)} portfolios"
    )
    r = _write_section(ws, 4, "What is in this file", 2)
    for name, what in sheets:
        ws.cell(row=r, column=1, value=name).font = Font(bold=True)
        ws.cell(row=r, column=2, value=what).alignment = _WRAP
        r += 1
    r = _write_section(ws, r + 1, "At a glance", 2)
    for s in summaries:
        ws.cell(row=r, column=1, value=s["name"]).font = Font(bold=True)
        ws.cell(row=r, column=2, value=s["headline"]).alignment = _WRAP
        r += 1
    r = _write_section(ws, r + 1, "How to read it", 2)
    for line in (
        f"1Y is the last 12 months and 5Y the last 5 years, up to the latest close. Returns include "
        f"dividends and are in {ccy}, so currency moves are part of them.",
        "Each portfolio is weighted as it is set in the app: its active weight preset, else by market "
        "value. The benchmarks are the SPY (S&P 500) and QQQ (Nasdaq-100) funds, in the same currency.",
        _trading_line(trading),
        "Green is a gain, red a loss. 'pts' are percentage points: the gap between two percentages.",
        "A % figure is in percent (12.5 means 12.5%); weights, margins and R² are Excel percentages.",
        "Hover any column or metric name for its formula and typical range, or see Definitions.",
        "The Data sheet has every portfolio figure in one long table (portfolio, period, metric, "
        "value, unit) to filter, pivot or give to an AI as context.",
        "Past returns do not predict future ones. Prices, fundamentals and analyst figures come from "
        "Yahoo Finance and can be late or wrong; analysts' price targets lean optimistic.",
    ):
        if not line:
            continue
        ws.cell(row=r, column=2, value=line).alignment = _WRAP
        r += 1
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 110


def _per_symbol_analyst(analytics: dict) -> dict[str, dict]:
    """The per-holding analyst rows of analytics["analyst"]["holdings"], by
    ticker."""
    out: dict[str, dict] = {}
    for h in (analytics.get("analyst") or {}).get("holdings") or []:
        if isinstance(h, dict) and h.get("symbol"):
            out[h["symbol"]] = h
    return out


def _analyst_cell(key: str, row: dict, ana: dict):
    """One ANALYST_COLS value: the analytics' per-holding block first (its
    target range is fetched when an older row lacks it), else the row."""
    if key == "target_upside_pct":
        v = ana.get("upside_pct")
        price, tgt = _maybe_num(row.get("price")), _maybe_num(row.get("target_mean_price"))
        return v if v is not None or not (price and tgt) else (tgt / price - 1.0) * 100.0
    src = {"recommendation_key": ("rec_key", "rec_key"),
           "recommendation_mean": ("mean_rating", "recommendation_mean"),
           "num_analysts": ("n_analysts", "n_analysts"),
           "target_mean": ("target_mean", "target_mean_price"),
           "target_high": ("target_high", "target_high"),
           "target_low": ("target_low", "target_low"),
           "target_median": ("target_median", "target_median")}.get(key, (None, key))  # fmt: skip
    v = ana.get(src[0]) if src[0] else None
    return v if v is not None else row.get(src[1])


def _write_portfolio_sheet(
    ws, pf: dict, metric_periods: list[str], data: _Data, table_n: int
) -> None:
    name, view, abp = pf["name"], pf["view"], pf["analytics_by_period"]
    rows = [x for x in view.get("rows") or [] if isinstance(x, dict)]
    primary = _first_available_analytics(abp, metric_periods)
    period = primary.get("period") or metric_periods[0]
    weights = primary.get("weights_applied") or pf["weights"]
    _write_title(ws, name, f"{len(rows)} holdings · weighted {pf['weighting']} · values in {data.ccy} · "
                           f"holdings data from {_when(view.get('saved_at'))}")  # fmt: skip
    ws.merge_cells("A3:H3")
    ws["A3"] = pf["headline"]
    ws["A3"].font = Font(size=12)
    ws["A3"].alignment = _WRAP
    ws.row_dimensions[3].height = 36
    r = 5

    # Performance: every period side by side, the portfolio next to its benchmarks.
    r = _write_section(ws, r, "Performance")
    cols = [(p, label, b) for p in metric_periods for label, b in _BENCH_SERIES]
    _write_header_cells(
        ws, r, ["Metric", *[f"{p} {label}" for p, label, _ in cols], "What it means"]
    )
    ws.row_dimensions[r].height = 30
    r += 1
    for key, label in [*STATS_KEYS, ("excess_vs_spy", "Excess vs SPY (pts)")]:
        lc = ws.cell(row=r, column=1, value=label)
        lc.font = Font(bold=True)
        _add_comment(lc, METRIC_DEFS.get(key))
        for c, (p, series, b) in enumerate(cols, 2):
            v = _maybe_num(_series_stats(abp.get(p) or {}, b).get(key))
            _put(ws, r, c, v, _fmt(key))
            data.add(name, p, "Performance", key, label, v, series)
        ws.cell(row=r, column=len(cols) + 2, value=METRIC_DEFS[key][1]).font = _MEANING_FONT
        r += 1
    r += 1

    weighted = primary.get("weighted") or {}
    if weighted:
        r = _write_section(ws, r, f"Valuation (the portfolio as one company, {period})")
        cov = weighted.get("coverage") or {}
        for key, label in WEIGHTED_KEYS:
            v = weighted.get(key[2:])
            c = _maybe_num(cov.get(key[2:]))
            note = METRIC_DEFS[key][1] + (
                f" Based on {c * 100:.0f}% of the portfolio." if c is not None else ""
            )
            label = f"{label} ({data.ccy})" if _unit(key) == "money" else label
            r = _write_kv(ws, r, label, key, v, note=note)
            data.add(name, period, "Valuation", key, label, v)
            data.add(
                name,
                period,
                "Valuation",
                f"coverage.{key[2:]}",
                f"{label}: coverage",
                c,
                unit="fraction",
            )
        r += 1

    analyst = primary.get("analyst") or {}
    if analyst:
        r = _write_section(ws, r, "Analysts (weighted by holding)")
        for key, label in ANALYST_AGG_KEYS:
            r = _write_kv(ws, r, label, key, analyst.get(key))
            data.add(name, period, "Analysts", key, label, analyst.get(key))
        for k, label in (("strongBuy", "Strong buy"), ("buy", "Buy"), ("hold", "Hold"),
                         ("sell", "Sell"), ("strongSell", "Strong sell")):  # fmt: skip
            v = (analyst.get("distribution_pct") or {}).get(k)
            if v is not None:
                r = _write_kv(
                    ws,
                    r,
                    f"Ratings: {label}",
                    "dist",
                    v,
                    note="Share of analyst ratings, weighted by holding.",
                )
                data.add(name, period, "Analysts", f"dist.{k}", f"Ratings: {label}", v, unit="%")
        r += 1

    exposure = primary.get("exposure") or {}
    if exposure:
        r = _write_section(ws, r, "Where the money is")
        top = r
        for col, (key, title) in ((1, ("by_sector", "Sector")), (5, ("by_bucket", "Company size")),
                                  (9, ("by_industry", "Industry"))):  # fmt: skip
            rr = top
            for c, label in enumerate((title, "Weight"), col):
                h = ws.cell(row=rr, column=c, value=label)
                h.font, h.fill = _HEADER_FONT, _HEADER_FILL
            for k, w in sorted((exposure.get(key) or {}).items(), key=lambda kv: -kv[1]):
                rr += 1
                ws.cell(row=rr, column=col, value=k)
                _put(ws, rr, col + 1, _maybe_num(w), _FMT["fraction"])
                data.add(name, period, title, f"{key}.{k}", k, w, unit="fraction")
            r = max(r, rr + 1)
        r += 1

    concentration = primary.get("concentration") or {}
    if concentration:
        r = _write_section(ws, r, "Concentration")
        for key, label in CONCENTRATION_KEYS:
            r = _write_kv(ws, r, label, key, concentration.get(key))
            data.add(name, period, "Concentration", key, label, concentration.get(key))
        r += 1

    contribution = primary.get("contribution") or []
    if contribution:
        r = _write_section(ws, r, f"What drove the return ({period})")
        _write_header_cells(ws, r, ["Holding", "Ticker", "Sector", "Weight", "Return %", "Contribution, pts"],
                            [None, None, None, COLUMN_DEFS["weight"], METRIC_DEFS["period_return"],
                             METRIC_DEFS["contribution"]])  # fmt: skip
        for x in contribution:
            r += 1
            for c, (v, fmt) in enumerate(((x.get("name"), None), (x.get("symbol"), None), (x.get("sector"), None),
                                          (x.get("weight"), _FMT["fraction"]),
                                          (x.get("period_return"), _SIGNED_FMT),
                                          (x.get("contribution"), _fmt("contribution"))), 1):  # fmt: skip
                _put(ws, r, c, v, fmt)
            data.add(
                name,
                period,
                "Contribution",
                f"contribution.{x.get('symbol')}",
                x.get("name"),
                x.get("contribution"),
                unit="pts",
            )
            data.add(
                name,
                period,
                "Contribution",
                f"period_return.{x.get('symbol')}",
                x.get("name"),
                x.get("period_return"),
                unit="%",
            )
        r += 1
        ws.cell(row=r, column=1, value="Total").font = Font(bold=True)
        _put(
            ws,
            r,
            6,
            sum(_maybe_num(x.get("contribution")) or 0.0 for x in contribution),
            _fmt("contribution"),
        )
        r += 2

    notes = list(
        dict.fromkeys(w for p in metric_periods for w in (abp.get(p) or {}).get("warnings") or [])
    )
    notes += [f"{p}: {a['error']}" for p in metric_periods if (a := abp.get(p) or {}).get("error")]
    r = _write_section(ws, r, "Notes")
    for line in notes or ["None: every holding had a full price history."]:
        ws.cell(row=r, column=1, value=line).font = _MEANING_FONT
        r += 1
    r += 1

    _write_holdings(ws, _write_section(ws, r, "Holdings"), rows, weights, primary, table_n)
    ws.column_dimensions["A"].width = 40
    for col in range(2, 9):
        ws.column_dimensions[get_column_letter(col)].width = 15
    ws.column_dimensions["B"].width = 18


def _write_holdings(
    ws, r: int, rows: list[dict], weights: dict, primary: dict, table_n: int
) -> None:
    """Every row field plus the analyst columns, as an Excel table (filter
    and sort). Unknown row keys are appended, so fetch_one additions show up."""
    primary_keys = {k for k, _ in HOLDINGS_PRIMARY_COLS}
    extra_keys = sorted({k for row in rows for k in row} - primary_keys - HOLDINGS_SKIP_EXTRAS)
    headers = (
        HOLDINGS_PRIMARY_COLS
        + SENTIMENT_COLS
        + [(k, EXTRA_LABELS.get(k, k)) for k in extra_keys]
        + ANALYST_COLS
    )
    _write_header_cells(
        ws, r, [label for _k, label in headers], [COLUMN_DEFS.get(k) for k, _ in headers]
    )
    ws.row_dimensions[r].height = 30
    header_row = r
    analyst_keys = {k for k, _ in ANALYST_COLS}
    by_sym = _per_symbol_analyst(primary)
    for row in rows:
        r += 1
        sym = row.get("symbol")
        for c, (key, _label) in enumerate(headers, 1):
            if key == "weight":
                v = weights.get(sym)
            elif key in analyst_keys:
                v = _analyst_cell(key, row, by_sym.get(sym) or {})
            elif key.startswith("ns:"):
                v = _sentiment_value(key, row.get("news_sentiment"))
            else:
                v = row.get(key)
            _put(ws, r, c, _flatten_value(v), _HOLDING_FMT.get(key))
    if r > header_row:
        ref = f"A{header_row}:{get_column_letter(len(headers))}{r}"
        t = Table(displayName=f"Holdings{table_n}", ref=ref)
        t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
        ws.add_table(t)
    for c, (_key, label) in enumerate(headers, 1):
        if c > 8:
            ws.column_dimensions[get_column_letter(c)].width = max(11, min(len(str(label)) + 2, 24))


def _write_data(ws, data: _Data) -> None:
    _write_title(ws, "Data", "Every portfolio figure in one long table: filter, pivot, or give it to an AI as context. "
                             "Units are in the last column.")  # fmt: skip
    _write_header_cells(ws, 4, _Data.HEADER)
    for r, vals in enumerate(data.rows, 5):
        for c, v in enumerate(vals, 1):
            ws.cell(row=r, column=c, value=v)
    if data.rows:
        t = Table(displayName="Data", ref=f"A4:H{4 + len(data.rows)}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
        ws.add_table(t)
    for col, width in zip("ABCDEFGH", (24, 8, 16, 30, 34, 12, 14, 26), strict=True):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A5"


def definition_rows() -> list[tuple[str, str, str, str, str]]:
    """Every definition the workbook uses, as (section, label, formula,
    meaning, range) rows in sheet order: holdings columns, then portfolio
    metrics. Extras only appear when EXTRA_LABELS names them, so a row key
    without a label never reaches the sheet as a raw key."""
    out: list[tuple[str, str, str, str, str]] = []
    holdings = (
        HOLDINGS_PRIMARY_COLS
        + SENTIMENT_COLS
        + sorted(EXTRA_LABELS.items(), key=lambda kv: kv[1])
        + ANALYST_COLS
    )
    for key, label in holdings:
        d = COLUMN_DEFS.get(key)
        if d:
            out.append(("Holdings columns", label, *d))
    for section, keys in (
        ("Portfolio metrics", STATS_KEYS + [("excess_vs_spy", "Excess vs SPY (pts)")]),
        ("Valuation", WEIGHTED_KEYS),
        ("Analyst coverage", ANALYST_AGG_KEYS),
        ("Concentration", CONCENTRATION_KEYS),
        ("Contribution", [("period_return", "Return %"), ("contribution", "Contribution, pts")]),
    ):
        for key, label in keys:
            d = METRIC_DEFS.get(key)
            if d:
                out.append((section, label, *d))
    return out


def _write_definitions(ws) -> None:
    """The last sheet: every metric in the workbook, with its formula, what it
    means and a typical range, so the file explains itself when handed on."""
    _write_title(ws, "Definitions", "The same text is on each header as a hover comment.")
    _write_header_cells(ws, 4, ["Section", "Metric", "Formula", "Meaning", "Typical range"])
    for r, row in enumerate(definition_rows(), 5):
        for col, v in enumerate(row, 1):
            ws.cell(row=r, column=col, value=v or None).alignment = _WRAP
        ws.cell(row=r, column=2).font = Font(bold=True)
    for col, width in zip("ABCDE", (18, 30, 44, 60, 44), strict=True):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A5"


# --------------------------------------------------------------------------
# Weighting resolution
# --------------------------------------------------------------------------


def _resolve_active_weights(view: dict, rows: list[dict]) -> tuple[dict[str, float], str, str]:
    """Resolve the weighting the export should use for one portfolio.

    Mirrors the frontend (``weightsForMode`` in app.js): the view's active
    weight preset — the ``weight_presets`` entry whose name == the view's
    ``active_weight_preset`` — projected onto the current row symbols, with
    symbols absent from the preset defaulting to 0 and negatives clamped to 0,
    then renormalized to sum 1.0. Falls back to market-cap weighting when there
    is no active preset, the named preset is missing, or the projection sums to
    <= 0 (e.g. the preset predates every current holding) — same cap fallback
    the frontend uses.

    Returns ``(weights, set_name, label)``: ``set_name`` is the key handed to
    analyze_portfolios_multi (so ``_analytics_result_for_period`` can pull the
    matching result back out), and ``label`` describes the weighting in the
    sheet's "Portfolio Metrics" header.
    """
    active_name = str((view or {}).get("active_weight_preset") or "").strip()
    if active_name:
        presets = view.get("weight_presets")
        preset = None
        if isinstance(presets, list):
            for p in presets:
                if isinstance(p, dict) and str(p.get("name") or "").strip() == active_name:
                    preset = p
                    break
        if isinstance(preset, dict):
            # Preset weight keys are stored upper-cased (persistence.
            # _normalize_preset_weights); match row symbols the same way.
            raw = preset.get("weights") if isinstance(preset.get("weights"), dict) else {}
            projected: dict[str, float] = {}
            total = 0.0
            for r in rows:
                sym = r.get("symbol") if isinstance(r, dict) else None
                if not sym:
                    continue
                w = _maybe_num(raw.get(str(sym).strip().upper()))
                w = w if (w is not None and w > 0) else 0.0
                projected[sym] = w
                total += w
            if total > 0:
                weights = {s: w / total for s, w in projected.items()}
                return (weights, active_name, f"by its '{active_name}' preset")
    # No usable preset → market-cap weighting (matches the frontend fallback).
    return (_cap_weights(rows), "cap", "by market value")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def _with_model(view: dict, model_runner) -> dict:
    """``view`` with each row's beta / model columns from ``model_runner``
    (rows copied: the saved view is never touched). Without a runner the
    stale beta is dropped rather than exported under the new label."""
    rows = [r for r in view["rows"] if isinstance(r, dict)]
    models = {}
    if model_runner is not None:
        try:
            models = model_runner(rows) or {}
        except Exception as exc:
            print(f"[export] model prices: {type(exc).__name__}: {exc}", flush=True)
    out = []
    for r in rows:
        r = {k: v for k, v in r.items() if k not in ("beta", "beta_meta")}
        m = models.get((r.get("symbol") or "").upper())
        if m:
            bm = m["beta_meta"]
            how = {"bswa": f"{bm['years']}Y daily, BSWA", "ols1y": "1Y daily OLS",
                   "weekly": f"{bm['years']}Y weekly in USD",
                   "prior": "prior (too little history)"}.get(bm["method"], bm["method"])  # fmt: skip
            via = f" · via {bm['via']}" if bm.get("via") else ""
            r.update(beta=m["beta"], beta_vs=f"{bm['proxy_name']} · {how}{via}",
                     model_mu=m["mu"], model_price=m["model_price"])  # fmt: skip
        out.append(r)
    return {**view, "rows": out}


def build_workbook(
    views_map: dict[str, dict],
    *,
    analytics_runner=None,
    prefetch=None,
    period: str = "1Y",
    display_ccy: str = "USD",
    book: dict | None = None,
    model_runner=None,
) -> bytes:
    """Build an .xlsx blob covering every saved portfolio.

    ``analytics_runner(rows, weight_sets, period, display_ccy)`` is
    ``analytics.analyze_portfolios_multi``; without it the sheets still build
    with the holdings only. ``prefetch(rows, periods, ccy)`` (``analytics.
    prefetch_closes``) downloads every portfolio's prices in one go first, so
    the runs that follow read the cache. ``book`` is My Investments' page
    payload; it adds the Positions and Ledger sheets. ``model_runner(rows)``
    is ``capm.for_rows``: the app's beta, mu and 12-month model price for
    every row, one batch, so the sheet shows the grid's numbers (a saved row
    may still carry a pre-1.29 Yahoo beta, which is never exported).
    """
    metric_periods = _metric_periods(period)
    valid = [(n, v) for n, v in views_map.items() if isinstance(v, dict) and v.get("rows")]
    valid = [(n, _with_model(v, model_runner)) for n, v in valid]
    if analytics_runner is not None and prefetch is not None and valid:
        PROGRESS.update(step="downloading prices", done=0, total=len(valid))
        try:
            rows = [r for _n, v in valid for r in v["rows"] if isinstance(r, dict)]
            prefetch(rows, metric_periods, display_ccy)
        except Exception as exc:  # each run then fetches what it needs itself
            print(f"[export] prefetch failed: {type(exc).__name__}: {exc}", flush=True)

    deadline = time.monotonic() + ANALYTICS_BUDGET_S
    portfolios: list[dict] = []
    for i, (name, view) in enumerate(valid):
        PROGRESS.update(step="analysing", done=i, total=len(valid))
        rows = view["rows"]
        weights, set_name, weighting = _resolve_active_weights(view, rows)
        abp: dict[str, dict] = {}
        for p in metric_periods if analytics_runner is not None else ():
            if time.monotonic() > deadline:
                abp[p] = {"error": "not computed in time; export again (the prices are cached now)"}
                continue
            try:
                abp[p] = _analytics_result_for_period(
                    analytics_runner(rows, {set_name: weights}, p, display_ccy), set_name
                )
            except Exception as exc:
                abp[p] = {"error": f"runner failed: {exc}"}
        portfolios.append({"name": name, "view": view, "weights": weights, "weighting": weighting,
                           "analytics_by_period": abp, "rows": len(rows),
                           "headline": headline(abp, metric_periods)})  # fmt: skip

    PROGRESS.update(step="writing", done=len(valid), total=len(valid))
    wb = Workbook()
    used = {s.lower() for s in _RESERVED_SHEETS}
    readme, overview = wb.active, wb.create_sheet("Overview")
    readme.title = "Read me"
    sheets = [
        (
            "Overview",
            "Every portfolio side by side: return, risk and how it did against the S&P 500.",
        )
    ]
    has_book = bool(book and not book.get("empty") and book.get("headline"))
    if has_book:
        _write_positions(wb.create_sheet("Positions"), book, period)
        _write_ledger(wb.create_sheet("Ledger"), book, period)
        sheets += [("Positions", "My Investments: what you hold now, its value, profit and dividends."),
                   ("Ledger", "My Investments: every trade, dividend, deposit and fee you entered.")]  # fmt: skip
    data = _Data(display_ccy)
    titles = []
    for i, pf in enumerate(portfolios, 1):
        titles.append(_sanitize_sheet_name(pf["name"], used))
        _write_portfolio_sheet(wb.create_sheet(titles[-1]), pf, metric_periods, data, i)
    if titles:
        sheets.append(("Portfolio sheets", f"One per portfolio ({', '.join(titles)}): its summary, "
                       "performance, risk, analysts, sectors, what drove the return, then every holding."))  # fmt: skip
    sheets += [
        ("Data", "Every figure above in one long table, with its unit (for analysis or AI)."),
        ("Definitions", "What each column and metric means, with its formula and typical range."),
    ]
    _write_data(wb.create_sheet("Data"), data)
    _write_definitions(wb.create_sheet("Definitions"))
    _write_overview(overview, portfolios, metric_periods, display_ccy)
    trading = next((a["trading"] for p in portfolios for a in p["analytics_by_period"].values()
                    if isinstance(a, dict) and a.get("trading")), None)  # fmt: skip
    _write_readme(readme, portfolios, sheets, display_ccy, trading)

    buf = io.BytesIO()
    _colour_gains(wb)
    wb.save(buf)
    PROGRESS.update(step="", done=0, total=0)
    return buf.getvalue()


_MONEY, _PCT, _QTY = "#,##0.00", "0.0%", "#,##0.######"


def _pct(v):
    """A percent figure (12.5) as the fraction Excel formats (0.125)."""
    v = _maybe_num(v)
    return v / 100 if v is not None else None


def _write_table(ws, r: int, cols: list[tuple[str, str | None]], rows: list[list]) -> int:
    """Header plus rows; ``cols`` is (label, number format). Returns the
    next free row."""
    _write_header_cells(ws, r, [c[0] for c in cols])
    for vals in rows:
        r += 1
        for k, ((_, fmt), v) in enumerate(zip(cols, vals, strict=True), 1):
            cell = ws.cell(row=r, column=k, value=v)
            if isinstance(v, str) and v.startswith("="):
                cell.data_type = "s"  # a note or a name, never a formula Excel runs
            if fmt and isinstance(v, int | float):
                cell.number_format = fmt
    for k, (label, _) in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(k)].width = max(12, len(label) + 4)
    ws.column_dimensions["A"].width = 32
    return r + 1


def _write_positions(ws, book: dict, period: str) -> None:
    """My Investments' holdings as the page shows them (Phase 4): money in
    the display currency, prices in each share's quote unit, then cash, the
    total and the sold-out holdings."""
    ccy, h = book.get("ccy") or "USD", book["headline"]
    contrib = (h.get("contrib") or {}).get(period) or {}
    ws["A1"] = "My Investments — Positions"
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = (f"Generated: {datetime.now(UTC).isoformat(timespec='seconds')} · money in {ccy} · "
                f"contribution over {period}, in points of the return")  # fmt: skip
    ws["A2"].font = Font(italic=True, color="6B7280")
    cols = [("Holding", None), ("Ticker", None), ("Exchange", None), ("Quote ccy", None),
            ("Shares", _QTY), ("Avg price", _MONEY), ("Price", _MONEY), (f"Value ({ccy})", _MONEY),
            ("Weight", _PCT), ("Risk share", _PCT), ("Month", _PCT), (f"Profit ({ccy})", _MONEY),
            ("On what you hold", _MONEY), ("On what you sold", _MONEY), ("Dividends", _MONEY),
            ("Dividends, estimated", _MONEY), ("Return", _PCT), (f"Contribution {period} (pts)", "0.00"),
            ("Yield on cost", _PCT), ("Analyst upside", _PCT)]  # fmt: skip
    rows = []
    for x in book.get("positions") or []:
        if x.get("pending"):
            rows.append([x.get("name"), x.get("symbol"), x.get("exchange"), x.get("quote_ccy")]
                        + [None] * (len(cols) - 5) + ["waiting for Yahoo's split history"])  # fmt: skip
            continue
        rs = _maybe_num(x.get("risk_share"))
        rows.append([x.get("name"), x.get("symbol"), x.get("exchange"), x.get("quote_ccy"),
                     x.get("shares"), x.get("avg_price"), x.get("price"), x.get("value"),
                     _pct(x.get("weight")), rs, _pct(x.get("month_pct")), x.get("profit"),
                     x.get("profit_held"), x.get("profit_sold"), x.get("dividends"),
                     x.get("dividends_est"), _pct(x.get("return_pct")), contrib.get(x.get("symbol")),
                     _pct(x.get("yoc")), _pct(x.get("upside_pct"))])  # fmt: skip
    labels = [c[0] for c in cols]

    def line(**by_label):
        return [by_label.get(k) for k in labels]

    rows.append(line(Holding="Cash", **{"Quote ccy": ccy, f"Value ({ccy})": h.get("cash"),
                     "Weight": _pct(h.get("cash_weight")), "Risk share": 0.0,
                     f"Contribution {period} (pts)": contrib.get("_cash")}))  # fmt: skip
    rows.append(line(Holding="Total", **{f"Value ({ccy})": h.get("value"), "Weight": 1.0,
                     "Risk share": 1.0, "Month": _pct(h.get("month_pct")),
                     f"Profit ({ccy})": h.get("profit_total"),
                     f"Contribution {period} (pts)": (h.get("twr") or {}).get(period)}))  # fmt: skip
    r = _write_table(ws, 4, cols, rows)
    closed = book.get("closed") or []
    if closed:
        r = _write_section(ws, r + 1, "Closed")
        _write_table(ws, r, [("Holding", None), ("Ticker", None), (f"Profit ({ccy})", _MONEY),
                             ("On what you sold", _MONEY), ("Dividends", _MONEY),
                             ("Dividends, estimated", _MONEY), (f"Contribution {period} (pts)", "0.00")],
                     [[x.get("name"), x.get("symbol"), x.get("profit"), x.get("profit_sold"),
                       x.get("dividends"), x.get("dividends_est"), contrib.get(x.get("symbol"))]
                      for x in closed])  # fmt: skip


def _write_ledger(ws, book: dict, period: str) -> None:
    """Every ledger entry, oldest first, as entered (amounts in each
    entry's own currency), with its effect on cash in the book's base
    currency."""
    ws["A1"] = "My Investments — Ledger"
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = f"Generated: {datetime.now(UTC).isoformat(timespec='seconds')}"
    ws["A2"].font = Font(italic=True, color="6B7280")
    cols = [("Date", None), ("Type", None), ("Holding", None), ("Ticker", None), ("Quantity", _QTY),
            ("Price", _MONEY), ("Fee", _MONEY), ("Amount", _MONEY), ("Tax", _MONEY), ("Currency", None),
            ("FX rate", "0.000000"), ("Quantity basis", None), ("Cash effect", _MONEY),
            ("Source", None), ("Note", None)]  # fmt: skip
    rows = [[e.get("date"), e.get("type"), e.get("name"), e.get("symbol"), e.get("qty"),
             e.get("price"), e.get("fee"), e.get("amount"), e.get("tax"), e.get("ccy"),
             e.get("fx_rate"), e.get("qty_basis"), e.get("cash"), e.get("source"), e.get("note")]
            for e in reversed(book.get("entries") or [])]  # fmt: skip
    _write_table(ws, 4, cols, rows)
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["C"].width = 32


def _cap_weights(rows: list[dict]) -> dict[str, float]:
    """Market-cap weighting from row payloads. Symbols without a usable
    market cap get an equal share of the residual after the cap-weighted
    symbols sum, so the result is always normalised to 1.0."""
    caps: dict[str, float] = {}
    no_cap: list[str] = []
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        # In USD: raw caps are in each listing's own currency (the frontend's
        # capWeightsOf and analytics do the same).
        mc = _market_cap_usd(r)
        if mc and mc > 0:
            caps[sym] = mc
        else:
            no_cap.append(sym)
    total_cap = sum(caps.values())
    if total_cap <= 0:
        # All symbols capless → equal weights.
        n = max(1, len(rows))
        return {r["symbol"]: 1.0 / n for r in rows if r.get("symbol")}
    weights: dict[str, float] = {s: c / total_cap for s, c in caps.items()}
    if no_cap:
        # Reserve 5% of the book to share equally among capless tickers,
        # rescale the rest. Keeps capless rows from being silently dropped.
        share = 0.05 / len(no_cap)
        for s in no_cap:
            weights[s] = share
        scale = 0.95
        for s in caps:
            weights[s] *= scale
    return weights
