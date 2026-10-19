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
    _dividends_get,
    _dividends_put,
    _cache_put,
)
from convexity.fetcher import _SECTOR_ETF
from convexity.fx import _apply_fx_to_closes, convert_amount
from convexity.persistence import REBALANCE
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


def _basket_weights(avail: pd.DataFrame, w: pd.Series) -> pd.DataFrame:
    """Each day's weights when some holdings have no price yet: the target
    weights of the ones that do, scaled back up to 100% (rows = days)."""
    m = avail[w.index].astype(float) * w
    tot = m.sum(axis=1)
    return m.div(tot.where(tot > 0), axis=0).fillna(0.0)


def _linked_contribution(
    win_ret: pd.DataFrame,
    w: pd.Series | pd.DataFrame,
    cost: pd.Series | None = None,
    traded: pd.DataFrame | None = None,
    tc: float = 0.0,
) -> dict[str, float]:
    """Each holding's contribution (percentage points) to the portfolio's
    period return, adding up to it exactly.

    The portfolio's daily return is r_p,t = (1 - c_t)(1 + Σ_i w_i,t·r_i,t) - 1,
    with w_t the start-of-day weights (a constant Series, or one row per day
    when they drift between rebalances, see _drift) and c_t the day's trading
    cost. Each holding pays for its own trades, so its day is
    (1 - c_t)·w_i,t·r_i,t - tc·|Δw_i,t|, and the holdings add up to r_p,t.
    Those add up within a day but not across days (compounding), and
    `weight x the holding's own period return` adds up to neither. Carino
    (1999) logarithmic linking scales each day by k_t / k, with
    k_t = ln(1+r_p,t)/r_p,t and k = ln(1+R)/R, so the parts sum to R.
    """
    if win_ret is None or win_ret.empty:
        return {}
    if isinstance(w, pd.Series):
        daily = win_ret.mul(w.reindex(win_ret.columns).fillna(0.0), axis=1)
    else:
        daily = win_ret * w.loc[win_ret.index, win_ret.columns]
    if traded is not None and cost is not None:
        daily = (
            daily.mul(1.0 - cost.loc[win_ret.index], axis=0)
            - tc * traded.loc[win_ret.index, win_ret.columns]
        )
    return {s: v * 100.0 for s, v in _carino_link(daily).items()}


def _rebalance_days(index: pd.DatetimeIndex, freq: str) -> np.ndarray:
    """True on the first trading day of each new calendar period."""
    if freq == "D":
        return np.ones(len(index), dtype=bool)
    if freq == "N" or not len(index):
        return np.zeros(len(index), dtype=bool)
    y, m = index.year.to_numpy(), index.month.to_numpy()
    key = {
        "W": (index - pd.to_timedelta(index.weekday, unit="D")).normalize().to_numpy(),
        "M": y * 12 + m,
        "Q": y * 4 + (m - 1) // 3,
        "S": y * 2 + (m - 1) // 6,
        "Y": y,
    }[freq]
    return np.r_[False, key[1:] != key[:-1]]


def _drift(ret: np.ndarray, target: np.ndarray, reset: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Start-of-day weights of a book set to ``target`` (one row per day) on
    ``reset`` days and left to drift with the prices in between, and each
    day's traded weight |Δw| (what a reset buys and sells). Day 0 starts on
    target with nothing traded. A reset every day keeps the target exactly,
    so the daily-rebalanced index is reproduced bit for bit."""
    n_days, n = ret.shape
    weights, traded = np.empty((n_days, n)), np.zeros((n_days, n))
    w = target[0].copy()
    for t in range(n_days):
        if reset[t]:
            traded[t] = np.abs(target[t] - w)
            w = target[t].copy()
        weights[t] = w
        g = w * (1.0 + ret[t])
        tot = g.sum()
        if tot > 0:
            w = g / tot
    return weights, traded


def _carino_link(daily: pd.DataFrame) -> dict[str, float]:
    """Carino-link daily contributions (rows = days, columns = buckets,
    each cell a fraction of that day's start capital) so they add up to the
    compounded total of the row sums. Fractions in, fractions out. Shared by
    the research tabs and My Investments' per-holding contribution."""
    rp = daily.sum(axis=1).to_numpy(dtype=float)
    total = float(np.prod(1.0 + rp) - 1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        kt = np.where(np.abs(rp) > 1e-12, np.log1p(rp) / rp, 1.0)
    k = math.log1p(total) / total if abs(total) > 1e-12 else 1.0
    scaled = daily.mul(kt / k, axis=0)
    return {s: float(scaled[s].sum()) for s in daily.columns}


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


def _bulk_close(
    symbols: list[str], period: str, adjusted: bool = True, retry: bool = True
) -> pd.DataFrame:
    """Close prices indexed by date, columns = symbols (those that returned data).

    `period` is an app label ("1Y") or a raw yfinance period ("2y", "10y").
    Caches per-symbol. Retries missing symbols individually with backoff.
    A repeated symbol is fetched once (the result is keyed by symbol anyway,
    so a repeat would only cost a wasted download and a doubled retry sleep).

    ``adjusted=False`` returns closes as traded, for My Investments: the book
    books dividends as cash, so a dividend-adjusted close would count each one
    twice. Yahoo's ``auto_adjust=False`` close is still split-adjusted, which
    matches the ledger's quantities in today's shares. These closes have their
    own cache key (``|raw``), store no bars (the portfolio's OHLC is
    adjusted), and bring each symbol's dividends (per current share, quote
    unit: pence for LSE) to ``cache._dividends_put``.

    ``retry=False`` skips the one-by-one retries (each sleeps up to ~5 s):
    for stand-in candidates, where a line Yahoo has no batch data for is
    simply not a candidate. On a book with two young listings the retries
    were 16 of a cold page's 26 seconds (2026-10-12)."""
    symbols = list(dict.fromkeys(symbols or []))
    if not symbols:
        return pd.DataFrame()
    period_yf = _PERIOD_YF.get(period.upper(), period.lower())
    key = period_yf if adjusted else f"{period_yf}|raw"
    actions = {} if adjusted else {"actions": True}

    out: dict[str, pd.Series] = {}
    to_fetch: list[str] = []
    for s in symbols:
        cached = _bulk_close_get_cached(s, key)
        if cached is _BULK_CLOSE_MISS or (
            not adjusted and cached is not None and not cached.empty and _dividends_get(s) is None
        ):
            to_fetch.append(s)  # raw closes and their dividends: both or neither
        elif cached is not None and not cached.empty:
            out[s] = cached

    def keep(sym: str, frame: pd.DataFrame) -> bool:
        """Store one symbol's closes (and its bars or dividends); False when
        the frame has none."""
        try:
            col = frame["Close"].dropna()
        except (KeyError, ValueError):
            return False
        if col.empty:
            return False
        out[sym] = col
        _bulk_close_put(sym, key, col)
        if adjusted:
            _put_bars(sym, period_yf, frame.loc[col.index])
        else:
            divs = frame["Dividends"].dropna() if "Dividends" in frame.columns else col.iloc[:0]
            _dividends_put(sym, divs[divs > 0])
        return True

    if to_fetch:
        try:
            df = yf.download(
                tickers=to_fetch,
                period=period_yf,
                interval="1d",
                auto_adjust=adjusted,
                **actions,
                group_by="ticker",
                threads=True,
                progress=False,
            )
        except Exception:
            df = None
        got: set[str] = set()
        if df is not None and not df.empty:
            multi = isinstance(df.columns, pd.MultiIndex)
            for s in to_fetch if multi else to_fetch[:1]:
                try:
                    if keep(s, df[s] if multi else df):
                        got.add(s)
                except KeyError:
                    continue

        missing = [s for s in to_fetch if s not in got]
        if not retry:
            for s in missing:
                _bulk_close_put(s, key, pd.Series(dtype=float))
            missing = []

        def retry_one(i: int, s: str) -> None:
            time.sleep(0.4 + 0.2 * (i % 4))
            for attempt in range(3):
                try:
                    tk = yf.Ticker(s)
                    h = tk.history(period=period_yf, interval="1d", auto_adjust=adjusted, **actions)
                    # Same date keys as the yf.download path above; the old
                    # tz_convert(None) moved bars to UTC, off by a day for
                    # London and off-midnight for New York.
                    if h is not None and not h.empty and keep(s, by_trading_date(h)):
                        return
                except Exception:
                    pass
                time.sleep(0.6 * (attempt + 1))
            _bulk_close_put(s, key, pd.Series(dtype=float))

        # Four at a time (Ticker.history keeps its state per object, unlike
        # yf.download): one after another, a delisted name or two cost ~5 s
        # each on every call.
        if missing:
            with ThreadPoolExecutor(max_workers=min(4, len(missing))) as pool:
                list(pool.map(retry_one, range(len(missing)), missing))

    if not out:
        return pd.DataFrame()
    return pd.concat(out, axis=1).sort_index()


_WINDOW_YEARS = {"2y": 2, "5y": 5, "10y": 10, "max": 1000}


def prefetch_closes(rows: list[dict], periods, display_ccy: str = "USD") -> None:
    """Warm the caches many portfolios' analytics read (the Excel export):
    one download of every holding, benchmark, sector ETF and the short rate
    over the longest warm-up window, the shorter windows cut from it. Beside
    it (Ticker calls keep their state per object, unlike yf.download): the
    FX histories, and the analyst target range of rows cached before
    fetch_one stored it. One portfolio and period at a time, a cold export
    of 10 portfolios took ~6 minutes (2026-10-13); Yahoo's download of ~150
    tickers over 10 years is ~45 s of it, the floor."""
    from convexity import fx

    windows = sorted({_WARMUP_YF[p] for p in periods if p in _WARMUP_YF}, key=_WINDOW_YEARS.get)
    if not windows:
        return
    by_sym = {str(r["symbol"]): r for r in rows if r.get("symbol")}
    secs = {_SECTOR_ETF[x] for r in rows if (x := (r.get("sector") or "").strip()) in _SECTOR_ETF}
    bench = [t for t, _, _ in _BENCHMARKS.values()]
    syms = list(dict.fromkeys([*by_sym, *bench, *sorted(secs), _RF_TICKER]))
    ccys = (
        {r.get("currency") for r in rows} | {c for _, _, c in _BENCHMARKS.values()} | {display_ccy}
    )
    longest = windows[-1]
    with ThreadPoolExecutor(max_workers=8) as pool:
        side = [pool.submit(fx.prefetch_usd_series, ccys, w) for w in windows]
        side += [pool.submit(_analyst_for, s, r) for s, r in by_sym.items()]
        wide = _bulk_close(syms, longest)
        for f in side:
            with contextlib.suppress(Exception):
                f.result()
    # A listing Yahoo had no prices for: its check downloads too, so not
    # beside the download above (yf.download shares module state).
    for s in by_sym:
        if s not in wide.columns:
            _known_listing(s)
    for w in windows[:-1]:
        for s in syms:
            if _bulk_close_get_cached(s, w) is not _BULK_CLOSE_MISS:
                continue
            col = _bulk_close_get_cached(s, longest)
            if col is _BULK_CLOSE_MISS or col is None or col.empty:
                _bulk_close_put(s, w, pd.Series(dtype=float))  # failed for the longer window too
                continue
            start = col.index[-1] - pd.DateOffset(years=_WINDOW_YEARS[w])
            _bulk_close_put(s, w, col.loc[start:])
            bars = _bulk_bars_get(s, longest)
            if bars is not None and not bars.empty:
                _bulk_bars_put(s, w, bars.loc[start:])


def dividends_cached(sym: str) -> pd.Series | None:
    """The dividends the last as-traded download brought for ``sym``
    (ex-date -> per current share, quote unit), or None when not cached."""
    return _dividends_get(sym)


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
    port_val: pd.Series,
    w_vec: pd.Series | pd.DataFrame,
    period_yf: str,
    conv_closes: pd.DataFrame,
    cost: pd.Series | None = None,
) -> dict | None:
    """Approximate daily open/high/low and traded value for the portfolio index.

    A portfolio has no traded high or low of its own. With start-of-day
    weights w_i,t (constant, or one row per day when they drift between
    rebalances) and the day's trading cost c_t, day t's value is
    P(t-1) * (1 - c_t) * sum_i w_i,t * (1 + r_i,t). Replacing each holding's
    close by its open/high/low on that day gives the bar:

        X_t = P(t-1) * (1 - c_t) * sum_i w_i,t * (1 + r_i,t) * X_i,t / C_i,t,   X in {O, H, L}

    with P(t-1) = P(t) / ((1 - c_t) * sum_i w_i,t (1 + r_i,t)). X/C is taken in the listing's
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
    frame = isinstance(w_vec, pd.DataFrame)
    try:
        for s in w_vec.columns if frame else w_vec.index:
            w = w_vec[s].reindex(idx).fillna(0.0) if frame else w_vec[s]
            if (w <= 0).all() if frame else w <= 0:
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
    if cost is not None:
        growth = growth * (1.0 - cost.reindex(idx).fillna(0.0))
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
    # No mean target on a built row = no analyst coverage (a fund): the range
    # comes from the same Yahoo block, so asking would only cost a call.
    uncovered = out["target_mean"] is None and price is not None
    # A row whose analysts came from its research line (fetcher._apply_research)
    # already carries everything that line has; its own listing has nothing.
    borrowed = bool(row.get("research_analysts"))
    if not uncovered and not borrowed and any(v is None for v in trio.values()):
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


# ------------------------------------------------- short listings' history
# A listing younger than the company (a US company's young Xetra line;
# Gedeon Richter's Budapest ticker was renamed on 2026-10-09 and starts that
# day) used to cut a whole basket's history to its own start. Before a
# short line's first close, the same company's longest other listing stands
# in: converted into the short line's currency at each day's rate and scaled
# to meet it at that first close, so returns are the company's and nothing
# jumps at the join.
_PROXY_TTL_S = 24 * 3600.0
_PROXY: dict[str, tuple[float, tuple[str, ...]]] = {}
_SHORT_ROWS = 5  # a line starting this many rows into the frame is "short"
# A stretch this long with no close inside a listing's history is a hole in
# Yahoo's data, not a quiet market: Novartis's Xetra line (NOT.DE) has none
# from Sep 2019 to Jan 2025, which ffill drew as five flat years.
_GAP_ROWS = 60


def _after_gap(col: pd.Series):
    """The first close after the last hole of more than ``_GAP_ROWS`` rows
    in ``col`` (None when it has none)."""
    pos = np.flatnonzero(col.notna().to_numpy())
    if len(pos) < 2:
        return None
    big = np.flatnonzero(np.diff(pos) > _GAP_ROWS)
    return col.index[pos[big[-1] + 1]] if len(big) else None


_PROXY_DISK_TTL_S = 7 * 24 * 3600.0  # a company's other listings rarely change


def _proxy_disk() -> dict:
    """Found stand-ins saved across launches (``state/proxies.json``): the
    Lookup behind them is several Yahoo calls, which every launch of My
    Investments used to repeat. Market data only; a bad file is ignored."""
    import json

    from convexity import paths

    try:
        d = json.loads(paths.state_file("proxies").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _proxy_disk_put(symbol: str, found: tuple[str, ...]) -> None:
    import json

    from convexity import paths
    from convexity.persistence import _atomic_write

    d = _proxy_disk()
    d[symbol] = [time.time(), list(found)]
    with contextlib.suppress(OSError):
        _atomic_write(paths.state_file("proxies"), json.dumps(d, sort_keys=True))


def known_proxies(symbol: str) -> tuple[str, ...]:
    """Stand-ins already found for ``symbol`` (memory or disk), never a
    fetch: My Investments downloads their closes with the holdings'."""
    hit = _PROXY.get(symbol)
    if hit and time.time() - hit[0] < _PROXY_TTL_S:
        return hit[1]
    row = _proxy_disk().get(symbol)
    if (isinstance(row, list) and len(row) == 2 and isinstance(row[0], int | float)
            and time.time() - row[0] < _PROXY_DISK_TTL_S
            and isinstance(row[1], list) and all(isinstance(t, str) for t in row[1])):  # fmt: skip
        return tuple(row[1])
    return ()


def _proxy_candidates(symbol: str, name: str | None) -> tuple[str, ...]:
    """The same company's other listings: its pack group, else Yahoo's live
    Lookup by name (cached a day in memory, a week on disk)."""
    hit = _PROXY.get(symbol)
    if hit and time.time() - hit[0] < _PROXY_TTL_S:
        return hit[1]
    if saved := known_proxies(symbol):
        _PROXY[symbol] = (time.time(), saved)
        return saved
    from convexity import symbol_db as sdb

    out: list[str] = []
    try:
        h = sdb.get(symbol)
        if h is not None and h.group != sdb.YAHOO_GROUP:
            out = [x for x in sdb.listings(h.group) if x.ticker != symbol]
            name = name or h.name
        if not out:
            # By the bare ticker, then by name: Lookup finds Gedeon Richter's
            # Frankfurt lines for "RICHT", not for its full English name. The
            # line's own hit there names the company better than a short
            # stored name ("Richter").
            hits = [x for q in dict.fromkeys([symbol.split(".")[0], name or ""]) if q
                    for x in sdb.yahoo_lookup(q, limit=10)]  # fmt: skip
            me = [x.name for x in hits if x.ticker == symbol] + [name or ""]
            ref = max(me, key=lambda n: len(n.split()))
            out = [x for x in hits if x.ticker != symbol and sdb.same_company(x.name, ref)]
    except Exception as e:
        print(f"[analytics] no history proxy for {symbol}: {type(e).__name__}", flush=True)
    # Exchange lines first, then re-listing venues; OTC and order-book lines
    # (GEDSF, 0QFP.IL) last: they trade on few days.
    thin = {"PNK", "OQB", "OQX", "IOB", "DXE", "CXE", "AQS"}
    out.sort(key=lambda x: (x.exchange in thin, sdb.relisting(x.ticker, x.exchange)))
    found = tuple(dict.fromkeys(x.ticker for x in out))[:6]
    # An empty answer (often a timeout) is asked again in ten minutes.
    _PROXY[symbol] = (time.time() - (0 if found else _PROXY_TTL_S - 600.0), found)
    if found:
        _proxy_disk_put(symbol, found)
    return found


def extend_history(
    wide: pd.DataFrame, symbols: list[str], ccy_of: dict, names: dict, period: str,
    adjusted: bool = True, fetch=None, need: dict | None = None,
) -> tuple[pd.DataFrame, dict[str, tuple[str, str]]]:  # fmt: skip
    """``wide`` (closes in each listing's quote unit, columns = symbols) with
    every short listing's history extended back by the same company's
    longest other listing. Returns the frame and {symbol: (proxy, first own
    close ISO)} for the ones extended. A line with no usable proxy is left
    as it is. ``fetch(symbols, period)`` gets closes (default: this
    module's ``_bulk_close`` with ``adjusted``). ``need`` ({symbol: ISO
    date}) is when each line's prices are first needed (a book holds it
    from then); without it a line is short when it starts later than most
    of the frame's lines."""

    def fetch_default(syms, period, retry=True):
        if retry:  # the plain call, so a stubbed _bulk_close needs no ``retry``
            return _bulk_close(syms, period, adjusted=adjusted)
        return _bulk_close(syms, period, adjusted=adjusted, retry=False)

    fetch = fetch or fetch_default

    if wide is None or wide.empty or len(wide.index) <= _SHORT_ROWS:
        return wide, {}
    wide = wide.copy()
    lost = [s for s in symbols if s not in wide.columns and _known_listing(s)]
    if lost:
        # A line so new that Yahoo refuses a long period for it (a ticker
        # renamed yesterday answers only "1d"/"5d"): its last month, to join on.
        try:
            recent = fetch(lost, "1mo")
        except Exception:
            recent = None
        for s in lost:
            if recent is not None and s in recent.columns:
                col = recent[s].dropna()
                col.index = pd.DatetimeIndex(col.index).normalize()
                own = col.reindex(pd.DatetimeIndex(wide.index).normalize())
                if own.notna().any():
                    wide[s] = own.to_numpy()
    # Short means younger than most of the basket (or than the frame, when
    # the frame is a fixed period): on MAX, a 2000 start is not short next to
    # Unilever's 1988, and no ADR should stand in for it.
    firsts = sorted(wide.index.get_loc(f) for s in symbols if s in wide.columns
                    and (f := wide[s].first_valid_index()) is not None)  # fmt: skip
    floor = firsts[(len(firsts) - 1) // 2] if len(firsts) >= 3 else 0
    short, gapped = {}, set()
    for s in symbols:
        if s not in wide.columns:
            continue
        fv = wide[s].first_valid_index()
        if fv is None:
            continue
        # A line with a hole starts, for this purpose, after the hole: the
        # same company elsewhere fills everything before it.
        if (g := _after_gap(wide[s])) is not None:
            fv = g
            gapped.add(s)
        if need is not None:
            if s in need and fv > pd.Timestamp(need[s]) + pd.Timedelta(days=7):
                short[s] = fv
        elif wide.index.get_loc(fv) > floor + _SHORT_ROWS:
            short[s] = fv
    if not short:
        return wide, {}
    out, done = wide, {}
    for s, fv in short.items():
        orig = out[s].copy()
        if s in gapped:
            out.loc[out.index < fv, s] = np.nan
        _splice_one(out, done, s, fv, names, ccy_of, period, adjusted, fetch)
        if s not in done:
            out[s] = orig  # no stand-in: the line's own data, hole and all
    return out, done


def _splice_one(out, done, s, fv, names, ccy_of, period, adjusted, fetch) -> None:
    """Extend ``out[s]`` back from ``fv`` with the best stand-in (see
    extend_history); records it in ``done`` when it did."""
    from convexity import fx
    from convexity.investments import _QCCY, _fetch_quote_ccy

    cands = _proxy_candidates(s, names.get(s))
    if not cands:
        return
    try:
        try:
            pw = fetch(list(cands), period, retry=False)
        except TypeError:  # a fetch that takes no ``retry`` (tests' stubs)
            pw = fetch(list(cands), period)
    except Exception:
        return
    start = out.index[0] + pd.Timedelta(days=7)

    def usable(frame):
        clean = {}
        for c in cands:
            if c in frame.columns:
                col = _clean_tail(frame[c].loc[:fv])
                # A stand-in must trade: a new price on at least half its
                # days (Gedeon Richter's London order-book line moved six
                # times in five years: a flat line, not the company).
                if len(col) > 1 and col.index[0] < fv and (col.diff() != 0).mean() >= 0.5:
                    clean[c] = col
        return clean

    clean = usable(pw)
    if adjusted and not any(c.index[0] <= start for c in clean.values()):
        # Yahoo's dividend adjustment can break a whole history (Frankfurt's
        # Richter lines read negative before June 2026); as traded, the
        # same line is fine, and a dividend a year is a small error next
        # to no history at all.
        with contextlib.suppress(Exception):
            try:
                raw = _bulk_close(list(cands), period, adjusted=False, retry=False)
            except TypeError:  # tests' stubs take no ``retry``
                raw = _bulk_close(list(cands), period, adjusted=False)
            clean = usable(raw) or clean
    if not clean:
        return
    # The first line (in _proxy_candidates' order: exchanges before OTC)
    # that reaches back to the frame's start, else the one reaching
    # furthest back.
    best = next((c for c in clean if clean[c].index[0] <= start), None) or min(
        clean, key=lambda c: clean[c].index[0]
    )
    pq = _QCCY.get(best) or _fetch_quote_ccy(best)
    if not pq:
        return
    _QCCY[best] = pq
    idx = out.index
    dates = [str(d)[:10] for d in idx]
    mult = fx.fx_multipliers([pq], ccy_of.get(s) or pq, dates).get(pq)
    if mult is None:
        return
    conv = clean[best].reindex(idx.union(clean[best].index)).ffill().reindex(idx) * mult
    conv = conv.where(idx >= clean[best].index[0])
    at = conv.loc[fv]
    if pd.isna(at) or at <= 0:
        return
    # Minor units on either side (pence, cents) are a constant factor:
    # the scale at the join absorbs them.
    back = (conv * (out.at[fv, s] / at)).loc[idx < fv]
    out.loc[back.index, s] = out.loc[back.index, s].fillna(back)
    done[s] = (best, str(fv)[:10])


def _known_listing(symbol: str) -> bool:
    """Is this a real listing (the pack or Yahoo's Lookup knows it)? A
    ticker Yahoo has never heard of would otherwise cost a second round of
    download retries (~7 s) on every first load."""
    from convexity import symbol_db as sdb

    try:
        return sdb.get(symbol) is not None or any(
            h.ticker == symbol for h in sdb.yahoo_lookup(symbol)
        )
    except Exception:
        return False


def _clean_tail(col: pd.Series) -> pd.Series:
    """The unbroken stretch of a stand-in's closes that ends at the join.
    Yahoo's old data has glitches: Frankfurt's Gedeon Richter line has
    negative closes in 2009, London's Unilever a 52x jump in 1993 (a unit
    change), Xetra's Novartis no close for five years. A close at or below
    zero, a day more than tripling or losing two thirds, or a hole of more
    than three months starts the stretch at the first close after it."""
    col = col.dropna()
    if col.empty:
        return col
    r = col / col.shift(1)
    # More than three months without a close is a hole in the data too.
    # (Marked on the last close before it, so the stretch keeps the first after.)
    gap = pd.DatetimeIndex(col.index).to_series().diff().dt.days.to_numpy() > 92
    hole = pd.Series(np.append(gap[1:], False), index=col.index)
    bad = (col <= 0) | (r > 3) | (r < 1 / 3) | hole
    if bad.any():
        col = col.loc[bad[bad].index[-1] :].iloc[1:]
    return col


def _when(iso: str) -> str:
    d = pd.Timestamp(iso)
    return f"{d:%b} {d.day}, {d.year}"


def analyze_portfolios_multi(
    rows: list[dict],
    weight_sets: dict[str, dict],
    period: str,
    display_ccy: str = "USD",
    rebalance: str = "D",
    tc_bps: float = 0.0,
) -> dict:
    period_u = (period or "1Y").upper()
    rebalance = rebalance if rebalance in REBALANCE else "D"
    tc = min(max(float(tc_bps or 0.0), 0.0), 200.0) / 1e4
    if period_u not in _PERIOD_YF:
        period_u = "1Y"

    display_ccy = major_ccy(display_ccy or "USD")

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
            + f"|{period_u}|{display_ccy}|{rebalance}|{tc:.6f}"
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
    # Short lines first: one Yahoo serves only a few days of (a ticker
    # renamed yesterday) joins here, and is not "missing".
    names = {s: by_sym.get(s, {}).get("name") for s in symbols}
    wide, proxied = extend_history(
        wide, symbols, {s: by_sym.get(s, {}).get("currency") for s in symbols}, names, warmup_yf
    )
    for s, (p, until) in proxied.items():
        warnings.append(f"{names.get(s) or s} ({s}): before {_when(until)} the prices are "
                        f"{p}'s, the same company on another exchange.")  # fmt: skip
    missing = [s for s in symbols if s not in wide.columns]
    if missing:
        warnings.append(f"No price history for: {', '.join(missing)}")
    active = [s for s in symbols if s in wide.columns]
    if not active:
        return {"error": "no price history for portfolio", "warnings": warnings}
    ccy_by_sym = {**by_sym, **{t: {"currency": c} for t, _, c in _BENCHMARKS.values()}}
    wide = _apply_fx_to_closes(wide, ccy_by_sym, display_ccy, warmup_yf)

    # Trading calendar = days any holding traded (foreign indices must not
    # inject their own holidays as zero-return days). A holding that starts
    # later than the others (a young company, no older listing to stand in)
    # no longer cuts everyone's history: before its first close the basket
    # is the other holdings, reweighted (see _basket_returns). The frame
    # starts once at least half the holdings have a price.
    wide_sym = wide[active].dropna(how="all").ffill()
    # Holdings that start within a few rows of each other (a holiday on one
    # exchange on the first day) are not late: start once all have a close.
    firsts = [wide_sym[s].first_valid_index() for s in active]
    prompt = [f for f in firsts if f is not None and wide_sym.index.get_loc(f) <= _SHORT_ROWS]
    if prompt:
        wide_sym = wide_sym.loc[max(prompt) :]
    have = wide_sym.notna().sum(axis=1)
    wide_sym = wide_sym.loc[have >= max(1, (len(active) + 1) // 2)]
    ragged = bool(len(wide_sym)) and bool(wide_sym.isna().any().any())
    late = (
        {s: wide_sym[s].first_valid_index() for s in active if wide_sym[s].isna().any()}
        if ragged
        else {}
    )
    if len(wide_sym) < 3:
        return {"error": "insufficient overlapping history", "warnings": warnings}
    start = _period_start(period_u, wide_sym.index[-1])
    sym_closes = _period_slice(wide_sym, start)
    if len(sym_closes) < 3:
        return {"error": "insufficient overlapping history", "warnings": warnings}
    common_index = sym_closes.index
    for s, fv in late.items():  # only a start inside the period shown is worth saying
        if fv > common_index[0]:
            warnings.append(f"{names.get(s) or s} ({s}) has prices only since {_when(str(fv)[:10])}; "
                            "before that the basket is the other holdings.")  # fmt: skip
    # A day counts for a holding when it has that close and the one before.
    avail = (wide_sym.notna() & wide_sym.shift(1).notna()) if ragged else None
    wide_ret = wide_sym.pct_change().fillna(0.0)
    if ragged:
        # A ragged basket reaches decades back, into Yahoo's glitches
        # (Unilever's London line: x52 in 1993, -98% in 1997, unit changes):
        # a day more than eight-fold or losing over 94% counts as no move.
        # Real days go past -67% or +200% (a biotech's trial, a squeeze);
        # these bounds are unit changes, not markets (adversarial review).
        glitch = (wide_ret > 7.0) | (wide_ret < -0.94)
        if glitch.to_numpy().any():
            wide_ret = wide_ret.mask(glitch, 0.0)
            hit = [s for s in active if glitch[s].loc[common_index[0] :].any()]
            if hit:
                warnings.append("Ignored impossible one-day moves in Yahoo's prices for "
                                f"{', '.join(hit)} (a unit change, not a market move).")  # fmt: skip
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
        s: float(col.iloc[-1] / col.iloc[0] - 1.0) * 100.0
        for s in active
        if len(col := sym_closes[s].dropna())
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
        # The index over the whole warm-up window, set to its weights on the
        # first day of the period shown (free: the weights apply from there)
        # and then on each rebalance day, paying tc on what that trades; a
        # holding starting to trade is a rebalance too. The reported series is
        # its period slice rebased to 100.
        ret_np = wide_ret[active].to_numpy()
        target = (
            _basket_weights(avail, w_vec).to_numpy()
            if ragged
            else np.broadcast_to(w_vec.to_numpy(), ret_np.shape)
        )
        reset = _rebalance_days(wide_ret.index, rebalance)
        if ragged:
            reset |= np.r_[False, (avail.to_numpy()[1:] != avail.to_numpy()[:-1]).any(axis=1)]
        p1 = wide_ret.index.get_loc(common_index[1])
        reset[p1] = True
        w_np, tr_np = _drift(ret_np, target, reset)
        tr_np[p1] = 0.0
        day_w = pd.DataFrame(w_np, index=wide_ret.index, columns=active)
        traded = pd.DataFrame(tr_np, index=wide_ret.index, columns=active)
        cost = pd.Series(tc * tr_np.sum(axis=1), index=wide_ret.index)
        port_ext = ((1.0 + (wide_ret[active] * day_w).sum(axis=1)) * (1.0 - cost)).cumprod()
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
                        "via": row.get("research_symbol") if row.get("research_analysts") else None,
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

        linked = _linked_contribution(win_ret, day_w, cost, traded, tc)
        years = max((common_index[-1] - common_index[0]).days / 365.25, 1e-9)
        win_traded, win_cost = traded.loc[common_index[1:]], cost.loc[common_index[1:]]
        trading = {
            "rebalance": rebalance,
            "tc_bps": round(tc * 1e4, 6),
            "rebalances": int((win_traded.sum(axis=1) > 0).sum()),
            # One-way: half of what is bought and sold, per year.
            "turnover_ann": float(win_traded.to_numpy().sum() / 2.0 / years),
            "cost_drag_ann": float(1.0 - np.prod(1.0 - win_cost.to_numpy()) ** (1.0 / years)),
        }
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

        # A basket whose members change inside the period has no clean bar:
        # the line is drawn. A holding that listed during the warm-up,
        # before the period, leaves the bars intact.
        whole = not sym_closes[active].isna().to_numpy().any()
        approx = (
            _portfolio_bars(port_val, day_w, warmup_yf, wide_sym, cost)
            if whole
            else {"ohlc": [], "volume": []}
        )
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
            "trading": trading,
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
