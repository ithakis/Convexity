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

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from convexity.helpers import (
    _FH_LIMITER,
    _MAX_RETRIES,
    _RETRY_SLEEP_S,
    _load_local_secret,
    _notify_rate,
    _safe_num,
)

# Key resolution (env var, then config.json in the data folder) lives in
# helpers._load_local_secret — shared with news_sentiment.py so both modules
# resolve secrets identically.
FINNHUB_API_KEY = _load_local_secret("FINNHUB_API_KEY")
_BASE_URL = "https://finnhub.io/api/v1/"

# TTL cache mirroring cache.py's (timestamp, ttl, val) tuple shape.
_FH_CACHE: dict[str, tuple[float, float, Any]] = {}

# Negative-cache sentinel. A genuine empty / no-coverage response (e.g. a
# non-US ticker Finnhub doesn't cover) is cached as _MISS so repeated builds
# don't re-hit the API for it — that's what keeps warm builds at zero calls.
# Transient failures (network error, non-200, 429) are NOT cached, so they
# get retried on the next build.
_MISS = object()
_NEG_TTL = 1800.0


def reload_keys() -> None:
    """Re-read the key after Settings -> API keys changed it (it used to be
    read once at import). A changed key drops the cache: its _MISS entries may
    have been recorded while the old key was being rejected."""
    global FINNHUB_API_KEY
    new = _load_local_secret("FINNHUB_API_KEY")
    if new != FINNHUB_API_KEY:
        _FH_CACHE.clear()
    FINNHUB_API_KEY = new


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
    Any exception is swallowed → ``None``, so empty data for non-US tickers
    stays quiet.

    Rate limiting (v1.11.0): this shares ``helpers._FH_LIMITER`` — and
    therefore Finnhub's free per-minute allowance — with news_sentiment.
    Before, it had no limiter and no retry at all: it printed one line on a
    429 and gave up, so a quotes build and a news refresh each burned through
    the same quota independently and ``Rec Δ6M`` / ``MSPR`` silently went
    blank. Retries mirror news_sentiment's (3 attempts, 5 s apart) and emit
    ``rate_limited`` events so a background refresh job can report the wait
    instead of just looking stalled.
    """
    query = dict(params)
    query["token"] = FINNHUB_API_KEY
    url = _BASE_URL + path + "?" + urllib.parse.urlencode(query)
    for attempt in range(_MAX_RETRIES):
        _FH_LIMITER.acquire()
        try:
            with urllib.request.urlopen(url, timeout=6) as resp:
                if resp.status != 200:
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                # "Not on your plan": the free plan's answer to every non-US
                # ticker. Permanent, so callers negative-cache it like an
                # empty answer instead of asking again on every build.
                return {}
            if exc.code == 429 and attempt < _MAX_RETRIES - 1:
                _FH_LIMITER.penalize()
                print(
                    f"[finnhub] 429 on {symbol} (attempt {attempt + 1}/{_MAX_RETRIES}) "
                    f"— sleeping {_RETRY_SLEEP_S}s",
                    file=sys.stderr,
                )
                _notify_rate(
                    provider="finnhub",
                    reason="http_429",
                    symbol=symbol,
                    attempt=attempt + 1,
                    max_attempts=_MAX_RETRIES,
                    retry_in_s=_RETRY_SLEEP_S,
                )
                time.sleep(_RETRY_SLEEP_S)
                continue
            if exc.code == 429:
                print(
                    f"[finnhub] rate limited on {symbol} — cached result will serve",
                    file=sys.stderr,
                )
            return None
        except Exception:
            return None
    return None


def finnhub_symbol(symbol: str) -> str | None:
    """The ticker to ask Finnhub for a row: the company's US line
    (symbol_db.us_line: TTE for TTE.PA, KO for CCC3.DE), in Finnhub's
    class-share spelling (BRK.B). None when the company has no US line —
    the free plan answers 403 to every other listing, so there is nothing
    to ask."""
    try:
        from convexity import symbol_db

        us = symbol_db.us_line(symbol)
    except Exception:
        us = symbol if symbol and "." not in symbol else None
    return us.replace("-", ".") if us else None


def get_insider_sentiment(symbol: str) -> dict | None:
    """Most-recent monthly insider sentiment (MSPR). TTL 1800s.

    Return ``{mspr, change, year, month}`` for the entry with the highest
    (year, month). ``None`` if no data (e.g. non-US ticker).

    The query window is intentionally wide (5 years). Finnhub's free-tier
    insider-sentiment feed lags 1-2 years for many names — a trailing
    12-month window returns *zero* rows for them even though history exists,
    which used to blank the MSPR column. We pull the wide window in the same
    single call (zero extra API cost) and select the newest available month;
    the UI tooltip surfaces its month/year so a stale reading is visible.

    Asked of the company's US line (`finnhub_symbol`); ``via`` names it when
    it is not the row's own ticker. MSPR comes from SEC Form 4, which only
    US companies file: an ADR (TTE, NVO) has none, but KO answers for
    CCC3.DE.
    """
    fsym = finnhub_symbol(symbol)
    if not FINNHUB_API_KEY or not fsym:
        return None
    cache_key = f"insider|{fsym}"
    cached = _fh_get(cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return _via(cached, fsym, symbol)
    try:
        today = datetime.now(UTC).date()
        frm = (today - timedelta(days=5 * 365)).isoformat()
        to = today.isoformat()
        raw = _fh_call(
            "stock/insider-sentiment",
            {"symbol": fsym, "from": frm, "to": to},
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
            "mspr": _safe_num(latest.get("mspr")),
            "change": int(latest.get("change") or 0),
            "year": int(latest.get("year") or 0),
            "month": int(latest.get("month") or 0),
        }
        _fh_put(cache_key, out, 1800.0)
        return _via(out, fsym, symbol)
    except Exception:
        return None


def get_recommendation_trend(symbol: str) -> list[dict] | None:
    """6 most-recent monthly recommendation snapshots, newest first. TTL 3600s.

    Each entry carries period/strongBuy/buy/hold/sell/strongSell as
    returned by Finnhub (already newest-first). ``None`` if no data.

    Asked of the company's US line (`finnhub_symbol`): Finnhub answers an
    ADR with the company's worldwide consensus (TTE comes back as TTE.PA).
    """
    fsym = finnhub_symbol(symbol)
    if not FINNHUB_API_KEY or not fsym:
        return None
    cache_key = f"rec|{fsym}"
    cached = _fh_get(cache_key)
    if cached is _MISS:
        return None
    if cached is not None:
        return cached
    try:
        raw = _fh_call("stock/recommendation", {"symbol": fsym}, symbol)
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


def _via(out: dict, fsym: str, symbol: str) -> dict:
    """``out`` with the line that answered, when it is another listing."""
    return {**out, "via": fsym} if fsym.replace(".", "-") != symbol.upper() else out
