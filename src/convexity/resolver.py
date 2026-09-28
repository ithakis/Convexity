"""Symbol resolution pipeline — maps fuzzy human input to Yahoo Finance tickers."""

from __future__ import annotations

import math
import re

import yfinance as yf

_TICKER_SHAPE = re.compile(r"^[A-Z0-9][A-Z0-9.\-\^=]{0,9}$")

_EXCHANGE_SUFFIX: dict[str, str] = {
    "NASDAQ": "",
    "NMS": "",
    "NSDQ": "",
    "NDAQ": "",
    "NYSE": "",
    "NYS": "",
    "NYQ": "",
    "AMEX": "",
    "ASE": "",
    "ARCA": "",
    "ARCX": "",
    "PCX": "",
    "BATS": "",
    "CBOE": "",
    "OTC": "",
    "OTCQX": "",
    "OTCQB": "",
    "US": "",
    "USA": "",
    "EU": ".DE",
    "XETRA": ".DE",
    "XETR": ".DE",
    "GER": ".DE",
    "DE": ".DE",
    "FRA": ".F",
    "BER": ".BE",
    "MUN": ".MU",
    "HAM": ".HM",
    "STU": ".SG",
    "SWX": ".SW",
    "SIX": ".SW",
    "CH": ".SW",
    "VIE": ".VI",
    "AT": ".VI",
    "LSE": ".L",
    "LON": ".L",
    "UK": ".L",
    "GB": ".L",
    "EPA": ".PA",
    "PAR": ".PA",
    "FR": ".PA",
    "EURONEXT": ".PA",
    "EURONEXT PARIS": ".PA",
    "AMS": ".AS",
    "NL": ".AS",
    "EURONEXT AMSTERDAM": ".AS",
    "BRU": ".BR",
    "BE": ".BR",
    "EURONEXT BRUSSELS": ".BR",
    "LIS": ".LS",
    "PT": ".LS",
    "EURONEXT LISBON": ".LS",
    "EURONEXT MILAN": ".MI",
    "BIT": ".MI",
    "MIL": ".MI",
    "IT": ".MI",
    "BORSA ITALIANA": ".MI",
    "BME": ".MC",
    "MAD": ".MC",
    "ES": ".MC",
    "BOLSA MADRID": ".MC",
    "ATH": ".AT",
    "GR": ".AT",
    "ATHEX": ".AT",
    "ASEX": ".AT",
    "STO": ".ST",
    "OMX": ".ST",
    "SE": ".ST",
    "STOCKHOLM": ".ST",
    "HEL": ".HE",
    "FI": ".HE",
    "HELSINKI": ".HE",
    "CPH": ".CO",
    "DK": ".CO",
    "COPENHAGEN": ".CO",
    "OSL": ".OL",
    "NO": ".OL",
    "OSLO": ".OL",
    "ICE": ".IC",
    "REYKJAVIK": ".IC",
    "WSE": ".WA",
    "PL": ".WA",
    "WARSAW": ".WA",
    "PRA": ".PR",
    "CZ": ".PR",
    "PRAGUE": ".PR",
    "BUD": ".BD",
    "HU": ".BD",
    "BUDAPEST": ".BD",
    "TYO": ".T",
    "JP": ".T",
    "TOKYO": ".T",
    "HKG": ".HK",
    "HK": ".HK",
    "HKEX": ".HK",
    "HONG KONG": ".HK",
    "SHA": ".SS",
    "SSE": ".SS",
    "SHANGHAI": ".SS",
    "SHE": ".SZ",
    "SZSE": ".SZ",
    "SHENZHEN": ".SZ",
    "TSX": ".TO",
    "TO": ".TO",
    "TORONTO": ".TO",
    "TSXV": ".V",
    "TSX VENTURE": ".V",
    "ASX": ".AX",
    "AU": ".AX",
    "AUSTRALIA": ".AX",
    "NZX": ".NZ",
    "NZ": ".NZ",
    "NSE": ".NS",
    "BSE": ".BO",
    "JSE": ".JO",
    "ZA": ".JO",
    "JOHANNESBURG": ".JO",
    "SGX": ".SI",
    "SG": ".SI",
    "SINGAPORE": ".SI",
    "KRX": ".KS",
    "KR": ".KS",
    "KOREA": ".KS",
    "TWSE": ".TW",
    "TW": ".TW",
    "TAIWAN": ".TW",
    "BMV": ".MX",
    "MX": ".MX",
    "SAO": ".SA",
    "B3": ".SA",
    "BR": ".SA",
    "BVL": ".LM",
    "TASE": ".TA",
    "IL": ".TA",
}

_TAIL_OK = re.compile(r"[A-Z0-9.\-]+")

_RESOLVED_CACHE: dict[str, str] = {}


def _try_prefix_pair(head: str, tail: str) -> tuple[str, str] | None:
    head_up = " ".join(head.strip().upper().split())
    tail_up = tail.strip().upper()
    if not head_up or not tail_up:
        return None
    if head_up not in _EXCHANGE_SUFFIX:
        return None
    suffix = _EXCHANGE_SUFFIX[head_up]
    tail_clean = tail_up.lstrip(".")
    if not _TAIL_OK.fullmatch(tail_clean):
        return None
    return tail_clean + suffix, suffix


def _normalize_exchange_prefix(entry: str) -> tuple[str, str] | None:
    if not entry or "." not in entry:
        return None
    head, _, tail = entry.partition(".")
    forward = _try_prefix_pair(head, tail)
    if forward is not None:
        return forward
    return _try_prefix_pair(tail, head)


def _looks_like_ticker(s: str) -> bool:
    s = s.strip()
    if not s or " " in s:
        return False
    if any(c.islower() for c in s):
        return False
    return bool(_TICKER_SHAPE.match(s))


def _symbol_db_lookup(entry: str, min_score: float = 72.0) -> str | None:
    try:
        from convexity import symbol_db
    except ImportError:
        return None
    try:
        hits = symbol_db.lookup(entry, min_score=min_score, limit=1)
    except Exception:
        return None
    if not hits:
        return None
    return hits[0].ticker


def _yf_symbol_has_data(sym: str) -> bool:
    try:
        fi = yf.Ticker(sym).fast_info
        v = getattr(fi, "last_price", None)
        return v is not None and isinstance(v, (int, float)) and math.isfinite(v) and v > 0
    except Exception:
        return False


def _refine_via_search(tail: str, expected_suffix: str) -> str | None:
    try:
        search = yf.Search(tail, max_results=10, news_count=0)
        quotes = getattr(search, "quotes", None) or []
    except Exception:
        return None
    suf = expected_suffix.upper()
    for q in quotes:
        if not isinstance(q, dict):
            continue
        sym = str(q.get("symbol") or "").strip()
        if not sym:
            continue
        if suf == "" and "." not in sym:
            return sym
        if suf and sym.upper().endswith(suf):
            return sym
    for q in quotes:
        if isinstance(q, dict) and q.get("symbol"):
            return str(q["symbol"]).strip()
    return None


def _normalize_google_colon(entry: str) -> str:
    if ":" not in entry:
        return entry
    head, _, tail = entry.partition(":")
    head = head.strip()
    tail = tail.strip()
    if not head or not tail:
        return entry
    return f"{head}.{tail}"


def resolve_symbol(entry: str) -> str | None:
    entry = entry.strip()
    if not entry:
        return None
    cached = _RESOLVED_CACHE.get(entry)
    if cached:
        return cached

    normalised = _normalize_google_colon(entry)

    mapped = _normalize_exchange_prefix(normalised)
    if mapped is not None:
        symbol, suffix = mapped
        if _yf_symbol_has_data(symbol):
            _RESOLVED_CACHE[entry] = symbol
            return symbol
        tail = symbol[: -len(suffix)] if suffix and symbol.endswith(suffix) else symbol
        refined = _refine_via_search(tail, suffix)
        if refined and _yf_symbol_has_data(refined):
            _RESOLVED_CACHE[entry] = refined
            return refined
        _RESOLVED_CACHE[entry] = symbol
        return symbol

    if _looks_like_ticker(entry):
        upper = entry.upper()
        _RESOLVED_CACHE[entry] = upper
        return upper

    local = _symbol_db_lookup(entry)
    if local:
        _RESOLVED_CACHE[entry] = local
        return local

    try:
        search = yf.Search(entry, max_results=1, news_count=0)
        quotes = getattr(search, "quotes", None) or []
        if quotes and isinstance(quotes[0], dict):
            sym = quotes[0].get("symbol")
            if sym:
                _RESOLVED_CACHE[entry] = sym
                return sym
    except Exception:
        pass
    fallback = entry.upper()
    _RESOLVED_CACHE[entry] = fallback
    return fallback


def _ordered_resolve(entries: list[str]) -> list[str]:
    """Resolve entries preserving order, deduplicating by resolved symbol."""
    seen: set[str] = set()
    result: list[str] = []
    for e in entries:
        sym = resolve_symbol(e)
        if sym and sym not in seen:
            seen.add(sym)
            result.append(sym)
    return result
