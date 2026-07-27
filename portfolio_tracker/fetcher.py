"""Data fetching — per-symbol rows, portfolio batches, and detail modal payloads."""

from __future__ import annotations

import math
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from portfolio_tracker.cache import (
    _BENCH_CACHE, _BENCH_TTL,
    _CACHE_TTL_ANALYTICS,
    _cache_get, _cache_put,
)
from portfolio_tracker.helpers import (
    _bollinger_pct_b,
    _is_rate_limited_error,
    _macd_hist_pct,
    _normalize_dividend_yield,
    _pct_change,
    _rsi,
    _safe_num,
    _series_to_points,
    _ytd_change,
)
from portfolio_tracker.resolver import _ordered_resolve

try:
    from portfolio_tracker import finnhub_adapter as _fh
except ImportError:
    _fh = None

try:
    from portfolio_tracker import news_sentiment as _ns
except ImportError:
    _ns = None


_SECTOR_ETF = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Financial": "XLF",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Communication Services": "XLC",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}


def _safe_info(tk: yf.Ticker) -> dict:
    info: dict = {}
    try:
        raw = tk.info
        if isinstance(raw, dict):
            info = raw
    except Exception:
        info = {}
    try:
        fi = tk.fast_info
        if fi is not None:
            for k in ("market_cap", "currency", "exchange", "shares", "last_price"):
                try:
                    v = getattr(fi, k, None)
                except Exception:
                    v = None
                if v is not None and info.get(k) in (None, ""):
                    info[k] = v
    except Exception:
        pass
    return info


def _earnings_surprise_yf(tk: yf.Ticker, symbol: str) -> list[dict] | None:
    """Up to 8 quarters of EPS surprise from yfinance, most-recent first."""
    cache_key = f"eps_surprise|{symbol}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached.get("v")
    try:
        df = tk.get_earnings_dates(limit=24)
    except Exception:
        return None
    if df is None or getattr(df, "empty", True):
        return None
    out: list[dict] = []
    try:
        for ts, row in df.iterrows():
            actual = _safe_num(row.get("Reported EPS"))
            if actual is None:
                continue
            estimate = _safe_num(row.get("EPS Estimate"))
            if estimate in (None, 0):
                surprise_pct = None
            else:
                surprise_pct = (actual - estimate) / abs(estimate) * 100.0
            period = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            out.append({"period": period, "actual": actual,
                        "estimate": estimate, "surprise_pct": surprise_pct})
            if len(out) >= 8:
                break
    except Exception:
        return None
    if not out:
        return None
    _cache_put(cache_key, {"v": out}, _CACHE_TTL_ANALYTICS)
    return out


def fetch_one(symbol: str, max_attempts: int = 3) -> dict:
    out: dict = {"symbol": symbol}
    last_err: str | None = None
    for attempt in range(max_attempts):
        try:
            tk = yf.Ticker(symbol)
            hist = tk.history(period="2y", auto_adjust=True, actions=False)
            if hist is None or getattr(hist, "empty", True):
                last_err = "no data"
                time.sleep(0.7 + attempt * 1.2 + random.random() * 0.3)
                continue
            close = hist["Close"].dropna()
            if close.empty:
                last_err = "no data"
                time.sleep(0.7 + attempt * 1.2)
                continue

            last = float(close.iloc[-1])
            prev_close = float(close.iloc[-2]) if len(close) >= 2 else last
            out["price"] = last
            out["change_abs_1d"] = last - prev_close
            out["pct_1d"] = (last / prev_close - 1.0) * 100.0 if prev_close else None
            # 2-day return: trading bars, not calendar days (unlike pct_1w and
            # friends below, which are calendar offsets). Over a 2-bar horizon a
            # calendar window would silently swallow weekends and holidays.
            prev2 = float(close.iloc[-3]) if len(close) >= 3 else None
            out["pct_2d"] = (last / prev2 - 1.0) * 100.0 if prev2 else None
            out["pct_1w"] = _pct_change(close, 7)
            out["pct_1m"] = _pct_change(close, 30)
            out["pct_3m"] = _pct_change(close, 91)
            out["pct_6m"] = _pct_change(close, 182)
            out["pct_ytd"] = _ytd_change(close)
            out["pct_1y"] = _pct_change(close, 365)

            last_year = close[close.index >= (close.index[-1] - pd.Timedelta(days=365))]
            if not last_year.empty:
                out["w52_high"] = float(last_year.max())
                out["w52_low"] = float(last_year.min())
            ath = float(close.max())
            out["ath"] = ath
            out["delta_ath"] = (last / ath - 1.0) * 100.0 if ath else None

            out["sparkline"] = [float(x) for x in close.tail(252).to_list() if pd.notna(x)]
            try:
                out["volume"] = float(hist["Volume"].iloc[-1]) if "Volume" in hist else None
            except Exception:
                out["volume"] = None

            out["rsi_14"] = _rsi(close, 14)
            out["macd_hist_pct"] = _macd_hist_pct(close)
            out["bb_pct_b"] = _bollinger_pct_b(close)

            for n in (20, 50, 200):
                if len(close) >= n:
                    sma_val = float(close.tail(n).mean())
                    out[f"sma_{n}"] = sma_val
                    out[f"above_sma_{n}"] = last > sma_val
                else:
                    out[f"sma_{n}"] = None
                    out[f"above_sma_{n}"] = None

            if len(close) >= 22:
                out["above_1m"] = last > float(close.iloc[-22])
            elif len(close) >= 2:
                out["above_1m"] = last > float(close.iloc[0])
            else:
                out["above_1m"] = None

            monthly = close.resample("ME").last().dropna().tail(12)
            rs: list[float] = []
            for ts in monthly.index:
                window = close[(close.index <= ts) & (close.index >= ts - pd.Timedelta(days=365))]
                if len(window) < 2:
                    continue
                lo, hi = float(window.min()), float(window.max())
                v = float(monthly.loc[ts])
                rs.append(0.0 if hi == lo else (v - lo) / (hi - lo))
            out["rs_rank"] = rs

            info = _safe_info(tk)
            out["name"] = info.get("longName") or info.get("shortName") or symbol
            out["market_cap"] = info.get("marketCap") or info.get("market_cap")
            out["currency"] = info.get("currency") or "USD"
            out["exchange"] = info.get("exchange") or info.get("fullExchangeName") or ""
            out["sector"] = info.get("sector") or ""
            out["industry"] = info.get("industry") or ""
            out["quote_type"] = (info.get("quoteType") or "").upper()
            out["website"] = info.get("website") or ""

            ps = info.get("priceToSalesTrailing12Months")
            try:
                out["ps_ratio"] = float(ps) if ps not in (None, "") else None
            except (TypeError, ValueError):
                out["ps_ratio"] = None
            pe = info.get("trailingPE") or info.get("forwardPE")
            try:
                out["pe_ratio"] = float(pe) if pe not in (None, "") and float(pe) > 0 else None
            except (TypeError, ValueError):
                out["pe_ratio"] = None

            out["forward_pe"] = _safe_num(info.get("forwardPE"))
            out["peg"] = _safe_num(info.get("pegRatio") or info.get("trailingPegRatio"))
            out["ev_ebitda"] = _safe_num(info.get("enterpriseToEbitda"))
            out["ev_revenue"] = _safe_num(info.get("enterpriseToRevenue"))
            out["beta"] = _safe_num(info.get("beta"))
            out["dividend_yield"] = _normalize_dividend_yield(
                info.get("dividendYield"),
                price=last,
                dividend_rate=info.get("dividendRate"),
                trailing_yield=info.get("trailingAnnualDividendYield"),
                trailing_rate=info.get("trailingAnnualDividendRate"),
            )
            out["operating_margin"] = _safe_num(info.get("operatingMargins"))
            out["debt_equity"] = _safe_num(info.get("debtToEquity"))
            out["current_ratio"] = _safe_num(info.get("currentRatio"))
            # Extended fundamentals (all straight from the same info dict —
            # zero extra network cost on the streaming path). Margins/growth/
            # returns come back as fractions (0.42 = 42%); the frontend
            # formats them, never re-detects units.
            out["price_book"] = _safe_num(info.get("priceToBook"))
            out["roe"] = _safe_num(info.get("returnOnEquity"))
            out["roa"] = _safe_num(info.get("returnOnAssets"))
            out["gross_margin"] = _safe_num(info.get("grossMargins"))
            out["profit_margin"] = _safe_num(info.get("profitMargins"))
            out["revenue_growth"] = _safe_num(info.get("revenueGrowth"))
            # Prefer annual earningsGrowth; fall back to quarterly ONLY when
            # annual is truly absent — `or` would wrongly discard a legitimate
            # 0.0 (flat YoY) and substitute the quarterly figure instead.
            _eg = info.get("earningsGrowth")
            if _eg is None:
                _eg = info.get("earningsQuarterlyGrowth")
            out["earnings_growth"] = _safe_num(_eg)
            out["quick_ratio"] = _safe_num(info.get("quickRatio"))
            out["payout_ratio"] = _safe_num(info.get("payoutRatio"))
            # FCF yield = free cash flow / market cap, both from info. Only
            # computed when both legs are present and positive-denominator.
            fcf = _safe_num(info.get("freeCashflow"))
            mcap = _safe_num(out.get("market_cap"))
            out["fcf_yield"] = (fcf / mcap) if (fcf is not None and mcap) else None
            out["recommendation_mean"] = _safe_num(info.get("recommendationMean"))
            out["target_mean_price"] = _safe_num(info.get("targetMeanPrice"))
            out["rec_key"] = (info.get("recommendationKey") or "").strip().lower() or None
            out["n_analysts"] = _safe_num(info.get("numberOfAnalystOpinions"))

            out["earnings_surprise"] = _earnings_surprise_yf(tk, symbol)

            rating_dist = None
            try:
                recs = tk.recommendations
                if recs is not None and not recs.empty:
                    rec_row = recs.iloc[0]
                    row_lower = {str(k).lower(): v for k, v in rec_row.items()}
                    def _rd_int(key):
                        v = row_lower.get(key, 0)
                        if v is None or (isinstance(v, float) and math.isnan(v)):
                            return 0
                        try:
                            return int(v)
                        except (TypeError, ValueError):
                            return 0
                    d = {
                        "strongBuy":  _rd_int("strongbuy"),
                        "buy":        _rd_int("buy"),
                        "hold":       _rd_int("hold"),
                        "sell":       _rd_int("sell"),
                        "strongSell": _rd_int("strongsell"),
                    }
                    if any(d.values()):
                        rating_dist = d
            except Exception:
                pass
            out["rating_dist"] = rating_dist

            if _fh is not None:
                out["insider_mspr"] = _fh.get_insider_sentiment(symbol)
                out["rec_trend_fh"] = _fh.get_recommendation_trend(symbol)
            else:
                out["insider_mspr"] = None
                out["rec_trend_fh"] = None

            # Cache-only read — never triggers a fetch from the hot path.
            out["news_sentiment"] = _ns.get_cached_sentiment(symbol) if _ns else None

            return out

        except Exception as exc:
            msg = str(exc)
            last_err = msg[:200] if msg else type(exc).__name__
            if _is_rate_limited_error(exc):
                time.sleep(2.0 + attempt * 2.0 + random.random() * 0.5)
            else:
                time.sleep(0.6 + attempt * 1.2 + random.random() * 0.4)

    out["error"] = last_err or "fetch failed"
    return out


def fetch_portfolio(entries: list[str]) -> list[dict]:
    cache_key = "|".join(sorted(set(entries)))
    cached = _cache_get(cache_key)
    if cached:
        return cached["rows"]
    symbols = _ordered_resolve(entries)
    rows: list[dict] = []
    if symbols:
        workers = min(5, len(symbols))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_one, s): s for s in symbols}
            for fut in as_completed(futures):
                rows.append(fut.result())
        order = {s: i for i, s in enumerate(symbols)}
        rows.sort(key=lambda r: order.get(r["symbol"], 9999))
    _cache_put(cache_key, {"rows": rows})
    return rows


# ----------------------------- Detail fetch ---------------------------------


def _fetch_bench_history(symbol: str, period: str = "10y") -> pd.Series | None:
    key = f"{symbol}|{period}"
    hit = _BENCH_CACHE.get(key)
    if hit and time.time() - hit[0] <= _BENCH_TTL:
        return hit[1].get("close")
    try:
        tk = yf.Ticker(symbol)
        hist = tk.history(period=period, auto_adjust=True, actions=False)
        if hist is None or hist.empty:
            return None
        close = hist["Close"].dropna()
        _BENCH_CACHE[key] = (time.time(), {"close": close})
        return close
    except Exception:
        return None


def _aligned_pct(a: pd.Series, b: pd.Series, days: int) -> tuple[float | None, float | None]:
    if a is None or a.empty or b is None or b.empty:
        return (None, None)
    end = min(a.index[-1], b.index[-1])
    start_target = end - pd.Timedelta(days=days)
    a_w = a[a.index <= end]
    b_w = b[b.index <= end]
    a_p = a_w[a_w.index <= start_target]
    b_p = b_w[b_w.index <= start_target]
    if a_p.empty or b_p.empty:
        return (None, None)
    return (
        float((a_w.iloc[-1] / a_p.iloc[-1] - 1.0) * 100.0),
        float((b_w.iloc[-1] / b_p.iloc[-1] - 1.0) * 100.0),
    )


def _beta_corr(stock_close: pd.Series, bench_close: pd.Series) -> tuple[float | None, float | None]:
    if stock_close is None or bench_close is None:
        return (None, None)
    df = pd.concat([stock_close.rename("s"), bench_close.rename("b")], axis=1, join="inner").dropna()
    if len(df) < 30:
        return (None, None)
    df = df.tail(252)
    rs = df["s"].pct_change().dropna()
    rb = df["b"].pct_change().dropna()
    common = rs.index.intersection(rb.index)
    rs, rb = rs.loc[common], rb.loc[common]
    if len(rs) < 20:
        return (None, None)
    var_b = float(rb.var())
    cov = float(rs.cov(rb))
    beta = cov / var_b if var_b else None
    try:
        corr = float(rs.corr(rb))
    except Exception:
        corr = None
    return (beta, corr)


def _statement_values(df: pd.DataFrame | None, labels: list[str]) -> list[float]:
    if df is None or getattr(df, "empty", True):
        return []
    for label in labels:
        if label not in df.index:
            continue
        row = df.loc[label]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        if not isinstance(row, pd.Series):
            continue
        vals: list[float] = []
        for raw in row.tolist():
            num = _safe_num(raw)
            if num is not None:
                vals.append(num)
        if vals:
            return vals
    return []


def _latest_statement_value(df: pd.DataFrame | None, labels: list[str]) -> float | None:
    vals = _statement_values(df, labels)
    return vals[0] if vals else None


def _ttm_statement_value(df: pd.DataFrame | None, labels: list[str]) -> float | None:
    vals = _statement_values(df, labels)
    if len(vals) >= 4:
        return float(sum(vals[:4]))
    return vals[0] if vals else None


def fetch_detail(symbol: str) -> dict:
    cache_key = f"detail|{symbol}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    out: dict = {"symbol": symbol}
    tk = yf.Ticker(symbol)

    try:
        hist = tk.history(period="max", auto_adjust=True, actions=False)
    except Exception:
        hist = None
    if hist is None or hist.empty:
        try:
            hist = tk.history(period="10y", auto_adjust=True, actions=False)
        except Exception:
            hist = None
    if hist is None or hist.empty:
        out["error"] = "no history"
        return out

    close = hist["Close"].dropna()
    vol = hist["Volume"] if "Volume" in hist else pd.Series(dtype=float)
    out["history"] = _series_to_points(close)
    out["volume"] = _series_to_points(vol.dropna()) if not vol.empty else []

    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) >= 2 else last
    out["price"] = last
    out["prev_close"] = prev
    out["change_abs"] = last - prev
    out["pct_1d"] = (last / prev - 1.0) * 100.0 if prev else None
    prev2 = float(close.iloc[-3]) if len(close) >= 3 else None
    out["pct_2d"] = (last / prev2 - 1.0) * 100.0 if prev2 else None

    today = hist.iloc[-1]
    out["day_open"] = _safe_num(today.get("Open"))
    out["day_high"] = _safe_num(today.get("High"))
    out["day_low"] = _safe_num(today.get("Low"))
    out["day_volume"] = _safe_num(today.get("Volume"))

    if "Volume" in hist:
        try:
            out["avg_volume"] = float(hist["Volume"].tail(30).mean())
        except Exception:
            out["avg_volume"] = None

    last_year = close[close.index >= (close.index[-1] - pd.Timedelta(days=365))]
    if not last_year.empty:
        out["w52_high"] = float(last_year.max())
        out["w52_low"] = float(last_year.min())
    out["ath"] = float(close.max())
    out["atl"] = float(close.min())

    info = _safe_info(tk)
    out["name"] = info.get("longName") or info.get("shortName") or symbol
    out["currency"] = info.get("currency") or "USD"
    out["exchange"] = info.get("exchange") or info.get("fullExchangeName") or ""
    out["sector"] = info.get("sector") or ""
    out["industry"] = info.get("industry") or ""
    out["website"] = info.get("website") or ""
    out["summary"] = (info.get("longBusinessSummary") or "")[:600]
    out["country"] = info.get("country") or ""
    out["employees"] = _safe_num(info.get("fullTimeEmployees"))
    out["quote_type"] = (info.get("quoteType") or "").upper()

    out["market_cap"] = _safe_num(info.get("marketCap") or info.get("market_cap"))
    out["shares"] = _safe_num(info.get("sharesOutstanding"))
    out["float_shares"] = _safe_num(info.get("floatShares"))

    out["pe"] = _safe_num(info.get("trailingPE"))
    out["forward_pe"] = _safe_num(info.get("forwardPE"))
    out["ps"] = _safe_num(info.get("priceToSalesTrailing12Months"))
    out["pb"] = _safe_num(info.get("priceToBook"))
    out["peg"] = _safe_num(info.get("pegRatio") or info.get("trailingPegRatio"))
    out["ev_ebitda"] = _safe_num(info.get("enterpriseToEbitda"))
    out["ev_revenue"] = _safe_num(info.get("enterpriseToRevenue"))

    out["beta"] = _safe_num(info.get("beta"))
    out["dividend_yield"] = _normalize_dividend_yield(
        info.get("dividendYield"),
        price=out.get("price") or info.get("currentPrice") or info.get("regularMarketPrice") or info.get("last_price"),
        dividend_rate=info.get("dividendRate"),
        trailing_yield=info.get("trailingAnnualDividendYield"),
        trailing_rate=info.get("trailingAnnualDividendRate"),
    )
    out["dividend_rate"] = _safe_num(info.get("dividendRate"))
    out["payout_ratio"] = _safe_num(info.get("payoutRatio"))
    ex_div = info.get("exDividendDate")
    if ex_div:
        try:
            out["ex_div_date"] = datetime.fromtimestamp(int(ex_div), tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            out["ex_div_date"] = None

    out["profit_margin"] = _safe_num(info.get("profitMargins"))
    out["operating_margin"] = _safe_num(info.get("operatingMargins"))
    out["gross_margin"] = _safe_num(info.get("grossMargins"))
    out["roe"] = _safe_num(info.get("returnOnEquity"))
    out["roa"] = _safe_num(info.get("returnOnAssets"))
    out["debt_equity"] = _safe_num(info.get("debtToEquity"))
    out["current_ratio"] = _safe_num(info.get("currentRatio"))
    out["revenue_growth"] = _safe_num(info.get("revenueGrowth"))
    out["earnings_growth"] = _safe_num(info.get("earningsGrowth"))
    out["total_revenue"] = _safe_num(info.get("totalRevenue"))
    out["free_cashflow"] = _safe_num(info.get("freeCashflow"))

    if out["roe"] is None or out["debt_equity"] is None:
        try:
            balance_sheet = tk.balance_sheet
        except Exception:
            balance_sheet = None
        try:
            quarterly_balance_sheet = tk.quarterly_balance_sheet
        except Exception:
            quarterly_balance_sheet = None
        try:
            income_stmt = tk.income_stmt
        except Exception:
            income_stmt = None
        try:
            quarterly_income_stmt = tk.quarterly_income_stmt
        except Exception:
            quarterly_income_stmt = None

        equity_labels = [
            "Stockholders Equity", "Common Stock Equity",
            "Total Stockholder Equity", "Total Equity Gross Minority Interest",
        ]
        debt_labels = ["Total Debt"]
        current_debt_labels = ["Current Debt", "Current Debt And Capital Lease Obligation"]
        long_debt_labels = ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"]
        net_income_labels = [
            "Net Income", "Diluted NI Availto Com Stockholders",
            "Net Income Common Stockholders", "Net Income Continuous Operations",
        ]

        equity = _latest_statement_value(balance_sheet, equity_labels)
        if equity is None:
            equity = _latest_statement_value(quarterly_balance_sheet, equity_labels)

        debt = _latest_statement_value(balance_sheet, debt_labels)
        if debt is None:
            debt = _latest_statement_value(quarterly_balance_sheet, debt_labels)
        if debt is None:
            current_debt = _latest_statement_value(balance_sheet, current_debt_labels)
            long_debt = _latest_statement_value(balance_sheet, long_debt_labels)
            if current_debt is None:
                current_debt = _latest_statement_value(quarterly_balance_sheet, current_debt_labels)
            if long_debt is None:
                long_debt = _latest_statement_value(quarterly_balance_sheet, long_debt_labels)
            if current_debt is not None or long_debt is not None:
                debt = float((current_debt or 0.0) + (long_debt or 0.0))

        net_income = _latest_statement_value(income_stmt, net_income_labels)
        if net_income is None:
            net_income = _ttm_statement_value(quarterly_income_stmt, net_income_labels)

        if out["roe"] is None and equity not in (None, 0) and net_income is not None:
            out["roe"] = float(net_income / equity)
        if out["debt_equity"] is None and equity not in (None, 0) and debt is not None:
            out["debt_equity"] = float((debt / equity) * 100.0)

    out["yf_52w_change"] = _safe_num(info.get("52WeekChange") or info.get("fiftyTwoWeekChange"))
    out["sp_52w_change"] = _safe_num(info.get("SandP52WeekChange"))

    out["recommendation_key"] = (info.get("recommendationKey") or "").lower()
    out["recommendation_mean"] = _safe_num(info.get("recommendationMean"))
    out["num_analysts"] = _safe_num(info.get("numberOfAnalystOpinions"))
    out["target_mean"] = _safe_num(info.get("targetMeanPrice"))
    out["target_high"] = _safe_num(info.get("targetHighPrice"))
    out["target_low"] = _safe_num(info.get("targetLowPrice"))
    out["target_median"] = _safe_num(info.get("targetMedianPrice"))

    rec_trend: list[dict] = []
    try:
        rec = tk.recommendations
        if rec is not None and not rec.empty:
            cols = {c.lower(): c for c in rec.columns}
            for _, row in rec.iterrows():
                rec_trend.append(
                    {
                        "period": str(row.get(cols.get("period", "period"), "")),
                        "strongBuy": int(row.get(cols.get("strongbuy", "strongBuy"), 0) or 0),
                        "buy": int(row.get(cols.get("buy", "buy"), 0) or 0),
                        "hold": int(row.get(cols.get("hold", "hold"), 0) or 0),
                        "sell": int(row.get(cols.get("sell", "sell"), 0) or 0),
                        "strongSell": int(row.get(cols.get("strongsell", "strongSell"), 0) or 0),
                    }
                )
    except Exception:
        pass
    out["recommendations_trend"] = rec_trend[:4]

    try:
        cal = tk.calendar
        if isinstance(cal, dict):
            edates = cal.get("Earnings Date")
            if isinstance(edates, list) and edates:
                try:
                    out["next_earnings"] = pd.Timestamp(edates[0]).strftime("%Y-%m-%d")
                except Exception:
                    pass
    except Exception:
        pass

    news_out: list[dict] = []
    try:
        news = tk.news or []
        for n in news[:6]:
            content = n.get("content") if isinstance(n, dict) else None
            if isinstance(content, dict):
                title = content.get("title")
                pub = (
                    (content.get("provider") or {}).get("displayName")
                    if isinstance(content.get("provider"), dict)
                    else content.get("publisher")
                )
                link = (
                    (content.get("canonicalUrl") or {}).get("url")
                    if isinstance(content.get("canonicalUrl"), dict)
                    else None
                ) or content.get("link")
                ts = content.get("pubDate") or content.get("displayTime")
            else:
                title = n.get("title")
                pub = n.get("publisher")
                link = n.get("link")
                pt = n.get("providerPublishTime")
                ts = datetime.fromtimestamp(int(pt), tz=timezone.utc).isoformat() if pt else None
            if title:
                news_out.append({"title": title, "publisher": pub or "", "link": link or "", "time": ts or ""})
    except Exception:
        pass
    out["news"] = news_out

    spy_close = _fetch_bench_history("SPY", "10y")
    sector_etf = _SECTOR_ETF.get(out.get("sector") or "")
    out["sector_etf"] = sector_etf
    sector_close = _fetch_bench_history(sector_etf, "10y") if sector_etf else None

    if spy_close is not None:
        out["benchmark_spy"] = _series_to_points(spy_close)
    if sector_close is not None:
        out["benchmark_sector"] = _series_to_points(sector_close)

    horizons = {
        "1d": 1, "1w": 7, "1m": 30, "3m": 91,
        "6m": 182, "ytd": 0, "1y": 365, "5y": 1825,
    }
    perf: dict[str, dict] = {}
    for label, days in horizons.items():
        if label == "ytd":
            if not close.empty:
                year_start = pd.Timestamp(year=close.index[-1].year, month=1, day=1, tz=close.index.tz)
                s_w = close[close.index >= year_start]
                s_pct = float((s_w.iloc[-1] / s_w.iloc[0] - 1.0) * 100.0) if len(s_w) >= 2 else None
            else:
                s_pct = None
            if spy_close is not None and not spy_close.empty:
                ys = pd.Timestamp(year=spy_close.index[-1].year, month=1, day=1, tz=spy_close.index.tz)
                b_w = spy_close[spy_close.index >= ys]
                b_pct = float((b_w.iloc[-1] / b_w.iloc[0] - 1.0) * 100.0) if len(b_w) >= 2 else None
            else:
                b_pct = None
            if sector_close is not None and not sector_close.empty:
                ys = pd.Timestamp(year=sector_close.index[-1].year, month=1, day=1, tz=sector_close.index.tz)
                k_w = sector_close[sector_close.index >= ys]
                k_pct = float((k_w.iloc[-1] / k_w.iloc[0] - 1.0) * 100.0) if len(k_w) >= 2 else None
            else:
                k_pct = None
        else:
            s_pct, b_pct = _aligned_pct(close, spy_close if spy_close is not None else close, days)
            _, k_pct = (
                _aligned_pct(close, sector_close if sector_close is not None else close, days)
                if sector_close is not None
                else (None, None)
            )
            if spy_close is None:
                b_pct = None
        perf[label] = {"stock": s_pct, "spy": b_pct, "sector": k_pct}
    out["performance"] = perf

    beta_spy, corr_spy = _beta_corr(close, spy_close)
    out["beta_computed"] = beta_spy
    out["correlation_spy"] = corr_spy
    if sector_close is not None:
        beta_sec, corr_sec = _beta_corr(close, sector_close)
        out["beta_sector"] = beta_sec
        out["correlation_sector"] = corr_sec

    _cache_put(cache_key, out)
    return out
