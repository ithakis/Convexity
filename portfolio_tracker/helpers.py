"""Shared utility functions used across multiple modules."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


# ----------------------------- Path helpers ---------------------------------

def _repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in (current.parent, *current.parents):
        if (parent / ".git").exists():
            return parent
    return current.parent


def _load_local_secret(env_var: str, filename: str) -> str:
    """Resolve a secret: env var first, then a strictly-local file.

    Order: ``env_var`` first, then ``filename`` found by walking upward from
    this module's directory (NOT via ``_repo_root()`` — a worktree run needs
    the key from the worktree checkout itself, which ``_repo_root()``'s
    ``.git``-boundary search would skip past). Shared by finnhub_adapter.py
    and news_sentiment.py so both modules resolve secrets identically instead
    of maintaining two independently-drifting copies of this walk-up loop.
    """
    env = os.environ.get(env_var, "").strip()
    if env:
        return env
    search = Path(__file__).resolve().parent
    for _ in range(6):
        try:
            p = search / filename
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        search = search.parent
    return ""


# ----------------------------- FX constants ---------------------------------

SUPPORTED_FX: list[str] = [
    "USD", "EUR", "GBP", "JPY", "CHF",
    "CAD", "AUD", "NZD", "CNY", "ZAR",
    "MXN", "SGD", "HKD", "INR",
]


# ----------------------------- Rate-limit heuristics -------------------------

def _is_rate_limited_error(exc: Exception) -> bool:
    """Yahoo/yfinance don't raise a typed rate-limit exception — this is the
    shared substring heuristic used by every yfinance retry loop in the app
    (fetcher.fetch_one, news_sentiment._fetch_yf_news) so the two don't drift
    out of sync on what counts as "back off and retry" vs "fail fast"."""
    msg = str(exc).lower()
    return "rate" in msg or "429" in msg or "too many" in msg


# ----------------------------- Numeric helpers ------------------------------

def _safe_num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _normalize_dividend_yield(
    raw_yield,
    *,
    price=None,
    dividend_rate=None,
    trailing_yield=None,
    trailing_rate=None,
) -> float | None:
    """Return dividend yield as a fraction (0.0315 = 3.15%).

    Yahoo sometimes returns dividendYield as a fraction and sometimes as a
    percent-like number. Prefer the explicit dividend-rate/price ratio when
    available, then fall back to raw yield fields with percent-to-fraction
    correction for values above 1.
    """
    px = _safe_num(price)
    if px is not None and px > 0:
        for rate_value in (dividend_rate, trailing_rate):
            rate = _safe_num(rate_value)
            if rate is not None and rate >= 0:
                return float(rate / px)

    for yield_value in (raw_yield, trailing_yield):
        val = _safe_num(yield_value)
        if val is None or val < 0:
            continue
        return float(val / 100.0) if val > 1.0 else float(val)
    return None


# ----------------------------- Technical indicators -------------------------

def _pct_change(series: pd.Series, lookback_days: int) -> float | None:
    if series is None or series.empty:
        return None
    last = series.iloc[-1]
    end_date = series.index[-1]
    target = end_date - pd.Timedelta(days=lookback_days)
    prior = series[series.index <= target]
    if prior.empty:
        return None
    return float((last / prior.iloc[-1] - 1.0) * 100.0)


def _ytd_change(series: pd.Series) -> float | None:
    if series is None or series.empty:
        return None
    last = series.iloc[-1]
    year_start = pd.Timestamp(year=series.index[-1].year, month=1, day=1, tz=series.index.tz)
    prior = series[series.index >= year_start]
    if prior.empty:
        return None
    return float((last / prior.iloc[0] - 1.0) * 100.0)


def _rsi(series: pd.Series, period: int = 14) -> float | None:
    if series is None or len(series) < period + 1:
        return None
    delta = series.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    last_gain = avg_gain.iloc[-1]
    last_loss = avg_loss.iloc[-1]
    if pd.isna(last_gain) or pd.isna(last_loss):
        return None
    if float(last_loss) == 0.0:
        return 100.0
    rs = float(last_gain / last_loss)
    return float(100.0 - (100.0 / (1.0 + rs)))


def _macd_hist_pct(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> float | None:
    if series is None or len(series) < slow + signal:
        return None
    ema_fast = series.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = series.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line.iloc[-1] - signal_line.iloc[-1]
    last = series.iloc[-1]
    if pd.isna(hist) or pd.isna(last) or not last:
        return None
    return float(hist / last * 100.0)


def _bollinger_pct_b(series: pd.Series, period: int = 20, width: float = 2.0) -> float | None:
    if series is None or len(series) < period:
        return None
    window = series.tail(period)
    mid = float(window.mean())
    std = float(window.std(ddof=0))
    if not math.isfinite(mid) or not math.isfinite(std):
        return None
    if std == 0.0:
        return 0.5
    upper = mid + width * std
    lower = mid - width * std
    band = upper - lower
    if band == 0.0:
        return None
    return float((float(window.iloc[-1]) - lower) / band)


# ----------------------------- Series conversion ----------------------------

def _series_to_points(s: pd.Series) -> list[list[float]]:
    """Compact [[ts_ms, close], ...] list, dropping NaN."""
    out: list[list[float]] = []
    for ts, v in s.items():
        if pd.isna(v):
            continue
        try:
            ms = int(ts.timestamp() * 1000)
        except Exception:
            continue
        out.append([ms, float(v)])
    return out


# ----------------------------- JSON serialization ---------------------------

def _json_default(o):
    if isinstance(o, (np.floating,)):
        v = float(o)
        return v if math.isfinite(v) else None
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    raise TypeError(f"not serializable: {type(o)}")


def _safe_json(payload: dict) -> bytes:
    return (json.dumps(payload, default=_json_default) + "\n").encode("utf-8")
