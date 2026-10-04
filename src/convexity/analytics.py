"""Portfolio analytics — bulk close, analyst blocks, multi-weight analysis."""

import contextlib
import math
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import yfinance as yf

from convexity.cache import (
    _BULK_CLOSE_MISS,
    _CACHE_TTL_ANALYTICS,
    _bulk_bars_get,
    _bulk_bars_put,
    _bulk_close_get_cached,
    _bulk_close_put,
    _cache_get,
    _cache_put,
)
from convexity.fetcher import _SECTOR_ETF
from convexity.fx import _apply_fx_to_closes, _norm_ccy_for_fx, convert_amount
from convexity.helpers import (
    _dedupe_rows_by_symbol,
    _safe_num,
    _series_to_points,
    by_trading_date,
    major_ccy,
)

_PERIOD_YF = {
    "3M": "3mo",
    "6M": "6mo",
    "YTD": "ytd",
    "1Y": "1y",
    "3Y": "3y",
    "5Y": "5y",
    "MAX": "max",
}
_PERIOD_MONTHS = {"3M": 3, "6M": 6, "1Y": 12, "3Y": 36, "5Y": 60}

# Analytics fetches this much wider window than it reports, so the moving
# averages have 200 trading days of warm-up before the period's first bar —
# computing them on the trimmed period left SMA 200 empty for its first
# ~10 months. Stats are computed on the period slice only.
_WARMUP_YF = {
    "3M": "2y",
    "6M": "2y",
    "YTD": "2y",
    "1Y": "2y",
    "3Y": "5y",
    "5Y": "10y",
    "MAX": "max",
}
_SMA_WINDOWS = (20, 50, 200)

# Selectable benchmarks: key -> (Yahoo ticker, label, quote currency).
# "SECTOR" (the portfolio's own sector-ETF blend) is added per weight set.
_BENCHMARKS = {
    "SPY": ("SPY", "S&P 500", "USD"),
    "QQQ": ("QQQ", "Nasdaq-100", "USD"),
    "STOXX50": ("^STOXX50E", "Euro Stoxx 50", "EUR"),
    "N225": ("^N225", "Nikkei 225", "JPY"),
    "KOSPI": ("^KS11", "KOSPI", "KRW"),
}


def _period_start(period_u: str, last: pd.Timestamp) -> pd.Timestamp | None:
    """Start date of the reported window (Jan 1 for YTD, last − N months)."""
    if period_u == "YTD":
        return pd.Timestamp(year=last.year, month=1, day=1)
    months = _PERIOD_MONTHS.get(period_u)
    return last - pd.DateOffset(months=months) if months else None


def _period_slice(frame: pd.DataFrame, start: pd.Timestamp | None) -> pd.DataFrame:
    """The reported window: from the last close ON OR BEFORE `start` — the base
    a period return is measured from (YTD from the previous year's last close,
    1Y from the close a year ago) — to the end. Starting at the first bar
    AFTER `start` dropped that first session's move from every period return
    (YTD missed the year's first trading day). A holding with no earlier bar
    starts at its first one."""
    if start is None or frame.empty:
        return frame
    pos = int(frame.index.searchsorted(start, side="right")) - 1  # last bar <= start
    return frame.iloc[max(pos, 0) :]


# Risk-free rate for Sharpe / Sortino in USD: the 13-week T-bill yield (^IRX,
# annualised percent), fetched with the benchmarks in the same download. Yahoo
# has no comparable short-rate series for the other display currencies, so
# they use 0 and the payload says so ("rf_source").
_RF_TICKER = "^IRX"


def _daily_rf(wide: pd.DataFrame, index: pd.DatetimeIndex, display_ccy: str) -> pd.Series | None:
    """Daily risk-free return on `index`: (1 + y/100)^(1/252) − 1, y the ^IRX
    yield carried forward over holidays."""
    if display_ccy != "USD" or _RF_TICKER not in wide.columns:
        return None
    y = wide[_RF_TICKER].ffill().reindex(index).ffill().bfill()
    if y.isna().all():
        return None
    return (1.0 + y.fillna(0.0) / 100.0) ** (1.0 / 252.0) - 1.0


def _stats(ret: pd.Series, val: pd.Series, rf: pd.Series | None = None) -> dict:
    """Return/risk stats from daily returns `ret` and a value index `val`.

    Sharpe and Sortino use EXCESS returns r_t − rf_t (rf: daily risk-free
    returns aligned to `ret`; None = 0). Both annualise the arithmetic mean
    (×252) over the volatility (×√252) — the standard daily-data Sharpe —
    while "ann_return" is the compound (geometric) rate. Sortino's downside
    deviation is the root-mean-square of excess returns below 0 over ALL
    observations (Sortino & Price 1994), not the std of the negative ones.
    """
    if ret.empty or val.empty:
        return {}
    years = max(((val.index[-1] - val.index[0]).days or 1) / 365.25, 1e-6)
    growth = float(val.iloc[-1] / val.iloc[0])
    ann_return = (growth ** (1.0 / years) - 1.0) * 100.0
    ex = ret - rf.reindex(ret.index).fillna(0.0) if rf is not None else ret
    std = ret.std()
    ex_std = ex.std()
    downside = math.sqrt((ex.clip(upper=0) ** 2).mean())
    max_dd = float((val / val.cummax() - 1.0).min() * 100.0)
    return {
        "total_return": (growth - 1.0) * 100.0,
        "ann_return": ann_return,
        "ann_vol": float(std * math.sqrt(252)) * 100.0,
        "sharpe": float(ex.mean() * 252 / (ex_std * math.sqrt(252))) if ex_std else None,
        "sortino": float(ex.mean() * 252 / (downside * math.sqrt(252))) if downside > 0 else None,
        "max_dd": max_dd,
        "calmar": (ann_return / abs(max_dd)) if max_dd < 0 else None,
        "rf_ann": float(((1.0 + rf.reindex(ret.index).fillna(0.0)).prod()) ** (1.0 / years) - 1.0)
        * 100.0
        if rf is not None
        else 0.0,
    }


def _relative(rp: pd.Series, rb: pd.Series) -> dict:
    """Beta, R², annualised tracking error (%) and information ratio of daily
    returns `rp` vs `rb`. IR = annualised mean active return / TE (both on
    the daily active series r_p − r_b, so the ratio is scale-consistent)."""
    common = rp.index.intersection(rb.index)
    if len(common) < 30:
        return {}
    rp, rb = rp.loc[common], rb.loc[common]
    active = rp - rb
    var_b, corr, te = rb.var(), rp.corr(rb), active.std()
    return {
        "beta": float(rp.cov(rb) / var_b) if var_b else None,
        "r2": float(corr * corr) if pd.notna(corr) else None,
        "te": float(te * math.sqrt(252) * 100.0) if pd.notna(te) else None,
        "ir": float(active.mean() * 252 / (te * math.sqrt(252)))
        if (pd.notna(te) and te > 0)
        else None,
    }


def _bench_block(
    label: str,
    val: pd.Series,
    spy_ret: pd.Series | None,
    port_ret: pd.Series,
    rf: pd.Series | None = None,
) -> dict:
    """One benchmark: its stats (+ beta/R²/TE vs SPY) and the portfolio's
    beta/R²/TE/IR against it. `val` is its value index on the period."""
    ret = val.pct_change().dropna()
    stats = _stats(ret, val, rf)
    if spy_ret is not None:
        stats.update(_relative(ret, spy_ret))
    return {
        "label": label,
        "stats": stats,
        "rel": _relative(port_ret, ret),
        "series": _series_to_points(100.0 * val / val.iloc[0]),
    }


def _normalize_weights(weights_in: dict, symbols: list[str]) -> dict[str, float]:
    raw: dict[str, float] = {}
    for s in symbols:
        v = weights_in.get(s)
        try:
            f = float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            f = 0.0
        raw[s] = max(0.0, f)
    total = sum(raw.values())
    if total <= 0:
        n = len(symbols)
        if not n:
            return {}
        return dict.fromkeys(symbols, 1.0 / n)
    return {s: v / total for s, v in raw.items()}


def _usable(v) -> float | None:
    x = _safe_num(v)
    return x if (x is not None and math.isfinite(x)) else None


def _w_avg(values: dict, weights: dict) -> float | None:
    """Weighted arithmetic mean over the holdings that have a value,
    renormalised over their weights (uncovered names drop out of both)."""
    num = den = 0.0
    for s, v in values.items():
        x = _usable(v)
        if x is None:
            continue
        num += x * weights.get(s, 0.0)
        den += weights.get(s, 0.0)
    return num / den if den > 0 else None


def _w_harmonic(values: dict, weights: dict) -> float | None:
    """Weighted harmonic mean Σw / Σ(w/x) over holdings with a POSITIVE
    multiple x. For P/E this is the portfolio's look-through multiple: the
    weighted average of earnings YIELDS (w·E/P, which do add up), inverted.
    Negative multiples are not meaningful and are excluded, as by the index
    providers that report a portfolio P/E."""
    num = den = 0.0
    for s, v in values.items():
        x = _usable(v)
        w = weights.get(s, 0.0)
        if x is None or x <= 0 or w <= 0:
            continue
        num += w
        den += w / x
    return num / den if den > 0 else None


def _coverage(values: dict, weights: dict) -> float:
    """Share of the portfolio's weight with a usable (positive) multiple."""
    return float(sum(weights.get(s, 0.0) for s, v in values.items() if (x := _usable(v)) and x > 0))


def _market_cap_usd(row: dict) -> float | None:
    """A row's market cap in USD: the fetcher's `market_cap_usd`, else the
    local cap converted (rows cached before that field existed). The local cap
    is in the currency's major unit (GBP for a GBp quote)."""
    v = _usable(row.get("market_cap_usd"))
    if v is not None and v > 0:
        return v
    cap = _usable(row.get("market_cap"))
    if cap is None or cap <= 0:
        return None
    return convert_amount(cap, major_ccy(row.get("currency")), "USD")


def _linked_contribution(win_ret: pd.DataFrame, w: pd.Series) -> dict[str, float]:
    """Each holding's contribution (percentage points) to the portfolio's
    period return, adding up to it exactly.

    The portfolio is rebalanced to its weights daily, so its return is
    compounded from daily returns r_p,t = Σ_i w_i·r_i,t. The daily
    contributions w_i·r_i,t add up within a day but not across days
    (compounding), and `weight x the holding's own period return` adds up to
    neither. Carino (1999) logarithmic linking scales each day by
    k_t / k, with k_t = ln(1+r_p,t)/r_p,t and k = ln(1+R)/R, so that
    Σ_i Σ_t (k_t/k)·w_i·r_i,t = R, the period return shown above it.
    """
    if win_ret is None or win_ret.empty:
        return {}
    w = w.reindex(win_ret.columns).fillna(0.0)
    daily = win_ret.mul(w, axis=1)
    rp = daily.sum(axis=1).to_numpy(dtype=float)
    total = float(np.prod(1.0 + rp) - 1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        kt = np.where(np.abs(rp) > 1e-12, np.log1p(rp) / rp, 1.0)
    k = math.log1p(total) / total if abs(total) > 1e-12 else 1.0
    scaled = daily.mul(kt / k, axis=0)
    return {s: float(scaled[s].sum()) * 100.0 for s in win_ret.columns}


def _mcap_bucket(mcap: float | None) -> str:
    """Size bucket of a market cap IN USD (the thresholds are dollar ones)."""
    if mcap is None or not math.isfinite(mcap) or mcap <= 0:
        return "Unknown"
    if mcap >= 2e11:
        return "Mega ($200B+)"
    if mcap >= 1e10:
        return "Large ($10B–200B)"
    if mcap >= 2e9:
        return "Mid ($2B–10B)"
    if mcap >= 3e8:
        return "Small ($300M–2B)"
    return "Micro (<$300M)"


def _bulk_close(symbols: list[str], period: str) -> pd.DataFrame:
    """Close prices indexed by date, columns = symbols (those that returned data).

    `period` is an app label ("1Y") or a raw yfinance period ("2y", "10y").
    Caches per-symbol. Retries missing symbols individually with backoff.
    A repeated symbol is fetched once (the result is keyed by symbol anyway,
    so a repeat would only cost a wasted download and a doubled retry sleep)."""
    symbols = list(dict.fromkeys(symbols or []))
    if not symbols:
        return pd.DataFrame()
    period_yf = _PERIOD_YF.get(period.upper(), period.lower())

    out: dict[str, pd.Series] = {}
    to_fetch: list[str] = []
    for s in symbols:
        cached = _bulk_close_get_cached(s, period_yf)
        if cached is _BULK_CLOSE_MISS:
            to_fetch.append(s)
        elif cached is not None and not cached.empty:
            out[s] = cached

    if to_fetch:
        try:
            df = yf.download(
                tickers=to_fetch,
                period=period_yf,
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                progress=False,
            )
        except Exception:
            df = None
        got: set[str] = set()
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                for s in to_fetch:
                    try:
                        col = df[s]["Close"].dropna()
                    except (KeyError, ValueError):
                        continue
                    if not col.empty:
                        out[s] = col
                        _bulk_close_put(s, period_yf, col)
                        _put_bars(s, period_yf, df[s].loc[col.index])
                        got.add(s)
            else:
                try:
                    col = df["Close"].dropna()
                    if not col.empty:
                        out[to_fetch[0]] = col
                        _bulk_close_put(to_fetch[0], period_yf, col)
                        _put_bars(to_fetch[0], period_yf, df.loc[col.index])
                        got.add(to_fetch[0])
                except (KeyError, ValueError):
                    pass

        missing = [s for s in to_fetch if s not in got]
        for i, s in enumerate(missing):
            delay = 0.4 + 0.2 * min(i, 6)
            time.sleep(delay)
            ser = None
            for attempt in range(3):
                try:
                    tk = yf.Ticker(s)
                    h = tk.history(period=period_yf, interval="1d", auto_adjust=True)
                    if h is not None and not h.empty:
                        col = h["Close"].dropna()
                        if not col.empty:
                            # Same date keys as the yf.download path above; the
                            # old tz_convert(None) moved bars to UTC, off by a
                            # day for London and off-midnight for New York.
                            ser = by_trading_date(col)
                            _put_bars(s, period_yf, by_trading_date(h.loc[col.index]))
                            break
                except Exception:
                    pass
                time.sleep(0.6 * (attempt + 1))
            if ser is not None:
                out[s] = ser
                _bulk_close_put(s, period_yf, ser)
            else:
                _bulk_close_put(s, period_yf, pd.Series(dtype=float))

    if not out:
        return pd.DataFrame()
    return pd.concat(out, axis=1).sort_index()


def _put_bars(sym: str, period_yf: str, frame: pd.DataFrame) -> None:
    """Keep the OHLCV that came with a bulk close (see cache._BULK_BARS_CACHE).
    Best effort: a frame without the columns is simply not stored. Duplicate
    dates are dropped here because _portfolio_bars reindexes on the date."""
    cols = ["Open", "High", "Low", "Close", "Volume"]
    with contextlib.suppress(KeyError, ValueError, TypeError):
        f = frame[cols].astype(float)
        _bulk_bars_put(sym, period_yf, f[~f.index.duplicated(keep="last")])


def _sig(v: float, digits: int = 6) -> float:
    return float(f"{v:.{digits}g}")


def _wick_cap(b: pd.DataFrame) -> float:
    """Largest believable wick, as a fraction of the bar body: 5x the median
    daily range, at least 3%. Yahoo has bad prints (VOD.L 2007-10-09: high 357
    on a 176 close) that would otherwise set the width of the portfolio band.
    The frontend's clipBadWicks applies the same rule to single stocks."""
    rel = ((b["High"] - b["Low"]) / b["Close"]).replace([np.inf, -np.inf], np.nan).dropna()
    if len(rel) < 20:
        return np.inf  # too little history to call anything a bad print
    return max(5.0 * float(rel.median()), 0.03)


def _portfolio_bars(
    port_val: pd.Series, w_vec: pd.Series, period_yf: str, conv_closes: pd.DataFrame
) -> dict | None:
    """Approximate daily open/high/low and traded value for the portfolio index.

    A portfolio has no traded high or low of its own. The index is rebalanced
    daily, so day t's value is P(t-1) * sum_i w_i * (1 + r_i,t). Replacing each
    holding's close by its open/high/low on that day gives the bar:

        X_t = P(t-1) * sum_i w_i * (1 + r_i,t) * X_i,t / C_i,t,   X in {O, H, L}

    with P(t-1) = P(t) / sum_i w_i (1 + r_i,t). X/C is taken in the listing's
    own currency and r in the display currency, so FX is applied at the close
    rate. This is exact for a basket whose holdings all peak (or trough) at the
    same moment and otherwise an upper (lower) bound on the true basket high
    (low); since every holding's high is at or above its close, the band
    always contains the close line. Explained in Settings → About, not on the
    chart (the user's call).

    Volume is the weighted traded value, sum(w * shares * close) in the display
    currency. If any weighted holding has no cached bars, the result is empty
    and the client draws a line: a band built from part of the book would be
    wrong in a way nobody could see.
    """
    empty = {"ohlc": [], "volume": []}
    idx = port_val.index
    num = {k: pd.Series(0.0, index=idx) for k in ("Open", "High", "Low")}
    growth = pd.Series(0.0, index=idx)
    traded = pd.Series(0.0, index=idx)
    try:
        for s, w in w_vec.items():
            if w <= 0:
                continue
            bars = _bulk_bars_get(s, period_yf)
            if bars is None or getattr(bars, "empty", True) or s not in conv_closes:
                return empty
            cap = _wick_cap(bars)
            b = bars.reindex(idx)
            close = b["Close"].where(b["Close"] > 0)
            body_hi = np.maximum(b["Open"], close)
            body_lo = np.minimum(b["Open"], close)
            ratio = {
                "Open": b["Open"] / close,
                "High": np.minimum(b["High"], body_hi * (1 + cap)) / close,
                "Low": np.maximum(b["Low"], body_lo * (1 - cap)) / close,
            }
            conv = conv_closes[s]
            g = (1.0 + conv.pct_change()).reindex(idx).fillna(1.0)
            growth += w * g
            for k in num:
                # A day this listing did not trade (its own holiday, close
                # forward-filled in the index) adds no range: ratio 1.
                num[k] += w * g * ratio[k].fillna(1.0)
            traded += w * b["Volume"].fillna(0.0) * conv.reindex(idx).fillna(0.0)
    except (KeyError, ValueError, TypeError):
        return empty  # a malformed cached frame must never 500 the analytics
    prev = port_val / growth.where(growth > 0)
    hi = np.maximum(prev * num["High"], port_val)
    lo = np.minimum(prev * num["Low"], port_val)
    op = (prev * num["Open"]).clip(lower=lo, upper=hi)
    ohlc = [
        [int(ts.timestamp() * 1000), _sig(o), _sig(h), _sig(low)]
        for ts, o, h, low in zip(idx, op, hi, lo, strict=True)
        if np.isfinite(o) and np.isfinite(h) and np.isfinite(low)
    ]
    volume = [[ms, _sig(v, 4)] for ms, v in _series_to_points(traded)]
    return {"ohlc": ohlc, "volume": volume}


def _fetch_target_trio(symbol: str) -> dict:
    """One bare `Ticker.info` reduced to the analyst target low/high/median.

    Returns all-None on any failure, including the empty dict Yahoo hands back
    when it is throttling — the caller distinguishes the two by retrying.
    """
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception:
        info = {}
    return {
        "target_low": _safe_num(info.get("targetLowPrice")),
        "target_high": _safe_num(info.get("targetHighPrice")),
        "target_median": _safe_num(info.get("targetMedianPrice")),
    }


def _analyst_for(symbol: str, row: dict | None = None) -> dict:
    """Analyst block for one symbol, reusing the fields `fetch_one` already put
    on the row instead of re-fetching them.

    The streaming row already carries recommendation_mean / target_mean_price /
    rec_key / n_analysts / rating_dist / dividend_yield / ev_ebitda / price /
    currency per symbol, so re-hitting Ticker.info + tk.recommendations for those
    is pure waste. Only the target low/high/median trio is absent from the row,
    so that trio is the *sole* reason to touch Ticker.info here — and
    tk.recommendations is dropped entirely (dist comes from row["rating_dist"]).
    Net effect: cold analytics drops from ~2 yfinance calls/active symbol
    (tk.info via _safe_info + tk.recommendations) to at most 1 (a bare tk.info
    for the target trio), and to 0 when the row already supplies that trio.
    Output shape is byte-for-byte identical to before (same 12 keys)."""
    row = row or {}
    price = _safe_num(row.get("price"))
    out = {
        "mean_rating": _safe_num(row.get("recommendation_mean")),
        "rec_key": (row.get("rec_key") or "").strip().lower() or None,
        "n_analysts": _safe_num(row.get("n_analysts")),
        "target_mean": _safe_num(row.get("target_mean_price")),
        # Row's dividend_yield is already normalized to a fraction at ingestion
        # (fetcher._normalize_dividend_yield) — reuse it, don't re-derive.
        "div_yield": _safe_num(row.get("dividend_yield")),
        "ev_ebitda": _safe_num(row.get("ev_ebitda")),
        "price": price,
        "currency": (row.get("currency") or "").upper() or None,
        "dist": row.get("rating_dist") if isinstance(row.get("rating_dist"), dict) else None,
    }

    # Prefer any target trio the row supplies (future-proofing); only the gaps
    # justify a network call. A single bare tk.info covers all three — cheaper
    # than _safe_info (which additionally pings fast_info we don't need here).
    trio = {
        "target_low": _safe_num(row.get("target_low")),
        "target_high": _safe_num(row.get("target_high")),
        "target_median": _safe_num(row.get("target_median")),
    }
    if any(v is None for v in trio.values()):
        cache_key = f"analyst_targets|{symbol}"
        hit = _cache_get(cache_key)
        if hit is not None:
            for k in trio:
                if trio[k] is None:
                    trio[k] = hit.get(k)
        else:
            # This runs on 8 pool threads at once (see analyze_portfolios_multi),
            # and Yahoo answers a burst of .info calls by handing some of them an
            # EMPTY dict rather than an error. That is a throttle, not "this name
            # has no published range" — verified directly: the mega-caps that came
            # back thin here return a full low/median/high on a single sequential
            # call moments later. Without the retry the whole batch of biggest
            # holdings rendered "—" in the Upside (median) / Upside range columns,
            # and the 300 s negative cache meant the next refresh usually failed
            # the same way. Retries are jittered and escalating, and happen ONLY on
            # the empty path — same shape as fx_index_history's bulk-then-
            # sequential fallback. Two are needed, not one: with 8 workers a single
            # retry still left roughly one name per batch thin, because the retries
            # themselves collide.
            fetched = _fetch_target_trio(symbol)
            for attempt in range(2):
                if not all(v is None for v in fetched.values()):
                    break
                time.sleep((0.5 + random.random()) * (attempt + 1))
                fetched = _fetch_target_trio(symbol)
            for k in trio:
                if trio[k] is None:
                    trio[k] = fetched[k]
            thin = all(v is None for v in fetched.values())
            _cache_put(cache_key, fetched, ttl=300.0 if thin else _CACHE_TTL_ANALYTICS)
    out.update(trio)
    return out


def analyze_portfolio(
    rows: list[dict], weights_in: dict, period: str, display_ccy: str = "USD"
) -> dict:
    out = analyze_portfolios_multi(
        rows, {"__single__": weights_in or {}}, period, display_ccy=display_ccy
    )
    if isinstance(out, dict) and "error" in out:
        return out
    return (out or {}).get("__single__", {"error": "no result"})


def analyze_portfolios_multi(
    rows: list[dict],
    weight_sets: dict[str, dict],
    period: str,
    display_ccy: str = "USD",
) -> dict:
    period_u = (period or "1Y").upper()
    if period_u not in _PERIOD_YF:
        period_u = "1Y"

    display_ccy = _norm_ccy_for_fx(display_ccy or "USD")

    # Deduped here even though save_view/load_view already do it: rows reach
    # this function straight from the client's DATA array, and one repeated
    # symbol turns `closes[[...]]` below into a frame with duplicate columns —
    # a "float() ... not 'Series'" 500 rather than a wrong number.
    rows = _dedupe_rows_by_symbol([r for r in (rows or []) if r and r.get("symbol")])
    symbols = [str(r["symbol"]) for r in rows]
    if not symbols:
        return {"error": "no symbols"}

    if not isinstance(weight_sets, dict) or not weight_sets:
        weight_sets = {"__single__": {}}

    by_sym = {r["symbol"]: r for r in rows}

    normalized_sets: dict[str, dict[str, float]] = {
        name: _normalize_weights(w_in or {}, symbols) for name, w_in in weight_sets.items()
    }

    per_set_cache_keys: dict[str, str] = {}
    cached_results: dict[str, dict] = {}
    for name, weights in normalized_sets.items():
        key = (
            "pf|"
            + "|".join(f"{s}:{weights[s]:.6f}" for s in sorted(symbols))
            + f"|{period_u}|{display_ccy}"
        )
        per_set_cache_keys[name] = key
        hit = _cache_get(key)
        if hit is not None:
            cached_results[name] = hit
    if len(cached_results) == len(normalized_sets):
        return cached_results

    # One wide fetch covers holdings, benchmarks and sector ETFs; FX-convert it
    # all to the display currency (indices quote in EUR/JPY/KRW, and a USD
    # display still has to convert non-USD holdings).
    warmup_yf = _WARMUP_YF[period_u]
    sec_etfs = sorted(
        {_SECTOR_ETF[sec] for r in rows if (sec := (r.get("sector") or "").strip()) in _SECTOR_ETF}
    )
    bench_tickers = [t for t, _, _ in _BENCHMARKS.values()]
    rf_tickers = [_RF_TICKER] if display_ccy == "USD" else []
    wide = _bulk_close(
        list(dict.fromkeys(symbols + bench_tickers + sec_etfs + rf_tickers)), warmup_yf
    )
    warnings: list[str] = []
    missing = [s for s in symbols if s not in wide.columns]
    if missing:
        warnings.append(f"No price history for: {', '.join(missing)}")
    active = [s for s in symbols if s in wide.columns]
    if not active:
        return {"error": "no price history for portfolio", "warnings": warnings}
    ccy_by_sym = {**by_sym, **{t: {"currency": c} for t, _, c in _BENCHMARKS.values()}}
    wide = _apply_fx_to_closes(wide, ccy_by_sym, display_ccy, warmup_yf)

    # Trading calendar = days any holding traded (foreign indices must not
    # inject their own holidays as zero-return days).
    wide_sym = wide[active].dropna(how="all").ffill().dropna(how="any")
    if len(wide_sym) < 3:
        return {"error": "insufficient overlapping history", "warnings": warnings}
    start = _period_start(period_u, wide_sym.index[-1])
    sym_closes = _period_slice(wide_sym, start)
    if len(sym_closes) < 3:
        return {"error": "insufficient overlapping history", "warnings": warnings}
    common_index = sym_closes.index
    wide_ret = wide_sym.pct_change().fillna(0.0)
    rf_daily = _daily_rf(wide, common_index, display_ccy)
    rf_source = "US 13-week T-bill (^IRX)" if rf_daily is not None else "0 (no short-rate series)"

    def _on_period(col: str) -> pd.Series | None:
        """A benchmark column aligned to the portfolio's calendar, or None."""
        if col not in wide.columns:
            return None
        ser = wide[col].ffill().reindex(common_index).dropna()
        return ser if len(ser) >= 2 else None

    shared_bench = {
        k: (label, v)
        for k, (t, label, _) in _BENCHMARKS.items()
        if (v := _on_period(t)) is not None
    }
    spy_ret = shared_bench["SPY"][1].pct_change().dropna() if "SPY" in shared_bench else None
    sec_ret_df = None
    sec_cols = [e for e in sec_etfs if e in wide.columns]
    if sec_cols:
        sec_ret_df = wide[sec_cols].ffill().reindex(common_index).pct_change().fillna(0.0)

    analyst_blocks: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(active))) as pool:
        futs = {pool.submit(_analyst_for, s, by_sym.get(s, {})): s for s in active}
        for fut in as_completed(futs):
            try:
                analyst_blocks[futs[fut]] = fut.result()
            except Exception:
                analyst_blocks[futs[fut]] = {}

    pe_vals = {s: _safe_num(by_sym.get(s, {}).get("pe_ratio")) for s in active}
    ps_vals = {s: _safe_num(by_sym.get(s, {}).get("ps_ratio")) for s in active}
    ev_vals = {s: analyst_blocks.get(s, {}).get("ev_ebitda") for s in active}
    div_vals = {s: analyst_blocks.get(s, {}).get("div_yield") for s in active}
    # Market caps come in each listing's own currency (a Tokyo cap in yen is
    # ~150x its dollar value), so they are compared and averaged in USD.
    mcap_usd = {s: _market_cap_usd(by_sym.get(s, {})) for s in active}

    period_returns = {
        s: float(sym_closes[s].iloc[-1] / sym_closes[s].iloc[0] - 1.0) * 100.0 for s in active
    }
    # Daily holding returns inside the window (the base bar excluded), for the
    # linked contribution below.
    win_ret = wide_ret.loc[common_index[1:], active]

    results: dict[str, dict] = dict(cached_results)
    for name, weights in normalized_sets.items():
        if name in results:
            continue

        if len(active) != len(symbols):
            weights = _normalize_weights({s: weights.get(s, 0.0) for s in active}, active)

        w_vec = pd.Series([weights[s] for s in active], index=active)
        # Daily-rebalanced index over the whole warm-up window; the reported
        # series is its period slice rebased to 100.
        port_ext = (1.0 + (wide_ret[active] * w_vec).sum(axis=1)).cumprod()
        base = port_ext.loc[common_index[0]]
        port_val = 100.0 * port_ext.loc[common_index] / base
        port_ret = port_val.pct_change().dropna()
        drawdown = (port_val / port_val.cummax() - 1.0) * 100.0
        # min_periods=1: only where no earlier data exists at all (MAX, or a
        # young holding) does an average start on fewer than n bars.
        sma = {
            str(n): _series_to_points(
                100.0 * port_ext.rolling(n, min_periods=1).mean().loc[common_index] / base
            )
            for n in _SMA_WINDOWS
        }

        benchmarks = {
            k: _bench_block(label, v, spy_ret, port_ret, rf_daily)
            for k, (label, v) in shared_bench.items()
        }
        if sec_ret_df is not None:
            sector_alloc: dict[str, float] = {}
            for s in active:
                etf = _SECTOR_ETF.get((by_sym.get(s, {}).get("sector") or "").strip())
                if etf in sec_ret_df.columns:
                    sector_alloc[etf] = sector_alloc.get(etf, 0.0) + weights[s]
            total = sum(sector_alloc.values())
            if total > 0:
                blend = sum(sec_ret_df[etf] * (w / total) for etf, w in sector_alloc.items())
                benchmarks["SECTOR"] = _bench_block(
                    "Sector mix", (1.0 + blend).cumprod(), spy_ret, port_ret, rf_daily
                )

        pf_stats = _stats(port_ret, port_val, rf_daily)
        pf_stats["rf_source"] = rf_source

        mcap_avg_usd = _w_avg(mcap_usd, weights)
        weighted = {
            # Price multiples: weighted HARMONIC mean, the portfolio's
            # look-through multiple (see _w_harmonic). The arithmetic mean of
            # P/Es overweights the expensive names.
            "pe": _w_harmonic(pe_vals, weights),
            "ps": _w_harmonic(ps_vals, weights),
            "ev_ebitda": _w_harmonic(ev_vals, weights),
            # Yields add up, so they average arithmetically.
            "div_yield": _w_avg(div_vals, weights),
            "market_cap": convert_amount(mcap_avg_usd, "USD", display_ccy)
            if mcap_avg_usd is not None
            else None,
            "market_cap_ccy": display_ccy,
            "coverage": {
                k: _coverage(v, weights)
                for k, v in (("pe", pe_vals), ("ps", ps_vals), ("ev_ebitda", ev_vals))
            },
        }

        rating_num = rating_w = upside_num = upside_w = 0.0
        n_analysts_total = 0
        dist_sum = {"strongBuy": 0.0, "buy": 0.0, "hold": 0.0, "sell": 0.0, "strongSell": 0.0}
        dist_w = 0.0
        holdings_out: list[dict] = []
        not_covered: list[dict] = []
        for s in active:
            blk = analyst_blocks.get(s, {})
            row = by_sym.get(s, {})
            w = weights.get(s, 0.0)
            mr = blk.get("mean_rating")
            tgt = blk.get("target_mean")
            tgt_med = blk.get("target_median")
            tgt_lo = blk.get("target_low")
            tgt_hi = blk.get("target_high")
            px = _safe_num(row.get("price")) or blk.get("price")
            na = blk.get("n_analysts")
            dist = blk.get("dist")
            upside = None
            if tgt and px and px > 0:
                upside = (float(tgt) / float(px) - 1.0) * 100.0
                upside_num += upside * w
                upside_w += w
            if mr is not None and math.isfinite(float(mr)):
                rating_num += float(mr) * w
                rating_w += w
            if na is not None and math.isfinite(float(na)):
                n_analysts_total += int(na)
            if dist and isinstance(dist, dict):
                tot_votes = sum(int(v or 0) for v in dist.values())
                if tot_votes > 0:
                    # Each holding's SHARE of votes, weighted by the holding:
                    # weighting raw counts let a name with 50 analysts outvote
                    # an equal-weight name with 5 ten to one.
                    for k in dist_sum:
                        dist_sum[k] += float(dist.get(k, 0) or 0) / tot_votes * w
                    dist_w += w
            has_coverage = bool((na and na > 0) or mr is not None or tgt or dist)
            if has_coverage:
                holdings_out.append(
                    {
                        "symbol": s,
                        "name": row.get("name") or s,
                        "currency": row.get("currency") or blk.get("currency") or "USD",
                        "weight": w,
                        "price": px,
                        "target_mean": tgt,
                        "target_median": tgt_med,
                        "target_low": tgt_lo,
                        "target_high": tgt_hi,
                        "upside_pct": upside,
                        "mean_rating": mr,
                        "rec_key": blk.get("rec_key"),
                        "n_analysts": int(na)
                        if (na is not None and math.isfinite(float(na)))
                        else None,
                        "dist": dist,
                    }
                )
            else:
                not_covered.append({"symbol": s, "name": row.get("name") or s, "weight": w})
        holdings_out.sort(key=lambda h: (-(h.get("weight") or 0), h["symbol"]))
        dist_norm = None
        if dist_w > 0:
            dist_total_w = sum(dist_sum.values())
            if dist_total_w > 0:
                dist_norm = {k: (v / dist_total_w) * 100.0 for k, v in dist_sum.items()}
        analyst = {
            "mean_rating": (rating_num / rating_w) if rating_w > 0 else None,
            "rating_coverage_weight": rating_w,
            "weighted_target_upside_pct": (upside_num / upside_w) if upside_w > 0 else None,
            "target_coverage_weight": upside_w,
            "n_analysts_total": n_analysts_total,
            "distribution_pct": dist_norm,
            "distribution_weight": dist_w,
            "holdings": holdings_out,
            "not_covered": not_covered,
            "covered_count": len(holdings_out),
            "active_count": len(active),
        }

        by_sector: dict[str, float] = {}
        by_industry: dict[str, float] = {}
        by_bucket: dict[str, float] = {}
        for s in active:
            r = by_sym.get(s, {})
            w = weights.get(s, 0.0)
            sec = (r.get("sector") or "Unknown").strip() or "Unknown"
            ind = (r.get("industry") or "Unknown").strip() or "Unknown"
            bucket = _mcap_bucket(mcap_usd.get(s))
            by_sector[sec] = by_sector.get(sec, 0.0) + w
            by_industry[ind] = by_industry.get(ind, 0.0) + w
            by_bucket[bucket] = by_bucket.get(bucket, 0.0) + w

        weights_sorted = sorted(weights.values(), reverse=True)
        top5 = float(sum(weights_sorted[:5])) if weights_sorted else 0.0
        herfindahl = float(sum(w * w for w in weights.values()))
        effective_n = (1.0 / herfindahl) if herfindahl > 0 else 0.0

        linked = _linked_contribution(win_ret, w_vec)
        contribution = []
        for s in active:
            w = weights.get(s, 0.0)
            pr = period_returns.get(s, 0.0)
            contribution.append(
                {
                    "symbol": s,
                    "name": by_sym.get(s, {}).get("name") or s,
                    "weight": w,
                    "period_return": pr,
                    "contribution": linked.get(s, w * pr),
                    "sector": by_sym.get(s, {}).get("sector") or "",
                }
            )
        contribution.sort(key=lambda x: x["contribution"], reverse=True)

        approx = _portfolio_bars(port_val, w_vec, warmup_yf, wide_sym)
        out_one = {
            "period": period_u,
            "display_ccy": display_ccy,
            "weights_applied": weights,
            "active_symbols": active,
            "missing_symbols": missing,
            "series": {
                "portfolio": _series_to_points(port_val),
                "drawdown": _series_to_points(drawdown),
                "sma": sma,
                # Approximate, from the holdings (see _portfolio_bars); empty
                # lists when any holding's bars are missing (the key is always
                # there, so the client knows the payload is current).
                **approx,
            },
            "stats": pf_stats,
            "benchmarks": benchmarks,
            "weighted": weighted,
            "analyst": analyst,
            "exposure": {
                "by_sector": by_sector,
                "by_industry": by_industry,
                "by_bucket": by_bucket,
            },
            "concentration": {"top5": top5, "herfindahl": herfindahl, "effective_n": effective_n},
            "contribution": contribution,
            "warnings": warnings,
        }
        _cache_put(per_set_cache_keys[name], out_one, ttl=_CACHE_TTL_ANALYTICS)
        results[name] = out_one

    return results
