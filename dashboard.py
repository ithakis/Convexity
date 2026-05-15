"""Portfolio Dashboard — single-file local web app.

Run:   python dashboard.py
Browser opens to a dashboard. Paste tickers or names, press Build, and the
table populates with prices, valuation multiples, returns, drawdowns,
sparklines, RS-rank histograms and moving-average flags.

Backed by yfinance with retry + reduced concurrency to dodge Yahoo's rate
limiter. Logos pulled from financialmodelingprep.com with Parqet and an
initial-badge fallback.
"""

from __future__ import annotations

import json
import math
import random
import re
import socket
import threading
import time
import warnings
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

warnings.filterwarnings("ignore")
# yfinance 1.0 + pandas 4 emit a flood of deprecation noise on every call;
# silence by category before importing so the terminal stays readable.
try:
    from pandas.errors import Pandas4Warning  # type: ignore

    warnings.simplefilter("ignore", Pandas4Warning)
except Exception:
    pass
warnings.simplefilter("ignore", DeprecationWarning)
warnings.simplefilter("ignore", FutureWarning)

import logging
from pathlib import Path

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

import numpy as np
import pandas as pd
import yfinance as yf

# ----------------------------- Rate-limit handling -------------------------
# yfinance 1.0 ships its own curl_cffi-based session (with TLS fingerprinting
# that dodges most of Yahoo's anti-bot filtering); passing a plain
# requests.Session crashes it. So we let YF manage its own session and just
# control concurrency + add backoff at our layer.


# ----------------------------- Cache --------------------------------------

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 120.0
_WATCHLISTS_LOCK = threading.Lock()


def _cache_get(key: str):
    hit = _CACHE.get(key)
    if hit is None:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_put(key: str, val: dict) -> None:
    _CACHE[key] = (time.time(), val)


def _repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in (current.parent, *current.parents):
        if (parent / ".git").exists():
            return parent
    return current.parent


_WATCHLISTS_FILE = _repo_root() / ".portfolio_tracker_watchlists.json"
_VIEWS_FILE = _repo_root() / ".portfolio_tracker_views.json"
_LEGACY_SESSION_FILE = _repo_root() / ".portfolio_tracker_session.json"
_VIEWS_LOCK = threading.Lock()

_CURRENT_KEY = "__current__"


def _watchlists_path() -> Path:
    return Path(_WATCHLISTS_FILE)


def _views_path() -> Path:
    return Path(_VIEWS_FILE)


def _read_views_raw() -> dict:
    """Read and return the raw views map. Migrates the legacy single-session
    file in-place if present."""
    path = _views_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data
    # Migrate legacy single-session file, if any.
    legacy = Path(_LEGACY_SESSION_FILE)
    if legacy.exists():
        try:
            legacy_data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            legacy_data = None
        if isinstance(legacy_data, dict) and legacy_data.get("rows"):
            views = {
                "views": {_CURRENT_KEY: legacy_data},
                "last_view": _CURRENT_KEY,
            }
            try:
                path.write_text(json.dumps(views, indent=2) + "\n", encoding="utf-8")
                legacy.unlink()
            except OSError:
                pass
            return views
        try:
            legacy.unlink()
        except OSError:
            pass
    return {}


def _write_views_raw(raw: dict) -> None:
    body = json.dumps(raw, default=_json_default, ensure_ascii=True, indent=2)
    _views_path().write_text(body + "\n", encoding="utf-8")


def list_views() -> dict:
    """Return {views: {name: meta}, last_view: name} — meta excludes the
    heavy `rows` payload so the listing endpoint stays small."""
    with _VIEWS_LOCK:
        raw = _read_views_raw()
    views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
    out: dict[str, dict] = {}
    for name, entry in views_map.items():
        if not isinstance(entry, dict):
            continue
        rows = entry.get("rows") or []
        out[name] = {
            "entries": entry.get("entries") or "",
            "saved_at": entry.get("saved_at"),
            "row_count": len(rows) if isinstance(rows, list) else 0,
            "stale": bool(entry.get("stale")),
        }
    return {"views": out, "last_view": raw.get("last_view")}


def load_view(name: str) -> dict:
    with _VIEWS_LOCK:
        raw = _read_views_raw()
    if not name:
        return {}
    views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
    entry = views_map.get(name)
    if not isinstance(entry, dict):
        return {}
    return entry


def save_view(name: str, entries: str, rows: list, *, set_last: bool = True) -> dict:
    clean_name = (name or "").strip() or _CURRENT_KEY
    payload = {
        "entries": str(entries or "").strip(),
        "rows": rows or [],
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "stale": False,
    }
    with _VIEWS_LOCK:
        raw = _read_views_raw()
        views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
        views_map[clean_name] = payload
        raw["views"] = views_map
        if set_last:
            raw["last_view"] = clean_name
        _write_views_raw(raw)
    return payload


def mark_view_stale(name: str, entries: str) -> bool:
    """Mark an existing view as stale (constituents edited) and update its
    entries field. Returns True if a view existed and was updated."""
    clean_name = (name or "").strip()
    if not clean_name:
        return False
    with _VIEWS_LOCK:
        raw = _read_views_raw()
        views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
        entry = views_map.get(clean_name)
        if not isinstance(entry, dict):
            return False
        entry["entries"] = str(entries or "").strip()
        entry["stale"] = True
        views_map[clean_name] = entry
        raw["views"] = views_map
        _write_views_raw(raw)
    return True


def set_last_view(name: str) -> None:
    clean_name = (name or "").strip()
    with _VIEWS_LOCK:
        raw = _read_views_raw()
        if clean_name:
            views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
            if clean_name not in views_map:
                return
        raw["last_view"] = clean_name or None
        _write_views_raw(raw)


def delete_view(name: str) -> None:
    clean_name = (name or "").strip()
    with _VIEWS_LOCK:
        raw = _read_views_raw()
        views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
        views_map.pop(clean_name, None)
        raw["views"] = views_map
        if raw.get("last_view") == clean_name:
            raw["last_view"] = None
        _write_views_raw(raw)


def load_watchlists() -> dict[str, str]:
    watchlists_path = _watchlists_path()
    with _WATCHLISTS_LOCK:
        if not watchlists_path.exists():
            return {}
        try:
            raw = json.loads(watchlists_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    watchlists: dict[str, str] = {}
    for name, entries in raw.items():
        clean_name = str(name).strip()
        clean_entries = str(entries).strip()
        if clean_name and clean_entries:
            watchlists[clean_name] = clean_entries
    return watchlists


def save_watchlists(watchlists: dict[str, str]) -> dict[str, str]:
    watchlists_path = _watchlists_path()
    cleaned: dict[str, str] = {}
    for name, entries in watchlists.items():
        clean_name = str(name).strip()
        clean_entries = str(entries).strip()
        if clean_name and clean_entries:
            cleaned[clean_name] = clean_entries
    payload = json.dumps(cleaned, ensure_ascii=True, indent=2, sort_keys=True)
    with _WATCHLISTS_LOCK:
        watchlists_path.write_text(payload + "\n", encoding="utf-8")
    return cleaned


def upsert_watchlist(name: str, entries: str) -> tuple[dict[str, str], bool]:
    """Upsert a watchlist. Returns (watchlists_after, entries_changed)."""
    clean_name = name.strip()
    clean_entries = entries.strip()
    if not clean_name:
        raise ValueError("watchlist name required")
    if not clean_entries:
        raise ValueError("watchlist entries required")
    watchlists = load_watchlists()
    prior = watchlists.get(clean_name)
    entries_changed = (prior is None) or (prior != clean_entries)
    watchlists[clean_name] = clean_entries
    saved = save_watchlists(watchlists)
    if entries_changed:
        mark_view_stale(clean_name, clean_entries)
    return saved, entries_changed


def delete_watchlist(name: str) -> dict[str, str]:
    clean_name = name.strip()
    watchlists = load_watchlists()
    watchlists.pop(clean_name, None)
    saved = save_watchlists(watchlists)
    if clean_name:
        delete_view(clean_name)
    return saved


# ----------------------------- Symbol resolution --------------------------

_TICKER_SHAPE = re.compile(r"^[A-Z0-9][A-Z0-9.\-\^=]{0,9}$")


def _looks_like_ticker(s: str) -> bool:
    s = s.strip()
    if not s or " " in s:
        return False
    if any(c.islower() for c in s):
        return False
    return bool(_TICKER_SHAPE.match(s))


def resolve_symbol(entry: str) -> str | None:
    entry = entry.strip()
    if not entry:
        return None
    if _looks_like_ticker(entry):
        return entry.upper()
    try:
        search = yf.Search(entry, max_results=1, news_count=0)
        quotes = getattr(search, "quotes", None) or []
        if quotes and isinstance(quotes[0], dict):
            sym = quotes[0].get("symbol")
            if sym:
                return sym
    except Exception:
        pass
    return entry.upper()


# ----------------------------- Math helpers --------------------------------


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


def _safe_info(tk: yf.Ticker) -> dict:
    """Pull .info defensively. Yahoo sometimes returns None; we coalesce."""
    info: dict = {}
    try:
        raw = tk.info
        if isinstance(raw, dict):
            info = raw
    except Exception:
        info = {}
    try:
        fi = tk.fast_info
        if fi is not None:
            for k in ("market_cap", "currency", "exchange", "shares", "last_price"):
                try:
                    v = getattr(fi, k, None)
                except Exception:
                    v = None
                if v is not None and info.get(k) in (None, ""):
                    info[k] = v
    except Exception:
        pass
    return info


# ----------------------------- Per-symbol fetch ---------------------------


def fetch_one(symbol: str, max_attempts: int = 3) -> dict:
    out: dict = {"symbol": symbol}
    last_err: str | None = None
    for attempt in range(max_attempts):
        try:
            tk = yf.Ticker(symbol)
            hist = tk.history(period="2y", auto_adjust=True, actions=False)
            if hist is None or getattr(hist, "empty", True):
                last_err = "no data"
                time.sleep(0.7 + attempt * 1.2 + random.random() * 0.3)
                continue
            close = hist["Close"].dropna()
            if close.empty:
                last_err = "no data"
                time.sleep(0.7 + attempt * 1.2)
                continue

            last = float(close.iloc[-1])
            prev_close = float(close.iloc[-2]) if len(close) >= 2 else last
            out["price"] = last
            out["change_abs_1d"] = last - prev_close
            out["pct_1d"] = (last / prev_close - 1.0) * 100.0 if prev_close else None
            out["pct_1w"] = _pct_change(close, 7)
            out["pct_1m"] = _pct_change(close, 30)
            out["pct_3m"] = _pct_change(close, 91)
            out["pct_6m"] = _pct_change(close, 182)
            out["pct_ytd"] = _ytd_change(close)
            out["pct_1y"] = _pct_change(close, 365)

            last_year = close[close.index >= (close.index[-1] - pd.Timedelta(days=365))]
            if not last_year.empty:
                out["w52_high"] = float(last_year.max())
                out["w52_low"] = float(last_year.min())
            ath = float(close.max())
            out["ath"] = ath
            out["delta_ath"] = (last / ath - 1.0) * 100.0 if ath else None

            out["sparkline"] = [float(x) for x in close.tail(252).to_list() if pd.notna(x)]
            try:
                out["volume"] = float(hist["Volume"].iloc[-1]) if "Volume" in hist else None
            except Exception:
                out["volume"] = None

            for n in (20, 50, 200):
                if len(close) >= n:
                    sma_val = float(close.tail(n).mean())
                    out[f"sma_{n}"] = sma_val
                    out[f"above_sma_{n}"] = last > sma_val
                else:
                    out[f"sma_{n}"] = None
                    out[f"above_sma_{n}"] = None

            if len(close) >= 22:
                out["above_1m"] = last > float(close.iloc[-22])
            elif len(close) >= 2:
                out["above_1m"] = last > float(close.iloc[0])
            else:
                out["above_1m"] = None

            # RS-rank histogram: 12 monthly samples of where price sat within
            # its trailing-12-month range (0..1).
            monthly = close.resample("ME").last().dropna().tail(12)
            rs: list[float] = []
            for ts in monthly.index:
                window = close[(close.index <= ts) & (close.index >= ts - pd.Timedelta(days=365))]
                if len(window) < 2:
                    continue
                lo, hi = float(window.min()), float(window.max())
                v = float(monthly.loc[ts])
                rs.append(0.0 if hi == lo else (v - lo) / (hi - lo))
            out["rs_rank"] = rs

            info = _safe_info(tk)
            out["name"] = info.get("longName") or info.get("shortName") or symbol
            out["market_cap"] = info.get("marketCap") or info.get("market_cap")
            out["currency"] = info.get("currency") or "USD"
            out["exchange"] = info.get("exchange") or info.get("fullExchangeName") or ""
            out["sector"] = info.get("sector") or ""
            out["industry"] = info.get("industry") or ""
            out["quote_type"] = (info.get("quoteType") or "").upper()
            out["website"] = info.get("website") or ""

            ps = info.get("priceToSalesTrailing12Months")
            try:
                out["ps_ratio"] = float(ps) if ps not in (None, "") else None
            except (TypeError, ValueError):
                out["ps_ratio"] = None
            pe = info.get("trailingPE") or info.get("forwardPE")
            try:
                out["pe_ratio"] = float(pe) if pe not in (None, "") and float(pe) > 0 else None
            except (TypeError, ValueError):
                out["pe_ratio"] = None

            return out  # success

        except Exception as exc:
            msg = str(exc)
            last_err = msg[:200] if msg else type(exc).__name__
            # Heuristic: rate-limit type messages → sleep longer.
            low = msg.lower()
            if "rate" in low or "429" in low or "too many" in low:
                time.sleep(2.0 + attempt * 2.0 + random.random() * 0.5)
            else:
                time.sleep(0.6 + attempt * 1.2 + random.random() * 0.4)

    out["error"] = last_err or "fetch failed"
    return out


def _ordered_resolve(entries: list[str]) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for e in entries:
        sym = resolve_symbol(e)
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols


def fetch_portfolio(entries: list[str]) -> list[dict]:
    cache_key = "|".join(sorted(set(entries)))
    cached = _cache_get(cache_key)
    if cached:
        return cached["rows"]
    symbols = _ordered_resolve(entries)
    rows: list[dict] = []
    if symbols:
        # Modest concurrency dramatically reduces 429s.
        workers = min(5, len(symbols))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_one, s): s for s in symbols}
            for fut in as_completed(futures):
                rows.append(fut.result())
        order = {s: i for i, s in enumerate(symbols)}
        rows.sort(key=lambda r: order.get(r["symbol"], 9999))
    _cache_put(cache_key, {"rows": rows})
    return rows


# ----------------------------- Detail fetch --------------------------------
# Benchmark cache: SPY + sector ETFs are reused across many symbols. Keep
# them on a longer TTL than per-symbol data so the detail modal stays snappy.

_BENCH_CACHE: dict[str, tuple[float, dict]] = {}
_BENCH_TTL = 1800.0  # 30 minutes


_SECTOR_ETF = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Financial": "XLF",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Communication Services": "XLC",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}


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


def _fetch_bench_history(symbol: str, period: str = "10y") -> pd.Series | None:
    key = f"{symbol}|{period}"
    hit = _BENCH_CACHE.get(key)
    if hit and time.time() - hit[0] <= _BENCH_TTL:
        return hit[1].get("close")
    try:
        tk = yf.Ticker(symbol)
        hist = tk.history(period=period, auto_adjust=True, actions=False)
        if hist is None or hist.empty:
            return None
        close = hist["Close"].dropna()
        _BENCH_CACHE[key] = (time.time(), {"close": close})
        return close
    except Exception:
        return None


def _aligned_pct(a: pd.Series, b: pd.Series, days: int) -> tuple[float | None, float | None]:
    """Return last-period pct change for a and b aligned on dates."""
    if a is None or a.empty or b is None or b.empty:
        return (None, None)
    end = min(a.index[-1], b.index[-1])
    start_target = end - pd.Timedelta(days=days)
    a_w = a[a.index <= end]
    b_w = b[b.index <= end]
    a_p = a_w[a_w.index <= start_target]
    b_p = b_w[b_w.index <= start_target]
    if a_p.empty or b_p.empty:
        return (None, None)
    return (
        float((a_w.iloc[-1] / a_p.iloc[-1] - 1.0) * 100.0),
        float((b_w.iloc[-1] / b_p.iloc[-1] - 1.0) * 100.0),
    )


def _beta_corr(stock_close: pd.Series, bench_close: pd.Series) -> tuple[float | None, float | None]:
    """Daily-return beta and Pearson correlation vs benchmark, 1-year window."""
    if stock_close is None or bench_close is None:
        return (None, None)
    df = pd.concat([stock_close.rename("s"), bench_close.rename("b")], axis=1, join="inner").dropna()
    if len(df) < 30:
        return (None, None)
    df = df.tail(252)
    rs = df["s"].pct_change().dropna()
    rb = df["b"].pct_change().dropna()
    common = rs.index.intersection(rb.index)
    rs, rb = rs.loc[common], rb.loc[common]
    if len(rs) < 20:
        return (None, None)
    var_b = float(rb.var())
    cov = float(rs.cov(rb))
    beta = cov / var_b if var_b else None
    try:
        corr = float(rs.corr(rb))
    except Exception:
        corr = None
    return (beta, corr)


def _safe_num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _statement_values(df: pd.DataFrame | None, labels: list[str]) -> list[float]:
    if df is None or getattr(df, "empty", True):
        return []
    for label in labels:
        if label not in df.index:
            continue
        row = df.loc[label]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        if not isinstance(row, pd.Series):
            continue
        vals: list[float] = []
        for raw in row.tolist():
            num = _safe_num(raw)
            if num is not None:
                vals.append(num)
        if vals:
            return vals
    return []


def _latest_statement_value(df: pd.DataFrame | None, labels: list[str]) -> float | None:
    vals = _statement_values(df, labels)
    return vals[0] if vals else None


def _ttm_statement_value(df: pd.DataFrame | None, labels: list[str]) -> float | None:
    vals = _statement_values(df, labels)
    if len(vals) >= 4:
        return float(sum(vals[:4]))
    return vals[0] if vals else None


def fetch_detail(symbol: str) -> dict:
    """Deep-dive payload for a single symbol — chart history, fundamentals,
    analyst recs, benchmarks, news. Designed for the click-through modal."""
    cache_key = f"detail|{symbol}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    out: dict = {"symbol": symbol}
    tk = yf.Ticker(symbol)

    # ---- Price history (one big pull, frontend slices it) ----
    try:
        hist = tk.history(period="max", auto_adjust=True, actions=False)
    except Exception:
        hist = None
    if hist is None or hist.empty:
        # Fall back to 10y if "max" fails (some symbols glitch)
        try:
            hist = tk.history(period="10y", auto_adjust=True, actions=False)
        except Exception:
            hist = None
    if hist is None or hist.empty:
        out["error"] = "no history"
        return out

    close = hist["Close"].dropna()
    vol = hist["Volume"] if "Volume" in hist else pd.Series(dtype=float)
    out["history"] = _series_to_points(close)
    out["volume"] = _series_to_points(vol.dropna()) if not vol.empty else []

    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) >= 2 else last
    out["price"] = last
    out["prev_close"] = prev
    out["change_abs"] = last - prev
    out["pct_1d"] = (last / prev - 1.0) * 100.0 if prev else None

    today = hist.iloc[-1]
    out["day_open"] = _safe_num(today.get("Open"))
    out["day_high"] = _safe_num(today.get("High"))
    out["day_low"] = _safe_num(today.get("Low"))
    out["day_volume"] = _safe_num(today.get("Volume"))

    # Avg volume — trailing 30 trading days
    if "Volume" in hist:
        try:
            out["avg_volume"] = float(hist["Volume"].tail(30).mean())
        except Exception:
            out["avg_volume"] = None

    last_year = close[close.index >= (close.index[-1] - pd.Timedelta(days=365))]
    if not last_year.empty:
        out["w52_high"] = float(last_year.max())
        out["w52_low"] = float(last_year.min())
    out["ath"] = float(close.max())
    out["atl"] = float(close.min())

    # ---- Info / fundamentals ----
    info = _safe_info(tk)
    out["name"] = info.get("longName") or info.get("shortName") or symbol
    out["currency"] = info.get("currency") or "USD"
    out["exchange"] = info.get("exchange") or info.get("fullExchangeName") or ""
    out["sector"] = info.get("sector") or ""
    out["industry"] = info.get("industry") or ""
    out["website"] = info.get("website") or ""
    out["summary"] = (info.get("longBusinessSummary") or "")[:600]
    out["country"] = info.get("country") or ""
    out["employees"] = _safe_num(info.get("fullTimeEmployees"))
    out["quote_type"] = (info.get("quoteType") or "").upper()

    out["market_cap"] = _safe_num(info.get("marketCap") or info.get("market_cap"))
    out["shares"] = _safe_num(info.get("sharesOutstanding"))
    out["float_shares"] = _safe_num(info.get("floatShares"))

    out["pe"] = _safe_num(info.get("trailingPE"))
    out["forward_pe"] = _safe_num(info.get("forwardPE"))
    out["ps"] = _safe_num(info.get("priceToSalesTrailing12Months"))
    out["pb"] = _safe_num(info.get("priceToBook"))
    out["peg"] = _safe_num(info.get("pegRatio") or info.get("trailingPegRatio"))
    out["ev_ebitda"] = _safe_num(info.get("enterpriseToEbitda"))
    out["ev_revenue"] = _safe_num(info.get("enterpriseToRevenue"))

    out["beta"] = _safe_num(info.get("beta"))
    out["dividend_yield"] = _safe_num(info.get("dividendYield"))
    out["dividend_rate"] = _safe_num(info.get("dividendRate"))
    out["payout_ratio"] = _safe_num(info.get("payoutRatio"))
    ex_div = info.get("exDividendDate")
    if ex_div:
        try:
            out["ex_div_date"] = datetime.fromtimestamp(int(ex_div), tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            out["ex_div_date"] = None

    out["profit_margin"] = _safe_num(info.get("profitMargins"))
    out["operating_margin"] = _safe_num(info.get("operatingMargins"))
    out["gross_margin"] = _safe_num(info.get("grossMargins"))
    out["roe"] = _safe_num(info.get("returnOnEquity"))
    out["roa"] = _safe_num(info.get("returnOnAssets"))
    out["debt_equity"] = _safe_num(info.get("debtToEquity"))
    out["current_ratio"] = _safe_num(info.get("currentRatio"))
    out["revenue_growth"] = _safe_num(info.get("revenueGrowth"))
    out["earnings_growth"] = _safe_num(info.get("earningsGrowth"))
    out["total_revenue"] = _safe_num(info.get("totalRevenue"))
    out["free_cashflow"] = _safe_num(info.get("freeCashflow"))

    if out["roe"] is None or out["debt_equity"] is None:
        try:
            balance_sheet = tk.balance_sheet
        except Exception:
            balance_sheet = None
        try:
            quarterly_balance_sheet = tk.quarterly_balance_sheet
        except Exception:
            quarterly_balance_sheet = None
        try:
            income_stmt = tk.income_stmt
        except Exception:
            income_stmt = None
        try:
            quarterly_income_stmt = tk.quarterly_income_stmt
        except Exception:
            quarterly_income_stmt = None

        equity_labels = [
            "Stockholders Equity",
            "Common Stock Equity",
            "Total Stockholder Equity",
            "Total Equity Gross Minority Interest",
        ]
        debt_labels = ["Total Debt"]
        current_debt_labels = ["Current Debt", "Current Debt And Capital Lease Obligation"]
        long_debt_labels = ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"]
        net_income_labels = [
            "Net Income",
            "Diluted NI Availto Com Stockholders",
            "Net Income Common Stockholders",
            "Net Income Continuous Operations",
        ]

        equity = _latest_statement_value(balance_sheet, equity_labels)
        if equity is None:
            equity = _latest_statement_value(quarterly_balance_sheet, equity_labels)

        debt = _latest_statement_value(balance_sheet, debt_labels)
        if debt is None:
            debt = _latest_statement_value(quarterly_balance_sheet, debt_labels)
        if debt is None:
            current_debt = _latest_statement_value(balance_sheet, current_debt_labels)
            long_debt = _latest_statement_value(balance_sheet, long_debt_labels)
            if current_debt is None:
                current_debt = _latest_statement_value(quarterly_balance_sheet, current_debt_labels)
            if long_debt is None:
                long_debt = _latest_statement_value(quarterly_balance_sheet, long_debt_labels)
            if current_debt is not None or long_debt is not None:
                debt = float((current_debt or 0.0) + (long_debt or 0.0))

        net_income = _latest_statement_value(income_stmt, net_income_labels)
        if net_income is None:
            net_income = _ttm_statement_value(quarterly_income_stmt, net_income_labels)

        if out["roe"] is None and equity not in (None, 0) and net_income is not None:
            out["roe"] = float(net_income / equity)
        if out["debt_equity"] is None and equity not in (None, 0) and debt is not None:
            out["debt_equity"] = float((debt / equity) * 100.0)

    out["yf_52w_change"] = _safe_num(info.get("52WeekChange") or info.get("fiftyTwoWeekChange"))
    out["sp_52w_change"] = _safe_num(info.get("SandP52WeekChange"))

    # ---- Analyst recommendations ----
    out["recommendation_key"] = (info.get("recommendationKey") or "").lower()
    out["recommendation_mean"] = _safe_num(info.get("recommendationMean"))
    out["num_analysts"] = _safe_num(info.get("numberOfAnalystOpinions"))
    out["target_mean"] = _safe_num(info.get("targetMeanPrice"))
    out["target_high"] = _safe_num(info.get("targetHighPrice"))
    out["target_low"] = _safe_num(info.get("targetLowPrice"))
    out["target_median"] = _safe_num(info.get("targetMedianPrice"))

    # Recommendation distribution trend (last 4 months: strongBuy/buy/hold/sell/strongSell)
    rec_trend: list[dict] = []
    try:
        rec = tk.recommendations
        if rec is not None and not rec.empty:
            cols = {c.lower(): c for c in rec.columns}
            for _, row in rec.iterrows():
                rec_trend.append(
                    {
                        "period": str(row.get(cols.get("period", "period"), "")),
                        "strongBuy": int(row.get(cols.get("strongbuy", "strongBuy"), 0) or 0),
                        "buy": int(row.get(cols.get("buy", "buy"), 0) or 0),
                        "hold": int(row.get(cols.get("hold", "hold"), 0) or 0),
                        "sell": int(row.get(cols.get("sell", "sell"), 0) or 0),
                        "strongSell": int(row.get(cols.get("strongsell", "strongSell"), 0) or 0),
                    }
                )
    except Exception:
        pass
    out["recommendations_trend"] = rec_trend[:4]

    # ---- Next earnings date ----
    try:
        cal = tk.calendar
        if isinstance(cal, dict):
            edates = cal.get("Earnings Date")
            if isinstance(edates, list) and edates:
                try:
                    out["next_earnings"] = pd.Timestamp(edates[0]).strftime("%Y-%m-%d")
                except Exception:
                    pass
    except Exception:
        pass

    # ---- News ----
    news_out: list[dict] = []
    try:
        news = tk.news or []
        for n in news[:6]:
            content = n.get("content") if isinstance(n, dict) else None
            if isinstance(content, dict):
                title = content.get("title")
                pub = (
                    (content.get("provider") or {}).get("displayName")
                    if isinstance(content.get("provider"), dict)
                    else content.get("publisher")
                )
                link = (
                    (content.get("canonicalUrl") or {}).get("url")
                    if isinstance(content.get("canonicalUrl"), dict)
                    else None
                ) or content.get("link")
                ts = content.get("pubDate") or content.get("displayTime")
            else:
                title = n.get("title")
                pub = n.get("publisher")
                link = n.get("link")
                pt = n.get("providerPublishTime")
                ts = datetime.fromtimestamp(int(pt), tz=timezone.utc).isoformat() if pt else None
            if title:
                news_out.append({"title": title, "publisher": pub or "", "link": link or "", "time": ts or ""})
    except Exception:
        pass
    out["news"] = news_out

    # ---- Benchmark comparisons (SPY + sector ETF) ----
    spy_close = _fetch_bench_history("SPY", "10y")
    sector_etf = _SECTOR_ETF.get(out.get("sector") or "")
    out["sector_etf"] = sector_etf
    sector_close = _fetch_bench_history(sector_etf, "10y") if sector_etf else None

    if spy_close is not None:
        out["benchmark_spy"] = _series_to_points(spy_close)
    if sector_close is not None:
        out["benchmark_sector"] = _series_to_points(sector_close)

    # Stock vs benchmarks: return table
    horizons = {
        "1d": 1,
        "1w": 7,
        "1m": 30,
        "3m": 91,
        "6m": 182,
        "ytd": 0,
        "1y": 365,
        "5y": 1825,
    }
    perf: dict[str, dict] = {}
    for label, days in horizons.items():
        if label == "ytd":
            if not close.empty:
                year_start = pd.Timestamp(year=close.index[-1].year, month=1, day=1, tz=close.index.tz)
                s_w = close[close.index >= year_start]
                s_pct = float((s_w.iloc[-1] / s_w.iloc[0] - 1.0) * 100.0) if len(s_w) >= 2 else None
            else:
                s_pct = None
            if spy_close is not None and not spy_close.empty:
                ys = pd.Timestamp(year=spy_close.index[-1].year, month=1, day=1, tz=spy_close.index.tz)
                b_w = spy_close[spy_close.index >= ys]
                b_pct = float((b_w.iloc[-1] / b_w.iloc[0] - 1.0) * 100.0) if len(b_w) >= 2 else None
            else:
                b_pct = None
            if sector_close is not None and not sector_close.empty:
                ys = pd.Timestamp(year=sector_close.index[-1].year, month=1, day=1, tz=sector_close.index.tz)
                k_w = sector_close[sector_close.index >= ys]
                k_pct = float((k_w.iloc[-1] / k_w.iloc[0] - 1.0) * 100.0) if len(k_w) >= 2 else None
            else:
                k_pct = None
        else:
            s_pct, b_pct = _aligned_pct(close, spy_close if spy_close is not None else close, days)
            _, k_pct = (
                _aligned_pct(close, sector_close if sector_close is not None else close, days)
                if sector_close is not None
                else (None, None)
            )
            if spy_close is None:
                b_pct = None
        perf[label] = {"stock": s_pct, "spy": b_pct, "sector": k_pct}
    out["performance"] = perf

    beta_spy, corr_spy = _beta_corr(close, spy_close)
    out["beta_computed"] = beta_spy
    out["correlation_spy"] = corr_spy
    if sector_close is not None:
        beta_sec, corr_sec = _beta_corr(close, sector_close)
        out["beta_sector"] = beta_sec
        out["correlation_sector"] = corr_sec

    _cache_put(cache_key, out)
    return out


# ----------------------------- Portfolio analytics ------------------------

_PERIOD_YF = {
    "3M": "3mo", "6M": "6mo", "YTD": "ytd",
    "1Y": "1y", "3Y": "3y", "5Y": "5y", "MAX": "max",
}
_PERIOD_DAYS = {
    "3M": 91, "6M": 182, "YTD": None, "1Y": 365,
    "3Y": 365 * 3, "5Y": 365 * 5, "MAX": None,
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
        # Fall back to equal weights.
        n = len(symbols)
        if not n:
            return {}
        return {s: 1.0 / n for s in symbols}
    return {s: v / total for s, v in raw.items()}


def _mcap_bucket(mcap: float | None) -> str:
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
    """Return a DataFrame of Close prices indexed by date, columns = symbols
    (those that returned data)."""
    if not symbols:
        return pd.DataFrame()
    period_yf = _PERIOD_YF.get(period.upper(), "1y")
    try:
        df = yf.download(
            tickers=symbols,
            period=period_yf,
            interval="1d",
            auto_adjust=True,
            group_by="ticker",
            threads=True,
            progress=False,
        )
    except Exception:
        df = None
    if df is None or df.empty:
        return pd.DataFrame()
    out: dict[str, pd.Series] = {}
    if isinstance(df.columns, pd.MultiIndex):
        for s in symbols:
            try:
                col = df[s]["Close"].dropna()
            except (KeyError, ValueError):
                continue
            if not col.empty:
                out[s] = col
    else:
        # Single-symbol fallback.
        try:
            col = df["Close"].dropna()
            if not col.empty:
                out[symbols[0]] = col
        except (KeyError, ValueError):
            pass
    if not out:
        return pd.DataFrame()
    return pd.concat(out, axis=1).sort_index()


def _analyst_for(symbol: str) -> dict:
    """Lightweight per-symbol analyst block. Cached via _safe_info's session
    + our own analytics cache."""
    try:
        info = _safe_info(yf.Ticker(symbol))
    except Exception:
        return {}
    return {
        "mean_rating": _safe_num(info.get("recommendationMean")),
        "n_analysts": _safe_num(info.get("numberOfAnalystOpinions")),
        "target_mean": _safe_num(info.get("targetMeanPrice")),
        "target_low": _safe_num(info.get("targetLowPrice")),
        "target_high": _safe_num(info.get("targetHighPrice")),
        "div_yield": _safe_num(info.get("dividendYield")),
        "ev_ebitda": _safe_num(info.get("enterpriseToEbitda")),
        "price": _safe_num(info.get("currentPrice") or info.get("regularMarketPrice")),
    }


def analyze_portfolio(rows: list[dict], weights_in: dict, period: str) -> dict:
    out = analyze_portfolios_multi(rows, {"__single__": weights_in or {}}, period)
    if isinstance(out, dict) and "error" in out:
        return out
    return (out or {}).get("__single__", {"error": "no result"})


def analyze_portfolios_multi(
    rows: list[dict],
    weight_sets: dict[str, dict],
    period: str,
) -> dict:
    """Run analytics for multiple weight vectors against the same row set + period.

    Shares the (slow) bulk price download, sector-ETF download, and per-symbol
    analyst-info fetch across every weight set so swapping modes feels instant.
    Returns ``{set_name: analytics_dict | {"error": ...}}``. If the shared
    data cannot be produced, returns a flat ``{"error": ...}`` instead.
    """
    period_u = (period or "1Y").upper()
    if period_u not in _PERIOD_YF:
        period_u = "1Y"

    rows = [r for r in (rows or []) if r and r.get("symbol")]
    symbols = [str(r["symbol"]) for r in rows]
    if not symbols:
        return {"error": "no symbols"}

    if not isinstance(weight_sets, dict) or not weight_sets:
        weight_sets = {"__single__": {}}

    by_sym = {r["symbol"]: r for r in rows}

    # Normalize every requested weight set up-front (over the original symbol list).
    normalized_sets: dict[str, dict[str, float]] = {
        name: _normalize_weights(w_in or {}, symbols) for name, w_in in weight_sets.items()
    }

    # If every requested set is fully cached, return cached results immediately.
    per_set_cache_keys: dict[str, str] = {}
    cached_results: dict[str, dict] = {}
    for name, weights in normalized_sets.items():
        key = "pf|" + "|".join(f"{s}:{weights[s]:.6f}" for s in sorted(symbols)) + f"|{period_u}"
        per_set_cache_keys[name] = key
        hit = _cache_get(key)
        if hit is not None:
            cached_results[name] = hit
    if len(cached_results) == len(normalized_sets):
        return cached_results

    closes = _bulk_close(symbols + ["SPY"], period_u)
    warnings: list[str] = []
    missing = [s for s in symbols if s not in closes.columns]
    if missing:
        warnings.append(f"No price history for: {', '.join(missing)}")
    spy = closes["SPY"].dropna() if "SPY" in closes.columns else None
    sym_closes = closes[[s for s in symbols if s in closes.columns]].dropna(how="all")
    if sym_closes.empty:
        return {"error": "no price history for portfolio", "warnings": warnings}

    active = [s for s in symbols if s in sym_closes.columns]
    if not active:
        return {"error": "no price history for portfolio", "warnings": warnings}

    sym_closes = sym_closes.ffill().dropna(how="any")
    if sym_closes.empty or len(sym_closes) < 3:
        return {"error": "insufficient overlapping history", "warnings": warnings}

    daily_ret = sym_closes.pct_change().dropna(how="all").fillna(0.0)
    common_index = sym_closes.index

    spy_aligned_raw = None
    spy_ret_full = None
    if spy is not None and not spy.empty:
        spy_aligned_raw = spy.reindex(common_index).ffill().dropna()
        if len(spy_aligned_raw) >= 2:
            spy_ret_full = spy_aligned_raw.pct_change().dropna()
        else:
            spy_aligned_raw = None

    # Pre-fetch sector ETFs for every sector present in the row set (regardless
    # of weight), so different weight modes share the same download.
    all_sectors = {(by_sym.get(s, {}).get("sector") or "").strip() for s in active}
    all_sectors.discard("")
    sec_etfs = list({_SECTOR_ETF.get(sec) for sec in all_sectors if _SECTOR_ETF.get(sec)})
    sec_ret_df = None
    if sec_etfs:
        sec_closes_df = _bulk_close(sec_etfs, period_u)
        if not sec_closes_df.empty:
            sec_closes_df = sec_closes_df.reindex(common_index).ffill().dropna(how="any")
            if not sec_closes_df.empty:
                sec_ret_df = sec_closes_df.pct_change().fillna(0.0)

    # Per-symbol analyst blocks (shared across weight sets).
    analyst_blocks: dict[str, dict] = {}
    if active:
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(active)))) as pool:
            futs = {pool.submit(_analyst_for, s): s for s in active}
            for fut in as_completed(futs):
                try:
                    analyst_blocks[futs[fut]] = fut.result()
                except Exception:
                    analyst_blocks[futs[fut]] = {}

    # Per-symbol fundamentals — gathered once.
    pe_vals = {s: _safe_num(by_sym.get(s, {}).get("pe_ratio")) for s in active}
    ps_vals = {s: _safe_num(by_sym.get(s, {}).get("ps_ratio")) for s in active}
    ev_vals = {s: analyst_blocks.get(s, {}).get("ev_ebitda") for s in active}
    div_vals = {s: analyst_blocks.get(s, {}).get("div_yield") for s in active}
    mcap_vals = {s: _safe_num(by_sym.get(s, {}).get("market_cap")) for s in active}

    period_returns: dict[str, float] = {}
    for s in active:
        col = sym_closes[s]
        period_returns[s] = float(col.iloc[-1] / col.iloc[0] - 1.0) * 100.0 if len(col) >= 2 else 0.0

    def _stats(ret: pd.Series, val: pd.Series) -> dict:
        if ret.empty or val.empty:
            return {}
        n_days = (val.index[-1] - val.index[0]).days or 1
        years = max(n_days / 365.25, 1e-6)
        total_return = float(val.iloc[-1] / val.iloc[0] - 1.0) * 100.0
        ann_return = float((val.iloc[-1] / val.iloc[0]) ** (1.0 / years) - 1.0) * 100.0
        ann_vol = float(ret.std() * math.sqrt(252)) * 100.0
        sharpe = float((ret.mean() * 252) / (ret.std() * math.sqrt(252))) if ret.std() else None
        downside = ret[ret < 0].std()
        sortino = float((ret.mean() * 252) / (downside * math.sqrt(252))) if downside and downside > 0 else None
        run_mx = val.cummax()
        max_dd = float((val / run_mx - 1.0).min() * 100.0)
        calmar = (ann_return / abs(max_dd)) if max_dd < 0 else None
        return {
            "total_return": total_return,
            "ann_return": ann_return,
            "ann_vol": ann_vol,
            "sharpe": sharpe,
            "sortino": sortino,
            "max_dd": max_dd,
            "calmar": calmar,
        }

    spy_stats_shared = {}
    spy_aligned_rebased = None
    if spy_aligned_raw is not None:
        spy_aligned_rebased = 100.0 * spy_aligned_raw / spy_aligned_raw.iloc[0]
        spy_stats_shared = _stats(spy_ret_full, spy_aligned_rebased)
    spy_points_shared = _series_to_points(spy_aligned_rebased) if spy_aligned_rebased is not None else []
    spy_var_full = float(spy_ret_full.var()) if (spy_ret_full is not None and len(spy_ret_full) > 30) else None

    daily_ret_active = daily_ret[active]

    results: dict[str, dict] = dict(cached_results)
    for name, weights in normalized_sets.items():
        if name in results:
            continue

        # Renormalize over active symbols.
        if len(active) != len(symbols):
            weights = _normalize_weights({s: weights.get(s, 0.0) for s in active}, active)

        w_vec = pd.Series([weights[s] for s in active], index=active)
        port_ret = (daily_ret_active * w_vec).sum(axis=1)
        port_val = (1.0 + port_ret).cumprod()
        port_val = 100.0 * port_val / port_val.iloc[0] if not port_val.empty else port_val
        drawdown = (port_val / port_val.cummax() - 1.0) * 100.0

        # Sector-mix blend.
        sec_blend = None
        if sec_ret_df is not None:
            sector_alloc: dict[str, float] = {}
            for s in active:
                sec = (by_sym.get(s, {}).get("sector") or "").strip()
                if sec:
                    sector_alloc[sec] = sector_alloc.get(sec, 0.0) + weights[s]
            if sector_alloc:
                pairs = [(sec, _SECTOR_ETF.get(sec), w) for sec, w in sector_alloc.items() if _SECTOR_ETF.get(sec)]
                sec_w_total = sum(w for _, _, w in pairs)
                parts = [sec_ret_df[etf] * (w / sec_w_total)
                         for _, etf, w in pairs
                         if sec_w_total > 0 and etf in sec_ret_df.columns]
                if parts:
                    sec_blend_ret = sum(parts)
                    sec_blend = (1.0 + sec_blend_ret).cumprod()
                    sec_blend = 100.0 * sec_blend / sec_blend.iloc[0]

        pf_stats = _stats(port_ret, port_val)

        beta_spy = r2_spy = te_spy = None
        if spy_ret_full is not None and spy_var_full and len(spy_ret_full) > 30:
            common = port_ret.index.intersection(spy_ret_full.index)
            if len(common) >= 30:
                rp, rb = port_ret.loc[common], spy_ret_full.loc[common]
                var_b = float(rb.var())
                cov = float(rp.cov(rb))
                beta_spy = cov / var_b if var_b else None
                corr = rp.corr(rb)
                r2_spy = float(corr * corr) if pd.notna(corr) else None
                te = (rp - rb).std()
                te_spy = float(te * math.sqrt(252) * 100.0) if te and pd.notna(te) else None

        def _w_avg(values, _w=weights):
            num = 0.0
            denom = 0.0
            for s, v in values.items():
                if v is None or not math.isfinite(float(v)):
                    continue
                num += float(v) * _w.get(s, 0.0)
                denom += _w.get(s, 0.0)
            return num / denom if denom > 0 else None

        weighted = {
            "pe": _w_avg(pe_vals),
            "ps": _w_avg(ps_vals),
            "ev_ebitda": _w_avg(ev_vals),
            "div_yield": _w_avg(div_vals),
            "market_cap": _w_avg(mcap_vals),
        }

        rating_num = rating_w = upside_num = upside_w = 0.0
        n_analysts_total = 0
        for s in active:
            blk = analyst_blocks.get(s, {})
            w = weights.get(s, 0.0)
            mr = blk.get("mean_rating")
            if mr is not None and math.isfinite(float(mr)):
                rating_num += float(mr) * w
                rating_w += w
            tgt = blk.get("target_mean")
            px = _safe_num(by_sym.get(s, {}).get("price")) or blk.get("price")
            if tgt and px and px > 0:
                upside = (float(tgt) / float(px) - 1.0) * 100.0
                upside_num += upside * w
                upside_w += w
            na = blk.get("n_analysts")
            if na is not None and math.isfinite(float(na)):
                n_analysts_total += int(na)
        analyst = {
            "mean_rating": (rating_num / rating_w) if rating_w > 0 else None,
            "rating_coverage_weight": rating_w,
            "weighted_target_upside_pct": (upside_num / upside_w) if upside_w > 0 else None,
            "target_coverage_weight": upside_w,
            "n_analysts_total": n_analysts_total,
        }

        by_sector: dict[str, float] = {}
        by_industry: dict[str, float] = {}
        by_bucket: dict[str, float] = {}
        by_country: dict[str, float] = {}
        for s in active:
            r = by_sym.get(s, {})
            w = weights.get(s, 0.0)
            sec = (r.get("sector") or "Unknown").strip() or "Unknown"
            ind = (r.get("industry") or "Unknown").strip() or "Unknown"
            country = (r.get("country") or "Unknown").strip() or "Unknown"
            bucket = _mcap_bucket(_safe_num(r.get("market_cap")))
            by_sector[sec] = by_sector.get(sec, 0.0) + w
            by_industry[ind] = by_industry.get(ind, 0.0) + w
            by_country[country] = by_country.get(country, 0.0) + w
            by_bucket[bucket] = by_bucket.get(bucket, 0.0) + w

        weights_sorted = sorted(weights.values(), reverse=True)
        top5 = float(sum(weights_sorted[:5])) if weights_sorted else 0.0
        herfindahl = float(sum(w * w for w in weights.values()))
        effective_n = (1.0 / herfindahl) if herfindahl > 0 else 0.0

        contribution = []
        for s in active:
            w = weights.get(s, 0.0)
            pr = period_returns.get(s, 0.0)
            contribution.append({
                "symbol": s,
                "name": by_sym.get(s, {}).get("name") or s,
                "weight": w,
                "period_return": pr,
                "contribution": w * pr,
                "sector": by_sym.get(s, {}).get("sector") or "",
            })
        contribution.sort(key=lambda x: x["contribution"], reverse=True)

        out_one = {
            "period": period_u,
            "weights_applied": weights,
            "active_symbols": active,
            "missing_symbols": missing,
            "series": {
                "portfolio": _series_to_points(port_val),
                "spy": spy_points_shared,
                "sector_mix": _series_to_points(sec_blend) if sec_blend is not None else [],
                "drawdown": _series_to_points(drawdown),
            },
            "stats": {**pf_stats, "beta_spy": beta_spy, "r2_spy": r2_spy, "te_spy": te_spy},
            "spy_stats": spy_stats_shared,
            "weighted": weighted,
            "analyst": analyst,
            "exposure": {
                "by_sector": by_sector,
                "by_industry": by_industry,
                "by_bucket": by_bucket,
                "by_country": by_country,
            },
            "concentration": {"top5": top5, "herfindahl": herfindahl, "effective_n": effective_n},
            "contribution": contribution,
            "warnings": warnings,
        }
        _cache_put(per_set_cache_keys[name], out_one)
        results[name] = out_one

    return results


# ----------------------------- HTML payload --------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<title>Portfolio Tracker</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css" crossorigin="anonymous">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js" crossorigin="anonymous"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js" crossorigin="anonymous"></script>
<style>
  :root, [data-theme="light"] {
    --bg: #ffffff;
    --bg-subtle: #f6f8fa;
    --bg-canvas: #ffffff;
    --border: #d0d7de;
    --text: #1f2328;
    --muted: #59636e;
    --accent: #0969da;
    --accent-soft: #ddf4ff;
    --pos: #1f883d;
    --pos-rgb: 31, 136, 61;
    --neg: #cf222e;
    --neg-rgb: 207, 34, 46;
    --warn: #9a6700;
    --warn-rgb: 191, 135, 0;
    --bg-rgb: 255, 255, 255;
    --row-alt: #f6f8fa;
    --header-bg: #f6f8fa;
    --hover: #fff8c5;
  }
  [data-theme="dark"] {
    --bg: #0d1117;
    --bg-subtle: #161b22;
    --bg-canvas: #0d1117;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #7d8590;
    --accent: #2f81f7;
    --accent-soft: #163b66;
    --pos: #3fb950;
    --pos-rgb: 63, 185, 80;
    --neg: #f85149;
    --neg-rgb: 248, 81, 73;
    --warn: #d29922;
    --warn-rgb: 210, 153, 34;
    --bg-rgb: 13, 17, 23;
    --row-alt: #161b22;
    --header-bg: #161b22;
    --hover: #1f2937;
  }
  * { box-sizing: border-box; }
  html, body {
    background: var(--bg); color: var(--text); margin: 0;
    font-family: -apple-system, "SF Pro Text", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
  }

  /* Top control bar */
  .topbar {
    position: sticky; top: 0; z-index: 30;
    background: var(--bg);
    padding: 10px 16px;
    display: flex; gap: 8px; align-items: center;
    border-bottom: 1px solid transparent;
  }
  .topbar.scrolled { border-bottom-color: var(--border); }
  .topbar button, .topbar select {
    height: 30px; padding: 0 12px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--bg-canvas); cursor: pointer; font-size: 12.5px; color: var(--text);
    transition: background 0.12s, border-color 0.12s;
  }
  .topbar button:hover { background: var(--bg-subtle); border-color: var(--accent); }
  .topbar button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .topbar button.primary:hover { filter: brightness(1.08); background: var(--accent); }
  .topbar button:disabled { opacity: 0.55; cursor: progress; }
  .topbar .spacer { flex: 1; }
  .topbar .status { color: var(--muted); font-size: 12px; }
  /* Primary Portfolio button — accent fill, distinct call to action */
  .topbar button.portfolio-btn {
    background: var(--accent); color: #fff; border-color: var(--accent);
    font-weight: 600; padding: 0 14px; gap: 6px;
    display: inline-flex; align-items: center;
  }
  .topbar button.portfolio-btn:hover { filter: brightness(1.08); background: var(--accent); border-color: var(--accent); }
  .topbar button.portfolio-btn.active { box-shadow: 0 0 0 2px rgba(47, 129, 247, 0.30); }
  .topbar button.portfolio-btn .pf-icon { font-size: 13px; line-height: 1; }
  /* Danger style for delete buttons */
  .topbar button.danger, button.danger {
    color: var(--neg); border-color: rgba(248, 81, 73, 0.4);
  }
  button.danger:hover { background: rgba(248, 81, 73, 0.12); border-color: var(--neg); }
  button.danger:disabled { opacity: 0.4; cursor: not-allowed; }

  /* Theme toggle pill */
  .theme-switch {
    display: inline-flex; align-items: center; gap: 6px;
    cursor: pointer; user-select: none; border: none; background: none; padding: 0;
  }
  .ts-icon {
    font-size: 12px; line-height: 1; color: var(--muted);
    font-style: normal;
    /* Force text rendering, not emoji color */
    font-family: -apple-system, "Segoe UI Symbol", sans-serif;
  }
  .ts-track {
    width: 30px; height: 17px; background: var(--border);
    border-radius: 99px; position: relative;
    transition: background 0.2s;
    flex-shrink: 0;
  }
  .ts-track.on { background: var(--accent); }
  .ts-thumb {
    position: absolute; top: 2.5px; left: 2.5px;
    width: 12px; height: 12px; border-radius: 50%;
    background: var(--bg-canvas); transition: transform 0.2s;
    box-shadow: 0 1px 3px rgba(0,0,0,0.18);
  }
  .ts-track.on .ts-thumb { transform: translateX(13px); }

  /* Info icon button */
  .info-btn {
    width: 30px; height: 30px; padding: 0; display: inline-flex;
    align-items: center; justify-content: center;
    font-size: 15px; color: var(--muted); font-style: normal;
  }
  .info-btn:hover { color: var(--accent); border-color: var(--accent); }
  .info-icon-circle {
    display: inline-flex; width: 16px; height: 16px;
    border: 1.5px solid currentColor; border-radius: 50%;
    align-items: center; justify-content: center;
    font-size: 10.5px; font-weight: 700; font-style: normal;
    font-family: Georgia, "Times New Roman", serif; line-height: 1;
    letter-spacing: 0; flex-shrink: 0;
  }

  /* Info modal */
  .info-bg {
    position: fixed; inset: 0; background: rgba(0,0,0,0.50);
    display: none; align-items: center; justify-content: center; z-index: 60;
    padding: 20px;
  }
  .info-bg.show { display: flex; }
  .info-modal {
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 14px;
    width: min(680px, 96vw); max-height: 88vh; overflow-y: auto; color: var(--text);
  }
  .info-head {
    display: flex; justify-content: space-between; align-items: flex-start;
    padding: 20px 22px 16px; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; background: var(--bg-canvas); z-index: 1; border-radius: 14px 14px 0 0;
  }
  .info-head-title { font-size: 17px; font-weight: 700; }
  .info-head-sub { font-size: 12px; color: var(--muted); margin-top: 3px; }
  .info-head-close {
    width: 28px; height: 28px; border-radius: 6px; border: 1px solid var(--border);
    background: transparent; cursor: pointer; font-size: 18px; color: var(--muted);
    display: flex; align-items: center; justify-content: center; line-height: 1;
    flex-shrink: 0;
  }
  .info-head-close:hover { background: var(--bg-subtle); color: var(--text); }
  .info-intro {
    padding: 14px 22px 6px; margin: 0;
    color: var(--muted); font-size: 12.5px; line-height: 1.6;
  }
  .info-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px; padding: 12px 22px 22px;
  }
  @media (max-width: 500px) { .info-grid { grid-template-columns: 1fr; } }
  .info-card {
    background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 8px;
    padding: 11px 13px;
  }
  .info-card-name {
    font-weight: 700; font-size: 12.5px; color: var(--text); margin-bottom: 5px;
  }
  .info-card-desc {
    font-size: 12px; color: var(--muted); line-height: 1.55;
  }
  .info-formula {
    margin-top: 7px; padding: 6px 10px;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 5px;
    color: var(--text); overflow-x: auto;
  }
  /* KaTeX display-math: remove default large vertical margin, keep compact */
  .info-formula .katex-display { margin: 2px 0 0 !important; overflow-x: auto; overflow-y: hidden; }
  .info-formula .katex { font-size: 0.96em; }
  /* Fallback plain-text formulas (when KaTeX not loaded) */
  .info-formula.plain {
    font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px;
    line-height: 1.5; white-space: pre-wrap;
  }
  .formula-note {
    display: block; margin-top: 3px; font-size: 11px;
    color: var(--muted); font-family: -apple-system, "Segoe UI", sans-serif;
  }

  /* Progress bar */
  .progress-wrap {
    height: 3px; background: transparent; overflow: hidden;
    border: none; position: sticky; top: 50px; z-index: 25;
    transition: opacity 0.2s; opacity: 0;
  }
  .progress-wrap.show { opacity: 1; }
  .progress-bar {
    height: 100%; background: var(--accent);
    width: 0%; transition: width 0.25s ease-out;
    border-radius: 0 2px 2px 0;
  }

  /* Input panel */
  #input-panel {
    background: var(--bg-subtle);
    border-radius: 0 0 12px 12px;
    margin: 0 16px;
    padding: 12px 14px 14px;
    border: 1px solid var(--border);
    border-top: none;
  }
  #input-panel.hidden { display: none; }
  #input-panel textarea {
    width: 100%; min-height: 56px; height: 56px; resize: vertical;
    font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 13px;
    line-height: 1.5;
    border: 1px solid var(--border); border-radius: 8px; padding: 8px 11px;
    background: var(--bg-canvas); color: var(--text);
  }
  #input-panel textarea:focus { outline: none; border-color: var(--accent); }
  .panel-row { display: flex; gap: 8px; align-items: center; margin-top: 10px; flex-wrap: wrap; }
  .panel-row .status { color: var(--muted); font-size: 12px; }
  .panel-row .spacer { flex: 1; }
  .panel-row button {
    height: 34px; padding: 0 16px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--bg-canvas); color: var(--text);
    font-size: 13px; font-weight: 500; cursor: pointer;
    display: inline-flex; align-items: center; gap: 6px;
    transition: background 0.12s, border-color 0.12s, box-shadow 0.12s, transform 0.06s;
  }
  .panel-row button:hover { background: var(--bg-subtle); border-color: var(--accent); }
  .panel-row button:active { transform: translateY(1px); }
  .panel-row button:disabled { opacity: 0.55; cursor: not-allowed; }
  .panel-row button.primary {
    background: var(--accent); color: #fff; border-color: var(--accent);
    box-shadow: 0 1px 2px rgba(9, 105, 218, 0.25);
    font-weight: 600;
  }
  .panel-row button.primary:hover { filter: brightness(1.08); background: var(--accent); border-color: var(--accent); }
  .panel-row button.ghost {
    background: transparent; border-style: dashed; color: var(--muted);
  }
  .panel-row button.ghost:hover { color: var(--accent); border-style: solid; }

  /* Confirmation modal (delete portfolio) */
  .confirm-bg {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    display: none; align-items: center; justify-content: center; z-index: 80;
    padding: 20px;
  }
  .confirm-bg.show { display: flex; }
  .confirm-modal {
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 12px;
    width: min(420px, 96vw);
    box-shadow: 0 20px 50px rgba(0,0,0,0.3);
    overflow: hidden;
  }
  .confirm-head {
    padding: 16px 18px 8px;
    font-weight: 700; font-size: 15px; color: var(--text);
  }
  .confirm-body {
    padding: 0 18px 16px; color: var(--muted); font-size: 13px; line-height: 1.5;
  }
  .confirm-body b { color: var(--text); }
  .confirm-foot {
    display: flex; justify-content: flex-end; gap: 8px;
    padding: 12px 16px; border-top: 1px solid var(--border);
    background: var(--bg-subtle);
  }
  .confirm-foot button {
    height: 32px; padding: 0 14px; border-radius: 7px;
    border: 1px solid var(--border); background: var(--bg-canvas); color: var(--text);
    font-size: 12.5px; font-weight: 500; cursor: pointer;
  }
  .confirm-foot button:hover { background: var(--bg-subtle); }
  .confirm-foot button.danger {
    background: var(--neg); color: #fff; border-color: var(--neg);
  }
  .confirm-foot button.danger:hover { filter: brightness(1.08); }

  /* Portfolio tab strip — fully rounded pill tabs */
  .pf-tabs {
    display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
    padding: 0 0 12px; margin-bottom: 4px;
  }
  .pf-tab {
    display: inline-flex; align-items: center; gap: 8px;
    background: var(--bg-canvas); border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 12px; font-size: 13px; font-weight: 500;
    border-radius: 999px;
    cursor: pointer;
    max-width: 240px; min-height: 30px;
    position: relative;
    transition: background 0.12s, border-color 0.12s, box-shadow 0.12s;
  }
  .pf-tab:hover { background: var(--bg-subtle); border-color: var(--accent); }
  .pf-tab.active {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
    box-shadow: 0 1px 3px rgba(9, 105, 218, 0.18);
  }
  [data-theme="dark"] .pf-tab.active { color: #fff; background: rgba(47, 129, 247, 0.22); }
  .pf-tab .pf-tab-label {
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 180px;
  }
  .pf-tab .pf-tab-stale {
    display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: var(--warn); flex-shrink: 0;
  }
  .pf-tab .pf-tab-close {
    width: 18px; height: 18px; border-radius: 50%;
    color: var(--muted); font-size: 12px; line-height: 1;
    display: inline-flex; align-items: center; justify-content: center;
    cursor: pointer; flex-shrink: 0;
    transition: background 0.12s, color 0.12s;
  }
  .pf-tab .pf-tab-close:hover { background: rgba(207, 34, 46, 0.16); color: var(--neg); }
  .pf-tab-newadhoc {
    background: transparent !important; border-style: dashed !important;
  }
  .pf-tab-add {
    background: transparent; border: 1px dashed var(--border); color: var(--muted);
    padding: 6px 12px; font-size: 13px; border-radius: 999px;
    cursor: pointer; min-height: 30px;
    transition: color 0.12s, border-color 0.12s;
  }
  .pf-tab-add:hover { color: var(--accent); border-color: var(--accent); }

  /* Editor header */
  .pf-editor { padding: 0 0 4px; }
  .pf-editor-head {
    display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px;
  }
  .pf-editor-head #pf-editor-title { font-weight: 600; font-size: 13px; color: var(--text); }
  .pf-editor-meta { color: var(--muted); font-size: 11.5px; }
  .pf-editor-meta .stale { color: var(--warn); font-weight: 600; }

  /* Analytics sub-window */
  .pf-analytics {
    margin-top: 14px;
    background: var(--bg-canvas);
    border: 1px solid var(--border);
    border-radius: 10px;
    box-shadow: 0 1px 0 rgba(0,0,0,0.04), 0 6px 14px rgba(0,0,0,0.06);
    overflow: hidden;
  }
  .pf-analytics-head {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 9px 12px;
    background: linear-gradient(180deg, var(--bg-subtle), var(--bg-canvas));
    border-bottom: 1px solid var(--border);
  }
  .pf-analytics-title { font-weight: 700; font-size: 12.5px; margin-right: 6px; }
  .pf-mode-toggle, .pf-period-tabs {
    display: inline-flex; background: var(--bg-canvas); border: 1px solid var(--border);
    border-radius: 7px; overflow: hidden;
  }
  .pf-mode-toggle button, .pf-period-tabs button {
    background: transparent; border: none; padding: 5px 10px; font-size: 11.5px;
    color: var(--muted); cursor: pointer; border-right: 1px solid var(--border);
  }
  .pf-mode-toggle button:last-child, .pf-period-tabs button:last-child { border-right: none; }
  .pf-mode-toggle button:hover, .pf-period-tabs button:hover { color: var(--text); background: var(--bg-subtle); }
  .pf-mode-toggle button.active, .pf-period-tabs button.active {
    background: var(--accent); color: #fff;
  }
  .pf-overlay-toggles {
    display: inline-flex; gap: 10px; margin-left: auto; flex-wrap: wrap;
    font-size: 11.5px; color: var(--muted);
  }
  .pf-overlay-toggles label { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; }
  .pf-overlay-toggles input[type="checkbox"] { accent-color: var(--accent); }

  .pf-analytics-body { padding: 12px; }
  .pf-empty {
    color: var(--muted); font-size: 12.5px; text-align: center; padding: 32px 8px;
  }
  .pf-loading { display: inline-block; color: var(--muted); font-size: 11.5px; margin-left: 6px; }

  .pf-grid {
    display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr);
    gap: 12px; align-items: stretch;
  }
  @media (max-width: 900px) { .pf-grid { grid-template-columns: 1fr; } }

  .pf-card {
    background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 12px;
  }
  .pf-card h4 {
    margin: 0 0 8px; font-size: 12px; font-weight: 700;
    color: var(--text); display: flex; align-items: baseline; gap: 8px;
    text-transform: uppercase; letter-spacing: 0.4px;
  }
  .pf-card h4 .sub { font-size: 11px; color: var(--muted); font-weight: 500; text-transform: none; letter-spacing: 0; }

  .pf-chart-wrap { position: relative; }
  .pf-chart-wrap svg { display: block; width: 100%; height: 220px; }
  .pf-chart-wrap .pf-dd-svg { height: 80px; margin-top: 4px; }
  .pf-chart-legend { display: flex; gap: 12px; font-size: 11px; color: var(--muted); margin-top: 4px; flex-wrap: wrap; }
  .pf-chart-legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; vertical-align: middle; margin-right: 4px; }

  .pf-stats { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 6px 12px; font-size: 12px; }
  @media (max-width: 600px) { .pf-stats { grid-template-columns: 1fr; } }
  .pf-stat-row { display: flex; justify-content: space-between; padding: 3px 0; border-bottom: 1px dashed var(--border); }
  .pf-stat-row:last-child { border-bottom: none; }
  .pf-stat-row .l { color: var(--muted); }
  .pf-stat-row .v { font-variant-numeric: tabular-nums; font-weight: 600; }
  .pf-stat-row .v.pos { color: var(--pos); }
  .pf-stat-row .v.neg { color: var(--neg); }

  .pf-bars { display: flex; flex-direction: column; gap: 4px; font-size: 11.5px; }
  .pf-bars .pf-bar { display: grid; grid-template-columns: minmax(70px, 1.2fr) 4fr minmax(46px, auto); gap: 6px; align-items: center; }
  .pf-bars .pf-bar-name { color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .pf-bars .pf-bar-track { background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 4px; height: 9px; overflow: hidden; }
  .pf-bars .pf-bar-fill { height: 100%; background: var(--accent); }
  .pf-bars .pf-bar-val { color: var(--muted); font-variant-numeric: tabular-nums; text-align: right; }

  .pf-contrib-table { width: 100%; border-collapse: collapse; font-size: 11.5px; font-variant-numeric: tabular-nums; }
  .pf-contrib-table th { text-align: right; padding: 4px 6px; color: var(--muted); border-bottom: 1px solid var(--border); font-weight: 600; }
  .pf-contrib-table th:first-child, .pf-contrib-table td:first-child { text-align: left; }
  .pf-contrib-table td { padding: 3px 6px; border-bottom: 1px dashed var(--border); }
  .pf-contrib-table tr:last-child td { border-bottom: none; }
  .pf-contrib-table td.pos { color: var(--pos); }
  .pf-contrib-table td.neg { color: var(--neg); }

  /* Custom weight popup */
  .pf-weights-bg {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    display: none; align-items: center; justify-content: center; z-index: 70;
    padding: 20px;
  }
  .pf-weights-bg.show { display: flex; }
  .pf-weights-modal {
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 12px;
    width: min(560px, 96vw); max-height: 86vh; display: flex; flex-direction: column;
  }
  .pf-weights-head {
    display: flex; align-items: center; gap: 8px;
    padding: 12px 14px; border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .pf-weights-title { font-weight: 700; font-size: 13.5px; flex: 1; }
  .pf-weights-sum { font-variant-numeric: tabular-nums; font-weight: 700; font-size: 12.5px; }
  .pf-weights-sum.bad { color: var(--neg); }
  .pf-weights-sum.good { color: var(--pos); }
  .pf-weights-action {
    height: 26px; padding: 0 10px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg-subtle); cursor: pointer; font-size: 11.5px; color: var(--text);
  }
  .pf-weights-action:hover { border-color: var(--accent); }
  .pf-weights-hint { padding: 8px 14px; color: var(--muted); font-size: 11.5px; }
  .pf-weights-body { padding: 4px 14px 12px; overflow-y: auto; flex: 1; }
  .pf-w-row {
    display: grid; grid-template-columns: 64px 1fr 64px 28px; gap: 8px;
    align-items: center; padding: 4px 0; border-bottom: 1px dashed var(--border);
  }
  .pf-w-row:last-child { border-bottom: none; }
  .pf-w-row.locked .pf-w-slider { opacity: 0.55; }
  .pf-w-sym { font-weight: 600; font-size: 12px; }
  .pf-w-slider { width: 100%; accent-color: var(--accent); }
  .pf-w-num {
    width: 100%; padding: 3px 6px; border: 1px solid var(--border);
    border-radius: 5px; background: var(--bg-canvas); color: var(--text);
    font-size: 11.5px; font-variant-numeric: tabular-nums; text-align: right;
  }
  .pf-w-num:focus { outline: none; border-color: var(--accent); }
  .pf-w-lock {
    width: 28px; height: 24px; border-radius: 5px; border: 1px solid var(--border);
    background: var(--bg-subtle); cursor: pointer; color: var(--muted); padding: 0;
    font-size: 12px;
  }
  .pf-w-lock.on { background: var(--accent); color: #fff; border-color: var(--accent); }
  .pf-weights-foot {
    display: flex; justify-content: flex-end; gap: 8px;
    padding: 10px 14px; border-top: 1px solid var(--border);
  }
  .pf-weights-foot button {
    height: 30px; padding: 0 14px; border-radius: 7px; border: 1px solid var(--border);
    background: var(--bg-subtle); cursor: pointer; color: var(--text); font-size: 12.5px;
  }
  .pf-weights-foot button.primary {
    background: var(--accent); color: #fff; border-color: var(--accent);
  }
  .pf-weights-foot button.primary:hover { filter: brightness(1.08); }

  /* Stale view banner inside the analytics body */
  .pf-stale-banner {
    background: rgba(210, 153, 34, 0.12); border: 1px solid rgba(210, 153, 34, 0.4);
    color: var(--warn); border-radius: 7px; padding: 7px 10px; font-size: 11.5px;
    margin-bottom: 10px; display: flex; align-items: center; gap: 8px;
  }
  .pf-stale-banner button {
    margin-left: auto; height: 24px; padding: 0 10px; border-radius: 6px;
    border: 1px solid var(--warn); background: transparent; color: var(--warn); cursor: pointer;
    font-size: 11.5px;
  }
  .pf-stale-banner button:hover { background: rgba(210, 153, 34, 0.18); }

  /* Sort menu */
  .sort-menu {
    position: relative; display: inline-block;
  }
  .sort-menu .dropdown {
    position: absolute; top: 34px; left: 0; z-index: 40;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 6px 18px rgba(0,0,0,0.12);
    min-width: 180px; padding: 4px 0; display: none;
  }
  .sort-menu.open .dropdown { display: block; }
  .sort-menu .dropdown button {
    display: block; width: 100%; text-align: left; border: none; border-radius: 0;
    background: transparent; padding: 6px 12px; font-size: 12.5px; color: var(--text);
    cursor: pointer; height: auto;
  }
  .sort-menu .dropdown button:hover { background: var(--bg-subtle); }
  .sort-menu .dropdown button.active { color: var(--accent); font-weight: 600; }
  .sort-menu .dropdown .dir { float: right; color: var(--muted); }

  /* Table */
  .table-wrap { padding: 12px 16px 16px; }
  table#tbl {
    width: 100%; border-collapse: separate; border-spacing: 0;
    font-variant-numeric: tabular-nums; font-size: 12px;
    background: var(--bg-canvas);
    border-radius: 10px; overflow: hidden;
    border: 1px solid var(--border);
  }
  table#tbl th {
    background: var(--header-bg); color: var(--text); font-weight: 700;
    border: none; border-bottom: 1px solid var(--border);
    padding: 7px 8px; text-align: center;
    cursor: pointer; user-select: none; white-space: nowrap;
    position: relative;
  }
  table#tbl th.no-sort { cursor: default; }
  table#tbl th:hover:not(.no-sort) { color: var(--accent); }
  table#tbl td {
    border: none; padding: 4px 8px;
    text-align: right; white-space: nowrap; height: 26px;
    border-bottom: 1px solid var(--border);
  }
  table#tbl tbody tr:last-child td { border-bottom: none; }
  table#tbl td.left { text-align: left; }
  table#tbl td.center { text-align: center; }
  table#tbl tbody tr:nth-child(even) td.alt-stripe { background: var(--row-alt); }
  table#tbl tbody tr:hover td.alt-stripe { background: var(--hover); }
  table#tbl tbody tr:hover { cursor: pointer; }
  th .arrow { margin-left: 5px; color: var(--accent); font-size: 10px; }

  /* Hover-tooltip on column headers — same visual style as the About modal cards */
  table#tbl th[data-tip]::after {
    content: attr(data-tip);
    position: absolute; left: 50%; top: calc(100% + 8px);
    transform: translateX(-50%);
    background: var(--bg-canvas); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 11px; font-size: 11.5px; font-weight: 500; font-style: normal;
    font-family: -apple-system, "Segoe UI", sans-serif;
    line-height: 1.5; text-align: left; white-space: normal;
    width: max-content; max-width: 260px;
    box-shadow: 0 6px 18px rgba(0,0,0,0.18);
    opacity: 0; pointer-events: none;
    transition: opacity 0.12s ease 0.15s;
    z-index: 40;
  }
  table#tbl th[data-tip]::before {
    content: ""; position: absolute; left: 50%; top: calc(100% + 2px);
    transform: translateX(-50%);
    border: 6px solid transparent; border-bottom-color: var(--border);
    opacity: 0; pointer-events: none;
    transition: opacity 0.12s ease 0.15s; z-index: 41;
  }
  table#tbl th[data-tip]:hover::after,
  table#tbl th[data-tip]:hover::before { opacity: 1; }
  /* Right-most columns: anchor tooltip to the right edge to avoid clipping */
  table#tbl th[data-tip]:nth-last-child(-n+4)::after { left: auto; right: 0; transform: none; }
  table#tbl th[data-tip]:nth-last-child(-n+4)::before { left: auto; right: 4px; transform: none; }

  .logo {
    width: 18px; height: 18px; vertical-align: middle; border-radius: 4px;
    object-fit: contain; background: transparent;
  }
  .logo-fallback {
    display: inline-flex; width: 18px; height: 18px; border-radius: 4px;
    background: var(--bg-subtle); color: var(--text); font-size: 9px; font-weight: 700;
    align-items: center; justify-content: center; vertical-align: middle;
  }
  td.sym { font-weight: 700; letter-spacing: 0.2px; }
  td.name { color: var(--text); max-width: 240px; overflow: hidden; text-overflow: ellipsis; }

  /* Δ Highs bar */
  .bar-cell {
    position: relative; width: 100%; height: 18px;
    border-radius: 4px; overflow: hidden; background: var(--bg-subtle);
  }
  .bar-cell .bar { position: absolute; right: 0; top: 0; height: 100%; border-radius: 4px; }
  .bar-cell .bar-label {
    position: relative; z-index: 2; padding: 0 6px; line-height: 18px;
    display: block; text-align: right; font-weight: 600;
  }

  svg.spark { width: 96px; height: 22px; vertical-align: middle; }
  svg.rs    { width: 86px; height: 22px; vertical-align: middle; }

  .tri-up   { color: var(--pos); font-size: 13px; }
  .tri-down { color: var(--neg); font-size: 13px; }
  .na { color: var(--muted); }

  /* Footer */
  .footer {
    border-top: 1px solid var(--border); padding: 9px 16px; display: flex;
    justify-content: space-between; align-items: center; color: var(--muted);
    font-size: 12px; background: var(--bg-subtle);
    margin: 0 16px 16px; border-radius: 0 0 10px 10px;
    border-left: 1px solid var(--border); border-right: 1px solid var(--border);
    border-bottom: 1px solid var(--border); border-top: none;
  }
  .footer .averages b { color: var(--text); font-weight: 700; }

  /* ===== Detail modal ===== */
  .modal-bg {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55); display: none;
    align-items: flex-start; justify-content: center; z-index: 50;
    padding: 24px 16px; overflow-y: auto;
  }
  .modal-bg.show { display: flex; }
  .modal {
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 14px;
    width: min(1120px, 96vw); color: var(--text);
    box-shadow: 0 18px 60px rgba(0,0,0,0.35);
    overflow: hidden;
  }
  .m-head {
    display: flex; align-items: flex-start; gap: 14px;
    padding: 18px 22px 14px; border-bottom: 1px solid var(--border);
  }
  .m-head .logo, .m-head .logo-fallback { width: 40px; height: 40px; font-size: 14px; flex-shrink: 0; }
  .m-title { flex: 1; min-width: 0; }
  .m-title h2 { margin: 0; font-size: 18px; font-weight: 700; line-height: 1.2; }
  .m-title .ticker { font-weight: 700; color: var(--accent); }
  .m-title .meta { font-size: 12px; color: var(--muted); margin-top: 4px; line-height: 1.4; }
  .m-title .meta a { color: var(--accent); text-decoration: none; }
  .m-title .meta a:hover { text-decoration: underline; }
  .m-price-block { text-align: right; flex-shrink: 0; }
  .m-price-block .p-now { font-size: 26px; font-weight: 700; line-height: 1.1; }
  .m-price-block .p-chg { font-size: 13px; font-weight: 600; margin-top: 4px; }
  .m-price-block .p-chg.pos { color: var(--pos); }
  .m-price-block .p-chg.neg { color: var(--neg); }
  .m-close {
    width: 32px; height: 32px; border-radius: 8px; border: 1px solid var(--border);
    background: transparent; cursor: pointer; font-size: 18px; color: var(--muted);
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .m-close:hover { background: var(--bg-subtle); color: var(--text); }

  /* Chart area */
  .m-chart-wrap { padding: 14px 22px 6px; position: relative; }
  .m-chart-toolbar {
    display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
    margin-bottom: 10px;
  }
  .m-range-tabs { display: inline-flex; gap: 2px; background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 8px; padding: 2px; }
  .m-range-tabs button {
    border: none; background: transparent; color: var(--muted);
    padding: 4px 10px; border-radius: 6px; font-size: 12px; cursor: pointer; font-weight: 600;
  }
  .m-range-tabs button:hover { color: var(--text); }
  .m-range-tabs button.active { background: var(--bg-canvas); color: var(--accent); box-shadow: 0 1px 2px rgba(0,0,0,0.08); }
  .m-toolbar-spacer { flex: 1; }
  .m-toolbar-btn {
    border: 1px solid var(--border); background: var(--bg-canvas); color: var(--text);
    padding: 4px 10px; border-radius: 7px; font-size: 12px; cursor: pointer;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .m-toolbar-btn:hover { border-color: var(--accent); color: var(--accent); }
  .m-toolbar-btn.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
  .m-toolbar-btn .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .dot.sp { background: #8b5cf6; }
  .dot.sec { background: #f59e0b; }

  .m-chart {
    position: relative; width: 100%; height: 320px;
    border: 1px solid var(--border); border-radius: 10px; overflow: hidden;
    background: var(--bg-canvas);
  }
  .m-chart svg { display: block; width: 100%; height: 100%; }
  .m-chart .crosshair-line { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 3 3; opacity: 0; pointer-events: none; }
  .m-chart .crosshair-dot { fill: var(--accent); stroke: var(--bg-canvas); stroke-width: 2; opacity: 0; pointer-events: none; }
  .m-chart .crosshair-dot.sp { fill: #8b5cf6; }
  .m-chart .crosshair-dot.sec { fill: #f59e0b; }
  .m-chart .sel-rect { fill: var(--accent); opacity: 0.10; pointer-events: none; }
  .m-tooltip {
    position: absolute; pointer-events: none; z-index: 5;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 8px;
    padding: 7px 10px; font-size: 11.5px; line-height: 1.5; color: var(--text);
    box-shadow: 0 4px 14px rgba(0,0,0,0.15);
    opacity: 0; transition: opacity 0.08s;
    min-width: 130px; white-space: nowrap;
  }
  .m-tooltip.show { opacity: 1; }
  .m-tooltip .tt-date { color: var(--muted); font-size: 11px; }
  .m-tooltip .tt-row { display: flex; justify-content: space-between; gap: 10px; }
  .m-tooltip .tt-label { display: inline-flex; align-items: center; gap: 5px; }

  .m-range-info {
    margin-top: 8px; padding: 8px 12px;
    background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 8px;
    font-size: 12px; color: var(--muted);
    display: flex; gap: 18px; flex-wrap: wrap;
  }
  .m-range-info b { color: var(--text); font-weight: 700; font-variant-numeric: tabular-nums; }
  .m-range-info .pos { color: var(--pos); }
  .m-range-info .neg { color: var(--neg); }

  /* Section grid */
  .m-sections {
    padding: 14px 22px 22px;
    display: grid; grid-template-columns: 1fr 1fr; gap: 14px;
  }
  @media (max-width: 760px) { .m-sections { grid-template-columns: 1fr; } }
  .m-sec {
    background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px;
  }
  .m-sec.full { grid-column: 1 / -1; }
  .m-sec h3 {
    margin: 0 0 8px; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.6px;
    color: var(--muted); font-weight: 700;
  }
  .m-kv {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px 10px;
  }
  .m-kv .k { color: var(--muted); font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.4px; }
  .m-kv .v { font-weight: 600; font-size: 13px; margin-top: 2px; font-variant-numeric: tabular-nums; }
  .m-kv .v.pos { color: var(--pos); }
  .m-kv .v.neg { color: var(--neg); }

  /* Performance table */
  .m-perf { width: 100%; font-size: 12px; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  .m-perf th, .m-perf td {
    padding: 5px 8px; text-align: right; border-bottom: 1px solid var(--border);
  }
  .m-perf th { font-weight: 700; color: var(--muted); text-transform: uppercase; font-size: 10.5px; letter-spacing: 0.4px; }
  .m-perf td:first-child, .m-perf th:first-child { text-align: left; }
  .m-perf tr:last-child td { border-bottom: none; }
  .m-perf .pos { color: var(--pos); font-weight: 600; }
  .m-perf .neg { color: var(--neg); font-weight: 600; }
  .m-perf .legend { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }

  /* Analyst */
  .m-analyst-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; align-items: start; }
  @media (max-width: 520px) { .m-analyst-grid { grid-template-columns: 1fr; } }
  .m-gauge {
    width: 100%; height: 9px; background: linear-gradient(to right, var(--pos) 0%, var(--pos) 20%, #fcd34d 40%, #f59e0b 60%, var(--neg) 80%, var(--neg) 100%);
    border-radius: 99px; position: relative; margin: 6px 0 4px;
  }
  .m-gauge .needle {
    position: absolute; top: -3px; width: 4px; height: 15px;
    background: var(--text); border-radius: 2px;
    transform: translateX(-50%);
    box-shadow: 0 0 0 2px var(--bg-subtle);
  }
  .m-gauge-labels {
    display: flex; justify-content: space-between; font-size: 9.5px;
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; margin-top: 4px;
  }
  .m-rec-key {
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
  }
  .m-rec-key.buy { background: rgba(var(--pos-rgb), 0.15); color: var(--pos); }
  .m-rec-key.hold { background: rgba(var(--warn-rgb), 0.15); color: var(--warn); }
  .m-rec-key.sell { background: rgba(var(--neg-rgb), 0.15); color: var(--neg); }
  .m-target-bar {
    position: relative; height: 30px; margin-top: 10px;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 7px;
  }
  .m-target-bar .tb-track {
    position: absolute; top: 50%; left: 6%; right: 6%; height: 4px;
    transform: translateY(-50%); background: linear-gradient(to right, rgba(var(--neg-rgb),0.3), rgba(var(--pos-rgb),0.3));
    border-radius: 2px;
  }
  .m-target-bar .tb-mark {
    position: absolute; top: 4px; bottom: 4px; width: 2px;
    transform: translateX(-50%);
  }
  .m-target-bar .tb-mark.current { background: var(--accent); }
  .m-target-bar .tb-mark.target { background: var(--text); width: 3px; }
  .m-target-bar .tb-low, .m-target-bar .tb-high {
    position: absolute; top: 50%; transform: translateY(-50%);
    font-size: 10px; color: var(--muted);
  }
  .m-target-bar .tb-low { left: 4px; }
  .m-target-bar .tb-high { right: 4px; }
  .m-target-labels {
    display: flex; justify-content: space-between; font-size: 11px; margin-top: 6px;
    color: var(--muted);
  }
  .m-target-labels b { color: var(--text); font-variant-numeric: tabular-nums; }
  .m-target-labels .upside.pos { color: var(--pos); font-weight: 700; }
  .m-target-labels .upside.neg { color: var(--neg); font-weight: 700; }

  /* Recommendation distribution bars */
  .m-rec-bars { display: flex; height: 18px; border-radius: 5px; overflow: hidden; margin-top: 6px; border: 1px solid var(--border); }
  .m-rec-bars div {
    display: flex; align-items: center; justify-content: center;
    font-size: 10px; color: #fff; font-weight: 700; min-width: 0;
  }
  .m-rec-bars .sb { background: #15803d; }
  .m-rec-bars .b  { background: #22c55e; }
  .m-rec-bars .h  { background: #eab308; }
  .m-rec-bars .s  { background: #f97316; }
  .m-rec-bars .ss { background: #dc2626; }
  .m-rec-legend { display: flex; gap: 10px; font-size: 10.5px; color: var(--muted); margin-top: 6px; flex-wrap: wrap; }
  .m-rec-legend span { display: inline-flex; align-items: center; gap: 4px; }
  .m-rec-legend i { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }

  /* News */
  .m-news-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  .m-news-list li {
    border-bottom: 1px dashed var(--border); padding-bottom: 8px;
  }
  .m-news-list li:last-child { border-bottom: none; padding-bottom: 0; }
  .m-news-list a {
    color: var(--text); text-decoration: none; font-size: 12.5px; font-weight: 600; line-height: 1.4;
    display: block;
  }
  .m-news-list a:hover { color: var(--accent); }
  .m-news-list .src { font-size: 10.5px; color: var(--muted); margin-top: 3px; }

  .m-summary { font-size: 12px; color: var(--muted); line-height: 1.55; }
  .m-summary.collapsed { display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .m-summary-toggle { background: none; border: none; color: var(--accent); font-size: 11.5px; cursor: pointer; padding: 4px 0 0; font-weight: 600; }

  .toast {
    position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%);
    background: var(--bg-canvas); border: 1px solid var(--border); padding: 9px 14px;
    border-radius: 8px; font-size: 12.5px; opacity: 0; transition: opacity 0.25s;
    z-index: 100; box-shadow: 0 4px 14px rgba(0,0,0,0.10); color: var(--text);
  }
  .toast.show { opacity: 1; }
</style>
</head>
<body>

<div class="topbar" id="topbar">
  <button id="edit-btn" class="portfolio-btn"><span class="pf-icon">▦</span> Portfolio</button>
  <button id="refresh">↻ Refresh</button>
  <button id="export">⬇ CSV</button>
  <button id="save">★ Save Watchlist</button>
  <div class="sort-menu" id="sort-menu">
    <button id="sort-btn">⇅ Sort: <span id="sort-label">% YTD ▼</span></button>
    <div class="dropdown" id="sort-dropdown"></div>
  </div>
  <span class="spacer"></span>
  <button class="info-btn" id="info-btn" title="How this works"><span class="info-icon-circle">i</span></button>
  <button class="theme-switch" id="theme-switch" title="Toggle theme" aria-label="Toggle theme">
    <i class="ts-icon" id="ts-sun">&#9728;</i>
    <span class="ts-track" id="ts-track"><span class="ts-thumb"></span></span>
    <i class="ts-icon" id="ts-moon">&#9790;</i>
  </button>
  <span class="status" id="status">Idle</span>
</div>
<div class="progress-wrap" id="progress-wrap"><div class="progress-bar" id="progress-bar"></div></div>

<div id="input-panel" class="hidden">
  <!-- Browser-style tab strip of saved portfolios -->
  <div class="pf-tabs" id="pf-tabs" role="tablist"></div>

  <!-- Constituents editor -->
  <div class="pf-editor">
    <div class="pf-editor-head">
      <span id="pf-editor-title">Constituents</span>
      <span id="pf-editor-meta" class="pf-editor-meta"></span>
    </div>
    <textarea id="tickers" rows="2" placeholder="Paste tickers or company names — comma or newline separated.
e.g.  DELL, TXN, DaVita, JBL, KLAC, MARA, COMT, FFIV, Alphabet, ETN, AVGO, NVDA, XLK, Trane, MSTR, COST, Apple, Microsoft"></textarea>
    <div class="panel-row">
      <button id="build" class="primary">Build Dashboard</button>
      <button id="save-as" class="ghost">＋ Save as new</button>
      <span class="spacer"></span>
      <span class="status" id="editor-status">Paste your tickers and press Build (or Cmd/Ctrl + Enter).</span>
    </div>
  </div>

  <!-- Analytics sub-window -->
  <div class="pf-analytics" id="pf-analytics">
    <div class="pf-analytics-head">
      <span class="pf-analytics-title">Portfolio analytics</span>
      <div class="pf-mode-toggle" id="pf-mode-toggle">
        <button data-mode="equal">Equal-weight</button>
        <button data-mode="cap" class="active">Cap-weighted</button>
        <button data-mode="custom">Custom…</button>
      </div>
      <div class="pf-period-tabs" id="pf-period-tabs">
        <button data-p="3M">3M</button>
        <button data-p="6M">6M</button>
        <button data-p="YTD">YTD</button>
        <button data-p="1Y" class="active">1Y</button>
        <button data-p="3Y">3Y</button>
        <button data-p="5Y">5Y</button>
        <button data-p="MAX">Max</button>
      </div>
      <div class="pf-overlay-toggles" id="pf-overlay-toggles">
        <label><input type="checkbox" id="pf-show-spy" checked> SPY</label>
        <label><input type="checkbox" id="pf-show-sec"> Sector mix</label>
        <label><input type="checkbox" id="pf-show-dd" checked> Drawdown</label>
      </div>
    </div>
    <div class="pf-analytics-body" id="pf-analytics-body">
      <div class="pf-empty" id="pf-empty">Build the dashboard to compute portfolio analytics.</div>
    </div>
  </div>
</div>

<!-- Delete-portfolio confirmation modal -->
<div class="confirm-bg" id="confirm-bg">
  <div class="confirm-modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
    <div class="confirm-head" id="confirm-title">Delete portfolio?</div>
    <div class="confirm-body" id="confirm-body">This portfolio and its cached results will be removed.</div>
    <div class="confirm-foot">
      <button id="confirm-cancel" type="button">Cancel</button>
      <button id="confirm-ok" class="danger" type="button">Delete</button>
    </div>
  </div>
</div>

<!-- Custom-weight popup -->
<div class="pf-weights-bg" id="pf-weights-bg">
  <div class="pf-weights-modal">
    <div class="pf-weights-head">
      <span class="pf-weights-title">Custom weights</span>
      <div class="pf-weights-sum" id="pf-weights-sum">100.0%</div>
      <button id="pf-weights-equal" class="pf-weights-action" title="Set all to equal weight">Equal-weighted</button>
      <button id="pf-weights-reset" class="pf-weights-action" title="Reset to market-cap weights">Cap-weighted</button>
      <button id="pf-weights-close" class="pf-weights-action" title="Close">✕</button>
    </div>
    <div class="pf-weights-hint">Slide or type to set weights. Lock pins a row; others auto-rebalance.</div>
    <div class="pf-weights-body" id="pf-weights-body"></div>
    <div class="pf-weights-foot">
      <button id="pf-weights-apply" class="primary">Apply</button>
      <button id="pf-weights-cancel">Cancel</button>
    </div>
  </div>
</div>

<div class="table-wrap">
  <table id="tbl">
    <thead><tr id="thead"></tr></thead>
    <tbody id="tbody"><tr><td colspan="16" style="padding:30px; text-align:center; color:var(--muted);">Press <b>Build Dashboard</b> above to load your portfolio.</td></tr></tbody>
  </table>
</div>

<div class="footer">
  <span class="credit">📈 Local Portfolio Dashboard · yfinance · no API key</span>
  <span class="averages" id="averages">Avg. P/S <b>—</b> &nbsp;&nbsp; P/E <b>—</b></span>
  <span class="date" id="footer-date"></span>
</div>

<div class="info-bg" id="info-bg">
  <div class="info-modal">
    <div class="info-head">
      <div>
        <div class="info-head-title">Portfolio Tracker — Column Guide</div>
        <div class="info-head-sub">What each column measures and how it is calculated</div>
      </div>
      <button class="info-head-close" onclick="closeInfo()">&#215;</button>
    </div>
    <p class="info-intro">
      Data is fetched live from Yahoo Finance via yfinance. Each row is one security. Click any row for a
      detail view. Use <b>⇅ Sort</b> to reorder by any metric. Colors are heatmaps — hover a cell to see raw values.
    </p>
    <div class="info-grid">
      <div class="info-card">
        <div class="info-card-name">Ticker</div>
        <div class="info-card-desc">Exchange symbol used to identify the security (e.g. AAPL, NVDA, XLK). Company names are resolved automatically.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Company</div>
        <div class="info-card-desc">Full legal name of the company or fund as reported by Yahoo Finance.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Price</div>
        <div class="info-card-desc">Last available closing price in USD, as of the build/refresh time.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Market Cap</div>
        <div class="info-card-desc">Total market value of all outstanding shares.</div>
        <div class="info-formula">$$\text{Market Cap} = P \times \text{Shares Outstanding}$$</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">P/S &mdash; Price-to-Sales</div>
        <div class="info-card-desc">How much investors pay per dollar of trailing-12-month revenue. Useful for companies with zero or negative earnings. Heat anchors at P/S = 10 (expensive); n/a is also flagged.</div>
        <div class="info-formula">$$P/S = \dfrac{\text{Market Cap}}{\text{TTM Revenue}}$$</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">P/E &mdash; Price-to-Earnings</div>
        <div class="info-card-desc">Most widely-used valuation multiple. Heat anchors at P/E = 40; values above 50 imply heavy growth expectations being priced in.</div>
        <div class="info-formula">$$P/E = \dfrac{P}{\text{EPS}_{\text{TTM}}}$$</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">% YTD</div>
        <div class="info-card-desc">Return from the first trading day of the current calendar year to today. Green = positive, red = negative, white = flat.</div>
        <div class="info-formula">$$\text{YTD} = \left(\dfrac{P_1}{P_0} - 1\right) \times 100$$
          <span class="formula-note">P&#8320; = first close of the calendar year</span></div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Chart 1Y</div>
        <div class="info-card-desc">Sparkline of the last 252 trading days' closing prices. Line is green if the 1-year return is positive, red otherwise. Click the row for a full-size chart.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">% 1Y</div>
        <div class="info-card-desc">Total price return over the last 365 calendar days. Same diverging color scale as % YTD.</div>
        <div class="info-formula">$$\text{1Y} = \left(\dfrac{P_1}{P_0} - 1\right) \times 100$$
          <span class="formula-note">P&#8320; = close 365 calendar days ago</span></div>
      </div>
      <div class="info-card">
        <div class="info-card-name">&#916; Highs &mdash; Distance from ATH</div>
        <div class="info-card-desc">How far the current price sits below its all-time high over the available history. 0% means the stock is at an all-time high. The bar grows as the drawdown deepens; full bar = 50% below ATH.</div>
        <div class="info-formula">$$\Delta\text{ATH} = \left(\dfrac{P}{P_{\text{ATH}}} - 1\right) \times 100$$</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">RS Rank 1M &mdash; Relative Strength</div>
        <div class="info-card-desc">Histogram of 12 monthly bars. Each bar's height shows where that month's closing price ranked within the stock's own trailing 12-month price range. Taller, brighter green = stronger relative position.</div>
        <div class="info-formula">$$\text{RS} = \dfrac{P_{m} - \text{Low}_{12m}}{\text{High}_{12m} - \text{Low}_{12m}} \;\in\; [0,\,1]$$</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">20 / 50 / 200 SMA</div>
        <div class="info-card-desc">Simple Moving Average flags. &#9650; = price is above the SMA (bullish momentum). &#9660; = price is below (bearish). The three periods correspond to roughly 1 month, 1 quarter, and 1 year of trading days.</div>
        <div class="info-formula">$$\text{SMA}_n = \dfrac{1}{n}\sum_{i=1}^{n} P_i$$</div>
      </div>
    </div>
  </div>
</div>

<div class="modal-bg" id="modal-bg"><div class="modal" id="modal"></div></div>
<div class="toast" id="toast"></div>

<script>
"use strict";

/* ===========================================================================
 * Column definitions
 * --------------------------------------------------------------------------- */
const COLS = [
  { key: "logo",        label: "",          w: 26,  align: "center", sortable: false,
    render: (r) => logoImg(r.symbol) },
  { key: "symbol",      label: "Ticker",    w: 64,  align: "left", sortable: true,
    render: (r) => `<span>${r.symbol}</span>`, td_cls: "sym left" },
  { key: "name",        label: "Company",   w: 220, align: "left", sortable: true,
    render: (r) => escapeHtml(r.name || ""), td_cls: "name left" },
  { key: "price",       label: "Price",     w: 90,  align: "right", sortable: true,
    render: (r) => fmtMoney(r.price) },
  { key: "market_cap",  label: "Market Cap",w: 86,  align: "right", sortable: true,
    render: (r) => fmtCompactMoney(r.market_cap) },
  /* P/S: analyst rule-of-thumb — anchor full-orange at P/S = 10. n/a is also
     suspicious so we paint it the most saturated colour. */
  { key: "ps_ratio",    label: "P/S",       w: 56,  align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 0, clipMax: 10, naMax: true },
    render: (r) => fmt2(r.ps_ratio) },
  /* P/E: anchor full-orange at P/E = 40 (anything above is growth/speculative). */
  { key: "pe_ratio",    label: "P/E",       w: 56,  align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 0, clipMax: 40, naMax: true },
    render: (r) => fmt2(r.pe_ratio) },
  { key: "pct_ytd",     label: "% YTD",     w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 100 },
    render: (r) => fmtPctSigned(r.pct_ytd) },
  { key: "spark",       label: "Chart 1Y",  w: 100, align: "center", sortable: false,
    bg: (r) => sparkBg(r.pct_1y),
    render: (r) => sparkSvg(r.sparkline, r.pct_1y) },
  { key: "pct_1y",      label: "% 1Y",      w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 100 },
    render: (r) => fmtPctSigned(r.pct_1y) },
  /* Δ Highs: percent below all-time high. 0% = at high, -50% = halved.
     Anchor full-red at -50%. */
  { key: "delta_ath",   label: "Δ Highs",   w: 110, align: "right", sortable: true,
    render: (r) => deltaBar(r.delta_ath) },
  { key: "rs_rank",     label: "RS Rank 1M",w: 92,  align: "center", sortable: false,
    render: (r) => rsBars(r.rs_rank) },
  { key: "above_sma_20",  label: "20SMA",   w: 46,  align: "center", sortable: true,
    render: (r) => triangle(r.above_sma_20),
    sortValue: (r) => r.above_sma_20 === null ? null : (r.above_sma_20 ? 1 : 0) },
  { key: "above_sma_50",  label: "50SMA",   w: 46,  align: "center", sortable: true,
    render: (r) => triangle(r.above_sma_50),
    sortValue: (r) => r.above_sma_50 === null ? null : (r.above_sma_50 ? 1 : 0) },
  { key: "above_sma_200", label: "200SMA",  w: 50,  align: "center", sortable: true,
    render: (r) => triangle(r.above_sma_200),
    sortValue: (r) => r.above_sma_200 === null ? null : (r.above_sma_200 ? 1 : 0) },
];

/* Short hover descriptions for the column-header info icons.
   Long-form versions with formulas live in the About / Column Guide modal. */
const COL_INFO = {
  symbol:        "Exchange ticker symbol (e.g. AAPL, NVDA, XLK).",
  name:          "Full company or fund name from Yahoo Finance.",
  price:         "Last available closing price in USD, as of the build/refresh time.",
  market_cap:    "Total market value of all outstanding shares (Price × Shares Outstanding).",
  ps_ratio:      "Price-to-Sales: market cap ÷ trailing-12-month revenue. Heat anchors at P/S = 10; n/a is flagged.",
  pe_ratio:      "Price-to-Earnings: price ÷ trailing-12-month EPS. Heat anchors at P/E = 40; above 50 implies heavy growth pricing.",
  pct_ytd:       "Return from the first trading day of the current calendar year to today.",
  spark:         "Sparkline of the last 252 trading days. Green if 1Y return is positive, red otherwise.",
  pct_1y:        "Total price return over the last 365 calendar days.",
  delta_ath:     "Distance from all-time high. 0% = at ATH; full bar = 50% below ATH.",
  rs_rank:       "Relative Strength: 12 monthly bars showing where each month's close ranked within its trailing-12-month price range.",
  above_sma_20:  "20-day Simple Moving Average flag. ▲ price above SMA (bullish), ▼ below (bearish). ~1 month of trading days.",
  above_sma_50:  "50-day Simple Moving Average flag. ▲ price above SMA (bullish), ▼ below (bearish). ~1 quarter of trading days.",
  above_sma_200: "200-day Simple Moving Average flag. ▲ price above SMA (bullish), ▼ below (bearish). ~1 year of trading days.",
};

let DATA = [];
let SORT = { key: "pct_ytd", dir: -1 };

/* ===========================================================================
 * Theme
 * --------------------------------------------------------------------------- */
function getTheme() { return document.documentElement.dataset.theme || "light"; }
function setTheme(name) {
  document.documentElement.dataset.theme = name;
  localStorage.setItem("theme", name);
  const track = document.getElementById("ts-track");
  if (track) track.classList.toggle("on", name === "dark");
  if (DATA.length) render();
}
function readTheme() {
  const saved = localStorage.getItem("theme");
  if (saved === "dark" || saved === "light") return saved;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/* Heat-map endpoint colors per theme. */
const THEME_COLORS = {
  light: {
    bg:   [255, 255, 255],
    pos:  [31, 136, 61],     /* #1f883d  github success.emphasis */
    neg:  [207, 34, 46],     /* #cf222e  github danger.emphasis  */
    warn: [249, 115, 22],    /* #f97316  vivid orange (Tailwind orange-500) */
  },
  dark: {
    bg:   [13, 17, 23],      /* #0d1117 */
    pos:  [63, 185, 80],     /* #3fb950 */
    neg:  [248, 81, 73],     /* #f85149 */
    warn: [251, 146, 60],    /* #fb923c  orange-400, lighter on dark bg */
  },
};

/* ===========================================================================
 * Utility helpers
 * --------------------------------------------------------------------------- */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
function escapeHtml(s) {
  return String(s).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[c]));
}
function lerp(a, b, t) { return a + (b - a) * t; }
function clamp(x, a, b) { return Math.max(a, Math.min(b, x)); }
function rgb(r, g, b) { return `rgb(${r|0},${g|0},${b|0})`; }
function rgbMix(c1, c2, t) {
  return rgb(lerp(c1[0], c2[0], t), lerp(c1[1], c2[1], t), lerp(c1[2], c2[2], t));
}

function fmtMoney(v) {
  if (v == null || !isFinite(v)) return na();
  return "$" + Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
function fmtCompactMoney(v) {
  if (v == null || !isFinite(v) || v === 0) return na();
  const a = Math.abs(v);
  let unit, scaled;
  if (a >= 1e12) { unit = "T"; scaled = v/1e12; }
  else if (a >= 1e9) { unit = "B"; scaled = v/1e9; }
  else if (a >= 1e6) { unit = "M"; scaled = v/1e6; }
  else if (a >= 1e3) { unit = "K"; scaled = v/1e3; }
  else { return "$" + v.toFixed(2); }
  return "$" + scaled.toFixed(1) + unit;
}
function fmt2(v) { return (v == null || !isFinite(v)) ? na() : Number(v).toFixed(2); }
function fmtPctSigned(v) {
  if (v == null || !isFinite(v)) return na();
  const sign = v > 0 ? "+" : (v < 0 ? "" : "+");
  return sign + v.toFixed(2) + "%";
}
function fmtPctDirect(v) {
  if (v == null || !isFinite(v)) return na();
  return (Number(v) * 100).toFixed(2) + "%";
}
function na() { return '<span class="na">n/a</span>'; }

/* ===========================================================================
 * Heat-map scales
 * --------------------------------------------------------------------------- */
function colorYO(t, theme) {
  /* yellow → orange.  Tints the background colour toward the "warn" endpoint
     so it works in both themes. */
  t = clamp(t, 0, 1);
  const C = THEME_COLORS[theme];
  /* Use ~92% of the way to warn at full saturation so text stays readable. */
  return rgbMix(C.bg, C.warn, t * 0.92);
}
function colorDiverging(t, theme) {
  /* t in [-1, 1]; 0 → background (white in light, near-black in dark). */
  t = clamp(t, -1, 1);
  const C = THEME_COLORS[theme];
  const tgt = t >= 0 ? C.pos : C.neg;
  return rgbMix(C.bg, tgt, Math.abs(t) * 0.9);
}
function textOnHeat(t, theme) {
  /* Switch to white text once tint is deep enough that the standard fg
     would lose contrast.  Pick threshold per theme. */
  const mag = Math.abs(t);
  if (theme === "dark") return mag > 0.65 ? "#ffffff" : "var(--text)";
  return mag > 0.55 ? "#ffffff" : "var(--text)";
}

/* ===========================================================================
 * Visual primitives
 * --------------------------------------------------------------------------- */
function logoImg(ticker) {
  const t = encodeURIComponent(ticker);
  return `<img class="logo" loading="lazy" alt=""
    src="https://financialmodelingprep.com/image-stock/${t}.png"
    onerror="logoFallback(this, '${t.replace(/'/g,"\\'")}')">`;
}
window.logoFallback = function(img, ticker) {
  if (img.dataset.tried === "parqet") {
    img.onerror = null;
    const span = document.createElement("span");
    span.className = "logo-fallback";
    const sym = String(ticker).replace(/^\^/, "").replace(/[^A-Za-z0-9]/g, "");
    span.textContent = sym.slice(0, 2) || "?";
    img.replaceWith(span);
    return;
  }
  img.dataset.tried = "parqet";
  img.src = `https://assets.parqet.com/logos/symbol/${ticker}?format=png`;
};

function triangle(v) {
  if (v === null || v === undefined) return '<span class="na">—</span>';
  return v ? '<span class="tri-up">▲</span>' : '<span class="tri-down">▼</span>';
}

function sparkSvg(pts, pct1y) {
  if (!pts || pts.length < 2) return na();
  const w = 96, h = 22, pad = 1;
  const lo = Math.min(...pts), hi = Math.max(...pts);
  const rng = (hi - lo) || 1;
  const step = (w - 2 * pad) / (pts.length - 1);
  let d = "";
  for (let i = 0; i < pts.length; i++) {
    const x = pad + i * step;
    const y = h - pad - ((pts[i] - lo) / rng) * (h - 2 * pad);
    d += (i === 0 ? "M" : "L") + x.toFixed(1) + "," + y.toFixed(1) + " ";
  }
  const up = (pct1y != null ? pct1y : (pts[pts.length-1] - pts[0])) >= 0;
  const stroke = up
    ? getComputedStyle(document.documentElement).getPropertyValue("--pos").trim() || "#1f883d"
    : getComputedStyle(document.documentElement).getPropertyValue("--neg").trim() || "#cf222e";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.2"/>
  </svg>`;
}
function sparkBg(pct1y) {
  /* light tint for the chart cell so positive/negative reads at a glance. */
  if (pct1y == null || !isFinite(pct1y)) return "";
  const theme = getTheme();
  const C = THEME_COLORS[theme];
  const tgt = pct1y >= 0 ? C.pos : C.neg;
  return `background:${rgbMix(C.bg, tgt, 0.18)};`;
}

function rsBars(arr) {
  if (!arr || !arr.length) return na();
  const w = 86, h = 22, n = arr.length, gap = 1;
  const bw = (w - (n - 1) * gap) / n;
  const theme = getTheme();
  const C = THEME_COLORS[theme];
  let svg = "";
  for (let i = 0; i < n; i++) {
    const v = clamp(arr[i] || 0, 0, 1);
    const bh = Math.max(1, v * (h - 2));
    const x = i * (bw + gap);
    const y = h - bh;
    /* Brighter green for higher rank — tint pos endpoint into bg. */
    const c = rgbMix(C.bg, C.pos, 0.35 + v * 0.6);
    svg += `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${bw.toFixed(2)}" height="${bh.toFixed(2)}" fill="${c}" rx="0.5"/>`;
  }
  return `<svg class="rs" viewBox="0 0 ${w} ${h}">${svg}</svg>`;
}

function deltaBar(v) {
  if (v == null || !isFinite(v)) return na();
  /* v ≤ 0 normally (% below ATH). 0 = at high (good), large neg = bad. */
  const mag = Math.abs(v);
  const MAX = 50;  // anchor: 50% drawdown = full bar
  const width = clamp(mag / MAX, 0, 1) * 100;
  const theme = getTheme();
  const C = THEME_COLORS[theme];
  /* Bar tint: at high → green, getting darker red as drawdown grows. */
  let intensity;
  let tgt;
  if (mag < 3) { tgt = C.pos; intensity = 0.30; }
  else if (mag < 15) { tgt = C.warn; intensity = 0.55; }
  else { tgt = C.neg; intensity = clamp(0.55 + (mag - 15) / 50, 0.55, 0.95); }
  const bg = rgbMix(C.bg, tgt, intensity);
  const label = (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  const txt = intensity > 0.6 ? "#fff" : "var(--text)";
  return `<div class="bar-cell">
    <div class="bar" style="width:${width.toFixed(1)}%; background:${bg}"></div>
    <span class="bar-label" style="color:${txt}">${label}</span>
  </div>`;
}

/* ===========================================================================
 * Heat-map cell styles
 * --------------------------------------------------------------------------- */
function cellStyleHeat(col, value, theme) {
  const h = col.heat;
  if (!h) return "";
  if (h.kind === "yo") {
    /* Fixed-ceiling orange ramp. n/a → max if h.naMax. */
    let t;
    if (value == null || !isFinite(value)) {
      if (!h.naMax) return "";
      t = 1;
    } else {
      const lo = h.clipMin, hi = h.clipMax;
      t = (hi === lo) ? 0.5 : clamp((value - lo) / (hi - lo), 0, 1);
    }
    return `background:${colorYO(t, theme)};`;
  }
  if (h.kind === "div") {
    if (value == null || !isFinite(value)) return "";
    const a = h.anchor || 100;
    const t = clamp(value / a, -1, 1);
    return `background:${colorDiverging(t, theme)}; color:${textOnHeat(t, theme)};`;
  }
  return "";
}

/* ===========================================================================
 * Sort menu
 * --------------------------------------------------------------------------- */
function renderSortMenu() {
  const dd = $("#sort-dropdown"); dd.innerHTML = "";
  for (const c of COLS) {
    if (!c.sortable) continue;
    const b = document.createElement("button");
    const isActive = SORT.key === c.key;
    if (isActive) b.classList.add("active");
    const dir = isActive ? (SORT.dir > 0 ? " ▲" : " ▼") : "";
    b.innerHTML = `${c.label || c.key}<span class="dir">${dir}</span>`;
    b.onclick = () => {
      if (SORT.key === c.key) SORT.dir *= -1;
      else { SORT.key = c.key; SORT.dir = ["pct_ytd", "pct_1y", "delta_ath", "market_cap", "price"].includes(c.key) ? -1 : 1; }
      $("#sort-menu").classList.remove("open");
      render();
    };
    dd.appendChild(b);
  }
}
function updateSortLabel() {
  const c = COLS.find(c => c.key === SORT.key);
  $("#sort-label").textContent = (c ? c.label : SORT.key) + " " + (SORT.dir > 0 ? "▲" : "▼");
}

/* ===========================================================================
 * Render
 * --------------------------------------------------------------------------- */
function renderHeader() {
  const tr = $("#thead"); tr.innerHTML = "";
  for (const c of COLS) {
    const th = document.createElement("th");
    th.textContent = c.label || "";
    th.style.minWidth = c.w + "px";
    if (!c.sortable) th.classList.add("no-sort");
    if (c.sortable) {
      th.onclick = () => {
        if (SORT.key === c.key) SORT.dir *= -1;
        else { SORT.key = c.key; SORT.dir = ["pct_ytd", "pct_1y", "delta_ath", "market_cap", "price"].includes(c.key) ? -1 : 1; }
        render();
      };
      if (SORT.key === c.key) {
        const a = document.createElement("span"); a.className = "arrow";
        a.textContent = SORT.dir > 0 ? "▲" : "▼"; th.appendChild(a);
      }
    }
    if (COL_INFO[c.key]) {
      th.setAttribute("data-tip", COL_INFO[c.key]);
      th.setAttribute("aria-label", (c.label || c.key) + ": " + COL_INFO[c.key]);
    }
    tr.appendChild(th);
  }
}

function render() {
  renderHeader();
  renderSortMenu();
  updateSortLabel();
  const tbody = $("#tbody"); tbody.innerHTML = "";
  if (!DATA.length) {
    tbody.innerHTML = `<tr><td colspan="${COLS.length}" style="padding:30px; text-align:center; color:var(--muted);">Press <b>Build Dashboard</b> above to load your portfolio.</td></tr>`;
    $("#averages").innerHTML = "Avg. P/S <b>—</b> &nbsp;&nbsp; P/E <b>—</b>";
    return;
  }
  let rows = DATA.slice();
  if (SORT.key) {
    const col = COLS.find(c => c.key === SORT.key);
    const getter = col && col.sortValue ? col.sortValue : (r) => r[SORT.key];
    rows.sort((a, b) => {
      const av = getter(a), bv = getter(b);
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * SORT.dir;
      return String(av).localeCompare(String(bv)) * SORT.dir;
    });
  }
  const theme = getTheme();
  for (const r of rows) {
    const tr = document.createElement("tr");
    for (const c of COLS) {
      const td = document.createElement("td");
      if (c.align === "left") td.classList.add("left");
      else if (c.align === "center") td.classList.add("center");
      if (c.td_cls) td.className = c.td_cls;
      let styles = "";
      if (c.heat) styles += cellStyleHeat(c, r[c.key], theme);
      if (c.bg)   styles += c.bg(r);
      /* Mark cells that should follow the alt-row stripe (no heat / no custom bg). */
      if (!c.heat && !c.bg) td.classList.add("alt-stripe");
      if (styles) td.style.cssText = styles;
      if (r.error && c.key !== "symbol" && c.key !== "name" && c.key !== "logo") {
        td.innerHTML = c.key === "price"
          ? `<span style="color:var(--neg)">${escapeHtml(r.error)}</span>` : "";
      } else {
        td.innerHTML = c.render(r);
      }
      tr.appendChild(td);
    }
    tr.onclick = () => openModal(r);
    tbody.appendChild(tr);
  }
  const ps = rows.map(r => r.ps_ratio).filter(v => v != null && isFinite(v));
  const pe = rows.map(r => r.pe_ratio).filter(v => v != null && isFinite(v));
  const psAvg = ps.length ? (ps.reduce((a,b)=>a+b,0) / ps.length) : null;
  const peAvg = pe.length ? (pe.reduce((a,b)=>a+b,0) / pe.length) : null;
  $("#averages").innerHTML = `Avg. P/S <b>${psAvg != null ? psAvg.toFixed(2) : "—"}</b> &nbsp;&nbsp; P/E <b>${peAvg != null ? peAvg.toFixed(2) : "—"}</b>`;
}

/* ===========================================================================
 * Modal detail view
 * --------------------------------------------------------------------------- */
/* ----- Detail modal state ----- */
const DETAIL = {
  data: null,           // detail payload from /api/detail
  row: null,            // original row data (fallback while loading)
  range: "1Y",          // active range tab
  showSP: false,        // overlay S&P 500
  showSector: false,    // overlay sector ETF
  showVol: true,        // volume bars
  // chart geometry — rebuilt every render
  geom: null,
};
const RANGES = ["1M","3M","6M","YTD","1Y","5Y","MAX"];

function openModal(r) {
  if (r.error) return;
  DETAIL.data = null; DETAIL.row = r;
  DETAIL.range = "1Y"; DETAIL.showSP = false; DETAIL.showSector = false; DETAIL.showVol = true;
  renderModalSkeleton();
  $("#modal-bg").classList.add("show");
  fetch("/api/detail?symbol=" + encodeURIComponent(r.symbol))
    .then(res => res.json())
    .then(d => {
      if (d && !d.error) {
        DETAIL.data = d;
        renderModalFull();
      } else {
        $("#m-loading").textContent = "Failed to load detail: " + (d.error || "unknown");
      }
    })
    .catch(e => { $("#m-loading").textContent = "Network error: " + e.message; });
}
function closeModal() { $("#modal-bg").classList.remove("show"); DETAIL.data = null; }
$("#modal-bg").addEventListener("click", (e) => { if (e.target.id === "modal-bg") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeInfo(); } });

function renderModalSkeleton() {
  const r = DETAIL.row;
  const pc = r.pct_1d;
  const chgCls = (pc != null && pc >= 0) ? "pos" : "neg";
  const sign = (pc != null && pc >= 0) ? "▲" : "▼";
  const change = (r.change_abs_1d != null) ? (r.change_abs_1d >= 0 ? "+" : "") + r.change_abs_1d.toFixed(2) : "—";
  $("#modal").innerHTML = `
    <div class="m-head">
      ${logoImg(r.symbol)}
      <div class="m-title">
        <h2><span class="ticker">${r.symbol}</span> — ${escapeHtml(r.name || "")}</h2>
        <div class="meta">
          ${[r.exchange, r.sector, r.industry, r.currency].filter(Boolean).map(escapeHtml).join(" · ")}
          ${r.website ? ` · <a href="${escapeHtml(r.website)}" target="_blank" rel="noopener">website ↗</a>` : ""}
        </div>
      </div>
      <div class="m-price-block">
        <div class="p-now">${fmtMoney(r.price)}</div>
        <div class="p-chg ${chgCls}">${sign} ${fmtPctSigned(pc)} <span style="opacity:0.7">(${change})</span></div>
      </div>
      <button class="m-close" onclick="closeModal()" title="Close">×</button>
    </div>
    <div class="m-chart-wrap">
      <div class="m-chart-toolbar">
        <div class="m-range-tabs" id="m-range-tabs">
          ${RANGES.map(rg => `<button data-range="${rg}" class="${rg === DETAIL.range ? "active" : ""}">${rg}</button>`).join("")}
        </div>
        <span class="m-toolbar-spacer"></span>
        <button class="m-toolbar-btn" id="m-toggle-sp" title="Compare to S&P 500"><span class="dot sp"></span>S&amp;P 500</button>
        <button class="m-toolbar-btn" id="m-toggle-sec" title="Compare to sector ETF"><span class="dot sec"></span>Sector</button>
        <button class="m-toolbar-btn active" id="m-toggle-vol" title="Toggle volume bars">Volume</button>
      </div>
      <div class="m-chart" id="m-chart">
        <div id="m-loading" style="position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:12.5px;">Loading detailed data…</div>
      </div>
      <div class="m-range-info" id="m-range-info" style="display:none;"></div>
    </div>
    <div class="m-sections" id="m-sections"></div>
  `;
  $("#m-range-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    DETAIL.range = b.dataset.range;
    renderModalFull();
  });
  $("#m-toggle-sp").onclick = () => { DETAIL.showSP = !DETAIL.showSP; renderModalFull(); };
  $("#m-toggle-sec").onclick = () => { DETAIL.showSector = !DETAIL.showSector; renderModalFull(); };
  $("#m-toggle-vol").onclick = () => { DETAIL.showVol = !DETAIL.showVol; renderModalFull(); };
}

function renderModalFull() {
  if (!DETAIL.data) return;
  // Update toolbar active states
  document.querySelectorAll("#m-range-tabs button").forEach(b => {
    b.classList.toggle("active", b.dataset.range === DETAIL.range);
  });
  $("#m-toggle-sp")?.classList.toggle("active", DETAIL.showSP);
  $("#m-toggle-sec")?.classList.toggle("active", DETAIL.showSector);
  $("#m-toggle-vol")?.classList.toggle("active", DETAIL.showVol);
  // Hide sector toggle if no sector ETF data
  if (!DETAIL.data.benchmark_sector || !DETAIL.data.benchmark_sector.length) {
    $("#m-toggle-sec").style.display = "none";
    DETAIL.showSector = false;
  } else {
    $("#m-toggle-sec").title = "Compare to " + (DETAIL.data.sector_etf || "sector ETF");
  }
  renderChart();
  renderSections();
}

/* ---- Chart rendering with crosshair + selection ---- */
function sliceHistory(pts, range) {
  if (!pts || !pts.length) return [];
  if (range === "MAX") return pts;
  const last = pts[pts.length - 1][0];
  const d = new Date(last);
  let cutoff;
  if (range === "YTD") cutoff = new Date(d.getFullYear(), 0, 1).getTime();
  else {
    const months = { "1M": 1, "3M": 3, "6M": 6, "1Y": 12, "5Y": 60 }[range] || 12;
    const c = new Date(d); c.setMonth(c.getMonth() - months); cutoff = c.getTime();
  }
  return pts.filter(p => p[0] >= cutoff);
}

function normalizedTo(pts, startVal) {
  if (!pts.length) return [];
  const base = pts[0][1];
  return pts.map(p => [p[0], (p[1] / base) * startVal]);
}

function renderChart() {
  const d = DETAIL.data;
  const wrap = $("#m-chart");
  const range = DETAIL.range;
  const stock = sliceHistory(d.history, range);
  if (stock.length < 2) {
    wrap.innerHTML = `<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);">No data for ${range}.</div>`;
    return;
  }

  // Slice volume + benchmarks aligned to stock's window
  const t0 = stock[0][0], t1 = stock[stock.length - 1][0];
  const vol = (d.volume || []).filter(p => p[0] >= t0 && p[0] <= t1);
  let spy = null, sec = null;
  if (DETAIL.showSP && d.benchmark_spy) {
    const s = d.benchmark_spy.filter(p => p[0] >= t0 && p[0] <= t1);
    if (s.length >= 2) spy = normalizedTo(s, stock[0][1]);
  }
  if (DETAIL.showSector && d.benchmark_sector) {
    const s = d.benchmark_sector.filter(p => p[0] >= t0 && p[0] <= t1);
    if (s.length >= 2) sec = normalizedTo(s, stock[0][1]);
  }

  // Geometry
  const W = wrap.clientWidth || 800;
  const H = 320;
  const padL = 48, padR = 10, padT = 12, padB = DETAIL.showVol && vol.length ? 60 : 22;
  const chartH = H - padT - padB;
  const xScale = (t) => padL + ((t - t0) / Math.max(1, t1 - t0)) * (W - padL - padR);

  // y range from stock + visible benchmarks
  let lo = Infinity, hi = -Infinity;
  for (const p of stock) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (spy) for (const p of spy) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (sec) for (const p of sec) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  const rng = (hi - lo) || 1;
  const padPct = 0.05;
  lo -= rng * padPct; hi += rng * padPct;
  const yScale = (v) => padT + (1 - (v - lo) / (hi - lo)) * chartH;

  // Volume scale
  let volMax = 1;
  if (vol.length) for (const p of vol) if (p[1] > volMax) volMax = p[1];
  const volH = 32;
  const volTop = H - padB + 18;
  const volScale = (v) => (v / volMax) * volH;

  // Main path
  const buildPath = (pts) => {
    let s = "";
    for (let i = 0; i < pts.length; i++) {
      const x = xScale(pts[i][0]).toFixed(1);
      const y = yScale(pts[i][1]).toFixed(1);
      s += (i === 0 ? "M" : "L") + x + "," + y + " ";
    }
    return s;
  };
  const stockPath = buildPath(stock);
  const stockUp = stock[stock.length - 1][1] >= stock[0][1];
  const css = getComputedStyle(document.documentElement);
  const posCol = css.getPropertyValue("--pos").trim();
  const negCol = css.getPropertyValue("--neg").trim();
  const posRgb = css.getPropertyValue("--pos-rgb").trim();
  const negRgb = css.getPropertyValue("--neg-rgb").trim();
  const stroke = stockUp ? posCol : negCol;
  const fillRgb = stockUp ? posRgb : negRgb;
  const baseY = yScale(lo);
  const area = stockPath + ` L ${xScale(t1).toFixed(1)},${baseY.toFixed(1)} L ${xScale(t0).toFixed(1)},${baseY.toFixed(1)} Z`;

  // Y-axis ticks (4)
  const ticks = [];
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * (i / 4);
    ticks.push({ v, y: yScale(v) });
  }
  // X-axis ticks (4-6 evenly spaced)
  const xTicks = [];
  const N_XT = 5;
  for (let i = 0; i <= N_XT; i++) {
    const t = t0 + (t1 - t0) * (i / N_XT);
    xTicks.push({ t, x: xScale(t) });
  }
  const fmtTickDate = (ts) => {
    const dt = new Date(ts);
    if (range === "1M" || range === "3M") return dt.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    if (range === "6M" || range === "YTD" || range === "1Y") return dt.toLocaleDateString(undefined, { month: "short", year: "2-digit" });
    return dt.toLocaleDateString(undefined, { year: "numeric" });
  };
  const fmtTickVal = (v) => {
    if (v >= 1000) return v.toFixed(0);
    if (v >= 100) return v.toFixed(1);
    return v.toFixed(2);
  };

  // Volume bars
  let volSvg = "";
  if (DETAIL.showVol && vol.length) {
    const bw = Math.max(1, (W - padL - padR) / Math.max(vol.length, 1) - 0.5);
    volSvg = vol.map(p => {
      const x = xScale(p[0]) - bw/2;
      const h = volScale(p[1]);
      return `<rect x="${x.toFixed(1)}" y="${(volTop + volH - h).toFixed(1)}" width="${bw.toFixed(2)}" height="${h.toFixed(1)}" fill="rgba(${fillRgb},0.35)"/>`;
    }).join("");
  }

  const spyPath = spy ? buildPath(spy) : null;
  const secPath = sec ? buildPath(sec) : null;

  wrap.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" id="m-svg">
      <defs>
        <linearGradient id="g-area" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="rgba(${fillRgb},0.28)"/>
          <stop offset="100%" stop-color="rgba(${fillRgb},0.02)"/>
        </linearGradient>
      </defs>
      ${ticks.map(t => `<line x1="${padL}" y1="${t.y.toFixed(1)}" x2="${W-padR}" y2="${t.y.toFixed(1)}" stroke="var(--border)" stroke-width="0.5" stroke-dasharray="2 3"/>`).join("")}
      ${ticks.map(t => `<text x="${padL-6}" y="${t.y+3}" font-size="10" fill="var(--muted)" text-anchor="end">${fmtTickVal(t.v)}</text>`).join("")}
      ${xTicks.map(t => `<text x="${t.x}" y="${H-padB+12}" font-size="10" fill="var(--muted)" text-anchor="middle">${fmtTickDate(t.t)}</text>`).join("")}
      <path d="${area}" fill="url(#g-area)"/>
      ${spyPath ? `<path d="${spyPath}" fill="none" stroke="#8b5cf6" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
      ${secPath ? `<path d="${secPath}" fill="none" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
      <path d="${stockPath}" fill="none" stroke="${stroke}" stroke-width="1.8"/>
      ${volSvg}
      <rect class="sel-rect" id="m-sel" x="0" y="0" width="0" height="${chartH}" style="display:none"/>
      <line class="crosshair-line" id="m-cross-v" x1="0" y1="${padT}" x2="0" y2="${padT+chartH}"/>
      <line class="crosshair-line" id="m-cross-h" x1="${padL}" y1="0" x2="${W-padR}" y2="0"/>
      <circle class="crosshair-dot" id="m-dot" r="4" cx="0" cy="0"/>
      ${spyPath ? `<circle class="crosshair-dot sp" id="m-dot-sp" r="3.5" cx="0" cy="0"/>` : ""}
      ${secPath ? `<circle class="crosshair-dot sec" id="m-dot-sec" r="3.5" cx="0" cy="0"/>` : ""}
      <rect id="m-overlay" x="${padL}" y="${padT}" width="${W-padL-padR}" height="${chartH}" fill="transparent" style="cursor:crosshair"/>
    </svg>
    <div class="m-tooltip" id="m-tt"></div>
  `;

  // Save geometry + data for interaction
  DETAIL.geom = { W, H, padL, padR, padT, padB, chartH, xScale, yScale, stock, spy, sec, t0, t1, fillRgb };

  // Range return summary (when not dragging)
  const sPct = ((stock[stock.length-1][1] / stock[0][1] - 1) * 100);
  const sCls = sPct >= 0 ? "pos" : "neg";
  let parts = [`<span><b class="${sCls}">${(sPct>=0?"+":"")+sPct.toFixed(2)}%</b> · ${range} (${DETAIL.data.symbol})</span>`];
  if (spy) {
    const p = (spy[spy.length-1][1] / spy[0][1] - 1) * 100;
    parts.push(`<span><b class="${p>=0?"pos":"neg"}">${(p>=0?"+":"")+p.toFixed(2)}%</b> · S&amp;P 500</span>`);
  }
  if (sec) {
    const p = (sec[sec.length-1][1] / sec[0][1] - 1) * 100;
    parts.push(`<span><b class="${p>=0?"pos":"neg"}">${(p>=0?"+":"")+p.toFixed(2)}%</b> · ${DETAIL.data.sector_etf || "Sector"}</span>`);
  }
  parts.push(`<span style="margin-left:auto">${new Date(t0).toLocaleDateString()} → ${new Date(t1).toLocaleDateString()}</span>`);
  const info = $("#m-range-info");
  info.innerHTML = parts.join("");
  info.style.display = "flex";
  info.dataset.default = "1";

  attachChartInteraction();
}

function attachChartInteraction() {
  const svg = $("#m-svg");
  const overlay = $("#m-overlay");
  const tt = $("#m-tt");
  const cv = $("#m-cross-v"), ch = $("#m-cross-h"), dot = $("#m-dot");
  const dotSp = $("#m-dot-sp"), dotSec = $("#m-dot-sec");
  const sel = $("#m-sel");
  const wrap = $("#m-chart");
  const info = $("#m-range-info");
  const g = DETAIL.geom;

  function pxToData(px) {
    // px is in svg viewBox units → matches our coords
    const tx = g.t0 + (px - g.padL) / (g.W - g.padL - g.padR) * (g.t1 - g.t0);
    return tx;
  }
  function nearestIdx(pts, t) {
    if (!pts.length) return -1;
    let lo = 0, hi = pts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (pts[mid][0] < t) lo = mid + 1; else hi = mid;
    }
    if (lo > 0 && Math.abs(pts[lo - 1][0] - t) < Math.abs(pts[lo][0] - t)) return lo - 1;
    return lo;
  }
  function clientToSvgX(clientX) {
    const r = svg.getBoundingClientRect();
    return (clientX - r.left) * (g.W / r.width);
  }

  let dragging = false, dragStartT = null;

  overlay.addEventListener("mousemove", (e) => {
    const svgX = clientToSvgX(e.clientX);
    const t = pxToData(Math.max(g.padL, Math.min(g.W - g.padR, svgX)));
    const i = nearestIdx(g.stock, t);
    if (i < 0) return;
    const sp = g.stock[i];
    const x = g.xScale(sp[0]);
    const y = g.yScale(sp[1]);
    cv.setAttribute("x1", x); cv.setAttribute("x2", x); cv.style.opacity = 1;
    ch.setAttribute("y1", y); ch.setAttribute("y2", y); ch.style.opacity = 1;
    dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.style.opacity = 1;
    let extra = "";
    if (dotSp && g.spy) {
      const j = nearestIdx(g.spy, sp[0]);
      if (j >= 0) {
        const px = g.xScale(g.spy[j][0]); const py = g.yScale(g.spy[j][1]);
        dotSp.setAttribute("cx", px); dotSp.setAttribute("cy", py); dotSp.style.opacity = 1;
        const pct = (g.spy[j][1] / g.spy[0][1] - 1) * 100;
        extra += `<div class="tt-row"><span class="tt-label"><span class="dot sp" style="width:8px;height:8px;background:#8b5cf6;border-radius:50%;display:inline-block"></span>S&amp;P</span><b class="${pct>=0?'pos':'neg'}" style="color:${pct>=0?'var(--pos)':'var(--neg)'}">${(pct>=0?'+':'')+pct.toFixed(2)}%</b></div>`;
      }
    }
    if (dotSec && g.sec) {
      const j = nearestIdx(g.sec, sp[0]);
      if (j >= 0) {
        const px = g.xScale(g.sec[j][0]); const py = g.yScale(g.sec[j][1]);
        dotSec.setAttribute("cx", px); dotSec.setAttribute("cy", py); dotSec.style.opacity = 1;
        const pct = (g.sec[j][1] / g.sec[0][1] - 1) * 100;
        extra += `<div class="tt-row"><span class="tt-label"><span style="width:8px;height:8px;background:#f59e0b;border-radius:50%;display:inline-block"></span>${DETAIL.data.sector_etf||'Sector'}</span><b style="color:${pct>=0?'var(--pos)':'var(--neg)'}">${(pct>=0?'+':'')+pct.toFixed(2)}%</b></div>`;
      }
    }
    const stockPct = (sp[1] / g.stock[0][1] - 1) * 100;
    const dt = new Date(sp[0]);
    tt.innerHTML = `
      <div class="tt-date">${dt.toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric'})}</div>
      <div class="tt-row"><span class="tt-label">Price</span><b>${fmtMoney(sp[1])}</b></div>
      <div class="tt-row"><span class="tt-label">${DETAIL.data.symbol}</span><b style="color:${stockPct>=0?'var(--pos)':'var(--neg)'}">${(stockPct>=0?'+':'')+stockPct.toFixed(2)}%</b></div>
      ${extra}
    `;
    tt.classList.add("show");
    // Position tooltip near cursor
    const wrapRect = wrap.getBoundingClientRect();
    const lx = e.clientX - wrapRect.left + 12;
    const ly = e.clientY - wrapRect.top - 8;
    const ttRect = tt.getBoundingClientRect();
    const maxX = wrap.clientWidth - ttRect.width - 6;
    tt.style.left = Math.min(lx, Math.max(6, maxX)) + "px";
    tt.style.top = Math.max(6, ly) + "px";

    if (dragging && dragStartT != null) {
      const a = Math.min(dragStartT, sp[0]), b = Math.max(dragStartT, sp[0]);
      const ax = g.xScale(a), bx = g.xScale(b);
      sel.style.display = "";
      sel.setAttribute("x", ax);
      sel.setAttribute("y", g.padT);
      sel.setAttribute("width", Math.max(1, bx - ax));
      // Update range-info to show drag return
      const iA = nearestIdx(g.stock, a), iB = nearestIdx(g.stock, b);
      if (iA >= 0 && iB >= 0 && iA !== iB) {
        const va = g.stock[iA][1], vb = g.stock[iB][1];
        const pct = (vb/va - 1) * 100;
        const cls = pct >= 0 ? "pos" : "neg";
        let drag = `<span><b class="${cls}">${(pct>=0?"+":"")+pct.toFixed(2)}%</b> · selection</span>`;
        if (g.spy) {
          const jA = nearestIdx(g.spy, a), jB = nearestIdx(g.spy, b);
          if (jA >= 0 && jB >= 0 && jA !== jB) {
            const p = (g.spy[jB][1]/g.spy[jA][1] - 1) * 100;
            drag += `<span><b class="${p>=0?'pos':'neg'}">${(p>=0?'+':'')+p.toFixed(2)}%</b> · S&amp;P</span>`;
          }
        }
        drag += `<span style="margin-left:auto">${new Date(g.stock[iA][0]).toLocaleDateString()} → ${new Date(g.stock[iB][0]).toLocaleDateString()}</span>`;
        info.innerHTML = drag;
      }
    }
  });
  overlay.addEventListener("mouseleave", () => {
    cv.style.opacity = 0; ch.style.opacity = 0; dot.style.opacity = 0;
    if (dotSp) dotSp.style.opacity = 0;
    if (dotSec) dotSec.style.opacity = 0;
    tt.classList.remove("show");
  });
  overlay.addEventListener("mousedown", (e) => {
    dragging = true;
    const svgX = clientToSvgX(e.clientX);
    dragStartT = pxToData(Math.max(g.padL, Math.min(g.W - g.padR, svgX)));
    sel.style.display = "";
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false; dragStartT = null;
    // Keep selection visible briefly, then revert range-info to defaults
    setTimeout(() => {
      sel.style.display = "none";
      // Restore range summary
      renderChart();
    }, 1800);
  });
}

/* ---- Information sections ---- */
function fmtPctRaw(v, digits) {
  if (v == null || !isFinite(v)) return "—";
  const s = v >= 0 ? "+" : "";
  return s + Number(v).toFixed(digits == null ? 2 : digits) + "%";
}
function pctCell(v) {
  if (v == null || !isFinite(v)) return `<td class="na">—</td>`;
  const cls = v >= 0 ? "pos" : "neg";
  return `<td class="${cls}">${(v>=0?"+":"")+v.toFixed(2)}%</td>`;
}
function renderSections() {
  const d = DETAIL.data;
  const sec = $("#m-sections");
  const fcfYield = (d.free_cashflow != null && d.market_cap != null && isFinite(d.free_cashflow) && isFinite(d.market_cap) && d.market_cap !== 0)
    ? d.free_cashflow / d.market_cap
    : null;

  // ---- Snapshot
  const snap = [
    ["Volume", fmtCompactNum(d.day_volume)],
    ["Avg Volume", fmtCompactNum(d.avg_volume)],
    ["52W High", fmtMoney(d.w52_high)],
    ["52W Low", fmtMoney(d.w52_low)],
    ["ATH", fmtMoney(d.ath)],
    ["Market Cap", fmtCompactMoney(d.market_cap)],
    ["Shares Out", fmtCompactNum(d.shares)],
    ["Beta", fmt2(d.beta)],
  ];

  // ---- Valuation
  const val = [
    ["Revenue (TTM)", fmtCompactMoney(d.total_revenue)],
    ["Revenue Growth", fmtPctFrac(d.revenue_growth)],
    ["Free Cash Flow", fmtCompactMoney(d.free_cashflow)],
    ["FCF Yield", fmtPctFrac(fcfYield)],
    ["Fwd P/E", fmt2(d.forward_pe)],
    ["P/E (TTM)", fmt2(d.pe)],
    ["EV/EBITDA", fmt2(d.ev_ebitda)],
    ["EV/Revenue", fmt2(d.ev_revenue)],
    ["Operating Mgn", fmtPctFrac(d.operating_margin)],
    ["Gross Margin", fmtPctFrac(d.gross_margin)],
    ["Profit Margin", fmtPctFrac(d.profit_margin)],
    ["ROE", fmtPctFrac(d.roe)],
    ["D/E", fmt2(d.debt_equity)],
  ];

  // ---- Performance table
  const perf = d.performance || {};
  const labels = [["1d","1 Day"],["1w","1 Week"],["1m","1 Month"],["3m","3 Months"],["6m","6 Months"],["ytd","YTD"],["1y","1 Year"],["5y","5 Years"]];
  const sectorETF = d.sector_etf || "Sector";
  const perfRows = labels.map(([k,lbl]) => {
    const row = perf[k] || {};
    const s = row.stock, b = row.spy, c = row.sector;
    const dvs = (s != null && b != null) ? (s - b) : null;
    return `<tr>
      <td>${lbl}</td>
      ${pctCell(s)}${pctCell(b)}${pctCell(c)}
      ${dvs == null ? `<td class="na">—</td>` : `<td class="${dvs>=0?'pos':'neg'}">${(dvs>=0?'+':'')+dvs.toFixed(2)}</td>`}
    </tr>`;
  }).join("");

  const betaC = d.beta_computed, corrC = d.correlation_spy;
  const betaSec = d.beta_sector, corrSec = d.correlation_sector;

  // ---- Analyst
  let analystHtml = "";
  if (d.recommendation_mean != null || d.target_mean != null || (d.recommendations_trend && d.recommendations_trend.length)) {
    const recMean = d.recommendation_mean; // 1=Strong Buy ... 5=Strong Sell
    const recKey = (d.recommendation_key || "").replace("_", " ");
    const keyCls = recKey.includes("buy") ? "buy" : recKey.includes("sell") ? "sell" : "hold";
    // Needle position: 1 → left, 5 → right
    const needlePct = recMean != null ? Math.max(0, Math.min(100, ((recMean - 1) / 4) * 100)) : null;
    // Target bar: position current price between low and high
    let tbHtml = "";
    if (d.target_low != null && d.target_high != null && d.price != null) {
      const lo = Math.min(d.target_low, d.price), hi = Math.max(d.target_high, d.price);
      const pct = v => 6 + ((v - lo) / Math.max(0.0001, (hi - lo))) * 88;
      const upside = d.target_mean != null ? ((d.target_mean / d.price - 1) * 100) : null;
      tbHtml = `
        <div class="m-target-bar">
          <div class="tb-track"></div>
          <span class="tb-low">${fmtMoney(d.target_low)}</span>
          <div class="tb-mark current" style="left:${pct(d.price).toFixed(1)}%" title="Current"></div>
          ${d.target_mean != null ? `<div class="tb-mark target" style="left:${pct(d.target_mean).toFixed(1)}%" title="Mean target"></div>` : ""}
          <span class="tb-high">${fmtMoney(d.target_high)}</span>
        </div>
        <div class="m-target-labels">
          <span>Current <b>${fmtMoney(d.price)}</b></span>
          <span>Mean target <b>${fmtMoney(d.target_mean)}</b>
            ${upside != null ? `<span class="upside ${upside>=0?'pos':'neg'}">(${(upside>=0?'+':'')+upside.toFixed(1)}%)</span>` : ""}
          </span>
        </div>
      `;
    }
    // Latest recommendation distribution
    let recBars = "";
    if (d.recommendations_trend && d.recommendations_trend.length) {
      const r = d.recommendations_trend[0];
      const tot = (r.strongBuy||0) + (r.buy||0) + (r.hold||0) + (r.sell||0) + (r.strongSell||0);
      if (tot > 0) {
        const pct = (n) => ((n||0) / tot * 100).toFixed(1) + "%";
        recBars = `
          <div class="m-rec-bars">
            <div class="sb" style="flex:${r.strongBuy||0}" title="Strong Buy: ${r.strongBuy}">${r.strongBuy||""}</div>
            <div class="b"  style="flex:${r.buy||0}" title="Buy: ${r.buy}">${r.buy||""}</div>
            <div class="h"  style="flex:${r.hold||0}" title="Hold: ${r.hold}">${r.hold||""}</div>
            <div class="s"  style="flex:${r.sell||0}" title="Sell: ${r.sell}">${r.sell||""}</div>
            <div class="ss" style="flex:${r.strongSell||0}" title="Strong Sell: ${r.strongSell}">${r.strongSell||""}</div>
          </div>
          <div class="m-rec-legend">
            <span><i style="background:#15803d"></i>Strong Buy</span>
            <span><i style="background:#22c55e"></i>Buy</span>
            <span><i style="background:#eab308"></i>Hold</span>
            <span><i style="background:#f97316"></i>Sell</span>
            <span><i style="background:#dc2626"></i>Strong Sell</span>
          </div>`;
      }
    }
    analystHtml = `
      <div class="m-sec full">
        <h3>Analyst Coverage</h3>
        <div class="m-analyst-grid">
          <div>
            <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
              ${recKey ? `<span class="m-rec-key ${keyCls}">${escapeHtml(recKey)}</span>` : ""}
              ${recMean != null ? `<span style="font-size:12px;color:var(--muted);">Mean rating <b style="color:var(--text)">${recMean.toFixed(2)}</b> / 5</span>` : ""}
              ${d.num_analysts ? `<span style="font-size:12px;color:var(--muted);">from <b style="color:var(--text)">${d.num_analysts}</b> analysts</span>` : ""}
            </div>
            ${needlePct != null ? `
              <div class="m-gauge"><div class="needle" style="left:${needlePct.toFixed(1)}%"></div></div>
              <div class="m-gauge-labels"><span>Strong Buy</span><span>Buy</span><span>Hold</span><span>Sell</span><span>Strong Sell</span></div>
            ` : ""}
            ${recBars}
          </div>
          <div>
            ${tbHtml || `<div class="m-summary">No price targets available.</div>`}
          </div>
        </div>
      </div>`;
  }

  // ---- Fundamentals
  const funda = [
    ["Revenue (TTM)", fmtCompactMoney(d.total_revenue)],
    ["Revenue Growth", fmtPctFrac(d.revenue_growth)],
    ["Earnings Growth", fmtPctFrac(d.earnings_growth)],
    ["Free Cash Flow", fmtCompactMoney(d.free_cashflow)],
    ["ROA", fmtPctFrac(d.roa)],
    ["Current Ratio", fmt2(d.current_ratio)],
  ];

  // ---- Dividend (only if any data)
  let divHtml = "";
  if (d.dividend_yield != null || d.dividend_rate != null || d.ex_div_date) {
    const divItems = [
      ["Yield", fmtPctDirect(d.dividend_yield)],
      ["Rate", fmtMoney(d.dividend_rate)],
      ["Payout", fmtPctFrac(d.payout_ratio)],
      ["Ex-Div Date", d.ex_div_date || "—"],
    ];
    divHtml = `
      <div class="m-sec">
        <h3>Dividend</h3>
        <div class="m-kv">${divItems.map(([k,v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
      </div>`;
  }

  // ---- News
  let newsHtml = "";
  if (d.news && d.news.length) {
    newsHtml = `
      <div class="m-sec full">
        <h3>Latest News</h3>
        <ul class="m-news-list">
          ${d.news.map(n => `
            <li>
              ${n.link ? `<a href="${escapeHtml(n.link)}" target="_blank" rel="noopener">${escapeHtml(n.title)}</a>` : `<span>${escapeHtml(n.title)}</span>`}
              <div class="src">${escapeHtml(n.publisher || "")}${n.time ? " · " + relTime(n.time) : ""}</div>
            </li>`).join("")}
        </ul>
      </div>`;
  }

  // ---- About
  let aboutHtml = "";
  if (d.summary) {
    aboutHtml = `
      <div class="m-sec full">
        <h3>About${d.next_earnings ? ` · Next earnings: <b style="color:var(--text); font-weight:700">${d.next_earnings}</b>` : ""}</h3>
        <div class="m-summary collapsed" id="m-summary">${escapeHtml(d.summary)}</div>
        <button class="m-summary-toggle" onclick="this.previousElementSibling.classList.toggle('collapsed'); this.textContent = this.previousElementSibling.classList.contains('collapsed') ? 'Show more' : 'Show less';">Show more</button>
      </div>`;
  } else if (d.next_earnings) {
    aboutHtml = `<div class="m-sec full"><h3>Upcoming</h3><div class="m-summary">Next earnings: <b style="color:var(--text)">${d.next_earnings}</b></div></div>`;
  }

  sec.innerHTML = `
    <div class="m-sec">
      <h3>Snapshot</h3>
      <div class="m-kv">${snap.map(([k,v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
    </div>
    <div class="m-sec">
      <h3>Valuation &amp; Profitability</h3>
      <div class="m-kv">${val.map(([k,v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
    </div>
    <div class="m-sec full">
      <h3>Performance vs Benchmarks
        ${betaC != null ? ` · β(SPY) <b style="color:var(--text); font-weight:700">${betaC.toFixed(2)}</b>` : ""}
        ${corrC != null ? ` · ρ(SPY) <b style="color:var(--text); font-weight:700">${corrC.toFixed(2)}</b>` : ""}
        ${betaSec != null ? ` · β(${sectorETF}) <b style="color:var(--text); font-weight:700">${betaSec.toFixed(2)}</b>` : ""}
      </h3>
      <table class="m-perf">
        <thead><tr>
          <th>Window</th>
          <th><span class="legend" style="background:${getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()}"></span>${d.symbol}</th>
          <th><span class="legend" style="background:#8b5cf6"></span>S&amp;P 500</th>
          <th><span class="legend" style="background:#f59e0b"></span>${sectorETF}</th>
          <th>vs SPY</th>
        </tr></thead>
        <tbody>${perfRows}</tbody>
      </table>
    </div>
    ${analystHtml}
    <div class="m-sec">
      <h3>Fundamentals</h3>
      <div class="m-kv">${funda.map(([k,v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
    </div>
    ${divHtml || `<div class="m-sec"><h3>Profile</h3><div class="m-kv">
      <div><div class="k">Country</div><div class="v">${escapeHtml(d.country || "—")}</div></div>
      <div><div class="k">Employees</div><div class="v">${fmtCompactNum(d.employees)}</div></div>
      <div><div class="k">Exchange</div><div class="v">${escapeHtml(d.exchange || "—")}</div></div>
      <div><div class="k">Currency</div><div class="v">${escapeHtml(d.currency || "—")}</div></div>
    </div></div>`}
    ${newsHtml}
    ${aboutHtml}
  `;
}

function fmtCompactNum(v) {
  if (v == null || !isFinite(v)) return "—";
  v = Number(v);
  const abs = Math.abs(v);
  if (abs >= 1e12) return (v/1e12).toFixed(2) + "T";
  if (abs >= 1e9)  return (v/1e9).toFixed(2) + "B";
  if (abs >= 1e6)  return (v/1e6).toFixed(2) + "M";
  if (abs >= 1e3)  return (v/1e3).toFixed(2) + "K";
  return v.toFixed(0);
}
function fmtPctFrac(v) {
  if (v == null || !isFinite(v)) return "—";
  return (Number(v) * 100).toFixed(2) + "%";
}
function relTime(iso) {
  try {
    const t = new Date(iso).getTime();
    const diff = (Date.now() - t) / 1000;
    if (diff < 3600) return Math.max(1, Math.floor(diff/60)) + "m ago";
    if (diff < 86400) return Math.floor(diff/3600) + "h ago";
    if (diff < 86400*30) return Math.floor(diff/86400) + "d ago";
    return new Date(iso).toLocaleDateString();
  } catch { return ""; }
}

/* ===========================================================================
 * Build / fetch (NDJSON streaming with progress)
 * --------------------------------------------------------------------------- */
function showProgress(pct) {
  $("#progress-wrap").classList.add("show");
  $("#progress-bar").style.width = clamp(pct, 0, 100) + "%";
}
function hideProgress() {
  setTimeout(() => {
    $("#progress-wrap").classList.remove("show");
    $("#progress-bar").style.width = "0%";
  }, 400);
}

/* ===========================================================================
 * Portfolio state — per-portfolio views, tabs, and analytics
 * --------------------------------------------------------------------------- */
const AD_HOC_KEY = "__current__";
let STATE = {
  activeView: null,        // current view name (or AD_HOC_KEY for ad-hoc input, or null = none yet)
  mode: "cap",             // "equal" | "cap" | "custom"
  customWeights: null,     // {symbol: fraction}, set after user applies the popup
  period: "1Y",
  showSpy: true,
  showSec: false,
  showDd: true,
  analytics: null,
  analyticsLoading: false,
  analyticsByMode: {},   // {"<mode>|<period>": result}
};
let WATCHLISTS = {};        // name -> entries-string (saved watchlists)
let VIEWS = {};             // name -> {entries, saved_at, row_count, stale}
let LAST_VIEW = null;

function viewIsAdhoc(name) { return !name || name === AD_HOC_KEY; }
function entriesArr(raw) { return String(raw || "").split(/[\n,]+/).map(s => s.trim()).filter(Boolean); }

async function build(opts) {
  opts = opts || {};
  const raw = $("#tickers").value.trim();
  if (!raw) { toast("Enter at least one ticker or company name."); return; }
  const entries = entriesArr(raw);
  $("#build").disabled = true; $("#refresh").disabled = true;
  $("#status").textContent = `Fetching ${entries.length} symbol${entries.length>1?"s":""}…`;
  showProgress(2);
  DATA = [];
  render();

  let total = entries.length;
  let done = 0;
  try {
    const r = await fetch("/api/quotes-stream", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({entries}),
    });
    if (!r.ok || !r.body) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || ("HTTP " + r.status));
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done: rDone, value } = await reader.read();
      if (rDone) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        let msg;
        try { msg = JSON.parse(line); } catch { continue; }
        if (msg.type === "start") {
          total = msg.total || total;
          showProgress(3);
        } else if (msg.type === "row") {
          done = msg.done || (done + 1);
          DATA.push(msg.row);
          render();
          showProgress((done / Math.max(1, total)) * 100);
          $("#status").textContent = `Loaded ${done}/${total}…`;
        } else if (msg.type === "done") {
          showProgress(100);
        }
      }
    }
    $("#status").textContent = `Loaded ${DATA.length} symbol${DATA.length>1?"s":""} · updated ${new Date().toLocaleTimeString()}`;
    // Persist this build as a view under the active tab name (or ad-hoc).
    const targetName = STATE.activeView || AD_HOC_KEY;
    await persistView(targetName, raw, DATA);
    STATE.customWeights = null;  // new build — drop stale custom weights
    if (STATE.mode === "custom") STATE.mode = "cap";
    STATE.analyticsByMode = {};  // invalidate cache for new data
    requestAnalytics();
  } catch (e) {
    toast("Error: " + e.message);
    $("#status").textContent = "Error.";
  } finally {
    hideProgress();
    $("#build").disabled = false; $("#refresh").disabled = false;
  }
}

async function persistView(name, entries, rows) {
  const targetName = name || AD_HOC_KEY;
  try {
    const r = await fetch(`/api/views/${encodeURIComponent(targetName)}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({entries, rows, set_last: true}),
    });
    if (r.ok) {
      const d = await r.json();
      VIEWS[targetName] = {
        entries: d.view.entries,
        saved_at: d.view.saved_at,
        row_count: (d.view.rows || []).length,
        stale: false,
      };
      LAST_VIEW = targetName;
      STATE.activeView = targetName;
      renderTabs();
      renderEditorMeta();
    }
  } catch (e) { /* best-effort */ }
}

async function loadAllAtStartup() {
  await Promise.all([loadWatchlists(), loadViews()]);
  renderTabs();
  // Restore the last open view if any.
  if (LAST_VIEW && VIEWS[LAST_VIEW]) {
    await activateTab(LAST_VIEW, {silent: true});
  } else {
    // No prior view — show the editor in ad-hoc mode but keep the panel closed.
    STATE.activeView = null;
    renderEditorMeta();
  }
}

async function loadViews() {
  try {
    const r = await fetch("/api/views");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    VIEWS = d.views || {};
    LAST_VIEW = d.last_view || null;
  } catch (e) {
    VIEWS = {}; LAST_VIEW = null;
  }
}

async function activateTab(name, opts) {
  opts = opts || {};
  STATE.activeView = name;
  STATE.customWeights = null;
  STATE.analyticsByMode = {};
  if (STATE.mode === "custom") STATE.mode = "cap";
  renderTabs();
  renderEditorMeta();
  // Set the textarea to the watchlist entries (or stored view entries if ad-hoc).
  const view = VIEWS[name];
  const wlEntries = WATCHLISTS[name];
  $("#tickers").value = wlEntries != null ? wlEntries : (view ? view.entries : "");
  // Try to load cached rows for the view.
  if (view && view.row_count > 0) {
    try {
      const r = await fetch(`/api/views/${encodeURIComponent(name)}`);
      const d = await r.json();
      const rows = (d.view && Array.isArray(d.view.rows)) ? d.view.rows : [];
      if (rows.length) {
        DATA = rows;
        render();
        const savedAt = view.saved_at ? relTime(view.saved_at) : "previously";
        $("#status").textContent = `${viewLabel(name)} · cached ${savedAt}${view.stale ? " · stale" : ""}`;
        if (view.stale) {
          // Auto-refresh per user policy.
          if (!opts.silent) toast(`Constituents changed — refreshing ${viewLabel(name)}…`);
          await build({keepPanelOpen: true});
        } else {
          requestAnalytics();
        }
        await fetch("/api/last-view", {method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({name})});
        return;
      }
    } catch (e) { /* fall through */ }
  }
  // No cached rows yet — clear the table, prompt user.
  DATA = []; render();
  $("#pf-analytics-body").innerHTML = `<div class="pf-empty">Press <b>Build Dashboard</b> to load this portfolio.</div>`;
  $("#status").textContent = viewLabel(name);
  await fetch("/api/last-view", {method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({name})});
}

function viewLabel(name) {
  if (!name) return "Ad-hoc";
  if (name === AD_HOC_KEY) return "Ad-hoc (unsaved)";
  return name;
}

function renderTabs() {
  const wrap = $("#pf-tabs"); wrap.innerHTML = "";
  // Tab order: saved watchlists alphabetically, then ad-hoc (if present), then "+ New".
  const wlNames = Object.keys(WATCHLISTS).sort((a, b) => a.localeCompare(b));
  const hasAdhoc = !!VIEWS[AD_HOC_KEY];
  const tabNames = [...wlNames];
  if (hasAdhoc) tabNames.push(AD_HOC_KEY);

  for (const name of tabNames) {
    const tab = document.createElement("div");
    tab.className = "pf-tab" + (STATE.activeView === name ? " active" : "") + (name === AD_HOC_KEY ? " pf-tab-newadhoc" : "");
    tab.setAttribute("role", "tab");
    tab.setAttribute("data-name", name);

    const view = VIEWS[name];
    const staleDot = view && view.stale ? `<span class="pf-tab-stale" title="Constituents changed — refresh to update"></span>` : "";
    const closeBtn = `<span class="pf-tab-close" title="Delete">✕</span>`;
    tab.innerHTML = `${staleDot}<span class="pf-tab-label">${escapeHtml(viewLabel(name))}</span>${closeBtn}`;
    tab.addEventListener("click", (e) => {
      if (e.target.closest(".pf-tab-close")) return;
      activateTab(name);
    });
    tab.querySelector(".pf-tab-close").addEventListener("click", async (e) => {
      e.stopPropagation();
      await deletePortfolio(name);
    });
    wrap.appendChild(tab);
  }

  // "+ New" tab — always present.
  const add = document.createElement("button");
  add.className = "pf-tab-add";
  add.textContent = "＋ New portfolio";
  add.addEventListener("click", () => createNewTab());
  wrap.appendChild(add);
}

function renderEditorMeta() {
  const name = STATE.activeView;
  const meta = $("#pf-editor-meta");
  if (!name) { meta.innerHTML = ""; return; }
  const view = VIEWS[name];
  const parts = [];
  if (name === AD_HOC_KEY) parts.push("Unsaved portfolio");
  else parts.push(`Portfolio: <b>${escapeHtml(name)}</b>`);
  if (view) {
    parts.push(`${view.row_count} rows`);
    if (view.saved_at) parts.push(`cached ${relTime(view.saved_at)}`);
    if (view.stale) parts.push(`<span class="stale">stale — will auto-refresh</span>`);
  }
  meta.innerHTML = parts.join(" · ");
}

async function createNewTab() {
  // Open the panel in fresh ad-hoc state.
  STATE.activeView = AD_HOC_KEY;
  STATE.customWeights = null;
  $("#tickers").value = "";
  DATA = []; render();
  $("#input-panel").classList.remove("hidden");
  $("#pf-analytics-body").innerHTML = `<div class="pf-empty">Paste tickers and press <b>Build Dashboard</b>.</div>`;
  renderTabs(); renderEditorMeta();
  $("#tickers").focus();
}

function showConfirm({title, body, okLabel}) {
  return new Promise((resolve) => {
    const bg = $("#confirm-bg");
    $("#confirm-title").textContent = title || "Are you sure?";
    $("#confirm-body").innerHTML = body || "";
    $("#confirm-ok").textContent = okLabel || "Delete";
    bg.classList.add("show");
    const cleanup = (val) => {
      bg.classList.remove("show");
      $("#confirm-ok").onclick = null;
      $("#confirm-cancel").onclick = null;
      bg.onclick = null;
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onKey = (e) => {
      if (e.key === "Escape") cleanup(false);
      else if (e.key === "Enter") cleanup(true);
    };
    $("#confirm-ok").onclick = () => cleanup(true);
    $("#confirm-cancel").onclick = () => cleanup(false);
    bg.onclick = (e) => { if (e.target.id === "confirm-bg") cleanup(false); };
    document.addEventListener("keydown", onKey);
    setTimeout(() => $("#confirm-ok").focus(), 30);
  });
}

async function deletePortfolio(name) {
  if (!name) return;
  if (name === AD_HOC_KEY) {
    const ok = await showConfirm({
      title: "Discard unsaved portfolio?",
      body: "Your unsaved ad-hoc portfolio will be removed.",
      okLabel: "Discard",
    });
    if (!ok) return;
    try { await fetch(`/api/views/${encodeURIComponent(name)}`, {method: "DELETE"}); } catch(e) {}
    delete VIEWS[name];
    if (STATE.activeView === name) STATE.activeView = null;
    renderTabs(); renderEditorMeta();
    return;
  }
  const ok = await showConfirm({
    title: "Delete portfolio?",
    body: `<b>${escapeHtml(name)}</b> and its cached results will be removed. This can't be undone.`,
    okLabel: "Delete",
  });
  if (!ok) return;
  try {
    const r = await fetch(`/api/watchlists?name=${encodeURIComponent(name)}`, {method: "DELETE"});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Delete failed.");
    WATCHLISTS = d.watchlists || {};
    delete VIEWS[name];
    if (STATE.activeView === name) {
      STATE.activeView = null;
      DATA = []; render();
    }
    renderTabs(); renderEditorMeta();
    toast(`Deleted "${name}".`);
  } catch (err) {
    toast(err.message || "Delete failed.");
  }
}

async function saveAsNewWatchlist() {
  const raw = $("#tickers").value.trim();
  if (!raw) return toast("Enter tickers first.");
  const name = prompt("Save this portfolio as…", STATE.activeView && STATE.activeView !== AD_HOC_KEY ? STATE.activeView : "");
  if (!name || !name.trim()) return;
  const clean = name.trim();
  try {
    const r = await fetch("/api/watchlists", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: clean, entries: raw}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Save failed.");
    WATCHLISTS = d.watchlists || {};
    // If we were on ad-hoc and have data, move that view's rows under the new name.
    if (STATE.activeView === AD_HOC_KEY && DATA.length) {
      await persistView(clean, raw, DATA);
      try { await fetch(`/api/views/${encodeURIComponent(AD_HOC_KEY)}`, {method: "DELETE"}); } catch(e) {}
      delete VIEWS[AD_HOC_KEY];
    }
    STATE.activeView = clean;
    renderTabs(); renderEditorMeta();
    toast(`Saved "${clean}".`);
    // If the server reported the entries changed and we have rows, auto-rebuild.
    if (d.entries_changed && DATA.length) {
      await build({keepPanelOpen: true});
    }
  } catch (err) {
    toast(err.message || "Save failed.");
  }
}

// "Save" toolbar button — overwrite current named tab, or prompt if ad-hoc.
async function saveWatchlist() {
  const name = STATE.activeView;
  if (!name || name === AD_HOC_KEY) return saveAsNewWatchlist();
  const raw = $("#tickers").value.trim();
  if (!raw) return toast("Enter tickers first.");
  try {
    const r = await fetch("/api/watchlists", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({name, entries: raw}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Save failed.");
    WATCHLISTS = d.watchlists || {};
    renderTabs(); renderEditorMeta();
    toast(`Updated "${name}".`);
    if (d.entries_changed) {
      // Constituents changed → auto-refresh data (user preference).
      await build({keepPanelOpen: true});
    }
  } catch (err) {
    toast(err.message || "Save failed.");
  }
}

async function loadWatchlists() {
  try {
    const res = await fetch("/api/watchlists");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to load watchlists.");
    WATCHLISTS = data.watchlists || {};
  } catch (err) {
    WATCHLISTS = {};
    toast(err.message || "Failed to load watchlists.");
  }
}

function exportCsv() {
  if (!DATA.length) { toast("Nothing to export."); return; }
  const cols = ["symbol","name","price","market_cap","ps_ratio","pe_ratio","pct_ytd","pct_1y","delta_ath","above_1m","above_sma_20","above_sma_50","above_sma_200","w52_low","w52_high","sector","industry"];
  const lines = [cols.join(",")];
  for (const r of DATA) {
    lines.push(cols.map(k => {
      const v = r[k]; if (v == null) return "";
      const s = String(v).replace(/"/g,'""'); return /[,"\n]/.test(s) ? `"${s}"` : s;
    }).join(","));
  }
  const blob = new Blob([lines.join("\n")], {type:"text/csv"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "portfolio_" + new Date().toISOString().slice(0,10) + ".csv";
  a.click(); URL.revokeObjectURL(a.href);
}

/* ===========================================================================
 * Portfolio analytics — weights, request, render, chart
 * --------------------------------------------------------------------------- */
function computeWeights() {
  const rows = DATA.filter(r => r && r.symbol);
  if (!rows.length) return {};
  if (STATE.mode === "equal") {
    const w = 1 / rows.length;
    return Object.fromEntries(rows.map(r => [r.symbol, w]));
  }
  if (STATE.mode === "custom" && STATE.customWeights) {
    // Validate and renormalize over the current row set.
    const raw = {};
    let total = 0;
    for (const r of rows) {
      const v = Number(STATE.customWeights[r.symbol] || 0);
      raw[r.symbol] = isFinite(v) && v >= 0 ? v : 0;
      total += raw[r.symbol];
    }
    if (total <= 0) {
      const w = 1 / rows.length;
      return Object.fromEntries(rows.map(r => [r.symbol, w]));
    }
    return Object.fromEntries(rows.map(r => [r.symbol, raw[r.symbol] / total]));
  }
  // Cap-weighted (default).
  const caps = rows.map(r => Math.max(0, Number(r.market_cap) || 0));
  const total = caps.reduce((a, b) => a + b, 0);
  if (total > 0) return Object.fromEntries(rows.map((r, i) => [r.symbol, caps[i] / total]));
  // Fallback to equal if no caps.
  const w = 1 / rows.length;
  return Object.fromEntries(rows.map(r => [r.symbol, w]));
}

function capWeightsFromData() {
  const rows = DATA.filter(r => r && r.symbol);
  if (!rows.length) return {};
  const caps = rows.map(r => Math.max(0, Number(r.market_cap) || 0));
  const total = caps.reduce((a, b) => a + b, 0);
  if (total > 0) return Object.fromEntries(rows.map((r, i) => [r.symbol, caps[i] / total]));
  const w = 1 / rows.length;
  return Object.fromEntries(rows.map(r => [r.symbol, w]));
}
function equalWeightsFromData() {
  const rows = DATA.filter(r => r && r.symbol);
  if (!rows.length) return {};
  const w = 1 / rows.length;
  return Object.fromEntries(rows.map(r => [r.symbol, w]));
}
function analyticsCacheKey(mode, period) { return mode + "|" + period; }

let _analyticsReqId = 0;
async function requestAnalytics(opts) {
  opts = opts || {};
  if (!DATA.length) {
    STATE.analytics = null;
    renderAnalyticsBody();
    return;
  }
  const period = STATE.period;
  const mode = STATE.mode;
  const key = analyticsCacheKey(mode, period);

  // Cache hit → instant.
  const cached = STATE.analyticsByMode[key];
  if (cached && !opts.force) {
    STATE.analytics = cached;
    STATE.analyticsLoading = false;
    renderAnalyticsBody();
    return;
  }

  const reqId = ++_analyticsReqId;
  STATE.analyticsLoading = true;
  renderAnalyticsBody();

  // Build the weight_sets we'll request. For equal/cap, also pre-compute the
  // sibling mode (free piggyback on the same backend call). For custom we
  // only request the custom set — but the first time custom is opened it's
  // the same as cap, so we can serve it from cache instantly.
  const cap = capWeightsFromData();
  const eq = equalWeightsFromData();
  const wsets = {};
  if (mode === "custom") {
    const cw = STATE.customWeights || cap;
    wsets.custom = cw;
    // Piggyback: also (re)compute equal+cap if they aren't cached for this period.
    if (!STATE.analyticsByMode[analyticsCacheKey("equal", period)]) wsets.equal = eq;
    if (!STATE.analyticsByMode[analyticsCacheKey("cap", period)]) wsets.cap = cap;
  } else {
    wsets.equal = eq;
    wsets.cap = cap;
  }

  try {
    const r = await fetch("/api/portfolio-analytics-multi", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({rows: DATA, weight_sets: wsets, period}),
    });
    const d = await r.json();
    if (reqId !== _analyticsReqId) return;
    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
    if (d.error) throw new Error(d.error);
    const results = d.results || {};
    for (const k of Object.keys(results)) {
      STATE.analyticsByMode[analyticsCacheKey(k, period)] = results[k];
    }
    // If custom wasn't explicitly computed, alias it to cap for this period
    // so switching to Custom… is instant on first open.
    if (!STATE.analyticsByMode[analyticsCacheKey("custom", period)] &&
        STATE.analyticsByMode[analyticsCacheKey("cap", period)] &&
        !STATE.customWeights) {
      STATE.analyticsByMode[analyticsCacheKey("custom", period)] =
          STATE.analyticsByMode[analyticsCacheKey("cap", period)];
    }
    STATE.analytics = STATE.analyticsByMode[key] || results[mode] || null;
  } catch (e) {
    STATE.analytics = {error: e.message};
  } finally {
    if (reqId === _analyticsReqId) STATE.analyticsLoading = false;
    renderAnalyticsBody();
  }
}

function renderAnalyticsBody() {
  const body = $("#pf-analytics-body");
  if (!DATA.length) {
    body.innerHTML = `<div class="pf-empty">Build the dashboard to compute portfolio analytics.</div>`;
    return;
  }
  if (STATE.analyticsLoading && !STATE.analytics) {
    body.innerHTML = `<div class="pf-empty">Computing analytics…</div>`;
    return;
  }
  const a = STATE.analytics;
  if (!a) { body.innerHTML = `<div class="pf-empty">No analytics yet.</div>`; return; }
  if (a.error) { body.innerHTML = `<div class="pf-empty" style="color:var(--neg)">Analytics error: ${escapeHtml(a.error)}</div>`; return; }

  // Build cards.
  const banner = staleBannerHtml();
  body.innerHTML = `
    ${banner}
    <div class="pf-grid">
      <div class="pf-card">
        <h4>Portfolio chart <span class="sub">${escapeHtml(STATE.period)} · ${labelForMode(STATE.mode)}${STATE.analyticsLoading ? '<span class="pf-loading"> refreshing…</span>' : ''}</span></h4>
        <div class="pf-chart-wrap" id="pf-chart-host"></div>
        <div class="pf-chart-legend" id="pf-chart-legend"></div>
      </div>
      <div class="pf-card">
        <h4>Risk &amp; return <span class="sub">vs SPY</span></h4>
        ${renderStatsHtml(a)}
      </div>
      <div class="pf-card">
        <h4>Valuation &amp; analyst <span class="sub">weighted</span></h4>
        ${renderValuationAnalystHtml(a)}
      </div>
      <div class="pf-card">
        <h4>Concentration</h4>
        ${renderConcentrationHtml(a)}
      </div>
      <div class="pf-card">
        <h4>Sector exposure</h4>
        ${renderBarsHtml(a.exposure && a.exposure.by_sector)}
      </div>
      <div class="pf-card">
        <h4>Market-cap buckets</h4>
        ${renderBarsHtml(a.exposure && a.exposure.by_bucket)}
      </div>
      <div class="pf-card" style="grid-column: 1 / -1;">
        <h4>Contribution to ${escapeHtml(STATE.period)} return</h4>
        ${renderContribHtml(a)}
      </div>
    </div>
  `;
  drawPortfolioChart(a, $("#pf-chart-host"), $("#pf-chart-legend"));
}

function staleBannerHtml() {
  const name = STATE.activeView;
  if (!name || !VIEWS[name] || !VIEWS[name].stale) return "";
  return `<div class="pf-stale-banner">Constituents changed since last build — analytics may be stale.
    <button onclick="build({keepPanelOpen:true})">Refresh now</button></div>`;
}

function labelForMode(m) {
  if (m === "equal") return "Equal-weight";
  if (m === "custom") return "Custom-weight";
  return "Cap-weighted";
}

function fmtPctSigned(v, d) {
  if (v == null || !isFinite(v)) return "—";
  d = d == null ? 2 : d;
  const sign = v > 0 ? "+" : "";
  return sign + Number(v).toFixed(d) + "%";
}
function fmtPctPlain(v, d) {
  if (v == null || !isFinite(v)) return "—";
  d = d == null ? 2 : d;
  return Number(v).toFixed(d) + "%";
}
function fmtNumOr(v, d, suffix) {
  if (v == null || !isFinite(v)) return "—";
  d = d == null ? 2 : d;
  return Number(v).toFixed(d) + (suffix || "");
}
function fmtCapBig(v) {
  if (v == null || !isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e12) return (v/1e12).toFixed(2) + "T";
  if (abs >= 1e9)  return (v/1e9).toFixed(2) + "B";
  if (abs >= 1e6)  return (v/1e6).toFixed(2) + "M";
  if (abs >= 1e3)  return (v/1e3).toFixed(2) + "K";
  return v.toFixed(0);
}

function renderStatsHtml(a) {
  const s = a.stats || {};
  const sp = a.spy_stats || {};
  const cls = (v) => v == null ? "" : (v >= 0 ? "pos" : "neg");
  const rows = [
    ["Period return", fmtPctSigned(s.total_return), cls(s.total_return), fmtPctSigned(sp.total_return)],
    ["Ann. return", fmtPctSigned(s.ann_return), cls(s.ann_return), fmtPctSigned(sp.ann_return)],
    ["Ann. vol", fmtPctPlain(s.ann_vol), "", fmtPctPlain(sp.ann_vol)],
    ["Sharpe", fmtNumOr(s.sharpe, 2), "", fmtNumOr(sp.sharpe, 2)],
    ["Sortino", fmtNumOr(s.sortino, 2), "", fmtNumOr(sp.sortino, 2)],
    ["Max drawdown", fmtPctSigned(s.max_dd), cls(s.max_dd), fmtPctSigned(sp.max_dd)],
    ["Calmar", fmtNumOr(s.calmar, 2), "", fmtNumOr(sp.calmar, 2)],
    ["Beta (SPY)", fmtNumOr(s.beta_spy, 2), "", "—"],
    ["R² (SPY)", fmtNumOr(s.r2_spy, 2), "", "—"],
    ["Tracking err", fmtPctPlain(s.te_spy), "", "—"],
  ];
  const html = rows.map(r => `
    <div class="pf-stat-row" title="Portfolio vs SPY">
      <span class="l">${r[0]}</span>
      <span class="v ${r[2]}">${r[1]} <span style="color:var(--muted); font-weight:400">/ ${r[3]}</span></span>
    </div>`).join("");
  return `<div class="pf-stats">${html}</div>`;
}

function renderValuationAnalystHtml(a) {
  const w = a.weighted || {};
  const an = a.analyst || {};
  const rows = [
    ["P/E (wtd)", fmtNumOr(w.pe, 2)],
    ["P/S (wtd)", fmtNumOr(w.ps, 2)],
    ["EV/EBITDA (wtd)", fmtNumOr(w.ev_ebitda, 2)],
    ["Div yield (wtd)", w.div_yield == null ? "—" : fmtPctPlain(w.div_yield)],
    ["Market cap (wtd avg)", fmtCapBig(w.market_cap)],
    ["Analyst rating (1=SB, 5=SS)", fmtNumOr(an.mean_rating, 2)],
    ["Weighted target upside", fmtPctSigned(an.weighted_target_upside_pct)],
    ["Analysts covering (sum)", an.n_analysts_total != null ? an.n_analysts_total : "—"],
  ];
  return `<div class="pf-stats">${rows.map(r => `
    <div class="pf-stat-row"><span class="l">${r[0]}</span><span class="v">${r[1]}</span></div>
  `).join("")}</div>`;
}

function renderConcentrationHtml(a) {
  const c = a.concentration || {};
  const rows = [
    ["Top-5 weight", fmtPctPlain((c.top5 || 0) * 100, 1)],
    ["Herfindahl (HHI)", fmtNumOr(c.herfindahl, 3)],
    ["Effective # of names", fmtNumOr(c.effective_n, 1)],
    ["Active holdings", (a.active_symbols || []).length],
    ["Dropped (no history)", (a.missing_symbols || []).join(", ") || "—"],
  ];
  return `<div class="pf-stats">${rows.map(r => `
    <div class="pf-stat-row"><span class="l">${r[0]}</span><span class="v">${r[1]}</span></div>
  `).join("")}</div>`;
}

function renderBarsHtml(map) {
  if (!map) return `<div class="pf-empty" style="padding:6px 0;">—</div>`;
  const entries = Object.entries(map).filter(([k, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return `<div class="pf-empty" style="padding:6px 0;">No data</div>`;
  const max = entries[0][1];
  return `<div class="pf-bars">${entries.map(([k, v]) => `
    <div class="pf-bar">
      <span class="pf-bar-name" title="${escapeHtml(k)}">${escapeHtml(k)}</span>
      <div class="pf-bar-track"><div class="pf-bar-fill" style="width:${((v/max)*100).toFixed(1)}%"></div></div>
      <span class="pf-bar-val">${(v*100).toFixed(1)}%</span>
    </div>`).join("")}</div>`;
}

function renderContribHtml(a) {
  const list = a.contribution || [];
  if (!list.length) return `<div class="pf-empty" style="padding:6px 0;">No contribution data</div>`;
  return `<table class="pf-contrib-table">
    <thead><tr><th>Symbol</th><th>Weight</th><th>${escapeHtml(STATE.period)} return</th><th>Contribution</th></tr></thead>
    <tbody>${list.map(c => `<tr>
      <td title="${escapeHtml(c.name || c.symbol)}">${escapeHtml(c.symbol)} <span style="color:var(--muted)">${escapeHtml(c.sector || "")}</span></td>
      <td>${(c.weight*100).toFixed(2)}%</td>
      <td class="${c.period_return >= 0 ? 'pos':'neg'}">${fmtPctSigned(c.period_return)}</td>
      <td class="${c.contribution >= 0 ? 'pos':'neg'}">${fmtPctSigned(c.contribution)}</td>
    </tr>`).join("")}</tbody>
  </table>`;
}

function drawPortfolioChart(a, hostEl, legendEl) {
  const series = a.series || {};
  const port = series.portfolio || [];
  if (port.length < 2) { hostEl.innerHTML = `<div class="pf-empty">Not enough data to plot.</div>`; return; }
  const showSpy = STATE.showSpy && series.spy && series.spy.length;
  const showSec = STATE.showSec && series.sector_mix && series.sector_mix.length;
  const showDd  = STATE.showDd && series.drawdown && series.drawdown.length;

  const W = 720, H = 220;
  const padL = 36, padR = 12, padT = 8, padB = 22;
  const t0 = port[0][0], t1 = port[port.length-1][0];
  const xScale = (t) => padL + ((t - t0) / Math.max(1, (t1 - t0))) * (W - padL - padR);
  let lo = Infinity, hi = -Infinity;
  for (const p of port) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (showSpy) for (const p of series.spy) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (showSec) for (const p of series.sector_mix) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  const pad = (hi - lo) * 0.06 || 1;
  lo -= pad; hi += pad;
  const yScale = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);
  const path = (pts) => {
    let s = "";
    for (let i = 0; i < pts.length; i++) {
      const x = xScale(pts[i][0]).toFixed(1), y = yScale(pts[i][1]).toFixed(1);
      s += (i === 0 ? "M" : "L") + x + "," + y + " ";
    }
    return s;
  };
  const ticks = [];
  for (let i = 0; i <= 4; i++) { const v = lo + (hi - lo) * (i/4); ticks.push({v, y: yScale(v)}); }
  const N_XT = 5; const xt = [];
  for (let i = 0; i <= N_XT; i++) { const t = t0 + (t1-t0)*(i/N_XT); xt.push({t, x: xScale(t)}); }
  const fmtT = (ts) => {
    const d = new Date(ts);
    if (STATE.period === "3M" || STATE.period === "6M") return d.toLocaleDateString(undefined, {month:"short", day:"numeric"});
    if (STATE.period === "YTD" || STATE.period === "1Y") return d.toLocaleDateString(undefined, {month:"short", year:"2-digit"});
    return d.toLocaleDateString(undefined, {year:"numeric"});
  };
  const accent = "var(--accent)";
  const svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    ${ticks.map(t => `<line x1="${padL}" y1="${t.y.toFixed(1)}" x2="${W-padR}" y2="${t.y.toFixed(1)}" stroke="var(--border)" stroke-width="0.5" stroke-dasharray="2 3"/>`).join("")}
    ${ticks.map(t => `<text x="${padL-6}" y="${(t.y+3).toFixed(1)}" font-size="10" fill="var(--muted)" text-anchor="end">${t.v.toFixed(0)}</text>`).join("")}
    ${xt.map(t => `<text x="${t.x.toFixed(1)}" y="${(H-padB+12).toFixed(0)}" font-size="10" fill="var(--muted)" text-anchor="middle">${fmtT(t.t)}</text>`).join("")}
    ${showSec ? `<path d="${path(series.sector_mix)}" fill="none" stroke="#f59e0b" stroke-width="1.4" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
    ${showSpy ? `<path d="${path(series.spy)}" fill="none" stroke="#8b5cf6" stroke-width="1.4" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
    <path d="${path(port)}" fill="none" stroke="${accent}" stroke-width="2"/>
  </svg>`;
  let ddSvg = "";
  if (showDd) {
    const dd = series.drawdown;
    const W2 = W, H2 = 80, padT2 = 4, padB2 = 12;
    const minDd = Math.min(...dd.map(p => p[1])); const maxDd = 0;
    const yS2 = (v) => padT2 + (1 - (v - minDd) / Math.max(1e-9, (maxDd - minDd))) * (H2 - padT2 - padB2);
    const path2 = (pts) => {
      let s = "";
      for (let i = 0; i < pts.length; i++) {
        const x = xScale(pts[i][0]).toFixed(1), y = yS2(pts[i][1]).toFixed(1);
        s += (i === 0 ? "M" : "L") + x + "," + y + " ";
      }
      return s + ` L ${xScale(dd[dd.length-1][0]).toFixed(1)},${yS2(0).toFixed(1)} L ${xScale(dd[0][0]).toFixed(1)},${yS2(0).toFixed(1)} Z`;
    };
    ddSvg = `<svg class="pf-dd-svg" viewBox="0 0 ${W2} ${H2}" preserveAspectRatio="none">
      <line x1="${padL}" y1="${yS2(0).toFixed(1)}" x2="${W-padR}" y2="${yS2(0).toFixed(1)}" stroke="var(--border)" stroke-width="0.5"/>
      <path d="${path2(dd)}" fill="rgba(248,81,73,0.18)" stroke="#f85149" stroke-width="1.3"/>
      <text x="${padL-6}" y="${(yS2(minDd)+3).toFixed(1)}" font-size="10" fill="var(--muted)" text-anchor="end">${minDd.toFixed(0)}%</text>
      <text x="${padL-6}" y="${(yS2(0)+3).toFixed(1)}" font-size="10" fill="var(--muted)" text-anchor="end">0</text>
    </svg>`;
  }
  hostEl.innerHTML = svg + ddSvg;
  const legend = [];
  legend.push(`<span><i style="background:#2f81f7"></i> Portfolio</span>`);
  if (showSpy) legend.push(`<span><i style="background:#8b5cf6"></i> SPY</span>`);
  if (showSec) legend.push(`<span><i style="background:#f59e0b"></i> Sector mix</span>`);
  if (showDd)  legend.push(`<span><i style="background:#f85149"></i> Drawdown</span>`);
  legendEl.innerHTML = legend.join("");
}

/* ===========================================================================
 * Custom weights popup
 * --------------------------------------------------------------------------- */
let WEIGHTS_DRAFT = null;  // [{symbol, name, weight (0-1), locked}]

function capWeightsOf(rows) {
  const caps = rows.map(r => Math.max(0, Number(r.market_cap) || 0));
  const total = caps.reduce((a, b) => a + b, 0);
  if (total > 0) return Object.fromEntries(rows.map((r, i) => [r.symbol, caps[i] / total]));
  return Object.fromEntries(rows.map(r => [r.symbol, 1 / Math.max(1, rows.length)]));
}

function openWeightsPopup() {
  if (!DATA.length) return toast("Build a portfolio first.");
  // Initialize draft from saved custom weights if present, otherwise from cap-weighted
  // (per spec: the custom slider starts from cap-weighted).
  const rows = DATA.filter(r => r && r.symbol);
  const init = STATE.customWeights || capWeightsOf(rows);
  WEIGHTS_DRAFT = rows.map(r => ({
    symbol: r.symbol,
    name: r.name || r.symbol,
    weight: Number(init[r.symbol] || 0),
    locked: false,
  }));
  renderWeightsRows();
  $("#pf-weights-bg").classList.add("show");
}

function closeWeightsPopup() {
  $("#pf-weights-bg").classList.remove("show");
}

function renderWeightsRows() {
  const body = $("#pf-weights-body"); body.innerHTML = "";
  WEIGHTS_DRAFT.forEach((row, i) => {
    const r = document.createElement("div");
    r.className = "pf-w-row" + (row.locked ? " locked" : "");
    r.innerHTML = `
      <span class="pf-w-sym" title="${escapeHtml(row.name)}">${escapeHtml(row.symbol)}</span>
      <input class="pf-w-slider" type="range" min="0" max="1" step="0.001" value="${row.weight.toFixed(3)}" ${row.locked ? "disabled" : ""}/>
      <input class="pf-w-num" type="number" min="0" max="100" step="0.1" value="${(row.weight*100).toFixed(2)}"/>
      <button class="pf-w-lock${row.locked ? " on" : ""}" title="Lock">${row.locked ? "🔒" : "🔓"}</button>
    `;
    const slider = r.querySelector(".pf-w-slider");
    const num = r.querySelector(".pf-w-num");
    const lock = r.querySelector(".pf-w-lock");
    slider.addEventListener("input", () => onWeightInput(i, Number(slider.value)));
    num.addEventListener("input", () => onWeightInput(i, Math.max(0, Math.min(100, Number(num.value))) / 100));
    lock.addEventListener("click", () => { row.locked = !row.locked; renderWeightsRows(); updateWeightsSum(); });
    body.appendChild(r);
  });
  updateWeightsSum();
}

function onWeightInput(i, newVal) {
  const row = WEIGHTS_DRAFT[i];
  if (row.locked) return;
  newVal = Math.max(0, Math.min(1, newVal));
  // Distribute the delta proportionally over the other unlocked rows so total stays at 1.
  const lockedTotal = WEIGHTS_DRAFT.filter(r => r.locked).reduce((a, r) => a + r.weight, 0);
  const otherUnlocked = WEIGHTS_DRAFT.filter((r, j) => !r.locked && j !== i);
  const otherCurrent = otherUnlocked.reduce((a, r) => a + r.weight, 0);
  const remaining = Math.max(0, 1 - lockedTotal - newVal);
  if (otherUnlocked.length === 0) {
    row.weight = newVal;
  } else if (otherCurrent <= 0) {
    const share = remaining / otherUnlocked.length;
    for (const r of otherUnlocked) r.weight = share;
    row.weight = newVal;
  } else {
    const factor = remaining / otherCurrent;
    for (const r of otherUnlocked) r.weight = r.weight * factor;
    row.weight = newVal;
  }
  // Sync sliders without rebuilding the DOM (preserve focus).
  syncWeightsInputs();
  updateWeightsSum();
}

function syncWeightsInputs() {
  const rows = $$(".pf-w-row");
  rows.forEach((el, i) => {
    const w = WEIGHTS_DRAFT[i];
    if (!w) return;
    const s = el.querySelector(".pf-w-slider"); const n = el.querySelector(".pf-w-num");
    if (document.activeElement !== s) s.value = w.weight.toFixed(3);
    if (document.activeElement !== n) n.value = (w.weight*100).toFixed(2);
    el.classList.toggle("locked", w.locked);
  });
}

function updateWeightsSum() {
  const total = WEIGHTS_DRAFT.reduce((a, r) => a + r.weight, 0);
  const el = $("#pf-weights-sum");
  const pct = total * 100;
  el.textContent = pct.toFixed(2) + "%";
  el.className = "pf-weights-sum" + (Math.abs(pct - 100) < 0.5 ? " good" : " bad");
}

function normalizeDraft() {
  const total = WEIGHTS_DRAFT.reduce((a, r) => a + r.weight, 0);
  if (total <= 0) return;
  for (const r of WEIGHTS_DRAFT) r.weight = r.weight / total;
  syncWeightsInputs(); updateWeightsSum();
}

function resetDraftToEqual() {
  if (!WEIGHTS_DRAFT || !WEIGHTS_DRAFT.length) return;
  const w = 1 / WEIGHTS_DRAFT.length;
  WEIGHTS_DRAFT.forEach((row) => { row.weight = w; row.locked = false; });
  renderWeightsRows();
}

function resetDraftToCap() {
  // Re-derive cap weights from DATA.
  const caps = DATA.map(r => Math.max(0, Number(r.market_cap) || 0));
  const total = caps.reduce((a, b) => a + b, 0);
  const w = total > 0 ? DATA.map((r, i) => caps[i] / total) : DATA.map(() => 1 / DATA.length);
  WEIGHTS_DRAFT.forEach((row, i) => {
    row.weight = w[i] || 0;
    row.locked = false;
  });
  renderWeightsRows();
}

function applyWeightsDraft() {
  // Always renormalize before applying so the backend gets a sum-to-1 vector.
  const total = WEIGHTS_DRAFT.reduce((a, r) => a + r.weight, 0);
  if (total <= 0) { toast("Weights must sum to a positive value."); return; }
  STATE.customWeights = {};
  for (const r of WEIGHTS_DRAFT) STATE.customWeights[r.symbol] = r.weight / total;
  STATE.mode = "custom";
  // Invalidate any cached custom result (weights changed).
  for (const k of Object.keys(STATE.analyticsByMode)) {
    if (k.startsWith("custom|")) delete STATE.analyticsByMode[k];
  }
  updateModeButtons();
  closeWeightsPopup();
  requestAnalytics({force: true});
}

function updateModeButtons() {
  $$("#pf-mode-toggle button").forEach(b => b.classList.toggle("active", b.dataset.mode === STATE.mode));
}
function updatePeriodButtons() {
  $$("#pf-period-tabs button").forEach(b => b.classList.toggle("active", b.dataset.p === STATE.period));
}

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(window._toastT); window._toastT = setTimeout(() => t.classList.remove("show"), 2400);
}

/* ===========================================================================
 * Info panel
 * --------------------------------------------------------------------------- */
let _katexRendered = false;
function openInfo() {
  $("#info-bg").classList.add("show");
  if (!_katexRendered && window.renderMathInElement) {
    renderMathInElement(document.getElementById("info-bg"), {
      delimiters: [{ left: "$$", right: "$$", display: true }],
      throwOnError: false,
    });
    _katexRendered = true;
  }
}
function closeInfo() { $("#info-bg").classList.remove("show"); }
$("#info-bg").addEventListener("click", (e) => { if (e.target.id === "info-bg") closeInfo(); });

/* ===========================================================================
 * Wire up
 * --------------------------------------------------------------------------- */
$("#build").onclick = () => build({keepPanelOpen: true});
$("#refresh").onclick = () => build({keepPanelOpen: $("#input-panel").classList.contains("hidden") ? false : true});
$("#save").onclick = saveWatchlist;
$("#save-as").onclick = saveAsNewWatchlist;
$("#export").onclick = exportCsv;
$("#edit-btn").onclick = () => {
  const panel = $("#input-panel");
  const willOpen = panel.classList.contains("hidden");
  panel.classList.toggle("hidden");
  $("#edit-btn").classList.toggle("active", willOpen);
  if (willOpen) {
    // If we open the panel without an active view yet, start an ad-hoc tab.
    if (!STATE.activeView) STATE.activeView = AD_HOC_KEY;
    renderTabs(); renderEditorMeta();
    if (DATA.length && (!STATE.analytics || STATE.analytics.error)) requestAnalytics();
  }
};
$("#info-btn").onclick = openInfo;
$("#theme-switch").onclick = () => setTheme(getTheme() === "dark" ? "light" : "dark");
$("#sort-btn").onclick = (e) => {
  e.stopPropagation();
  $("#sort-menu").classList.toggle("open");
  renderSortMenu();
};
document.addEventListener("click", (e) => {
  if (!e.target.closest("#sort-menu")) $("#sort-menu").classList.remove("open");
});
$("#tickers").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); build({keepPanelOpen: true}); }
});
window.addEventListener("scroll", () => {
  $("#topbar").classList.toggle("scrolled", window.scrollY > 4);
});

/* --- Analytics controls --- */
$("#pf-mode-toggle").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-mode]");
  if (!btn) return;
  const mode = btn.dataset.mode;
  if (mode === "custom") { openWeightsPopup(); return; }
  STATE.mode = mode;
  updateModeButtons();
  requestAnalytics();
});
$("#pf-period-tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-p]");
  if (!btn) return;
  STATE.period = btn.dataset.p;
  updatePeriodButtons();
  requestAnalytics();
});
$("#pf-show-spy").addEventListener("change", (e) => { STATE.showSpy = e.target.checked; renderAnalyticsBody(); });
$("#pf-show-sec").addEventListener("change", (e) => { STATE.showSec = e.target.checked; renderAnalyticsBody(); });
$("#pf-show-dd").addEventListener("change", (e) => { STATE.showDd = e.target.checked; renderAnalyticsBody(); });

/* --- Weights popup wiring --- */
$("#pf-weights-bg").addEventListener("click", (e) => { if (e.target.id === "pf-weights-bg") closeWeightsPopup(); });
$("#pf-weights-close").addEventListener("click", closeWeightsPopup);
$("#pf-weights-cancel").addEventListener("click", closeWeightsPopup);
$("#pf-weights-equal").addEventListener("click", resetDraftToEqual);
$("#pf-weights-reset").addEventListener("click", resetDraftToCap);
$("#pf-weights-apply").addEventListener("click", applyWeightsDraft);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#pf-weights-bg").classList.contains("show")) closeWeightsPopup(); });

$("#footer-date").textContent = new Date().toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
setTheme(readTheme());
renderHeader();
renderSortMenu();
updateSortLabel();
loadAllAtStartup();
</script>

</body>
</html>
"""


# ----------------------------- HTTP server ---------------------------------


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # quieter logs
        msg = format % args
        if "/api/" in msg or msg.startswith('"GET / '):
            print(f"[{self.log_date_time_string()}] {msg}")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/health":
            self._send_json(200, {"ok": True, "ts": datetime.now(timezone.utc).isoformat()})
            return
        if parsed.path == "/api/watchlists":
            self._send_json(200, {"watchlists": load_watchlists()})
            return
        if parsed.path == "/api/views":
            self._send_json(200, list_views())
            return
        if parsed.path.startswith("/api/views/"):
            name = parsed.path[len("/api/views/"):]
            from urllib.parse import unquote
            name = unquote(name)
            view = load_view(name)
            self._send_json(200, {"view": view, "name": name})
            return
        if parsed.path == "/api/detail":
            from urllib.parse import parse_qs

            q = parse_qs(parsed.query)
            sym = (q.get("symbol") or [""])[0].strip().upper()
            if not sym:
                self._send_json(400, {"error": "symbol required"})
                return
            try:
                payload = fetch_detail(sym)
                self._send_json(200, payload)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/watchlists":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                watchlists, changed = upsert_watchlist(
                    str(payload.get("name") or ""),
                    str(payload.get("entries") or ""),
                )
                self._send_json(200, {"watchlists": watchlists, "entries_changed": changed})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/quotes":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                entries = payload.get("entries") or []
                rows = fetch_portfolio([str(e) for e in entries])
                self._send_json(200, {"rows": rows})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path.startswith("/api/views/"):
            from urllib.parse import unquote
            name = unquote(parsed.path[len("/api/views/"):])
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                entries = str(payload.get("entries") or "")
                rows = payload.get("rows") or []
                if not isinstance(rows, list):
                    rows = []
                set_last_arg = payload.get("set_last", True)
                saved = save_view(name, entries, rows, set_last=bool(set_last_arg))
                self._send_json(200, {"name": name, "view": saved})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/last-view":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                set_last_view(str(payload.get("name") or ""))
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/portfolio-analytics":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                rows = payload.get("rows") or []
                weights = payload.get("weights") or {}
                period = str(payload.get("period") or "1Y")
                if not isinstance(rows, list) or not isinstance(weights, dict):
                    self._send_json(400, {"error": "rows[] and weights{} required"})
                    return
                result = analyze_portfolio(rows, weights, period)
                self._send_json(200, result)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/portfolio-analytics-multi":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                rows = payload.get("rows") or []
                weight_sets = payload.get("weight_sets") or {}
                period = str(payload.get("period") or "1Y")
                if not isinstance(rows, list) or not isinstance(weight_sets, dict) or not weight_sets:
                    self._send_json(400, {"error": "rows[] and weight_sets{name: weights} required"})
                    return
                results = analyze_portfolios_multi(rows, weight_sets, period)
                self._send_json(200, {"results": results} if "error" not in results else results)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/quotes-stream":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                entries = payload.get("entries") or []
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
                return

            symbols = _ordered_resolve([str(e) for e in entries])
            total = len(symbols)

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self.wfile.write(_safe_json({"type": "start", "total": total, "symbols": symbols}))
                self.wfile.flush()
                if total:
                    workers = min(5, total)
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = {pool.submit(fetch_one, s): s for s in symbols}
                        done = 0
                        for fut in as_completed(futures):
                            row = fut.result()
                            done += 1
                            try:
                                self.wfile.write(
                                    _safe_json(
                                        {
                                            "type": "row",
                                            "row": row,
                                            "done": done,
                                            "total": total,
                                        }
                                    )
                                )
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                return
                self.wfile.write(_safe_json({"type": "done", "total": total}))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/views/"):
            from urllib.parse import unquote
            name = unquote(parsed.path[len("/api/views/"):])
            try:
                delete_view(name)
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path != "/api/watchlists":
            self.send_response(404)
            self.end_headers()
            return

        from urllib.parse import parse_qs

        name = (parse_qs(parsed.query).get("name") or [""])[0]
        try:
            watchlists = delete_watchlist(name)
            self._send_json(200, {"watchlists": watchlists})
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})


def _pick_port(preferred: int = 8765) -> int:
    for port in [preferred, 8766, 8767, 8768, 0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


def main() -> None:
    port = _pick_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}/"
    print("=" * 60)
    print(f"  📊 Portfolio Tracker running at {url}")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
        server.shutdown()


if __name__ == "__main__":
    main()
