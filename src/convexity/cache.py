"""Process-global caches shared across all modules.

Every cache dict lives here so any module can import and mutate the same
object (Python module singletons guarantee identity). Never reassign a
cache dict — only mutate via [] / .pop / .get.
"""

import time

# ----------------------------- Generic TTL cache ----------------------------

_CACHE: dict[str, tuple[float, float, dict]] = {}
_CACHE_TTL_DEFAULT = 300.0
_CACHE_TTL_ANALYTICS = 1800.0


def _cache_get(key: str):
    hit = _CACHE.get(key)
    if hit is None:
        return None
    ts, ttl, val = hit
    if time.time() - ts > ttl:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_put(key: str, val: dict, ttl: float | None = None) -> None:
    _CACHE[key] = (time.time(), ttl if ttl is not None else _CACHE_TTL_DEFAULT, val)


# ----------------------------- Bulk-close cache -----------------------------

_BULK_CLOSE_CACHE: dict[tuple[str, str], tuple[float, object]] = {}
_BULK_CLOSE_TTL = 1800.0
_BULK_CLOSE_NEG_TTL = 60.0
_BULK_CLOSE_MISS = object()


def _bulk_close_get_cached(sym: str, period_yf: str):
    hit = _BULK_CLOSE_CACHE.get((sym, period_yf))
    if hit is None:
        return _BULK_CLOSE_MISS
    ts, ser = hit
    is_empty = ser is None or getattr(ser, "empty", True)
    ttl = _BULK_CLOSE_NEG_TTL if is_empty else _BULK_CLOSE_TTL
    if time.time() - ts > ttl:
        return _BULK_CLOSE_MISS
    return ser


def _bulk_close_put(sym: str, period_yf: str, ser) -> None:
    _BULK_CLOSE_CACHE[(sym, period_yf)] = (time.time(), ser)


# Open/High/Low/Close/Volume per symbol, in the listing's own currency, stored
# beside the bulk close by the same download (analytics._bulk_close). Only the
# approximate portfolio bars read it, so a miss just means "no bars", never a
# fetch: the portfolio chart then falls back to a line.
_BULK_BARS_CACHE: dict[tuple[str, str], tuple[float, object]] = {}


def _bulk_bars_get(sym: str, period_yf: str):
    hit = _BULK_BARS_CACHE.get((sym, period_yf))
    if hit is None or time.time() - hit[0] > _BULK_CLOSE_TTL:
        return None
    return hit[1]


def _bulk_bars_put(sym: str, period_yf: str, df) -> None:
    _BULK_BARS_CACHE[(sym, period_yf)] = (time.time(), df)


# ----------------------------- FX caches ------------------------------------

_FX_RATES_CACHE: dict[str, tuple[float, dict]] = {}
_FX_HIST_CACHE: dict[str, tuple[float, list]] = {}
_FX_CCY_HIST_CACHE: dict[str, tuple[float, object]] = {}
_FX_RATES_TTL = 1800.0
_FX_HIST_TTL = 14400.0
_FX_CCY_HIST_TTL = 14400.0

# ----------------------------- Benchmark cache ------------------------------

_BENCH_CACHE: dict[str, tuple[float, dict]] = {}
_BENCH_TTL = 1800.0
