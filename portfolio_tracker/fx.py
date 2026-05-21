"""FX rates, index history, and currency conversion for the denomination selector."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from portfolio_tracker.cache import (
    _FX_CCY_HIST_CACHE, _FX_CCY_HIST_TTL,
    _FX_HIST_CACHE, _FX_HIST_TTL,
    _FX_RATES_CACHE, _FX_RATES_TTL,
)
from portfolio_tracker.helpers import SUPPORTED_FX

_FX_BASKET_MAJORS: list[str] = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD"]


def _norm_ccy_for_fx(ccy: str) -> str:
    c = (ccy or "USD").upper()
    if c in ("GBP", "GBX"):
        return "GBP"
    if c == "ZAC":
        return "ZAR"
    return c


def _fx_usd_series(ccy: str, period_yf: str) -> pd.Series | None:
    ccy = _norm_ccy_for_fx(ccy)
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
    _FX_CCY_HIST_CACHE[key] = (time.time(), series)
    return series


def _apply_fx_to_closes(
    sym_closes: pd.DataFrame,
    by_sym: dict,
    display_ccy: str,
    period_yf: str,
) -> pd.DataFrame:
    display_ccy = _norm_ccy_for_fx(display_ccy)
    syms = list(sym_closes.columns)
    sym_ccy = {s: _norm_ccy_for_fx(by_sym.get(s, {}).get("currency") or "USD") for s in syms}

    need: set[str] = set()
    for c in sym_ccy.values():
        if c != "USD":
            need.add(c)
    if display_ccy != "USD":
        need.add(display_ccy)

    if not need:
        return sym_closes

    pairs = [f"{c}USD=X" for c in need]
    fx_hist: dict[str, pd.Series] = {}
    try:
        raw = yf.download(pairs, period=period_yf, auto_adjust=True, progress=False)
        if not raw.empty:
            if isinstance(raw.columns, pd.MultiIndex):
                cl = raw["Close"]
                for c in need:
                    p = f"{c}USD=X"
                    if p in cl.columns:
                        s = cl[p].dropna()
                        if not s.empty:
                            fx_hist[c] = s
            elif "Close" in raw.columns and len(pairs) == 1:
                c = next(iter(need))
                s = raw["Close"].dropna()
                if not s.empty:
                    fx_hist[c] = s
    except Exception:
        pass

    for c in need:
        if c not in fx_hist:
            s = _fx_usd_series(c, period_yf)
            if s is not None and not s.empty:
                fx_hist[c] = s

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
            tickers=syms, period="5d", interval="1d",
            auto_adjust=True, group_by="ticker", threads=True, progress=False,
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
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _FX_RATES_CACHE[key] = (time.time(), payload)
    return payload


_PERIOD_DOWNLOAD = {
    "1y": ("2y", 365), "1Y": ("2y", 365),
    "6mo": ("6mo", None), "3mo": ("3mo", None),
    "2y": ("2y", None), "5y": ("5y", None), "max": ("max", None),
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
            tickers=syms, period=download_period, interval="1d",
            auto_adjust=True, group_by="ticker", threads=True, progress=False,
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
                        h = yf.Ticker(s).history(period=download_period, auto_adjust=True, actions=False)
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
    basket = combined.mean(axis=1) * 100.0
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
