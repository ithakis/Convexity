"""FX rates, index history, and currency conversion for the denomination selector."""

import random
import time
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import yfinance as yf

from convexity.cache import (
    _FX_CCY_HIST_CACHE,
    _FX_CCY_HIST_TTL,
    _FX_HIST_CACHE,
    _FX_HIST_TTL,
    _FX_RATES_CACHE,
    _FX_RATES_TTL,
)
from convexity.helpers import SUPPORTED_FX, major_ccy

_FX_BASKET_MAJORS: list[str] = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD"]


# Spot "USD per one unit of ccy", for converting single amounts (market caps,
# statement figures) rather than whole close series. Checked against the shared
# fx_rates("USD") cache first — the frontend loads it on every page open — and
# only falls back to one ccyUSD=X download for currencies outside that basket
# (TWD, KRW, DKK, …). A miss is cached for 10 minutes, a hit for the FX TTL.
_SPOT_CACHE: dict[str, tuple[float, float | None]] = {}
_SPOT_MISS_TTL = 600.0


def usd_per_unit(ccy: str | None, *, allow_fetch: bool = True) -> float | None:
    c = major_ccy(ccy or "USD")
    if c == "USD":
        return 1.0
    now = time.time()
    hit = _SPOT_CACHE.get(c)
    if hit is not None:
        ttl = _FX_RATES_TTL if hit[1] is not None else _SPOT_MISS_TTL
        if now - hit[0] < ttl:
            return hit[1]
    shared = _FX_RATES_CACHE.get("rates|USD")
    if shared and now - shared[0] < _FX_RATES_TTL:
        r = (shared[1].get("rates") or {}).get(c)
        if r and r > 0:
            _SPOT_CACHE[c] = (now, 1.0 / float(r))
            return 1.0 / float(r)
    if not allow_fetch:
        return None
    v = None
    if c in SUPPORTED_FX:
        r = (fx_rates("USD").get("rates") or {}).get(c)
        v = 1.0 / float(r) if r and r > 0 else None
    if v is None:
        v = _fx_latest_close(c, "USD")
        v = v if (v is not None and v > 0) else None
    _SPOT_CACHE[c] = (now, v)
    return v


def convert_amount(amount, from_ccy: str | None, to_ccy: str | None) -> float | None:
    """A major-unit amount from one currency to another, or None without a rate."""
    if amount is None:
        return None
    a, b = major_ccy(from_ccy or "USD"), major_ccy(to_ccy or "USD")
    if a == b:
        return float(amount)
    ua, ub = usd_per_unit(a), usd_per_unit(b)
    if not ua or not ub:
        return None
    return float(amount) * ua / ub


def _fx_usd_series(ccy: str, period_yf: str) -> pd.Series | None:
    ccy = major_ccy(ccy)
    if ccy == "USD":
        return None
    key = f"{ccy}|{period_yf}"
    hit = _FX_CCY_HIST_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _FX_CCY_HIST_TTL:
        return hit[1]
    try:
        df = yf.download(f"{ccy}USD=X", period=period_yf, auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel(1, axis=1)
        series = df["Close"].dropna() if "Close" in df.columns else pd.Series(dtype=float)
    except Exception:
        series = pd.Series(dtype=float)
    # Only full-TTL cache a real series. A transient yfinance failure yields an
    # empty series; caching that for the full 4h TTL would poison every non-USD
    # conversion until it expired (CLAUDE.md §9). Back-date the timestamp so an
    # empty entry expires in ~10s — same short-negative-cache pattern as
    # fx_index_history's empty-result guard.
    if series.empty:
        _FX_CCY_HIST_CACHE[key] = (time.time() - _FX_CCY_HIST_TTL + 10.0, series)
    else:
        _FX_CCY_HIST_CACHE[key] = (time.time(), series)
    return series


def prefetch_usd_series(ccys, period_yf: str = "max") -> None:
    """Fill ``_fx_usd_series``' cache for several currencies at once, in
    parallel (My Investments' cold page read them one after another: ~2 s
    each). Uses ``Ticker.history``, which keeps its state per object, so it
    can run beside a ``yf.download`` (concurrent downloads share yfinance's
    module state). Same dates and closes as the download path (checked
    2026-10-12). A failure caches nothing: the normal path asks again."""
    from concurrent.futures import ThreadPoolExecutor

    todo = []
    for c in dict.fromkeys(major_ccy(c) for c in ccys if c):
        hit = _FX_CCY_HIST_CACHE.get(f"{c}|{period_yf}")
        if c != "USD" and not (
            hit and (time.time() - hit[0]) < _FX_CCY_HIST_TTL and not hit[1].empty
        ):
            todo.append(c)

    def one(c: str) -> None:
        try:
            h = yf.Ticker(f"{c}USD=X").history(period=period_yf, auto_adjust=True)
            series = h["Close"].dropna() if "Close" in h.columns else pd.Series(dtype=float)
        except Exception:
            return
        if series.empty:
            return
        if getattr(series.index, "tz", None) is not None:
            series.index = series.index.tz_localize(None).normalize()
        _FX_CCY_HIST_CACHE[f"{c}|{period_yf}"] = (time.time(), series)

    if todo:
        with ThreadPoolExecutor(max_workers=min(4, len(todo))) as ex:
            list(ex.map(one, todo))


def fx_multipliers(ccys, base: str, dates: list[str]) -> dict[str, np.ndarray | None]:
    """Daily ``base`` units per one unit of each currency, on each ISO date.

    My Investments values a real book in its display currency day by day, at
    each day's own rate. Rates come from Yahoo's ``ccyUSD=X`` history (max
    range, the shared 4 h cache) and are crossed through USD. A date between
    fixings takes the last one on or before it; a date before the history
    starts takes its first rate, the best there is. Minor units (GBp) map to
    their major currency. A currency with no history maps to None, so the
    caller can fall back to today's rate and say so.
    """
    base = major_ccy(base or "USD")
    keys = np.array(dates, dtype="datetime64[D]")

    def usd_per(ccy: str) -> np.ndarray | None:
        if ccy == "USD":
            return np.ones(len(dates))
        s = _fx_usd_series(ccy, "max")
        if s is None or len(s) == 0:
            return None
        s = s.dropna()
        s = s[s > 0]
        if s.empty:
            return None
        idx = np.array([str(d)[:10] for d in s.index], dtype="datetime64[D]")
        order = np.argsort(idx, kind="stable")
        idx, vals = idx[order], s.to_numpy(dtype=float)[order]
        pos = np.searchsorted(idx, keys, side="right") - 1
        return vals[np.maximum(pos, 0)]

    out: dict[str, np.ndarray | None] = {}
    base_usd = usd_per(base)
    for c in dict.fromkeys(ccys):
        m = major_ccy(c or "USD")
        if m == base:
            out[c] = np.ones(len(dates))
            continue
        a = usd_per(m)
        out[c] = a / base_usd if a is not None and base_usd is not None else None
    return out


def _apply_fx_to_closes(
    sym_closes: pd.DataFrame,
    by_sym: dict,
    display_ccy: str,
    period_yf: str,
) -> pd.DataFrame:
    display_ccy = major_ccy(display_ccy)
    syms = list(sym_closes.columns)
    sym_ccy = {s: major_ccy(by_sym.get(s, {}).get("currency") or "USD") for s in syms}

    need: set[str] = set()
    for c in sym_ccy.values():
        if c != "USD":
            need.add(c)
    if display_ccy != "USD":
        need.add(display_ccy)

    if not need:
        return sym_closes

    # Through the per-currency cache (4 h), fetched in parallel: a batch
    # download here ran on every analytics call, uncached (41 s of a cold
    # ten-portfolio export, 2026-10-13).
    prefetch_usd_series(need, period_yf)
    fx_hist = {
        c: s for c in need if (s := _fx_usd_series(c, period_yf)) is not None and not s.empty
    }

    disp_usd = fx_hist.get(display_ccy)
    idx = sym_closes.index
    result = sym_closes.copy()

    for s in syms:
        stock_ccy = sym_ccy[s]
        if stock_ccy == display_ccy:
            continue
        stock_usd = fx_hist.get(stock_ccy)
        if stock_usd is None and disp_usd is None:
            continue
        if stock_usd is None:
            fx = (1.0 / disp_usd).reindex(idx, method="ffill").bfill().fillna(1.0)
        elif disp_usd is None:
            fx = stock_usd.reindex(idx, method="ffill").bfill().fillna(1.0)
        else:
            fx = (stock_usd / disp_usd).reindex(idx, method="ffill").bfill().fillna(1.0)
        result[s] = result[s].values * fx.values

    return result


def _fx_pair_symbol(base: str, quote: str) -> str:
    return f"{base.upper()}{quote.upper()}=X"


def _fx_latest_close(base: str, quote: str) -> float | None:
    if base == quote:
        return 1.0
    try:
        tk = yf.Ticker(_fx_pair_symbol(base, quote))
        hist = tk.history(period="5d", auto_adjust=True, actions=False)
        if hist is None or hist.empty:
            return None
        close = hist["Close"].dropna()
        if close.empty:
            return None
        return float(close.iloc[-1])
    except Exception:
        return None


def fx_rates(base: str = "USD") -> dict:
    base = (base or "USD").upper()
    if base not in SUPPORTED_FX:
        base = "USD"
    key = f"rates|{base}"
    hit = _FX_RATES_CACHE.get(key)
    if hit and time.time() - hit[0] < _FX_RATES_TTL:
        return hit[1]

    quotes = [q for q in SUPPORTED_FX if q != base]
    syms = [_fx_pair_symbol(base, q) for q in quotes]
    out: dict[str, float] = {base: 1.0}
    try:
        df = yf.download(
            tickers=syms,
            period="5d",
            interval="1d",
            auto_adjust=True,
            group_by="ticker",
            threads=True,
            progress=False,
        )
    except Exception:
        df = None
    if df is not None and not df.empty:
        if isinstance(df.columns, pd.MultiIndex):
            for q in quotes:
                sym = _fx_pair_symbol(base, q)
                try:
                    col = df[sym]["Close"].dropna()
                except (KeyError, ValueError):
                    col = pd.Series(dtype=float)
                if not col.empty:
                    out[q] = float(col.iloc[-1])
        else:
            try:
                col = df["Close"].dropna()
                if not col.empty and len(quotes) == 1:
                    out[quotes[0]] = float(col.iloc[-1])
            except (KeyError, ValueError):
                pass
    for q in quotes:
        if q in out:
            continue
        v = _fx_latest_close(base, q)
        if v is not None:
            out[q] = v
    payload = {
        "base": base,
        "rates": out,
        "supported": SUPPORTED_FX,
        "ts": datetime.now(UTC).isoformat(),
    }
    _FX_RATES_CACHE[key] = (time.time(), payload)
    return payload


_PERIOD_DOWNLOAD = {
    "1y": ("2y", 365),
    "1Y": ("2y", 365),
    "6mo": ("6mo", None),
    "3mo": ("3mo", None),
    "2y": ("2y", None),
    "5y": ("5y", None),
    "max": ("max", None),
}


def fx_index_history(base: str, period: str = "1y") -> list:
    base = (base or "USD").upper()
    if base not in SUPPORTED_FX:
        return []
    key = f"hist|{base}|{period}"
    hit = _FX_HIST_CACHE.get(key)
    if hit and time.time() - hit[0] < _FX_HIST_TTL:
        return hit[1]

    download_period, slice_days = _PERIOD_DOWNLOAD.get(period, (period, None))
    others = [c for c in _FX_BASKET_MAJORS if c != base]
    if base not in _FX_BASKET_MAJORS:
        others = list(_FX_BASKET_MAJORS)
    syms = [_fx_pair_symbol(base, c) for c in others]
    try:
        df = yf.download(
            tickers=syms,
            period=download_period,
            interval="1d",
            auto_adjust=True,
            group_by="ticker",
            threads=True,
            progress=False,
        )
    except Exception:
        df = None
    if df is None or df.empty:
        try:
            parts = {}
            for i, s in enumerate(syms):
                if i:
                    time.sleep(0.15 + random.random() * 0.10)
                for attempt in range(2):
                    try:
                        h = yf.Ticker(s).history(
                            period=download_period, auto_adjust=True, actions=False
                        )
                        if h is not None and not h.empty and "Close" in h.columns:
                            parts[s] = h["Close"].dropna()
                            break
                    except Exception:
                        if attempt == 0:
                            time.sleep(0.4 + random.random() * 0.3)
            if parts:
                df = pd.concat({s: pd.DataFrame({"Close": v}) for s, v in parts.items()}, axis=1)
        except Exception:
            df = None
    if df is None or df.empty:
        _FX_HIST_CACHE[key] = (time.time() - _FX_HIST_TTL + 10.0, [])
        return []
    series_list: list[pd.Series] = []
    if isinstance(df.columns, pd.MultiIndex):
        for c in others:
            sym = _fx_pair_symbol(base, c)
            try:
                s = df[sym]["Close"].dropna()
            except (KeyError, ValueError):
                continue
            if s.empty or float(s.iloc[0]) == 0.0:
                continue
            series_list.append(s / float(s.iloc[0]))
    else:
        try:
            s = df["Close"].dropna()
            if not s.empty and float(s.iloc[0]) != 0.0:
                series_list.append(s / float(s.iloc[0]))
        except (KeyError, ValueError):
            pass
    if not series_list:
        _FX_HIST_CACHE[key] = (time.time(), [])
        return []
    combined = pd.concat(series_list, axis=1).ffill().dropna(how="any")
    if combined.empty:
        _FX_HIST_CACHE[key] = (time.time() - _FX_HIST_TTL + 60.0, [])
        return []
    if slice_days is not None and len(combined) > 0:
        cutoff = combined.index[-1] - pd.Timedelta(days=slice_days)
        sliced = combined[combined.index >= cutoff]
        if not sliced.empty:
            combined = sliced
        combined = combined.div(combined.iloc[0]).fillna(1.0)
    # Geometric mean of the relatives, the convention of currency indices (the
    # ICE Dollar Index is a weighted geometric mean). An arithmetic mean of
    # price relatives is biased upward: offsetting moves (x2 against one
    # currency, x0.5 against another) read as +25% instead of 0%.
    basket = np.exp(np.log(combined.where(combined > 0)).mean(axis=1)) * 100.0
    out: list[list[float]] = []
    for ts, v in basket.items():
        if pd.isna(v):
            continue
        try:
            out.append([int(ts.timestamp() * 1000), float(v)])
        except Exception:
            continue
    _FX_HIST_CACHE[key] = (time.time(), out)
    return out
