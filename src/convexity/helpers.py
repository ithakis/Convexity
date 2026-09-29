"""Shared utility functions used across multiple modules."""

import json
import math
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime
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


# config.json key for each env var. Settings -> API keys (keys.py) writes
# this file (mode 0600); it can also be edited by hand.
_CONFIG_KEYS = {
    "FINNHUB_API_KEY": "finnhub_api_key",
    "NVIDIA_API_KEY": "nvidia_api_key",
}


_CONFIG_WARNED: list = []


def _config_secret(env_var: str) -> str:
    key = _CONFIG_KEYS.get(env_var)
    if not key:
        return ""
    from convexity import paths

    cfg = paths.config_file()
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        # Hand-edited and broken: say so (once) rather than reporting a
        # "missing" key with no hint. The error names the file, never content.
        if not _CONFIG_WARNED:
            _CONFIG_WARNED.append(True)
            print(
                f"[config] ignoring {cfg}: {type(exc).__name__} — fix the JSON "
                f"(keys: {', '.join(sorted(_CONFIG_KEYS.values()))})",
                file=sys.stderr,
            )
        return ""
    val = data.get(key) if isinstance(data, dict) else None
    return val.strip() if isinstance(val, str) else ""


def _resolve_secret(env_var: str, filename: str) -> tuple[str, str | None]:
    """(value, source) — source is "env", "config", "legacy" or None.

    The legacy step finds ``filename`` by walking upward from this module's
    directory (NOT via ``_repo_root()`` — a worktree run needs the key from
    the worktree checkout itself, which ``_repo_root()``'s ``.git``-boundary
    search would skip past). It is kept for one release (1.14) and logged when
    used — by name only, never the value.
    """
    env = os.environ.get(env_var, "").strip()
    if env:
        return env, "env"
    cfg = _config_secret(env_var)
    if cfg:
        return cfg, "config"
    search = Path(__file__).resolve().parent
    for _ in range(6):
        try:
            p = search / filename
            if p.is_file():
                val = p.read_text(encoding="utf-8").strip()
                if val:
                    from convexity import paths

                    paths.note_legacy(
                        f"key file {filename}",
                        p,
                        f"move it to {paths.config_file().name} "
                        f'("{_CONFIG_KEYS.get(env_var, "?")}") or set {env_var}',
                    )
                    return val, "legacy"
                return "", None
        except Exception:
            pass
        search = search.parent
    return "", None


def _load_local_secret(env_var: str, filename: str) -> str:
    """Resolve a secret: env var, then ``config.json`` in the data dir, then a
    legacy key file (see ``_resolve_secret``). Shared by finnhub_adapter.py,
    news_sentiment.py and keys.py so all resolve secrets identically."""
    return _resolve_secret(env_var, filename)[0]


def secret_source(env_var: str, filename: str) -> dict:
    """{"set": bool, "source": ...} — never the value. What /api/keys serves."""
    val, src = _resolve_secret(env_var, filename)
    return {"set": bool(val), "source": src if val else None}


# ----------------------------- FX constants ---------------------------------

SUPPORTED_FX: list[str] = [
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "CHF",
    "CAD",
    "AUD",
    "NZD",
    "CNY",
    "ZAR",
    "MXN",
    "SGD",
    "HKD",
    "INR",
]


# ----------------------------- Rate-limit heuristics -------------------------


def _is_rate_limited_error(exc: Exception) -> bool:
    """Yahoo/yfinance don't raise a typed rate-limit exception — this is the
    shared substring heuristic used by every yfinance retry loop in the app
    (fetcher.fetch_one, news_sentiment._fetch_yf_news) so the two don't drift
    out of sync on what counts as "back off and retry" vs "fail fast"."""
    msg = str(exc).lower()
    return "rate" in msg or "429" in msg or "too many" in msg


# ----------------------------- Rate limiting --------------------------------
# The limiters live here rather than in news_sentiment because they are
# PROCESS-GLOBAL budgets against shared upstream quotas, and more than one
# module spends them: news_sentiment (company + market news, NIM scoring) and
# finnhub_adapter (the MSPR / rec-trend columns on every quotes build). While
# the adapter had no limiter of its own, a portfolio build and a news refresh
# could each independently blow through Finnhub's free allowance.

_RATE_LIMIT_BACKOFF_S = 65
_RETRY_SLEEP_S = 5.0
_MAX_RETRIES = 3


class _RateLimiter:
    """Sleeps (not fails) when at the per-minute budget. On 429 the caller
    invokes `penalize()` to backfill the window so subsequent calls wait."""

    def __init__(self, max_per_min: int, name: str):
        self.max = max_per_min
        self.name = name
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, cancel=None) -> None:
        """Block until a slot is free.

        `cancel` is any object with .is_set() (a threading.Event). Without it a
        cancelled refresh job would still sit here for up to a minute per call,
        which makes "cancel" a lie — this is one of the sleep gates that has to
        be interruptible for cancellation to mean anything.
        """
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled(f"{self.name} rate-limit wait cancelled")
            with self._lock:
                now = time.time()
                while self._calls and now - self._calls[0] > 60.0:
                    self._calls.popleft()
                if len(self._calls) < self.max:
                    self._calls.append(now)
                    return
                wait = 60.0 - (now - self._calls[0]) + 0.05
            _notify_rate(
                provider=self.name, reason="budget", retry_in_s=round(min(max(wait, 0.05), 5.0), 2)
            )
            time.sleep(min(max(wait, 0.05), 5.0))

    def penalize(self) -> None:
        """Backfill the rolling window so further acquires block ~60s."""
        with self._lock:
            t = time.time()
            slots = self.max - len(self._calls)
            for _ in range(max(slots, 0)):
                self._calls.append(t)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            now = time.time()
            while self._calls and now - self._calls[0] > 60.0:
                self._calls.popleft()
            return {"used": len(self._calls), "limit": self.max}


class Cancelled(Exception):
    """Raised inside a sleep gate when the owning job was cancelled."""


_FH_LIMITER = _RateLimiter(max_per_min=55, name="finnhub")
_NV_LIMITER = _RateLimiter(max_per_min=60, name="nvidia")
# yfinance has no official rate limit and no app-level throttle anywhere else
# in the codebase (price/analytics paths burst at 5-8 workers). Windowed news
# fetches deeper per ticker AND for benchmark indices, so a full-portfolio
# refresh could realistically burst Yahoo into a 429. This limiter caps just
# the news calls; kept conservative because it shares Yahoo's backend with the
# unthrottled price/analytics traffic.
_YF_LIMITER = _RateLimiter(max_per_min=40, name="yfinance-news")
_YF_MAX_RETRIES = 3


# A single global observer, installed by jobs.py, that turns backoff events
# into `rate_limited` progress frames on the running refresh job.
#
# Global rather than per-thread ON PURPOSE, and only correct because the job
# registry is SINGLE-FLIGHT (jobs.submit rejects a second concurrent job). If
# that guarantee is ever relaxed, rate events will start landing on the wrong
# job and this needs a context-local binding propagated into every
# ThreadPoolExecutor worker instead.
_RATE_OBSERVER = None


def set_rate_observer(fn) -> None:
    """Install (or clear, with None) the rate-limit event sink."""
    global _RATE_OBSERVER
    _RATE_OBSERVER = fn


def _notify_rate(**event) -> None:
    """Best-effort. A broken observer must never take down a fetch."""
    fn = _RATE_OBSERVER
    if fn is None:
        return
    try:
        fn(event)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[rate] observer failed: {type(exc).__name__}: {exc}", file=sys.stderr)


# ----------------------------- Row helpers ----------------------------------


def _dedupe_rows_by_symbol(rows) -> list:
    """One row per symbol: first position, latest row — upsert semantics.

    Every consumer of a rows list treats `symbol` as a key (analytics and the
    frontier index a price frame by it), and a repeated one breaks them in ways
    that look unrelated to the cause: `closes[[...list with a repeat...]]`
    returns duplicate COLUMNS, so `closes[s]` is a DataFrame and a later
    float() raises "not 'Series'" — a 500 on /api/portfolio-analytics-multi.
    Under equal weight a duplicate also silently doubles that holding.

    Position of the FIRST occurrence is kept so the table order doesn't jump;
    the LATER row wins, mirroring the frontend's rfPatchRow — except that a
    good row is never traded for an error row (a failed re-fetch must not erase
    a price we already have). Entries without a symbol pass through untouched:
    this helper dedupes, it doesn't validate.
    """
    out: list = []
    pos: dict[str, int] = {}
    for r in rows or []:
        sym = r.get("symbol") if isinstance(r, dict) else None
        if not sym:
            out.append(r)
            continue
        key = str(sym)
        i = pos.get(key)
        if i is None:
            pos[key] = len(out)
            out.append(r)
        elif not r.get("error") or out[i].get("error"):
            out[i] = r
    return out


# ----------------------------- Numeric helpers ------------------------------


def _safe_num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# Yahoo quotes some exchanges in the currency's minor unit: LSE prices in pence
# (GBp / GBX), JSE in cents (ZAc), Tel Aviv in agorot (ILA). Per-share prices
# and price targets come in that minor unit, but marketCap, dividendRate and the
# statements are in the MAJOR unit — verified on SHEL.L: price 3654.5 GBp,
# marketCap 208e9 (GBP), dividendRate 1.16 (GBP).
MINOR_UNIT_CCY = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}


def major_ccy(ccy: str | None) -> str:
    """The major-unit ISO code for a Yahoo currency ("GBp" -> "GBP")."""
    c = (ccy or "USD").strip()
    return MINOR_UNIT_CCY.get(c) or MINOR_UNIT_CCY.get(c.upper()) or c.upper()


def price_in_major(price, ccy: str | None) -> float | None:
    """A per-share price converted from a minor unit to the major one."""
    px = _safe_num(price)
    if px is None:
        return None
    c = (ccy or "").strip()
    return px / 100.0 if (c in MINOR_UNIT_CCY or c.upper() in MINOR_UNIT_CCY) else px


def _normalize_dividend_yield(
    raw_yield,
    *,
    price=None,
    dividend_rate=None,
    trailing_yield=None,
    trailing_rate=None,
    currency: str | None = None,
    financial_currency: str | None = None,
) -> float | None:
    """Forward dividend yield as a fraction (0.0315 = 3.15%).

    Sources, most reliable first:

    1. ``dividendRate / price`` — the forward indicated annual dividend over the
       last close. Both are per-share amounts in the listing's currency, except
       that a minor-unit listing (GBp, ZAc, ILA) quotes the price in pence /
       cents and the rate in pounds / rand, so the price is scaled to the major
       unit first (SHEL.L: 1.16 / 36.545 = 3.2%, not 1.16 / 3654.5 = 0.03%).
    2. ``dividendYield`` — Yahoo's own forward yield. yfinance >= 1.0 (the
       pyproject floor) reports it in PERCENT (AAPL 0.32 means 0.32%), so it is
       always divided by 100; guessing the unit from its size misread every
       yield under 1% as a fraction (0.32 -> 32%).
    3. The trailing fields — only when the statements are in the listing's
       currency. For an ADR the trailing rate is in the home currency (TSM: 26
       TWD over a 452.88 USD price), so rate / price mixes currencies.
    """
    px = price_in_major(price, currency) if currency else _safe_num(price)
    rate = _safe_num(dividend_rate)
    if px is not None and px > 0 and rate is not None and rate >= 0:
        return float(rate / px)

    val = _safe_num(raw_yield)
    if val is not None and val >= 0:
        return float(val / 100.0)

    same_ccy = (
        not currency
        or not financial_currency
        or major_ccy(currency) == major_ccy(financial_currency)
    )
    if not same_ccy:
        return None
    trate = _safe_num(trailing_rate)
    if px is not None and px > 0 and trate is not None and trate >= 0:
        return float(trate / px)
    tval = _safe_num(trailing_yield)
    if tval is not None and tval >= 0:
        # trailingAnnualDividendYield is a fraction (KO 0.0237 = 2.37%).
        return float(tval)
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
    """Year-to-date return, measured from the LAST CLOSE OF THE PREVIOUS YEAR —
    the convention of every index provider and broker. Using the first close of
    the new year as the base drops the first session's move from YTD. Only a
    security listed this year (no earlier close) falls back to its first close."""
    if series is None or series.empty:
        return None
    last = series.iloc[-1]
    year_start = pd.Timestamp(year=series.index[-1].year, month=1, day=1, tz=series.index.tz)
    before = series[series.index < year_start]
    if not before.empty:
        base = before.iloc[-1]
    else:
        this_year = series[series.index >= year_start]
        if len(this_year) < 2:
            return None
        base = this_year.iloc[0]
    return float((last / base - 1.0) * 100.0)


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
        # No losses in the window: 100 if there were gains; a flat series has
        # neither, and RSI is undefined there — 50 (neutral), not "overbought".
        return 100.0 if float(last_gain) > 0.0 else 50.0
    rs = float(last_gain / last_loss)
    return float(100.0 - (100.0 / (1.0 + rs)))


def _macd_hist_pct(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> float | None:
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
