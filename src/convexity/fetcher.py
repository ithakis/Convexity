"""Data fetching — per-symbol rows, portfolio batches, and detail modal payloads."""

from __future__ import annotations

import math
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from convexity.cache import (
    _BENCH_CACHE,
    _BENCH_TTL,
    _CACHE_TTL_ANALYTICS,
    _cache_get,
    _cache_put,
)
from convexity.helpers import (
    _bollinger_pct_b,
    _is_rate_limited_error,
    _macd_hist_pct,
    _normalize_dividend_yield,
    _pct_change,
    _rsi,
    _safe_num,
    _series_to_points,
    _ytd_change,
    major_ccy,
    price_in_major,
)
from convexity.fx import convert_amount, usd_per_unit
from convexity.resolver import _ordered_resolve

try:
    from convexity import finnhub_adapter as _fh
except ImportError:
    _fh = None

try:
    from convexity import news_sentiment as _ns
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


def _market_cap(info: dict, currency: str) -> float | None:
    """Market cap in the MAJOR unit of the listing currency.

    Yahoo's ``marketCap`` is already in the major unit (SHEL.L: GBP, not GBp).
    The ``fast_info`` fallback ``_safe_info`` copies in when it is missing is
    shares x last price, i.e. in the QUOTE unit — pence for an LSE name — so it
    is scaled like a price."""
    mc = _safe_num(info.get("marketCap"))
    if mc is not None:
        return mc
    return price_in_major(info.get("market_cap"), currency)


def _positive(v) -> float | None:
    """A valuation multiple, or None when it is not meaningful.

    A negative P/E, EV/EBITDA or PEG (losses, negative EBITDA, shrinking
    earnings) is reported as "NM" by convention: it is not cheap, and a heat
    ramp that favours low values would otherwise colour it as the best on
    screen."""
    x = _safe_num(v)
    return x if (x is not None and x > 0) else None


def _currency_consistent(info: dict, out: dict, currency: str) -> None:
    """Fill the fields that combine a market price with the financial statements.

    Most listings report in their trading currency. Those that do not — ADRs
    (TSM reports in TWD, TM in JPY, BABA in CNY, NVO in DKK) and London names
    reporting in USD (SHEL.L) — get multiples from Yahoo that divide a
    trading-currency market cap by home-currency statements. Verified on
    2026-09-29: TM's EV/EBITDA 6.3 and P/B 15.3 against 11.9 and 0.91 for the
    same company's Tokyo line (7203.T); TSM's EV/EBITDA 5.2 against ~23.

    For those listings the multiples are rebuilt from their components with
    the market cap converted into the statement currency (fx.usd_per_unit —
    cache first, one FX download at most per currency per few hours):

        EV = MC + Total Debt - Total Cash            (statement currency)
        EV/EBITDA = EV / EBITDA,  EV/Revenue = EV / Revenue,  P/S = MC / Revenue
        P/B = MC / Equity,  Equity = Total Debt / (D/E / 100)

    and set to None when no rate is available rather than shown wrong. The
    trailing P/E, forward P/E and PEG are per-share ratios Yahoo computes in
    one currency and are kept. FCF yield and the USD market cap (the common
    unit for cap-weighting and size buckets across currencies) are computed
    here for every row.
    """
    trade = major_ccy(currency)
    fin = major_ccy(info.get("financialCurrency") or currency)
    out["financial_currency"] = fin
    mcap = _market_cap(info, currency)
    usd = usd_per_unit(trade) if mcap else None
    out["market_cap_usd"] = mcap * usd if (mcap and usd) else None
    if fin == trade:
        mcap_fin = mcap
    else:
        mcap_fin = convert_amount(mcap, trade, fin) if mcap else None
        rev = _safe_num(info.get("totalRevenue"))
        ebitda = _safe_num(info.get("ebitda"))
        debt = _safe_num(info.get("totalDebt"))
        cash = _safe_num(info.get("totalCash"))
        ev = None
        if mcap_fin is not None and debt is not None and cash is not None:
            ev = mcap_fin + debt - cash
        out["ev_ebitda"] = _positive(ev / ebitda) if (ev is not None and ebitda) else None
        out["ev_revenue"] = _positive(ev / rev) if (ev is not None and rev) else None
        out["ps_ratio"] = _positive(mcap_fin / rev) if (mcap_fin and rev) else None
        de = _safe_num(info.get("debtToEquity"))
        equity = debt / (de / 100.0) if (debt and de and de > 0) else None
        out["price_book"] = _positive(mcap_fin / equity) if (mcap_fin and equity) else None
    fcf = _safe_num(info.get("freeCashflow"))
    out["fcf_yield"] = (fcf / mcap_fin) if (fcf is not None and mcap_fin) else None


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
            out.append(
                {
                    "period": period,
                    "actual": actual,
                    "estimate": estimate,
                    "surprise_pct": surprise_pct,
                }
            )
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
            out["currency"] = info.get("currency") or "USD"
            out["market_cap"] = _market_cap(info, out["currency"])
            out["exchange"] = info.get("exchange") or info.get("fullExchangeName") or ""
            out["sector"] = info.get("sector") or ""
            out["industry"] = info.get("industry") or ""
            out["quote_type"] = (info.get("quoteType") or "").upper()
            out["website"] = info.get("website") or ""

            out["ps_ratio"] = _positive(info.get("priceToSalesTrailing12Months"))
            # Trailing only. The old `trailingPE or forwardPE` put a FORWARD
            # multiple in the trailing column whenever trailing EPS was
            # negative (Yahoo then omits trailingPE) — exactly the loss-makers
            # where the two differ most. Loss-makers are n/a here, as P/E is
            # not meaningful for them; the Fwd P/E column still has theirs.
            out["pe_ratio"] = _positive(info.get("trailingPE"))

            out["forward_pe"] = _positive(info.get("forwardPE"))
            out["peg"] = _positive(info.get("pegRatio") or info.get("trailingPegRatio"))
            out["ev_ebitda"] = _positive(info.get("enterpriseToEbitda"))
            out["ev_revenue"] = _positive(info.get("enterpriseToRevenue"))
            out["beta"] = _safe_num(info.get("beta"))
            out["dividend_yield"] = _normalize_dividend_yield(
                info.get("dividendYield"),
                price=last,
                dividend_rate=info.get("dividendRate"),
                trailing_yield=info.get("trailingAnnualDividendYield"),
                trailing_rate=info.get("trailingAnnualDividendRate"),
                currency=out["currency"],
                financial_currency=info.get("financialCurrency"),
            )
            out["operating_margin"] = _safe_num(info.get("operatingMargins"))
            # Yahoo reports debtToEquity in PERCENT (AAPL 78.4 = total debt is
            # 0.78x equity); kept in that unit and shown with a % sign.
            out["debt_equity"] = _safe_num(info.get("debtToEquity"))
            out["current_ratio"] = _safe_num(info.get("currentRatio"))
            # Extended fundamentals (all straight from the same info dict —
            # zero extra network cost on the streaming path). Margins/growth/
            # returns come back as fractions (0.42 = 42%); the frontend
            # formats them, never re-detects units.
            out["price_book"] = _positive(info.get("priceToBook"))
            out["roe"] = _safe_num(info.get("returnOnEquity"))
            out["roa"] = _safe_num(info.get("returnOnAssets"))
            out["gross_margin"] = _safe_num(info.get("grossMargins"))
            out["profit_margin"] = _safe_num(info.get("profitMargins"))
            out["revenue_growth"] = _safe_num(info.get("revenueGrowth"))
            # Both Yahoo fields are year-over-year growth of the most recent
            # quarter (earningsGrowth: EPS, earningsQuarterlyGrowth: net
            # income). The first is preferred; the second only when it is
            # truly absent — `or` would discard a legitimate 0.0.
            _eg = info.get("earningsGrowth")
            if _eg is None:
                _eg = info.get("earningsQuarterlyGrowth")
            out["earnings_growth"] = _safe_num(_eg)
            out["quick_ratio"] = _safe_num(info.get("quickRatio"))
            out["payout_ratio"] = _safe_num(info.get("payoutRatio"))
            # FCF yield, USD market cap, and the multiples Yahoo gets wrong
            # for listings whose statements are in another currency.
            _currency_consistent(info, out, out["currency"])
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
                        "strongBuy": _rd_int("strongbuy"),
                        "buy": _rd_int("buy"),
                        "hold": _rd_int("hold"),
                        "sell": _rd_int("sell"),
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


# ----------------------------- Quote streaming ------------------------------


def stream_quotes(entries, *, workers: int = 5, cancel=None, resolve: bool = True):
    """Yield the NDJSON message bodies /api/quotes-stream has always written:
    ``{"type":"start", total, symbols}``, then one ``{"type":"row", row, done,
    total}`` per symbol as it lands, then ``{"type":"done", total}``.

    Extracted from the route handler in v1.11.0 so the streaming endpoint and
    the background refresh job (jobs.py) run the SAME code rather than two
    copies that drift.

    `cancel` is any object with ``.is_set()`` — duck-typed so this module never
    imports jobs.py.

    The pool is deliberately NOT a ``with`` block. ``__exit__`` calls
    ``shutdown(wait=True)``, which JOINS every queued future — so the old
    handler's ``return`` on BrokenPipe still sat there finishing all 150
    symbols, hammering Yahoo for ~30 s on a build the user had already
    abandoned. ``shutdown(wait=False, cancel_futures=True)`` de-queues
    everything untouched; only the <= `workers` already in flight run to
    completion, and their results are discarded. The same ``finally`` runs on
    ``GeneratorExit``, so a caller closing the generator gets the same
    cancellation for free.
    """
    symbols = _ordered_resolve([str(e) for e in entries]) if resolve else list(entries)
    total = len(symbols)
    yield {"type": "start", "total": total, "symbols": symbols}
    if total:
        pool = ThreadPoolExecutor(max_workers=max(1, min(workers, total)))
        try:
            futures = {pool.submit(fetch_one, s): s for s in symbols}
            done = 0
            for fut in as_completed(futures):
                if cancel is not None and cancel.is_set():
                    break
                row = fut.result()
                done += 1
                yield {"type": "row", "row": row, "done": done, "total": total}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    yield {"type": "done", "total": total}


# ----------------------------- Intraday history -----------------------------

# Yahoo's own hard caps on how far back each interval reaches, in days. Exceed
# one and the request comes back empty rather than erroring, so we check first
# and let the caller fall back to the daily payload instead of drawing nothing.
_INTERVAL_MAX_DAYS = {
    "1m": 7,
    "2m": 60,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "90m": 60,
    "60m": 730,
    "1h": 730,
}

# Range -> (interval, fetch period). Two entries in practice: 3M and 6M share
# one payload, so a symbol costs at most two intraday fetches no matter how the
# user tabs around.
#
# The fetch period is deliberately MUCH wider than the range being displayed.
# That is not laziness — it is what makes the moving averages work. An SMA 200
# over 30m bars needs 200 bars of warm-up; a 1M window only holds ~280 of them,
# so computing on exactly the visible window would leave the line empty (or
# nearly) on every short range. Fetching 60d of 30m bars gives ~780, of which
# the last ~280 are shown, and the MA is fully populated across all of them.
_RANGE_INTRADAY = {
    "1M": ("30m", "60d"),
    "3M": ("1h", "1y"),
    "6M": ("1h", "1y"),
}


# Suffix -> days. Longest first: "mo" and "wk" must be tested before "d"/"y"
# would match their tail.
_PERIOD_UNITS = (("mo", 30.44), ("wk", 7.0), ("d", 1.0), ("y", 365.25))


def _period_days(period: str) -> float:
    """Rough calendar-day length of a yfinance period string.

    Anything unrecognised returns a deliberately huge number so the caller's cap
    check REJECTS it. Guessing small in the other direction would let an
    over-cap request through, and Yahoo answers those with an empty frame rather
    than an error — i.e. a blank chart instead of a graceful fall back to daily.
    """
    p = (period or "").strip().lower()
    if p in ("max", "ytd"):
        return 10_000.0
    for suffix, mult in _PERIOD_UNITS:
        if p.endswith(suffix):
            try:
                return float(p[: -len(suffix)]) * mult
            except ValueError:
                return 10_000.0
    return 10_000.0


def intraday_history(symbol: str, interval: str, period: str) -> dict | None:
    """Sub-daily closes + volume for one symbol, or None if unavailable.

    None is a normal outcome, not an error: the interval/period pair may exceed
    Yahoo's cap, the symbol may not trade intraday (many non-US listings), or
    the fetch may simply fail. Every caller falls back to the daily series, so
    the chart degrades to what it drew before rather than going blank.

    Cached under the default 300 s TTL — intraday bars go stale within the
    trading day, unlike the daily `detail|` payload they supplement. Benchmark
    symbols (SPY, sector ETFs) come through here too and are shared across every
    holding, so their hit rate is ~100% after the first call.
    """
    cache_key = f"intraday|{symbol}|{interval}|{period}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached or None  # {} is the cached "no data" marker

    cap = _INTERVAL_MAX_DAYS.get(interval)
    if cap is not None and _period_days(period) > cap:
        return None  # caller's problem to fall back; don't cache

    try:
        hist = yf.Ticker(symbol).history(
            period=period, interval=interval, auto_adjust=True, actions=False
        )
    except Exception:
        return None  # transient — don't cache, retry next open
    if hist is None or hist.empty or "Close" not in hist:
        # Cache the miss so a symbol with no intraday data doesn't re-hit Yahoo
        # on every range tab click. Short TTL via the default.
        _cache_put(cache_key, {})
        return None

    close = hist["Close"].dropna()
    if len(close) < 2:
        _cache_put(cache_key, {})
        return None
    vol = hist["Volume"].dropna() if "Volume" in hist else pd.Series(dtype=float)
    out = {
        "interval": interval,
        "period": period,
        "history": _series_to_points(close),
        "volume": _series_to_points(vol) if not vol.empty else [],
    }
    _cache_put(cache_key, out)
    return out


def range_history(symbol: str, rng: str, benchmarks: list[str] | None = None) -> dict:
    """Payload for GET /api/history — the intraday half of the granularity ladder.

    Only 1M/3M/6M route here; the longer ranges stay client-side slices of the
    one daily `period="max"` payload /api/detail already returns. `fallback`
    tells the client the intraday fetch didn't happen so it can draw the daily
    series and say so in the legend.
    """
    spec = _RANGE_INTRADAY.get((rng or "").upper())
    if spec is None:
        return {
            "symbol": symbol,
            "range": rng,
            "fallback": True,
            "reason": "range is served from the daily payload",
        }
    interval, period = spec
    main = intraday_history(symbol, interval, period)
    if main is None:
        return {
            "symbol": symbol,
            "range": rng,
            "interval": interval,
            "fallback": True,
            "reason": "no intraday data",
        }

    out = {
        "symbol": symbol,
        "range": rng,
        "interval": interval,
        "period": period,
        "fallback": False,
        "history": main["history"],
        "volume": main["volume"],
        "benchmarks": {},
    }
    # Benchmarks must be on the SAME bar frequency or the overlay steps against
    # a smooth line. The client passes the ones it's actually showing.
    for b in benchmarks or []:
        b = (b or "").strip().upper()
        if not b or b == symbol.upper():
            continue
        got = intraday_history(b, interval, period)
        if got:
            out["benchmarks"][b] = got["history"]
    return out


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
    df = pd.concat(
        [stock_close.rename("s"), bench_close.rename("b")], axis=1, join="inner"
    ).dropna()
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
    """Sum of the last four quarters, or None with fewer: one quarter is not a
    twelve-month figure (it would put a quarterly profit over annual equity)."""
    vals = _statement_values(df, labels)
    if len(vals) >= 4:
        return float(sum(vals[:4]))
    return None


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

    out["market_cap"] = _market_cap(info, out["currency"])
    out["shares"] = _safe_num(info.get("sharesOutstanding"))
    out["float_shares"] = _safe_num(info.get("floatShares"))

    out["pe"] = _positive(info.get("trailingPE"))
    out["forward_pe"] = _positive(info.get("forwardPE"))
    out["ps"] = _positive(info.get("priceToSalesTrailing12Months"))
    out["pb"] = _positive(info.get("priceToBook"))
    out["peg"] = _positive(info.get("pegRatio") or info.get("trailingPegRatio"))
    out["ev_ebitda"] = _positive(info.get("enterpriseToEbitda"))
    out["ev_revenue"] = _positive(info.get("enterpriseToRevenue"))
    # Same currency repair as the table row (ADRs, LSE names reporting in USD);
    # it writes the row-style keys, mapped back onto the detail ones here.
    fixed = {"ps_ratio": out["ps"], "price_book": out["pb"]}
    _currency_consistent(info, fixed, out["currency"])
    out["ps"], out["pb"] = fixed["ps_ratio"], fixed["price_book"]
    for k in ("ev_ebitda", "ev_revenue"):
        if k in fixed:
            out[k] = fixed[k]
    out["financial_currency"] = fixed["financial_currency"]
    out["market_cap_usd"] = fixed["market_cap_usd"]
    out["fcf_yield"] = fixed["fcf_yield"]

    out["beta"] = _safe_num(info.get("beta"))
    out["dividend_yield"] = _normalize_dividend_yield(
        info.get("dividendYield"),
        price=out.get("price")
        or info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("last_price"),
        dividend_rate=info.get("dividendRate"),
        trailing_yield=info.get("trailingAnnualDividendYield"),
        trailing_rate=info.get("trailingAnnualDividendRate"),
        currency=out["currency"],
        financial_currency=info.get("financialCurrency"),
    )
    out["dividend_rate"] = _safe_num(info.get("dividendRate"))
    out["payout_ratio"] = _safe_num(info.get("payoutRatio"))
    ex_div = info.get("exDividendDate")
    if ex_div:
        try:
            out["ex_div_date"] = datetime.fromtimestamp(int(ex_div), tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
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
            "Stockholders Equity",
            "Common Stock Equity",
            "Total Stockholder Equity",
            "Total Equity Gross Minority Interest",
        ]
        debt_labels = ["Total Debt"]
        current_debt_labels = ["Current Debt", "Current Debt And Capital Lease Obligation"]
        long_debt_labels = ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"]
        net_income_labels = [
            "Net Income",
            "Diluted NI Availto Com Stockholders",
            "Net Income Common Stockholders",
            "Net Income Continuous Operations",
        ]

        # Balance-sheet items from the most recent quarter first (Yahoo's own
        # D/E is "mrq"), the last annual report only when that is missing.
        equity = _latest_statement_value(quarterly_balance_sheet, equity_labels)
        if equity is None:
            equity = _latest_statement_value(balance_sheet, equity_labels)

        debt = _latest_statement_value(quarterly_balance_sheet, debt_labels)
        if debt is None:
            debt = _latest_statement_value(balance_sheet, debt_labels)
        if debt is None:
            current_debt = _latest_statement_value(quarterly_balance_sheet, current_debt_labels)
            long_debt = _latest_statement_value(quarterly_balance_sheet, long_debt_labels)
            if current_debt is None:
                current_debt = _latest_statement_value(balance_sheet, current_debt_labels)
            if long_debt is None:
                long_debt = _latest_statement_value(balance_sheet, long_debt_labels)
            if current_debt is not None or long_debt is not None:
                debt = float((current_debt or 0.0) + (long_debt or 0.0))

        # Trailing twelve months (sum of the last four quarters), like Yahoo's
        # returnOnEquity; the last fiscal year only when quarters are missing.
        net_income = _ttm_statement_value(quarterly_income_stmt, net_income_labels)
        if net_income is None:
            net_income = _latest_statement_value(income_stmt, net_income_labels)

        # Negative equity makes both ratios meaningless (a loss-maker would
        # show a positive ROE, a levered one a negative D/E): n/a, as Yahoo
        # itself does.
        if out["roe"] is None and equity is not None and equity > 0 and net_income is not None:
            out["roe"] = float(net_income / equity)
        if out["debt_equity"] is None and equity is not None and equity > 0 and debt is not None:
            # Percent, the unit of Yahoo's debtToEquity (78.4 = 0.784x).
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
                news_out.append(
                    {"title": title, "publisher": pub or "", "link": link or "", "time": ts or ""}
                )
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
        "1d": 1,
        "1w": 7,
        "1m": 30,
        "3m": 91,
        "6m": 182,
        "ytd": 0,
        "1y": 365,
        "5y": 1825,
    }
    perf: dict[str, dict] = {}
    for label, days in horizons.items():
        if label == "ytd":
            if not close.empty:
                s_pct = _ytd_change(close)
            else:
                s_pct = None
            b_pct = _ytd_change(spy_close) if spy_close is not None else None
            k_pct = _ytd_change(sector_close) if sector_close is not None else None
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
