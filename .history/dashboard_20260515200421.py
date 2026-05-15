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


def _watchlists_path() -> Path:
  return Path(__file__).resolve().parent / ".portfolio_tracker_watchlists.json"


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


def upsert_watchlist(name: str, entries: str) -> dict[str, str]:
  clean_name = name.strip()
  clean_entries = entries.strip()
  if not clean_name:
    raise ValueError("watchlist name required")
  if not clean_entries:
    raise ValueError("watchlist entries required")
  watchlists = load_watchlists()
  watchlists[clean_name] = clean_entries
  return save_watchlists(watchlists)


def delete_watchlist(name: str) -> dict[str, str]:
  watchlists = load_watchlists()
  watchlists.pop(name.strip(), None)
  return save_watchlists(watchlists)


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
    padding: 14px 16px;
    border: 1px solid var(--border);
    border-top: none;
  }
  #input-panel.hidden { display: none; }
  #input-panel textarea {
    width: 100%; min-height: 70px; resize: vertical;
    font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 13px;
    border: 1px solid var(--border); border-radius: 8px; padding: 9px 11px;
    background: var(--bg-canvas); color: var(--text);
  }
  #input-panel textarea:focus { outline: none; border-color: var(--accent); }
  .panel-row { display: flex; gap: 8px; align-items: center; margin-top: 10px; }
  .panel-row .status { color: var(--muted); font-size: 12px; }
  .watchlists { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .chip {
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 999px;
    padding: 3px 10px; font-size: 12px; cursor: pointer;
    display: inline-flex; gap: 6px; align-items: center; color: var(--text);
  }
  .chip:hover { border-color: var(--accent); }
  .chip .x { color: var(--muted); }
  .chip .x:hover { color: var(--neg); }

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

  /* Modal */
  .modal-bg {
    position: fixed; inset: 0; background: rgba(0,0,0,0.45); display: none;
    align-items: center; justify-content: center; z-index: 50;
  }
  .modal-bg.show { display: flex; }
  .modal {
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 12px;
    width: min(820px, 92vw); padding: 22px 24px; max-height: 88vh; overflow: auto;
    color: var(--text);
  }
  .modal h2 { margin: 0 0 4px; font-size: 18px; }
  .modal .sub { color: var(--muted); font-size: 12.5px; margin-bottom: 14px; }
  .modal .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 12px; }
  .modal .stats div {
    background: var(--bg-subtle); border: 1px solid var(--border);
    border-radius: 8px; padding: 9px;
  }
  .modal .stats .k { color: var(--muted); font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.4px; }
  .modal .stats .v { font-weight: 600; margin-top: 2px; }
  .modal .close { float: right; cursor: pointer; color: var(--muted); font-size: 22px; line-height: 1; }

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
  <button id="edit-btn">✎ Edit Portfolio</button>
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

<div id="input-panel">
  <textarea id="tickers" placeholder="Paste tickers or company names — comma or newline separated.
e.g.  DELL, TXN, DaVita, JBL, KLAC, MARA, COMT, FFIV, Alphabet, ETN, AVGO, NVDA, XLK, Trane, MSTR, COST, Apple, Microsoft"></textarea>
  <div class="panel-row">
    <button id="build" class="primary">Build Dashboard</button>
    <span class="status">Paste your tickers and press Build (or Cmd/Ctrl + Enter).</span>
  </div>
  <div class="watchlists" id="watchlists"></div>
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
function openModal(r) {
  if (r.error) return;
  const stats = [
    ["Exchange", r.exchange || "—"], ["Sector", r.sector || "—"], ["Industry", r.industry || "—"],
    ["Market Cap", fmtCompactMoney(r.market_cap)], ["P/S", fmt2(r.ps_ratio)], ["P/E", fmt2(r.pe_ratio)],
    ["52W High", fmtMoney(r.w52_high)], ["52W Low", fmtMoney(r.w52_low)], ["ATH", fmtMoney(r.ath)],
    ["1D", r.pct_1d != null ? r.pct_1d.toFixed(2)+"%" : "—"],
    ["1M", r.pct_1m != null ? r.pct_1m.toFixed(2)+"%" : "—"],
    ["YTD", r.pct_ytd != null ? r.pct_ytd.toFixed(2)+"%" : "—"],
  ];
  $("#modal").innerHTML = `
    <span class="close" onclick="closeModal()">×</span>
    <h2>${logoImg(r.symbol)} &nbsp; ${r.symbol} — ${escapeHtml(r.name||"")}</h2>
    <div class="sub">${fmtMoney(r.price)} &nbsp; <b style="color:${(r.pct_1d||0) >= 0 ? 'var(--pos)' : 'var(--neg)'}">${fmtPctSigned(r.pct_1d)}</b> today</div>
    ${bigChart(r.sparkline, r.pct_1y)}
    <div class="stats">${stats.map(([k,v]) => `<div><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>
  `;
  $("#modal-bg").classList.add("show");
}
function closeModal() { $("#modal-bg").classList.remove("show"); }
$("#modal-bg").addEventListener("click", (e) => { if (e.target.id === "modal-bg") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeInfo(); } });

function bigChart(pts, pct1y) {
  if (!pts || pts.length < 2) return '<div class="sub">No chart data.</div>';
  const w = 760, h = 200, pad = 6;
  const lo = Math.min(...pts), hi = Math.max(...pts);
  const rng = (hi - lo) || 1;
  const step = (w - 2*pad) / (pts.length - 1);
  let d = "";
  for (let i = 0; i < pts.length; i++) {
    const x = pad + i * step;
    const y = h - pad - ((pts[i] - lo) / rng) * (h - 2*pad);
    d += (i === 0 ? "M" : "L") + x.toFixed(1) + "," + y.toFixed(1) + " ";
  }
  const up = (pct1y != null ? pct1y : (pts[pts.length-1] - pts[0])) >= 0;
  const stroke = getComputedStyle(document.documentElement).getPropertyValue(up ? "--pos" : "--neg").trim();
  const fill = `rgba(${getComputedStyle(document.documentElement).getPropertyValue(up ? "--pos-rgb" : "--neg-rgb")}, 0.12)`;
  const area = d + ` L ${w-pad},${h-pad} L ${pad},${h-pad} Z`;
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%; height:200px; margin-top:6px;">
    <path d="${area}" fill="${fill}" stroke="none"/>
    <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.8"/>
  </svg>`;
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

async function build() {
  const raw = $("#tickers").value.trim();
  if (!raw) { toast("Enter at least one ticker or company name."); return; }
  const entries = raw.split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
  $("#build").disabled = true; $("#refresh").disabled = true;
  $("#status").textContent = `Fetching ${entries.length} symbol${entries.length>1?"s":""}…`;
  showProgress(2);
  DATA = [];
  /* Initial empty render so the user sees the headers fill in. */
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
    $("#input-panel").classList.add("hidden");
  } catch (e) {
    toast("Error: " + e.message);
    $("#status").textContent = "Error.";
  } finally {
    hideProgress();
    $("#build").disabled = false; $("#refresh").disabled = false;
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
 * Watchlists (server-backed)
 * --------------------------------------------------------------------------- */
let WATCHLISTS = {};

function renderWatchlists() {
  const wls = WATCHLISTS;
  const wrap = $("#watchlists"); wrap.innerHTML = "";
  const names = Object.keys(wls);
  if (!names.length) {
    wrap.innerHTML = '<span style="color:var(--muted); font-size:12px;">No saved watchlists yet. Build one and click <b>Save Watchlist</b>.</span>';
    return;
  }
  for (const name of names) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `<span>${escapeHtml(name)}</span><span class="x" title="Delete">✕</span>`;
    chip.firstElementChild.onclick = () => { $("#tickers").value = wls[name]; build(); };
    chip.querySelector(".x").onclick = async (e) => {
      e.stopPropagation();
      try {
        const res = await fetch(`/api/watchlists?name=${encodeURIComponent(name)}`, { method: "DELETE" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Delete failed.");
        WATCHLISTS = data.watchlists || {};
        renderWatchlists();
      } catch (err) {
        toast(err.message || "Delete failed.");
      }
    };
    wrap.appendChild(chip);
  }
}

async function loadWatchlists() {
  try {
    const res = await fetch("/api/watchlists");
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to load watchlists.");
    WATCHLISTS = data.watchlists || {};
    renderWatchlists();
  } catch (err) {
    WATCHLISTS = {};
    renderWatchlists();
    toast(err.message || "Failed to load watchlists.");
  }
}

async function saveWatchlist() {
  const raw = $("#tickers").value.trim();
  if (!raw) return toast("Enter tickers first.");
  const name = prompt("Watchlist name?", "");
  if (!name) return;
  try {
    const res = await fetch("/api/watchlists", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, entries: raw }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Save failed.");
    WATCHLISTS = data.watchlists || {};
    renderWatchlists();
    toast(`Saved "${name.trim()}".`);
  } catch (err) {
    toast(err.message || "Save failed.");
  }
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
$("#build").onclick = build;
$("#refresh").onclick = build;
$("#save").onclick = saveWatchlist;
$("#export").onclick = exportCsv;
$("#edit-btn").onclick = () => $("#input-panel").classList.toggle("hidden");
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
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); build(); }
});
window.addEventListener("scroll", () => {
  $("#topbar").classList.toggle("scrolled", window.scrollY > 4);
});

$("#footer-date").textContent = new Date().toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
setTheme(readTheme());
renderHeader();
renderSortMenu();
updateSortLabel();
loadWatchlists();
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
        self.send_response(404); self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/watchlists":
          length = int(self.headers.get("Content-Length") or 0)
          try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            watchlists = upsert_watchlist(
              str(payload.get("name") or ""),
              str(payload.get("entries") or ""),
            )
            self._send_json(200, {"watchlists": watchlists})
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
                                self.wfile.write(_safe_json({
                                    "type": "row", "row": row, "done": done, "total": total,
                                }))
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                return
                self.wfile.write(_safe_json({"type": "done", "total": total}))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        self.send_response(404); self.end_headers()

        def do_DELETE(self):
          parsed = urlparse(self.path)
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
    print(f"  Press Ctrl+C to stop.")
    print("=" * 60)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
        server.shutdown()


if __name__ == "__main__":
    main()
