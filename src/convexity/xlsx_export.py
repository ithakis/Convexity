"""Excel export — one sheet per saved portfolio, intended to be the
"everything you can see in the app" artifact you'd hand to an AI/chatbot.

Design contract (matches the inline comment next to the Export button in
static/app.js — keep them in sync when extending):

  • Every saved portfolio becomes its own sheet (sheet name = portfolio
    name, Excel-sanitized).
  • Each sheet contains, top-to-bottom:
       1. Meta block: portfolio name, constituents string, cache info.
         2. Portfolio analytics summary with static 1Y and 5Y risk/return
             sections (Sharpe / drawdown / beta / ann return / etc. from
             analyze_portfolios_multi). Time-series payloads are deliberately
             excluded from the workbook.
       3. Holdings table: one row per ticker, every field the row payload
          carries, the two news reads (SENTIMENT_COLS: News read tier, one
          column per lens, Market read tier and z, the divergence sentence),
          PLUS analyst recommendations fetched at export time.
  • The first sheet ("Overview") lists every portfolio with the headline
    analytics side-by-side, for quick cross-portfolio comparison.

  • Every header carries a hover comment explaining the metric, and a last
    "Definitions" sheet lists them all (Metric | Formula | Meaning | Typical
    range). The text lives in COLUMN_DEFS / METRIC_DEFS below and mirrors the
    app's hover tips (COL_INFO / METRIC_INFO in static/app.js).

When new columns / analytics / fields land in the dashboard, add them
here so the file stays the comprehensive view. The format is forgiving:
unknown row keys go into the holdings table automatically (see
``HOLDINGS_PRIMARY_COLS`` + the "extras" pass at the end of each row).
A new column also needs a COLUMN_DEFS entry (and a readable EXTRA_LABELS
header if it is an extra): tests/test_xlsx_definitions.py fails without one.
"""

from __future__ import annotations

import io
import re
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterable, Optional

# openpyxl is an optional dep; the export endpoint will surface a friendly
# error if it's missing.
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import yfinance as yf

# Numeric coercion + dividend-yield normalisation live in helpers.py — the
# canonical copies. `_maybe_num` is an alias for `_safe_num` (identical finite-
# float-or-None semantics) kept only so the many call sites below read the same.
from convexity.analytics import _market_cap_usd
from convexity.fetcher import _positive
from convexity.helpers import _normalize_dividend_yield, major_ccy, _safe_num as _maybe_num


# --------------------------------------------------------------------------
# Holdings table — primary columns in display order. Anything else found on
# the row payload gets appended as an "extras" column so additions to
# fetch_one show up automatically.
# --------------------------------------------------------------------------

HOLDINGS_PRIMARY_COLS: list[tuple[str, str]] = [
    # (row_key, header_label)
    ("symbol", "Ticker"),
    ("name", "Company"),
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
    ("target_upside_pct", "Target Upside (%)"),
    ("forward_pe", "Forward P/E"),
    ("ev_ebitda", "EV/EBITDA"),
    ("peg", "PEG"),
    ("beta_info", "Beta (info)"),
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
    "beta": "Beta (Yahoo, 5Y monthly)",
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
        "Cov(stock, S&P 500) / Var(S&P 500), 5 years monthly",
        "Yahoo-reported beta against the S&P 500.",
        "1 moves with the market; above 1 amplifies it.",
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
    "beta_info": (
        "Cov(stock, S&P 500) / Var(S&P 500), 5 years monthly",
        "Yahoo-reported beta (the same figure as the Beta column).",
        "",
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
        "prod_t(1 + sum_i w_i r_i,t) - 1",
        "Total return over the period in the display currency, dividend-adjusted, rebalanced to the weights daily.",
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
    ("Ann Return %", "ann_return"),
    ("Ann Vol %", "ann_vol"),
    ("Sharpe", "sharpe"),
    ("Max DD %", "max_dd"),
    ("Excess vs SPY %", "excess_vs_spy"),
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
# Analyst-info fetch — lean wrapper that hits only Ticker.info, parallelised
# with a small worker pool and a per-process cache. fetch_detail() would
# also work but pulls full history which we don't need here.
# --------------------------------------------------------------------------

_ANALYST_CACHE: dict[str, dict] = {}
_ANALYST_TTL_S = 60 * 30  # 30 min


def _analyst_info_one(symbol: str) -> dict:
    now = time.time()
    cached = _ANALYST_CACHE.get(symbol)
    if cached and (now - cached.get("_t", 0)) < _ANALYST_TTL_S:
        return cached
    out: dict = {"_t": now}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tk = yf.Ticker(symbol)
            info = tk.info or {}
    except Exception:
        info = {}
    out["recommendation_key"] = (info.get("recommendationKey") or "").lower() or None
    out["recommendation_mean"] = _maybe_num(info.get("recommendationMean"))
    out["num_analysts"] = _maybe_num(info.get("numberOfAnalystOpinions"))
    out["target_mean"] = _maybe_num(info.get("targetMeanPrice"))
    out["target_high"] = _maybe_num(info.get("targetHighPrice"))
    out["target_low"] = _maybe_num(info.get("targetLowPrice"))
    # Negative multiples are not meaningful (fetcher._positive); Yahoo's
    # EV/EBITDA is only trusted when the statements share the listing
    # currency (see fetcher._currency_consistent).
    same_ccy = major_ccy(info.get("currency")) == major_ccy(
        info.get("financialCurrency") or info.get("currency")
    )
    out["forward_pe"] = _positive(info.get("forwardPE"))
    out["ev_ebitda"] = _positive(info.get("enterpriseToEbitda")) if same_ccy else None
    out["peg"] = _positive(info.get("pegRatio") or info.get("trailingPegRatio"))
    out["beta_info"] = _maybe_num(info.get("beta"))
    out["dividend_yield"] = _normalize_dividend_yield(
        info.get("dividendYield"),
        price=info.get("currentPrice") or info.get("regularMarketPrice"),
        dividend_rate=info.get("dividendRate"),
        trailing_yield=info.get("trailingAnnualDividendYield"),
        trailing_rate=info.get("trailingAnnualDividendRate"),
        currency=info.get("currency"),
        financial_currency=info.get("financialCurrency"),
    )
    _ANALYST_CACHE[symbol] = out
    return out


def gather_analyst_info(symbols: Iterable[str], *, max_workers: int = 5) -> dict[str, dict]:
    """Dedup, fetch info in parallel, return ``{symbol: analyst_dict}``."""
    uniq = sorted({s for s in symbols if s})
    if not uniq:
        return {}
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_analyst_info_one, s): s for s in uniq}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                out[s] = fut.result()
            except Exception:
                out[s] = {}
    return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_INVALID_SHEET_CHARS = re.compile(r"[\\/?*\[\]:]")


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
        # Compress lists into a count so the sheet stays readable.
        return f"[{len(v)} items]"
    if isinstance(v, dict):
        return f"[{len(v)} keys]"
    return str(v)


# --------------------------------------------------------------------------
# Sheet writers
# --------------------------------------------------------------------------

_HEADER_FILL = PatternFill("solid", fgColor="1F2937")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_SECTION_FONT = Font(bold=True, size=12)
_TITLE_FONT = Font(bold=True, size=14)


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


def _excess_vs_spy(analytics: dict) -> Optional[float]:
    if not isinstance(analytics, dict):
        return None
    port_tr = _maybe_num((analytics.get("stats") or {}).get("total_return"))
    spy_tr = _maybe_num(_bench(analytics, "SPY").get("stats", {}).get("total_return"))
    if port_tr is None or spy_tr is None:
        return None
    return port_tr - spy_tr


def _metric_periods(primary_period: str) -> list[str]:
    periods: list[str] = []
    for candidate in (primary_period, *EXPORT_STATS_PERIODS):
        period = str(candidate or "").upper()
        if period and period not in periods:
            periods.append(period)
    return periods or list(EXPORT_STATS_PERIODS)


def _write_overview(wb: Workbook, summaries: list[dict], metric_periods: list[str]) -> None:
    ws = wb.active
    ws.title = "Overview"
    ws["A1"] = "Convexity — Export"
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    ws["A2"].font = Font(italic=True, color="6B7280")

    headers = ["Portfolio", "Rows", "Cached At"]
    defs: list = [None, None, None]
    for period in metric_periods:
        for suffix, key in OVERVIEW_METRICS:
            headers.append(f"{period} {suffix}")
            defs.append(METRIC_DEFS[key])
    row = 4
    _write_header_cells(ws, row, headers, defs)
    for s in summaries:
        row += 1
        ws.cell(row=row, column=1, value=s.get("name"))
        ws.cell(row=row, column=2, value=s.get("rows"))
        ws.cell(row=row, column=3, value=s.get("saved_at"))
        col = 4
        analytics_by_period = s.get("analytics_by_period") or {}
        for period in metric_periods:
            analytics = analytics_by_period.get(period) or {}
            stats = analytics.get("stats") or {}
            ws.cell(row=row, column=col, value=_maybe_num(stats.get("ann_return")))
            col += 1
            ws.cell(row=row, column=col, value=_maybe_num(stats.get("ann_vol")))
            col += 1
            ws.cell(row=row, column=col, value=_maybe_num(stats.get("sharpe")))
            col += 1
            ws.cell(row=row, column=col, value=_maybe_num(stats.get("max_dd")))
            col += 1
            ws.cell(row=row, column=col, value=_excess_vs_spy(analytics))
            col += 1

    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18
    ws.column_dimensions["A"].width = 28


def _write_kv(ws, r: int, label: str, value, label_bold: bool = True, definition=None) -> int:
    """Single key/value row helper. Returns the next row index."""
    a = ws.cell(row=r, column=1, value=label)
    if label_bold:
        a.font = Font(bold=True)
    _add_comment(a, definition)
    ws.cell(row=r, column=2, value=value)
    return r + 1


def _write_section(ws, r: int, title: str) -> int:
    c = ws.cell(row=r, column=1, value=title)
    c.font = _SECTION_FONT
    return r + 1


def _write_header_cells(ws, r: int, labels: Iterable[str], defs: Iterable = ()) -> None:
    """Write one styled table-header row (bold white on dark, centered) from
    column 1. Shared by the overview, per-period metrics, and holdings tables
    so the three headers stay visually identical without repeating the
    fill/font/alignment triple at each call site. `defs`, aligned with
    `labels`, attaches each column's definition as a hover comment."""
    defs = list(defs)
    for col_idx, label in enumerate(labels, 1):
        c = ws.cell(row=r, column=col_idx, value=label)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
        c.alignment = Alignment(horizontal="center")
        if col_idx <= len(defs):
            _add_comment(c, defs[col_idx - 1])


def _per_symbol_analyst(analytics: dict) -> dict[str, dict]:
    """Pull the per-holding analyst rows out of analytics["analyst"]["holdings"]
    and key them by ticker, so the holdings table can look them up cheaply."""
    out: dict[str, dict] = {}
    if not isinstance(analytics, dict):
        return out
    ana = analytics.get("analyst") or {}
    for h in ana.get("holdings") or []:
        if isinstance(h, dict) and h.get("symbol"):
            out[h["symbol"]] = h
    return out


def _write_portfolio_sheet(
    wb: Workbook,
    sheet_name: str,
    portfolio_name: str,
    view: dict,
    analytics_by_period: dict[str, dict],
    metric_periods: list[str],
    analyst_fallback: dict[str, dict],
    weighting_label: str = "cap-weighted",
) -> None:
    ws = wb.create_sheet(title=sheet_name)
    rows = view.get("rows") or []
    entries = view.get("entries") or ""
    saved_at = view.get("saved_at") or ""
    analytics_by_period = analytics_by_period or {}
    primary_analytics = _first_available_analytics(analytics_by_period, metric_periods)
    analyst_block = primary_analytics.get("analyst") or {}
    analyst_by_sym = _per_symbol_analyst(primary_analytics)
    exposure = primary_analytics.get("exposure") or {}
    concentration = primary_analytics.get("concentration") or {}
    has_analytics = any(a and not a.get("error") for a in analytics_by_period.values())

    # 1) Meta block
    r = 1
    c = ws.cell(row=r, column=1, value=portfolio_name)
    c.font = _TITLE_FONT
    r += 1
    r = _write_kv(ws, r, "Constituents:", entries)
    ws.cell(row=r - 1, column=2).alignment = Alignment(wrap_text=True)
    r = _write_kv(ws, r, "Cached at:", saved_at)
    r = _write_kv(ws, r, "Holdings:", len(rows))
    r += 1

    # 2) Portfolio metrics + benchmark comparison side-by-side.
    if not has_analytics:
        errors = [
            a.get("error")
            for a in analytics_by_period.values()
            if isinstance(a, dict) and a.get("error")
        ]
        msg = (
            f"(analytics unavailable: {errors[0]})"
            if errors
            else "(no analytics computed for this export)"
        )
        ws.cell(row=r, column=1, value=msg).font = Font(italic=True, color="9CA3AF")
        r += 2
    else:
        for period in metric_periods:
            analytics = analytics_by_period.get(period) or {}
            r = _write_section(ws, r, f"Portfolio Metrics ({weighting_label}, {period})")
            if analytics.get("error"):
                ws.cell(
                    row=r, column=1, value=f"(analytics unavailable: {analytics['error']})"
                ).font = Font(italic=True, color="9CA3AF")
                r += 2
                continue
            spy = _bench(analytics, "SPY")
            columns = [
                {**(analytics.get("stats") or {}), **(spy.get("rel") or {})},
                spy.get("stats") or {},
                _bench(analytics, "QQQ").get("stats") or {},
            ]
            _write_header_cells(ws, r, ["Metric", "Portfolio", "SPY", "NASDAQ"])
            r += 1
            for key, label in STATS_KEYS:
                lc = ws.cell(row=r, column=1, value=label)
                lc.font = Font(bold=True)
                _add_comment(lc, METRIC_DEFS.get(key))
                for col, stats in enumerate(columns, start=2):
                    ws.cell(row=r, column=col, value=_maybe_num(stats.get(key)))
                r += 1
            r += 1

        # 3) Aggregated analyst coverage
        if analyst_block:
            r = _write_section(ws, r, "Analyst Coverage (portfolio-weighted)")
            for key, label in ANALYST_AGG_KEYS:
                v = analyst_block.get(key)
                if v is None:
                    continue
                r = _write_kv(ws, r, label, _maybe_num(v), definition=METRIC_DEFS.get(key))
            # Rating distribution
            dist = analyst_block.get("distribution_pct") or {}
            if dist:
                ws.cell(row=r, column=1, value="Rating Distribution (%)").font = Font(bold=True)
                r += 1
                for k in ("strongBuy", "buy", "hold", "sell", "strongSell"):
                    if k in dist:
                        ws.cell(row=r, column=1, value=f"  {k}")
                        ws.cell(row=r, column=2, value=_maybe_num(dist.get(k)))
                        r += 1
            r += 1

        # 4) Sector / industry exposure
        sec = exposure.get("by_sector") if isinstance(exposure, dict) else None
        if sec:
            r = _write_section(ws, r, "Sector Exposure (weight)")
            for sname, w in sorted(sec.items(), key=lambda kv: kv[1], reverse=True):
                ws.cell(row=r, column=1, value=sname)
                ws.cell(row=r, column=2, value=_maybe_num(w))
                r += 1
            r += 1

        # 5) Concentration
        if concentration:
            r = _write_section(ws, r, "Concentration")
            for key, label in CONCENTRATION_KEYS:
                v = concentration.get(key)
                if v is None:
                    continue
                r = _write_kv(ws, r, label, _maybe_num(v), definition=METRIC_DEFS.get(key))
            r += 1

    # 6) Holdings table — every row field + per-symbol analyst data
    r = _write_section(ws, r, "Holdings")

    # Discover any extra row-payload keys not in HOLDINGS_PRIMARY_COLS, so
    # future fetch_one extensions show up automatically.
    primary_keys = {k for k, _ in HOLDINGS_PRIMARY_COLS}
    extra_keys_all = set()
    for row in rows:
        if isinstance(row, dict):
            extra_keys_all.update(row.keys())
    extra_keys = sorted(extra_keys_all - primary_keys - HOLDINGS_SKIP_EXTRAS)
    extra_cols = [(k, EXTRA_LABELS.get(k, k)) for k in extra_keys]

    headers = HOLDINGS_PRIMARY_COLS + SENTIMENT_COLS + extra_cols + ANALYST_COLS
    _write_header_cells(
        ws, r, [label for _key, label in headers], [COLUMN_DEFS.get(key) for key, _ in headers]
    )
    header_row = r

    analyst_col_keys = {k for k, _ in ANALYST_COLS}

    for row_data in rows:
        if not isinstance(row_data, dict):
            continue
        r += 1
        sym = row_data.get("symbol")
        # Prefer analytics-engine per-symbol data (already fetched, richer);
        # fall back to the parallel info pull only if analytics wasn't run.
        ana_row = analyst_by_sym.get(sym) or {}
        fallback_row = analyst_fallback.get(sym, {}) if not ana_row else {}
        for col_idx, (key, _label) in enumerate(headers, 1):
            if key in analyst_col_keys:
                # Map our column key to whatever the source row uses.
                v = None
                if key == "target_upside_pct":
                    v = ana_row.get("upside_pct")
                    if v is None and fallback_row:
                        price = _maybe_num(row_data.get("price"))
                        tgt = _maybe_num(fallback_row.get("target_mean"))
                        if price and tgt and price > 0:
                            v = (tgt / price - 1.0) * 100.0
                # The row's own fields are the last resort: they are no longer
                # exported as extras (HOLDINGS_SKIP_EXTRAS), so a failed info
                # pull must not leave these columns empty.
                elif key == "recommendation_key":
                    v = (
                        ana_row.get("rec_key")
                        or fallback_row.get("recommendation_key")
                        or row_data.get("rec_key")
                    )
                elif key == "recommendation_mean":
                    v = (
                        ana_row.get("mean_rating")
                        or fallback_row.get("recommendation_mean")
                        or row_data.get("recommendation_mean")
                    )
                elif key == "num_analysts":
                    v = (
                        ana_row.get("n_analysts")
                        or fallback_row.get("num_analysts")
                        or row_data.get("n_analysts")
                    )
                elif key in {"target_mean", "target_high", "target_low"}:
                    v = ana_row.get(key) or fallback_row.get(key)
                else:
                    # forward_pe, ev_ebitda, peg, beta_info, dividend_yield —
                    # not in the per-portfolio analyst block. The row carries
                    # them (fetch_one, with the ADR currency repair); the info
                    # pull only fills a row that lacks them. Reading only the
                    # fallback left these columns empty whenever analytics ran.
                    rv = row_data.get("beta" if key == "beta_info" else key)
                    v = rv if rv is not None else analyst_fallback.get(sym, {}).get(key)
            elif key.startswith("ns:"):
                v = _sentiment_value(key, row_data.get("news_sentiment"))
            else:
                v = row_data.get(key)
            cell = ws.cell(row=r, column=col_idx, value=_flatten_value(v))
            if key in FRACTION_KEYS and isinstance(v, (int, float)):
                cell.number_format = "0.00%"

    # Approximate column widths.
    for col_idx, (_key, label) in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(12, len(str(label)) + 2)
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 32
    # Freeze the header row + the ticker column.
    ws.freeze_panes = ws.cell(row=header_row + 1, column=2)


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
        ("Portfolio metrics", STATS_KEYS + [("excess_vs_spy", "Excess vs SPY (%)")]),
        ("Analyst coverage", ANALYST_AGG_KEYS),
        ("Concentration", CONCENTRATION_KEYS),
    ):
        for key, label in keys:
            d = METRIC_DEFS.get(key)
            if d:
                out.append((section, label, *d))
    return out


def _write_definitions(wb: Workbook, sheet_name: str) -> None:
    """The last sheet: every metric in the workbook, with its formula, what it
    means and a typical range, so the file explains itself when handed on."""
    ws = wb.create_sheet(title=sheet_name)
    ws["A1"] = "Definitions"
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = "The same text is on each header as a hover comment."
    ws["A2"].font = Font(italic=True, color="6B7280")
    r = 4
    _write_header_cells(ws, r, ["Section", "Metric", "Formula", "Meaning", "Typical range"])
    wrap = Alignment(wrap_text=True, vertical="top")
    for row in definition_rows():
        r += 1
        for col, v in enumerate(row, 1):
            c = ws.cell(row=r, column=col, value=v or None)
            c.alignment = wrap
        ws.cell(row=r, column=2).font = Font(bold=True)
    for col, width in zip("ABCDE", (18, 30, 44, 60, 44)):
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
                return ({s: w / total for s, w in projected.items()}, active_name, active_name)
    # No usable preset → market-cap weighting (matches the frontend fallback).
    return (_cap_weights(rows), "cap", "cap-weighted")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def build_workbook(
    views_map: dict[str, dict],
    *,
    analytics_runner=None,
    period: str = "1Y",
    display_ccy: str = "USD",
) -> bytes:
    """Build an .xlsx blob covering every saved portfolio.

    Parameters
    ----------
    views_map
        ``{portfolio_name: view_dict}`` — view_dict has keys ``entries``,
        ``rows``, ``saved_at``, ``stale``.
    analytics_runner
        Optional callable ``(rows, weight_sets, period, display_ccy) -> dict``
        used to compute the analytics block per portfolio. Pass
        ``dashboard.analyze_portfolios_multi`` here. When omitted, sheets
        still build but the analytics block notes that nothing was run.
    period, display_ccy
        Forwarded to ``analytics_runner``.
    """
    wb = Workbook()
    used_names: set[str] = set()
    metric_periods = _metric_periods(period)

    # Pre-pass: collect valid views + every unique symbol across portfolios.
    valid_views: list[tuple[str, dict]] = []
    all_symbols: set[str] = set()
    for name, view in views_map.items():
        if not isinstance(view, dict):
            continue
        rows = view.get("rows") or []
        if not rows:
            continue
        valid_views.append((name, view))
        for r in rows:
            if isinstance(r, dict) and r.get("symbol"):
                all_symbols.add(str(r["symbol"]))

    # Run analytics per portfolio. Track which symbols got per-row analyst
    # data from the engine (the rich `analyst.holdings` block) so we only
    # do the redundant info-only fetch for the leftovers.
    summaries: list[dict] = []
    # (name, view, analytics_by_period, weighting_label)
    portfolio_analytics: list[tuple[str, dict, dict[str, dict], str]] = []
    covered_symbols: set[str] = set()
    for name, view in valid_views:
        rows = view.get("rows") or []
        # Use the portfolio's own saved active weighting, not a blanket cap
        # assumption, so the exported metrics match what the user sees.
        weights, set_name, weighting_label = _resolve_active_weights(view, rows)
        analytics_by_period: dict[str, dict] = {}
        if analytics_runner is not None:
            for analytics_period in metric_periods:
                try:
                    result = analytics_runner(
                        rows, {set_name: weights}, analytics_period, display_ccy
                    )
                    analytics_by_period[analytics_period] = _analytics_result_for_period(
                        result, set_name
                    )
                except Exception as exc:
                    analytics_by_period[analytics_period] = {"error": f"runner failed: {exc}"}
        portfolio_analytics.append((name, view, analytics_by_period, weighting_label))
        # Symbols already covered by analytics["analyst"]["holdings"].
        for analytics in analytics_by_period.values():
            for h in (analytics.get("analyst") or {}).get("holdings") or []:
                if isinstance(h, dict) and h.get("symbol"):
                    covered_symbols.add(h["symbol"])

    # Fallback info fetch ONLY for symbols not covered by the engine's
    # per-holding analyst block. Avoids the slow redundant info call when
    # analytics succeeded for every symbol (the common case).
    leftovers = all_symbols - covered_symbols
    analyst_fallback = gather_analyst_info(leftovers) if leftovers else {}

    # Write per-portfolio sheets + summaries.
    for name, view, analytics_by_period, weighting_label in portfolio_analytics:
        sheet_name = _sanitize_sheet_name(name, used_names)
        _write_portfolio_sheet(
            wb,
            sheet_name,
            name,
            view,
            analytics_by_period,
            metric_periods,
            analyst_fallback,
            weighting_label,
        )
        summaries.append(
            {
                "name": name,
                "rows": len(view.get("rows") or []),
                "saved_at": view.get("saved_at"),
                "analytics_by_period": analytics_by_period,
            }
        )

    _write_overview(wb, summaries, metric_periods)
    # Move Overview to the front (openpyxl appends it after creation).
    wb.move_sheet("Overview", offset=-len(wb.sheetnames) + 1)
    _write_definitions(wb, _sanitize_sheet_name("Definitions", used_names))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


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
