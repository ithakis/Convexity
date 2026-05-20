"""Finnhub supplemental data adapter — optional, stdlib-only.

WHY this module exists, and the contract it must honour
-------------------------------------------------------
The dashboard's per-row payload comes from yfinance. Finnhub adds three
data points yfinance does not expose cleanly: per-quarter EPS surprise,
Form-4 insider sentiment (MSPR), and the monthly analyst recommendation
trend. We pull those here as a *supplemental* source.

Design mirrors ``symbol_db.py``: standalone, importable without the full
dashboard stack, no new pip dependencies (``urllib.request`` only). The
whole thing degrades to ``None`` when ``FINNHUB_API_KEY`` is unset, so a
user with no key sees the new columns render an em-dash and the server
log stays clean.

Rate-limit reality (Finnhub free tier = 60 calls/min): 3 calls/symbol ×
5 concurrent build workers = up to 15 calls/min on a cold build, within
budget. Every result is TTL-cached, so warm builds make zero calls. The
only thing we log is a single line on HTTP 429 — non-US symbols that
return empty data are silent (that's expected, not an error).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _load_api_key() -> str:
    """Resolve the Finnhub key: env var first, then a strictly-local file.

    Order: ``FINNHUB_API_KEY`` env var → ``.finnhub_key`` sitting next to
    this module. That file is gitignored and a pre-commit guard blocks it
    from ever being staged, so the secret stays local across commits.
    """
    env = os.environ.get("FINNHUB_API_KEY", "").strip()
    if env:
        return env
    try:
        key_file = Path(__file__).resolve().parent / ".finnhub_key"
        if key_file.is_file():
            return key_file.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


FINNHUB_API_KEY = _load_api_key()
_BASE_URL = "https://finnhub.io/api/v1/"

# TTL cache mirroring dashboard.py's (timestamp, ttl, val) tuple shape.
_FH_CACHE: dict[str, tuple[float, float, Any]] = {}

# Negative-cache sentinel. A genuine empty / no-coverage response (e.g. a
# non-US ticker Finnhub doesn't cover) is cached as _MISS so repeated builds
# don't re-hit the API for it — that's what keeps warm builds at zero calls.
# Transient failures (network error, non-200, 429) are NOT cached, so they
# get retried on the next build.
_MISS = object()
_NEG_TTL = 1800.0


def _fh_get(key: str):
    hit = _FH_CACHE.get(key)
    if hit is None:
        return None
    ts, ttl, val = hit
    if time.time() - ts > ttl:
        _FH_CACHE.pop(key, None)
        return None
    return val


def _fh_put(key: str, val: Any, ttl: float) -> None:
    _FH_CACHE[key] = (time.time(), ttl, val)


def _fh_call(path: str, params: dict[str, Any], symbol: str) -> Any | None:
    """Single GET against the Finnhub REST API.

    Returns the parsed JSON (dict or list) on HTTP 200, else ``None``.
    Any exception is swallowed → ``None``. A 429 (rate limit) prints one
    stderr line so the user knows why a column may be sparse; nothing
    else is logged, so empty data for non-US tickers stays quiet.
    """
    query = dict(params)
    query["token"] = FINNHUB_API_KEY
    url = _BASE_URL + path + "?" + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            print(
                f"[finnhub] rate limited on {symbol} — cached result will serve",
                file=sys.stderr,
            )
        return None
    except Exception:
        return None


def _num(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def get_earnings_surprise(symbol: str) -> list[dict] | None:
    """Up to 8 quarters of EPS surprise, most-recent first.

    Each entry normalised to
    ``{period, actual, estimate, surprise_pct}`` where
    ``surprise_pct = (actual - estimate) / abs(estimate) * 100`` (None
    when estimate is 0 or missing). TTL 3600s — only moves on earnings day.
    """
    if not FINNHUB_API_KEY:
        return None
    cache_key = f"earnings|{symbol}"
    cached = _fh_get(cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached
    try:
        raw = _fh_call("stock/earnings", {"symbol": symbol, "limit": 8}, symbol)
        if raw is None:
            return None  # transient failure — don't cache, retry next build
        if not isinstance(raw, list):
            _fh_put(cache_key, _MISS, _NEG_TTL)
            return None
        out: list[dict] = []
        for e in raw[:8]:
            actual = _num(e.get("actual"))
            estimate = _num(e.get("estimate"))
            if estimate in (None, 0) or actual is None:
                surprise_pct = None
            else:
                surprise_pct = (actual - estimate) / abs(estimate) * 100
            out.append({
                "period": e.get("period"),
                "actual": actual,
                "estimate": estimate,
                "surprise_pct": surprise_pct,
            })
        if not out:
            _fh_put(cache_key, _MISS, _NEG_TTL)  # genuine no-coverage
            return None
        _fh_put(cache_key, out, 3600.0)
        return out
    except Exception:
        return None


def get_insider_sentiment(symbol: str) -> dict | None:
    """Most-recent monthly insider sentiment (MSPR). TTL 1800s.

    Return ``{mspr, change, year, month}`` for the entry with the highest
    (year, month). ``None`` if no data (e.g. non-US ticker).
    """
    if not FINNHUB_API_KEY:
        return None
    cache_key = f"insider|{symbol}"
    cached = _fh_get(cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached
    try:
        today = datetime.now(timezone.utc).date()
        frm = (today - timedelta(days=365)).isoformat()
        to = today.isoformat()
        raw = _fh_call(
            "stock/insider-sentiment",
            {"symbol": symbol, "from": frm, "to": to},
            symbol,
        )
        if raw is None:
            return None  # transient failure — don't cache, retry next build
        if not isinstance(raw, dict):
            _fh_put(cache_key, _MISS, _NEG_TTL)
            return None
        data = raw.get("data") or []
        if not data:
            _fh_put(cache_key, _MISS, _NEG_TTL)  # genuine no-coverage
            return None
        latest = max(data, key=lambda d: (d.get("year", 0), d.get("month", 0)))
        out = {
            "mspr": _num(latest.get("mspr")),
            "change": int(latest.get("change") or 0),
            "year": int(latest.get("year") or 0),
            "month": int(latest.get("month") or 0),
        }
        _fh_put(cache_key, out, 1800.0)
        return out
    except Exception:
        return None


def get_recommendation_trend(symbol: str) -> list[dict] | None:
    """6 most-recent monthly recommendation snapshots, newest first. TTL 3600s.

    Each entry carries period/strongBuy/buy/hold/sell/strongSell as
    returned by Finnhub (already newest-first). ``None`` if no data.
    """
    if not FINNHUB_API_KEY:
        return None
    cache_key = f"rec|{symbol}"
    cached = _fh_get(cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached
    try:
        raw = _fh_call("stock/recommendation", {"symbol": symbol}, symbol)
        if raw is None:
            return None  # transient failure — don't cache, retry next build
        if not isinstance(raw, list):
            _fh_put(cache_key, _MISS, _NEG_TTL)
            return None
        out = raw[:6]
        if not out:
            _fh_put(cache_key, _MISS, _NEG_TTL)  # genuine no-coverage
            return None
        _fh_put(cache_key, out, 3600.0)
        return out
    except Exception:
        return None
