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

import mpt

# ----------------------------- Rate-limit handling -------------------------
# yfinance 1.0 ships its own curl_cffi-based session (with TLS fingerprinting
# that dodges most of Yahoo's anti-bot filtering); passing a plain
# requests.Session crashes it. So we let YF manage its own session and just
# control concurrency + add backoff at our layer.


# ----------------------------- Cache --------------------------------------

_CACHE: dict[str, tuple[float, float, dict]] = {}
_CACHE_TTL_DEFAULT = 300.0
_CACHE_TTL_ANALYTICS = 1800.0
_WATCHLISTS_LOCK = threading.Lock()


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
        # Preserve named weight presets + cached analyst panel across row refreshes
        # — they're per-portfolio metadata, not derived from the row payload itself.
        prior = views_map.get(clean_name) if isinstance(views_map.get(clean_name), dict) else None
        if prior:
            for k in ("weight_presets", "active_weight_preset", "analytics_cache"):
                if k in prior:
                    payload[k] = prior[k]
        views_map[clean_name] = payload
        raw["views"] = views_map
        if set_last:
            raw["last_view"] = clean_name
        _write_views_raw(raw)
    return payload


# ----------------------------- Weight presets (per-portfolio) ---------------

def _normalize_preset_weights(weights_in) -> dict[str, float]:
    """Coerce a {symbol: number} dict to a clean float map, dropping non-numeric
    entries. We DO NOT renormalise here — the frontend already shows the user
    a sum indicator, and storing the unsanctioned sum lets us round-trip
    'work-in-progress' presets faithfully."""
    out: dict[str, float] = {}
    if not isinstance(weights_in, dict):
        return out
    for sym, w in weights_in.items():
        try:
            out[str(sym).strip().upper()] = float(w)
        except (TypeError, ValueError):
            continue
    return out


def list_weight_presets(view_name: str) -> dict:
    """Return {presets: [...], active: name|None} for the view, defensively
    handling missing/legacy entries."""
    entry = load_view(view_name) or {}
    presets = entry.get("weight_presets")
    if not isinstance(presets, list):
        presets = []
    return {"presets": presets, "active": entry.get("active_weight_preset")}


def upsert_weight_preset(view_name: str, preset_name: str, weights: dict,
                         *, rename_from: str | None = None,
                         set_active: bool = True) -> dict:
    """Create or update a named preset on the given view. If `rename_from` is
    supplied and matches an existing preset, that preset is renamed in-place
    (preserving order)."""
    clean_view = (view_name or "").strip()
    clean_name = (preset_name or "").strip()
    if not clean_view:
        raise ValueError("view name required")
    if not clean_name:
        raise ValueError("preset name required")
    payload = {
        "name": clean_name,
        "weights": _normalize_preset_weights(weights),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    with _VIEWS_LOCK:
        raw = _read_views_raw()
        views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
        entry = views_map.get(clean_view)
        if not isinstance(entry, dict):
            raise ValueError(f"view '{clean_view}' not found")
        presets = entry.get("weight_presets")
        if not isinstance(presets, list):
            presets = []
        # In-place replacement: prefer rename_from, fall back to name match
        replaced = False
        for i, p in enumerate(presets):
            if not isinstance(p, dict):
                continue
            pname = (p.get("name") or "").strip()
            if rename_from and pname == rename_from.strip():
                presets[i] = payload
                replaced = True
                break
            if not rename_from and pname == clean_name:
                presets[i] = payload
                replaced = True
                break
        if not replaced:
            presets.append(payload)
        entry["weight_presets"] = presets
        if set_active:
            entry["active_weight_preset"] = clean_name
        views_map[clean_view] = entry
        raw["views"] = views_map
        _write_views_raw(raw)
    return {"presets": presets, "active": entry.get("active_weight_preset")}


def delete_weight_preset(view_name: str, preset_name: str) -> dict:
    clean_view = (view_name or "").strip()
    clean_name = (preset_name or "").strip()
    if not clean_view or not clean_name:
        return list_weight_presets(clean_view)
    with _VIEWS_LOCK:
        raw = _read_views_raw()
        views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
        entry = views_map.get(clean_view)
        if not isinstance(entry, dict):
            return {"presets": [], "active": None}
        presets = [p for p in (entry.get("weight_presets") or [])
                   if isinstance(p, dict) and (p.get("name") or "").strip() != clean_name]
        entry["weight_presets"] = presets
        if (entry.get("active_weight_preset") or "").strip() == clean_name:
            entry["active_weight_preset"] = None
        views_map[clean_view] = entry
        raw["views"] = views_map
        _write_views_raw(raw)
    return {"presets": presets, "active": entry.get("active_weight_preset")}


def set_active_weight_preset(view_name: str, preset_name: str | None) -> dict:
    clean_view = (view_name or "").strip()
    if not clean_view:
        return {"presets": [], "active": None}
    clean_name = (preset_name or "").strip() or None
    with _VIEWS_LOCK:
        raw = _read_views_raw()
        views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
        entry = views_map.get(clean_view)
        if not isinstance(entry, dict):
            return {"presets": [], "active": None}
        entry["active_weight_preset"] = clean_name
        views_map[clean_view] = entry
        raw["views"] = views_map
        _write_views_raw(raw)
    return list_weight_presets(clean_view)


# ----------------------------- Analytics cache (per-portfolio) --------------
# Persists the *static* slices of the analytics payload (`analyst`, `exposure`,
# `concentration`, `stats`, `spy_stats`, `nasdaq_stats`) inside the view JSON,
# so reopening a saved portfolio paints the Rating Distribution panel from
# disk instead of re-querying yfinance every time. Time-series fields are
# deliberately excluded — they're large and only the live chart consumes them.

_ANALYTICS_CACHE_MAX_PER_VIEW = 8


def get_analytics_cache(view_name: str) -> dict:
    """Return {key: payload} for the view. Empty dict if the view doesn't
    exist or has no cache yet."""
    entry = load_view(view_name) or {}
    cache = entry.get("analytics_cache")
    return cache if isinstance(cache, dict) else {}


def upsert_analytics_cache(view_name: str, key: str, payload: dict) -> dict:
    """Store one cache entry (overwriting any existing entry with the same key).
    Caps total entries at _ANALYTICS_CACHE_MAX_PER_VIEW (LRU by saved_at)."""
    clean_view = (view_name or "").strip()
    clean_key = (key or "").strip()
    if not clean_view or not clean_key:
        return {}
    record = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload if isinstance(payload, dict) else {},
    }
    with _VIEWS_LOCK:
        raw = _read_views_raw()
        views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
        entry = views_map.get(clean_view)
        if not isinstance(entry, dict):
            return {}
        cache = entry.get("analytics_cache")
        if not isinstance(cache, dict):
            cache = {}
        cache[clean_key] = record
        # LRU cap: drop oldest entries by saved_at.
        if len(cache) > _ANALYTICS_CACHE_MAX_PER_VIEW:
            items = sorted(cache.items(),
                           key=lambda kv: kv[1].get("saved_at", "") if isinstance(kv[1], dict) else "")
            for old_key, _ in items[: len(cache) - _ANALYTICS_CACHE_MAX_PER_VIEW]:
                cache.pop(old_key, None)
        entry["analytics_cache"] = cache
        views_map[clean_view] = entry
        raw["views"] = views_map
        _write_views_raw(raw)
    return cache


def clear_analytics_cache(view_name: str) -> None:
    """Drop every cached analytics entry for the view. Called when the user
    refreshes the portfolio so they get fresh analyst data on next open."""
    clean_view = (view_name or "").strip()
    if not clean_view:
        return
    with _VIEWS_LOCK:
        raw = _read_views_raw()
        views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
        entry = views_map.get(clean_view)
        if not isinstance(entry, dict):
            return
        if "analytics_cache" in entry:
            entry["analytics_cache"] = {}
            views_map[clean_view] = entry
            raw["views"] = views_map
            _write_views_raw(raw)


# ----------------------------- MPT run history ------------------------------

_MPT_FILE = _repo_root() / ".portfolio_tracker_mpt.json"
_MPT_LOCK = threading.Lock()


def _read_mpt_raw() -> dict:
    path = Path(_MPT_FILE)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_mpt_raw(raw: dict) -> None:
    body = json.dumps(raw, default=_json_default, ensure_ascii=True, indent=2)
    Path(_MPT_FILE).write_text(body + "\n", encoding="utf-8")


def list_mpt_runs(view_name: str) -> list[dict]:
    """Return MPT runs for a view (metadata only — strip heavy fields)."""
    with _MPT_LOCK:
        raw = _read_mpt_raw()
    runs = (raw.get("runs") or {}).get(view_name) or []
    meta: list[dict] = []
    for r in runs:
        if not isinstance(r, dict):
            continue
        meta.append({
            "id": r.get("id"),
            "params": r.get("params") or {},
            "symbols": r.get("symbols") or [],
            "saved_at": r.get("saved_at"),
            "tangency": r.get("tangency"),
        })
    return meta


def load_mpt_run(view_name: str, run_id: str) -> dict | None:
    with _MPT_LOCK:
        raw = _read_mpt_raw()
    for r in (raw.get("runs") or {}).get(view_name) or []:
        if isinstance(r, dict) and r.get("id") == run_id:
            return r
    return None


def save_mpt_run(view_name: str, run: dict) -> dict:
    """Persist an MPT run for a portfolio with aggressive eviction.

    The dashboard auto-saves every successful Run Optimization click, so this
    function churns the saved list to keep it relevant:
      * Any prior run whose symbol set differs from the new run's symbols is
        deleted (portfolio composition changed → old runs are no longer
        meaningful for this portfolio).
      * Any prior run whose risk-free rate differs from the new run's rf by
        more than 1e-6 is deleted (rf is a continuous parameter with
        effectively infinite combinations; the user can always regenerate).
    After eviction the new payload is appended, dedup-by-id, and the list
    is capped at 30 newest entries as a final ceiling.
    """
    clean_view = (view_name or "").strip()
    if not clean_view:
        raise ValueError("view name required")
    rid = run.get("id") or ("run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
    payload = {**run, "id": rid, "saved_at": datetime.now(timezone.utc).isoformat()}
    new_symbols = set(payload.get("symbols") or [])
    new_rf = float((payload.get("params") or {}).get("rf") or 0.0)
    with _MPT_LOCK:
        raw = _read_mpt_raw()
        runs_map = raw.get("runs") if isinstance(raw.get("runs"), dict) else {}
        runs = runs_map.get(clean_view) or []
        # Eviction: drop runs from a different portfolio composition OR rf.
        def _keep(r: dict) -> bool:
            if not isinstance(r, dict):
                return False
            if r.get("id") == rid:
                return False  # will be re-added below (replace-by-id)
            old_syms = set(r.get("symbols") or [])
            if old_syms != new_symbols:
                return False
            old_rf = float((r.get("params") or {}).get("rf") or 0.0)
            if abs(old_rf - new_rf) > 1e-6:
                return False
            return True
        kept = [r for r in runs if _keep(r)]
        kept.append(payload)
        kept.sort(key=lambda r: r.get("saved_at") or "", reverse=True)
        runs_map[clean_view] = kept[:30]
        raw["runs"] = runs_map
        _write_mpt_raw(raw)
    return payload


def delete_mpt_run(view_name: str, run_id: str) -> None:
    with _MPT_LOCK:
        raw = _read_mpt_raw()
        runs_map = raw.get("runs") if isinstance(raw.get("runs"), dict) else {}
        runs = runs_map.get(view_name) or []
        runs_map[view_name] = [r for r in runs if not (isinstance(r, dict) and r.get("id") == run_id)]
        raw["runs"] = runs_map
        _write_mpt_raw(raw)


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


def rename_view(old: str, new: str) -> bool:
    """Rename a view key from `old` to `new`. Updates last_view if it pointed
    at `old`. Returns True if a rename actually happened."""
    old_clean = (old or "").strip()
    new_clean = (new or "").strip()
    if not old_clean or not new_clean or old_clean == new_clean:
        return False
    with _VIEWS_LOCK:
        raw = _read_views_raw()
        views_map = raw.get("views") if isinstance(raw.get("views"), dict) else {}
        if old_clean not in views_map:
            return False
        if new_clean in views_map:
            raise ValueError(f"view '{new_clean}' already exists")
        views_map[new_clean] = views_map.pop(old_clean)
        raw["views"] = views_map
        if raw.get("last_view") == old_clean:
            raw["last_view"] = new_clean
        _write_views_raw(raw)
    # Cascade: MPT runs are keyed by view name; rename the bucket too.
    with _MPT_LOCK:
        raw_m = _read_mpt_raw()
        runs_map = raw_m.get("runs") if isinstance(raw_m.get("runs"), dict) else {}
        if old_clean in runs_map:
            runs_map[new_clean] = runs_map.pop(old_clean)
            raw_m["runs"] = runs_map
            _write_mpt_raw(raw_m)
    return True


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
    # Cascade: drop any MPT runs associated with the deleted view.
    with _MPT_LOCK:
        raw_m = _read_mpt_raw()
        runs_map = raw_m.get("runs") if isinstance(raw_m.get("runs"), dict) else {}
        if clean_name in runs_map:
            runs_map.pop(clean_name, None)
            raw_m["runs"] = runs_map
            _write_mpt_raw(raw_m)


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


def rename_watchlist(old: str, new: str) -> dict[str, str]:
    """Rename a watchlist key. Raises if the new name collides."""
    old_clean = (old or "").strip()
    new_clean = (new or "").strip()
    if not old_clean or not new_clean:
        raise ValueError("watchlist name required")
    if old_clean == new_clean:
        return load_watchlists()
    watchlists = load_watchlists()
    if old_clean not in watchlists:
        raise ValueError(f"watchlist '{old_clean}' not found")
    if new_clean in watchlists:
        raise ValueError(f"watchlist '{new_clean}' already exists")
    watchlists[new_clean] = watchlists.pop(old_clean)
    return save_watchlists(watchlists)


def delete_watchlist(name: str) -> dict[str, str]:
    clean_name = name.strip()
    watchlists = load_watchlists()
    watchlists.pop(clean_name, None)
    saved = save_watchlists(watchlists)
    if clean_name:
        delete_view(clean_name)
    return saved


# ----------------------------- Column views (Pass D) -----------------------

# Persistence for user-defined column views (which columns to show + their
# order). Built-in presets live in the frontend (BUILTIN_VIEWS) and are
# never written here; this file only stores user-created custom views and
# the currently-active view name (global, applies to all portfolios).
_COLUMN_VIEWS_FILE = _repo_root() / ".portfolio_tracker_column_views.json"
_COLUMN_VIEWS_LOCK = threading.Lock()
_BUILTIN_COLUMN_VIEW_ALIASES = {
  "IB View": "Fundamentals",
  "Trader View": "Momentum",
}
_BUILTIN_COLUMN_VIEW_NAMES = {"Default", "Fundamentals", "Momentum"}


def _normalize_builtin_column_view_name(name: str) -> str:
  clean = (name or "Default").strip() or "Default"
  return _BUILTIN_COLUMN_VIEW_ALIASES.get(clean, clean)


def _column_views_path() -> Path:
    return Path(_COLUMN_VIEWS_FILE)


def _read_column_views_raw() -> dict:
    path = _column_views_path()
    if not path.exists():
        return {"custom_views": {}, "active_view": "Default"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"custom_views": {}, "active_view": "Default"}
    if not isinstance(data, dict):
        return {"custom_views": {}, "active_view": "Default"}
    cv = data.get("custom_views") if isinstance(data.get("custom_views"), dict) else {}
    active = _normalize_builtin_column_view_name(str(data.get("active_view") or "Default"))
    cleaned: dict[str, dict] = {}
    for name, entry in cv.items():
        if not isinstance(entry, dict):
            continue
        cols = entry.get("columns")
        if not isinstance(cols, list):
            continue
        clean_cols = [str(c) for c in cols if isinstance(c, (str, int))]
        cleaned[str(name).strip()] = {
            "columns": clean_cols,
            "created_at": entry.get("created_at"),
        }
    return {"custom_views": cleaned, "active_view": active}


def _write_column_views_raw(raw: dict) -> None:
    body = json.dumps(raw, ensure_ascii=True, indent=2, sort_keys=True)
    _column_views_path().write_text(body + "\n", encoding="utf-8")


def load_column_views() -> dict:
    with _COLUMN_VIEWS_LOCK:
        return _read_column_views_raw()


def upsert_column_view(name: str, columns: list) -> dict:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("view name required")
    if clean_name in _BUILTIN_COLUMN_VIEW_NAMES:
        raise ValueError(f"'{clean_name}' is a built-in view and cannot be overwritten")
    if not isinstance(columns, list) or not columns:
        raise ValueError("columns must be a non-empty list")
    clean_cols = [str(c) for c in columns if isinstance(c, (str, int))]
    with _COLUMN_VIEWS_LOCK:
        raw = _read_column_views_raw()
        existing = raw["custom_views"].get(clean_name) or {}
        raw["custom_views"][clean_name] = {
            "columns": clean_cols,
            "created_at": existing.get("created_at") or datetime.now(timezone.utc).isoformat(),
        }
        _write_column_views_raw(raw)
        return raw


def delete_column_view(name: str) -> dict:
    clean_name = (name or "").strip()
    if clean_name in _BUILTIN_COLUMN_VIEW_NAMES:
        raise ValueError(f"'{clean_name}' is a built-in view and cannot be deleted")
    with _COLUMN_VIEWS_LOCK:
        raw = _read_column_views_raw()
        raw["custom_views"].pop(clean_name, None)
        # If the deleted view was active, fall back to Default.
        if raw.get("active_view") == clean_name:
            raw["active_view"] = "Default"
        _write_column_views_raw(raw)
        return raw


def set_active_column_view(name: str) -> dict:
    clean_name = _normalize_builtin_column_view_name(name)
    with _COLUMN_VIEWS_LOCK:
        raw = _read_column_views_raw()
        if clean_name not in _BUILTIN_COLUMN_VIEW_NAMES and clean_name not in raw["custom_views"]:
            raise ValueError(f"unknown view '{clean_name}'")
        raw["active_view"] = clean_name
        _write_column_views_raw(raw)
        return raw


# ----------------------------- Symbol resolution --------------------------

_TICKER_SHAPE = re.compile(r"^[A-Z0-9][A-Z0-9.\-\^=]{0,9}$")

# Exchange prefix → yfinance suffix.  Lets users paste e.g. `XETRA.SAP`,
# `EU.SAP`, `NASDAQ.MSFT` or `Euronext Amsterdam.IMAE` without hand-massaging
# the Yahoo suffix.  Empty string means "US listing, no suffix".
_EXCHANGE_SUFFIX: dict[str, str] = {
    # ---- United States (Yahoo uses bare tickers, no suffix)
    "NASDAQ": "", "NMS": "", "NSDQ": "", "NDAQ": "",
    "NYSE": "", "NYS": "", "NYQ": "",
    "AMEX": "", "ASE": "", "ARCA": "", "ARCX": "", "PCX": "",
    "BATS": "", "CBOE": "", "OTC": "", "OTCQX": "", "OTCQB": "",
    "US": "", "USA": "",
    # ---- Europe — DACH + Northern Europe
    "EU": ".DE",     # default European → XETRA
    "XETRA": ".DE", "XETR": ".DE", "GER": ".DE", "DE": ".DE",
    "FRA": ".F",     # Frankfurt
    "BER": ".BE",    # Berlin
    "MUN": ".MU",    # Munich
    "HAM": ".HM",    # Hamburg
    "STU": ".SG",    # Stuttgart
    "SWX": ".SW", "SIX": ".SW", "CH": ".SW",
    "VIE": ".VI", "AT": ".VI",
    # ---- Europe — Western / Southern (Euronext family)
    "LSE": ".L", "LON": ".L", "UK": ".L", "GB": ".L",
    "EPA": ".PA", "PAR": ".PA", "FR": ".PA",
    "EURONEXT": ".PA",                         # bare "Euronext" → Paris
    "EURONEXT PARIS": ".PA",
    "AMS": ".AS", "NL": ".AS",
    "EURONEXT AMSTERDAM": ".AS",
    "BRU": ".BR", "BE": ".BR",
    "EURONEXT BRUSSELS": ".BR",
    "LIS": ".LS", "PT": ".LS",
    "EURONEXT LISBON": ".LS",
    "EURONEXT MILAN": ".MI",
    "BIT": ".MI", "MIL": ".MI", "IT": ".MI", "BORSA ITALIANA": ".MI",
    "BME": ".MC", "MAD": ".MC", "ES": ".MC", "BOLSA MADRID": ".MC",
    "ATH": ".AT", "GR": ".AT", "ATHEX": ".AT", "ASEX": ".AT",
    # ---- Europe — Nordics + Baltics
    "STO": ".ST", "OMX": ".ST", "SE": ".ST", "STOCKHOLM": ".ST",
    "HEL": ".HE", "FI": ".HE", "HELSINKI": ".HE",
    "CPH": ".CO", "DK": ".CO", "COPENHAGEN": ".CO",
    "OSL": ".OL", "NO": ".OL", "OSLO": ".OL",
    "ICE": ".IC", "REYKJAVIK": ".IC",
    "WSE": ".WA", "PL": ".WA", "WARSAW": ".WA",
    "PRA": ".PR", "CZ": ".PR", "PRAGUE": ".PR",
    "BUD": ".BD", "HU": ".BD", "BUDAPEST": ".BD",
    # ---- Other regions
    "TYO": ".T", "JP": ".T", "TOKYO": ".T",
    "HKG": ".HK", "HK": ".HK", "HKEX": ".HK", "HONG KONG": ".HK",
    "SHA": ".SS", "SSE": ".SS", "SHANGHAI": ".SS",
    "SHE": ".SZ", "SZSE": ".SZ", "SHENZHEN": ".SZ",
    "TSX": ".TO", "TO": ".TO", "TORONTO": ".TO",
    "TSXV": ".V", "TSX VENTURE": ".V",
    "ASX": ".AX", "AU": ".AX", "AUSTRALIA": ".AX",
    "NZX": ".NZ", "NZ": ".NZ",
    "NSE": ".NS",
    "BSE": ".BO",
    "JSE": ".JO", "ZA": ".JO", "JOHANNESBURG": ".JO",
    "SGX": ".SI", "SG": ".SI", "SINGAPORE": ".SI",
    "KRX": ".KS", "KR": ".KS", "KOREA": ".KS",
    "TWSE": ".TW", "TW": ".TW", "TAIWAN": ".TW",
    "BMV": ".MX", "MX": ".MX",
    "SAO": ".SA", "B3": ".SA", "BR": ".SA",
    "BVL": ".LM",
    "TASE": ".TA", "IL": ".TA",
}

# Tail-symbol validator: alnum + dot + dash, plus spaces inside a tail are
# disallowed (we trim the tail). The prefix may contain ASCII letters,
# whitespace and a few common separators (so "Euronext Amsterdam" works).
_TAIL_OK = re.compile(r"[A-Z0-9.\-]+")


def _try_prefix_pair(head: str, tail: str) -> tuple[str, str] | None:
    """Single-direction prefix lookup: head is the exchange/region token,
    tail is the bare ticker. Returns ``(yf_symbol, suffix)`` or ``None``."""
    head_up = " ".join(head.strip().upper().split())   # collapse spaces
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
    """Map prefixed forms into ``(symbol, suffix)``. Accepts BOTH orderings
    so users can paste whichever feels natural:

        XETRA.SAP   SAP.XETRA   DE.SAP   SAP.DE   EU.SAP   SAP.EU
        NASDAQ.MSFT MSFT.NASDAQ
        Euronext Amsterdam.IMAE   IMAE.Euronext Amsterdam

    Forward (`prefix.ticker`) is tried first to preserve the original syntax;
    reverse (`ticker.prefix`) is the fallback. Multi-word exchange names use
    ``rpartition`` for the reverse split so `IMAE.Euronext Amsterdam` works.

    Returns ``None`` when neither ordering matches a known prefix (caller
    falls through to the existing ticker / fuzzy / search logic). ``suffix``
    is the Yahoo suffix appended to the tail (empty string for US listings).
    """
    if not entry or "." not in entry:
        return None
    head, _, tail = entry.partition(".")
    forward = _try_prefix_pair(head, tail)
    if forward is not None:
        return forward
    # Reverse ordering: swap head/tail. Covers `SAP.XETRA`, `SAP.EU`,
    # `MSFT.NASDAQ`, `IMAE.Euronext Amsterdam`, etc. For multi-dot inputs we
    # still only swap once (the trailing token is the exchange) — anything
    # weirder falls through to the ticker/fuzzy/search path.
    return _try_prefix_pair(tail, head)


def _looks_like_ticker(s: str) -> bool:
    s = s.strip()
    if not s or " " in s:
        return False
    if any(c.islower() for c in s):
        return False
    return bool(_TICKER_SHAPE.match(s))


# Per-process cache: entry-string → final resolved Yahoo symbol. Keeps the
# fast_info + yf.Search refinement cost a one-time hit per unique input.
_RESOLVED_CACHE: dict[str, str] = {}


def _symbol_db_lookup(entry: str, min_score: float = 72.0) -> str | None:
    """Consult the local symbol database before round-tripping to the
    upstream search. Returns a provider-format ticker on confident hit,
    or ``None`` to let the caller fall through.

    The DB module + the sqlite file are both OPTIONAL — if either is
    missing the dashboard still works, just without the local fast-path.
    Run ``python build_symbol_db.py`` once to populate it.
    """
    try:
        import symbol_db
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
    """Cheap existence check via `fast_info.last_price`. Single network round
    trip — much faster than `.history()`."""
    try:
        fi = yf.Ticker(sym).fast_info
        v = getattr(fi, "last_price", None)
        return v is not None and isinstance(v, (int, float)) and math.isfinite(v) and v > 0
    except Exception:
        return False


def _refine_via_search(tail: str, expected_suffix: str) -> str | None:
    """Use yf.Search to refine a tail that doesn't match a real Yahoo symbol
    (e.g. `LSE.SHELL` → search "SHELL" + prefer ".L" → `SHEL.L`)."""
    try:
        search = yf.Search(tail, max_results=10, news_count=0)
        quotes = getattr(search, "quotes", None) or []
    except Exception:
        return None
    suf = expected_suffix.upper()
    # Pass 1: result whose symbol ends with the expected suffix.
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
    # Pass 2: any result.
    for q in quotes:
        if isinstance(q, dict) and q.get("symbol"):
            return str(q["symbol"]).strip()
    return None


def _normalize_google_colon(entry: str) -> str:
    """Convert Google Finance-style ``EXCHANGE: TICKER`` (with or without
    space after the colon) into the dot-separated form the prefix parser
    already understands. Examples:

        "BME: SAN"        -> "BME.SAN"
        "NASDAQ: MSFT"    -> "NASDAQ.MSFT"
        "BME:SAN"         -> "BME.SAN"
        "NYSE: BRK.B"     -> "NYSE.BRK.B"   (preserves inner dots)

    A colon inside a tail is unusual but safely passes through (we only
    swap the FIRST colon). Returns the entry unchanged when no colon.
    """
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

    # Google Finance-style "EXCHANGE: TICKER" → normalise to "EXCHANGE.TICKER"
    # so the prefix parser handles it. Keep the original `entry` as the cache
    # key so the user gets the same answer next time they paste it.
    normalised = _normalize_google_colon(entry)

    # Exchange-prefixed forms (XETRA.SAP, NASDAQ.MSFT, BME.SAN, …).
    mapped = _normalize_exchange_prefix(normalised)
    if mapped is not None:
        symbol, suffix = mapped
        # Literal mapping wins if Yahoo recognises it.
        if _yf_symbol_has_data(symbol):
            _RESOLVED_CACHE[entry] = symbol
            return symbol
        # Otherwise search for the tail with the prefix's market as a hint.
        tail = symbol[: -len(suffix)] if suffix and symbol.endswith(suffix) else symbol
        refined = _refine_via_search(tail, suffix)
        if refined and _yf_symbol_has_data(refined):
            _RESOLVED_CACHE[entry] = refined
            return refined
        _RESOLVED_CACHE[entry] = symbol  # give up; row will surface as "no data"
        return symbol

    if _looks_like_ticker(entry):
        upper = entry.upper()
        _RESOLVED_CACHE[entry] = upper
        return upper

    # Local symbol DB lookup — instant, no network. Handles "microsoft",
    # "DaVita", typos like "Microsft", etc. Only the high-confidence hit
    # short-circuits; weaker matches let yf.Search take over.
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

      # Momentum / trading indicators derived from the same price series.
      out["rsi_14"] = _rsi(close, 14)
      out["macd_hist_pct"] = _macd_hist_pct(close)
      out["bb_pct_b"] = _bollinger_pct_b(close)

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

      # Pass D — fundamentals & analyst columns. All sourced from the
      # info dict that _safe_info() already returned; no extra calls.
      out["forward_pe"] = _safe_num(info.get("forwardPE"))
      out["peg"] = _safe_num(info.get("pegRatio") or info.get("trailingPegRatio"))
      out["ev_ebitda"] = _safe_num(info.get("enterpriseToEbitda"))
      out["ev_revenue"] = _safe_num(info.get("enterpriseToRevenue"))
      out["beta"] = _safe_num(info.get("beta"))
      out["dividend_yield"] = _normalize_dividend_yield(
        info.get("dividendYield"),
        price=last,
        dividend_rate=info.get("dividendRate"),
        trailing_yield=info.get("trailingAnnualDividendYield"),
        trailing_rate=info.get("trailingAnnualDividendRate"),
      )
      out["operating_margin"] = _safe_num(info.get("operatingMargins"))
      out["debt_equity"] = _safe_num(info.get("debtToEquity"))
      out["current_ratio"] = _safe_num(info.get("currentRatio"))
      out["recommendation_mean"] = _safe_num(info.get("recommendationMean"))
      out["target_mean_price"] = _safe_num(info.get("targetMeanPrice"))

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


# ----------------------------- FX -----------------------------------------
# Spot rates and basket-index history for the topbar denomination selector.
# yfinance pairs are `<BASE><QUOTE>=X` where the close is QUOTE per BASE.

SUPPORTED_FX: list[str] = [
    "USD", "EUR", "GBP", "JPY", "CHF",
    "CAD", "AUD", "NZD", "CNY", "ZAR",
    "MXN", "SGD", "HKD", "INR",
]
# Major basket used by the hover currency-index chart. Whichever currency the
# user is hovering over is excluded; the remaining six form the basket.
_FX_BASKET_MAJORS: list[str] = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD"]

_FX_RATES_CACHE: dict[str, tuple[float, dict]] = {}
_FX_HIST_CACHE: dict[str, tuple[float, list]] = {}
_FX_CCY_HIST_CACHE: dict[str, tuple[float, pd.Series]] = {}
_FX_RATES_TTL = 1800.0   # 30 min for spot
_FX_HIST_TTL = 14400.0   # 4 h for the 1Y index series
_FX_CCY_HIST_TTL = 14400.0  # 4 h for per-ccy USD series (analytics FX adjustment)


def _norm_ccy_for_fx(ccy: str) -> str:
    """Normalize subunit currencies: GBp/GBX→GBP, ZAc→ZAR."""
    c = (ccy or "USD").upper()
    if c in ("GBP", "GBX"):
        return "GBP"
    if c == "ZAC":
        return "ZAR"
    return c


def _fx_usd_series(ccy: str, period_yf: str) -> "pd.Series | None":
    """Daily close for {CCY}USD=X (USD per 1 unit of ccy). None for USD. Cached 4 h."""
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
    sym_closes: "pd.DataFrame",
    by_sym: dict,
    display_ccy: str,
    period_yf: str,
) -> "pd.DataFrame":
    """Re-express every column of sym_closes in display_ccy.

    Multiplies each price series by (USD_per_stock_ccy / USD_per_display_ccy).
    GBp pence-vs-pound scaling cancels in return ratios so no special handling needed.
    """
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
        return sym_closes  # all USD → display USD, no-op

    # Bulk download all needed {CCY}USD=X pairs in one call.
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

    # Per-pair fallback for any still missing.
    for c in need:
        if c not in fx_hist:
            s = _fx_usd_series(c, period_yf)
            if s is not None and not s.empty:
                fx_hist[c] = s

    disp_usd = fx_hist.get(display_ccy)  # None ↔ display_ccy is USD
    idx = sym_closes.index
    result = sym_closes.copy()

    for s in syms:
        stock_ccy = sym_ccy[s]
        if stock_ccy == display_ccy:
            continue
        stock_usd = fx_hist.get(stock_ccy)  # None ↔ stock_ccy is USD
        if stock_usd is None and disp_usd is None:
            continue
        if stock_usd is None:
            # USD stock → non-USD display
            fx = (1.0 / disp_usd).reindex(idx, method="ffill").bfill().fillna(1.0)
        elif disp_usd is None:
            # non-USD stock → USD display
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
    """Return {ccy: rate} where rate = `ccy` per 1 `base`. Cached."""
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
    # Per-symbol fallback for any pair the bulk fetch missed.
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
    # yfinance returns empty for "1y" on many FX pairs; fetch a longer window
    # and slice client-side to keep the requested view honest.
    "1y": ("2y", 365),
    "1Y": ("2y", 365),
    "6mo": ("6mo", None),
    "3mo": ("3mo", None),
    "2y": ("2y", None),
    "5y": ("5y", None),
    "max": ("max", None),
}

def fx_index_history(base: str, period: str = "1y") -> list:
    """Return [[ts_ms, level], ...] for a synthetic trade-weighted index of
    `base` vs the other six majors (USD/EUR/GBP/JPY/CHF/CAD/AUD basket).
    Level is normalised to 100 at the start of the window; rising = `base`
    is strengthening vs the basket.
    """
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
        # For non-major bases (e.g. ZAR), benchmark against the full G7 majors.
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
    # If bulk fails (rate-limited), retry sequentially with a small jittered
    # back-off between pairs — this is the path that gets hit on rapid hover
    # bursts. A handful of paced calls is dramatically more reliable than one
    # parallel volley that the upstream throttles.
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
        # Short negative cache (10s) — long enough to absorb a hover burst
        # without re-firing the same failing batch, short enough that the
        # next deliberate hover after a rate-limit window gets fresh data.
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
        # Cache empties briefly so a transient yfinance hiccup doesn't lock us
        # out of fresh data for the full TTL.
        _FX_HIST_CACHE[key] = (time.time() - _FX_HIST_TTL + 60.0, [])
        return []
    if slice_days is not None and len(combined) > 0:
        cutoff = combined.index[-1] - pd.Timedelta(days=slice_days)
        sliced = combined[combined.index >= cutoff]
        if not sliced.empty:
            combined = sliced
        # Re-base each pair so the level starts at 100 at the slice start.
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
    out["dividend_yield"] = _normalize_dividend_yield(
      info.get("dividendYield"),
      price=out.get("price") or info.get("currentPrice") or info.get("regularMarketPrice") or info.get("last_price"),
      dividend_rate=info.get("dividendRate"),
      trailing_yield=info.get("trailingAnnualDividendYield"),
      trailing_rate=info.get("trailingAnnualDividendRate"),
    )
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


_BULK_CLOSE_CACHE: dict[tuple[str, str], tuple[float, "pd.Series | None"]] = {}
_BULK_CLOSE_TTL = 1800.0  # 30 min
_BULK_CLOSE_NEG_TTL = 60.0  # empty results re-tried after 60s
_BULK_CLOSE_MISS = object()  # sentinel — distinguish "cache miss" from a valid Series


def _bulk_close_get_cached(sym: str, period_yf: str):
    """Return a cached Close Series (possibly empty), or the _BULK_CLOSE_MISS sentinel
    when there is no usable entry (never cached, or TTL expired)."""
    hit = _BULK_CLOSE_CACHE.get((sym, period_yf))
    if hit is None:
        return _BULK_CLOSE_MISS
    ts, ser = hit
    is_empty = ser is None or ser.empty
    ttl = _BULK_CLOSE_NEG_TTL if is_empty else _BULK_CLOSE_TTL
    if time.time() - ts > ttl:
        return _BULK_CLOSE_MISS
    return ser


def _bulk_close_put(sym: str, period_yf: str, ser: "pd.Series | None") -> None:
    _BULK_CLOSE_CACHE[(sym, period_yf)] = (time.time(), ser)


def _bulk_close(symbols: list[str], period: str) -> pd.DataFrame:
    """Return a DataFrame of Close prices indexed by date, columns = symbols
    (those that returned data).

    Caches per-symbol so reload/period-mix calls reuse data. Retries missing
    symbols via per-ticker downloads with a small delay between calls so
    yfinance's rate-limiter is less likely to drop them on the floor."""
    if not symbols:
        return pd.DataFrame()
    period_yf = _PERIOD_YF.get(period.upper(), "1y")

    out: dict[str, pd.Series] = {}
    to_fetch: list[str] = []
    for s in symbols:
        cached = _bulk_close_get_cached(s, period_yf)
        if cached is _BULK_CLOSE_MISS:
            to_fetch.append(s)
        elif cached is not None and not cached.empty:
            out[s] = cached
        # else: cached as empty within neg-TTL — skip retrying

    if to_fetch:
        try:
            df = yf.download(
                tickers=to_fetch,
                period=period_yf,
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                progress=False,
            )
        except Exception:
            df = None
        got: set[str] = set()
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                for s in to_fetch:
                    try:
                        col = df[s]["Close"].dropna()
                    except (KeyError, ValueError):
                        continue
                    if not col.empty:
                        out[s] = col
                        _bulk_close_put(s, period_yf, col)
                        got.add(s)
            else:
                # Single-symbol shape: columns are OHLCV.
                try:
                    col = df["Close"].dropna()
                    if not col.empty:
                        out[to_fetch[0]] = col
                        _bulk_close_put(to_fetch[0], period_yf, col)
                        got.add(to_fetch[0])
                except (KeyError, ValueError):
                    pass

        missing = [s for s in to_fetch if s not in got]
        # Retry each missing symbol individually with backoff — yfinance often
        # drops a couple of symbols in a bulk request under rate-limit pressure.
        for i, s in enumerate(missing):
            delay = 0.4 + 0.2 * min(i, 6)
            time.sleep(delay)
            ser = None
            for attempt in range(3):
                try:
                    tk = yf.Ticker(s)
                    h = tk.history(period=period_yf, interval="1d", auto_adjust=True)
                    if h is not None and not h.empty:
                        col = h["Close"].dropna()
                        if not col.empty:
                            # Normalise to tz-naive DatetimeIndex so it lines up with
                            # the tz-naive index returned by yf.download() above.
                            try:
                                if getattr(col.index, "tz", None) is not None:
                                    col.index = col.index.tz_convert(None)
                            except Exception:
                                try:
                                    col.index = col.index.tz_localize(None)
                                except Exception:
                                    pass
                            ser = col
                            break
                except Exception:
                    pass
                time.sleep(0.6 * (attempt + 1))
            if ser is not None:
                out[s] = ser
                _bulk_close_put(s, period_yf, ser)
            else:
                _bulk_close_put(s, period_yf, pd.Series(dtype=float))

    if not out:
        return pd.DataFrame()
    return pd.concat(out, axis=1).sort_index()


def _analyst_for(symbol: str) -> dict:
    """Per-symbol analyst block. Pulls fields from `Ticker.info` plus the
    current-month row of `Ticker.recommendations` for the rating distribution.
    Cached for 30 min via the global analytics cache."""
    cache_key = f"analyst|{symbol}"
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit
    info: dict = {}
    try:
        tk = yf.Ticker(symbol)
        info = _safe_info(tk)
    except Exception:
        tk = None  # type: ignore
    price = _safe_num(info.get("currentPrice") or info.get("regularMarketPrice") or info.get("last_price"))
    out = {
        "mean_rating": _safe_num(info.get("recommendationMean")),
        "rec_key": (info.get("recommendationKey") or "").strip().lower() or None,
        "n_analysts": _safe_num(info.get("numberOfAnalystOpinions")),
        "target_mean": _safe_num(info.get("targetMeanPrice")),
        "target_median": _safe_num(info.get("targetMedianPrice")),
        "target_low": _safe_num(info.get("targetLowPrice")),
        "target_high": _safe_num(info.get("targetHighPrice")),
        "div_yield": _normalize_dividend_yield(
            info.get("dividendYield"),
            price=price,
            dividend_rate=info.get("dividendRate"),
            trailing_yield=info.get("trailingAnnualDividendYield"),
            trailing_rate=info.get("trailingAnnualDividendRate"),
        ),
        "ev_ebitda": _safe_num(info.get("enterpriseToEbitda")),
        "price": price,
        "currency": (info.get("financialCurrency") or info.get("currency") or "").upper() or None,
        "dist": None,  # populated below when recommendations are available
    }
    if tk is not None:
        try:
            recs = tk.recommendations
        except Exception:
            recs = None
        try:
            if recs is not None and not recs.empty:
                row = recs.iloc[0]
                d = {
                    "strongBuy": int(row.get("strongBuy", 0) or 0),
                    "buy": int(row.get("buy", 0) or 0),
                    "hold": int(row.get("hold", 0) or 0),
                    "sell": int(row.get("sell", 0) or 0),
                    "strongSell": int(row.get("strongSell", 0) or 0),
                }
                if any(d.values()):
                    out["dist"] = d
        except Exception:
            pass
    # If yfinance was rate-limited and we got almost nothing useful back,
    # cache only briefly so the next analytics run gets a real result.
    thin = (out["mean_rating"] is None and out["target_mean"] is None and out["price"] is None)
    _cache_put(cache_key, out, ttl=300.0 if thin else _CACHE_TTL_ANALYTICS)
    return out


def analyze_portfolio(rows: list[dict], weights_in: dict, period: str, display_ccy: str = "USD") -> dict:
    out = analyze_portfolios_multi(rows, {"__single__": weights_in or {}}, period, display_ccy=display_ccy)
    if isinstance(out, dict) and "error" in out:
        return out
    return (out or {}).get("__single__", {"error": "no result"})


def analyze_portfolios_multi(
    rows: list[dict],
    weight_sets: dict[str, dict],
    period: str,
    display_ccy: str = "USD",
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

    display_ccy = _norm_ccy_for_fx(display_ccy or "USD")

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
        key = "pf|" + "|".join(f"{s}:{weights[s]:.6f}" for s in sorted(symbols)) + f"|{period_u}|{display_ccy}"
        per_set_cache_keys[name] = key
        hit = _cache_get(key)
        if hit is not None:
            cached_results[name] = hit
    if len(cached_results) == len(normalized_sets):
        return cached_results

    closes = _bulk_close(symbols + ["SPY", "QQQ"], period_u)
    warnings: list[str] = []
    missing = [s for s in symbols if s not in closes.columns]
    if missing:
        warnings.append(f"No price history for: {', '.join(missing)}")
    spy = closes["SPY"].dropna() if "SPY" in closes.columns else None
    ndx = closes["QQQ"].dropna() if "QQQ" in closes.columns else None
    sym_closes = closes[[s for s in symbols if s in closes.columns]].dropna(how="all")
    if sym_closes.empty:
        return {"error": "no price history for portfolio", "warnings": warnings}

    active = [s for s in symbols if s in sym_closes.columns]
    if not active:
        return {"error": "no price history for portfolio", "warnings": warnings}

    # Convert all price series to display_ccy so all returns are FX-adjusted.
    # SPY is included in the same pass to share the FX download.
    period_yf = _PERIOD_YF.get(period_u, "1y")
    if display_ccy != "USD":
        combined = sym_closes.copy()
        if spy is not None and not spy.empty:
            combined = combined.join(spy.rename("__SPY__"), how="outer")
        if ndx is not None and not ndx.empty:
            combined = combined.join(ndx.rename("__QQQ__"), how="outer")
        combined_by_sym = dict(by_sym)
        combined_by_sym["__SPY__"] = {"currency": "USD"}
        combined_by_sym["__QQQ__"] = {"currency": "USD"}
        converted = _apply_fx_to_closes(combined, combined_by_sym, display_ccy, period_yf)
        sym_closes = converted[[c for c in converted.columns if c not in ("__SPY__", "__QQQ__")]]
        if "__SPY__" in converted.columns:
            spy = converted["__SPY__"].dropna()
        if "__QQQ__" in converted.columns:
            ndx = converted["__QQQ__"].dropna()

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

    ndx_aligned_raw = None
    ndx_ret_full = None
    if ndx is not None and not ndx.empty:
        ndx_aligned_raw = ndx.reindex(common_index).ffill().dropna()
        if len(ndx_aligned_raw) >= 2:
            ndx_ret_full = ndx_aligned_raw.pct_change().dropna()
        else:
            ndx_aligned_raw = None

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
        # Standard semi-deviation: sqrt(mean(min(r_i, 0)²)) over ALL N periods.
        # Using ret[ret<0].std() divides by N_neg-1 and subtracts mean_neg
        # instead of 0, which overstates Sortino by 20-60%. (Sortino & Price 1994)
        downside = math.sqrt((ret.clip(upper=0) ** 2).mean())
        sortino = float((ret.mean() * 252) / (downside * math.sqrt(252))) if downside > 0 else None
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
        # The SPY benchmark vs itself: beta=1, R²=1, tracking error=0.
        spy_stats_shared.setdefault("beta_spy", 1.0)
        spy_stats_shared.setdefault("r2_spy", 1.0)
        spy_stats_shared.setdefault("te_spy", 0.0)
    spy_points_shared = _series_to_points(spy_aligned_rebased) if spy_aligned_rebased is not None else []
    spy_var_full = float(spy_ret_full.var()) if (spy_ret_full is not None and len(spy_ret_full) > 30) else None

    ndx_stats_shared = {}
    ndx_aligned_rebased = None
    if ndx_aligned_raw is not None:
        ndx_aligned_rebased = 100.0 * ndx_aligned_raw / ndx_aligned_raw.iloc[0]
        ndx_stats_shared = _stats(ndx_ret_full, ndx_aligned_rebased)
        # NASDAQ vs SPY benchmark — compute beta/R²/TE if SPY available.
        if spy_ret_full is not None and spy_var_full and len(spy_ret_full) > 30:
            common_n = ndx_ret_full.index.intersection(spy_ret_full.index)
            if len(common_n) >= 30:
                rp = ndx_ret_full.loc[common_n]
                rb = spy_ret_full.loc[common_n]
                cov = float(rp.cov(rb))
                var_b = float(rb.var())
                if var_b:
                    ndx_stats_shared["beta_spy"] = cov / var_b
                corr = rp.corr(rb)
                if pd.notna(corr):
                    ndx_stats_shared["r2_spy"] = float(corr * corr)
                te = (rp - rb).std()
                if te and pd.notna(te):
                    ndx_stats_shared["te_spy"] = float(te * math.sqrt(252) * 100.0)
    ndx_points_shared = _series_to_points(ndx_aligned_rebased) if ndx_aligned_rebased is not None else []

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
        if not port_ret.empty:
          port_growth = (1.0 + port_ret).cumprod()
          port_val = pd.concat([
            pd.Series([1.0], index=[common_index[0]]),
            port_growth,
          ]) * 100.0
        else:
          port_val = pd.Series(dtype=float)
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
                    sec_blend_growth = (1.0 + sec_blend_ret).cumprod()
                    sec_blend = pd.concat([
                      pd.Series([1.0], index=[common_index[0]]),
                      sec_blend_growth,
                  ]) * 100.0

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
        dist_sum = {"strongBuy": 0.0, "buy": 0.0, "hold": 0.0, "sell": 0.0, "strongSell": 0.0}
        dist_w = 0.0
        holdings_out: list[dict] = []
        not_covered: list[dict] = []
        for s in active:
            blk = analyst_blocks.get(s, {})
            row = by_sym.get(s, {})
            w = weights.get(s, 0.0)
            mr = blk.get("mean_rating")
            tgt = blk.get("target_mean")
            tgt_med = blk.get("target_median")
            tgt_lo = blk.get("target_low")
            tgt_hi = blk.get("target_high")
            px = _safe_num(row.get("price")) or blk.get("price")
            na = blk.get("n_analysts")
            dist = blk.get("dist")
            upside = None
            if tgt and px and px > 0:
                upside = (float(tgt) / float(px) - 1.0) * 100.0
                upside_num += upside * w
                upside_w += w
            if mr is not None and math.isfinite(float(mr)):
                rating_num += float(mr) * w
                rating_w += w
            if na is not None and math.isfinite(float(na)):
                n_analysts_total += int(na)
            if dist and isinstance(dist, dict):
                tot_votes = sum(int(v or 0) for v in dist.values())
                if tot_votes > 0:
                    for k in dist_sum:
                        dist_sum[k] += float(dist.get(k, 0) or 0) * w
                    dist_w += w
            has_coverage = bool((na and na > 0) or mr is not None or tgt or dist)
            if has_coverage:
                holdings_out.append({
                    "symbol": s,
                    "name": row.get("name") or s,
                    "currency": row.get("currency") or blk.get("currency") or "USD",
                    "weight": w,
                    "price": px,
                    "target_mean": tgt,
                    "target_median": tgt_med,
                    "target_low": tgt_lo,
                    "target_high": tgt_hi,
                    "upside_pct": upside,
                    "mean_rating": mr,
                    "rec_key": blk.get("rec_key"),
                    "n_analysts": int(na) if (na is not None and math.isfinite(float(na))) else None,
                    "dist": dist,
                })
            else:
                not_covered.append({
                    "symbol": s,
                    "name": row.get("name") or s,
                    "weight": w,
                })
        holdings_out.sort(key=lambda h: (-(h.get("weight") or 0), h["symbol"]))
        dist_norm = None
        if dist_w > 0:
            dist_total_w = sum(dist_sum.values())
            if dist_total_w > 0:
                dist_norm = {k: (v / dist_total_w) * 100.0 for k, v in dist_sum.items()}
        analyst = {
            "mean_rating": (rating_num / rating_w) if rating_w > 0 else None,
            "rating_coverage_weight": rating_w,
            "weighted_target_upside_pct": (upside_num / upside_w) if upside_w > 0 else None,
            "target_coverage_weight": upside_w,
            "n_analysts_total": n_analysts_total,
            "distribution_pct": dist_norm,
            "distribution_weight": dist_w,
            "holdings": holdings_out,
            "not_covered": not_covered,
            "covered_count": len(holdings_out),
            "active_count": len(active),
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
            "display_ccy": display_ccy,
            "weights_applied": weights,
            "active_symbols": active,
            "missing_symbols": missing,
            "series": {
                "portfolio": _series_to_points(port_val),
                "spy": spy_points_shared,
                "nasdaq": ndx_points_shared,
                "sector_mix": _series_to_points(sec_blend) if sec_blend is not None else [],
                "drawdown": _series_to_points(drawdown),
            },
            "stats": {**pf_stats, "beta_spy": beta_spy, "r2_spy": r2_spy, "te_spy": te_spy},
            "spy_stats": spy_stats_shared,
            "nasdaq_stats": ndx_stats_shared,
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
        _cache_put(per_set_cache_keys[name], out_one, ttl=_CACHE_TTL_ANALYTICS)
        results[name] = out_one

    return results


# ----------------------------- Efficient frontier (MPT) --------------------

_MPT_LOOKBACK_YF = {"1Y": "1y", "3Y": "3y", "5Y": "5y", "10Y": "10y"}
_MPT_BUDGETS = {
    # cloud = number of Monte-Carlo portfolio configurations to evaluate
    # frontier = number of points sampled along the exact CLA frontier (cheap)
    # label = shown in the budget dropdown; mentions configs + estimated wall-time
    "fast":       {"cloud":    200_000, "frontier":  80, "label": "Fast — 200k configs (~1s)"},
    "standard":   {"cloud":  1_000_000, "frontier": 200, "label": "Standard — 1M configs (~4s)"},
    "thorough":   {"cloud":  3_500_000, "frontier": 400, "label": "Thorough — 3.5M configs (~14s)"},
    "exhaustive": {"cloud": 15_000_000, "frontier": 800, "label": "Exhaustive — 15M configs (~60s)"},
}

# Per-currency proxy ticker for the historical risk-free series. USD has a real
# yfinance ticker (^IRX — 13-week T-bill yield, quoted in %). Other currencies
# fall back to ^IRX as a placeholder until a better source per ccy is wired up;
# the source_note in the response makes the proxy explicit to the user.
_RF_YF: dict[str, str] = {
    "USD": "^IRX",
    "EUR": "^IRX",
    "GBP": "^IRX",
    "JPY": "^IRX",
    "CHF": "^IRX",
    "AUD": "^IRX",
    "CAD": "^IRX",
}


def _risk_free_history(ccy: str, lookback: str) -> dict:
    """Return a historical short-rate series for ``ccy`` over ``lookback``.

    Uses yfinance via the shared `_bulk_close` cache so repeated calls are
    cheap. The output is intended to drive the "Auto" risk-free button in
    the MPT overlay: a small sparkline plus a mean to pre-fill the input.
    """
    cc = (ccy or "USD").upper()
    lb = (lookback or "3Y").upper()
    if lb not in _MPT_LOOKBACK_YF:
        lb = "3Y"
    cache_key = f"rfhist|{cc}|{lb}"
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit

    ticker = _RF_YF.get(cc, "^IRX")
    period_yf = _MPT_LOOKBACK_YF[lb]
    try:
        df = _bulk_close([ticker], period_yf)
    except Exception:
        df = None
    series: list[tuple[str, float]] = []
    mean_pct = None
    current_pct = None
    if df is not None and not df.empty and ticker in df.columns:
        col = df[ticker].dropna()
        # ^IRX values are already in percent (e.g. 5.23 = 5.23%).
        for ts, val in col.items():
            try:
                series.append((ts.strftime("%Y-%m-%d"), float(val)))
            except Exception:
                continue
        if series:
            vals = [v for _, v in series]
            mean_pct = sum(vals) / len(vals)
            current_pct = vals[-1]

    note = (
        f"{ticker} (US 13-week T-bill yield)" if cc == "USD"
        else f"{ticker} proxy — no native yf series for {cc}; using US 13-week T-bill"
    )
    out = {
        "ccy": cc,
        "lookback": lb,
        "ticker": ticker,
        "series": series,
        "mean_pct": mean_pct,
        "current_pct": current_pct,
        "source_note": note,
    }
    # 4 h TTL: the short-rate moves slowly and we don't want to hammer yf
    # every time the user toggles "Auto".
    _cache_put(cache_key, out, ttl=14400.0)
    return out


def compute_efficient_frontier(
    rows: list[dict],
    *,
    lookback: str = "3Y",
    frequency: str = "weekly",
    display_ccy: str = "USD",
    rf: float = 0.04,
    budget: str = "standard",
    current_weights: dict | None = None,
    diversified: bool = False,
) -> dict:
    """Build the long-only efficient frontier for the supplied portfolio rows.

    Reuses the dashboard's bulk price/FX pipeline so cached close history
    benefits both analytics and MPT. Returns a JSON-friendly dict the
    frontend can render directly into the overlay's SVG chart.
    """
    t_total = time.perf_counter()
    lookback_u = (lookback or "3Y").upper()
    if lookback_u not in _MPT_LOOKBACK_YF:
        lookback_u = "3Y"
    period_yf = _MPT_LOOKBACK_YF[lookback_u]
    freq = (frequency or "weekly").lower()
    if freq not in mpt.FREQ_PER_YEAR:
        freq = "weekly"
    cfg = _MPT_BUDGETS.get((budget or "standard").lower(), _MPT_BUDGETS["standard"])
    display_ccy = _norm_ccy_for_fx(display_ccy or "USD")

    rows = [r for r in (rows or []) if r and r.get("symbol")]
    symbols = [str(r["symbol"]) for r in rows]
    if len(symbols) < 2:
        return {"error": "need at least 2 symbols with price history"}
    by_sym = {r["symbol"]: r for r in rows}

    # Fetch + FX-convert close history. We piggyback on _bulk_close's per-symbol
    # cache so repeat runs with the same lookback are essentially free.
    t_fetch = time.perf_counter()
    closes = _bulk_close(symbols, period_yf)
    if display_ccy != "USD":
        closes = _apply_fx_to_closes(closes, by_sym, display_ccy, period_yf)
    fetch_ms = int((time.perf_counter() - t_fetch) * 1000)

    if closes is None or closes.empty:
        return {"error": "no price history available", "fetch_ms": fetch_ms}

    # Compute returns at the requested frequency, then drop assets with too
    # little overlap (mpt.compute_returns handles the alignment).
    returns = mpt.compute_returns(closes, freq)
    if returns.shape[0] < 12 or returns.shape[1] < 2:
        return {"error": f"insufficient overlapping data ({returns.shape[0]} obs, "
                          f"{returns.shape[1]} assets)", "fetch_ms": fetch_ms}

    active = list(returns.columns)
    missing = [s for s in symbols if s not in active]

    mu, cov = mpt.annualize(returns, freq)
    cov_s = mpt.ledoit_wolf_shrink(cov, returns)

    t_opt = time.perf_counter()
    n_active = len(active)
    # Diversified mode: enforce a per-asset minimum weight. Floor scales with
    # portfolio size so it never exceeds 1/N (which would force equal-weight)
    # but is meaningful (≥0.5%) even for large baskets.
    if diversified and n_active >= 2:
        floor = min(1.0 / n_active, max(0.005, 0.5 / n_active))
    else:
        floor = 0.0
    if floor > 0:
        turning = mpt.critical_line_with_floor(mu, cov_s, floor)
    else:
        turning = mpt.critical_line(mu, cov_s)
    curve = mpt.frontier_curve(turning, mu, cov_s, n_samples=cfg["frontier"])
    tangency = mpt.tangency_portfolio(curve, rf=rf)
    # Use the explicit endpoints from the curve — frontier_curve now guarantees
    # curve[0] is the exact min-vol corner and curve[-1] is the exact max-ret
    # corner. That keeps the UI's white-circle anchors aligned with slider=0 /
    # slider=N-1 to floating-point precision.
    min_vol = curve[0] if curve else None
    max_ret = curve[-1] if curve else None
    # Inject anchor portfolios sampled along the CLA path so the MC cloud
    # explicitly contains points on the frontier — visual proof that the
    # rendered curve is achievable, and a self-check below to confirm it.
    anchor_W = mpt.anchor_samples(turning, n_per_segment=4)
    # Dense frontier samples used by the hybrid sampler to perturb portfolios
    # that sit near the optimal frontier — keeps the MC cloud's upper edge
    # hugging the curve instead of floating below the centroid mass.
    pert_basis = mpt.anchor_samples(turning, n_per_segment=12)
    cloud_arr = mpt.monte_carlo_cloud(
        mu, cov_s, n_samples=cfg["cloud"], rf=rf, anchor_weights=anchor_W,
        floor=floor, frontier_weights=pert_basis, perturbed_fraction=0.6,
    )
    n_samples_actual = int(cloud_arr.shape[0])
    optimize_ms = int((time.perf_counter() - t_opt) * 1000)

    # ---- CVaR frontier: minimum-CVaR portfolio at α = 99, 98, ..., 50.
    # Slider runs 99 → 50 (strict-tail on the left), so we emit confidence
    # levels in descending order to keep slider index 0 = CVaR99.
    t_cvar = time.perf_counter()
    cvar_alphas = [a / 100.0 for a in range(99, 49, -1)]
    cvar_frontier = mpt.cvar_curve(returns, cvar_alphas, mu, cov_s,
                                   rf=rf, floor=floor)
    cvar_ms = int((time.perf_counter() - t_cvar) * 1000)

    # Self-check: round-trip 5 evenly spaced frontier points through
    # portfolio_stats and confirm they match. Catches future drift between
    # the curve generator and the renderer's expectations.
    warnings: list[str] = []
    if curve:
        sample_idx = np.linspace(0, len(curve) - 1, 5, dtype=int)
        max_delta = 0.0
        for idx in sample_idx:
            p = curve[int(idx)]
            chk = mpt.portfolio_stats(p["weights"], mu, cov_s, rf=rf)
            d = max(abs(chk["vol"] - p["vol"]), abs(chk["ret"] - p["ret"]))
            if d > max_delta:
                max_delta = d
        if max_delta > 1e-9:
            warnings.append(f"frontier self-check delta {max_delta:.2e} > 1e-9")

    # Server-side subsample for JSON transit — full cloud_arr may be 15M
    # rows but the UI only needs ~200k to look dense. RNG seeded so the
    # subsample is deterministic for a given run.
    CLOUD_TRANSIT_MAX = 200_000
    if n_samples_actual > CLOUD_TRANSIT_MAX:
        rng = np.random.default_rng(42)
        keep = rng.choice(n_samples_actual, size=CLOUD_TRANSIT_MAX, replace=False)
        # Always include the anchors at the tail
        n_anchor = int(anchor_W.shape[0])
        if n_anchor > 0:
            anchor_idx = np.arange(n_samples_actual - n_anchor, n_samples_actual)
            keep = np.unique(np.concatenate([keep, anchor_idx]))
        cloud_arr = cloud_arr[keep]
    # Emit as a plain Python list of [vol, ret, sharpe] for JSON serialization
    cloud = cloud_arr.tolist()

    # Anchor points — equal, cap, and current — projected into (vol, ret).
    eq_w = {s: 1.0 / len(active) for s in active}
    caps = {s: max(0.0, float(by_sym.get(s, {}).get("market_cap") or 0.0)) for s in active}
    cap_total = sum(caps.values())
    if cap_total > 0:
        cap_w = {s: caps[s] / cap_total for s in active}
    else:
        cap_w = dict(eq_w)
    cur_w_in = current_weights or {}
    cur_total = sum(max(0.0, float(cur_w_in.get(s, 0.0))) for s in active)
    cur_w = ({s: max(0.0, float(cur_w_in.get(s, 0.0))) / cur_total for s in active}
             if cur_total > 0 else dict(eq_w))

    def _anchor(w: dict) -> dict:
        stats = mpt.portfolio_stats(w, mu, cov_s, rf=rf)
        return {"vol": stats["vol"], "ret": stats["ret"], "sharpe": stats["sharpe"],
                "weights": w}

    return {
        "symbols": active,
        "missing": missing,
        "frontier": curve,
        "cvar_frontier": cvar_frontier,
        "tangency": tangency,
        "min_vol": min_vol,
        "max_ret": max_ret,
        "cloud": cloud,
        "anchors": {
            "equal": _anchor(eq_w),
            "cap":   _anchor(cap_w),
            "current": _anchor(cur_w),
        },
        "mu":  {s: float(mu[s])    for s in active},
        "vol": {s: float(cov_s.iloc[i, i] ** 0.5) for i, s in enumerate(active)},
        "params": {
            "lookback": lookback_u,
            "frequency": freq,
            "display_ccy": display_ccy,
            "rf": rf,
            "budget": (budget or "standard").lower(),
            "diversified": bool(diversified),
            "floor": float(floor),
        },
        "meta": {
            "n_obs": int(returns.shape[0]),
            "n_assets": int(len(active)),
            "n_samples_target": int(cfg["cloud"]),
            "n_samples_actual": n_samples_actual,
            "cloud_transit_rows": int(len(cloud)),
            "n_anchor": int(anchor_W.shape[0]),
            "sampler_mix": "25% sparse-k · 30% Dir(0.05) · 25% Dir(0.3) · 20% Dir(1.0)",
            "fetch_ms": fetch_ms,
            "optimize_ms": optimize_ms,
            "cvar_ms": cvar_ms,
            "n_cvar": int(len(cvar_frontier)),
            "total_ms": int((time.perf_counter() - t_total) * 1000),
            "freq_label": freq,
            "budget_label": cfg["label"],
            "warnings": warnings,
        },
    }


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
  .topbar .status .status-name { color: var(--accent); font-weight: 700; font-size: 15px; letter-spacing: 0.1px; }
  .topbar .status .status-meta { color: var(--muted); font-size: 12px; margin-left: 6px; }
  .topbar .status .status-stale { color: var(--warn, #d97706); font-weight: 600; margin-left: 6px; }
  /* Shared tooltip used by every [data-tip] in the app. One <div> at body
     level; a small JS positioner picks above/below and clamps horizontally
     so the tip never clips off the viewport. The pseudo-element pattern
     this replaces could not be repositioned by JS, which is why tips
     anchored near a screen edge used to disappear under the chrome. */
  .app-tip {
    position: fixed; z-index: 9000; pointer-events: none;
    background: var(--bg-canvas); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 11px; font-size: 11.5px; font-weight: 500;
    line-height: 1.5; text-align: left; white-space: normal;
    overflow-wrap: anywhere; word-break: break-word;
    max-width: min(320px, calc(100vw - 32px));
    box-shadow: 0 6px 18px rgba(0,0,0,0.18);
    opacity: 0; transition: opacity 0.12s ease;
    left: 0; top: 0;
  }
  .app-tip.show { opacity: 1; }
  .app-tip .app-tip-arrow {
    position: absolute; width: 10px; height: 10px;
    background: var(--bg-canvas); border: 1px solid var(--border);
    transform: rotate(45deg); pointer-events: none;
  }
  .app-tip[data-side="below"] .app-tip-arrow {
    top: -6px; border-right: none; border-bottom: none;
  }
  .app-tip[data-side="above"] .app-tip-arrow {
    bottom: -6px; border-left: none; border-top: none;
  }
  /* Primary Portfolio button — accent fill, distinct call to action */
  .topbar button.portfolio-btn {
    background: var(--accent); color: #fff; border-color: var(--accent);
    font-weight: 600; padding: 0 14px; gap: 6px;
    display: inline-flex; align-items: center;
  }
  .topbar button.portfolio-btn:hover { filter: brightness(1.08); background: var(--accent); border-color: var(--accent); }
  .topbar button.portfolio-btn.active { box-shadow: 0 0 0 2px rgba(47, 129, 247, 0.30); }
  /* Danger style for delete buttons */
  .topbar button.danger, button.danger {
    color: var(--neg); border-color: rgba(248, 81, 73, 0.4);
  }
  button.danger:hover { background: rgba(248, 81, 73, 0.12); border-color: var(--neg); }
  button.danger:disabled { opacity: 0.4; cursor: not-allowed; }

  /* FX (denomination) selector */
  .fx-menu { position: relative; display: inline-block; }
  .fx-btn {
    height: 30px; padding: 0 10px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--bg-canvas);
    cursor: pointer; font-size: 12.5px; color: var(--text);
    display: inline-flex; align-items: center; gap: 6px;
    transition: background 0.12s, border-color 0.12s;
    font-variant-numeric: tabular-nums;
  }
  .fx-btn:hover { background: var(--bg-subtle); border-color: var(--accent); }
  .fx-btn.open { border-color: var(--accent); background: var(--bg-subtle); }
  .fx-btn-label { font-weight: 600; letter-spacing: 0.02em; }
  .fx-btn-caret { font-size: 10px; color: var(--muted); }
  .fx-dropdown {
    position: absolute; right: 0; top: 36px;
    background: var(--bg-canvas); border: 1px solid var(--border);
    border-radius: 10px; padding: 6px; min-width: 168px;
    box-shadow: 0 8px 28px rgba(0,0,0,0.18);
    display: none; z-index: 80;
  }
  .fx-dropdown.show { display: block; }
  .fx-opt {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; padding: 6px 10px; border-radius: 6px; cursor: pointer;
    font-size: 12.5px; color: var(--text);
  }
  .fx-opt:hover, .fx-opt.focused { background: var(--bg-subtle); }
  .fx-opt.selected { background: rgba(47, 129, 247, 0.12); color: var(--accent); font-weight: 600; }
  .fx-opt .fx-opt-code { font-weight: 600; letter-spacing: 0.02em; }
  .fx-opt .fx-opt-name { color: var(--muted); font-size: 11.5px; }
  .fx-hover {
    position: absolute; right: 180px; top: 36px;
    background: var(--bg-canvas); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px; width: 240px;
    box-shadow: 0 8px 28px rgba(0,0,0,0.18);
    display: none; z-index: 81;
  }
  .fx-hover.show { display: block; }
  .fx-hover-title { font-size: 12px; font-weight: 600; color: var(--text); margin-bottom: 4px; }
  .fx-hover-svg { width: 100%; height: 80px; display: block; }
  .fx-hover-foot { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .fx-hover-foot .pos { color: var(--pos); font-weight: 600; }
  .fx-hover-foot .neg { color: var(--neg); font-weight: 600; }

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
    width: min(860px, 96vw); max-height: 88vh; overflow-y: auto; color: var(--text);
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
  @media (max-width: 760px) { .info-grid { grid-template-columns: 1fr; } }
  .info-card {
    background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 8px;
    padding: 11px 13px;
  }
  .info-card.full { grid-column: 1 / -1; }
  .info-card-name {
    font-weight: 700; font-size: 12.5px; color: var(--text); margin-bottom: 5px;
  }
  .info-card-desc {
    font-size: 12px; color: var(--muted); line-height: 1.55;
  }
  .info-feature-list {
    margin: 8px 0 0; padding-left: 18px;
    color: var(--muted); font-size: 12px; line-height: 1.6;
  }
  .info-feature-list li + li { margin-top: 4px; }
  .info-card-why {
    margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--border);
    font-size: 11.5px; line-height: 1.55; color: var(--text);
  }
  .info-card-why .label {
    color: var(--muted); font-weight: 600; margin-right: 6px;
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

  /* ──────────────────────────────────────────────────────────────
     Loading chip ("lc") — a small terminal-flavored indicator that
     appears anywhere data is being fetched. Three moving parts:
       1. Braille spinner (cycles ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏) — the classic CLI vibe.
       2. Optional shimmer bar — accent gradient swept left-to-right.
       3. Optional meta counter — monospace tabular figure (e.g. 21·47).
     Use lcShow(target, text, {bar, meta}) / lcHide(target).
     ────────────────────────────────────────────────────────────── */
  .lc {
    display: inline-flex; align-items: center; gap: 7px;
    font-family: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 10.5px; letter-spacing: 0.02em; font-weight: 500;
    color: var(--muted);
    padding: 2px 9px 2px 8px; border-radius: 999px;
    background: rgba(125, 125, 125, 0.06);
    border: 1px solid var(--border);
    white-space: nowrap; user-select: none;
    vertical-align: middle;
  }
  .lc::before {
    content: "⠋";
    display: inline-block; width: 1ch;
    color: var(--accent);
    font-size: 12px; line-height: 1;
    animation: lc-spin 0.9s steps(1) infinite;
  }
  .lc .lc-bar {
    display: inline-block; width: 34px; height: 6px;
    background: rgba(125, 125, 125, 0.16); border-radius: 2px;
    overflow: hidden; position: relative;
  }
  .lc .lc-bar::after {
    content: ""; position: absolute; inset: 0;
    background: linear-gradient(90deg, transparent 0%, var(--accent) 50%, transparent 100%);
    transform: translateX(-100%);
    animation: lc-shimmer 1.15s linear infinite;
  }
  .lc .lc-meta {
    font-variant-numeric: tabular-nums;
    color: var(--text); opacity: 0.78;
    padding-left: 2px; border-left: 1px solid var(--border); margin-left: 1px;
  }
  .lc-block { display: flex; justify-content: center; align-items: center; padding: 22px 0; }
  @keyframes lc-spin {
    0%   { content: "⠋"; }
    11%  { content: "⠙"; }
    22%  { content: "⠹"; }
    33%  { content: "⠸"; }
    44%  { content: "⠼"; }
    55%  { content: "⠴"; }
    66%  { content: "⠦"; }
    77%  { content: "⠧"; }
    88%  { content: "⠇"; }
    100% { content: "⠏"; }
  }
  @keyframes lc-shimmer {
    from { transform: translateX(-100%); }
    to   { transform: translateX(120%); }
  }
  @media (prefers-reduced-motion: reduce) {
    .lc::before { animation: none; }
    .lc .lc-bar::after { animation: none; opacity: 0.55; transform: translateX(0); }
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
  .pf-tab .pf-tab-rename-input {
    background: var(--bg-canvas); color: var(--text);
    border: 1px solid var(--accent); border-radius: 4px;
    font: inherit; padding: 0 4px; min-width: 80px; max-width: 240px;
    outline: none;
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
  /* Preset pill bar — replaces the rigid 3-button mode toggle. Built/Cap pills
     stay first; named presets follow; trailing + / ✎ controls open the
     weights editor in new / edit mode. Overflow scrolls horizontally so a
     long preset list doesn't push the period selector off-row. */
  .pf-mode-bar {
    display: inline-flex; align-items: center; gap: 4px;
    max-width: min(680px, 60vw); overflow-x: auto; padding: 1px;
    scrollbar-width: none;
  }
  .pf-mode-bar::-webkit-scrollbar { display: none; }
  .pf-mode-pill {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 4px 10px; border-radius: 999px; cursor: pointer; user-select: none;
    background: var(--bg-canvas); border: 1px solid var(--border);
    color: var(--muted); font-size: 11.5px; font-weight: 600;
    white-space: nowrap; transition: all 0.12s ease;
  }
  .pf-mode-pill:hover { color: var(--text); border-color: var(--accent); }
  .pf-mode-pill.active {
    background: var(--accent); color: #fff; border-color: var(--accent);
  }
  .pf-mode-pill .pf-mode-edit {
    display: inline-flex; align-items: center; justify-content: center;
    width: 14px; height: 14px; margin-left: 2px; margin-right: -2px;
    border-radius: 50%; font-size: 9px; opacity: 0.75;
    background: rgba(255,255,255,0.18);
  }
  .pf-mode-pill .pf-mode-edit:hover { opacity: 1; }
  .pf-mode-pill.preset { font-style: normal; }
  .pf-mode-pill.icon {
    padding: 4px 8px; font-size: 14px; line-height: 1; color: var(--muted);
  }
  .pf-mode-pill.icon:hover { color: var(--accent); }
  .pf-mode-pill.dirty::after {
    content: "•"; color: var(--warn); margin-left: 2px; font-size: 14px; line-height: 0.5;
  }
  .pf-overlay-toggles {
    display: inline-flex; gap: 6px; margin-left: auto; flex-wrap: wrap;
    font-size: 11.5px;
  }
  .pf-overlay-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 9px; border-radius: 999px; cursor: pointer;
    background: var(--bg-canvas); border: 1px solid var(--border);
    color: var(--muted); user-select: none; transition: all 0.12s ease;
    position: relative;
  }
  .pf-overlay-pill:hover { color: var(--text); border-color: var(--accent); }
  .pf-overlay-pill .swatch {
    width: 9px; height: 9px; border-radius: 2px; display: inline-block;
    background: var(--muted); transition: background 0.12s;
  }
  .pf-overlay-pill[data-on="1"] {
    color: var(--text); background: var(--accent-soft, rgba(47,129,247,0.10));
    border-color: var(--accent); font-weight: 600;
  }
  .pf-overlay-pill[data-on="1"] .swatch.spy { background: #8b5cf6; }
  .pf-overlay-pill[data-on="1"] .swatch.ndx { background: #06b6d4; }
  .pf-overlay-pill[data-on="1"] .swatch.sec { background: #f59e0b; }
  .pf-overlay-pill[data-on="1"] .swatch.dd  { background: #f85149; }
  .pf-overlay-pill[data-on="0"] .swatch { opacity: 0.35; }
  .pf-overlay-pill .ovl-info {
    display: inline-flex; align-items: center; justify-content: center;
    width: 13px; height: 13px; border-radius: 50%; border: 1px solid currentColor;
    font-size: 9px; font-weight: 700; opacity: 0.55; cursor: default;
    font-family: -apple-system, "Segoe UI", sans-serif;
  }
  .pf-overlay-pill .ovl-info:hover { opacity: 1; }
  /* .pf-overlay-pill [data-tip] tooltips use the shared .app-tip system. */

  /* Risk & Return metric hover tooltips (LaTeX + explanation) */
  .pf-stat-row[data-info] { position: relative; }
  .pf-stat-row[data-info] .l { cursor: default; }
  .metric-tip-host[data-info] { position: relative; }
  .metric-tip-host[data-info] .k {
    cursor: default;
    text-decoration: underline dotted rgba(125, 125, 125, 0.75);
    text-underline-offset: 3px;
  }
  .pf-metric-tip {
    position: absolute; left: 0; top: 24px; z-index: 1000;
    width: 340px; max-width: 92vw;
    background: var(--bg-canvas); color: var(--text);
    border: 1px solid var(--border); border-radius: 10px;
    padding: 11px 13px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.20);
    font-size: 11.5px; line-height: 1.55;
    display: none;
  }
  .pf-stat-row[data-info]:hover .pf-metric-tip { display: block; }
  .metric-tip-host[data-info]:hover .pf-metric-tip { display: block; }
  .pf-metric-tip .mt-name { font-weight: 700; font-size: 12.5px; margin-bottom: 4px; }
  .pf-metric-tip .mt-formula { background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; margin: 4px 0 6px; overflow-x: auto; }
  .pf-metric-tip .mt-formula .katex { font-size: 1.0em; }
  .pf-metric-tip .mt-formula .katex-display { margin: 0 !important; }
  .pf-metric-tip .mt-desc { color: var(--text); margin-bottom: 4px; }
  .pf-metric-tip .mt-range { color: var(--muted); font-size: 11px; }
  .pf-metric-tip .mt-range b { color: var(--text); font-weight: 600; }
  /* Right-side metric: anchor tooltip to the right edge so it doesn't clip */
  .pf-stats .pf-stat-row:nth-child(2n)[data-info] .pf-metric-tip { left: auto; right: 0; }
  .m-kv .metric-tip-host .pf-metric-tip {
    top: calc(100% + 8px);
    width: min(320px, calc(100vw - 48px));
    max-width: min(320px, calc(100vw - 48px));
  }
  .m-kv .metric-tip-host.tip-right .pf-metric-tip { left: auto; right: 0; }

  /* Portfolio chart interaction (brush + crosshair tooltip) */
  .pf-chart-wrap svg .pf-cross { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 3 3; opacity: 0; pointer-events: none; }
  .pf-chart-wrap svg .pf-dot   { fill: var(--accent); stroke: var(--bg-canvas); stroke-width: 2; opacity: 0; pointer-events: none; }
  .pf-chart-wrap svg .pf-dot.spy { fill: #8b5cf6; }
  .pf-chart-wrap svg .pf-dot.ndx { fill: #06b6d4; }
  .pf-chart-wrap svg .pf-dot.sec { fill: #f59e0b; }
  .pf-chart-wrap svg .pf-sel { fill: var(--accent); opacity: 0.10; pointer-events: none; }
  .pf-chart-wrap .pf-tt {
    position: absolute; pointer-events: none; z-index: 5;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 8px;
    padding: 7px 10px; font-size: 11.5px; line-height: 1.5; color: var(--text);
    box-shadow: 0 4px 14px rgba(0,0,0,0.15);
    opacity: 0; transition: opacity 0.08s;
    min-width: 140px; white-space: nowrap;
  }
  .pf-chart-wrap .pf-tt.show { opacity: 1; }
  .pf-chart-wrap .pf-tt .tt-date { color: var(--muted); font-size: 11px; margin-bottom: 2px; }
  .pf-chart-wrap .pf-tt .tt-row { display: flex; justify-content: space-between; gap: 10px; }
  .pf-chart-wrap .pf-tt .tt-row b { font-weight: 700; font-variant-numeric: tabular-nums; }
  .pf-chart-wrap .pf-tt .pos { color: var(--pos); }
  .pf-chart-wrap .pf-tt .neg { color: var(--neg); }
  .pf-chart-info {
    margin-top: 6px; padding: 6px 10px;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 6px;
    font-size: 11.5px; color: var(--muted);
    display: flex; gap: 14px; flex-wrap: wrap;
  }
  .pf-chart-info b { color: var(--text); font-weight: 700; font-variant-numeric: tabular-nums; }
  .pf-chart-info .pos { color: var(--pos); }
  .pf-chart-info .neg { color: var(--neg); }
  .pf-chart-info .selection-hint { color: var(--muted); margin-left: auto; font-style: italic; }

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
  .pf-weights-foot button.danger {
    background: transparent; color: var(--neg); border-color: var(--neg);
  }
  .pf-weights-foot button.danger:hover { background: rgba(248, 81, 73, 0.10); }
  .pf-name-row {
    display: flex; align-items: center; gap: 6px;
    padding: 6px 14px 0; flex-wrap: wrap;
  }
  .pf-name-row input[type="text"] {
    flex: 1; min-width: 140px;
    padding: 5px 8px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg-subtle); color: var(--text);
    font-size: 12px;
  }
  .pf-name-row input[type="text"]:focus { outline: none; border-color: var(--accent); }
  .pf-name-row .pf-name-status {
    color: var(--muted); font-size: 11px; font-style: italic;
  }
  /* Inline overlay used for "Save as…" — renders fixed at top-level so it
     can appear over EITHER the weights modal or the MPT overlay. */
  .pf-inline-prompt {
    position: fixed; inset: 0; background: rgba(8,12,20,0.55);
    display: none; align-items: center; justify-content: center; z-index: 200;
    backdrop-filter: blur(2px);
  }
  .pf-weights-modal { position: relative; }
  .pf-inline-prompt.show { display: flex; }
  .pf-inline-prompt .pf-ip-box {
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; width: min(360px, 90%); display: flex; flex-direction: column; gap: 10px;
    box-shadow: 0 14px 32px rgba(0,0,0,0.32);
  }
  .pf-inline-prompt .pf-ip-title { font-weight: 700; font-size: 13px; }
  .pf-inline-prompt .pf-ip-desc {
    font-size: 11.5px; color: var(--muted); line-height: 1.45; margin: -2px 0 2px;
  }
  .pf-inline-prompt input {
    padding: 7px 9px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg-subtle); color: var(--text); font-size: 12.5px;
  }
  .pf-inline-prompt input:focus { outline: none; border-color: var(--accent); }
  .pf-inline-prompt .pf-ip-foot { display: flex; justify-content: flex-end; gap: 8px; }
  .pf-inline-prompt button {
    height: 28px; padding: 0 12px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg-subtle); color: var(--text);
    font-size: 12px; cursor: pointer;
  }
  .pf-inline-prompt button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .pf-inline-prompt .pf-ip-err { color: var(--neg); font-size: 11.5px; min-height: 14px; }

  /* MPT overlay (Portfolio Optimization workspace) — sits above everything,
     85% of viewport, backdrop blurs the dashboard so the user feels they're
     "drilling deeper" into a dedicated quantitative tool. */
  .pf-mpt-bg {
    position: fixed; inset: 0; z-index: 90;
    background: rgba(6, 10, 18, 0.55); backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    display: none; align-items: stretch; justify-content: center;
    padding: 32px;
  }
  .pf-mpt-bg.show { display: flex; }
  .pf-mpt-modal {
    width: 92vw; max-width: 1480px; height: 88vh; max-height: 920px;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 14px;
    box-shadow: 0 24px 60px rgba(0,0,0,0.45);
    display: flex; flex-direction: column; overflow: hidden;
  }
  .pf-mpt-head {
    display: flex; align-items: center; gap: 14px;
    padding: 12px 18px; border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, var(--bg-subtle), var(--bg-canvas));
  }
  .pf-mpt-head .pf-mpt-title { font-weight: 700; font-size: 14.5px; }
  .pf-mpt-head .pf-mpt-sub { color: var(--muted); font-size: 12px; }
  .pf-mpt-head .spacer { flex: 1; }
  .pf-mpt-close {
    width: 28px; height: 28px; border-radius: 50%; border: 1px solid var(--border);
    background: var(--bg-canvas); cursor: pointer; color: var(--muted); font-size: 13px;
  }
  .pf-mpt-close:hover { color: var(--text); border-color: var(--accent); }
  .pf-mpt-info-btn {
    width: 28px; height: 28px; padding: 0; display: inline-flex; align-items: center;
    justify-content: center; border-radius: 50%; border: 1px solid var(--border);
    background: var(--bg-canvas); cursor: pointer; color: var(--muted);
  }
  .pf-mpt-info-btn:hover { color: var(--accent); border-color: var(--accent); }
  /* About-MPT overlay needs to layer on top of pf-mpt-bg (z-index 90). */
  .pf-mpt-info-bg { z-index: 95; }
  .pf-mpt-controls {
    --mpt-ctl-h: 32px;
    display: grid; grid-template-columns: auto auto minmax(220px, 0.55fr) auto minmax(220px, 0.45fr) auto;
    gap: 12px 14px; align-items: end;
    padding: 12px 18px; border-bottom: 1px solid var(--border);
    background: var(--bg-subtle);
  }
  @media (max-width: 980px) {
    .pf-mpt-controls { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
  }
  .pf-mpt-ctl {
    display: flex; flex-direction: column; gap: 4px; font-size: 11.5px; min-width: 0;
  }
  .pf-mpt-ctl > label {
    color: var(--muted); font-weight: 600; font-size: 10px; letter-spacing: 0.06em;
    text-transform: uppercase; line-height: 14px;
  }
  /* Uniform-height controls strip: every interactive control in the bar uses
     --mpt-ctl-h so the four columns line up perfectly regardless of content. */
  .pf-mpt-ctl .seg,
  .pf-mpt-ctl input[type="number"],
  .pf-mpt-ctl .pf-mpt-select,
  .pf-mpt-ctl .pf-mpt-rf-wrap,
  .pf-mpt-run {
    height: var(--mpt-ctl-h); box-sizing: border-box;
  }
  .pf-mpt-ctl .seg {
    display: inline-flex; background: var(--bg-canvas); border: 1px solid var(--border);
    border-radius: 7px; overflow: hidden;
  }
  .pf-mpt-ctl .seg button {
    background: transparent; border: none; padding: 0 12px; font-size: 11.5px; color: var(--muted);
    cursor: pointer; border-right: 1px solid var(--border); line-height: 1;
  }
  .pf-mpt-ctl .seg button:last-child { border-right: none; }
  .pf-mpt-ctl .seg button:hover { color: var(--text); background: var(--bg-subtle); }
  .pf-mpt-ctl .seg button.active { background: var(--accent); color: #fff; }
  .pf-mpt-ctl input[type="number"] {
    padding: 0 9px; border-radius: 7px; border: 1px solid var(--border);
    background: var(--bg-canvas); color: var(--text); font-size: 12px;
    font-variant-numeric: tabular-nums; min-width: 0;
  }
  .pf-mpt-ctl input[type="number"]:focus { outline: none; border-color: var(--accent); }

  /* Risk-free input + inline sparkline + Auto button in a single shell. The
     sparkline gets its own horizontal lane between the number and the AUTO
     pill so it reads as a proper inline mini-chart, not a fading underline. */
  .pf-mpt-rf-wrap {
    position: relative; display: flex; align-items: center; gap: 6px;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 7px;
    padding: 3px 5px 3px 0; overflow: visible; min-width: 240px;
  }
  .pf-mpt-rf-wrap:focus-within { border-color: var(--accent); }
  .pf-mpt-rf-wrap input[type="number"] {
    width: 64px; flex: 0 0 auto; height: 100%; border: none; background: transparent;
    border-radius: 0; padding: 0 4px 0 8px; font-variant-numeric: tabular-nums;
  }
  .pf-mpt-rf-wrap input[type="number"]:focus { outline: none; }
  .pf-mpt-rf-spark-wrap {
    position: relative; flex: 1 1 auto; min-width: 130px; height: 100%;
    display: flex; align-items: center;
    border-left: 1px solid var(--border); border-right: 1px solid var(--border);
    padding: 0 6px; cursor: crosshair;
  }
  .pf-mpt-rf-spark {
    width: 100%; height: 100%; display: block; opacity: 0; transition: opacity 0.18s;
    cursor: crosshair;
  }
  .pf-mpt-rf-spark.show { opacity: 0.85; }
  .pf-mpt-rf-spark .rfs-area { fill: var(--accent); opacity: 0.16; }
  .pf-mpt-rf-spark .rfs-line { fill: none; stroke: var(--accent); stroke-width: 1.3;
    stroke-linejoin: round; stroke-linecap: round; }
  .pf-mpt-rf-spark .rfs-base { stroke: var(--muted); stroke-width: 0.6;
    stroke-dasharray: 2 2; opacity: 0.45; }
  .pf-mpt-rf-spark .rfs-dot  { fill: var(--accent); stroke: var(--bg-canvas); stroke-width: 0.7; }
  .pf-mpt-rf-spark .rfs-cross  { stroke: var(--muted); stroke-width: 0.5; opacity: 0; }
  .pf-mpt-rf-spark .rfs-cross.show { opacity: 0.7; }
  /* Rich hover card — multi-line, anchored to the cursor. */
  .pf-mpt-rf-spark-tip {
    position: absolute; pointer-events: none; opacity: 0;
    transform: translate(-50%, 10px);
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 7px;
    padding: 8px 11px; font-size: 11.5px; color: var(--text);
    font-variant-numeric: tabular-nums; transition: opacity 0.1s;
    box-shadow: 0 8px 22px rgba(0,0,0,0.35); z-index: 90;
    min-width: 220px; max-width: 320px; line-height: 1.45; white-space: normal;
  }
  .pf-mpt-rf-spark-tip.show { opacity: 1; }
  .pf-mpt-rf-spark-tip .tip-title { font-weight: 700; color: var(--text); margin-bottom: 2px; }
  .pf-mpt-rf-spark-tip .tip-sub { color: var(--muted); font-size: 10.5px; margin-bottom: 6px; }
  .pf-mpt-rf-spark-tip .tip-row { display: flex; justify-content: space-between; gap: 12px; }
  .pf-mpt-rf-spark-tip .tip-row .k { color: var(--muted); }
  .pf-mpt-rf-spark-tip .tip-row .v { font-weight: 600; }
  .pf-mpt-rf-spark-tip .tip-note { color: var(--muted); font-size: 10.5px; margin-top: 6px;
    padding-top: 5px; border-top: 1px dashed var(--border); }
  .pf-mpt-rf-auto {
    height: 22px; padding: 0 11px; border-radius: 5px; cursor: pointer;
    background: var(--bg-subtle); border: 1px solid var(--border); color: var(--muted);
    font-size: 10.5px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
    line-height: 1; flex-shrink: 0;
  }
  .pf-mpt-rf-auto:hover { color: var(--accent); border-color: var(--accent); }
  .pf-mpt-rf-auto.busy { opacity: 0.55; cursor: progress; }
  /* Inline info icon next to any control label (Risk-free, Allocation, …). */
  .pf-mpt-ctl > label { display: flex; align-items: center; gap: 6px; }
  .pf-mpt-rf-info {
    display: inline-flex; align-items: center; justify-content: center;
    width: 13px; height: 13px; border-radius: 50%; border: 1px solid var(--muted);
    color: var(--muted); font-size: 9px; font-weight: 700; line-height: 1;
    cursor: default; text-transform: none;
  }
  .pf-mpt-rf-info:hover { color: var(--accent); border-color: var(--accent); }
  /* Floating tooltip for frontier hover — appended to <body> so it isn't
     clipped by the overlay panel. */
  .pf-mpt-frontier-tip {
    position: fixed; pointer-events: none; opacity: 0; z-index: 200;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 7px;
    padding: 8px 10px; font-size: 11.5px; color: var(--text);
    box-shadow: 0 8px 22px rgba(0,0,0,0.4); min-width: 170px; max-width: 240px;
    font-variant-numeric: tabular-nums; transition: opacity 0.08s;
  }
  .pf-mpt-frontier-tip.show { opacity: 1; }
  .pf-mpt-frontier-tip .tip-title { font-weight: 700; margin-bottom: 4px; color: var(--text); }
  .pf-mpt-frontier-tip .tip-sub { color: var(--muted); margin-top: 5px; padding-top: 4px;
    border-top: 1px dashed var(--border); font-size: 10.5px; }
  .pf-mpt-frontier-tip .tip-row { display: flex; justify-content: space-between; gap: 12px; line-height: 1.45; }
  .pf-mpt-frontier-tip .tip-row .k { color: var(--muted); }
  .pf-mpt-frontier-tip .tip-row .v { font-weight: 600; }
  /* MPT overlay [data-tip] tooltips ride on the same shared .app-tip system
     as the topbar. The unified JS positioner handles viewport clamping for
     all of them — no per-overlay CSS overrides needed. */

  /* Custom-themed select to replace the native <select>. Built from a div +
     ul so the menu inherits the dashboard theme (border/colors/typography)
     and stays visually consistent with the segmented controls. */
  .pf-mpt-select {
    position: relative; display: flex; align-items: center; gap: 6px;
    padding: 0 8px 0 10px; border-radius: 7px; border: 1px solid var(--border);
    background: var(--bg-canvas); color: var(--text); font-size: 12px; cursor: pointer;
    user-select: none; line-height: 1;
  }
  .pf-mpt-select:focus, .pf-mpt-select.open { outline: none; border-color: var(--accent); }
  .pf-mpt-select-label { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .pf-mpt-select-caret { color: var(--muted); font-size: 10px; transition: transform 0.15s; }
  .pf-mpt-select.open .pf-mpt-select-caret { transform: rotate(180deg); }
  .pf-mpt-select-menu {
    position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 5;
    margin: 0; padding: 4px; list-style: none;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 7px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.22); font-size: 12px;
  }
  .pf-mpt-select-menu[hidden] { display: none; }
  .pf-mpt-select-menu li {
    padding: 6px 10px; border-radius: 5px; cursor: pointer; color: var(--text);
    font-variant-numeric: tabular-nums;
  }
  .pf-mpt-select-menu li:hover { background: var(--bg-subtle); }
  .pf-mpt-select-menu li[aria-selected="true"] { color: var(--accent); font-weight: 600; }

  .pf-mpt-run {
    padding: 0 18px; border-radius: 8px;
    background: var(--accent); color: #fff; border: 1px solid var(--accent);
    font-weight: 700; font-size: 12.5px; cursor: pointer; line-height: 1;
  }
  .pf-mpt-run:hover { filter: brightness(1.08); }
  .pf-mpt-run:disabled { opacity: 0.55; cursor: not-allowed; filter: none; }
  .pf-mpt-body {
    flex: 1; display: grid; grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
    gap: 14px; padding: 14px 18px; overflow: hidden;
  }
  @media (max-width: 980px) { .pf-mpt-body { grid-template-columns: 1fr; } }
  .pf-mpt-chartwrap {
    display: flex; flex-direction: column; gap: 8px;
    background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px; overflow: hidden;
  }
  .pf-mpt-chart { position: relative; flex: 1; min-height: 320px; }
  /* Two stacked canvases: base (axes + cloud + frontier) and overlay
     (selection ring + hover ghost). Both share the same backing-store size
     so nothing drifts. The overlay sits on top and is the only interactive
     surface for click + mousemove hit-testing. */
  .pf-mpt-cv {
    position: absolute; inset: 0; width: 100%; height: 100%; display: block;
  }
  .pf-mpt-cv-base { z-index: 1; pointer-events: none; }
  .pf-mpt-cv-overlay { z-index: 2; cursor: crosshair; pointer-events: auto; }
  .pf-mpt-chart .pf-mpt-status {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    z-index: 3; background: transparent;
  }
  .pf-mpt-chart .pf-mpt-status:empty { display: none; }

  /* Deterministic progress bar shown while /api/efficient-frontier is in
     flight. We know the expected duration from the budget, so the bar fills
     over that window and a 100 ms snap-to-100% finishes it on response. */
  .mpt-progress {
    width: min(420px, 80%); display: flex; flex-direction: column; gap: 6px;
    align-items: stretch; color: var(--muted); font-size: 11.5px;
  }
  .mpt-progress-track {
    position: relative; height: 6px; border-radius: 4px;
    background: var(--bg-subtle); border: 1px solid var(--border); overflow: hidden;
  }
  .mpt-progress-fill {
    position: absolute; top: 0; bottom: 0; left: 0; width: 0%;
    background: linear-gradient(90deg, var(--accent), rgba(9,105,218,0.45));
    transition: width 0s linear;
  }
  .mpt-progress.done .mpt-progress-fill { transition: width 0.15s ease-out !important; }
  .mpt-progress.fail .mpt-progress-fill { background: var(--neg); }
  .mpt-progress-label {
    display: flex; justify-content: space-between; gap: 12px;
    font-variant-numeric: tabular-nums;
  }
  .mpt-progress-text { color: var(--text); font-weight: 600; }
  .mpt-progress-timer { color: var(--muted); }
  .pf-mpt-chart .pf-mpt-tt {
    position: absolute; pointer-events: none; z-index: 5;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 8px;
    padding: 7px 10px; font-size: 11.5px; line-height: 1.5;
    box-shadow: 0 6px 16px rgba(0,0,0,0.22);
    opacity: 0; transition: opacity 0.08s;
  }
  .pf-mpt-chart .pf-mpt-tt.show { opacity: 1; }
  .pf-mpt-slider-row {
    display: flex; align-items: center; gap: 10px; font-size: 11.5px; color: var(--muted);
  }
  .pf-mpt-slider-row input[type="range"] { flex: 1; accent-color: var(--accent); }
  .pf-mpt-slider-row.cvar input[type="range"] { accent-color: #ea580c; }
  .pf-mpt-slider-row .pf-mpt-slider-label {
    min-width: 88px; font-weight: 600; color: var(--text);
  }
  .pf-mpt-slider-row.cvar .pf-mpt-slider-label { color: #ea580c; }
  .pf-mpt-slider-row .pf-mpt-slider-readout {
    min-width: 72px; text-align: right; font-variant-numeric: tabular-nums;
    color: var(--muted);
  }
  .pf-mpt-legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 11px; color: var(--muted); align-items: center; }
  .pf-mpt-legend > span {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 6px; border-radius: 5px; border: 1px solid transparent;
    cursor: pointer; user-select: none;
    transition: background-color 0.12s, border-color 0.12s;
  }
  .pf-mpt-legend > span[data-legend]:hover {
    background: var(--bg-subtle); border-color: var(--border);
  }
  .pf-mpt-legend > span.active {
    background: var(--bg-subtle); border-color: var(--accent);
  }
  .pf-mpt-legend > span.no-click { cursor: default; }
  .pf-mpt-legend > span.no-click:hover { background: transparent; border-color: transparent; }
  .pf-mpt-legend .lg-dot {
    display: inline-block; width: 10px; height: 10px; border-radius: 50%; vertical-align: middle;
  }
  .pf-mpt-legend .lg-swatch {
    display: inline-block; width: 14px; height: 12px; vertical-align: middle; flex-shrink: 0;
  }
  .pf-mpt-side {
    background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px; display: flex; flex-direction: column; gap: 10px; overflow-y: auto;
  }
  .pf-mpt-side h4 {
    margin: 0; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.4px; color: var(--text);
  }
  .pf-mpt-kv {
    display: grid; grid-template-columns: 1fr auto; gap: 4px 12px; font-size: 12px;
  }
  .pf-mpt-kv .k { color: var(--muted); }
  .pf-mpt-kv .v { font-weight: 600; font-variant-numeric: tabular-nums; }
  .pf-mpt-kv .v.pos { color: var(--pos); }
  .pf-mpt-kv .v.neg { color: var(--neg); }
  .pf-mpt-weights {
    display: flex; flex-direction: column; gap: 3px; font-size: 11.5px;
    max-height: 280px; overflow-y: auto;
  }
  .pf-mpt-weights .pf-mpt-wrow {
    display: grid; grid-template-columns: minmax(60px, 1fr) 4fr 48px; gap: 6px; align-items: center;
  }
  .pf-mpt-weights .pf-mpt-track {
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 3px; height: 8px; overflow: hidden;
  }
  .pf-mpt-weights .pf-mpt-fill {
    height: 100%; background: var(--accent);
  }
  .pf-mpt-weights .pf-mpt-val { font-variant-numeric: tabular-nums; text-align: right; color: var(--muted); }
  .pf-mpt-actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .pf-mpt-actions button {
    flex: 1; min-width: 110px; height: 32px; padding: 0 12px; border-radius: 7px;
    background: var(--bg-canvas); border: 1px solid var(--border); cursor: pointer; color: var(--text);
    font-size: 12px; font-weight: 600;
  }
  .pf-mpt-actions button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .pf-mpt-actions button:hover { filter: brightness(1.05); border-color: var(--accent); }
  @keyframes mpt-apply-flash { 0%,100%{box-shadow:none} 30%{box-shadow:0 0 0 3px rgba(var(--accent-rgb,9,105,218),0.4)} }
  .pf-mpt-apply-flash { animation: mpt-apply-flash 0.6s ease; }
  .pf-mpt-runs { display: flex; flex-direction: column; gap: 4px; font-size: 11.5px; }
  .pf-mpt-runs .pf-mpt-run-row {
    display: flex; justify-content: space-between; gap: 6px;
    padding: 5px 7px; border-radius: 6px; cursor: pointer; border: 1px solid transparent;
  }
  .pf-mpt-runs .pf-mpt-run-row:hover { background: var(--bg-canvas); border-color: var(--border); }
  .pf-mpt-runs .pf-mpt-run-row .meta { color: var(--muted); font-size: 11px; }
  .pf-mpt-runs .pf-mpt-run-row .del {
    border: none; background: transparent; color: var(--muted); cursor: pointer; font-size: 12px;
  }
  .pf-mpt-runs .pf-mpt-run-row .del:hover { color: var(--neg); }
  .pf-mpt-status {
    padding: 18px 12px; text-align: center; color: var(--muted); font-size: 12.5px;
  }
  .pf-mpt-error { padding: 12px; color: var(--neg); font-size: 12px; }

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

  /* Column-view switcher bar (Pass D) */
  .cv-bar {
    display: flex; align-items: center; gap: 10px;
    padding: 6px 16px 0;
    flex-wrap: wrap;
  }
  .cv-seg {
    display: inline-flex; align-items: center;
    background: var(--bg-subtle); border: 1px solid var(--border);
    border-radius: 8px; padding: 2px;
  }
  .cv-seg button {
    background: transparent; border: 0; color: var(--text);
    font-size: 12px; padding: 4px 10px; border-radius: 6px;
    cursor: pointer; font-weight: 500;
  }
  .cv-seg button:hover { background: var(--bg-canvas); }
  .cv-seg button.active {
    background: var(--bg-canvas); color: var(--accent);
    font-weight: 700; box-shadow: 0 1px 2px rgba(0,0,0,0.05);
  }
  .cv-custom-wrap { position: relative; }
  .cv-custom-btn {
    background: var(--bg-canvas); border: 1px solid var(--border);
    color: var(--text); font-size: 12px; padding: 5px 10px;
    border-radius: 7px; cursor: pointer; display: inline-flex;
    align-items: center; gap: 6px;
  }
  .cv-custom-btn .cv-caret { color: var(--muted); font-size: 10px; }
  .cv-custom-btn.active { color: var(--accent); font-weight: 600; }
  .cv-custom-dropdown {
    position: absolute; top: calc(100% + 4px); left: 0;
    background: var(--bg-canvas); border: 1px solid var(--border);
    border-radius: 7px; padding: 4px; min-width: 180px;
    box-shadow: 0 6px 18px rgba(0,0,0,0.12);
    z-index: 20; display: none;
  }
  .cv-custom-dropdown.open { display: block; }
  .cv-custom-dropdown .cv-cv-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 5px 8px; border-radius: 5px; cursor: pointer;
    font-size: 12px; gap: 8px;
  }
  .cv-custom-dropdown .cv-cv-row:hover { background: var(--bg-subtle); }
  .cv-custom-dropdown .cv-cv-row.active { color: var(--accent); font-weight: 600; }
  .cv-custom-dropdown .cv-cv-del {
    background: transparent; border: 0; color: var(--muted);
    cursor: pointer; padding: 0 4px; font-size: 14px; line-height: 1;
  }
  .cv-custom-dropdown .cv-cv-del:hover { color: var(--neg); }
  .cv-custom-dropdown .cv-cv-empty {
    padding: 6px 8px; color: var(--muted); font-size: 11px; font-style: italic;
  }
  .cv-customize {
    background: transparent; border: 1px dashed var(--border);
    color: var(--muted); font-size: 12px; padding: 5px 10px;
    border-radius: 7px; cursor: pointer;
  }
  .cv-customize:hover { color: var(--text); border-color: var(--text); }
  .cv-fit-toggle {
    background: var(--bg-canvas); border: 1px solid var(--border);
    color: var(--muted); font-size: 12px; padding: 5px 10px;
    border-radius: 7px; cursor: pointer; font-weight: 500;
  }
  .cv-fit-toggle:hover { color: var(--text); border-color: var(--text); }
  .cv-fit-toggle.active {
    color: var(--accent); border-color: var(--accent);
    background: rgba(47, 129, 247, 0.08);
  }
  .cv-dirty {
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(234, 179, 8, 0.12); border: 1px solid rgba(234, 179, 8, 0.4);
    color: #92400e; padding: 3px 4px 3px 10px; border-radius: 7px;
    font-size: 11px;
  }
  .cv-dirty[hidden] { display: none !important; }
  [data-theme="dark"] .cv-dirty { color: #facc15; background: rgba(234,179,8,0.08); }
  .cv-dirty button {
    background: transparent; border: 1px solid currentColor;
    color: inherit; font-size: 11px; padding: 2px 7px;
    border-radius: 5px; cursor: pointer;
  }
  .cv-dirty button:hover { background: rgba(0,0,0,0.06); }

  /* Header drag state */
  table#tbl th.cv-th-drag { opacity: 0.4; }
  table#tbl th.cv-th-over { box-shadow: inset 3px 0 0 var(--accent); }
  table#tbl th[draggable="true"] { cursor: grab; }
  table#tbl th[draggable="true"]:active { cursor: grabbing; }

  /* Customize modal */
  .cv-modal { width: min(560px, 92vw); max-height: 80vh; display: flex; flex-direction: column; }
  .cv-modal h2 { margin: 0 0 6px; font-size: 16px; }
  .cv-modal .cv-modal-sub { color: var(--muted); font-size: 12px; margin-bottom: 10px; }
  .cv-list {
    flex: 1 1 auto; overflow-y: auto;
    list-style: none; margin: 0; padding: 0;
    border: 1px solid var(--border); border-radius: 8px;
    background: var(--bg-canvas);
  }
  .cv-list li {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 8px; border-bottom: 1px solid var(--border);
    font-size: 13px; cursor: grab;
  }
  .cv-list li:last-child { border-bottom: 0; }
  .cv-list li:active { cursor: grabbing; }
  .cv-list li.cv-li-drag { opacity: 0.35; }
  .cv-list li.cv-li-over { border-top: 2px solid var(--accent); }
  .cv-list .cv-grip { color: var(--muted); font-size: 14px; cursor: grab; user-select: none; }
  .cv-list .cv-li-label { flex: 1; }
  .cv-list .cv-li-tag { color: var(--muted); font-size: 11px; }
  .cv-modal-foot {
    display: flex; gap: 8px; align-items: center;
    margin-top: 12px; padding-top: 10px;
    border-top: 1px solid var(--border);
  }
  .cv-modal-foot .cv-name-input {
    flex: 1; padding: 6px 8px; font-size: 13px;
    border: 1px solid var(--border); border-radius: 6px;
    background: var(--bg-canvas); color: var(--text);
  }
  .cv-modal-foot .cv-spacer { flex: 1; }
  .cv-modal-foot button {
    background: var(--bg-canvas); border: 1px solid var(--border);
    color: var(--text); font-size: 12px; padding: 6px 12px;
    border-radius: 6px; cursor: pointer;
  }
  .cv-modal-foot button.primary {
    background: var(--accent); border-color: var(--accent); color: white;
  }
  .cv-modal-foot button:hover { filter: brightness(0.95); }

  /* Table */
  .table-wrap { padding: 12px 16px 16px; overflow-x: auto; --table-scale: 1; }
  .table-wrap.fit-columns { overflow-x: hidden; }
  table#tbl {
    width: 100%; border-collapse: separate; border-spacing: 0;
    font-variant-numeric: tabular-nums; font-size: 12px;
    background: var(--bg-canvas);
    border-radius: 10px; overflow: hidden;
    border: 1px solid var(--border);
  }
  .table-wrap.fit-columns table#tbl { font-size: calc(12px * var(--table-scale)); }
  table#tbl th {
    background: var(--header-bg); color: var(--text); font-weight: 700;
    border: none; border-bottom: 1px solid var(--border);
    padding: 7px 8px; text-align: center;
    cursor: pointer; user-select: none; white-space: nowrap;
    position: relative;
  }
  .table-wrap.fit-columns table#tbl th {
    padding: calc(7px * var(--table-scale)) calc(8px * var(--table-scale));
  }
  table#tbl th.no-sort { cursor: default; }
  table#tbl th:hover:not(.no-sort) { color: var(--accent); }
  table#tbl td {
    border: none; padding: 4px 8px;
    text-align: right; white-space: nowrap; height: 26px;
    border-bottom: 1px solid var(--border);
  }
  .table-wrap.fit-columns table#tbl td {
    padding: calc(4px * var(--table-scale)) calc(7px * var(--table-scale));
    height: calc(26px * var(--table-scale));
  }
  table#tbl tbody tr:last-child td { border-bottom: none; }
  table#tbl td.left { text-align: left; }
  table#tbl td.center { text-align: center; }
  table#tbl tbody tr:nth-child(even) td.alt-stripe { background: var(--row-alt); }
  table#tbl tbody tr:hover td.alt-stripe { background: var(--hover); }
  table#tbl tbody tr:hover { cursor: pointer; }
  th .arrow { margin-left: 5px; color: var(--accent); font-size: 10px; }
  .table-wrap.fit-columns th .arrow {
    margin-left: calc(5px * var(--table-scale));
    font-size: calc(10px * var(--table-scale));
  }

  /* Column-header tooltips also ride the shared .app-tip system. */

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
  .table-wrap.fit-columns td.name { max-width: calc(240px * var(--table-scale)); }

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
  .table-wrap.fit-columns svg.spark {
    width: calc(96px * var(--table-scale)); height: calc(22px * var(--table-scale));
  }
  .table-wrap.fit-columns svg.rs {
    width: calc(86px * var(--table-scale)); height: calc(22px * var(--table-scale));
  }

  .table-wrap.fit-columns .logo,
  .table-wrap.fit-columns .logo-fallback {
    width: calc(18px * var(--table-scale));
    height: calc(18px * var(--table-scale));
    font-size: calc(9px * var(--table-scale));
  }

  .table-wrap.fit-columns .bar-cell { height: calc(18px * var(--table-scale)); }
  .table-wrap.fit-columns .bar-cell .bar-label {
    line-height: calc(18px * var(--table-scale));
    padding: 0 calc(6px * var(--table-scale));
  }

  .tri-up   { color: var(--pos); font-size: 13px; }
  .tri-down { color: var(--neg); font-size: 13px; }
  .table-wrap.fit-columns .tri-up,
  .table-wrap.fit-columns .tri-down { font-size: calc(13px * var(--table-scale)); }
  .na { color: var(--muted); }

  /* Analyst sentiment dashboard (replaces the old footer block) */
  #analyst-dashboard {
    margin: 16px 16px 16px; padding: 16px;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 12px;
  }
  .an-head {
    display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
    margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1px solid var(--border);
  }
  .an-head .an-title { font-size: 14px; font-weight: 700; color: var(--text); }
  .an-head .an-sub { font-size: 11.5px; color: var(--muted); }
  .an-head .an-mode-note { margin-left: auto; font-size: 11.5px; color: var(--muted); position: relative; }
  .an-head .an-mode-note b { color: var(--text); font-weight: 600; }
  .an-mode-btn {
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--bg-canvas); border: 1px solid var(--border);
    color: var(--text); font-weight: 600; font-size: 11.5px;
    padding: 3px 10px; border-radius: 999px; cursor: pointer;
    transition: border-color 0.12s ease, background 0.12s ease;
    font-family: inherit;
  }
  .an-mode-btn:hover { border-color: var(--accent); background: var(--bg-subtle); }
  .an-mode-btn .an-caret { font-size: 9px; opacity: 0.7; transition: transform 0.12s ease; }
  .an-mode-btn.open .an-caret { transform: rotate(180deg); }
  .an-mode-menu {
    display: none; position: absolute; top: calc(100% + 6px); right: 0;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 8px;
    box-shadow: 0 10px 28px rgba(0,0,0,0.22);
    min-width: 160px; z-index: 1000; padding: 4px;
  }
  .an-mode-menu.show { display: block; }
  .an-mode-opt {
    display: flex; justify-content: space-between; align-items: center;
    padding: 7px 10px; border-radius: 6px; cursor: pointer;
    font-size: 12px; color: var(--text); user-select: none;
  }
  .an-mode-opt:hover { background: var(--bg-subtle); }
  .an-mode-opt[data-selected="1"] { color: var(--accent); font-weight: 600; }
  .an-mode-opt[data-selected="1"]::after { content: "✓"; color: var(--accent); margin-left: 8px; }
  .an-grid {
    display: grid; gap: 12px;
    grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr) minmax(0, 1.1fr);
  }
  @media (max-width: 1100px) { .an-grid { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 740px)  { .an-grid { grid-template-columns: 1fr; } }
  .an-card {
    background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px;
  }
  .an-card h4 {
    margin: 0 0 10px; font-size: 11.5px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.6px; font-weight: 700;
    display: flex; align-items: baseline; gap: 8px;
  }
  .an-card h4 .an-sub { color: var(--muted); font-size: 11px; font-weight: 500; text-transform: none; letter-spacing: 0; }

  /* Hero gauge (weighted rating) */
  .an-rating-num {
    font-size: 30px; font-weight: 700; color: var(--text); line-height: 1;
    font-variant-numeric: tabular-nums; letter-spacing: -0.5px;
  }
  .an-rating-key {
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
    margin-left: 8px; vertical-align: middle;
  }
  .an-rating-key.buy { background: rgba(34,197,94,0.15); color: #16a34a; }
  .an-rating-key.strong-buy { background: rgba(21,128,61,0.20); color: #15803d; }
  .an-rating-key.hold { background: rgba(234,179,8,0.18); color: #ca8a04; }
  .an-rating-key.sell { background: rgba(249,115,22,0.18); color: #ea580c; }
  .an-rating-key.strong-sell { background: rgba(220,38,38,0.20); color: #dc2626; }
  .an-gauge {
    width: 100%; height: 10px; border-radius: 99px; margin: 12px 0 4px;
    background: linear-gradient(to right, #15803d 0%, #22c55e 25%, #eab308 50%, #f97316 75%, #dc2626 100%);
    position: relative;
  }
  .an-gauge .needle {
    position: absolute; top: -3px; width: 4px; height: 16px;
    background: var(--text); border-radius: 2px; transform: translateX(-50%);
    box-shadow: 0 0 0 2px var(--bg-subtle);
  }
  .an-gauge-labels {
    display: flex; justify-content: space-between; font-size: 9.5px;
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; margin-top: 4px;
  }
  .an-rating-meta { margin-top: 10px; font-size: 11.5px; color: var(--muted); display: flex; flex-direction: column; gap: 2px; }
  .an-rating-meta b { color: var(--text); font-weight: 600; }

  /* Distribution bar (Strong Buy / Buy / Hold / Sell / Strong Sell) */
  .an-dist-bar {
    display: flex; height: 22px; border-radius: 6px; overflow: hidden;
    background: var(--bg-canvas); border: 1px solid var(--border);
  }
  .an-dist-bar > div {
    display: flex; align-items: center; justify-content: center;
    color: #fff; font-size: 10.5px; font-weight: 700;
    text-shadow: 0 1px 0 rgba(0,0,0,0.2); overflow: hidden;
  }
  .an-dist-bar .sb { background: #15803d; }
  .an-dist-bar .b  { background: #22c55e; }
  .an-dist-bar .h  { background: #eab308; color: #1f2328; text-shadow: none; }
  .an-dist-bar .s  { background: #f97316; }
  .an-dist-bar .ss { background: #dc2626; }
  .an-dist-legend {
    display: flex; flex-wrap: wrap; gap: 8px 12px; margin-top: 10px;
    font-size: 10.5px; color: var(--muted);
  }
  .an-dist-legend span { display: inline-flex; align-items: center; gap: 4px; }
  .an-dist-legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; }

  /* Upside hero card (weighted target upside) */
  .an-upside-num {
    font-size: 30px; font-weight: 700; line-height: 1;
    font-variant-numeric: tabular-nums; letter-spacing: -0.5px;
  }
  .an-upside-num.pos { color: var(--pos); }
  .an-upside-num.neg { color: var(--neg); }
  .an-upside-meta { margin-top: 8px; font-size: 11.5px; color: var(--muted); display: flex; flex-direction: column; gap: 3px; }
  .an-upside-meta b { color: var(--text); font-weight: 600; }
  .an-coverage-bar {
    margin-top: 10px; height: 6px; border-radius: 99px; overflow: hidden;
    background: var(--bg-canvas); border: 1px solid var(--border);
    position: relative;
  }
  .an-coverage-bar .fill { height: 100%; background: var(--accent); }

  /* Per-holding table */
  .an-table-wrap { overflow-x: auto; }
  .an-table {
    width: 100%; border-collapse: collapse; font-size: 11.5px;
    font-variant-numeric: tabular-nums;
  }
  .an-table th {
    text-align: right; padding: 6px 8px; color: var(--muted); font-weight: 700;
    border-bottom: 1px solid var(--border); white-space: nowrap;
    text-transform: uppercase; font-size: 10px; letter-spacing: 0.4px;
    cursor: pointer; user-select: none;
  }
  .an-table th:hover { color: var(--accent); }
  .an-table th .arrow { color: var(--accent); margin-left: 3px; font-size: 9px; }
  .an-table th:first-child, .an-table td:first-child { text-align: left; }
  .an-table td {
    padding: 5px 8px; border-bottom: 1px solid var(--border);
    text-align: right; white-space: nowrap; color: var(--text);
  }
  .an-table tr:last-child td { border-bottom: none; }
  .an-table td.sym { font-weight: 700; }
  .an-table td .muted { color: var(--muted); font-size: 10.5px; margin-left: 4px; }
  .an-table td.pos { color: var(--pos); font-weight: 600; }
  .an-table td.neg { color: var(--neg); font-weight: 600; }
  .an-table td .rk {
    display: inline-block; padding: 1px 7px; border-radius: 999px;
    font-size: 9.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.4px;
  }
  .an-table td .rk.buy { background: rgba(34,197,94,0.15); color: #16a34a; }
  .an-table td .rk.strong-buy { background: rgba(21,128,61,0.20); color: #15803d; }
  .an-table td .rk.hold { background: rgba(234,179,8,0.18); color: #ca8a04; }
  .an-table td .rk.sell { background: rgba(249,115,22,0.18); color: #ea580c; }
  .an-table td .rk.strong-sell { background: rgba(220,38,38,0.20); color: #dc2626; }
  .an-table td .rk.none { background: var(--bg-subtle); color: var(--muted); }
  .an-mini-dist {
    display: inline-flex; height: 8px; border-radius: 3px; overflow: hidden;
    background: var(--bg-canvas); border: 1px solid var(--border); width: 96px;
  }
  .an-mini-dist > div { height: 100%; }
  .an-mini-dist .sb { background: #15803d; }
  .an-mini-dist .b  { background: #22c55e; }
  .an-mini-dist .h  { background: #eab308; }
  .an-mini-dist .s  { background: #f97316; }
  .an-mini-dist .ss { background: #dc2626; }

  /* Target waterfall (mini) — used in the per-row visualization */
  .an-target-bar {
    position: relative; height: 18px; width: 160px;
    background: var(--bg-canvas); border: 1px solid var(--border); border-radius: 4px;
    display: inline-block; vertical-align: middle;
  }
  .an-target-bar .range {
    position: absolute; top: 6px; height: 6px; background: var(--accent-soft, rgba(47,129,247,0.15));
    border-radius: 2px;
  }
  .an-target-bar .mark { position: absolute; top: 2px; width: 2px; height: 14px; transform: translateX(-1px); }
  .an-target-bar .mark.current { background: var(--text); }
  .an-target-bar .mark.target  { background: var(--accent); }
  .an-target-bar .mark.low, .an-target-bar .mark.high { background: var(--muted); opacity: 0.6; width: 1.5px; }

  /* Coverage footer chip */
  .an-coverage-foot {
    margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    flex-wrap: wrap; font-size: 11.5px; color: var(--muted);
  }
  .an-coverage-foot b { color: var(--text); font-weight: 600; }
  .an-coverage-foot .credit { color: var(--muted); font-size: 11px; }
  .an-not-covered {
    color: var(--muted); font-size: 11px;
  }
  .an-not-covered b { color: var(--text); font-weight: 600; }

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
  <button id="edit-btn" class="portfolio-btn" data-tip="Open the portfolio workspace: saved tabs, the constituents editor, and the analytics area that belongs to each portfolio.">Portfolio</button>
  <button id="optimize" data-tip="Open the Portfolio Optimization workspace: compute the long-only efficient frontier from your current holdings (Markowitz CLA, Ledoit-Wolf shrinkage), pick a risk/return point, and save the chosen weights as a preset.">⚙ Optimize</button>
  <button id="refresh" data-tip="Pull fresh quotes from Yahoo Finance for the current portfolio and rebuild the derived metrics as rows stream back in.">↻ Refresh</button>
  <!-- Export button: exports ALL saved portfolios to a multi-sheet .xlsx, one sheet
       per portfolio. Each sheet must include EVERY column the app can show for a
       row (full column registry, not just the currently visible view) PLUS the
       analyst recommendations and the portfolio analytics ratios shown in the
       Portfolio tab, with static 1Y and 5Y portfolio statistics but no exported
       time-series payloads. Intent: user feeds the file to an AI/chatbot for
       agentic analysis and portfolio discussion. Future maintainers — when new
       columns or analytics are added to the UI, extend the export accordingly so
       this stays the "everything you see in the app" artifact. -->
  <button id="export" data-tip="Create one Excel workbook with every saved portfolio, full holdings data, analyst fields, and static 1Y and 5Y portfolio statistics. Useful as structured input to an AI agent.">⬇ Export</button>
  <span class="spacer"></span>
  <span class="status" id="status">Idle</span>
  <div class="fx-menu" id="fx-menu">
    <button class="fx-btn" id="fx-btn" type="button" title="Portfolio denomination currency">
      <span class="fx-btn-label" id="fx-btn-label">USD</span>
      <span class="fx-btn-caret">▾</span>
    </button>
    <div class="fx-dropdown" id="fx-dropdown" role="listbox" aria-hidden="true"></div>
    <div class="fx-hover" id="fx-hover" aria-hidden="true">
      <div class="fx-hover-title" id="fx-hover-title"></div>
      <svg class="fx-hover-svg" id="fx-hover-svg" viewBox="0 0 220 80" preserveAspectRatio="none"></svg>
      <div class="fx-hover-foot" id="fx-hover-foot"></div>
    </div>
  </div>
  <button class="theme-switch" id="theme-switch" title="Toggle theme" aria-label="Toggle theme">
    <i class="ts-icon" id="ts-sun">&#9728;</i>
    <span class="ts-track" id="ts-track"><span class="ts-thumb"></span></span>
    <i class="ts-icon" id="ts-moon">&#9790;</i>
  </button>
  <button class="info-btn" id="info-btn" title="How this works"><span class="info-icon-circle">i</span></button>
</div>
<div class="progress-wrap" id="progress-wrap"><div class="progress-bar" id="progress-bar"></div></div>

<div id="input-panel" class="hidden">
  <!-- Browser-style tab strip of saved portfolios -->
  <div class="pf-tabs" id="pf-tabs" role="tablist"></div>

  <!-- Constituents editor -->
  <div class="pf-editor">
    <div class="pf-editor-head">
      <span id="pf-editor-title">Constituents</span>
    </div>
    <textarea id="tickers" rows="2" placeholder="Paste tickers or company names — comma or newline separated.
e.g.  DELL, TXN, DaVita, JBL, KLAC, MARA, COMT, FFIV, Alphabet, ETN, AVGO, NVDA, XLK, Trane, MSTR, COST, Apple, Microsoft"></textarea>
    <div class="panel-row">
      <button id="build" class="primary">Build Dashboard</button>
      <button id="save-as" class="ghost">＋ Save as new</button>
      <span class="spacer"></span>
      <span class="status" id="editor-status">Paste your tickers and press Build (or Cmd/Ctrl + Enter).</span>
    </div>
    <!-- Primary button (#build) and secondary (#save-as) swap labels/intent based on
         whether a saved portfolio is currently open. See updatePrimaryButtonLabels(). -->

  </div>

  <!-- Analytics sub-window -->
  <div class="pf-analytics" id="pf-analytics">
    <div class="pf-analytics-head">
      <span class="pf-analytics-title">Portfolio analytics</span>
      <!-- Mode bar: built-in pills (Equal, Cap) + any saved per-portfolio
           weight presets. Trailing "+" opens the weights editor in new-preset
           mode; the pencil on an active preset opens it for edit/delete. -->
      <div class="pf-mode-bar" id="pf-mode-toggle" role="tablist" aria-label="Weighting"></div>
      <div class="pf-period-tabs" id="pf-period-tabs">
        <button data-p="3M">3M</button>
        <button data-p="6M">6M</button>
        <button data-p="YTD">YTD</button>
        <button data-p="1Y" class="active">1Y</button>
        <button data-p="3Y">3Y</button>
        <button data-p="5Y">5Y</button>
        <button data-p="MAX">Max</button>
      </div>
      <div class="pf-overlay-toggles" id="pf-overlay-toggles" role="group" aria-label="Chart overlays">
        <span class="pf-overlay-pill" id="pf-show-spy" data-on="1" role="switch" aria-checked="true" tabindex="0">
          <span class="swatch spy"></span>SPY
        </span>
        <span class="pf-overlay-pill" id="pf-show-ndx" data-on="0" role="switch" aria-checked="false" tabindex="0">
          <span class="swatch ndx"></span>NASDAQ
        </span>
        <span class="pf-overlay-pill" id="pf-show-sec" data-on="0" role="switch" aria-checked="false" tabindex="0">
          <span class="swatch sec"></span>Sector mix
          <span class="ovl-info" data-tip="Sector mix is a synthetic benchmark: each portfolio holding is replaced by its sector ETF (XLK Technology, XLV Healthcare, XLF Financials, XLY Consumer Cyclical, XLP Consumer Defensive, XLC Communication, XLE Energy, XLI Industrials, XLB Materials, XLRE Real Estate, XLU Utilities). Weights match your active mode (equal / cap / custom). Compare your stock picks against the matching sector basket.">i</span>
        </span>
        <span class="pf-overlay-pill" id="pf-show-dd" data-on="1" role="switch" aria-checked="true" tabindex="0">
          <span class="swatch dd"></span>Drawdown
        </span>
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

<!-- Custom-weight popup. The name input + Save/Save-as/Delete row turns the
     editor into a named-preset manager; Apply alone still works as the
     legacy ad-hoc "custom" mode for one-off comparisons. -->
<div class="pf-weights-bg" id="pf-weights-bg">
  <div class="pf-weights-modal">
    <div class="pf-weights-head">
      <span class="pf-weights-title" id="pf-weights-title">Custom weights</span>
      <div class="pf-weights-sum" id="pf-weights-sum">100.0%</div>
      <button id="pf-weights-equal" class="pf-weights-action" title="Set all to equal weight">Equal-weighted</button>
      <button id="pf-weights-reset" class="pf-weights-action" title="Reset to market-cap weights">Cap-weighted</button>
      <button id="pf-weights-close" class="pf-weights-action" title="Close">✕</button>
    </div>
    <div class="pf-name-row">
      <input id="pf-weights-name" type="text" placeholder="Preset name (e.g. Growth Tilt)" autocomplete="off" spellcheck="false"/>
      <button id="pf-weights-save" class="pf-weights-action" title="Save as named preset">Save</button>
      <button id="pf-weights-saveas" class="pf-weights-action" title="Save under a new name">Save as…</button>
      <button id="pf-weights-delete" class="pf-weights-action" title="Delete this preset" style="display:none">Delete</button>
      <span id="pf-weights-status" class="pf-name-status"></span>
    </div>
    <div class="pf-weights-hint">Slide or type to set weights. Lock pins a row; others auto-rebalance. Apply alone for an unsaved comparison, or name + Save to keep it.</div>
    <div class="pf-weights-body" id="pf-weights-body"></div>
    <div class="pf-weights-foot">
      <button id="pf-weights-apply" class="primary">Apply</button>
      <button id="pf-weights-cancel">Cancel</button>
    </div>
  </div>
</div>

<!-- Inline "save as" prompt — top-level overlay so it can appear over either
     the weights modal or the MPT overlay. Uses the app's dark theme rather
     than a native prompt() so the UI feels consistent. -->
<div class="pf-inline-prompt" id="pf-name-prompt">
  <div class="pf-ip-box">
    <div class="pf-ip-title" id="pf-name-prompt-title">Save preset as…</div>
    <div class="pf-ip-desc" id="pf-name-prompt-desc" style="display:none"></div>
    <input id="pf-name-prompt-input" type="text" placeholder="Preset name" autocomplete="off" spellcheck="false"/>
    <div class="pf-ip-err" id="pf-name-prompt-err"></div>
    <div class="pf-ip-foot">
      <button id="pf-name-prompt-cancel">Cancel</button>
      <button id="pf-name-prompt-ok" class="primary">Save</button>
    </div>
  </div>
</div>

<!-- Portfolio Optimization (MPT) overlay. Computes the long-only efficient
     frontier from the active portfolio, lets the user explore it with a
     slider, and either save runs or apply the chosen weights as a preset. -->
<div class="pf-mpt-bg" id="pf-mpt-bg" role="dialog" aria-modal="true" aria-labelledby="pf-mpt-title">
  <div class="pf-mpt-modal">
    <div class="pf-mpt-head">
      <span class="pf-mpt-title" id="pf-mpt-title">Portfolio Optimization</span>
      <span class="pf-mpt-sub" id="pf-mpt-sub">Markowitz long-only · Critical Line Algorithm</span>
      <span class="spacer"></span>
      <button id="pf-mpt-info" class="info-btn pf-mpt-info-btn" title="About Modern Portfolio Theory" type="button"><span class="info-icon-circle">i</span></button>
      <button id="pf-mpt-close" class="pf-mpt-close" title="Close (Esc)">✕</button>
    </div>
    <div class="pf-mpt-controls" id="pf-mpt-controls">
      <div class="pf-mpt-ctl">
        <label>Lookback</label>
        <div class="seg" id="pf-mpt-lookback">
          <button data-v="1Y">1Y</button>
          <button data-v="3Y" class="active">3Y</button>
          <button data-v="5Y">5Y</button>
          <button data-v="10Y">10Y</button>
        </div>
      </div>
      <div class="pf-mpt-ctl">
        <label>Frequency</label>
        <div class="seg" id="pf-mpt-freq">
          <button data-v="daily">Daily</button>
          <button data-v="weekly" class="active">Weekly</button>
          <button data-v="monthly">Monthly</button>
        </div>
      </div>
      <div class="pf-mpt-ctl pf-mpt-ctl-rf">
        <label>Risk-free (annual)<span class="pf-mpt-rf-info" data-tip="Annualized risk-free rate (in %) used as the baseline return in Sharpe and tangency calculations. Auto fills it from the short-rate proxy for the portfolio currency (e.g. ^IRX, US 13-week T-Bill), averaged over the selected lookback. The sparkline shows that proxy's history — hover for the time series.">i</span></label>
        <div class="pf-mpt-rf-wrap">
          <input id="pf-mpt-rf" type="number" min="0" max="20" step="0.05" value="4.50"/>
          <div class="pf-mpt-rf-spark-wrap" id="pf-mpt-rf-spark-wrap">
            <svg id="pf-mpt-rf-spark" class="pf-mpt-rf-spark" viewBox="0 0 200 36" preserveAspectRatio="none" aria-hidden="true"></svg>
            <div class="pf-mpt-rf-spark-tip" id="pf-mpt-rf-spark-tip" role="tooltip"></div>
          </div>
          <button type="button" id="pf-mpt-rf-auto" class="pf-mpt-rf-auto" data-tip="Refill the input with the trailing-window mean of the short-rate proxy (e.g. ^IRX). Click to recompute after changing the lookback or currency.">Auto</button>
        </div>
      </div>
      <div class="pf-mpt-ctl pf-mpt-ctl-div">
        <label>Allocation <span class="pf-mpt-rf-info" data-tip="Sparse: unconstrained long-only Markowitz — the optimiser is free to drop assets to 0%, often leaving most holdings at zero weight. Diversified: every asset receives at least a minimum weight (auto-scaled by portfolio size — about 0.5/N, never above 1/N) so the frontier keeps all constituents in the mix. Same math (Critical Line Algorithm via variable substitution), no extra solver.">i</span></label>
        <div class="seg" id="pf-mpt-mode">
          <button data-v="sparse" class="active">Sparse</button>
          <button data-v="diversified">Diversified</button>
        </div>
      </div>
      <div class="pf-mpt-ctl">
        <label>Compute budget</label>
        <div class="pf-mpt-select" id="pf-mpt-budget" data-value="standard" tabindex="0" role="combobox" aria-expanded="false" aria-haspopup="listbox">
          <span class="pf-mpt-select-label">Standard — 1M configs (~4s)</span>
          <span class="pf-mpt-select-caret" aria-hidden="true">▾</span>
          <ul class="pf-mpt-select-menu" role="listbox" hidden>
            <li data-value="fast" role="option">Fast — 200k configs (~1s)</li>
            <li data-value="standard" role="option" aria-selected="true">Standard — 1M configs (~4s)</li>
            <li data-value="thorough" role="option">Thorough — 3.5M configs (~14s)</li>
            <li data-value="exhaustive" role="option">Exhaustive — 15M configs (~60s)</li>
          </ul>
        </div>
      </div>
      <div class="pf-mpt-ctl pf-mpt-ctl-run">
        <label>&nbsp;</label>
        <button class="pf-mpt-run" id="pf-mpt-run">Run optimization</button>
      </div>
    </div>
    <div class="pf-mpt-body">
      <div class="pf-mpt-chartwrap">
        <div class="pf-mpt-chart" id="pf-mpt-chart">
          <!-- Two-canvas chart: base layer carries axes + cloud + frontier line
               + anchors (redrawn only when data/viewport changes); overlay
               carries the selection ring + hover ghost (redrawn on cursor
               input). Both canvases are sized through one shared helper so
               the frontier line and Monte-Carlo cloud cannot drift. -->
          <canvas id="pf-mpt-base" class="pf-mpt-cv pf-mpt-cv-base" aria-hidden="true"></canvas>
          <canvas id="pf-mpt-overlay" class="pf-mpt-cv pf-mpt-cv-overlay" aria-hidden="true"></canvas>
          <div class="pf-mpt-status" id="pf-mpt-status">Run the optimization to draw the efficient frontier.</div>
        </div>
        <div class="pf-mpt-slider-row" data-line="frontier">
          <span class="pf-mpt-slider-label">Frontier</span>
          <span>Min-vol</span>
          <input type="range" id="pf-mpt-slider" min="0" max="100" value="50" disabled/>
          <span>Max-return</span>
          <span class="pf-mpt-slider-readout" id="pf-mpt-slider-readout">—</span>
        </div>
        <div class="pf-mpt-slider-row cvar" data-line="cvar">
          <span class="pf-mpt-slider-label">CVaR</span>
          <span>99%</span>
          <input type="range" id="pf-mpt-cvar-slider" min="0" max="49" value="0" disabled/>
          <span>50%</span>
          <span class="pf-mpt-slider-readout" id="pf-mpt-cvar-readout">—</span>
        </div>
        <div class="pf-mpt-legend" id="pf-mpt-legend"></div>
      </div>
      <div class="pf-mpt-side" id="pf-mpt-side">
        <h4>Selected portfolio</h4>
        <div class="pf-mpt-kv" id="pf-mpt-stats">
          <span class="k">Run the optimization to see metrics.</span>
        </div>
        <h4>Weights</h4>
        <div class="pf-mpt-weights" id="pf-mpt-wlist"></div>
        <div class="pf-mpt-actions">
          <button id="pf-mpt-apply" class="primary">Apply to Portfolio</button>
          <button id="pf-mpt-save">Save as Custom Weights</button>
        </div>
        <h4>Recent runs</h4>
        <div class="pf-mpt-runs" id="pf-mpt-runs"><span class="pf-mpt-status">No saved runs yet.</span></div>
      </div>
    </div>
  </div>
</div>

<!-- About MPT modal — layered on top of the MPT overlay (z-index 95 vs 90).
     Mirrors the main-page About modal (info-bg) for visual consistency;
     re-uses the same .info-modal / .info-head / .info-card / .info-formula
     classes so KaTeX auto-render works with zero new styling. -->
<div class="info-bg pf-mpt-info-bg" id="pf-mpt-info-bg">
  <div class="info-modal">
    <div class="info-head">
      <div>
        <div class="info-head-title">Modern Portfolio Theory — Guide</div>
        <div class="info-head-sub">Motivation, theory, limitations, and a worked example</div>
      </div>
      <button class="info-head-close" id="pf-mpt-info-close" type="button" title="Close (Esc)">&#215;</button>
    </div>
    <p class="info-intro">
      Modern Portfolio Theory (MPT) — introduced by Harry Markowitz in 1952 — is the
      mathematical foundation for trading off expected return against risk when
      combining risky assets. This optimiser implements long-only, fully-invested,
      mean-variance MPT with the Critical Line Algorithm (Markowitz, 1959).
    </p>
    <div class="info-grid">

      <div class="info-card info-card-wide">
        <div class="info-card-name">Motivation — the free-lunch intuition</div>
        <div class="info-card-desc">
          Most investors hold many assets because diversification reduces portfolio
          volatility without proportionally reducing expected return — what Markowitz
          called &ldquo;the only free lunch in finance&rdquo;. When two assets are imperfectly
          correlated (ρ &lt; 1), the variance of their weighted sum is strictly less
          than the weighted sum of their variances. Stacking many imperfectly
          correlated bets therefore shifts the portfolio toward the upper-left of
          the (volatility, return) plane: more return per unit of risk.
        </div>
        <div class="info-card-why">
          The <b>efficient frontier</b> is the locus of portfolios that achieve the
          maximum expected return for each given level of risk. Anything below the
          frontier is dominated — you can always find another long-only portfolio
          with the same risk but a higher expected return.
        </div>
      </div>

      <div class="info-card info-card-wide">
        <div class="info-card-name">Theory — the mean-variance optimisation</div>
        <div class="info-card-desc">
          Given an asset universe with annualised mean-return vector <b>μ</b> and
          annualised covariance matrix <b>Σ</b>, a portfolio is a weight vector
          <b>w</b> summing to one. Its expected return and variance are
        </div>
        <div class="info-formula">$$\mu_p = w^\top \mu \qquad \sigma_p^{\,2} = w^\top \Sigma\, w$$</div>
        <div class="info-card-desc">
          The Markowitz program seeks, for each target return <b>R⋆</b>, the
          long-only portfolio that minimises variance subject to that target:
        </div>
        <div class="info-formula">$$\min_{w}\ w^\top \Sigma\, w \quad \text{s.t.}\quad w^\top \mu = R^\star,\ \ w^\top \mathbf{1} = 1,\ \ w \ge 0$$</div>
        <div class="info-card-desc">
          Sweeping <b>R⋆</b> traces the efficient frontier. The
          <b>Critical Line Algorithm</b> (CLA) used here solves the entire
          piecewise-linear frontier in one pass by tracking which assets are at
          their lower bound versus &ldquo;free&rdquo;, jumping between turning points where the
          active set changes. The <b>tangency portfolio</b> — the point on the
          frontier maximising the Sharpe ratio — is
        </div>
        <div class="info-formula">$$w^{\mathrm{tan}} = \arg\max_{w \in \mathcal{F}}\ \frac{\mu_p - r_f}{\sigma_p}$$</div>
        <div class="info-card-desc">
          where <b>r<sub>f</sub></b> is the risk-free rate. Geometrically it is the
          point at which a line from <b>(0, r<sub>f</sub>)</b> is tangent to the
          frontier — the steepest reward-to-risk ratio achievable with risky assets.
        </div>
        <div class="info-card-desc">
          The dashed orange curve overlays a separate family of optima: the
          <b>minimum-CVaR</b> portfolios. For confidence level α∈[0.5, 0.99],
          <b>CVaR<sub>α</sub></b> (expected shortfall, ES<sub>α</sub>) is the
          expected loss conditional on being in the worst (1−α) tail of the
          empirical return distribution. Each point is solved as a
          <b>Rockafellar-Uryasev</b> linear program:
        </div>
        <div class="info-formula">$$\min_{w,\zeta,u}\ \zeta + \frac{1}{(1-\alpha)T} \sum_{t} u_t \quad \text{s.t.}\quad u_t \ge -R_t^\top w - \zeta,\ \ u_t \ge 0,\ \ w^\top \mathbf{1} = 1,\ \ w \ge L$$</div>
        <div class="info-card-desc">
          At the optimum, <b>ζ⋆ = VaR<sub>α</sub></b> and the objective equals
          <b>CVaR<sub>α</sub></b>. The slider scans α from 99% (strict tail) down
          to 50%; lower α weighs broader downside in the objective and typically
          accepts a tighter portfolio with less concentration.
        </div>
        <div class="info-card-why">
          Sample covariance is noisy when the number of assets approaches the
          number of observations. This module shrinks it toward a
          constant-correlation target using the <b>Ledoit-Wolf</b> estimator with a
          data-driven intensity α ∈ [0, 1]:
        </div>
        <div class="info-formula">$$\hat{\Sigma} = \alpha\, T + (1-\alpha)\, S$$</div>
        <div class="info-card-desc">
          where <b>S</b> is the sample covariance and <b>T</b> the
          constant-correlation target. This stabilises the optimisation when the
          frontier is highly sensitive to small changes in <b>Σ</b>.
        </div>
      </div>

      <div class="info-card">
        <div class="info-card-name">Limitations</div>
        <div class="info-card-desc">
          MPT&rsquo;s elegance hides several traps that matter in practice:
        </div>
        <ul class="info-feature-list">
          <li><b>Estimation error in μ dominates.</b> Historical mean returns are noisy
              forecasts; the optimiser amplifies that noise into extreme corner
              portfolios (&ldquo;Markowitz error maximisation&rdquo;).</li>
          <li><b>Covariance instability.</b> Sample Σ rotates with the lookback window;
              the Ledoit-Wolf shrinkage applied here mitigates but does not
              eliminate this.</li>
          <li><b>Long-only.</b> No shorting, no leverage — many academic results
              (efficient frontier as a hyperbola, two-fund separation) only hold
              when shorts are allowed.</li>
          <li><b>No transaction costs, taxes, or liquidity constraints.</b> The
              optimiser will happily produce a 0.18% NVDA allocation; whether that
              is sensible to trade is up to you.</li>
          <li><b>Variance ≠ risk.</b> Mean-variance treats upside and downside
              symmetrically. For asymmetric return distributions consider CVaR or
              downside-deviation formulations.</li>
          <li><b>Backward-looking.</b> The frontier reflects the chosen lookback
              window; it is not a forecast.</li>
        </ul>
      </div>

      <div class="info-card">
        <div class="info-card-name">Example — closed-form two-asset case</div>
        <div class="info-card-desc">
          For two risky assets with volatilities <b>σ<sub>1</sub>, σ<sub>2</sub></b>
          and correlation <b>ρ</b>, the minimum-variance long-only weight on asset 1 is
        </div>
        <div class="info-formula">$$w_1^\star = \frac{\sigma_2^{\,2} - \rho\,\sigma_1 \sigma_2}{\sigma_1^{\,2} + \sigma_2^{\,2} - 2\rho\,\sigma_1 \sigma_2}$$</div>
        <div class="info-card-desc">
          with <b>w<sub>2</sub><sup>⋆</sup> = 1 − w<sub>1</sub><sup>⋆</sup></b>
          (clipped to [0, 1] for the long-only constraint).
          For <b>σ<sub>1</sub></b> = 20%, <b>σ<sub>2</sub></b> = 30%, <b>ρ</b> = 0.2,
          this gives <b>w<sub>1</sub><sup>⋆</sup></b> ≈ 0.78 and a portfolio volatility
          of about 18.4% — strictly below either asset&rsquo;s standalone volatility.
          The free lunch in one line of algebra.
        </div>
        <div class="info-card-why">
          The <b>n</b>-asset generalisation requires no new ideas: the same KKT
          conditions yield a piecewise-linear path in weight space, which the CLA
          enumerates corner by corner.
        </div>
      </div>

    </div>
  </div>
</div>

<!-- Pass D — column-view switcher bar.
     Lets the user pick between built-in presets (Default / Fundamentals / Momentum),
     View), select a saved custom view, or open the Customize modal to
     create one. The active view drives renderHeader/renderSortMenu/render
     via getActiveColumns(). Drag-and-drop on the live <th> elements also
     mutates the active view (with a "modified" pill when reordering on a
     built-in preset). -->
<div class="cv-bar" id="cv-bar">
  <div class="cv-seg" id="cv-builtins" role="tablist" aria-label="Column view"></div>
  <div class="cv-custom-wrap" id="cv-custom-wrap"></div>
  <button class="cv-fit-toggle" id="cv-fit-toggle" type="button" data-tip="Shrink column widths, chart cells, and table text just enough to keep the active view on screen." aria-pressed="false">Fit to screen</button>
  <button class="cv-customize" id="cv-customize" type="button" title="Pick which columns to show">Customize…</button>
  <span class="cv-dirty" id="cv-dirty" hidden>
    <span class="cv-dirty-label">Modified</span>
    <button class="cv-dirty-save" id="cv-dirty-save" type="button">Save as new view</button>
    <button class="cv-dirty-reset" id="cv-dirty-reset" type="button">Reset</button>
  </span>
</div>

<div class="table-wrap">
  <table id="tbl">
    <thead><tr id="thead"></tr></thead>
    <tbody id="tbody"><tr><td colspan="16" style="padding:30px; text-align:center; color:var(--muted);">Press <b>Build Dashboard</b> above to load your portfolio.</td></tr></tbody>
  </table>
</div>

<!-- Pass D — Customize Columns modal. Populated by openColumnPicker().
     Lists every registry column with a checkbox + drag handle (HTML5 DnD).
     "Save as new view" prompts for a name; "Update <name>" overwrites the
     active custom view. Built-ins are read-only here. -->
<div class="modal-bg" id="cv-modal-bg" hidden><div class="modal cv-modal" id="cv-modal" role="dialog" aria-modal="true" aria-label="Customize columns"></div></div>

<div id="analyst-dashboard" aria-label="Analyst sentiment dashboard"></div>

<div class="info-bg" id="info-bg">
  <div class="info-modal">
    <div class="info-head">
      <div>
        <div class="info-head-title">Portfolio Tracker — Guide</div>
        <div class="info-head-sub">Feature library, workflow, and column formulas</div>
      </div>
      <button class="info-head-close" onclick="closeInfo()">&#215;</button>
    </div>
    <p class="info-intro">
      Data is fetched live from Yahoo Finance via yfinance. Each row is one security. Click any row for a
      detail view. Use <b>⇅ Sort</b> to reorder by any visible metric. Colors are heatmaps, and most controls in the top bar and column bar explain themselves on hover.
    </p>
    <div class="info-grid">
      <div class="info-card full">
        <div class="info-card-name">Workflow</div>
        <div class="info-card-desc">Open <b>Portfolio</b> to manage saved tabs and constituents. Build or update the table, then move across analytics, FX denomination, column views, the analyst dashboard, and the row detail modal without losing the underlying portfolio state.</div>
        <ul class="info-feature-list">
          <li>Use saved tabs as named portfolio workspaces with cached rows, stale markers, last-view restore, and inline rename.</li>
          <li>Build or refresh once, then inspect the same holdings through the table, analytics, sentiment, FX, and detail layers.</li>
          <li>Switch weighting modes and time windows to see how the same portfolio behaves under different assumptions.</li>
          <li>Export the whole saved state to Excel when you want a portable research artifact.</li>
        </ul>
        <div class="info-card-why"><span class="label">Why it matters:</span>This app is designed to move from idea capture to ranking, risk review, analyst context, and export without making you rebuild the portfolio in separate tools.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Portfolio Workspace</div>
        <div class="info-card-desc">Saved portfolio tabs hold constituents, cached rows, and analytics context. Tabs can be renamed inline and carry stale state when the saved view no longer matches live data.</div>
        <ul class="info-feature-list">
          <li>Saved tabs and an ad hoc current tab.</li>
          <li>Inline rename, delete, and restore-last-view behavior.</li>
          <li>Cached row payloads so previously built views reopen instantly.</li>
        </ul>
        <div class="info-card-why"><span class="label">Useful because:</span>it turns the dashboard into a persistent research workspace instead of a one-shot ticker paste box.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Build &amp; Refresh</div>
        <div class="info-card-desc">Dashboard builds stream row-by-row, so price, momentum, valuation, and technical fields appear progressively instead of waiting for the full portfolio to finish.</div>
        <ul class="info-feature-list">
          <li>Streaming quote build and update path.</li>
          <li>Per-row error states when Yahoo has no usable data.</li>
          <li>Smart primary action that flips between Build Dashboard and Update Portfolio.</li>
        </ul>
        <div class="info-card-why"><span class="label">Useful because:</span>you get fast feedback on large portfolios and can spot broken symbols without blocking the whole run.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Portfolio Analytics</div>
        <div class="info-card-desc">The analytics panel turns the holdings table into a portfolio object with benchmark-relative risk and return math.</div>
        <ul class="info-feature-list">
          <li>Equal-weight, cap-weighted, and custom-weight modes.</li>
          <li>3M, 6M, YTD, 1Y, 3Y, 5Y, and Max windows.</li>
          <li>SPY, NASDAQ, sector-mix, and drawdown overlays.</li>
          <li>Risk &amp; Return, weighted valuation, exposure, concentration, and contribution blocks.</li>
        </ul>
        <div class="info-card-why"><span class="label">Useful because:</span>you can judge whether an idea is attractive as a portfolio, not just as a collection of individually interesting names.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Analyst Sentiment</div>
        <div class="info-card-desc">A separate dashboard aggregates sell-side coverage across the active holdings.</div>
        <ul class="info-feature-list">
          <li>Weighted consensus rating on the 1 to 5 Yahoo scale.</li>
          <li>Coverage-weighted target upside.</li>
          <li>Recommendation distribution and per-holding consensus table.</li>
          <li>Coverage gaps so you can see what part of the book analysts do not cover.</li>
        </ul>
        <div class="info-card-why"><span class="label">Useful because:</span>it separates market-implied valuation from street expectations and shows where your book is consensus or contrarian.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">FX &amp; Denomination</div>
        <div class="info-card-desc">The display currency selector converts prices, portfolio values, and analytics into a consistent denomination while keeping the original quote context available.</div>
        <ul class="info-feature-list">
          <li>Spot FX conversion across the supported major currencies.</li>
          <li>Hoverable 1Y synthetic currency basket index for context.</li>
          <li>FX-adjusted portfolio analytics in the chosen display currency.</li>
        </ul>
        <div class="info-card-why"><span class="label">Useful because:</span>it keeps return comparisons honest when the portfolio mixes USD, EUR, GBP, JPY, CHF, or CAD exposures.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Column Views &amp; Sorting</div>
        <div class="info-card-desc">The table is no longer a single fixed layout. You can switch the lens instead of forcing every idea through one preset.</div>
        <ul class="info-feature-list">
          <li>Built-in presets for Default, Fundamentals, and Momentum.</li>
          <li>Custom column views saved separately from portfolios.</li>
          <li>Drag-to-reorder headers and a dirty-state indicator for modified presets.</li>
          <li>Fit to screen compaction and a Sort menu keyed to the active visible columns.</li>
        </ul>
        <div class="info-card-why"><span class="label">Useful because:</span>screen real estate becomes a deliberate ranking tool, so you can pivot from momentum review to quality review without rebuilding data.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Row Detail Modal</div>
        <div class="info-card-desc">Clicking a row opens the heavy research view for one security.</div>
        <ul class="info-feature-list">
          <li>1M to Max chart windows with benchmark and volume overlays.</li>
          <li>Snapshot, valuation, profitability, fundamentals, dividend, profile, and news sections.</li>
          <li>Analyst targets, consensus trend, and benchmark-relative performance.</li>
        </ul>
        <div class="info-card-why"><span class="label">Useful because:</span>the table stays fast, while the modal becomes the deep-dive layer for understanding what is driving a single name.</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Excel Export</div>
        <div class="info-card-desc">Export produces one workbook across every saved portfolio, not just the active tab.</div>
        <ul class="info-feature-list">
          <li>Overview sheet plus one sheet per saved portfolio.</li>
          <li>Holdings data, analyst fields, exposure, concentration, and portfolio analytics blocks.</li>
          <li>Designed as a portable input artifact for spreadsheet work or AI-assisted review.</li>
        </ul>
        <div class="info-card-why"><span class="label">Useful because:</span>it gives you a complete offline snapshot of what the app can already see, without re-querying everything manually.</div>
      </div>
      <div class="info-card full">
        <div class="info-card-name">Column Families Beyond The Default View</div>
        <div class="info-card-desc">The app exposes more than the original default table. Optional columns already available today include:</div>
        <ul class="info-feature-list">
          <li>Momentum and technical fields: % 1W, % 1M, % 3M, % 6M, RSI 14, MACD %, Bollinger %B, 20SMA, 50SMA, and 200SMA.</li>
          <li>Fundamental and balance-sheet fields: Sector, Industry, Beta, Forward P/E, PEG, EV/Revenue, EV/EBITDA, Operating Margin, D/E, Current Ratio, and Dividend Yield.</li>
          <li>Range and analyst fields: 52W High, 52W Low, Analyst Rating, and Target Δ.</li>
        </ul>
        <div class="info-card-why"><span class="label">Useful because:</span>you can choose whether the table is a momentum board, a fundamentals screen, a risk dashboard, or an analyst-consensus monitor.</div>
      </div>
      <div class="info-card full">
        <div class="info-card-name">Core Column Reference</div>
        <div class="info-card-desc">The cards below cover the columns in the default view plus several optional metrics that matter when you switch presets.</div>
      </div>
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
        <div class="info-card-desc">Last available closing price, converted to the selected display currency (see FX selector in the top bar).</div>
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
        <div class="info-card-name">&#916; Highs &mdash; Distance from 2Y High</div>
        <div class="info-card-desc">How far the current price sits below the highest close in the table's fast-path history window (currently 2 years). 0% means the stock is at that 2Y high. The detail modal still shows a full-history ATH separately.</div>
        <div class="info-formula">$$\Delta\text{2YH} = \left(\dfrac{P}{P_{\text{2Y High}}} - 1\right) \times 100$$</div>
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
      <div class="info-card">
        <div class="info-card-name">Forward P/E</div>
        <div class="info-card-desc">Price divided by consensus forward earnings. It tells you what multiple the market is paying on the next year rather than the trailing year.</div>
        <div class="info-formula">$$\text{Forward P/E} = \dfrac{P}{\text{EPS}_{\text{NTM}}}$$</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">EV/EBITDA</div>
        <div class="info-card-desc">Enterprise-value multiple that normalises for capital structure. Useful when debt loads differ materially across peers.</div>
        <div class="info-formula">$$\text{EV/EBITDA} = \dfrac{\text{Enterprise Value}}{\text{EBITDA}}$$</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Analyst Rating</div>
        <div class="info-card-desc">Mean sell-side recommendation on Yahoo's 1 to 5 scale, where 1 is Strong Buy and 5 is Sell.</div>
        <div class="info-formula">$$R = \dfrac{\sum_{j=1}^{N} r_j}{N},\quad 1=\text{Strong Buy},\; 5=\text{Sell}$$</div>
      </div>
      <div class="info-card">
        <div class="info-card-name">Target Δ</div>
        <div class="info-card-desc">Distance from the current price to the mean analyst target price. Positive values imply upside to consensus fair value.</div>
        <div class="info-formula">$$\Delta_{target} = \left(\dfrac{TP_{mean}}{P} - 1\right) \times 100$$</div>
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
    render: (r) => fmtMoney(r.price, r.currency) },
  { key: "market_cap",  label: "Market Cap",w: 86,  align: "right", sortable: true,
    render: (r) => fmtCompactMoney(r.market_cap, r.currency) },
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
  /* Δ Highs: percent below the fast-path 2Y high. 0% = at high, -50% = halved.
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

  /* Pass D — optional columns (not in Default preset; surfaced via Fundamentals,
     Momentum, or the custom-column picker). */
  { key: "pct_1w",        label: "% 1W",    w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 20 },
    render: (r) => fmtPctSigned(r.pct_1w) },
  { key: "pct_1m",        label: "% 1M",    w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 30 },
    render: (r) => fmtPctSigned(r.pct_1m) },
  { key: "pct_3m",        label: "% 3M",    w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 50 },
    render: (r) => fmtPctSigned(r.pct_3m) },
  { key: "pct_6m",        label: "% 6M",    w: 78,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 75 },
    render: (r) => fmtPctSigned(r.pct_6m) },
  { key: "rsi_14",        label: "RSI 14",  w: 64,  align: "right", sortable: true,
    render: (r) => fmt2(r.rsi_14) },
  { key: "macd_hist_pct", label: "MACD %",  w: 72,  align: "right", sortable: true,
    heat: { kind: "div", anchor: 2 },
    render: (r) => fmtPctSigned(r.macd_hist_pct) },
  { key: "bb_pct_b",      label: "%B",      w: 54,  align: "right", sortable: true,
    render: (r) => fmt2(r.bb_pct_b) },
  { key: "beta",          label: "Beta",    w: 58,  align: "right", sortable: true,
    render: (r) => fmt2(r.beta) },
  { key: "sector",        label: "Sector",  w: 130, align: "left",  sortable: true,
    render: (r) => escapeHtml(r.sector || ""), td_cls: "left" },
  { key: "industry",      label: "Industry",w: 160, align: "left",  sortable: true,
    render: (r) => escapeHtml(r.industry || ""), td_cls: "left" },
  /* Forward P/E and EV/EBITDA — lower = cheaper. n/a (no analyst coverage)
     is painted saturated to flag the absence. */
  { key: "forward_pe",    label: "Fwd P/E", w: 64,  align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 0, clipMax: 30, naMax: true },
    render: (r) => fmt2(r.forward_pe) },
  { key: "peg",           label: "PEG",     w: 58,  align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 0, clipMax: 3, naMax: true },
    render: (r) => fmt2(r.peg) },
  { key: "ev_revenue",    label: "EV/Rev",  w: 68,  align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 0, clipMax: 10, naMax: true },
    render: (r) => fmt2(r.ev_revenue) },
  { key: "ev_ebitda",     label: "EV/EBITDA", w: 78, align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 0, clipMax: 20, naMax: true },
    render: (r) => fmt2(r.ev_ebitda) },
  { key: "operating_margin", label: "Op Mgn", w: 68, align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 0, clipMax: 0.4, invert: true, naMax: true },
    render: (r) => fmtPctDirect(r.operating_margin) },
  { key: "debt_equity",   label: "D/E",     w: 56,  align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 0, clipMax: 250, naMax: true },
    render: (r) => fmt2(r.debt_equity) },
  { key: "current_ratio", label: "Curr Ratio", w: 78, align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 0.5, clipMax: 3, invert: true, naMax: true },
    render: (r) => fmt2(r.current_ratio) },
  { key: "dividend_yield", label: "Div Yield", w: 78, align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 0, clipMax: 0.06, invert: true, naMax: true },
    render: (r) => fmtPctDirect(r.dividend_yield) },
  /* Analyst recommendation mean: 1 = Strong Buy → 5 = Sell. Lower is
     more bullish, so the ramp paints high ratings (sell side) orange. */
  { key: "analyst_rating",label: "Rating",  w: 64,  align: "right", sortable: true,
    heat: { kind: "yo", clipMin: 1, clipMax: 5, naMax: true },
    render: (r) => fmt2(r.recommendation_mean),
    sortValue: (r) => r.recommendation_mean },
  /* Target upside derived client-side from analyst mean target and last price. */
  { key: "target_upside_pct", label: "Target Δ", w: 86, align: "right", sortable: true,
    heat: { kind: "div", anchor: 30 },
    render: (r) => {
      const t = r.target_mean_price, p = r.price;
      if (t == null || p == null || !isFinite(t) || !isFinite(p) || p <= 0) return fmtPctSigned(null);
      return fmtPctSigned((t / p - 1) * 100);
    },
    sortValue: (r) => {
      const t = r.target_mean_price, p = r.price;
      if (t == null || p == null || !isFinite(t) || !isFinite(p) || p <= 0) return null;
      return (t / p - 1) * 100;
    }
  },
  { key: "w52_high",      label: "52W High",w: 90,  align: "right", sortable: true,
    render: (r) => fmtMoney(r.w52_high, r.currency) },
  { key: "w52_low",       label: "52W Low", w: 90,  align: "right", sortable: true,
    render: (r) => fmtMoney(r.w52_low, r.currency) },
];

/* Short hover descriptions for the column-header info icons.
   Long-form versions with formulas live in the About / Column Guide modal. */
const COL_INFO = {
  symbol:        "Exchange ticker symbol (e.g. AAPL, NVDA, XLK).",
  logo:          "Brand logo for the company or fund (resolved from Yahoo's website field). Purely visual — no data column behind it.",
  name:          "Full company or fund name from Yahoo Finance.",
  price:         "Last available closing price, converted to the selected display currency (see FX selector in the top bar).",
  market_cap:    "Total market value of all outstanding shares (Price × Shares Outstanding).",
  ps_ratio:      "Price-to-Sales: market cap ÷ trailing-12-month revenue. Heat anchors at P/S = 10; n/a is flagged.",
  pe_ratio:      "Price-to-Earnings: price ÷ trailing-12-month EPS. Heat anchors at P/E = 40; above 50 implies heavy growth pricing.",
  pct_ytd:       "Return from the first trading day of the current calendar year to today.",
  spark:         "Sparkline of the last 252 trading days. Green if 1Y return is positive, red otherwise.",
  pct_1y:        "Total price return over the last 365 calendar days.",
  delta_ath:     "Distance from the highest close in the table row's 2-year history window. 0% = at that high; full bar = 50% below it.",
  rs_rank:       "Relative Strength: 12 monthly bars showing where each month's close ranked within its trailing-12-month price range.",
  above_sma_20:  "20-day Simple Moving Average flag. ▲ price above SMA (bullish), ▼ below (bearish). ~1 month of trading days.",
  above_sma_50:  "50-day Simple Moving Average flag. ▲ price above SMA (bullish), ▼ below (bearish). ~1 quarter of trading days.",
  above_sma_200: "200-day Simple Moving Average flag. ▲ price above SMA (bullish), ▼ below (bearish). ~1 year of trading days.",
  pct_1w:        "Total price return over the last 7 calendar days.",
  pct_1m:        "Total price return over the last 30 calendar days.",
  pct_3m:        "Total price return over the last 91 calendar days.",
  pct_6m:        "Total price return over the last 182 calendar days.",
  rsi_14:        "14-day Relative Strength Index. Below 30 is oversold, above 70 is overbought.",
  macd_hist_pct: "MACD histogram as a percent of price. Positive means MACD is above its signal line; negative means momentum is fading.",
  bb_pct_b:      "Bollinger %B. 0 = lower band, 0.5 = middle band, 1 = upper band. Above 1 or below 0 means price is outside the bands.",
  beta:          "Yahoo-reported beta versus the market. Around 1 moves with the market; above 1 is more volatile.",
  sector:        "GICS sector (e.g. Technology, Energy) reported by Yahoo Finance.",
  industry:      "GICS sub-industry — narrower than sector.",
  forward_pe:    "Forward Price/Earnings: price ÷ consensus next-12-month EPS. Heat anchors at 30; n/a is flagged.",
  peg:           "PEG ratio: P/E divided by expected earnings growth. Lower can mean cheaper growth, though very low values can also reflect weak forecasts.",
  ev_revenue:    "Enterprise Value ÷ Revenue. Useful when earnings are noisy or negative; lower usually means cheaper on sales.",
  ev_ebitda:     "Enterprise Value ÷ EBITDA. Cap-structure-neutral valuation multiple. Heat anchors at 20.",
  operating_margin: "Operating margin as a percent of revenue. Higher means more profit retained after core operating costs.",
  debt_equity:   "Debt-to-equity ratio. Higher means more leverage relative to shareholder equity.",
  current_ratio: "Current assets divided by current liabilities. Above 1 usually signals better short-term liquidity.",
  dividend_yield: "Annual cash dividend divided by price. Higher yields can support total return but may also reflect risk.",
  analyst_rating:"Mean analyst recommendation, 1 (Strong Buy) → 5 (Sell). Lower is more bullish.",
  target_upside_pct: "Distance from current price to mean analyst price target, signed (positive = upside).",
  w52_high:      "Highest closing price over the trailing 52 weeks.",
  w52_low:       "Lowest closing price over the trailing 52 weeks.",
};

let DATA = [];
let SORT = { key: "pct_ytd", dir: -1 };

/* ===========================================================================
 * Column views (Pass D)
 * ---------------------------------------------------------------------------
 * COLS is the registry of every available column. BUILTIN_VIEWS holds the
 * three immutable presets — ordered key lists drawn from COLS. STATE owns
 * the active selection (activeViewName, customViews, activeColumnOverride),
 * but all rendering goes through getActiveColumns() so consumers never
 * need to know whether a view is built-in, custom, or a dirty in-memory
 * override.
 *
 * To add a new column: append a registry entry to COLS above (with key,
 * label, render, optional heat/align/sortable/sortValue), add a COL_INFO
 * tooltip, and — if it belongs in a preset — add its key to BUILTIN_VIEWS
 * here. The registry is the single source of truth; no other code path
 * should hardcode column keys outside that table.
 * --------------------------------------------------------------------------- */
const BUILTIN_VIEW_ALIASES = {
  "IB View": "Fundamentals",
  "Trader View": "Momentum",
};
const BUILTIN_VIEWS = {
  "Default":      ["logo","symbol","name","price","market_cap","ps_ratio","pe_ratio","pct_ytd","spark","pct_1y","delta_ath","rs_rank","above_sma_20","above_sma_50","above_sma_200"],
  "Fundamentals": ["symbol","price","market_cap","sector","industry","ps_ratio","pe_ratio","forward_pe","peg","ev_revenue","ev_ebitda","operating_margin","debt_equity","current_ratio","dividend_yield"],
  "Momentum":     ["symbol","price","pct_1w","pct_1m","pct_3m","pct_6m","pct_ytd","rsi_14","macd_hist_pct","bb_pct_b","beta","spark","pct_1y","delta_ath","rs_rank","above_sma_20","above_sma_50","above_sma_200"],
};
const BUILTIN_ORDER = ["Default", "Fundamentals", "Momentum"];
const COLS_BY_KEY = Object.fromEntries(COLS.map(c => [c.key, c]));
const DESC_DEFAULT_KEYS = new Set(["pct_ytd","pct_1y","pct_1w","pct_1m","pct_3m","pct_6m","delta_ath","market_cap","price","target_upside_pct"]);

function normalizeBuiltinViewName(name) {
  return BUILTIN_VIEW_ALIASES[name] || name;
}

function getActiveViewKeys() {
  if (typeof STATE !== "undefined" && STATE && Array.isArray(STATE.activeColumnOverride)) {
    return STATE.activeColumnOverride;
  }
  const name = normalizeBuiltinViewName((typeof STATE !== "undefined" && STATE && STATE.activeViewName) || "Default");
  if (BUILTIN_VIEWS[name]) return BUILTIN_VIEWS[name];
  const cv = (typeof STATE !== "undefined" && STATE && STATE.customViews) || {};
  if (cv[name] && Array.isArray(cv[name].columns)) return cv[name].columns;
  return BUILTIN_VIEWS["Default"];
}
function getActiveColumns() {
  return getActiveViewKeys().map(k => COLS_BY_KEY[k]).filter(Boolean);
}
function defaultSortDirFor(key) {
  return DESC_DEFAULT_KEYS.has(key) ? -1 : 1;
}
function ensureSortKey() {
  /* If active view doesn't include the current sort column, fall back to
     a sensible default (pct_ytd if visible, else first sortable). */
  const active = getActiveColumns();
  const stillThere = active.find(c => c.key === SORT.key && c.sortable);
  if (stillThere) return;
  const ytd = active.find(c => c.key === "pct_ytd" && c.sortable);
  if (ytd) { SORT.key = "pct_ytd"; SORT.dir = -1; return; }
  const firstSortable = active.find(c => c.sortable);
  SORT.key = firstSortable ? firstSortable.key : null;
  SORT.dir = SORT.key ? defaultSortDirFor(SORT.key) : -1;
}

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

function readFitColumnsPreference() {
  try { return localStorage.getItem("fit_columns") === "1"; }
  catch (e) { return false; }
}

function persistFitColumnsPreference(on) {
  try { localStorage.setItem("fit_columns", on ? "1" : "0"); }
  catch (e) {}
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

/* ---------------------------------------------------------------------------
 * Unified tooltip positioner — single helper used by every hover-tip in the
 * app. Replaces the per-system CSS pseudo-element pattern that could not be
 * repositioned by JS and routinely clipped off the viewport.
 *
 * placeTip(tipEl, anchorRect, opts)
 *   - anchorRect:  {left, top, right, bottom, width, height} in CSS pixels
 *                  (a DOMRect, or anything quacking like one)
 *   - opts.preferred: "below" (default) | "above"
 *   - opts.offset:    distance from anchor edge to tip edge (default 8)
 *   - opts.gap:       minimum margin to keep from viewport edge (default 8)
 *   - opts.allowFlip: switch to the other side if the preferred overflows (default true)
 * The tip is set to position:fixed with left/top in CSS pixels. The chosen
 * side is written to data-side on the tip so an arrow indicator (if any)
 * can react via CSS.
 * --------------------------------------------------------------------------- */
function placeTip(tipEl, anchorRect, opts = {}) {
  if (!tipEl) return "below";
  const preferred = opts.preferred || "below";
  const offset = opts.offset == null ? 8 : opts.offset;
  const gap = opts.gap == null ? 8 : opts.gap;
  const allowFlip = opts.allowFlip !== false;
  // Make the tip measurable without flashing it in the wrong place — render
  // hidden first, measure, then move + reveal.
  const prevVis = tipEl.style.visibility;
  tipEl.style.visibility = "hidden";
  tipEl.style.left = "0px";
  tipEl.style.top = "0px";
  // Force a clean measurement.
  const rect = tipEl.getBoundingClientRect();
  const tw = rect.width, th = rect.height;
  const vw = window.innerWidth, vh = window.innerHeight;
  // Anchor center for horizontal placement.
  const cx = anchorRect.left + (anchorRect.width || 0) / 2;
  let side = preferred;
  let top;
  if (side === "below") {
    top = anchorRect.bottom + offset;
    if (allowFlip && top + th > vh - gap) {
      const tryAbove = anchorRect.top - offset - th;
      if (tryAbove >= gap) { side = "above"; top = tryAbove; }
    }
  } else {
    top = anchorRect.top - offset - th;
    if (allowFlip && top < gap) {
      const tryBelow = anchorRect.bottom + offset;
      if (tryBelow + th <= vh - gap) { side = "below"; top = tryBelow; }
    }
  }
  // Final vertical clamp (when neither side fits cleanly, prefer the
  // preferred side and clamp into the viewport).
  top = Math.max(gap, Math.min(vh - gap - th, top));
  // Horizontal: center on the anchor, then clamp to viewport.
  let left = cx - tw / 2;
  left = Math.max(gap, Math.min(vw - gap - tw, left));
  tipEl.style.left = left + "px";
  tipEl.style.top = top + "px";
  tipEl.setAttribute("data-side", side);
  // Arrow position (if the tip uses one) — point at the anchor center.
  const arrow = tipEl.querySelector(".app-tip-arrow");
  if (arrow) {
    const ax = Math.max(8, Math.min(tw - 8, cx - left));
    arrow.style.left = (ax - 5) + "px";
  }
  tipEl.style.visibility = prevVis || "";
  return side;
}

/* ---------------------------------------------------------------------------
 * Shared [data-tip] tooltip: one <div> at body level, populated and
 * positioned on hover by the delegated handler below. Works for every
 * [data-tip] in the DOM (topbar, column-view bar, MPT controls, overlay
 * pills, table headers, …).
 * --------------------------------------------------------------------------- */
const _APP_TIP = (() => {
  let el = null, currentTarget = null;
  function ensure() {
    if (el && document.body.contains(el)) return el;
    el = document.createElement("div");
    el.className = "app-tip";
    el.setAttribute("role", "tooltip");
    const arrow = document.createElement("div");
    arrow.className = "app-tip-arrow";
    el.appendChild(arrow);
    const body = document.createElement("div");
    body.className = "app-tip-body";
    el.appendChild(body);
    document.body.appendChild(el);
    return el;
  }
  function show(target) {
    const text = target.getAttribute && target.getAttribute("data-tip");
    if (!text) return;
    const tip = ensure();
    currentTarget = target;
    tip.querySelector(".app-tip-body").textContent = text;
    const r = target.getBoundingClientRect();
    // Default to below (matches the legacy CSS placement).
    placeTip(tip, r, {preferred: "below", offset: 8, gap: 8});
    tip.classList.add("show");
  }
  function hide(target) {
    // Only hide if we're hiding from the same element we showed for, so
    // back-to-back hovers don't fight each other.
    if (target && currentTarget && target !== currentTarget) return;
    if (el) el.classList.remove("show");
    currentTarget = null;
  }
  return {show, hide, ensure};
})();

document.addEventListener("mouseover", (ev) => {
  const t = ev.target.closest && ev.target.closest("[data-tip]");
  if (!t) return;
  // Skip if the element opted out (e.g. an editor input).
  if (t.getAttribute("data-tip-off") === "1") return;
  _APP_TIP.show(t);
}, true);
document.addEventListener("mouseout", (ev) => {
  const t = ev.target.closest && ev.target.closest("[data-tip]");
  if (!t) return;
  // Don't hide if cursor moved into a descendant.
  const next = ev.relatedTarget;
  if (next && t.contains(next)) return;
  _APP_TIP.hide(t);
}, true);
document.addEventListener("scroll", () => _APP_TIP.hide(), true);
window.addEventListener("resize", () => _APP_TIP.hide());
// Dismiss tooltip on any user click — pseudo-element ::after tooltips used
// to disappear naturally because the clicked element lost :hover; with a
// detached shared tip we need to hide it explicitly when the user takes any
// real action (e.g. opening a modal that covers the trigger).
document.addEventListener("mousedown", () => _APP_TIP.hide(), true);
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") _APP_TIP.hide();
}, true);

/* ---------------------------------------------------------------------------
 * .pf-metric-tip is a richer hover popover (LaTeX + description). Keep its
 * existing CSS-driven show/hide but route position through placeTip so it
 * stops clipping near viewport edges.
 * --------------------------------------------------------------------------- */
document.addEventListener("mouseenter", (ev) => {
  const t = ev.target && ev.target.nodeType === 1 && ev.target.closest
    ? ev.target.closest("[data-info]") : null;
  if (!t) return;
  const tip = t.querySelector(":scope > .pf-metric-tip, :scope .pf-metric-tip");
  if (!tip) return;
  // Render into fixed positioning so it can escape clipping ancestors and
  // get clamped by placeTip. Remember the original inline styles so we can
  // restore them on leave (avoids permanent style mutations).
  if (!tip.dataset._origPos) {
    tip.dataset._origPos = tip.style.position || "";
    tip.dataset._origLeft = tip.style.left || "";
    tip.dataset._origTop = tip.style.top || "";
    tip.dataset._origRight = tip.style.right || "";
  }
  tip.style.position = "fixed";
  tip.style.right = "auto";
  // Force display so it can be measured; CSS :hover rule will also keep it shown.
  const prevDisplay = tip.style.display;
  tip.style.display = "block";
  const r = t.getBoundingClientRect();
  placeTip(tip, r, {preferred: "below", offset: 8, gap: 10});
  // Restore inline display so the CSS :hover rule keeps owning visibility.
  tip.style.display = prevDisplay || "";
}, true);
document.addEventListener("mouseleave", (ev) => {
  const t = ev.target && ev.target.nodeType === 1 && ev.target.closest
    ? ev.target.closest("[data-info]") : null;
  if (!t) return;
  const tip = t.querySelector(":scope > .pf-metric-tip, :scope .pf-metric-tip");
  if (!tip) return;
  // Restore original inline styles so the CSS-positioned rule reapplies on
  // the next hover if placeTip isn't reached (e.g. tip rendered after the
  // hover already started).
  if (tip.dataset._origPos != null) {
    tip.style.position = tip.dataset._origPos;
    tip.style.left = tip.dataset._origLeft;
    tip.style.top = tip.dataset._origTop;
    tip.style.right = tip.dataset._origRight;
    delete tip.dataset._origPos; delete tip.dataset._origLeft;
    delete tip.dataset._origTop; delete tip.dataset._origRight;
  }
}, true);

function fitScaleForColumns(columns = getActiveColumns()) {
  if (!STATE.fitColumns) return 1;
  const wrap = document.querySelector(".table-wrap");
  if (!wrap || !columns.length) return 1;
  const available = Math.max(320, wrap.clientWidth - 4);
  const required = columns.reduce((sum, c) => sum + Math.max(56, c.w || 80), 0);
  if (!required) return 1;
  return clamp(available / required, 0.58, 1);
}

function applyTableFitMode(columns = getActiveColumns()) {
  const wrap = document.querySelector(".table-wrap");
  if (!wrap) return 1;
  const scale = fitScaleForColumns(columns);
  wrap.classList.toggle("fit-columns", !!STATE.fitColumns);
  wrap.style.setProperty("--table-scale", scale.toFixed(3));
  return scale;
}

function toggleFitColumns() {
  STATE.fitColumns = !STATE.fitColumns;
  persistFitColumnsPreference(STATE.fitColumns);
  render();
}
function rgb(r, g, b) { return `rgb(${r|0},${g|0},${b|0})`; }
function rgbMix(c1, c2, t) {
  return rgb(lerp(c1[0], c2[0], t), lerp(c1[1], c2[1], t), lerp(c1[2], c2[2], t));
}

/* ───────────────────────────── Loading chip ─────────────────────────────
 * lcHtml(text, opts)  → returns chip markup (use inside renderers)
 * lcShow(target, text, opts) → mounts/updates a chip inside `target`
 * lcHide(target) → removes the chip
 * opts: { bar: bool, meta: string }
 * --------------------------------------------------------------------- */
function lcHtml(text, opts) {
  opts = opts || {};
  const bar = opts.bar ? `<span class="lc-bar"></span>` : "";
  const meta = (opts.meta || opts.meta === 0) ? `<span class="lc-meta">${escapeHtml(opts.meta)}</span>` : "";
  const txt = text ? `<span class="lc-text">${escapeHtml(text)}</span>` : "";
  return `<span class="lc">${bar}${txt}${meta}</span>`;
}
function _lcTarget(t) { return typeof t === "string" ? document.querySelector(t) : t; }
function lcShow(target, text, opts) {
  const el = _lcTarget(target); if (!el) return;
  let anchor = el.querySelector(":scope > .lc-anchor");
  if (!anchor) {
    anchor = document.createElement("span");
    anchor.className = "lc-anchor";
    el.appendChild(anchor);
  }
  anchor.innerHTML = lcHtml(text, opts);
}
function lcHide(target) {
  const el = _lcTarget(target); if (!el) return;
  const anchor = el.querySelector(":scope > .lc-anchor");
  if (anchor) anchor.remove();
}

/* ===========================================================================
 * FX (denomination) module
 * --------------------------------------------------------------------------- */
const FX_SUPPORTED = ["USD","EUR","GBP","JPY","CHF","CAD","AUD","NZD","CNY","ZAR","MXN","SGD","HKD","INR"];
const FX_NAMES = {
  USD: "US Dollar",       EUR: "Euro",
  GBP: "British Pound",   JPY: "Japanese Yen",
  CHF: "Swiss Franc",     CAD: "Canadian Dollar",
  AUD: "Australian Dollar", NZD: "New Zealand Dollar",
  CNY: "Chinese Yuan",    ZAR: "South African Rand",
  MXN: "Mexican Peso",    SGD: "Singapore Dollar",
  HKD: "Hong Kong Dollar", INR: "Indian Rupee",
};
const FX_SYMBOL = {
  USD: "$",   EUR: "€",   GBP: "£",   JPY: "¥",   CHF: "Fr",
  CAD: "C$",  AUD: "A$",  NZD: "NZ$", CNY: "CN¥", ZAR: "R",
  MXN: "Mex$", SGD: "S$", HKD: "HK$", INR: "₹",
};
/* Number of decimals to show for the displayed currency. JPY/HKD/CNY trade
   in much larger nominal units, so .00 looks silly. */
const FX_DECIMALS = { JPY: 0, HKD: 1, CNY: 2 };
/* USD-based: rates[ccy] = how many `ccy` per 1 USD. Populated on load. */
let FX_RATES = { USD: 1.0 };
let FX_QUOTE = "USD";       // user-selected display currency
let FX_INDEX_CACHE = {};    // { ccy: [[ts,val],...] }
let FX_INDEX_INFLIGHT = {}; // { ccy: Promise }
let FX_HOVER_REQ_ID = 0;
let FX_HOVER_CCY = null;

function fxLoadPref() {
  try {
    const saved = localStorage.getItem("fx_quote");
    if (saved && FX_SUPPORTED.indexOf(saved) >= 0) FX_QUOTE = saved;
  } catch (e) {}
}

async function fxLoadRates() {
  try {
    const r = await fetch("/api/fx-rates?base=USD");
    if (!r.ok) return;
    const j = await r.json();
    if (j && j.rates) FX_RATES = j.rates;
  } catch (e) {}
}

/* Convert `amount` from `fromCcy` to the active display currency.
   Handles Yahoo's pence/cents subunits (LSE → GBp, JSE → ZAc). */
function fxConvert(amount, fromCcy) {
  if (amount == null || !isFinite(amount)) return amount;
  let from = fromCcy || "USD";
  let scale = 1;
  if (from === "GBp" || from === "GBX") { from = "GBP"; scale = 0.01; }
  else if (from === "ZAc") { from = "ZAR"; scale = 0.01; }
  from = String(from).toUpperCase();
  const base = amount * scale;
  const to = FX_QUOTE;
  if (from === to) return base;
  const rFrom = FX_RATES[from];   // from per USD
  const rTo = FX_RATES[to];       // to per USD
  if (!rFrom || !rTo) return base;  // graceful: no conversion data
  return base * (rTo / rFrom);
}

function fxDecimals(ccy) {
  const d = FX_DECIMALS[ccy || FX_QUOTE];
  return d == null ? 2 : d;
}

function fmtMoney(v, ccy) {
  if (v == null || !isFinite(v)) return na();
  const converted = fxConvert(v, ccy);
  const sym = FX_SYMBOL[FX_QUOTE] || (FX_QUOTE + " ");
  const dec = fxDecimals(FX_QUOTE);
  return sym + Number(converted).toLocaleString(undefined, {minimumFractionDigits: dec, maximumFractionDigits: dec});
}
function fmtCompactMoney(v, ccy) {
  if (v == null || !isFinite(v) || v === 0) return na();
  const converted = fxConvert(v, ccy);
  const sym = FX_SYMBOL[FX_QUOTE] || (FX_QUOTE + " ");
  const a = Math.abs(converted);
  let unit, scaled;
  if (a >= 1e12) { unit = "T"; scaled = converted/1e12; }
  else if (a >= 1e9) { unit = "B"; scaled = converted/1e9; }
  else if (a >= 1e6) { unit = "M"; scaled = converted/1e6; }
  else if (a >= 1e3) { unit = "K"; scaled = converted/1e3; }
  else { return sym + converted.toFixed(2); }
  return sym + scaled.toFixed(1) + unit;
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
    /* Fixed-ceiling orange ramp. n/a → max if h.naMax.
       h.invert flips the ramp so lower values get more saturated colour
       (used for analyst-rating, where 1 = strong buy, 5 = sell). */
    let t;
    if (value == null || !isFinite(value)) {
      if (!h.naMax) return "";
      t = 1;
    } else {
      const lo = h.clipMin, hi = h.clipMax;
      t = (hi === lo) ? 0.5 : clamp((value - lo) / (hi - lo), 0, 1);
      if (h.invert) t = 1 - t;
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
 * Column-view bar, customize modal, and header drag-and-drop (Pass D)
 * ---------------------------------------------------------------------------
 * UI surface: a slim row above the table with built-in preset chips,
 * a "+ Custom" dropdown listing user-saved views, and a Customize button
 * that opens the modal. STATE.activeViewName drives which set of column
 * keys getActiveColumns() returns; activeColumnOverride holds the live
 * in-memory order when the user is dragging a built-in preset (the
 * yellow "Modified" pill exposes Save-as-new and Reset actions).
 * --------------------------------------------------------------------------- */

const CV_CUSTOM_KEY = "__cv_custom__";

function isBuiltinView(name) { return Object.prototype.hasOwnProperty.call(BUILTIN_VIEWS, normalizeBuiltinViewName(name)); }

function sameColumnKeys(left, right) {
  if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
  for (let i = 0; i < left.length; i++) {
    if (left[i] !== right[i]) return false;
  }
  return true;
}

function syncBuiltinDirtyState(keys) {
  if (!isBuiltinView(STATE.activeViewName)) {
    STATE.activeColumnOverride = null;
    STATE.viewDirty = false;
    return;
  }
  const baseKeys = BUILTIN_VIEWS[normalizeBuiltinViewName(STATE.activeViewName)] || [];
  if (sameColumnKeys(keys, baseKeys)) {
    STATE.activeColumnOverride = null;
    STATE.viewDirty = false;
    return;
  }
  STATE.activeColumnOverride = keys.slice();
  STATE.viewDirty = true;
}

function currentActiveKeys() {
  /* Returns the live list of keys for the active view (override wins). */
  if (Array.isArray(STATE.activeColumnOverride)) return STATE.activeColumnOverride.slice();
  if (isBuiltinView(STATE.activeViewName)) return BUILTIN_VIEWS[normalizeBuiltinViewName(STATE.activeViewName)].slice();
  const cv = STATE.customViews[STATE.activeViewName];
  return cv && Array.isArray(cv.columns) ? cv.columns.slice() : BUILTIN_VIEWS["Default"].slice();
}

async function loadColumnViews() {
  try {
    const r = await fetch("/api/column-views");
    if (!r.ok) return;
    const j = await r.json();
    STATE.customViews = j.custom || {};
    const desired = normalizeBuiltinViewName(j.active || "Default");
    if (isBuiltinView(desired) || STATE.customViews[desired]) {
      STATE.activeViewName = desired;
    } else {
      STATE.activeViewName = "Default";
    }
    STATE.activeColumnOverride = null;
    STATE.viewDirty = false;
    render();
  } catch (e) { /* persistence is best-effort */ }
}

async function persistActiveView(name) {
  try {
    await fetch("/api/column-views/active", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name}),
    });
  } catch (e) { /* best-effort */ }
}

let _cvSaveTimer = null;
function debouncedSaveCustom(name, columns) {
  clearTimeout(_cvSaveTimer);
  _cvSaveTimer = setTimeout(async () => {
    try {
      const r = await fetch("/api/column-views", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name, columns}),
      });
      if (r.ok) {
        const j = await r.json();
        STATE.customViews = j.custom || STATE.customViews;
      }
    } catch (e) {}
  }, 250);
}

function setActiveView(name, {persist = true} = {}) {
  name = normalizeBuiltinViewName(name);
  if (!isBuiltinView(name) && !STATE.customViews[name]) return;
  STATE.activeViewName = name;
  STATE.activeColumnOverride = null;
  STATE.viewDirty = false;
  render();
  if (persist) persistActiveView(name);
}

function renderColumnViewBar() {
  const seg = document.getElementById("cv-builtins");
  if (!seg) return;
  seg.innerHTML = "";
  const activeName = normalizeBuiltinViewName(STATE.activeViewName || "Default");
  for (const name of BUILTIN_ORDER) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = name;
    b.setAttribute("role", "tab");
    if (name === activeName) b.classList.add("active");
    b.onclick = () => setActiveView(name);
    seg.appendChild(b);
  }
  /* Custom-view dropdown */
  const wrap = document.getElementById("cv-custom-wrap");
  wrap.innerHTML = "";
  const customNames = Object.keys(STATE.customViews || {}).sort();
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "cv-custom-btn";
  const isCustomActive = !isBuiltinView(activeName);
  if (isCustomActive) btn.classList.add("active");
  btn.innerHTML = `<span>${isCustomActive ? escapeHtml(activeName) : "Custom"}</span><span class="cv-caret">▾</span>`;
  const dd = document.createElement("div");
  dd.className = "cv-custom-dropdown";
  if (!customNames.length) {
    const e = document.createElement("div");
    e.className = "cv-cv-empty";
    e.textContent = "No saved views yet.";
    dd.appendChild(e);
  } else {
    for (const name of customNames) {
      const row = document.createElement("div");
      row.className = "cv-cv-row" + (name === activeName ? " active" : "");
      const lbl = document.createElement("span");
      lbl.textContent = name;
      lbl.style.flex = "1";
      lbl.onclick = () => { dd.classList.remove("open"); setActiveView(name); };
      const del = document.createElement("button");
      del.className = "cv-cv-del"; del.type = "button"; del.textContent = "×";
      del.title = "Delete view";
      del.onclick = async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete view "${name}"?`)) return;
        try {
          const r = await fetch(`/api/column-views/${encodeURIComponent(name)}`, {method: "DELETE"});
          if (r.ok) {
            const j = await r.json();
            STATE.customViews = j.custom || {};
            if (STATE.activeViewName === name) STATE.activeViewName = j.active || "Default";
            render();
          }
        } catch (err) {}
      };
      row.appendChild(lbl); row.appendChild(del);
      dd.appendChild(row);
    }
  }
  btn.onclick = (e) => {
    e.stopPropagation();
    dd.classList.toggle("open");
  };
  document.addEventListener("click", () => dd.classList.remove("open"), {once: true});
  wrap.appendChild(btn); wrap.appendChild(dd);
  /* Dirty pill */
  const dirty = document.getElementById("cv-dirty");
  if (dirty) dirty.hidden = !STATE.viewDirty;
  const fitBtn = document.getElementById("cv-fit-toggle");
  if (fitBtn) {
    fitBtn.classList.toggle("active", !!STATE.fitColumns);
    fitBtn.setAttribute("aria-pressed", STATE.fitColumns ? "true" : "false");
  }
}

function resetViewOverride() {
  STATE.activeColumnOverride = null;
  STATE.viewDirty = false;
  render();
}

async function promptAndSaveCurrentAsNew() {
  const suggested = isBuiltinView(STATE.activeViewName)
    ? `${STATE.activeViewName} (custom)` : `${STATE.activeViewName} copy`;
  const name = (prompt("Save current column layout as:", suggested) || "").trim();
  if (!name) return;
  if (isBuiltinView(name)) { alert(`"${name}" is a built-in name; pick another.`); return; }
  const columns = currentActiveKeys();
  try {
    const r = await fetch("/api/column-views", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name, columns}),
    });
    if (!r.ok) { const j = await r.json().catch(()=>({})); alert(j.error || "Save failed."); return; }
    const j = await r.json();
    STATE.customViews = j.custom || STATE.customViews;
    STATE.activeColumnOverride = null;
    STATE.viewDirty = false;
    setActiveView(name);
  } catch (e) { alert("Save failed."); }
}

/* ----- Customize modal ----- */
let CV_MODAL_STATE = null;  // {selected: Set<key>, order: string[]}

function openColumnPicker() {
  const bg = document.getElementById("cv-modal-bg");
  const modal = document.getElementById("cv-modal");
  if (!bg || !modal) return;
  const activeKeys = currentActiveKeys();
  const activeSet = new Set(activeKeys);
  /* Build a working order: active keys first (in current order), then
     remaining registry keys appended at the end. */
  const remaining = COLS.map(c => c.key).filter(k => !activeSet.has(k));
  CV_MODAL_STATE = {
    selected: new Set(activeKeys),
    order: activeKeys.concat(remaining),
  };
  const editingActiveCustom = !isBuiltinView(STATE.activeViewName) && STATE.customViews[STATE.activeViewName];
  modal.innerHTML = `
    <h2>Customize Columns</h2>
    <div class="cv-modal-sub">Toggle which columns appear and drag to reorder. ${editingActiveCustom ? `Editing <b>${escapeHtml(STATE.activeViewName)}</b>.` : "Save as a new view when you're done."}</div>
    <ul class="cv-list" id="cv-modal-list"></ul>
    <div class="cv-modal-foot">
      <input type="text" class="cv-name-input" id="cv-name-input" placeholder="${editingActiveCustom ? "New name (optional)" : "View name"}" value="${editingActiveCustom ? "" : ""}" />
      <button id="cv-modal-cancel">Cancel</button>
      ${editingActiveCustom ? `<button id="cv-modal-update" class="primary">Update "${escapeHtml(STATE.activeViewName)}"</button>` : ""}
      <button id="cv-modal-save" class="primary">${editingActiveCustom ? "Save as new" : "Save view"}</button>
    </div>
  `;
  renderColumnPickerList();
  bg.hidden = false;
  bg.classList.add("show");
  document.getElementById("cv-modal-cancel").onclick = closeColumnPicker;
  bg.onclick = (e) => { if (e.target === bg) closeColumnPicker(); };
  document.getElementById("cv-modal-save").onclick = () => saveColumnPicker({asNew: true});
  const upd = document.getElementById("cv-modal-update");
  if (upd) upd.onclick = () => saveColumnPicker({asNew: false});
}

function closeColumnPicker() {
  const bg = document.getElementById("cv-modal-bg");
  if (bg) {
    bg.classList.remove("show");
    bg.hidden = true;
  }
  CV_MODAL_STATE = null;
}

function renderColumnPickerList() {
  const ul = document.getElementById("cv-modal-list");
  if (!ul || !CV_MODAL_STATE) return;
  ul.innerHTML = "";
  for (const key of CV_MODAL_STATE.order) {
    const c = COLS_BY_KEY[key];
    if (!c) continue;
    const li = document.createElement("li");
    li.draggable = true;
    li.dataset.key = key;
    const grip = document.createElement("span"); grip.className = "cv-grip"; grip.textContent = "⋮⋮";
    const cb = document.createElement("input"); cb.type = "checkbox";
    cb.checked = CV_MODAL_STATE.selected.has(key);
    cb.onchange = () => {
      if (cb.checked) CV_MODAL_STATE.selected.add(key);
      else CV_MODAL_STATE.selected.delete(key);
    };
    const lbl = document.createElement("span"); lbl.className = "cv-li-label";
    lbl.textContent = c.label || key;
    const tag = document.createElement("span"); tag.className = "cv-li-tag";
    tag.textContent = key;
    li.appendChild(grip); li.appendChild(cb); li.appendChild(lbl); li.appendChild(tag);
    /* DnD */
    li.addEventListener("dragstart", (ev) => {
      li.classList.add("cv-li-drag");
      ev.dataTransfer.setData("text/plain", key);
      ev.dataTransfer.effectAllowed = "move";
    });
    li.addEventListener("dragend", () => li.classList.remove("cv-li-drag"));
    li.addEventListener("dragover", (ev) => { ev.preventDefault(); li.classList.add("cv-li-over"); });
    li.addEventListener("dragleave", () => li.classList.remove("cv-li-over"));
    li.addEventListener("drop", (ev) => {
      ev.preventDefault();
      li.classList.remove("cv-li-over");
      const fromKey = ev.dataTransfer.getData("text/plain");
      if (!fromKey || fromKey === key) return;
      const arr = CV_MODAL_STATE.order;
      const fromIdx = arr.indexOf(fromKey);
      const toIdx = arr.indexOf(key);
      if (fromIdx < 0 || toIdx < 0) return;
      arr.splice(fromIdx, 1);
      arr.splice(toIdx, 0, fromKey);
      renderColumnPickerList();
    });
    ul.appendChild(li);
  }
}

async function saveColumnPicker({asNew}) {
  if (!CV_MODAL_STATE) return;
  const cols = CV_MODAL_STATE.order.filter(k => CV_MODAL_STATE.selected.has(k));
  if (!cols.length) { alert("Select at least one column."); return; }
  if (!cols.includes("symbol")) {
    if (!confirm("This view doesn't include the Ticker column. Save anyway?")) return;
  }
  let name;
  const inputVal = (document.getElementById("cv-name-input").value || "").trim();
  if (asNew) {
    name = inputVal;
    if (!name) { alert("Enter a name for the new view."); return; }
    if (isBuiltinView(name)) { alert(`"${name}" is a built-in name; pick another.`); return; }
  } else {
    name = STATE.activeViewName;
  }
  try {
    const r = await fetch("/api/column-views", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name, columns: cols}),
    });
    if (!r.ok) { const j = await r.json().catch(()=>({})); alert(j.error || "Save failed."); return; }
    const j = await r.json();
    STATE.customViews = j.custom || STATE.customViews;
    closeColumnPicker();
    setActiveView(name);
  } catch (e) { alert("Save failed."); }
}

function wireColumnViewBar() {
  const cb = document.getElementById("cv-customize");
  if (cb && !cb._wired) { cb.onclick = openColumnPicker; cb._wired = true; }
  const fit = document.getElementById("cv-fit-toggle");
  if (fit && !fit._wired) { fit.onclick = toggleFitColumns; fit._wired = true; }
  const ds = document.getElementById("cv-dirty-save");
  if (ds && !ds._wired) { ds.onclick = promptAndSaveCurrentAsNew; ds._wired = true; }
  const dr = document.getElementById("cv-dirty-reset");
  if (dr && !dr._wired) { dr.onclick = resetViewOverride; dr._wired = true; }
}

/* ----- Header drag-and-drop reordering ----- */
function wireHeaderDnD(tr) {
  const ths = Array.from(tr.querySelectorAll("th"));
  for (const th of ths) {
    th.draggable = true;
    th.addEventListener("dragstart", (ev) => {
      th._dragStartX = ev.clientX;
      th.classList.add("cv-th-drag");
      ev.dataTransfer.setData("text/plain", th.dataset.colKey);
      ev.dataTransfer.effectAllowed = "move";
    });
    th.addEventListener("dragend", () => {
      th.classList.remove("cv-th-drag");
      /* Suppress the click that fires when a drag ends on the same th. */
      th._suppressClick = true;
      setTimeout(() => { th._suppressClick = false; }, 0);
    });
    th.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      th.classList.add("cv-th-over");
    });
    th.addEventListener("dragleave", () => th.classList.remove("cv-th-over"));
    th.addEventListener("drop", (ev) => {
      ev.preventDefault();
      th.classList.remove("cv-th-over");
      const fromKey = ev.dataTransfer.getData("text/plain");
      const toKey = th.dataset.colKey;
      if (!fromKey || !toKey || fromKey === toKey) return;
      reorderActiveColumns(fromKey, toKey);
    });
  }
}

function reorderActiveColumns(fromKey, toKey) {
  const cur = currentActiveKeys();
  const fromIdx = cur.indexOf(fromKey);
  const toIdx = cur.indexOf(toKey);
  if (fromIdx < 0 || toIdx < 0) return;
  cur.splice(fromIdx, 1);
  cur.splice(toIdx, 0, fromKey);
  if (isBuiltinView(STATE.activeViewName)) {
    syncBuiltinDirtyState(cur);
    render();
  } else {
    /* Custom view: persist new order immediately. */
    if (STATE.customViews[STATE.activeViewName]) {
      STATE.customViews[STATE.activeViewName].columns = cur;
    }
    STATE.activeColumnOverride = null;
    STATE.viewDirty = false;
    render();
    debouncedSaveCustom(STATE.activeViewName, cur);
  }
}


/* Sort menu removed — column-header click handles sorting. */

/* ===========================================================================
 * Render
 * --------------------------------------------------------------------------- */
function renderHeader(scale = 1) {
  const tr = $("#thead"); tr.innerHTML = "";
  const cols = getActiveColumns();
  for (const c of cols) {
    const th = document.createElement("th");
    th.textContent = c.label || "";
    const width = Math.round((c.w || 80) * scale);
    th.style.minWidth = width + "px";
    th.style.width = width + "px";
    th.dataset.colKey = c.key;
    if (!c.sortable) th.classList.add("no-sort");
    if (c.sortable) {
      th.onclick = (ev) => {
        /* Suppress click that fires at the end of a drag-reorder gesture. */
        if (th._suppressClick) { th._suppressClick = false; return; }
        if (SORT.key === c.key) SORT.dir *= -1;
        else { SORT.key = c.key; SORT.dir = defaultSortDirFor(c.key); }
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
  if (typeof wireHeaderDnD === "function") wireHeaderDnD(tr);
}

function render() {
  ensureSortKey();
  const cols = getActiveColumns();
  const scale = applyTableFitMode(cols);
  renderHeader(scale);
  if (typeof renderColumnViewBar === "function") renderColumnViewBar();
  const tbody = $("#tbody"); tbody.innerHTML = "";
  if (!DATA.length) {
    tbody.innerHTML = `<tr><td colspan="${cols.length}" style="padding:30px; text-align:center; color:var(--muted);">Press <b>Build Dashboard</b> above to load your portfolio.</td></tr>`;
    return;
  }
  let rows = DATA.slice();
  if (SORT.key) {
    const col = COLS_BY_KEY[SORT.key];
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
    for (const c of cols) {
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
  const change = (r.change_abs_1d != null)
    ? (r.change_abs_1d >= 0 ? "+" : "−") + fmtMoney(Math.abs(r.change_abs_1d), r.currency)
    : "—";
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
        <div class="p-now">${fmtMoney(r.price, r.currency)}</div>
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
        <div id="m-loading" style="position:absolute; inset:0; display:flex; align-items:center; justify-content:center;">${lcHtml("fetching detail", {bar: true})}</div>
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
      <div class="tt-row"><span class="tt-label">Price</span><b>${fmtMoney(sp[1], DETAIL.data && DETAIL.data.currency)}</b></div>
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
const DETAIL_METRIC_INFO = {
  "Volume": {
    formula: String.raw`\text{shares traded today}`,
    desc: "Total shares traded in the latest session. It is a liquidity read, not a valuation signal.",
    range: "Compare it with Avg Volume. A large spike often means news, earnings, rebalancing, or stress."
  },
  "Avg Volume": {
    formula: String.raw`\overline{V} = \dfrac{1}{n}\sum_{t=1}^{n} V_t`,
    desc: "Average daily trading volume over Yahoo's lookback window, used as the baseline liquidity reference.",
    range: "If current volume is far above this level, participation is unusual and the move may be more informative."
  },
  "52W High": {
    formula: String.raw`\max(P_t),\; t \in \text{last 52 weeks}`,
    desc: "Highest price reached in the trailing 52-week window.",
    range: "Names near the high are often in strong trends; deep gaps below it indicate a prior drawdown."
  },
  "52W Low": {
    formula: String.raw`\min(P_t),\; t \in \text{last 52 weeks}`,
    desc: "Lowest price reached in the trailing 52-week window.",
    range: "Useful for judging whether a stock is still washed out or already recovering from its low."
  },
  "ATH": {
    formula: String.raw`\max(P_t),\; t \in \text{available history}`,
    desc: "Highest price in the full available price history, not just the trailing year.",
    range: "The gap between spot and ATH is a quick read on how much prior optimism has been unwound."
  },
  "Market Cap": {
    formula: String.raw`MC = P \times \text{Shares Outstanding}`,
    desc: "Total equity value of the company at the current price.",
    range: "Useful for sizing the business and understanding whether you are buying a mega-cap, mid-cap, or micro-cap risk profile."
  },
  "Shares Out": {
    formula: String.raw`\text{issued shares currently outstanding}`,
    desc: "Number of shares currently outstanding. It moves with buybacks, issuance, stock-based comp, and corporate actions.",
    range: "Shrinking share counts support per-share growth; rising counts can dilute existing holders."
  },
  "Beta": {
    formula: String.raw`\beta = \dfrac{\mathrm{Cov}(r_i, r_m)}{\mathrm{Var}(r_m)}`,
    desc: "Yahoo-reported market beta. It estimates how sensitively the stock tends to move versus the market benchmark.",
    range: "Around 1 behaves like the market. Below 1 is more defensive; above 1.3 is usually high-beta growth or cyclicality."
  },
  "Revenue (TTM)": {
    formula: String.raw`\text{sales over the trailing 12 months}`,
    desc: "Trailing-12-month revenue. This is the current annualised top-line scale of the business.",
    range: "Best used with growth and margin metrics. Sales alone say nothing about quality or profitability."
  },
  "Revenue Growth": {
    formula: String.raw`g = \dfrac{\text{Revenue}_{TTM} - \text{Revenue}_{prior}}{\text{Revenue}_{prior}}`,
    desc: "Year-over-year revenue growth rate.",
    range: "Mid-single digits is mature; teens are healthy; 30%+ usually implies high-growth expectations and tougher comps ahead."
  },
  "Free Cash Flow": {
    formula: String.raw`FCF = CFO - CapEx`,
    desc: "Cash left after operating cash flow covers capital expenditures. It is the cleanest internal funding source for buybacks, dividends, and debt paydown.",
    range: "Positive and rising is a quality signal. Negative FCF can be fine in investment-heavy businesses, but it raises the financing burden."
  },
  "FCF Yield": {
    formula: String.raw`FCF\ Yield = \dfrac{FCF}{\text{Market Cap}}`,
    desc: "Free cash flow scaled by equity value. It is the cash-flow analogue of an earnings yield.",
    range: "Low single digits is common for quality growth. High single digits can mean cheap cash generation or market skepticism."
  },
  "Fwd P/E": {
    formula: String.raw`\text{Fwd P/E} = \dfrac{P}{\text{EPS}_{NTM}}`,
    desc: "Price divided by consensus next-12-month earnings per share.",
    range: "Lower than trailing P/E can indicate expected earnings growth; higher can mean analysts see an earnings dip ahead."
  },
  "P/E (TTM)": {
    formula: String.raw`P/E = \dfrac{P}{\text{EPS}_{TTM}}`,
    desc: "Trailing price-to-earnings multiple using the last 12 months of earnings.",
    range: "Broad-market quality names often live in the high teens to mid-20s. Very high multiples imply strong growth expectations."
  },
  "EV/EBITDA": {
    formula: String.raw`\dfrac{EV}{EBITDA}`,
    desc: "Enterprise value divided by EBITDA. It compares the total business value to an operating cash-earnings proxy.",
    range: "Useful across peers with different debt loads. Higher values usually mean better growth, higher quality, or richer pricing."
  },
  "EV/Revenue": {
    formula: String.raw`\dfrac{EV}{Revenue}`,
    desc: "Enterprise value divided by revenue. Often more informative than earnings multiples when margins are still immature.",
    range: "Best used for software, platforms, and cyclical turnarounds where earnings are temporarily noisy."
  },
  "Operating Mgn": {
    formula: String.raw`\text{Operating Margin} = \dfrac{\text{Operating Income}}{Revenue}`,
    desc: "Share of revenue left after core operating costs, before interest and taxes.",
    range: "Higher usually means stronger business quality, pricing power, or scale. Compare within the same industry, not across all sectors."
  },
  "Gross Margin": {
    formula: String.raw`\text{Gross Margin} = \dfrac{Revenue - COGS}{Revenue}`,
    desc: "Share of revenue left after direct production or delivery costs.",
    range: "High gross margins often signal pricing power or software-like economics, but they need operating discipline to turn into profits."
  },
  "Profit Margin": {
    formula: String.raw`\text{Profit Margin} = \dfrac{\text{Net Income}}{Revenue}`,
    desc: "Net income as a share of revenue after all expenses.",
    range: "A compact measure of business efficiency, but it can swing with tax effects, interest costs, and one-off items."
  },
  "ROE": {
    formula: String.raw`ROE = \dfrac{\text{Net Income}}{\text{Shareholders' Equity}}`,
    desc: "Return on equity measures how efficiently management converts book equity into earnings.",
    range: "Higher is usually better, but leverage can inflate ROE, so read it together with D/E."
  },
  "D/E": {
    formula: String.raw`D/E = \dfrac{\text{Total Debt}}{\text{Shareholders' Equity}}`,
    desc: "Debt-to-equity ratio. It shows how much leverage sits on top of the equity base.",
    range: "Low values imply balance-sheet flexibility. High values can amplify returns in good times and pain in bad times."
  },
};
function metricTipHtml(label, info) {
  if (!info) return "";
  return `
    <div class="pf-metric-tip" role="tooltip">
      <div class="mt-name">${escapeHtml(label)}</div>
      <div class="mt-formula">$$${info.formula}$$</div>
      <div class="mt-desc">${escapeHtml(info.desc)}</div>
      <div class="mt-range">${escapeHtml(info.range)}</div>
    </div>`;
}
function detailMetricCellHtml(item, idx) {
  const label = Array.isArray(item) ? item[0] : item.label;
  const value = Array.isArray(item) ? item[1] : item.value;
  const info = (Array.isArray(item) ? null : item.info) || DETAIL_METRIC_INFO[label];
  const dataAttr = info ? ` data-info="1" data-metric="${escapeHtml(label)}"` : "";
  const sideCls = idx % 2 ? " tip-right" : "";
  return `<div class="metric-tip-host${sideCls}"${dataAttr}><div class="k">${label}</div><div class="v">${value}</div>${metricTipHtml(label, info)}</div>`;
}
function renderDetailMetricGrid(items) {
  return `<div class="m-kv">${items.map((item, idx) => detailMetricCellHtml(item, idx)).join("")}</div>`;
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
    ["52W High", fmtMoney(d.w52_high, d.currency)],
    ["52W Low", fmtMoney(d.w52_low, d.currency)],
    ["ATH", fmtMoney(d.ath, d.currency)],
    ["Market Cap", fmtCompactMoney(d.market_cap, d.currency)],
    ["Shares Out", fmtCompactNum(d.shares)],
    ["Beta", fmt2(d.beta)],
  ];

  // ---- Valuation
  const val = [
    ["Revenue (TTM)", fmtCompactMoney(d.total_revenue, d.currency)],
    ["Revenue Growth", fmtPctFrac(d.revenue_growth)],
    ["Free Cash Flow", fmtCompactMoney(d.free_cashflow, d.currency)],
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
          <span class="tb-low">${fmtMoney(d.target_low, d.currency)}</span>
          <div class="tb-mark current" style="left:${pct(d.price).toFixed(1)}%" title="Current"></div>
          ${d.target_mean != null ? `<div class="tb-mark target" style="left:${pct(d.target_mean).toFixed(1)}%" title="Mean target"></div>` : ""}
          <span class="tb-high">${fmtMoney(d.target_high, d.currency)}</span>
        </div>
        <div class="m-target-labels">
          <span>Current <b>${fmtMoney(d.price, d.currency)}</b></span>
          <span>Mean target <b>${fmtMoney(d.target_mean, d.currency)}</b>
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
    ["Revenue (TTM)", fmtCompactMoney(d.total_revenue, d.currency)],
    ["Revenue Growth", fmtPctFrac(d.revenue_growth)],
    ["Earnings Growth", fmtPctFrac(d.earnings_growth)],
    ["Free Cash Flow", fmtCompactMoney(d.free_cashflow, d.currency)],
    ["ROA", fmtPctFrac(d.roa)],
    ["Current Ratio", fmt2(d.current_ratio)],
  ];

  // ---- Dividend (only if any data)
  let divHtml = "";
  if (d.dividend_yield != null || d.dividend_rate != null || d.ex_div_date) {
    const divItems = [
      ["Yield", fmtPctDirect(d.dividend_yield)],
      ["Rate", fmtMoney(d.dividend_rate, d.currency)],
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
      ${renderDetailMetricGrid(snap)}
    </div>
    <div class="m-sec">
      <h3>Valuation &amp; Profitability</h3>
      ${renderDetailMetricGrid(val)}
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
  renderStatTipsKatex("#m-sections");
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
  mode: "cap",             // "equal" | "cap" | "custom" | "preset:<name>"
  customWeights: null,     // {symbol: fraction}, set after user applies the popup
  period: "1Y",
  showSpy: true,
  showNdx: false,
  showSec: false,
  showDd: true,
  analytics: null,
  analyticsLoading: false,
  // {tabName: {"<mode>|<period>|<ccy>": result}} — persisted per tab so
  // switching back to a previously-visited tab paints from memory.
  analyticsByTab: {},
  // Named weight presets for the active portfolio. Loaded from the server
  // each time the active tab changes; saved/edited via the weights popup.
  weightPresets: [],       // [{name, weights, saved_at}]
  // Pass D — column views. activeViewName is global (one selection
  // across all portfolios). customViews mirrors the server's
  // .portfolio_tracker_column_views.json. activeColumnOverride is set
  // when the user drags headers on a built-in view (transient until
  // saved/reset).
  activeViewName: "Default",
  customViews: {},
  activeColumnOverride: null,
  viewDirty: false,
  fitColumns: readFitColumnsPreference(),
};

/* ===========================================================================
 * Weight-mode helpers (Equal / Cap / named presets / ad-hoc custom)
 * --------------------------------------------------------------------------- */
function presetByName(name) {
  if (!name || !STATE.weightPresets) return null;
  return STATE.weightPresets.find(p => p && p.name === name) || null;
}
function activePresetName() {
  if (typeof STATE.mode !== "string") return null;
  return STATE.mode.startsWith("preset:") ? STATE.mode.slice(7) : null;
}
function modeId(name) { return "preset:" + name; }
function weightsForMode(mode) {
  // Returns a {symbol: fraction} dict appropriate for `mode`, normalised over
  // the currently-loaded DATA symbols. Falls back to cap-weight on unknown modes.
  const rows = DATA.filter(r => r && r.symbol);
  if (mode === "equal") return equalWeightsOf(rows);
  if (mode === "cap")   return capWeightsOf(rows);
  if (mode === "custom") return STATE.customWeights || capWeightsOf(rows);
  if (typeof mode === "string" && mode.startsWith("preset:")) {
    const p = presetByName(mode.slice(7));
    if (!p) return capWeightsOf(rows);
    // Project preset weights onto current symbols and renormalise. Missing
    // symbols silently default to 0.
    const out = {}; let total = 0;
    for (const r of rows) { const w = Math.max(0, Number(p.weights[r.symbol] || 0)); out[r.symbol] = w; total += w; }
    if (total <= 0) return capWeightsOf(rows);
    for (const k of Object.keys(out)) out[k] /= total;
    return out;
  }
  return capWeightsOf(rows);
}
async function loadPresetsForView(name) {
  // Anonymous / unsaved tabs don't have presets — keep the list empty.
  if (!name || name === AD_HOC_KEY) {
    STATE.weightPresets = [];
    return;
  }
  try {
    const r = await fetch(`/api/weight-presets?view=${encodeURIComponent(name)}`);
    const d = await r.json();
    STATE.weightPresets = Array.isArray(d.presets) ? d.presets : [];
    // Honor the server-side active selection on initial load so the user's
    // last choice is restored across sessions. We only apply it if the
    // current mode is the safe default ("cap").
    if (d.active && STATE.mode === "cap" && presetByName(d.active)) {
      STATE.mode = modeId(d.active);
    }
  } catch (_) {
    STATE.weightPresets = [];
  }
}
async function persistActivePreset() {
  // Tell the server which preset (or none) is currently active for the
  // active view so it can be restored next launch.
  const view = STATE.activeView;
  if (!view || view === AD_HOC_KEY) return;
  const name = activePresetName();
  try {
    await fetch("/api/weight-presets/active", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({view, name}),
    });
  } catch (_) {}
}
async function savePresetServer(name, weights, opts) {
  opts = opts || {};
  const view = STATE.activeView;
  if (!view || view === AD_HOC_KEY) throw new Error("Save the portfolio first.");
  const body = {view, name, weights, set_active: opts.setActive !== false};
  if (opts.renameFrom) body.rename_from = opts.renameFrom;
  const r = await fetch("/api/weight-presets", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "save failed");
  STATE.weightPresets = Array.isArray(d.presets) ? d.presets : STATE.weightPresets;
  return d;
}
async function deletePresetServer(name) {
  const view = STATE.activeView;
  if (!view || view === AD_HOC_KEY) return;
  const url = `/api/weight-presets?view=${encodeURIComponent(view)}&name=${encodeURIComponent(name)}`;
  const r = await fetch(url, {method: "DELETE"});
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "delete failed");
  STATE.weightPresets = Array.isArray(d.presets) ? d.presets : [];
}

function analyticsMapForTab(name) {
  const key = name || AD_HOC_KEY;
  let m = STATE.analyticsByTab[key];
  if (!m) { m = {}; STATE.analyticsByTab[key] = m; }
  return m;
}
function currentAnalyticsMap() { return analyticsMapForTab(STATE.activeView); }
function invalidateAnalyticsForTab(name) {
  delete STATE.analyticsByTab[name || AD_HOC_KEY];
  // Cascade to the disk cache so the analyst panel doesn't paint stale
  // numbers after a quotes refresh. Fire-and-forget — a network blip here
  // only means we keep showing the old cached payload, which is fine.
  if (name && name !== AD_HOC_KEY) {
    fetch(`/api/analytics-cache?view=${encodeURIComponent(name)}`, {method: "DELETE"}).catch(() => {});
  }
}

// Pull persisted analytics off disk and seed the in-memory map for this tab.
// Called once on tab activation (same place we load the weight presets), so
// reopening a saved portfolio paints the Rating Distribution from cache —
// no spinner, no yfinance round-trip.
async function loadAnalyticsCacheForView(name) {
  if (!name || name === AD_HOC_KEY) return;
  try {
    const r = await fetch(`/api/analytics-cache?view=${encodeURIComponent(name)}`);
    const d = await r.json();
    const cache = d && d.cache;
    if (!cache || typeof cache !== "object") return;
    const tabMap = analyticsMapForTab(name);
    for (const k of Object.keys(cache)) {
      const rec = cache[k];
      if (rec && rec.payload && !tabMap[k]) tabMap[k] = rec.payload;
    }
  } catch (_) { /* ignore — fall back to in-memory + fresh fetch */ }
}

// Slim a full analytics payload to the static fields worth persisting.
// `series` is large and only used by the live time-series chart, which is
// always re-fetched on demand — no point storing it on disk.
function _slimAnalyticsForCache(a) {
  if (!a || typeof a !== "object" || a.error) return null;
  const out = {};
  const keep = ["period", "display_ccy", "weights_applied", "active_symbols",
                "missing_symbols", "stats", "spy_stats", "nasdaq_stats",
                "weighted", "contribution", "analyst", "exposure",
                "concentration", "warnings"];
  for (const k of keep) if (k in a) out[k] = a[k];
  return out;
}

function persistAnalyticsToCache(viewName, key, payload) {
  if (!viewName || viewName === AD_HOC_KEY || !key) return;
  const slim = _slimAnalyticsForCache(payload);
  if (!slim) return;
  fetch("/api/analytics-cache", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({view: viewName, key, payload: slim}),
  }).catch(() => {});
}
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
  $("#status").innerHTML = lcHtml("resolving symbols", {bar: true, meta: `0·${entries.length}`});
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
          $("#status").innerHTML = lcHtml("streaming quotes", {bar: true, meta: `${done}·${total}`});
        } else if (msg.type === "done") {
          showProgress(100);
        }
      }
    }
    {
      const _nm = STATE.activeView || AD_HOC_KEY;
      $("#status").innerHTML = `<span class="status-name">${escapeHtml(viewLabel(_nm))}</span><span class="status-meta">updated ${escapeHtml(new Date().toLocaleTimeString())}</span>`;
    }
    // Persist this build as a view under the active tab name (or ad-hoc).
    const targetName = STATE.activeView || AD_HOC_KEY;
    await persistView(targetName, raw, DATA);
    STATE.customWeights = null;  // new build — drop stale custom weights
    if (STATE.mode === "custom") STATE.mode = "cap";
    // Refresh per-portfolio presets — a brand-new build may have just
    // promoted the ad-hoc tab into a real named view.
    await loadPresetsForView(targetName);
    invalidateAnalyticsForTab(targetName);  // rebuild invalidates this tab only
    renderModeBar();
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
  $("#status").innerHTML = lcHtml("indexing portfolios", {bar: true});
  await Promise.all([loadWatchlists(), loadViews(), loadColumnViews()]);
  $("#status").textContent = "Idle";
  wireColumnViewBar();
  renderTabs();
  // Restore the last open view if any.
  if (LAST_VIEW && VIEWS[LAST_VIEW]) {
    await activateTab(LAST_VIEW, {silent: true});
  } else {
    // No prior view — show the editor in ad-hoc mode but keep the panel closed.
    STATE.activeView = null;
    renderEditorMeta();
  }
  // Background warm-up — sequenced so we don't saturate yfinance with parallel
  // requests. Portfolio warm-up first (one bulk download per tab is heavier
  // and benefits more from being warm), then FX hover charts.
  setTimeout(() => {
    warmRecentTabs().then(() => {
      setTimeout(() => preloadFxIndexes(), 1500);
    });
  }, 1200);
}

async function preloadFxIndexes() {
  // Bias toward the most common reserve / display currencies so the first
  // hover lands on a warm cache. The remaining ones are still loaded
  // lazily on demand by fxFetchIndex().
  const priority = ["USD","EUR","GBP","JPY","CHF","CAD","AUD"].filter(c => FX_SUPPORTED.indexOf(c) >= 0);
  const rest = (FX_SUPPORTED || []).filter(c => priority.indexOf(c) < 0);
  for (const batch of [priority, rest]) {
    if (!batch.length) continue;
    try {
      const r = await fetch(`/api/fx-indexes-bulk?ccys=${encodeURIComponent(batch.join(","))}`);
      if (!r.ok) continue;
      const d = await r.json();
      const indexes = d.indexes || {};
      for (const c of Object.keys(indexes)) {
        if (Array.isArray(indexes[c]) && indexes[c].length) FX_INDEX_CACHE[c] = indexes[c];
      }
    } catch (e) { /* best-effort */ }
    // Brief gap before the second batch.
    await new Promise(r => setTimeout(r, 800));
  }
}

async function warmRecentTabs(maxN) {
  const limit = (typeof maxN === "number" && maxN > 0) ? maxN : 3;
  // Skip the currently active view (it's been rendered) and pick the most-recent others.
  const others = Object.entries(VIEWS)
    .filter(([n, v]) => n !== STATE.activeView && v && v.row_count > 0 && n !== AD_HOC_KEY)
    .sort((a, b) => String(b[1].saved_at || "").localeCompare(String(a[1].saved_at || "")))
    .slice(0, limit);
  // Sequential — back-to-back analytics POSTs would compete for the same
  // yfinance session and Yahoo would start returning empty close-price frames.
  for (const [name, _meta] of others) {
    try {
      const r = await fetch(`/api/views/${encodeURIComponent(name)}`);
      if (!r.ok) continue;
      const d = await r.json();
      const rows = (d.view && Array.isArray(d.view.rows)) ? d.view.rows : [];
      if (!rows.length) continue;
      const eq = equalWeightsOf(rows);
      const cap = capWeightsOf(rows);
      const wsets = {equal: eq, cap: cap};
      const res = await fetch("/api/portfolio-analytics-multi", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({rows, weight_sets: wsets, period: STATE.period, display_ccy: FX_QUOTE}),
      });
      if (!res.ok) continue;
      const dr = await res.json();
      const results = dr.results || {};
      const tabMap = analyticsMapForTab(name);
      for (const k of Object.keys(results)) {
        tabMap[analyticsCacheKey(k, STATE.period)] = results[k];
      }
      if (results.cap && !tabMap[analyticsCacheKey("custom", STATE.period)]) {
        tabMap[analyticsCacheKey("custom", STATE.period)] = results.cap;
      }
      // Brief pause so we don't saturate Yahoo's rate limiter.
      await new Promise(r => setTimeout(r, 500));
    } catch (e) { /* skip this tab silently */ }
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
  // Keep STATE.analyticsByTab[*] across switches — re-visiting paints from memory.
  if (STATE.mode === "custom") STATE.mode = "cap";
  // Switching tabs swaps the per-portfolio preset list. loadPresetsForView
  // may also restore the active preset (only if the current mode is the safe
  // default "cap"), so the user's last selection persists across sessions.
  await loadPresetsForView(name);
  // Seed the in-memory analytics map from disk so reopening a portfolio
  // hydrates the Rating Distribution panel without re-fetching from
  // yfinance. requestAnalytics() will still kick off in the cached-rows
  // branch below, but its cache-hit path will take the disk seed.
  await loadAnalyticsCacheForView(name);
  renderModeBar();
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
        // DATA just landed — refresh the mode bar so the [+] pill appears
        // and the active preset paints with the correct active state.
        renderModeBar();
        const savedAt = view.saved_at ? relTime(view.saved_at) : "previously";
        $("#status").innerHTML = `<span class="status-name">${escapeHtml(viewLabel(name))}</span><span class="status-meta">cached ${escapeHtml(savedAt)}</span>${view.stale ? `<span class="status-stale">stale</span>` : ""}`;
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
  $("#status").innerHTML = `<span class="status-name">${escapeHtml(viewLabel(name))}</span>`;
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
    tab.innerHTML = `${staleDot}<span class="pf-tab-label" title="Double-click to rename">${escapeHtml(viewLabel(name))}</span>${closeBtn}`;
    tab.addEventListener("click", (e) => {
      if (e.target.closest(".pf-tab-close")) return;
      if (e.target.closest(".pf-tab-rename-input")) return;
      activateTab(name);
    });
    tab.querySelector(".pf-tab-close").addEventListener("click", async (e) => {
      e.stopPropagation();
      await deletePortfolio(name);
    });
    // Double-click the label → inline rename (saved portfolios only, not ad-hoc).
    if (name !== AD_HOC_KEY) {
      tab.querySelector(".pf-tab-label").addEventListener("dblclick", (e) => {
        e.stopPropagation();
        e.preventDefault();
        beginTabRename(tab, name);
      });
    }
    wrap.appendChild(tab);
  }

  // "+ New" tab — always present.
  const add = document.createElement("button");
  add.className = "pf-tab-add";
  add.textContent = "＋ New portfolio";
  add.addEventListener("click", () => createNewTab());
  wrap.appendChild(add);
}

function beginTabRename(tab, oldName) {
  const labelEl = tab.querySelector(".pf-tab-label");
  if (!labelEl || tab.querySelector(".pf-tab-rename-input")) return;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "pf-tab-rename-input";
  input.value = oldName;
  input.spellcheck = false;
  input.size = Math.max(8, oldName.length + 2);
  labelEl.style.display = "none";
  labelEl.insertAdjacentElement("afterend", input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return; done = true;
    const next = input.value.trim();
    input.remove();
    labelEl.style.display = "";
    if (!commit || !next || next === oldName) return;
    await renamePortfolio(oldName, next);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("dblclick", (e) => e.stopPropagation());
}

async function renamePortfolio(oldName, newName) {
  try {
    const r = await fetch("/api/portfolio/rename", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({old: oldName, new: newName}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Rename failed.");
    // Update local maps so the UI reflects the new key without a full reload.
    WATCHLISTS = d.watchlists || WATCHLISTS;
    if (VIEWS[oldName]) { VIEWS[newName] = VIEWS[oldName]; delete VIEWS[oldName]; }
    if (STATE.analyticsByTab && STATE.analyticsByTab[oldName]) {
      STATE.analyticsByTab[newName] = STATE.analyticsByTab[oldName];
      delete STATE.analyticsByTab[oldName];
    }
    if (STATE.activeView === oldName) STATE.activeView = newName;
    renderTabs(); renderEditorMeta();
    toast(`Renamed to "${newName}".`);
  } catch (err) {
    toast(err.message || "Rename failed.");
  }
}

function renderEditorMeta() {
  // Meta line was removed — name lives in the topbar status now.
  if (typeof updatePrimaryButtonLabels === "function") updatePrimaryButtonLabels();
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

/**
 * Export every saved portfolio to an .xlsx (one sheet per portfolio).
 * Server-side build — fetches analyst recs in parallel + computes the
 * analytics block from cached rows, so the download is "everything you
 * can see in the app." See xlsx_export.py and the inline comment by the
 * topbar Export button for the maintenance contract.
 *
 * UX: this can take a few seconds (the analyst-rec fetch is the slow
 * piece). We swap the button label for a loading chip while the request
 * is in flight, then restore it.
 */
async function exportXlsx() {
  const btn = $("#export");
  if (!btn) return;
  const origHtml = btn.innerHTML;
  const origDisabled = btn.disabled;
  btn.disabled = true;
  btn.innerHTML = lcHtml("exporting", {bar: true});
  try {
    const r = await fetch("/api/export-xlsx");
    if (!r.ok) {
      let msg = "Export failed (HTTP " + r.status + ").";
      try { const j = await r.json(); if (j && j.error) msg = j.error; } catch {}
      throw new Error(msg);
    }
    const blob = await r.blob();
    // Pull the filename out of Content-Disposition (server picks the timestamp).
    let fname = "portfolio_tracker_export.xlsx";
    const cd = r.headers.get("content-disposition") || "";
    const m = cd.match(/filename="?([^";]+)"?/i);
    if (m) fname = m[1];
    const a = document.createElement("a");
    const url = URL.createObjectURL(blob);
    a.href = url;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast(`Exported ${fname}`);
  } catch (err) {
    toast(err.message || "Export failed.");
  } finally {
    btn.innerHTML = origHtml;
    btn.disabled = origDisabled;
  }
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

function capWeightsFromData() { return capWeightsOf(DATA.filter(r => r && r.symbol)); }
function equalWeightsFromData() { return equalWeightsOf(DATA.filter(r => r && r.symbol)); }
function analyticsCacheKey(mode, period) { return mode + "|" + period + "|" + FX_QUOTE; }

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
  const tabMap = currentAnalyticsMap();

  // Cache hit → instant.
  const cached = tabMap[key];
  if (cached && !opts.force) {
    STATE.analytics = cached;
    STATE.analyticsLoading = false;
    renderAnalyticsBody();
    return;
  }

  const reqId = ++_analyticsReqId;
  STATE.analyticsLoading = true;
  renderAnalyticsBody();

  // Build the weight_sets we'll request. Always include the *active* mode;
  // also opportunistically include any other modes we don't already have
  // cached for this period (the backend shares the slow yfinance + analyst
  // fetches across every weight set in a single call). The set name we send
  // matches the mode id so analyticsCacheKey lines up server↔client.
  const wsets = {};
  function maybeRequest(modeKey) {
    if (!tabMap[analyticsCacheKey(modeKey, period)]) {
      wsets[modeKey] = weightsForMode(modeKey);
    }
  }
  wsets[mode] = weightsForMode(mode);
  maybeRequest("equal");
  maybeRequest("cap");

  try {
    const r = await fetch("/api/portfolio-analytics-multi", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({rows: DATA, weight_sets: wsets, period, display_ccy: FX_QUOTE}),
    });
    const d = await r.json();
    if (reqId !== _analyticsReqId) return;
    if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
    if (d.error) throw new Error(d.error);
    const results = d.results || {};
    for (const k of Object.keys(results)) {
      const cacheK = analyticsCacheKey(k, period);
      tabMap[cacheK] = results[k];
      // Mirror to disk so the next reload hydrates the panel instantly.
      persistAnalyticsToCache(STATE.activeView, cacheK, results[k]);
    }
    STATE.analytics = tabMap[key] || results[mode] || null;
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
    renderAnalystDashboard({});
    return;
  }
  if (STATE.analyticsLoading && !STATE.analytics) {
    body.innerHTML = `<div class="pf-empty lc-block">${lcHtml("computing analytics", {bar: true})}</div>`;
    renderAnalystDashboard(quickAnalystPreview() || {});
    return;
  }
  const a = STATE.analytics;
  if (!a) { body.innerHTML = `<div class="pf-empty">No analytics yet.</div>`; renderAnalystDashboard({}); return; }
  if (a.error) { body.innerHTML = `<div class="pf-empty" style="color:var(--neg)">Analytics error: ${escapeHtml(a.error)}</div>`; renderAnalystDashboard({}); return; }

  // Build cards.
  const banner = staleBannerHtml();
  body.innerHTML = `
    ${banner}
    <div class="pf-grid">
      <div class="pf-card">
        <h4>Portfolio chart <span class="sub">${escapeHtml(STATE.period)} · ${labelForMode(STATE.mode)} · ${escapeHtml(a.display_ccy || FX_QUOTE)}${STATE.analyticsLoading ? '<span class="pf-loading"> refreshing…</span>' : ''}</span></h4>
        <div class="pf-chart-wrap" id="pf-chart-host"></div>
        <div class="pf-chart-legend" id="pf-chart-legend"></div>
      </div>
      <div class="pf-card">
        <h4>Risk &amp; return <span class="sub">vs SPY · ${escapeHtml(a.display_ccy || FX_QUOTE)}</span></h4>
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
        <h4>Contribution to ${escapeHtml(STATE.period)} return <span class="sub">${escapeHtml(a.display_ccy || FX_QUOTE)}</span></h4>
        ${renderContribHtml(a)}
      </div>
    </div>
  `;
  drawPortfolioChart(a, $("#pf-chart-host"), $("#pf-chart-legend"));
  renderStatTipsKatex();
  renderAnalystDashboard(a);
}

function staleBannerHtml() {
  const name = STATE.activeView;
  if (!name || !VIEWS[name] || !VIEWS[name].stale) return "";
  return `<div class="pf-stale-banner">Constituents changed since last build — analytics may be stale.
    <button onclick="build({keepPanelOpen:true})">Refresh now</button></div>`;
}

function labelForMode(m) {
  if (m === "equal") return "Equal-weight";
  if (m === "custom") return "Custom";
  if (m === "cap") return "Cap-weighted";
  if (typeof m === "string" && m.startsWith("preset:")) return m.slice(7);
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

const METRIC_INFO = {
  "Period return": {
    formula: String.raw`R = \dfrac{V_T}{V_0} - 1`,
    desc: "Total return of the portfolio over the chosen period, FX-adjusted to the display currency.",
    range: "Compare to SPY in the same period. A positive read with low volatility is the cleanest win."
  },
  "Ann. return": {
    formula: String.raw`R_{ann} = \left(\dfrac{V_T}{V_0}\right)^{\frac{1}{y}} - 1`,
    desc: "Compound annual growth rate. Normalises returns across periods of different length so 3M, 1Y and 5Y are comparable.",
    range: "Above 10% sustained is strong; above the risk-free rate (~5%) is the bar for active equity."
  },
  "Ann. vol": {
    formula: String.raw`\sigma_{ann} = \sigma_{daily} \cdot \sqrt{252}`,
    desc: "Annualised standard deviation of daily returns — the headline risk measure.",
    range: "Equities cluster 15–25%. Below 12% is unusually smooth, above 35% is a high-octane book."
  },
  "Sharpe": {
    formula: String.raw`S = \dfrac{\overline{R} - R_f}{\sigma}`,
    desc: "Excess return per unit of total volatility. Risk-free rate treated as 0 here, so it’s a pure return/vol ratio.",
    range: "0.5 mediocre · 1.0 good · 2.0 excellent · >3.0 suspect (curve-fit or short sample)."
  },
  "Sortino": {
    formula: String.raw`S_o = \dfrac{\overline{R} - R_f}{\sigma_{down}}`,
    desc: "Like Sharpe but penalises only downside volatility — closer to how an investor actually feels risk.",
    range: "Usually higher than Sharpe. >1.0 is good, >2.0 is excellent. The Sortino/Sharpe gap reveals upside-skew."
  },
  "Max drawdown": {
    formula: String.raw`\text{DD}_{max} = \min_{t}\!\left(\dfrac{V_t}{\max_{s\le t} V_s} - 1\right)`,
    desc: "Worst peak-to-trough loss over the period. Drives the emotional pain of holding the strategy.",
    range: "Equity portfolios routinely see −20 to −35% in bear markets. Above −50% suggests concentrated risk."
  },
  "Calmar": {
    formula: String.raw`C = \dfrac{R_{ann}}{|\text{DD}_{max}|}`,
    desc: "Annualised return divided by max drawdown — return per unit of worst-case pain.",
    range: ">0.5 acceptable · >1.0 good · >2.0 exceptional. Penalises managers who run wild during crashes."
  },
  "Beta (SPY)": {
    formula: String.raw`\beta = \dfrac{\mathrm{Cov}(r_p, r_m)}{\mathrm{Var}(r_m)}`,
    desc: "Sensitivity to SPY moves. β=1 means it moves with the market; β=1.3 means 30% more responsive.",
    range: "0.6–0.8 defensive · 0.9–1.1 market-like · >1.3 high-beta growth. Negative is rare and means inverse exposure."
  },
  "R² (SPY)": {
    formula: String.raw`R^2 = \mathrm{Corr}(r_p, r_m)^2`,
    desc: "Share of portfolio variance explained by SPY. Tells you whether beta is a meaningful description of behaviour.",
    range: ">0.85 → portfolio is essentially SPY+leverage. <0.4 → diversification/idiosyncratic exposure. <0.1 → unrelated."
  },
  "Tracking err": {
    formula: String.raw`\mathrm{TE} = \sqrt{252}\cdot\sigma\!\left(r_p - r_m\right)`,
    desc: "Annualised standard deviation of the portfolio’s return *minus* SPY’s — how far you wander from the benchmark.",
    range: "Index funds <2%. Active managers 4–8% typical. Concentrated stock picks 10–20%+. Pair with information ratio."
  },
  /* --- Valuation & analyst (weighted) --- */
  "P/E (wtd)": {
    formula: String.raw`PE_{port} = \dfrac{\sum_i w_i \cdot PE_i}{\sum_i w_i \;:\; PE_i \text{ defined}}`,
    desc: "Portfolio-weighted trailing price-to-earnings. Names without an earnings figure (loss-makers, missing data) drop out of both numerator and denominator.",
    range: "S&P 500 average sits around 20–25. Above 30 is growth-tilt; below 15 is value-tilt. Heavily skewed by megacaps when cap-weighted."
  },
  "P/S (wtd)": {
    formula: String.raw`PS_{port} = \dfrac{\sum_i w_i \cdot PS_i}{\sum_i w_i \;:\; PS_i \text{ defined}}`,
    desc: "Portfolio-weighted price-to-sales. Useful when earnings are noisy or negative — sales are more stable across the cycle.",
    range: "Broad market ~2–3×. Tech / high-margin software often 8–15×. Above 20× is rare outside hyper-growth."
  },
  "EV/EBITDA (wtd)": {
    formula: String.raw`\dfrac{\sum_i w_i \cdot (EV/EBITDA)_i}{\sum_i w_i \;:\; EV/EBITDA_i \text{ defined}}`,
    desc: "Portfolio-weighted average EV/EBITDA across covered names. This is a weighted average of constituent multiples, not a reconstructed aggregate enterprise-value-to-aggregate-EBITDA ratio.",
    range: "Mature businesses 8–14×. Quality compounders 15–25×. Above 25× requires sustained growth to justify."
  },
  "Div yield (wtd)": {
    formula: String.raw`y_{port} = \dfrac{\sum_i w_i \cdot y_i}{\sum_i w_i \;:\; y_i \text{ defined}}`,
    desc: "Forward indicated dividend yield across covered names, weighted by portfolio share. Names without a usable dividend figure drop out of both numerator and denominator. Tax-unadjusted and cash-dividend-only.",
    range: "S&P 500 ~1.3–1.8%. Income-tilted books 3–5%. Above 6% often signals stress or capital return at the expense of growth."
  },
  "Market cap (wtd avg)": {
    formula: String.raw`MC_{port} = \sum_i w_i \cdot MC_i`,
    desc: "Portfolio-weighted average market capitalisation. Useful as a quick read on how mega-cap-heavy a book really is.",
    range: "Equal-weighted S&P sits in the low tens of billions; cap-weighted is dragged into the hundreds of billions by the top 7 names."
  },
  "Analyst rating (1=SB, 5=SS)": {
    formula: String.raw`R_{port} = \dfrac{\sum_i w_i \cdot R_i}{\sum_i w_i \;:\; R_i \text{ defined}}`,
    desc: "Mean sell-side analyst rating across covered names. Yahoo's 1–5 scale: 1 Strong Buy → 5 Strong Sell.",
    range: "Most large caps cluster 1.8–2.4 (Buy). Below 1.5 is unusually bullish; above 3.0 leans bearish."
  },
  "Weighted target upside": {
    formula: String.raw`U_{port} = \dfrac{\sum_i w_i \cdot \left(\dfrac{TP_i}{P_i} - 1\right)}{\sum_i w_i \;:\; TP_i, P_i \text{ defined}}`,
    desc: "Coverage-weighted average of analysts' 12-month price-target upside versus current price. Computed in each holding's local currency before weighting; uncovered names drop out of the denominator.",
    range: "Single-digit positive is typical. >20% upside often reflects beaten-down names or aggressive growth assumptions."
  },
  "Analysts covering (sum)": {
    formula: String.raw`N = \sum_i n_i`,
    desc: "Total count of unique analyst opinions across all covered holdings. A coverage-density gauge — high numbers mean the consensus is well-sampled.",
    range: "Megacap names alone often have 30–50 analysts. A diverse 20-name book commonly clears 300+."
  },
  /* --- Concentration --- */
  "Top-5 weight": {
    formula: String.raw`T_5 = \sum_{i \in \text{top 5}} w_i`,
    desc: "Combined weight of the five largest holdings. Direct gauge of concentration risk — how much of the portfolio rides on a handful of names.",
    range: "Diversified funds 15–25%. Active concentrated books 40–60%. Above 70% means a few names dominate the P&L."
  },
  "Herfindahl (HHI)": {
    formula: String.raw`H = \sum_i w_i^2`,
    desc: "Sum of squared weights. The textbook measure of concentration — small when weight is spread out, approaches 1 when one name dominates.",
    range: "An equal-weighted N-stock book has HHI = 1/N. <0.10 well-spread · 0.10–0.20 moderate · >0.25 concentrated."
  },
  "Effective # of names": {
    formula: String.raw`N_{eff} = \dfrac{1}{H} = \dfrac{1}{\sum_i w_i^2}`,
    desc: "Reciprocal of HHI. Reads as 'this portfolio behaves like N equally-weighted names.' Falls below the raw count whenever weights are uneven.",
    range: "Equal-weight: N_eff equals the holding count. Cap-weighted megacap books often have N_eff of 3–6 even with 20+ holdings."
  },
  "Active holdings": {
    formula: String.raw`|\{i : w_i > 0\}|`,
    desc: "Number of positions with non-zero weight that have usable price history (i.e. survived the analytics download).",
    range: "Anything below your input count means some symbols were dropped — see 'Dropped (no history)' for the list."
  },
  "Dropped (no history)": {
    formula: String.raw`\text{symbols}\notin\text{price history}`,
    desc: "Constituents that returned no usable price series for the chosen period (rate-limited fetch, new listings, delisted, or bad ticker).",
    range: "Empty is ideal. If recurring, hit Build again — yfinance's rate-limiter sometimes drops a couple of symbols on the first pass."
  },
};

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
    ["Beta (SPY)", fmtNumOr(s.beta_spy, 2), "", fmtNumOr(sp.beta_spy, 2)],
    ["R² (SPY)", fmtNumOr(s.r2_spy, 2), "", fmtNumOr(sp.r2_spy, 2)],
    ["Tracking err", fmtPctPlain(s.te_spy), "", fmtPctPlain(sp.te_spy)],
  ];
  const html = rows.map(r => {
    const info = METRIC_INFO[r[0]];
    const dataAttr = info ? ` data-info="1" data-metric="${escapeHtml(r[0])}"` : "";
    const tip = metricTipHtml(r[0], info);
    return `
    <div class="pf-stat-row"${dataAttr} title="${info ? '' : 'Portfolio vs SPY'}">
      <span class="l">${r[0]}</span>
      <span class="v ${r[2]}">${r[1]} <span style="color:var(--muted); font-weight:400">/ ${r[3]}</span></span>
      ${tip}
    </div>`;
  }).join("");
  return `<div class="pf-stats">${html}</div>`;
}

/* ---------------------------- Analyst sentiment dashboard ---------------------------- */
const _RATING_BUCKETS = [
  { max: 1.5, key: "strong-buy",  label: "Strong Buy" },
  { max: 2.5, key: "buy",         label: "Buy" },
  { max: 3.5, key: "hold",        label: "Hold" },
  { max: 4.5, key: "sell",        label: "Underperform" },
  { max: Infinity, key: "strong-sell", label: "Sell" },
];
function ratingBucket(mr) {
  if (mr == null || !isFinite(mr)) return null;
  for (const b of _RATING_BUCKETS) if (mr <= b.max) return b;
  return _RATING_BUCKETS[_RATING_BUCKETS.length - 1];
}
function recKeyToClass(k) {
  if (!k) return "none";
  k = String(k).toLowerCase().replace(/_/g, "-");
  if (k === "strongbuy") return "strong-buy";
  if (k === "strongsell" || k === "underperform") return "strong-sell";
  return k;
}
function recKeyLabel(k) {
  if (!k) return "—";
  return String(k).replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}
function quickAnalystPreview() {
  const rows = (DATA || []).filter(r => r && r.symbol);
  if (!rows.length) return null;
  const weights = weightsForMode(STATE.mode);
  let ratingNum = 0, ratingW = 0, upsideNum = 0, upsideW = 0;
  const holdings = [];
  const notCovered = [];
  for (const row of rows) {
    const symbol = row.symbol;
    const weight = Number(weights[symbol] || 0);
    const meanRating = row.recommendation_mean;
    const price = row.price;
    const targetMean = row.target_mean_price;
    const upside = (targetMean != null && price != null && isFinite(targetMean) && isFinite(price) && price > 0)
      ? ((targetMean / price - 1) * 100)
      : null;
    if (meanRating != null && isFinite(meanRating)) {
      ratingNum += meanRating * weight;
      ratingW += weight;
    }
    if (upside != null && isFinite(upside)) {
      upsideNum += upside * weight;
      upsideW += weight;
    }
    const hasCoverage = (meanRating != null && isFinite(meanRating)) || (upside != null && isFinite(upside));
    const payload = {
      symbol,
      name: row.name || symbol,
      currency: row.currency || FX_QUOTE,
      weight,
      price,
      target_mean: targetMean,
      target_median: null,
      target_low: null,
      target_high: null,
      upside_pct: upside,
      mean_rating: meanRating,
      rec_key: null,
      n_analysts: null,
      dist: null,
    };
    if (hasCoverage) holdings.push(payload);
    else notCovered.push({symbol, name: row.name || symbol, weight});
  }
  holdings.sort((a, b) => (b.weight || 0) - (a.weight || 0) || String(a.symbol).localeCompare(String(b.symbol)));
  return {
    display_ccy: FX_QUOTE,
    analyst: {
      mean_rating: ratingW > 0 ? (ratingNum / ratingW) : null,
      rating_coverage_weight: ratingW,
      weighted_target_upside_pct: upsideW > 0 ? (upsideNum / upsideW) : null,
      target_coverage_weight: upsideW,
      n_analysts_total: null,
      distribution_pct: null,
      holdings,
      not_covered: notCovered,
      covered_count: holdings.length,
      active_count: rows.length,
    },
  };
}
let _AN_SORT = { key: "weight", dir: -1 };
function renderAnalystDashboard(a) {
  const host = document.getElementById("analyst-dashboard");
  if (!host) return;
  const an = (a && a.analyst) || {};
  const holdings = Array.isArray(an.holdings) ? an.holdings.slice() : [];
  const notCovered = Array.isArray(an.not_covered) ? an.not_covered : [];
  const active = an.active_count || 0;
  const covered = an.covered_count || 0;
  const coverageWeight = an.target_coverage_weight || 0;
  const dist = an.distribution_pct;  // {strongBuy, buy, hold, sell, strongSell} as %
  const wRating = an.mean_rating;
  const wUpside = an.weighted_target_upside_pct;
  const nAnalysts = an.n_analysts_total || 0;

  if (STATE.analyticsLoading && !holdings.length && !notCovered.length) {
    host.innerHTML = `
      <div class="an-head">
        <span class="an-title">Analyst sentiment</span>
        <span class="an-sub">Portfolio-weighted analyst consensus from yfinance.</span>
      </div>
      <div class="an-grid"><div class="an-card"><div class="lc-block">${lcHtml("loading analyst sentiment", {bar: true})}</div></div></div>
      <div class="an-coverage-foot"><span class="credit">📈 Local Portfolio Dashboard · yfinance · no API key</span><span>${new Date().toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })}</span></div>
    `;
    return;
  }

  // Empty-state placeholder
  if (!holdings.length && !notCovered.length) {
    host.innerHTML = `
      <div class="an-head">
        <span class="an-title">Analyst sentiment</span>
        <span class="an-sub">Build a portfolio to see weighted analyst consensus, rating distribution and price-target upside.</span>
      </div>
      <div class="an-grid"><div class="an-card"><div style="color:var(--muted);font-size:12px;padding:6px 0">No analyst data yet.</div></div></div>
      <div class="an-coverage-foot"><span class="credit">📈 Local Portfolio Dashboard · yfinance · no API key</span><span>${new Date().toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })}</span></div>
    `;
    return;
  }

  const bucket = ratingBucket(wRating);
  const needlePct = wRating != null ? Math.max(0, Math.min(100, ((wRating - 1) / 4) * 100)) : 50;

  const card1 = `
    <div class="an-card">
      <h4>Weighted rating <span class="an-sub">${escapeHtml(labelForMode(STATE.mode))}</span></h4>
      <div>
        <span class="an-rating-num">${wRating != null ? wRating.toFixed(2) : "—"}</span>
        ${bucket ? `<span class="an-rating-key ${bucket.key}">${bucket.label}</span>` : ""}
        <span style="color:var(--muted);font-size:11px;margin-left:6px;">/ 5</span>
      </div>
      <div class="an-gauge"><div class="needle" style="left:${needlePct.toFixed(1)}%"></div></div>
      <div class="an-gauge-labels"><span>Strong Buy</span><span>Buy</span><span>Hold</span><span>Underperform</span><span>Sell</span></div>
      <div class="an-rating-meta">
        <span>Coverage <b>${covered}/${active}</b> positions${an.rating_coverage_weight != null ? ` · <b>${(an.rating_coverage_weight*100).toFixed(0)}%</b> of weight` : ""}</span>
        <span>Total analyst opinions: <b>${nAnalysts}</b></span>
      </div>
    </div>`;

  const card2 = (() => {
    if (!dist) {
      return `<div class="an-card"><h4>Rating distribution</h4>
        <div style="color:var(--muted);font-size:12px;padding:6px 0">No distribution data — coverage names lack a recent recommendations row.</div></div>`;
    }
    const total = (dist.strongBuy || 0) + (dist.buy || 0) + (dist.hold || 0) + (dist.sell || 0) + (dist.strongSell || 0);
    const norm = total > 0 ? total : 1;
    const seg = (v) => Math.max(0, (v || 0) / norm * 100);
    const lbl = (k) => seg(dist[k]) >= 6 ? Math.round(seg(dist[k])) + "%" : "";
    return `
    <div class="an-card">
      <h4>Rating distribution <span class="an-sub">weighted</span></h4>
      <div class="an-dist-bar">
        <div class="sb" style="flex:${seg(dist.strongBuy).toFixed(2)}" title="Strong Buy ${seg(dist.strongBuy).toFixed(1)}%">${lbl("strongBuy")}</div>
        <div class="b"  style="flex:${seg(dist.buy).toFixed(2)}"        title="Buy ${seg(dist.buy).toFixed(1)}%">${lbl("buy")}</div>
        <div class="h"  style="flex:${seg(dist.hold).toFixed(2)}"       title="Hold ${seg(dist.hold).toFixed(1)}%">${lbl("hold")}</div>
        <div class="s"  style="flex:${seg(dist.sell).toFixed(2)}"       title="Sell ${seg(dist.sell).toFixed(1)}%">${lbl("sell")}</div>
        <div class="ss" style="flex:${seg(dist.strongSell).toFixed(2)}" title="Strong Sell ${seg(dist.strongSell).toFixed(1)}%">${lbl("strongSell")}</div>
      </div>
      <div class="an-dist-legend">
        <span><i style="background:#15803d"></i>Strong Buy ${seg(dist.strongBuy).toFixed(1)}%</span>
        <span><i style="background:#22c55e"></i>Buy ${seg(dist.buy).toFixed(1)}%</span>
        <span><i style="background:#eab308"></i>Hold ${seg(dist.hold).toFixed(1)}%</span>
        <span><i style="background:#f97316"></i>Sell ${seg(dist.sell).toFixed(1)}%</span>
        <span><i style="background:#dc2626"></i>Strong Sell ${seg(dist.strongSell).toFixed(1)}%</span>
      </div>
    </div>`;
  })();

  const card3 = (() => {
    const upCls = wUpside == null ? "" : (wUpside >= 0 ? "pos" : "neg");
    const upSign = wUpside == null ? "" : (wUpside >= 0 ? "+" : "");
    const covPct = an.target_coverage_weight != null ? (an.target_coverage_weight * 100) : 0;
    return `
    <div class="an-card">
      <h4>Consensus price target upside <span class="an-sub">vs current</span></h4>
      <div>
        <span class="an-upside-num ${upCls}">${wUpside == null ? "—" : (upSign + wUpside.toFixed(2) + "%")}</span>
        <span style="color:var(--muted);font-size:11px;margin-left:8px;">12-month consensus</span>
      </div>
      <div class="an-upside-meta">
        <span>Computed in each position's local currency, then weighted by portfolio share.</span>
        <span>Coverage: <b>${covPct.toFixed(0)}%</b> of portfolio weight has price-target data.</span>
      </div>
      <div class="an-coverage-bar" title="Share of portfolio weight with analyst price targets"><div class="fill" style="width:${covPct.toFixed(1)}%"></div></div>
    </div>`;
  })();

  // Per-holding table
  const sortKey = _AN_SORT.key, sortDir = _AN_SORT.dir;
  holdings.sort((x, y) => {
    let av = x[sortKey], bv = y[sortKey];
    if (sortKey === "rec_key") { av = x.mean_rating; bv = y.mean_rating; }
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "number") return (av - bv) * sortDir;
    return String(av).localeCompare(String(bv)) * sortDir;
  });
  const arrow = (k) => sortKey === k ? `<span class="arrow">${sortDir > 0 ? "▲" : "▼"}</span>` : "";
  const fmtUp = (v) => v == null ? "—" : ((v >= 0 ? "+" : "") + v.toFixed(2) + "%");

  const rowsHtml = holdings.map(h => {
    const upCls = h.upside_pct == null ? "" : (h.upside_pct >= 0 ? "pos" : "neg");
    const cls = recKeyToClass(h.rec_key || (ratingBucket(h.mean_rating) || {}).key);
    const lbl = h.rec_key ? recKeyLabel(h.rec_key) : ((ratingBucket(h.mean_rating) || {}).label || "—");
    let mini = "";
    if (h.dist) {
      const tot = (h.dist.strongBuy||0)+(h.dist.buy||0)+(h.dist.hold||0)+(h.dist.sell||0)+(h.dist.strongSell||0);
      const s = (v) => tot > 0 ? (v || 0) / tot * 100 : 0;
      mini = `<span class="an-mini-dist" title="SB ${h.dist.strongBuy||0} · B ${h.dist.buy||0} · H ${h.dist.hold||0} · S ${h.dist.sell||0} · SS ${h.dist.strongSell||0}">
        <div class="sb" style="width:${s(h.dist.strongBuy).toFixed(2)}%"></div>
        <div class="b"  style="width:${s(h.dist.buy).toFixed(2)}%"></div>
        <div class="h"  style="width:${s(h.dist.hold).toFixed(2)}%"></div>
        <div class="s"  style="width:${s(h.dist.sell).toFixed(2)}%"></div>
        <div class="ss" style="width:${s(h.dist.strongSell).toFixed(2)}%"></div>
      </span>`;
    } else {
      mini = `<span style="color:var(--muted);font-size:10.5px">—</span>`;
    }
    return `<tr>
      <td class="sym">${escapeHtml(h.symbol)}<span class="muted">${escapeHtml((h.name || "").length > 22 ? h.name.slice(0, 22) + "…" : (h.name || ""))}</span></td>
      <td>${(h.weight*100).toFixed(2)}%</td>
      <td><span class="rk ${cls}">${escapeHtml(lbl)}</span></td>
      <td>${h.mean_rating != null ? h.mean_rating.toFixed(2) : "—"}</td>
      <td>${h.n_analysts != null ? h.n_analysts : "—"}</td>
      <td>${h.price != null ? fmtMoney(h.price, h.currency) : "—"}</td>
      <td>${h.target_mean != null ? fmtMoney(h.target_mean, h.currency) : "—"}</td>
      <td class="${upCls}">${fmtUp(h.upside_pct)}</td>
      <td>${mini}</td>
    </tr>`;
  }).join("");

  const tableHtml = holdings.length ? `
    <div class="an-card" style="grid-column: 1 / -1;">
      <h4>Per-holding consensus <span class="an-sub">click a column to sort</span></h4>
      <div class="an-table-wrap">
        <table class="an-table" id="an-table">
          <thead><tr>
            <th data-k="symbol">Symbol${arrow("symbol")}</th>
            <th data-k="weight">Weight${arrow("weight")}</th>
            <th data-k="rec_key">Consensus${arrow("rec_key")}</th>
            <th data-k="mean_rating">Rating${arrow("mean_rating")}</th>
            <th data-k="n_analysts"># Analysts${arrow("n_analysts")}</th>
            <th data-k="price">Price${arrow("price")}</th>
            <th data-k="target_mean">Target (mean)${arrow("target_mean")}</th>
            <th data-k="upside_pct">Upside${arrow("upside_pct")}</th>
            <th>Distribution</th>
          </tr></thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
    </div>` : "";

  const notCoveredHtml = notCovered.length ? (() => {
    const totalW = notCovered.reduce((a, n) => a + (n.weight || 0), 0);
    const names = notCovered.slice(0, 6).map(n => `${escapeHtml(n.symbol)}${n.weight ? ` (${(n.weight*100).toFixed(1)}%)` : ""}`).join(", ");
    return `<span class="an-not-covered">${notCovered.length} position${notCovered.length>1?"s":""} without analyst coverage — <b>${(totalW*100).toFixed(1)}%</b> of weight: ${names}${notCovered.length > 6 ? "…" : ""}</span>`;
  })() : "";

  host.innerHTML = `
    <div class="an-head">
      <span class="an-title">Analyst sentiment</span>
      <span class="an-sub">Portfolio-weighted analyst consensus from yfinance · positions in ${escapeHtml(a.display_ccy || FX_QUOTE)}.${STATE.analyticsLoading ? ` ${lcHtml("refreshing", {bar: true})}` : ""}</span>
      <span class="an-mode-note">Aggregated by
        <button type="button" class="an-mode-btn" id="an-mode-btn" aria-haspopup="listbox" aria-expanded="false">
          <span id="an-mode-label">${escapeHtml(labelForMode(STATE.mode))}</span>
          <span class="an-caret">▾</span>
        </button>
        <div class="an-mode-menu" id="an-mode-menu" role="listbox" aria-label="Aggregation method">
          <div class="an-mode-opt" data-mode="equal"  role="option" data-selected="${STATE.mode === 'equal' ? '1' : '0'}">Equal-weight</div>
          <div class="an-mode-opt" data-mode="cap"    role="option" data-selected="${STATE.mode === 'cap' ? '1' : '0'}">Cap-weighted</div>
          ${(STATE.weightPresets || []).map(p => `<div class="an-mode-opt" data-mode="${escapeHtml(modeId(p.name))}" role="option" data-selected="${STATE.mode === modeId(p.name) ? '1' : '0'}">${escapeHtml(p.name)}</div>`).join("")}
          ${STATE.mode === 'custom' ? `<div class="an-mode-opt" data-mode="custom" role="option" data-selected="1">Custom (unsaved)</div>` : ""}
        </div>
      </span>
    </div>
    <div class="an-grid">${card1}${card2}${card3}${tableHtml}</div>
    <div class="an-coverage-foot">
      <span class="credit">📈 Local Portfolio Dashboard · yfinance · no API key</span>
      ${notCoveredHtml}
      <span>${new Date().toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })}</span>
    </div>
  `;

  // Wire up the aggregation-mode dropdown in the header.
  const btn = document.getElementById("an-mode-btn");
  const menu = document.getElementById("an-mode-menu");
  if (btn && menu) {
    const closeMenu = () => {
      menu.classList.remove("show");
      btn.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
      document.removeEventListener("mousedown", onOutside, true);
      document.removeEventListener("keydown", onEsc, true);
    };
    function onOutside(e) {
      if (e.target.closest("#an-mode-btn") || e.target.closest("#an-mode-menu")) return;
      closeMenu();
    }
    function onEsc(e) { if (e.key === "Escape") closeMenu(); }
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = menu.classList.contains("show");
      if (isOpen) { closeMenu(); return; }
      menu.classList.add("show");
      btn.classList.add("open");
      btn.setAttribute("aria-expanded", "true");
      document.addEventListener("mousedown", onOutside, true);
      document.addEventListener("keydown", onEsc, true);
    });
    menu.addEventListener("click", (e) => {
      const opt = e.target.closest(".an-mode-opt");
      if (!opt) return;
      const mode = opt.dataset.mode;
      closeMenu();
      if (!mode) return;
      if (mode === "custom" && STATE.mode !== "custom") { openWeightsPopup({intent: "adhoc"}); return; }
      selectMode(mode);
    });
  }

  // Wire up column sorting on the per-holding table.
  const tbl = document.getElementById("an-table");
  if (tbl) {
    tbl.querySelectorAll("th[data-k]").forEach(th => {
      th.addEventListener("click", () => {
        const k = th.dataset.k;
        if (_AN_SORT.key === k) _AN_SORT.dir = -_AN_SORT.dir;
        else { _AN_SORT.key = k; _AN_SORT.dir = (k === "symbol" ? 1 : -1); }
        renderAnalystDashboard(STATE.analytics);
      });
    });
  }
}

function renderStatTipsKatex(rootOrSelector = "#pf-analytics-body") {
  if (!window.renderMathInElement) return;
  const root = typeof rootOrSelector === "string"
    ? document.querySelector(rootOrSelector)
    : rootOrSelector;
  if (!root) return;
  root.querySelectorAll(".pf-metric-tip").forEach(el => {
    if (el.dataset.katex === "1") return;
    try {
      renderMathInElement(el, { delimiters: [{ left: "$$", right: "$$", display: true }], throwOnError: false });
      el.dataset.katex = "1";
    } catch (_) {}
  });
}

function statRowHtml(label, value, valueCls) {
  const info = METRIC_INFO[label];
  const dataAttr = info ? ` data-info="1" data-metric="${escapeHtml(label)}"` : "";
  const tip = metricTipHtml(label, info);
  return `<div class="pf-stat-row"${dataAttr}>
    <span class="l">${label}</span>
    <span class="v ${valueCls || ""}">${value}</span>
    ${tip}
  </div>`;
}

function renderValuationAnalystHtml(a) {
  const w = a.weighted || {};
  const an = a.analyst || {};
  const rows = [
    ["P/E (wtd)", fmtNumOr(w.pe, 2)],
    ["P/S (wtd)", fmtNumOr(w.ps, 2)],
    ["EV/EBITDA (wtd)", fmtNumOr(w.ev_ebitda, 2)],
    ["Div yield (wtd)", w.div_yield == null ? "—" : fmtPctFrac(w.div_yield)],
    ["Market cap (wtd avg)", fmtCapBig(w.market_cap)],
    ["Analyst rating (1=SB, 5=SS)", fmtNumOr(an.mean_rating, 2)],
    ["Weighted target upside", fmtPctSigned(an.weighted_target_upside_pct), an.weighted_target_upside_pct == null ? "" : (an.weighted_target_upside_pct >= 0 ? "pos" : "neg")],
    ["Analysts covering (sum)", an.n_analysts_total != null ? an.n_analysts_total : "—"],
  ];
  return `<div class="pf-stats">${rows.map(r => statRowHtml(r[0], r[1], r[2])).join("")}</div>`;
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
  return `<div class="pf-stats">${rows.map(r => statRowHtml(r[0], r[1])).join("")}</div>`;
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
  const showNdx = STATE.showNdx && series.nasdaq && series.nasdaq.length;
  const showSec = STATE.showSec && series.sector_mix && series.sector_mix.length;
  const showDd  = STATE.showDd && series.drawdown && series.drawdown.length;

  const W = 720, H = 220;
  const padL = 36, padR = 12, padT = 8, padB = 22;
  const t0 = port[0][0], t1 = port[port.length-1][0];
  const xScale = (t) => padL + ((t - t0) / Math.max(1, (t1 - t0))) * (W - padL - padR);
  let lo = Infinity, hi = -Infinity;
  for (const p of port) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (showSpy) for (const p of series.spy) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (showNdx) for (const p of series.nasdaq) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
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
  const svg = `<svg id="pf-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    ${ticks.map(t => `<line x1="${padL}" y1="${t.y.toFixed(1)}" x2="${W-padR}" y2="${t.y.toFixed(1)}" stroke="var(--border)" stroke-width="0.5" stroke-dasharray="2 3"/>`).join("")}
    ${ticks.map(t => `<text x="${padL-6}" y="${(t.y+3).toFixed(1)}" font-size="10" fill="var(--muted)" text-anchor="end">${t.v.toFixed(0)}</text>`).join("")}
    ${xt.map(t => `<text x="${t.x.toFixed(1)}" y="${(H-padB+12).toFixed(0)}" font-size="10" fill="var(--muted)" text-anchor="middle">${fmtT(t.t)}</text>`).join("")}
    ${showSec ? `<path d="${path(series.sector_mix)}" fill="none" stroke="#f59e0b" stroke-width="1.4" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
    ${showNdx ? `<path d="${path(series.nasdaq)}" fill="none" stroke="#06b6d4" stroke-width="1.4" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
    ${showSpy ? `<path d="${path(series.spy)}" fill="none" stroke="#8b5cf6" stroke-width="1.4" stroke-dasharray="4 3" opacity="0.85"/>` : ""}
    <path d="${path(port)}" fill="none" stroke="${accent}" stroke-width="2"/>
    <rect class="pf-sel" id="pf-sel" x="0" y="${padT}" width="0" height="${H-padT-padB}" style="display:none"/>
    <line class="pf-cross" id="pf-cv" x1="0" x2="0" y1="${padT}" y2="${H-padB}"/>
    <line class="pf-cross" id="pf-ch" y1="0" y2="0" x1="${padL}" x2="${W-padR}"/>
    <circle class="pf-dot" id="pf-dot" r="4" cx="0" cy="0"/>
    ${showSpy ? `<circle class="pf-dot spy" id="pf-dot-spy" r="3.5" cx="0" cy="0"/>` : ""}
    ${showNdx ? `<circle class="pf-dot ndx" id="pf-dot-ndx" r="3.5" cx="0" cy="0"/>` : ""}
    ${showSec ? `<circle class="pf-dot sec" id="pf-dot-sec" r="3.5" cx="0" cy="0"/>` : ""}
    <rect id="pf-overlay" x="${padL}" y="${padT}" width="${W-padL-padR}" height="${H-padT-padB}" fill="transparent" style="cursor:crosshair"/>
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
  hostEl.innerHTML = svg + ddSvg + `<div class="pf-tt" id="pf-tt"></div>` + `<div class="pf-chart-info" id="pf-chart-info"></div>`;
  const legend = [];
  legend.push(`<span><i style="background:#2f81f7"></i> Portfolio</span>`);
  if (showSpy) legend.push(`<span><i style="background:#8b5cf6"></i> SPY</span>`);
  if (showNdx) legend.push(`<span><i style="background:#06b6d4"></i> NASDAQ</span>`);
  if (showSec) legend.push(`<span><i style="background:#f59e0b"></i> Sector mix</span>`);
  if (showDd)  legend.push(`<span><i style="background:#f85149"></i> Drawdown</span>`);
  legendEl.innerHTML = legend.join("");

  attachPortfolioChartInteraction({
    W, H, padL, padR, padT, padB,
    t0, t1, xScale, yScale,
    port, spy: showSpy ? series.spy : null,
    ndx: showNdx ? series.nasdaq : null,
    sec: showSec ? series.sector_mix : null,
    hostEl,
  });
}

function attachPortfolioChartInteraction(g) {
  const svg = document.getElementById("pf-svg");
  const overlay = document.getElementById("pf-overlay");
  if (!svg || !overlay) return;
  const tt = document.getElementById("pf-tt");
  const cv = document.getElementById("pf-cv");
  const ch = document.getElementById("pf-ch");
  const dot = document.getElementById("pf-dot");
  const dotSpy = document.getElementById("pf-dot-spy");
  const dotNdx = document.getElementById("pf-dot-ndx");
  const dotSec = document.getElementById("pf-dot-sec");
  const sel = document.getElementById("pf-sel");
  const info = document.getElementById("pf-chart-info");
  const wrap = g.hostEl;

  const defaultInfo = () => {
    const port = g.port;
    if (!port.length) return "";
    const pct = (port[port.length-1][1] / port[0][1] - 1) * 100;
    const cls = pct >= 0 ? "pos" : "neg";
    const parts = [
      `<span><b class="${cls}">${(pct>=0?"+":"")+pct.toFixed(2)}%</b> · Portfolio</span>`,
    ];
    if (g.spy) {
      const p = (g.spy[g.spy.length-1][1] / g.spy[0][1] - 1) * 100;
      parts.push(`<span><b class="${p>=0?'pos':'neg'}">${(p>=0?"+":"")+p.toFixed(2)}%</b> · SPY</span>`);
    }
    if (g.ndx) {
      const p = (g.ndx[g.ndx.length-1][1] / g.ndx[0][1] - 1) * 100;
      parts.push(`<span><b class="${p>=0?'pos':'neg'}">${(p>=0?"+":"")+p.toFixed(2)}%</b> · NASDAQ</span>`);
    }
    if (g.sec) {
      const p = (g.sec[g.sec.length-1][1] / g.sec[0][1] - 1) * 100;
      parts.push(`<span><b class="${p>=0?'pos':'neg'}">${(p>=0?"+":"")+p.toFixed(2)}%</b> · Sector mix</span>`);
    }
    parts.push(`<span class="selection-hint">drag on chart to measure a sub-period →</span>`);
    return parts.join("");
  };
  info.innerHTML = defaultInfo();

  const nearestIdx = (pts, t) => {
    if (!pts || !pts.length) return -1;
    let lo = 0, hi = pts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (pts[mid][0] < t) lo = mid + 1; else hi = mid;
    }
    if (lo > 0 && Math.abs(pts[lo - 1][0] - t) < Math.abs(pts[lo][0] - t)) return lo - 1;
    return lo;
  };
  const clientToSvgX = (clientX) => {
    const r = svg.getBoundingClientRect();
    return (clientX - r.left) * (g.W / r.width);
  };
  const pxToData = (px) => g.t0 + (px - g.padL) / (g.W - g.padL - g.padR) * (g.t1 - g.t0);

  let dragging = false, dragStartT = null;

  overlay.addEventListener("mousemove", (e) => {
    const svgX = clientToSvgX(e.clientX);
    const t = pxToData(Math.max(g.padL, Math.min(g.W - g.padR, svgX)));
    const i = nearestIdx(g.port, t);
    if (i < 0) return;
    const sp = g.port[i];
    const x = g.xScale(sp[0]);
    const y = g.yScale(sp[1]);
    cv.setAttribute("x1", x); cv.setAttribute("x2", x); cv.style.opacity = 1;
    ch.setAttribute("y1", y); ch.setAttribute("y2", y); ch.style.opacity = 1;
    dot.setAttribute("cx", x); dot.setAttribute("cy", y); dot.style.opacity = 1;
    const pct = (sp[1] / g.port[0][1] - 1) * 100;
    let extra = "";
    if (dotSpy && g.spy) {
      const j = nearestIdx(g.spy, sp[0]);
      if (j >= 0) {
        const px = g.xScale(g.spy[j][0]); const py = g.yScale(g.spy[j][1]);
        dotSpy.setAttribute("cx", px); dotSpy.setAttribute("cy", py); dotSpy.style.opacity = 1;
        const sPct = (g.spy[j][1] / g.spy[0][1] - 1) * 100;
        extra += `<div class="tt-row"><span>SPY</span><b class="${sPct>=0?'pos':'neg'}">${(sPct>=0?'+':'')+sPct.toFixed(2)}%</b></div>`;
      }
    }
    if (dotNdx && g.ndx) {
      const j = nearestIdx(g.ndx, sp[0]);
      if (j >= 0) {
        const px = g.xScale(g.ndx[j][0]); const py = g.yScale(g.ndx[j][1]);
        dotNdx.setAttribute("cx", px); dotNdx.setAttribute("cy", py); dotNdx.style.opacity = 1;
        const nPct = (g.ndx[j][1] / g.ndx[0][1] - 1) * 100;
        extra += `<div class="tt-row"><span>NASDAQ</span><b class="${nPct>=0?'pos':'neg'}">${(nPct>=0?'+':'')+nPct.toFixed(2)}%</b></div>`;
      }
    }
    if (dotSec && g.sec) {
      const j = nearestIdx(g.sec, sp[0]);
      if (j >= 0) {
        const px = g.xScale(g.sec[j][0]); const py = g.yScale(g.sec[j][1]);
        dotSec.setAttribute("cx", px); dotSec.setAttribute("cy", py); dotSec.style.opacity = 1;
        const sPct = (g.sec[j][1] / g.sec[0][1] - 1) * 100;
        extra += `<div class="tt-row"><span>Sector mix</span><b class="${sPct>=0?'pos':'neg'}">${(sPct>=0?'+':'')+sPct.toFixed(2)}%</b></div>`;
      }
    }
    const dt = new Date(sp[0]);
    tt.innerHTML = `
      <div class="tt-date">${dt.toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric'})}</div>
      <div class="tt-row"><span>Portfolio</span><b class="${pct>=0?'pos':'neg'}">${(pct>=0?'+':'')+pct.toFixed(2)}%</b></div>
      ${extra}
    `;
    tt.classList.add("show");
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
      sel.setAttribute("width", Math.max(1, bx - ax));
      const iA = nearestIdx(g.port, a), iB = nearestIdx(g.port, b);
      if (iA >= 0 && iB >= 0 && iA !== iB) {
        const va = g.port[iA][1], vb = g.port[iB][1];
        const pPct = (vb/va - 1) * 100;
        let drag = `<span><b class="${pPct>=0?'pos':'neg'}">${(pPct>=0?'+':'')+pPct.toFixed(2)}%</b> · Portfolio (selection)</span>`;
        if (g.spy) {
          const jA = nearestIdx(g.spy, a), jB = nearestIdx(g.spy, b);
          if (jA >= 0 && jB >= 0 && jA !== jB) {
            const sp2 = (g.spy[jB][1]/g.spy[jA][1] - 1) * 100;
            drag += `<span><b class="${sp2>=0?'pos':'neg'}">${(sp2>=0?'+':'')+sp2.toFixed(2)}%</b> · SPY</span>`;
          }
        }
        if (g.ndx) {
          const jA = nearestIdx(g.ndx, a), jB = nearestIdx(g.ndx, b);
          if (jA >= 0 && jB >= 0 && jA !== jB) {
            const nx = (g.ndx[jB][1]/g.ndx[jA][1] - 1) * 100;
            drag += `<span><b class="${nx>=0?'pos':'neg'}">${(nx>=0?'+':'')+nx.toFixed(2)}%</b> · NASDAQ</span>`;
          }
        }
        if (g.sec) {
          const jA = nearestIdx(g.sec, a), jB = nearestIdx(g.sec, b);
          if (jA >= 0 && jB >= 0 && jA !== jB) {
            const sc = (g.sec[jB][1]/g.sec[jA][1] - 1) * 100;
            drag += `<span><b class="${sc>=0?'pos':'neg'}">${(sc>=0?'+':'')+sc.toFixed(2)}%</b> · Sector mix</span>`;
          }
        }
        drag += `<span class="selection-hint">${new Date(g.port[iA][0]).toLocaleDateString()} → ${new Date(g.port[iB][0]).toLocaleDateString()}</span>`;
        info.innerHTML = drag;
      }
    }
  });
  overlay.addEventListener("mouseleave", () => {
    cv.style.opacity = 0; ch.style.opacity = 0; dot.style.opacity = 0;
    if (dotSpy) dotSpy.style.opacity = 0;
    if (dotNdx) dotNdx.style.opacity = 0;
    if (dotSec) dotSec.style.opacity = 0;
    tt.classList.remove("show");
  });
  overlay.addEventListener("mousedown", (e) => {
    dragging = true;
    const svgX = clientToSvgX(e.clientX);
    dragStartT = pxToData(Math.max(g.padL, Math.min(g.W - g.padR, svgX)));
    sel.style.display = "";
    sel.setAttribute("x", g.xScale(dragStartT));
    sel.setAttribute("width", 1);
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false; dragStartT = null;
    // Keep selection visible briefly, then fade back to defaults
    setTimeout(() => {
      sel.style.display = "none";
      info.innerHTML = defaultInfo();
    }, 1800);
  });
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
function equalWeightsOf(rows) {
  if (!rows.length) return {};
  const w = 1 / rows.length;
  return Object.fromEntries(rows.map(r => [r.symbol, w]));
}

// Tracks what the modal is currently editing: "adhoc" (ad-hoc custom, no save),
// "new" (saving a brand-new preset), or "edit" (editing an existing one).
// `editingName` is the original name of the preset under edit so we can rename.
let WEIGHTS_INTENT = "adhoc";
let WEIGHTS_EDITING_NAME = null;

function openWeightsPopup(opts) {
  if (!DATA.length) return toast("Build a portfolio first.");
  opts = opts || {};
  const rows = DATA.filter(r => r && r.symbol);
  // Seed the draft from the most appropriate source:
  //   intent=edit → the preset's saved weights
  //   intent=new  → current resolved mode weights (capture what user sees)
  //   intent=adhoc (default) → existing customWeights or active mode
  let init;
  if (opts.intent === "edit" && opts.presetName) {
    const p = presetByName(opts.presetName);
    init = (p && p.weights) ? p.weights : weightsForMode(STATE.mode);
    WEIGHTS_INTENT = "edit"; WEIGHTS_EDITING_NAME = opts.presetName;
  } else if (opts.intent === "new") {
    init = weightsForMode(STATE.mode);
    WEIGHTS_INTENT = "new"; WEIGHTS_EDITING_NAME = null;
  } else {
    init = STATE.customWeights || weightsForMode(STATE.mode);
    WEIGHTS_INTENT = "adhoc"; WEIGHTS_EDITING_NAME = null;
  }
  WEIGHTS_DRAFT = rows.map(r => ({
    symbol: r.symbol,
    name: r.name || r.symbol,
    weight: Number(init[r.symbol] || 0),
    locked: false,
  }));
  // Wire UI to reflect intent (title, prefilled name, delete-button visibility).
  const titleEl = document.getElementById("pf-weights-title");
  const nameEl = document.getElementById("pf-weights-name");
  const delBtn = document.getElementById("pf-weights-delete");
  const statusEl = document.getElementById("pf-weights-status");
  const saved = STATE.activeView && STATE.activeView !== AD_HOC_KEY;
  if (titleEl) titleEl.textContent =
      WEIGHTS_INTENT === "edit" ? `Edit preset · ${opts.presetName}` :
      WEIGHTS_INTENT === "new"  ? "New weight preset" :
                                  "Custom weights";
  if (nameEl) nameEl.value = WEIGHTS_INTENT === "edit" ? opts.presetName : "";
  if (delBtn) delBtn.style.display = WEIGHTS_INTENT === "edit" ? "" : "none";
  if (statusEl) statusEl.textContent = saved ? "" : "Save the portfolio to enable named presets.";
  // Disable save buttons when there's no active saved portfolio to attach to.
  ["pf-weights-save", "pf-weights-saveas"].forEach(id => {
    const b = document.getElementById(id); if (b) b.disabled = !saved;
  });
  renderWeightsRows();
  $("#pf-weights-bg").classList.add("show");
}

function closeWeightsPopup() {
  $("#pf-weights-bg").classList.remove("show");
  hideInlinePrompt();
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

function _normalizedDraft() {
  const total = WEIGHTS_DRAFT.reduce((a, r) => a + r.weight, 0);
  if (total <= 0) return null;
  const out = {};
  for (const r of WEIGHTS_DRAFT) out[r.symbol] = r.weight / total;
  return out;
}

function applyWeightsDraft() {
  // Apply alone (no Save) = legacy ad-hoc "custom" path. Updates STATE.mode to
  // "custom" and stores the normalized vector. Saved presets use savePresetClick
  // / saveAsPresetClick instead, which also call selectMode("preset:<name>").
  const w = _normalizedDraft();
  if (!w) { toast("Weights must sum to a positive value."); return; }
  STATE.customWeights = w;
  STATE.mode = "custom";
  const tabMap = currentAnalyticsMap();
  for (const k of Object.keys(tabMap)) {
    if (k.startsWith("custom|")) delete tabMap[k];
  }
  closeWeightsPopup();
  renderModeBar();
  persistActivePreset();
  requestAnalytics({force: true});
}

function _invalidateModeCache(mode) {
  // Drop any cached analytics for a specific mode across all periods so the
  // next request hits the backend with the updated weights.
  const tabMap = currentAnalyticsMap();
  for (const k of Object.keys(tabMap)) {
    if (k.startsWith(mode + "|")) delete tabMap[k];
  }
}

async function savePresetClick() {
  // "Save" — if intent=edit, update in place (rename if name field changed);
  // otherwise treat as Save-as so the user is forced to name it.
  const nameInput = document.getElementById("pf-weights-name");
  const name = (nameInput && nameInput.value || "").trim();
  if (!name) { saveAsPresetClick(); return; }
  const w = _normalizedDraft();
  if (!w) return toast("Weights must sum to a positive value.");
  try {
    const renameFrom = WEIGHTS_INTENT === "edit" && WEIGHTS_EDITING_NAME && WEIGHTS_EDITING_NAME !== name
                        ? WEIGHTS_EDITING_NAME : null;
    await savePresetServer(name, w, {renameFrom, setActive: true});
    _invalidateModeCache(modeId(name));
    if (renameFrom) _invalidateModeCache(modeId(renameFrom));
    STATE.mode = modeId(name);
    STATE.customWeights = null;
    closeWeightsPopup();
    renderModeBar();
    persistActivePreset();
    requestAnalytics({force: true});
    toast(`Preset "${name}" saved.`);
  } catch (e) {
    toast("Save failed: " + (e.message || e));
  }
}

function saveAsPresetClick() {
  const existing = (STATE.weightPresets || []).map(p => p.name);
  showInlinePrompt({
    title: "Save preset as…",
    initial: "",
    placeholder: "e.g. Growth Tilt",
    validate(name) {
      if (!name) return "Name required.";
      if (existing.includes(name)) return `"${name}" already exists. Pick a different name.`;
      return null;
    },
    onOk: async (name) => {
      const w = _normalizedDraft();
      if (!w) return toast("Weights must sum to a positive value.");
      try {
        await savePresetServer(name, w, {setActive: true});
        STATE.mode = modeId(name);
        STATE.customWeights = null;
        _invalidateModeCache(modeId(name));
        closeWeightsPopup();
        renderModeBar();
        persistActivePreset();
        requestAnalytics({force: true});
        toast(`Preset "${name}" saved.`);
      } catch (e) {
        toast("Save failed: " + (e.message || e));
      }
    },
  });
}

async function deletePresetClick() {
  if (WEIGHTS_INTENT !== "edit" || !WEIGHTS_EDITING_NAME) return;
  const name = WEIGHTS_EDITING_NAME;
  try {
    await deletePresetServer(name);
    if (STATE.mode === modeId(name)) STATE.mode = "cap";
    _invalidateModeCache(modeId(name));
    closeWeightsPopup();
    renderModeBar();
    persistActivePreset();
    requestAnalytics({force: true});
    toast(`Preset "${name}" deleted.`);
  } catch (e) {
    toast("Delete failed: " + (e.message || e));
  }
}

/* Inline name prompt — used by Save-as (and by MPT "Save as preset"). Keeps
   us off the native prompt() so the UI stays consistent. */
let _ipState = null;
function showInlinePrompt(opts) {
  _ipState = opts || {};
  const host = document.getElementById("pf-name-prompt");
  const titleEl = document.getElementById("pf-name-prompt-title");
  const descEl = document.getElementById("pf-name-prompt-desc");
  const input = document.getElementById("pf-name-prompt-input");
  const err = document.getElementById("pf-name-prompt-err");
  if (!host || !input) return;
  if (titleEl) titleEl.textContent = _ipState.title || "Name";
  if (descEl) {
    const desc = _ipState.description || "";
    if (desc) { descEl.textContent = desc; descEl.style.display = ""; }
    else { descEl.textContent = ""; descEl.style.display = "none"; }
  }
  input.placeholder = _ipState.placeholder || "";
  input.value = _ipState.initial || "";
  if (err) err.textContent = "";
  host.classList.add("show");
  setTimeout(() => { input.focus(); input.select(); }, 30);
}
function hideInlinePrompt() {
  const host = document.getElementById("pf-name-prompt");
  if (host) host.classList.remove("show");
  _ipState = null;
}
function _ipSubmit() {
  if (!_ipState) return;
  const input = document.getElementById("pf-name-prompt-input");
  const err = document.getElementById("pf-name-prompt-err");
  const v = (input.value || "").trim();
  const msg = _ipState.validate ? _ipState.validate(v) : null;
  if (msg) { if (err) err.textContent = msg; return; }
  const onOk = _ipState.onOk;
  hideInlinePrompt();
  if (onOk) onOk(v);
}

function updateModeButtons() { renderModeBar(); }

function renderModeBar() {
  // Rebuilds the pill bar: Equal, Cap, each saved preset, then [+] (and [✎]
  // when a preset is currently active so the user can jump straight to edit).
  const host = document.getElementById("pf-mode-toggle");
  if (!host) return;
  const active = STATE.mode;
  const pills = [];
  function pill(mode, label, extra) {
    const cls = "pf-mode-pill" + (active === mode ? " active" : "") + (extra ? " " + extra : "");
    return `<span class="${cls}" role="tab" data-mode="${escapeHtml(mode)}" tabindex="0">${escapeHtml(label)}</span>`;
  }
  pills.push(pill("equal", "Equal"));
  pills.push(pill("cap", "Cap"));
  for (const p of (STATE.weightPresets || [])) {
    if (!p || !p.name) continue;
    pills.push(pill(modeId(p.name), p.name, "preset"));
  }
  // Trailing controls (only meaningful for saved portfolios)
  const canAdd = !!STATE.activeView && STATE.activeView !== AD_HOC_KEY && DATA.length > 0;
  if (canAdd) {
    pills.push(`<span class="pf-mode-pill icon" id="pf-mode-add" title="New preset from current weights">＋</span>`);
  }
  const activeName = activePresetName();
  if (activeName) {
    pills.push(`<span class="pf-mode-pill icon" id="pf-mode-edit" title="Edit '${escapeHtml(activeName)}'">✎</span>`);
  }
  host.innerHTML = pills.join("");
  host.querySelectorAll(".pf-mode-pill[data-mode]").forEach(el => {
    el.addEventListener("click", () => selectMode(el.dataset.mode));
    el.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectMode(el.dataset.mode); }});
  });
  const addBtn = document.getElementById("pf-mode-add");
  if (addBtn) addBtn.onclick = () => openWeightsPopup({intent: "new"});
  const editBtn = document.getElementById("pf-mode-edit");
  if (editBtn) editBtn.onclick = () => openWeightsPopup({intent: "edit", presetName: activeName});
}

function selectMode(mode) {
  if (!mode || mode === STATE.mode) return;
  STATE.mode = mode;
  // Drop ad-hoc custom weights when switching away from "custom" — preset modes
  // resolve through STATE.weightPresets, not customWeights.
  if (mode !== "custom") STATE.customWeights = null;
  renderModeBar();
  persistActivePreset();
  requestAnalytics();
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

// About-MPT modal — layered on top of the MPT overlay (z-index 95 vs 90).
// Mirrors openInfo()/closeInfo() so KaTeX renders math the same way.
let _mptKatexRendered = false;
function openMptInfo() {
  const bg = document.getElementById("pf-mpt-info-bg");
  if (!bg) return;
  bg.classList.add("show");
  if (!_mptKatexRendered && window.renderMathInElement) {
    renderMathInElement(bg, {
      delimiters: [{ left: "$$", right: "$$", display: true }],
      throwOnError: false,
    });
    _mptKatexRendered = true;
  }
}
function closeMptInfo() {
  const bg = document.getElementById("pf-mpt-info-bg");
  if (bg) bg.classList.remove("show");
}

/* ===========================================================================
 * Wire up
 * --------------------------------------------------------------------------- */
// Primary button mode: "build" when no saved portfolio is loaded, "update" when one is.
// In "update" mode, #build becomes "Update Portfolio" (saves entries + refreshes), and
// #save-as becomes "Save as New Portfolio".
function primaryButtonMode() {
  const name = STATE.activeView;
  const namedLoaded = name && name !== AD_HOC_KEY;
  return namedLoaded ? "update" : "build";
}
function updatePrimaryButtonLabels() {
  const mode = primaryButtonMode();
  const buildBtn = $("#build");
  const saveAsBtn = $("#save-as");
  if (!buildBtn || !saveAsBtn) return;
  if (mode === "update") {
    buildBtn.textContent = "Update Portfolio";
    saveAsBtn.textContent = "Save as New Portfolio";
  } else {
    buildBtn.textContent = "Build Dashboard";
    saveAsBtn.textContent = "＋ Save as new";
  }
}
function runPrimary() {
  if (primaryButtonMode() === "update") {
    // saveWatchlist persists current entries and auto-rebuilds if they changed.
    // If they didn't change, fall back to a plain refresh so the button always "does something".
    const name = STATE.activeView;
    const savedView = name && VIEWS[name];
    const currentEntries = $("#tickers").value.trim();
    const savedEntries = (savedView && savedView.entries || "").trim();
    if (currentEntries && currentEntries !== savedEntries) {
      saveWatchlist();
    } else {
      build({keepPanelOpen: true});
    }
  } else {
    build({keepPanelOpen: true});
  }
}
$("#build").onclick = runPrimary;
$("#refresh").onclick = () => build({keepPanelOpen: $("#input-panel").classList.contains("hidden") ? false : true});
$("#save-as").onclick = saveAsNewWatchlist;
$("#export").onclick = exportXlsx;
// Keep labels in sync whenever the active view or the textarea changes.
$("#tickers").addEventListener("input", updatePrimaryButtonLabels);
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
$("#tickers").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); runPrimary(); }
});
window.addEventListener("scroll", () => {
  $("#topbar").classList.toggle("scrolled", window.scrollY > 4);
});
window.addEventListener("resize", () => {
  if (STATE.fitColumns) render();
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
function wireOverlayPill(id, stateKey) {
  const el = $(id);
  if (!el) return;
  const toggle = () => {
    STATE[stateKey] = !STATE[stateKey];
    el.dataset.on = STATE[stateKey] ? "1" : "0";
    el.setAttribute("aria-checked", STATE[stateKey] ? "true" : "false");
    renderAnalyticsBody();
  };
  el.addEventListener("click", (e) => {
    // Clicking the info icon shouldn't toggle the overlay.
    if (e.target.closest(".ovl-info")) return;
    toggle();
  });
  el.addEventListener("keydown", (e) => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); }
  });
  // Initial sync from STATE.
  el.dataset.on = STATE[stateKey] ? "1" : "0";
  el.setAttribute("aria-checked", STATE[stateKey] ? "true" : "false");
}
wireOverlayPill("#pf-show-spy", "showSpy");
wireOverlayPill("#pf-show-ndx", "showNdx");
wireOverlayPill("#pf-show-sec", "showSec");
wireOverlayPill("#pf-show-dd", "showDd");

/* --- Weights popup wiring --- */
$("#pf-weights-bg").addEventListener("click", (e) => { if (e.target.id === "pf-weights-bg") closeWeightsPopup(); });
$("#pf-weights-close").addEventListener("click", closeWeightsPopup);
$("#pf-weights-cancel").addEventListener("click", closeWeightsPopup);
$("#pf-weights-equal").addEventListener("click", resetDraftToEqual);
$("#pf-weights-reset").addEventListener("click", resetDraftToCap);
$("#pf-weights-apply").addEventListener("click", applyWeightsDraft);
$("#pf-weights-save").addEventListener("click", savePresetClick);
$("#pf-weights-saveas").addEventListener("click", saveAsPresetClick);
$("#pf-weights-delete").addEventListener("click", deletePresetClick);
$("#pf-name-prompt-ok").addEventListener("click", _ipSubmit);
$("#pf-name-prompt-cancel").addEventListener("click", hideInlinePrompt);
$("#pf-name-prompt-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); _ipSubmit(); }
  else if (e.key === "Escape") { e.preventDefault(); hideInlinePrompt(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const ip = document.getElementById("pf-name-prompt");
  if (ip && ip.classList.contains("show")) { hideInlinePrompt(); return; }
  if ($("#pf-weights-bg").classList.contains("show")) closeWeightsPopup();
});
// Initial paint — empty until DATA is built but renders the [+] / Equal/Cap baseline.
renderModeBar();

/* ===========================================================================
 * MPT — Portfolio Optimization overlay
 * --------------------------------------------------------------------------
 * The Optimize button opens a full-screen workspace that computes the
 * long-only efficient frontier from the active portfolio, lets the user pick
 * a point along it with a slider, and apply / save the resulting weights as
 * a named preset. Math lives in mpt.py (Critical Line Algorithm); this code
 * just drives the UI and renders the inline SVG chart.
 * --------------------------------------------------------------------------- */
const MPT = {
  result: null,        // latest /api/efficient-frontier response
  selectedIdx: 0,      // index into result.frontier for the slider marker
  cvarIdx: 0,          // index into result.cvar_frontier for the CVaR slider
  hoverIdx: null,      // index of point under cursor (cloud or frontier)
  activeLine: "frontier", // which curve drives sidebar/apply: "frontier" | "cvar"
  pulseUntil: 0,       // performance.now() time at which the current pulse ends
  pulseKind: null,     // legend key being pulsed (frontier|cvar|tangency|equal|cap|current)
  pulseRaf: 0,         // rAF id for the active pulse animation loop
  view: null,          // portfolio name this run is bound to
  runs: [],            // saved runs metadata for the active view
  busy: false,
};

function openMptOverlay() {
  if (!DATA.length) return toast("Build a portfolio first.");
  if (!STATE.activeView || STATE.activeView === AD_HOC_KEY) {
    // Ad-hoc tabs still get to optimize, but the runs sidebar + save flows
    // need a named view to attach to. We surface this with a status line.
    document.getElementById("pf-mpt-sub").textContent =
      "Save the portfolio first to keep runs / presets.";
  } else {
    document.getElementById("pf-mpt-sub").textContent =
      "Markowitz long-only · Critical Line Algorithm · " + STATE.activeView;
  }
  MPT.view = STATE.activeView;
  document.getElementById("pf-mpt-bg").classList.add("show");
  // Lock body scroll while the overlay is open so the page underneath
  // can't move. Inner sidebar still scrolls via its own overflow-y.
  document.body.dataset.mptPrevOverflow = document.body.style.overflow || "";
  document.body.style.overflow = "hidden";
  mptLoadRuns();
}
function closeMptOverlay() {
  document.getElementById("pf-mpt-bg").classList.remove("show");
  document.body.style.overflow = document.body.dataset.mptPrevOverflow || "";
  delete document.body.dataset.mptPrevOverflow;
  // Cancel any in-flight progress animation so a half-filled bar doesn't
  // linger if the user reopens the overlay before the next run.
  mptProgressStop();
}

// Budget label table — also drives the deterministic progress bar duration
// (#9) and the legend metadata strip.
const MPT_BUDGET_LABEL = {
  fast: "Fast (~2s)",
  standard: "Standard (~5s)",
  thorough: "Thorough (~15s)",
  exhaustive: "Exhaustive (~60s)",
};
const MPT_BUDGET_SECONDS = { fast: 2, standard: 5, thorough: 15, exhaustive: 60 };

function mptSetBudget(value) {
  const root = document.getElementById("pf-mpt-budget");
  if (!root) return;
  const v = MPT_BUDGET_LABEL[value] ? value : "standard";
  root.dataset.value = v;
  const label = root.querySelector(".pf-mpt-select-label");
  if (label) label.textContent = MPT_BUDGET_LABEL[v];
  root.querySelectorAll(".pf-mpt-select-menu li").forEach(li => {
    li.setAttribute("aria-selected", li.dataset.value === v ? "true" : "false");
  });
}

function mptGetParams() {
  const lookback = document.querySelector("#pf-mpt-lookback .active")?.dataset.v || "3Y";
  const frequency = document.querySelector("#pf-mpt-freq .active")?.dataset.v || "weekly";
  const rf = (Number(document.getElementById("pf-mpt-rf").value) || 0) / 100;
  const budget = document.getElementById("pf-mpt-budget").dataset.value || "standard";
  const mode = document.querySelector("#pf-mpt-mode .active")?.dataset.v || "sparse";
  const diversified = mode === "diversified";
  return {lookback, frequency, rf, budget, diversified};
}

/* --- Deterministic progress bar --- */
let _mptProgressTimer = null;
function mptProgressStart(budget) {
  const chart = document.getElementById("pf-mpt-chart");
  const status = document.getElementById("pf-mpt-status");
  if (!chart || !status) return;
  const dur = MPT_BUDGET_SECONDS[budget] || 5;
  status.innerHTML = `
    <div class="mpt-progress" id="mpt-progress">
      <div class="mpt-progress-track"><div class="mpt-progress-fill"></div></div>
      <div class="mpt-progress-label">
        <span class="mpt-progress-text">Optimizing portfolio…</span>
        <span class="mpt-progress-timer">0.0s / ~${dur}s</span>
      </div>
    </div>`;
  const root = status.querySelector("#mpt-progress");
  const fill = root.querySelector(".mpt-progress-fill");
  const timer = root.querySelector(".mpt-progress-timer");
  const text = root.querySelector(".mpt-progress-text");
  // Force layout, then start the CSS width transition over the budget window.
  void fill.offsetWidth;
  fill.style.transition = `width ${dur}s linear`;
  fill.style.width = "100%";
  const start = performance.now();
  _mptProgressTimer = setInterval(() => {
    const elapsed = (performance.now() - start) / 1000;
    if (elapsed >= dur) {
      text.textContent = "Finalizing…";
      timer.textContent = `${elapsed.toFixed(1)}s / ~${dur}s`;
    } else {
      timer.textContent = `${elapsed.toFixed(1)}s / ~${dur}s`;
    }
  }, 100);
}
function mptProgressStop(state) {
  if (_mptProgressTimer) { clearInterval(_mptProgressTimer); _mptProgressTimer = null; }
  if (state === "done" || state === "fail") {
    const root = document.querySelector("#mpt-progress");
    if (root) {
      root.classList.add(state);
      const fill = root.querySelector(".mpt-progress-fill");
      if (fill) fill.style.width = "100%";
    }
  }
}

async function mptRun() {
  if (!DATA.length) return;
  const btn = document.getElementById("pf-mpt-run");
  const status = document.getElementById("pf-mpt-status");
  // Clear any previous frame so the cloud/frontier disappear during compute.
  mptClearChart();
  const params = mptGetParams();
  mptProgressStart(params.budget);
  btn.disabled = true; MPT.busy = true;
  try {
    const r = await fetch("/api/efficient-frontier", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        rows: DATA, display_ccy: FX_QUOTE,
        ...params,
        current_weights: weightsForMode(STATE.mode),
      }),
    });
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || ("HTTP " + r.status));
    MPT.result = d;
    // Default selection = tangency (max Sharpe), if available, else mid-frontier
    if (d.tangency) {
      let best = 0, bestSh = -Infinity;
      d.frontier.forEach((p, i) => {
        const sh = p.vol > 1e-9 ? (p.ret - params.rf) / p.vol : -Infinity;
        if (sh > bestSh) { bestSh = sh; best = i; }
      });
      MPT.selectedIdx = best;
    } else {
      MPT.selectedIdx = Math.floor(d.frontier.length / 2);
    }
    document.getElementById("pf-mpt-slider").disabled = false;
    document.getElementById("pf-mpt-slider").max = String(Math.max(0, d.frontier.length - 1));
    document.getElementById("pf-mpt-slider").value = String(MPT.selectedIdx);
    // CVaR slider: index 0 = strictest tail (CVaR99), index N-1 = CVaR50.
    // Default selection = midpoint of the curve so neither extreme dominates.
    const cv = d.cvar_frontier || [];
    const cvarSlider = document.getElementById("pf-mpt-cvar-slider");
    if (cv.length) {
      MPT.cvarIdx = Math.floor(cv.length / 2);
      cvarSlider.disabled = false;
      cvarSlider.max = String(Math.max(0, cv.length - 1));
      cvarSlider.value = String(MPT.cvarIdx);
    } else {
      MPT.cvarIdx = 0;
      cvarSlider.disabled = true;
      cvarSlider.value = "0";
    }
    MPT.activeLine = "frontier";
    mptProgressStop("done");
    if (status) status.innerHTML = "";
    mptRender();
    // Auto-save the run. Server-side eviction (mpt save_mpt_run) drops any
    // stale runs whose portfolio composition or risk-free rate differs from
    // this one, keeping the Recent-runs list relevant without UI prompts.
    mptSaveRun({silent: true}).catch(() => {});
  } catch (e) {
    mptProgressStop("fail");
    if (status) {
      status.innerHTML = `<div class="pf-mpt-error">Optimization failed: ${escapeHtml(e.message || String(e))}</div>`;
    }
  } finally {
    btn.disabled = false; MPT.busy = false;
  }
}

function mptClearChart() {
  const base = document.getElementById("pf-mpt-base");
  if (base) { const ctx = base.getContext("2d"); ctx && ctx.clearRect(0, 0, base.width, base.height); }
  const ov = document.getElementById("pf-mpt-overlay");
  if (ov) { const ctx = ov.getContext("2d"); ctx && ctx.clearRect(0, 0, ov.width, ov.height); }
  const leg = document.getElementById("pf-mpt-legend"); if (leg) leg.innerHTML = "";
}

function mptRender() {
  const d = MPT.result; if (!d) return;
  mptRenderChart();
  mptRenderSide();
}

// MPT._proj is the shared projection used by every chart draw call and the
// hit-test. Single source of truth — both canvases agree on every coord by
// construction, eliminating the SVG/canvas drift the old 3-layer chart had.
function mptCurrentScale() { return MPT._proj; }

// Resize both canvases identically through one helper so their backing
// stores cannot drift apart. CSS controls the *display* size (inset:0 +
// width/height:100%); we touch ONLY the backing store. Setting inline
// width/height previously left the canvas stuck at its first measurement
// even when the modal reflowed, causing axis labels to render below the
// chart's visual box.
function mptSizeCanvases(host) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const rect = host.getBoundingClientRect();
  const cssW = Math.max(360, Math.floor(rect.width));
  const cssH = Math.max(280, Math.floor(rect.height));
  const W = Math.floor(cssW * dpr), H = Math.floor(cssH * dpr);
  for (const id of ["pf-mpt-base", "pf-mpt-overlay"]) {
    const cv = document.getElementById(id);
    if (!cv) continue;
    cv.width = W; cv.height = H;
  }
  return {cssW, cssH, dpr};
}

// Tick generation — nice rounding by powers of 10.
function mptTicks(lo, hi, n) {
  const span = hi - lo; if (span <= 0) return [lo];
  const step0 = Math.pow(10, Math.floor(Math.log10(span / n)));
  const norm = span / (n * step0);
  const step = step0 * (norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1);
  const start = Math.ceil(lo / step) * step;
  const out = []; for (let v = start; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

function mptRenderChart() {
  const d = MPT.result; if (!d) return;
  const host = document.getElementById("pf-mpt-chart");
  // Atomic size for both canvases.
  const {cssW, cssH, dpr} = mptSizeCanvases(host);
  const pad = {l: 56, r: 18, t: 18, b: 38};

  // Domain from cloud + frontier + anchors.
  let xMax = 0, xMin = Infinity, yMax = -Infinity, yMin = Infinity;
  const consume = (v, r) => { if (v < xMin) xMin = v; if (v > xMax) xMax = v; if (r < yMin) yMin = r; if (r > yMax) yMax = r; };
  (d.cloud || []).forEach(p => consume(p[0], p[1]));
  (d.frontier || []).forEach(p => consume(p.vol, p.ret));
  (d.cvar_frontier || []).forEach(p => consume(p.vol, p.ret));
  Object.values(d.anchors || {}).forEach(a => a && consume(a.vol, a.ret));
  if (!isFinite(xMin)) { xMin = 0; xMax = 0.3; yMin = 0; yMax = 0.2; }
  const params = mptGetParams();
  yMin = Math.min(yMin, params.rf);
  const dx = (xMax - xMin) * 0.06 || 0.01;
  const dy = (yMax - yMin) * 0.08 || 0.01;
  xMin = Math.max(0, xMin - dx); xMax += dx; yMin -= dy; yMax += dy;

  // Projection in CSS pixels (toPx); fromPx for hit-testing.
  const xToPx = v => pad.l + (v - xMin) / (xMax - xMin) * (cssW - pad.l - pad.r);
  const yToPx = r => cssH - pad.b - (r - yMin) / (yMax - yMin) * (cssH - pad.t - pad.b);
  const proj = {xMin, xMax, yMin, yMax, pad, cssW, cssH, dpr,
                X: xToPx, Y: yToPx, toPx: (v, r) => [xToPx(v), yToPx(r)]};
  MPT._proj = proj;

  // Sharpe-by-color setup.
  const sharpeOf = p => (p[0] > 1e-9 ? (p[1] - params.rf) / p[0] : 0);
  let sMin = Infinity, sMax = -Infinity;
  (d.cloud || []).forEach(p => { const s = sharpeOf(p); if (s < sMin) sMin = s; if (s > sMax) sMax = s; });
  if (!isFinite(sMin)) { sMin = 0; sMax = 1; }
  function sharpeColor(s) {
    const t = Math.max(0, Math.min(1, (s - sMin) / Math.max(1e-9, sMax - sMin)));
    const stops = [[94,40,120],[33,144,141],[253,231,37]];
    const i = t * 2, j = Math.floor(i), f = i - j;
    const a = stops[j], b = stops[Math.min(2, j + 1)];
    return `rgb(${Math.round(a[0]+(b[0]-a[0])*f)},${Math.round(a[1]+(b[1]-a[1])*f)},${Math.round(a[2]+(b[2]-a[2])*f)})`;
  }

  // --- Base canvas: axes + cloud + frontier + anchors + tangency line ---
  const baseCv = document.getElementById("pf-mpt-base");
  const ctx = baseCv.getContext("2d");
  // Reset transform to identity, then scale so every subsequent call is in
  // CSS pixels — the same units as the projection.
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  drawAxes(ctx, proj, d, params);
  drawCloud(ctx, d.cloud || [], proj, sharpeOf, sharpeColor);
  drawFrontierAndAnchors(ctx, d, proj, params);

  // --- Overlay canvas: selection + hover ghost ---
  drawOverlay();

  // --- Wire interaction on the overlay canvas (top of stack) ---
  const ov = document.getElementById("pf-mpt-overlay");
  // Replace listeners by cloning so we never stack handlers on re-render.
  const fresh = ov.cloneNode(false);
  ov.parentNode.replaceChild(fresh, ov);
  // Hit-test against BOTH the MV frontier and the CVaR curve. Returns
  // {line, idx} for the nearest point; line drives which slider moves.
  function _hitPoint(ev) {
    const r = fresh.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    const px = (ev.clientX - r.left) * (cssW / r.width);
    const py = (ev.clientY - r.top) * (cssH / r.height);
    let bestLine = "frontier", best = 0, bestD = Infinity;
    (d.frontier || []).forEach((p, i) => {
      const dxp = xToPx(p.vol) - px, dyp = yToPx(p.ret) - py;
      const dd = dxp * dxp + dyp * dyp;
      if (dd < bestD) { bestD = dd; best = i; bestLine = "frontier"; }
    });
    (d.cvar_frontier || []).forEach((p, i) => {
      const dxp = xToPx(p.vol) - px, dyp = yToPx(p.ret) - py;
      const dd = dxp * dxp + dyp * dyp;
      if (dd < bestD) { bestD = dd; best = i; bestLine = "cvar"; }
    });
    return {line: bestLine, idx: best};
  }
  fresh.addEventListener("click", (ev) => {
    const hit = _hitPoint(ev);
    if (!hit) return;
    if (hit.line === "cvar") {
      MPT.cvarIdx = hit.idx;
      MPT.activeLine = "cvar";
      document.getElementById("pf-mpt-cvar-slider").value = String(hit.idx);
    } else {
      MPT.selectedIdx = hit.idx;
      MPT.activeLine = "frontier";
      document.getElementById("pf-mpt-slider").value = String(hit.idx);
    }
    MPT.hoverIdx = null;
    mptHideFrontierTip();
    mptUpdateSelection();
    mptRenderSide();
    // Flash the Apply button so the user knows the point is selected and ready.
    const applyBtn = document.getElementById("pf-mpt-apply");
    if (applyBtn) {
      applyBtn.classList.add("pf-mpt-apply-flash");
      setTimeout(() => applyBtn.classList.remove("pf-mpt-apply-flash"), 600);
    }
  });
  fresh.addEventListener("mousemove", (ev) => {
    const hit = _hitPoint(ev);
    if (!hit || hit.line !== "frontier") {
      MPT.hoverIdx = null;
      mptUpdateSelection();
      mptHideFrontierTip();
      return;
    }
    MPT.hoverIdx = hit.idx;
    mptUpdateSelection();
    mptShowFrontierTip(ev, hit.idx);
  });
  fresh.addEventListener("mouseleave", () => {
    MPT.hoverIdx = null;
    mptUpdateSelection();
    mptHideFrontierTip();
  });

  // --- Legend with marker-shaped swatches. Each entry is clickable — see
  //     mptLegendClick below for the kind → action mapping. ---
  const legend = document.getElementById("pf-mpt-legend");
  const hasCvar = (d.cvar_frontier || []).length > 0;
  legend.innerHTML = `
    <span data-legend="frontier" title="Click to switch to the efficient-frontier slider and highlight the line.">${legendSwatch("frontier")}Efficient frontier</span>
    ${hasCvar ? `<span data-legend="cvar" title="Click to switch to the CVaR slider and highlight the curve.">${legendSwatch("cvar")}CVaR-optimal (α=99↔50)</span>` : ""}
    <span data-legend="tangency" title="Click to jump to the tangency portfolio.">${legendSwatch("tangency")}Tangency (max Sharpe)</span>
    <span data-legend="equal" title="Click to jump to the equal-weight portfolio on the frontier.">${legendSwatch("equal")}Equal-weight</span>
    <span data-legend="cap" title="Click to jump to the cap-weight portfolio on the frontier.">${legendSwatch("cap")}Cap-weight</span>
    <span data-legend="current" title="Click to jump to the current portfolio on the frontier.">${legendSwatch("current")}Current</span>
    <span class="no-click">${legendSwatch("selected")}Selected</span>
    <span class="no-click" style="margin-left:auto">${(d.meta?.n_samples_actual || (d.cloud || []).length).toLocaleString()} Monte-Carlo portfolios · ${d.meta?.n_obs || "?"} ${d.params?.frequency || "?"} obs · ${d.meta?.total_ms || "?"}ms</span>
  `;
  legend.querySelectorAll("span[data-legend]").forEach(el => {
    el.addEventListener("click", () => mptLegendClick(el.dataset.legend));
  });
}

// Snap the MV-frontier slider to the index nearest (vol, ret) — used by
// the legend's anchor entries so clicking "Equal-weight" jumps the
// frontier marker to the closest feasible point on the curve.
function _nearestFrontierIdx(vol, ret) {
  const d = MPT.result; if (!d || !(d.frontier || []).length) return 0;
  let best = 0, bestD = Infinity;
  d.frontier.forEach((p, i) => {
    const dvx = (p.vol - vol), dvy = (p.ret - ret);
    const dd = dvx * dvx + dvy * dvy;
    if (dd < bestD) { bestD = dd; best = i; }
  });
  return best;
}

// Legend click handler — kind → (select point + pulse). For the two line
// entries we switch activeLine; for anchor markers we snap the MV-frontier
// slider to the closest point on the curve.
function mptLegendClick(kind) {
  const d = MPT.result; if (!d) return;
  switch (kind) {
    case "frontier":
      MPT.activeLine = "frontier";
      break;
    case "cvar":
      if ((d.cvar_frontier || []).length) MPT.activeLine = "cvar";
      break;
    case "tangency":
      if (d.tangency) {
        MPT.selectedIdx = _nearestFrontierIdx(d.tangency.vol, d.tangency.ret);
        MPT.activeLine = "frontier";
        document.getElementById("pf-mpt-slider").value = String(MPT.selectedIdx);
      }
      break;
    case "equal":
    case "cap":
    case "current": {
      const a = (d.anchors || {})[kind];
      if (a) {
        MPT.selectedIdx = _nearestFrontierIdx(a.vol, a.ret);
        MPT.activeLine = "frontier";
        document.getElementById("pf-mpt-slider").value = String(MPT.selectedIdx);
      }
      break;
    }
  }
  mptPulse(kind);
  mptUpdateSelection();
  mptRenderSide();
}

function legendSwatch(kind) {
  const c = 'class="lg-swatch"';
  switch (kind) {
    case "frontier":
      return `<svg ${c} viewBox="0 0 14 12"><path d="M1 9 Q7 1 13 4" fill="none" stroke="var(--accent)" stroke-width="2.2"/></svg>`;
    case "cvar":
      return `<svg ${c} viewBox="0 0 14 12"><path d="M1 9 Q7 1 13 4" fill="none" stroke="#ea580c" stroke-width="2" stroke-dasharray="3 2"/></svg>`;
    case "tangency":
      return `<svg ${c} viewBox="0 0 14 12"><polygon points="${star(7,6,5,2.4,5)}" fill="#fbbf24" stroke="#7c2d12" stroke-width="0.6"/></svg>`;
    case "equal":
      return `<svg ${c} viewBox="0 0 14 12"><polygon points="7,1 12,6 7,11 2,6" fill="#8b5cf6"/></svg>`;
    case "cap":
      return `<svg ${c} viewBox="0 0 14 12"><polygon points="7,1 12,11 2,11" fill="#06b6d4"/></svg>`;
    case "current":
      return `<svg ${c} viewBox="0 0 14 12"><path d="M2 2 L12 10 M12 2 L2 10" stroke="#f59e0b" stroke-width="2.2"/></svg>`;
    case "selected":
      return `<svg ${c} viewBox="0 0 14 12"><circle cx="7" cy="6" r="4.5" fill="none" stroke="var(--accent)" stroke-width="1.8"/><circle cx="7" cy="6" r="1.6" fill="var(--accent)"/></svg>`;
    default:
      return `<span class="lg-dot" style="background:var(--muted)"></span>`;
  }
}

// CSS-variable color resolver: canvas can't read `var(--accent)` directly,
// so look it up against :root once and cache for the current render.
function _mptCssColor(name, fallback) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  } catch (_e) { return fallback; }
}

function drawAxes(ctx, proj, d, params) {
  const {pad, cssW, cssH, xMin, xMax, yMin, yMax, X, Y} = proj;
  const accent = _mptCssColor("--accent", "#0969da");
  const border = _mptCssColor("--border", "#d0d7de");
  const muted  = _mptCssColor("--muted",  "#6e7781");
  const bgCv   = _mptCssColor("--bg-canvas", "#ffffff");

  // Plot box background (subtle) + border.
  ctx.fillStyle = bgCv;
  ctx.globalAlpha = 0.04;
  ctx.fillRect(pad.l, pad.t, cssW - pad.l - pad.r, cssH - pad.t - pad.b);
  ctx.globalAlpha = 1;
  ctx.strokeStyle = border; ctx.lineWidth = 1;
  ctx.strokeRect(pad.l + 0.5, pad.t + 0.5, cssW - pad.l - pad.r - 1, cssH - pad.t - pad.b - 1);

  const xt = mptTicks(xMin, xMax, 5);
  const yt = mptTicks(yMin, yMax, 5);
  const fmtPct = v => (v * 100).toFixed(v < 0.1 ? 1 : 0) + "%";

  ctx.save();
  ctx.strokeStyle = border; ctx.globalAlpha = 0.45;
  ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
  for (const v of xt) {
    const x = Math.round(X(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, cssH - pad.b); ctx.stroke();
  }
  for (const v of yt) {
    const y = Math.round(Y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(cssW - pad.r, y); ctx.stroke();
  }
  ctx.restore();

  ctx.fillStyle = muted;
  ctx.font = "10px ui-sans-serif, -apple-system, system-ui, sans-serif";
  ctx.textBaseline = "middle";
  ctx.textAlign = "center";
  for (const v of xt) ctx.fillText(fmtPct(v), X(v), cssH - pad.b + 14);
  ctx.textAlign = "end";
  for (const v of yt) ctx.fillText(fmtPct(v), pad.l - 6, Y(v));

  // Axis titles.
  ctx.font = "11px ui-sans-serif, -apple-system, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText("Volatility (annualized)", (cssW - pad.r + pad.l) / 2, cssH - 6);
  ctx.save();
  ctx.translate(14, (cssH - pad.b + pad.t) / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("Return (annualized)", 0, 0);
  ctx.restore();

  // Tangency dashed line from rf marker → tangency, extended past it.
  if (d.tangency) {
    const tx = X(d.tangency.vol), ty = Y(d.tangency.ret);
    const rfX = X(0), rfY = Y(params.rf);
    const dxp = tx - rfX, dyp = ty - rfY;
    const k = 1.6;
    const ex = rfX + dxp * k, ey = rfY + dyp * k;
    ctx.save();
    ctx.strokeStyle = accent; ctx.lineWidth = 1.2;
    ctx.setLineDash([5, 4]); ctx.globalAlpha = 0.85;
    ctx.beginPath(); ctx.moveTo(rfX, rfY); ctx.lineTo(ex, ey); ctx.stroke();
    ctx.restore();
    ctx.fillStyle = accent; ctx.globalAlpha = 0.8;
    ctx.beginPath(); ctx.arc(rfX, rfY, 3, 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha = 1;
    ctx.fillStyle = accent;
    ctx.textAlign = "start"; ctx.textBaseline = "alphabetic";
    ctx.font = "10px ui-sans-serif, -apple-system, system-ui, sans-serif";
    ctx.fillText(`rf ${(params.rf * 100).toFixed(2)}%`, rfX + 8, rfY - 6);
  }
}

// Cloud paint on the base canvas — single fillRect per point. The caller
// already set ctx transform so we're in CSS pixels; no manual dpr math.
function drawCloud(ctx, cloud, proj, sharpeOf, sharpeColor) {
  if (!cloud || !cloud.length) return;
  const {X, Y, pad, cssW, cssH, dpr} = proj;
  ctx.save();
  // Clip to the plot box so cloud dots never escape onto the axes.
  ctx.beginPath();
  ctx.rect(pad.l, pad.t, cssW - pad.l - pad.r, cssH - pad.t - pad.b);
  ctx.clip();
  ctx.globalAlpha = 0.85;
  // Dot side ~1.7 CSS px; bumped slightly on hi-dpi so dots stay visible.
  const r = Math.max(1.1, 1.7 * Math.min(dpr, 1.5));
  const step = cloud.length > 1_200_000 ? Math.ceil(cloud.length / 1_200_000) : 1;
  for (let i = 0; i < cloud.length; i += step) {
    const p = cloud[i];
    const x = X(p[0]), y = Y(p[1]);
    ctx.fillStyle = sharpeColor(sharpeOf(p));
    ctx.fillRect(x - r * 0.5, y - r * 0.5, r, r);
  }
  ctx.restore();
}

function drawFrontierAndAnchors(ctx, d, proj, params) {
  const {X, Y} = proj;
  const accent = _mptCssColor("--accent", "#0969da");
  const cvarColor = "#ea580c";
  if (d.frontier && d.frontier.length) {
    ctx.save();
    ctx.strokeStyle = accent; ctx.lineWidth = 2.2;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.beginPath();
    d.frontier.forEach((p, i) => {
      const x = X(p.vol), y = Y(p.ret);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.restore();
  }
  // CVaR curve: dashed orange polyline distinct from the MV frontier so the
  // two families of optima don't blur together.
  if (d.cvar_frontier && d.cvar_frontier.length) {
    ctx.save();
    ctx.strokeStyle = cvarColor; ctx.lineWidth = 2.0;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    d.cvar_frontier.forEach((p, i) => {
      const x = X(p.vol), y = Y(p.ret);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.restore();
  }
  if (d.min_vol) drawMarker(ctx, "endpoint", X(d.min_vol.vol), Y(d.min_vol.ret));
  if (d.max_ret) drawMarker(ctx, "endpoint", X(d.max_ret.vol), Y(d.max_ret.ret));
  const an = d.anchors || {};
  if (an.equal)   drawMarker(ctx, "equal",   X(an.equal.vol),   Y(an.equal.ret));
  if (an.cap)     drawMarker(ctx, "cap",     X(an.cap.vol),     Y(an.cap.ret));
  if (an.current) drawMarker(ctx, "current", X(an.current.vol), Y(an.current.ret));
  if (d.tangency) drawMarker(ctx, "tangency", X(d.tangency.vol), Y(d.tangency.ret));
}

function drawMarker(ctx, kind, x, y) {
  ctx.save();
  switch (kind) {
    case "endpoint":
      ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.stroke();
      break;
    case "equal":
      ctx.fillStyle = "#8b5cf6"; ctx.globalAlpha = 0.95;
      ctx.beginPath();
      ctx.moveTo(x, y - 6); ctx.lineTo(x + 6, y); ctx.lineTo(x, y + 6); ctx.lineTo(x - 6, y); ctx.closePath();
      ctx.fill();
      break;
    case "cap":
      ctx.fillStyle = "#06b6d4"; ctx.globalAlpha = 0.95;
      ctx.beginPath();
      ctx.moveTo(x, y - 6); ctx.lineTo(x + 6, y + 5); ctx.lineTo(x - 6, y + 5); ctx.closePath();
      ctx.fill();
      break;
    case "current":
      ctx.strokeStyle = "#f59e0b"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(x - 5, y - 5); ctx.lineTo(x + 5, y + 5);
      ctx.moveTo(x + 5, y - 5); ctx.lineTo(x - 5, y + 5); ctx.stroke();
      break;
    case "tangency": {
      const R = 8, r = 4, n = 5;
      ctx.fillStyle = "#fbbf24"; ctx.strokeStyle = "#7c2d12"; ctx.lineWidth = 0.8;
      ctx.beginPath();
      for (let i = 0; i < 2 * n; i++) {
        const ang = -Math.PI / 2 + i * Math.PI / n;
        const rad = i % 2 === 0 ? R : r;
        const px = x + rad * Math.cos(ang), py = y + rad * Math.sin(ang);
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.closePath(); ctx.fill(); ctx.stroke();
      break;
    }
  }
  ctx.restore();
}

// Paint just the overlay canvas (selection ring + hover ghost + pulse).
// Cheap — called on slider input, mousemove, and the pulse animation loop;
// the base canvas is untouched.
function drawOverlay() {
  const d = MPT.result; if (!d) return;
  const proj = MPT._proj; if (!proj) return;
  const cv = document.getElementById("pf-mpt-overlay");
  if (!cv) return;
  const {cssW, cssH, dpr, X, Y} = proj;
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  const accent = _mptCssColor("--accent", "#0969da");
  const cvarColor = "#ea580c";

  // Hover ghost (only when it isn't the same point as the selection).
  if (MPT.hoverIdx != null && MPT.hoverIdx !== MPT.selectedIdx) {
    const hp = d.frontier[MPT.hoverIdx];
    if (hp) {
      const hx = X(hp.vol), hy = Y(hp.ret);
      ctx.save();
      ctx.strokeStyle = accent; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.6;
      ctx.beginPath(); ctx.arc(hx, hy, 6, 0, Math.PI * 2); ctx.stroke();
      ctx.fillStyle = accent; ctx.globalAlpha = 0.8;
      ctx.beginPath(); ctx.arc(hx, hy, 2.2, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
    }
  }

  // Inactive (muted) ring on the curve that ISN'T currently driving the
  // sidebar — keeps both sliders' positions visible at a glance.
  const inactiveIsCvar = MPT.activeLine !== "cvar";
  const cv_arr = d.cvar_frontier || [];
  const muted = inactiveIsCvar ? cv_arr[MPT.cvarIdx] : d.frontier[MPT.selectedIdx];
  if (muted) {
    const mx = X(muted.vol), my = Y(muted.ret);
    const mcol = inactiveIsCvar ? cvarColor : accent;
    ctx.save();
    ctx.strokeStyle = mcol; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.55;
    ctx.beginPath(); ctx.arc(mx, my, 7, 0, Math.PI * 2); ctx.stroke();
    ctx.restore();
  }

  // Active selection ring on the currently-driven curve.
  const activeIsCvar = MPT.activeLine === "cvar";
  const sel = activeIsCvar ? cv_arr[MPT.cvarIdx] : d.frontier[MPT.selectedIdx];
  if (sel) {
    const x = X(sel.vol), y = Y(sel.ret);
    const col = activeIsCvar ? cvarColor : accent;
    ctx.save();
    ctx.strokeStyle = col; ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.stroke();
    ctx.fillStyle = col;
    ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2); ctx.fill();
    ctx.restore();
  }

  // Pulse layer: fades over the pulse window.
  drawPulse(ctx, d, proj, accent, cvarColor);
}

// Pulse animation — fades a highlight ring (or thicker line stroke for the
// frontier/cvar entries) over a 700 ms window. Uses MPT.pulseUntil + MPT.pulseKind.
function drawPulse(ctx, d, proj, accent, cvarColor) {
  const now = performance.now();
  if (!MPT.pulseKind || now >= MPT.pulseUntil) return;
  const PULSE_MS = 700;
  const t = Math.max(0, Math.min(1, (MPT.pulseUntil - now) / PULSE_MS));
  // Ease-out: alpha fades 0.85 → 0; radius grows 6 → 22.
  const alpha = 0.85 * t;
  const radius = 6 + (1 - t) * 16;
  const {X, Y} = proj;
  const kind = MPT.pulseKind;

  const ringAt = (vol, ret, color) => {
    if (vol == null || ret == null) return;
    ctx.save();
    ctx.strokeStyle = color; ctx.lineWidth = 3; ctx.globalAlpha = alpha;
    ctx.beginPath(); ctx.arc(X(vol), Y(ret), radius, 0, Math.PI * 2); ctx.stroke();
    ctx.restore();
  };
  const lineWith = (pts, color, lw) => {
    if (!pts || !pts.length) return;
    ctx.save();
    ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.globalAlpha = alpha;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.beginPath();
    pts.forEach((p, i) => { const x = X(p.vol), y = Y(p.ret); if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); });
    ctx.stroke();
    ctx.restore();
  };

  switch (kind) {
    case "frontier":
      lineWith(d.frontier, accent, 5);
      break;
    case "cvar":
      lineWith(d.cvar_frontier, cvarColor, 5);
      break;
    case "tangency":
      if (d.tangency) ringAt(d.tangency.vol, d.tangency.ret, "#fbbf24");
      break;
    case "equal":
      if (d.anchors?.equal) ringAt(d.anchors.equal.vol, d.anchors.equal.ret, "#8b5cf6");
      break;
    case "cap":
      if (d.anchors?.cap) ringAt(d.anchors.cap.vol, d.anchors.cap.ret, "#06b6d4");
      break;
    case "current":
      if (d.anchors?.current) ringAt(d.anchors.current.vol, d.anchors.current.ret, "#f59e0b");
      break;
  }

  // Keep the animation running while the pulse hasn't expired.
  if (MPT.pulseRaf) cancelAnimationFrame(MPT.pulseRaf);
  MPT.pulseRaf = requestAnimationFrame(() => { MPT.pulseRaf = 0; drawOverlay(); });
}

// Trigger a pulse on the named legend entry. Called by the legend click
// handler (and by chart clicks if we want visual reinforcement).
function mptPulse(kind) {
  MPT.pulseKind = kind;
  MPT.pulseUntil = performance.now() + 700;
  drawOverlay();
}

// Public name preserved so existing slider/click handlers keep working.
function mptUpdateSelection() { drawOverlay(); }

// Floating tooltip anchored to the cursor while hovering the chart. Shows
// (vol, ret, sharpe) of the nearest frontier point plus its top-3 weights.
// Positioning routes through the unified placeTip() so it never clips off
// the viewport.
function mptShowFrontierTip(ev, idx) {
  const d = MPT.result; if (!d) return;
  const p = d.frontier[idx]; if (!p) return;
  const params = mptGetParams();
  const sharpe = p.vol > 1e-9 ? (p.ret - params.rf) / p.vol : NaN;
  const top = Object.entries(p.weights || {})
    .filter(([_, w]) => w > 1e-4)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);
  let tip = document.getElementById("pf-mpt-frontier-tip");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "pf-mpt-frontier-tip";
    tip.className = "pf-mpt-frontier-tip";
    document.body.appendChild(tip);
  }
  tip.innerHTML = `
    <div class="tip-title">Frontier point #${idx + 1} / ${d.frontier.length} <span style="font-weight:400;color:var(--muted);font-size:10px">· click to select</span></div>
    <div class="tip-row"><span class="k">Return</span><span class="v">${(p.ret * 100).toFixed(2)}%</span></div>
    <div class="tip-row"><span class="k">Vol</span><span class="v">${(p.vol * 100).toFixed(2)}%</span></div>
    <div class="tip-row"><span class="k">Sharpe</span><span class="v">${isFinite(sharpe) ? sharpe.toFixed(3) : "—"}</span></div>
    ${top.length ? `<div class="tip-sub">Top weights</div>` + top.map(([s, w]) =>
      `<div class="tip-row"><span class="k">${escapeHtml(s)}</span><span class="v">${(w * 100).toFixed(1)}%</span></div>`
    ).join("") : ""}
  `;
  tip.classList.add("show");
  // Anchor on the cursor and let placeTip pick a side / clamp to viewport.
  if (typeof placeTip === "function") {
    placeTip(tip, {left: ev.clientX, top: ev.clientY, right: ev.clientX, bottom: ev.clientY,
                   width: 0, height: 0}, {preferred: "above", offset: 14, gap: 8});
  } else {
    tip.style.left = (ev.clientX + 14) + "px";
    tip.style.top = (ev.clientY + 14) + "px";
  }
}
function mptHideFrontierTip() {
  const tip = document.getElementById("pf-mpt-frontier-tip");
  if (tip) tip.classList.remove("show");
}

function star(cx, cy, R, r, n) {
  // Render a star polygon centered at (cx, cy)
  const pts = [];
  for (let i = 0; i < 2 * n; i++) {
    const ang = -Math.PI / 2 + i * Math.PI / n;
    const rad = i % 2 === 0 ? R : r;
    pts.push(`${(cx + rad * Math.cos(ang)).toFixed(1)},${(cy + rad * Math.sin(ang)).toFixed(1)}`);
  }
  return pts.join(" ");
}

// Pull the currently-selected portfolio (frontier or CVaR curve) — single
// source of truth for the sidebar, apply button, and save flow.
function mptActiveSel() {
  const d = MPT.result; if (!d) return null;
  if (MPT.activeLine === "cvar") {
    const cv = d.cvar_frontier || [];
    return cv[MPT.cvarIdx] || null;
  }
  return (d.frontier || [])[MPT.selectedIdx] || null;
}

function mptRenderSide() {
  const d = MPT.result; if (!d) return;
  const sel = mptActiveSel(); if (!sel) return;
  const params = mptGetParams();
  const sharpe = sel.vol > 1e-9 ? (sel.ret - params.rf) / sel.vol : NaN;
  const stats = document.getElementById("pf-mpt-stats");
  const isCvar = MPT.activeLine === "cvar";
  // Sidebar readouts under each slider track current selection along its curve.
  const sliderReadout = document.getElementById("pf-mpt-slider-readout");
  if (sliderReadout) {
    const fp = (d.frontier || [])[MPT.selectedIdx];
    sliderReadout.textContent = fp
      ? `${(fp.ret * 100).toFixed(1)}% / ${(fp.vol * 100).toFixed(1)}%`
      : "—";
  }
  const cvarReadout = document.getElementById("pf-mpt-cvar-readout");
  if (cvarReadout) {
    const cv = (d.cvar_frontier || [])[MPT.cvarIdx];
    cvarReadout.textContent = cv ? `α=${Math.round(cv.conf * 100)}%` : "—";
  }
  // Stats block. CVaR/VaR are returned in per-period units (matches the
  // returns frequency used for optimisation). Multiply by 100 for %.
  let extraRows = "";
  if (isCvar && sel.cvar != null) {
    const freq = d.params?.frequency || "weekly";
    const conf = Math.round((sel.conf || 0) * 100);
    extraRows = `
      <span class="k">CVaR (α=${conf}%, ${freq})</span><span class="v neg">${(sel.cvar * 100).toFixed(2)}%</span>
      <span class="k">VaR (α=${conf}%, ${freq})</span><span class="v neg">${(sel.var * 100).toFixed(2)}%</span>`;
  }
  stats.innerHTML = `
    <span class="k">Source</span><span class="v">${isCvar ? "Min-CVaR" : "Efficient frontier"}</span>
    <span class="k">Annualised return</span><span class="v ${sel.ret >= 0 ? "pos" : "neg"}">${(sel.ret * 100).toFixed(2)}%</span>
    <span class="k">Annualised vol</span><span class="v">${(sel.vol * 100).toFixed(2)}%</span>
    <span class="k">Sharpe (rf ${(params.rf*100).toFixed(2)}%)</span><span class="v ${sharpe >= 0 ? "pos" : "neg"}">${isFinite(sharpe) ? sharpe.toFixed(3) : "—"}</span>
    ${extraRows}
    <span class="k">Active assets</span><span class="v">${(d.symbols || []).length}${(d.missing || []).length ? ` <span style="color:var(--muted);font-weight:400">(${(d.missing||[]).length} dropped)</span>` : ""}</span>
  `;
  // Weights bars (sorted descending; zero-weight rows hidden for clarity)
  const wlist = document.getElementById("pf-mpt-wlist");
  const ws = Object.entries(sel.weights || {})
    .filter(([_, w]) => w > 1e-4)
    .sort((a, b) => b[1] - a[1]);
  if (!ws.length) {
    wlist.innerHTML = `<span class="pf-mpt-status">No weights at this point.</span>`;
  } else {
    const maxW = ws[0][1];
    wlist.innerHTML = ws.map(([sym, w]) => `
      <div class="pf-mpt-wrow">
        <span title="${escapeHtml(sym)}">${escapeHtml(sym)}</span>
        <div class="pf-mpt-track"><div class="pf-mpt-fill" style="width:${(w/maxW*100).toFixed(1)}%"></div></div>
        <span class="pf-mpt-val">${(w * 100).toFixed(2)}%</span>
      </div>
    `).join("");
  }
  // Slider-row highlighting follows activeLine.
  document.querySelectorAll(".pf-mpt-slider-row").forEach(r => {
    r.classList.toggle("active-line", r.dataset.line === MPT.activeLine);
  });
}

function mptApplyToPortfolio() {
  const d = MPT.result; if (!d) return;
  const sel = mptActiveSel(); if (!sel) return;
  const w = sel.weights || {};
  if (!Object.keys(w).length) { toast("No weights at this point."); return; }
  // Invalidate any stale custom-mode analytics cache before switching.
  const tabMap = currentAnalyticsMap();
  for (const k of Object.keys(tabMap)) {
    if (k.startsWith("custom|")) delete tabMap[k];
  }
  STATE.customWeights = {...w};
  STATE.mode = "custom";
  closeMptOverlay();
  renderModeBar();
  persistActivePreset();
  requestAnalytics({force: true});
  if (MPT.activeLine === "cvar") {
    const conf = Math.round((sel.conf || 0) * 100);
    toast(`Applied min-CVaR α=${conf}% — ${(sel.ret * 100).toFixed(1)}% ret, ${(sel.vol * 100).toFixed(1)}% vol.`);
  } else {
    const pt = MPT.selectedIdx + 1;
    const total = (d.frontier || []).length;
    toast(`Applied MPT point ${pt}/${total} — ${(sel.ret * 100).toFixed(1)}% ret, ${(sel.vol * 100).toFixed(1)}% vol.`);
  }
}

function mptSaveAsPreset() {
  const d = MPT.result; if (!d) return;
  const sel = mptActiveSel(); if (!sel) return;
  if (!STATE.activeView || STATE.activeView === AD_HOC_KEY) {
    return toast("Save the portfolio first to keep custom weights.");
  }
  const existing = (STATE.weightPresets || []).map(p => p.name);
  const suggested = MPT.activeLine === "cvar"
    ? `CVaR α=${Math.round((sel.conf || 0) * 100)}% ${d.params.lookback} ${d.params.frequency}`
    : `MPT ${d.params.lookback} ${d.params.frequency}`;
  showInlinePrompt({
    title: "Save as Custom Weights",
    description:
      "Saves the currently selected portfolio (the point on the efficient frontier under the slider) as a named Custom-Weights configuration under this portfolio. " +
      "Switch between custom configurations from the mode bar to compare strategies side-by-side against Equal-weight and Cap-weight.",
    initial: suggested,
    placeholder: "e.g. Tangency 3Y Weekly",
    validate(name) {
      if (!name) return "Name required.";
      if (existing.includes(name)) return `"${name}" already exists.`;
      return null;
    },
    onOk: async (name) => {
      try {
        await savePresetServer(name, sel.weights, {setActive: true});
        STATE.mode = modeId(name);
        STATE.customWeights = null;
        closeMptOverlay();
        renderModeBar();
        persistActivePreset();
        requestAnalytics({force: true});
        toast(`Custom weights "${name}" saved.`);
      } catch (e) { toast("Save failed: " + (e.message || e)); }
    },
  });
}

async function mptSaveRun({silent} = {silent: false}) {
  const d = MPT.result; if (!d) return;
  if (!STATE.activeView || STATE.activeView === AD_HOC_KEY) {
    if (!silent) toast("Save the portfolio first.");
    return;
  }
  try {
    // Strip the (heavy) cloud before persisting; it can be re-sampled cheaply.
    const compact = {
      params: d.params, symbols: d.symbols, missing: d.missing,
      frontier: d.frontier, cvar_frontier: d.cvar_frontier || [],
      tangency: d.tangency,
      min_vol: d.min_vol, max_ret: d.max_ret, anchors: d.anchors,
      meta: d.meta,
    };
    const r = await fetch("/api/mpt-runs", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({view: STATE.activeView, run: compact}),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "save failed");
    await mptLoadRuns();
    if (!silent) toast("Run saved.");
  } catch (e) { if (!silent) toast("Save failed: " + (e.message || e)); }
}

async function mptLoadRuns() {
  const host = document.getElementById("pf-mpt-runs");
  if (!STATE.activeView || STATE.activeView === AD_HOC_KEY) {
    host.innerHTML = `<span class="pf-mpt-status">Saved runs are per-portfolio.</span>`;
    MPT.runs = []; return;
  }
  try {
    const r = await fetch(`/api/mpt-runs?view=${encodeURIComponent(STATE.activeView)}`);
    const j = await r.json();
    MPT.runs = j.runs || [];
  } catch (_) { MPT.runs = []; }
  if (!MPT.runs.length) {
    host.innerHTML = `<span class="pf-mpt-status">No saved runs yet.</span>`;
    return;
  }
  host.innerHTML = MPT.runs.map(r => {
    const p = r.params || {};
    const t = r.tangency || {};
    return `<div class="pf-mpt-run-row" data-id="${escapeHtml(r.id)}">
      <div style="flex:1">
        <div><b>${escapeHtml(p.lookback || "")} ${escapeHtml(p.frequency || "")}</b> · ${(p.display_ccy || "USD")}</div>
        <div class="meta">${escapeHtml(r.saved_at || "").slice(0, 16).replace("T", " ")} · tangent Sharpe ${t.sharpe != null ? Number(t.sharpe).toFixed(2) : "—"}</div>
      </div>
      <button class="del" title="Delete">✕</button>
    </div>`;
  }).join("");
  host.querySelectorAll(".pf-mpt-run-row").forEach(row => {
    const id = row.dataset.id;
    row.addEventListener("click", (e) => {
      if (e.target.classList.contains("del")) return;
      mptLoadRun(id);
    });
    row.querySelector(".del").addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await fetch(`/api/mpt-runs/${encodeURIComponent(id)}?view=${encodeURIComponent(STATE.activeView)}`, {method: "DELETE"});
        mptLoadRuns();
      } catch (_) {}
    });
  });
}

async function mptLoadRun(id) {
  try {
    const r = await fetch(`/api/mpt-runs/${encodeURIComponent(id)}?view=${encodeURIComponent(STATE.activeView)}`);
    const j = await r.json();
    if (!r.ok || !j.run) throw new Error(j.error || "not found");
    // Saved runs are persisted without the cloud — show an empty cloud so
    // the chart still renders.
    MPT.result = {...j.run, cloud: j.run.cloud || []};
    // Reflect run params in controls
    if (j.run.params) {
      const p = j.run.params;
      document.querySelectorAll("#pf-mpt-lookback button").forEach(b => b.classList.toggle("active", b.dataset.v === p.lookback));
      document.querySelectorAll("#pf-mpt-freq button").forEach(b => b.classList.toggle("active", b.dataset.v === p.frequency));
      if (p.rf != null) document.getElementById("pf-mpt-rf").value = (p.rf * 100).toFixed(2);
      if (p.budget) mptSetBudget(p.budget);
      const savedMode = p.diversified ? "diversified" : "sparse";
      document.querySelectorAll("#pf-mpt-mode button").forEach(b => b.classList.toggle("active", b.dataset.v === savedMode));
    }
    MPT.selectedIdx = Math.floor((MPT.result.frontier || []).length / 2);
    document.getElementById("pf-mpt-slider").max = String(Math.max(0, (MPT.result.frontier || []).length - 1));
    document.getElementById("pf-mpt-slider").value = String(MPT.selectedIdx);
    document.getElementById("pf-mpt-slider").disabled = false;
    // CVaR slider — pre-existing saved runs (before this feature) won't have
    // cvar_frontier, so disable the slider gracefully in that case.
    const cv = MPT.result.cvar_frontier || [];
    const cvarSlider = document.getElementById("pf-mpt-cvar-slider");
    if (cv.length) {
      MPT.cvarIdx = Math.floor(cv.length / 2);
      cvarSlider.max = String(Math.max(0, cv.length - 1));
      cvarSlider.value = String(MPT.cvarIdx);
      cvarSlider.disabled = false;
    } else {
      MPT.cvarIdx = 0;
      cvarSlider.value = "0";
      cvarSlider.disabled = true;
    }
    MPT.activeLine = "frontier";
    mptRender();
  } catch (e) {
    toast("Load failed: " + (e.message || e));
  }
}

/* --- MPT overlay wiring --- */
$("#optimize").addEventListener("click", openMptOverlay);
$("#pf-mpt-close").addEventListener("click", closeMptOverlay);
$("#pf-mpt-bg").addEventListener("click", (e) => { if (e.target.id === "pf-mpt-bg") closeMptOverlay(); });
$("#pf-mpt-run").addEventListener("click", mptRun);
$("#pf-mpt-apply").addEventListener("click", mptApplyToPortfolio);
$("#pf-mpt-save").addEventListener("click", mptSaveAsPreset);
$("#pf-mpt-info")?.addEventListener("click", openMptInfo);
$("#pf-mpt-info-close")?.addEventListener("click", closeMptInfo);
$("#pf-mpt-info-bg")?.addEventListener("click", (e) => { if (e.target.id === "pf-mpt-info-bg") closeMptInfo(); });

// Slider — coalesce rapid input events through requestAnimationFrame so we
// repaint the dynamic marker + sidebar at display rate, never more. The
// static cloud/frontier layers are NOT touched here, so this stays fast
// even with 1M cloud points.
let _mptSliderFrame = 0;
$("#pf-mpt-slider").addEventListener("input", (e) => {
  MPT.selectedIdx = Number(e.target.value) || 0;
  MPT.activeLine = "frontier";
  MPT.hoverIdx = null;
  if (_mptSliderFrame) return;
  _mptSliderFrame = requestAnimationFrame(() => {
    _mptSliderFrame = 0;
    mptUpdateSelection();
    mptRenderSide();
  });
});
let _mptCvarSliderFrame = 0;
$("#pf-mpt-cvar-slider").addEventListener("input", (e) => {
  MPT.cvarIdx = Number(e.target.value) || 0;
  MPT.activeLine = "cvar";
  MPT.hoverIdx = null;
  if (_mptCvarSliderFrame) return;
  _mptCvarSliderFrame = requestAnimationFrame(() => {
    _mptCvarSliderFrame = 0;
    mptUpdateSelection();
    mptRenderSide();
  });
});

// Segmented-control click handlers (lookback + frequency)
document.querySelectorAll("#pf-mpt-lookback button").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#pf-mpt-lookback button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
  });
});
document.querySelectorAll("#pf-mpt-freq button").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#pf-mpt-freq button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
  });
});
document.querySelectorAll("#pf-mpt-mode button").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#pf-mpt-mode button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
  });
});

// --- Custom Compute Budget dropdown ---
(function wireMptBudget() {
  const root = document.getElementById("pf-mpt-budget");
  if (!root) return;
  const menu = root.querySelector(".pf-mpt-select-menu");
  const open = () => { menu.hidden = false; root.classList.add("open"); root.setAttribute("aria-expanded", "true"); };
  const close = () => { menu.hidden = true; root.classList.remove("open"); root.setAttribute("aria-expanded", "false"); };
  root.addEventListener("click", (e) => {
    if (e.target.tagName === "LI") {
      mptSetBudget(e.target.dataset.value);
      close();
      return;
    }
    menu.hidden ? open() : close();
  });
  root.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); menu.hidden ? open() : close(); }
    else if (e.key === "Escape") { close(); }
  });
  document.addEventListener("click", (e) => {
    if (!root.contains(e.target)) close();
  });
})();

// --- Risk-free "Auto" pill + inline sparkline ---
// Holds the most-recent fetched series so the hover crosshair can re-derive
// per-pixel data without refetching.
const _RF_SPARK = { series: null, meta: null, lookback: null };
async function mptFetchRfAuto({silent} = {silent: false}) {
  const btn = document.getElementById("pf-mpt-rf-auto");
  const input = document.getElementById("pf-mpt-rf");
  const spark = document.getElementById("pf-mpt-rf-spark");
  if (!btn || !input || !spark) return;
  const lookback = document.querySelector("#pf-mpt-lookback .active")?.dataset.v || "3Y";
  btn.classList.add("busy");
  try {
    const r = await fetch(`/api/risk-free-history?ccy=${encodeURIComponent(FX_QUOTE)}&lookback=${encodeURIComponent(lookback)}`);
    const j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error || ("HTTP " + r.status));
    if (j.mean_pct != null && isFinite(j.mean_pct)) {
      input.value = Number(j.mean_pct).toFixed(2);
    }
    const series = (j.series || []).filter(p => isFinite(p[1]));
    if (series.length >= 2) {
      _RF_SPARK.series = series; _RF_SPARK.meta = j; _RF_SPARK.lookback = lookback;
      mptDrawRfSpark();
      // Tooltip content is rendered live by the hover handler from _RF_SPARK.
    } else {
      _RF_SPARK.series = null; _RF_SPARK.meta = null;
      spark.classList.remove("show");
      spark.innerHTML = "";
    }
    if (!silent && j.mean_pct == null) toast("No risk-free data available for " + FX_QUOTE + ".");
  } catch (e) {
    if (!silent) toast("Risk-free fetch failed: " + (e.message || e));
  } finally {
    btn.classList.remove("busy");
  }
}

// Draws the polished sparkline: filled area + line + dashed mean baseline +
// latest-value dot. The numeric label lives in the input + hover tooltip.
function mptDrawRfSpark() {
  const spark = document.getElementById("pf-mpt-rf-spark");
  if (!spark || !_RF_SPARK.series) return;
  const series = _RF_SPARK.series;
  const meta = _RF_SPARK.meta || {};
  const vals = series.map(p => p[1]);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = Math.max(1e-9, hi - lo);
  // viewBox = 200 x 36, with 3px top/bottom padding and a tiny right margin
  // for the latest-value dot — no in-SVG percent label anymore.
  const W = 200, H = 36, padY = 3, padR = 4;
  const usableW = W - padR - 2;
  const usableH = H - 2 * padY;
  const xs = series.map((_, i) => 2 + (i / (series.length - 1)) * usableW);
  const ys = series.map(p => H - padY - ((p[1] - lo) / span) * usableH);
  const linePts = xs.map((x, i) => `${x.toFixed(2)},${ys[i].toFixed(2)}`).join(" ");
  const areaPts = `${xs[0].toFixed(2)},${(H - padY).toFixed(2)} ${linePts} ${xs[xs.length-1].toFixed(2)},${(H - padY).toFixed(2)}`;
  const mean = meta.mean_pct != null ? meta.mean_pct : (vals.reduce((a, b) => a + b, 0) / vals.length);
  const meanY = H - padY - ((mean - lo) / span) * usableH;
  const lastX = xs[xs.length - 1], lastY = ys[ys.length - 1];
  spark.innerHTML = `
    <polygon class="rfs-area" points="${areaPts}"/>
    <line class="rfs-base" x1="2" y1="${meanY.toFixed(2)}" x2="${(W - padR).toFixed(2)}" y2="${meanY.toFixed(2)}"/>
    <polyline class="rfs-line" points="${linePts}"/>
    <line class="rfs-cross" id="rfs-cross-line" x1="0" y1="${padY}" x2="0" y2="${H - padY}"/>
    <circle class="rfs-dot" cx="${lastX.toFixed(2)}" cy="${lastY.toFixed(2)}" r="2.4"/>
  `;
  spark.classList.add("show");
}

function _rfLookbackLabel(lb) {
  if (!lb) return "lookback";
  const m = String(lb).match(/^(\d+)\s*Y/i);
  return m ? `${m[1]} Y` : String(lb);
}

// Hover crosshair + rich card. The card matches the data-tip aesthetic but
// supports multi-line content and follows the cursor.
(function wireRfSparkHover() {
  const wrap = document.getElementById("pf-mpt-rf-spark-wrap");
  const spark = document.getElementById("pf-mpt-rf-spark");
  const tip = document.getElementById("pf-mpt-rf-spark-tip");
  if (!wrap || !spark || !tip) return;
  wrap.addEventListener("mousemove", (e) => {
    if (!_RF_SPARK.series || _RF_SPARK.series.length < 2) return;
    const series = _RF_SPARK.series;
    const meta = _RF_SPARK.meta || {};
    const rect = wrap.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const frac = Math.max(0, Math.min(1, px / rect.width));
    const idx = Math.round(frac * (series.length - 1));
    const pt = series[idx];
    const date = (pt[0] || "").slice(0, 10);
    const val = Number(pt[1]);
    const vals = series.map(p => p[1]);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const mean = meta.mean_pct != null ? meta.mean_pct : (vals.reduce((a, b) => a + b, 0) / vals.length);
    const cur = meta.current_pct != null ? meta.current_pct : vals[vals.length - 1];
    const ticker = meta.ticker || "rf";
    const lbLabel = _rfLookbackLabel(_RF_SPARK.lookback);
    const note = meta.source_note ? `<div class="tip-note">${escapeHtml(meta.source_note)}</div>` : "";
    tip.innerHTML = `
      <div class="tip-title">${escapeHtml(ticker)} · risk-free proxy</div>
      <div class="tip-sub">Annualized yield used as the Sharpe / tangency baseline.</div>
      <div class="tip-row"><span class="k">Hover ${escapeHtml(date)}</span><span class="v">${val.toFixed(2)}%</span></div>
      <div class="tip-row"><span class="k">Current</span><span class="v">${cur != null ? cur.toFixed(2) + "%" : "—"}</span></div>
      <div class="tip-row"><span class="k">Mean (${escapeHtml(lbLabel)})</span><span class="v">${mean != null ? mean.toFixed(2) + "%" : "—"}</span></div>
      <div class="tip-row"><span class="k">Range</span><span class="v">${lo.toFixed(2)} – ${hi.toFixed(2)}%</span></div>
      ${note}
    `;
    tip.style.left = px + "px";
    tip.style.top = wrap.clientHeight + "px";
    tip.classList.add("show");
    // Move crosshair on the SVG (viewBox coords)
    const line = document.getElementById("rfs-cross-line");
    if (line) {
      const W = 200, padR = 4;
      const x = 2 + frac * (W - padR - 2);
      line.setAttribute("x1", x.toFixed(2));
      line.setAttribute("x2", x.toFixed(2));
      line.classList.add("show");
    }
  });
  wrap.addEventListener("mouseleave", () => {
    tip.classList.remove("show");
    const line = document.getElementById("rfs-cross-line");
    if (line) line.classList.remove("show");
  });
})();

document.getElementById("pf-mpt-rf-auto")?.addEventListener("click", () => mptFetchRfAuto({silent: false}));

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  // About-MPT modal takes precedence (it sits on top of the MPT overlay)
  const infoBg = document.getElementById("pf-mpt-info-bg");
  if (infoBg && infoBg.classList.contains("show")) { closeMptInfo(); return; }
  if (document.getElementById("pf-mpt-bg").classList.contains("show")) closeMptOverlay();
});
// Re-draw chart on window resize while overlay is open.
window.addEventListener("resize", () => {
  if (document.getElementById("pf-mpt-bg").classList.contains("show") && MPT.result) mptRender();
});

/* ===========================================================================
 * FX selector wiring (topbar dropdown + hover currency-index chart)
 * --------------------------------------------------------------------------- */
function fxLabelFor(ccy) {
  const sym = FX_SYMBOL[ccy];
  return sym ? `${ccy} ${sym}` : ccy;
}
function fxRenderDropdown() {
  const dd = $("#fx-dropdown");
  if (!dd) return;
  dd.innerHTML = FX_SUPPORTED.map(ccy => `
    <div class="fx-opt ${ccy === FX_QUOTE ? 'selected' : ''}" data-ccy="${ccy}" role="option">
      <span class="fx-opt-code">${ccy} ${FX_SYMBOL[ccy] || ''}</span>
      <span class="fx-opt-name">${FX_NAMES[ccy] || ''}</span>
    </div>
  `).join("");
}
function fxUpdateButton() {
  const lbl = $("#fx-btn-label");
  if (lbl) lbl.textContent = fxLabelFor(FX_QUOTE);
}
function fxOpen() {
  fxRenderDropdown();
  $("#fx-dropdown").classList.add("show");
  $("#fx-btn").classList.add("open");
}
function fxClose() {
  $("#fx-dropdown").classList.remove("show");
  $("#fx-btn").classList.remove("open");
  fxHoverHide();
}
function fxToggle() {
  $("#fx-dropdown").classList.contains("show") ? fxClose() : fxOpen();
}
function fxSelect(ccy) {
  if (!ccy || FX_SUPPORTED.indexOf(ccy) < 0) return;
  const prev = FX_QUOTE;
  FX_QUOTE = ccy;
  try { localStorage.setItem("fx_quote", ccy); } catch (e) {}
  fxUpdateButton();
  fxRenderDropdown();
  if (DATA && DATA.length) {
    if (prev !== ccy) {
      // Invalidate every tab's analytics — returns must be recomputed in the new currency.
      STATE.analyticsByTab = {};
      STATE.analytics = null;
    }
    render();
    if (prev !== ccy && DATA.length) requestAnalytics();
  }
  if (DETAIL && DETAIL.data) {
    const det = $("#modal");
    if (det && det.classList && document.getElementById("modal-bg").classList.contains("show")) {
      try { renderSections(); } catch (e) {}
    }
  }
}

/* --- hover currency-index chart ---
 *
 * Cache discipline: ONLY cache non-empty results. An empty array means the
 * upstream call failed (likely yfinance rate-limit on the basket pairs).
 * Caching `[]` made the "no data" message stick on every subsequent hover
 * even after the rate-limit window passed — so we now keep failures
 * uncached and retry on the next hover. Inflight-dedup still prevents
 * burst-fetching when the user wiggles the cursor. */
async function fxFetchIndex(ccy) {
  if (FX_INDEX_CACHE[ccy] && FX_INDEX_CACHE[ccy].length >= 2) {
    return FX_INDEX_CACHE[ccy];
  }
  if (FX_INDEX_INFLIGHT[ccy]) return FX_INDEX_INFLIGHT[ccy];
  const p = (async () => {
    try {
      let pts = [];
      for (let attempt = 0; attempt < 2; attempt++) {
        const r = await fetch(`/api/fx-index?ccy=${encodeURIComponent(ccy)}`);
        if (!r.ok) continue;
        const j = await r.json();
        pts = Array.isArray(j.index) ? j.index : [];
        if (pts.length >= 2) break;
      }
      // Cache only real data; transient failures (empty) stay uncached so
      // the next hover re-attempts the fetch.
      if (pts.length >= 2) FX_INDEX_CACHE[ccy] = pts;
      return pts;
    } catch (e) { return []; }
    finally { delete FX_INDEX_INFLIGHT[ccy]; }
  })();
  FX_INDEX_INFLIGHT[ccy] = p;
  return p;
}
function fxDrawHoverChart(pts) {
  const svg = $("#fx-hover-svg");
  if (!svg) return;
  if (!pts || pts.length < 2) {
    svg.innerHTML = `<text x="110" y="44" text-anchor="middle" fill="var(--muted)" font-size="11">no data</text>`;
    return;
  }
  const w = 220, h = 80, padL = 4, padR = 4, padT = 6, padB = 12;
  const xs = pts.map(p => p[0]);
  const ys = pts.map(p => p[1]);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const ymin = Math.min(...ys), ymax = Math.max(...ys);
  const xrng = (xmax - xmin) || 1;
  const yrng = (ymax - ymin) || 1;
  const sx = t => padL + ((t - xmin) / xrng) * (w - padL - padR);
  const sy = v => padT + (1 - (v - ymin) / yrng) * (h - padT - padB);
  let d = "";
  for (let i = 0; i < pts.length; i++) {
    const x = sx(pts[i][0]).toFixed(1);
    const y = sy(pts[i][1]).toFixed(1);
    d += (i === 0 ? "M" : "L") + x + "," + y + " ";
  }
  const last = ys[ys.length - 1];
  const first = ys[0];
  const up = last >= first;
  const stroke = up
    ? getComputedStyle(document.documentElement).getPropertyValue("--pos").trim() || "#1f883d"
    : getComputedStyle(document.documentElement).getPropertyValue("--neg").trim() || "#cf222e";
  const baseY = sy(100).toFixed(1);
  svg.innerHTML = `
    <line x1="${padL}" x2="${w-padR}" y1="${baseY}" y2="${baseY}" stroke="var(--border)" stroke-dasharray="2 3" stroke-width="1"/>
    <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.4"/>
  `;
}
async function fxHoverShow(ccy, anchorEl) {
  const box = $("#fx-hover");
  if (!box) return;
  if (FX_HOVER_CCY === ccy && box.classList.contains("show")) return;
  const reqId = ++FX_HOVER_REQ_ID;
  FX_HOVER_CCY = ccy;
  $("#fx-hover-title").textContent = `${ccy} basket index (1Y)`;
  $("#fx-hover-foot").innerHTML = lcHtml("fetching index", {bar: true});
  const svg = $("#fx-hover-svg");
  if (svg) svg.innerHTML = "";
  box.classList.add("show");
  // position next to dropdown (anchored to the right side of the topbar);
  // CSS already places it with right:180px,top:36px.
  const pts = await fxFetchIndex(ccy);
  if (!box.classList.contains("show") || reqId !== FX_HOVER_REQ_ID || FX_HOVER_CCY !== ccy) return;
  fxDrawHoverChart(pts);
  if (pts && pts.length >= 2) {
    const ret = (pts[pts.length-1][1] / pts[0][1] - 1) * 100;
    const cls = ret >= 0 ? "pos" : "neg";
    const sign = ret >= 0 ? "+" : "";
    $("#fx-hover-foot").innerHTML = `vs 6-major basket · 1Y <span class="${cls}">${sign}${ret.toFixed(2)}%</span>`;
  } else {
    $("#fx-hover-foot").textContent = "no data";
  }
}
function fxHoverHide() {
  const box = $("#fx-hover");
  FX_HOVER_REQ_ID += 1;
  FX_HOVER_CCY = null;
  if (box) box.classList.remove("show");
}

function fxInit() {
  fxLoadPref();
  fxUpdateButton();
  fxRenderDropdown();
  fxLoadRates().then(() => { if (DATA && DATA.length) render(); });
  const btn = $("#fx-btn");
  if (btn) btn.addEventListener("click", (e) => { e.stopPropagation(); fxToggle(); });
  const dd = $("#fx-dropdown");
  if (dd) {
    dd.addEventListener("click", (e) => {
      const opt = e.target.closest(".fx-opt");
      if (!opt) return;
      fxSelect(opt.dataset.ccy);
      fxClose();
    });
    dd.addEventListener("mouseover", (e) => {
      const opt = e.target.closest(".fx-opt");
      if (!opt) return;
      fxHoverShow(opt.dataset.ccy, opt);
    });
    dd.addEventListener("mouseleave", fxHoverHide);
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#fx-menu")) fxClose();
  });
}

setTheme(readTheme());
fxInit();
renderHeader();
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
            # The page is a one-shot SPA; always serve the latest copy so a
            # `python dashboard.py` restart never gets shadowed by browser cache.
            self.send_header("Cache-Control", "no-store, must-revalidate")
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
        if parsed.path == "/api/column-views":
            raw = load_column_views()
            self._send_json(200, {
            "builtins": ["Default", "Fundamentals", "Momentum"],
                "custom": raw.get("custom_views") or {},
                "active": raw.get("active_view") or "Default",
            })
            return
        if parsed.path.startswith("/api/views/"):
            name = parsed.path[len("/api/views/"):]
            from urllib.parse import unquote
            name = unquote(name)
            view = load_view(name)
            self._send_json(200, {"view": view, "name": name})
            return
        if parsed.path == "/api/fx-rates":
            from urllib.parse import parse_qs

            q = parse_qs(parsed.query)
            base = (q.get("base") or ["USD"])[0].strip().upper() or "USD"
            try:
                self._send_json(200, fx_rates(base))
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/fx-index":
            from urllib.parse import parse_qs

            q = parse_qs(parsed.query)
            ccy = (q.get("ccy") or ["USD"])[0].strip().upper() or "USD"
            period = (q.get("period") or ["1y"])[0].strip() or "1y"
            try:
                series = fx_index_history(ccy, period=period)
                self._send_json(200, {"ccy": ccy, "period": period, "index": series})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/fx-indexes-bulk":
            from urllib.parse import parse_qs

            q = parse_qs(parsed.query)
            period = (q.get("period") or ["1y"])[0].strip() or "1y"
            ccys = [c.strip().upper() for c in (q.get("ccys") or [",".join(SUPPORTED_FX)])[0].split(",") if c.strip()]
            ccys = [c for c in ccys if c in SUPPORTED_FX]
            results: dict[str, list] = {}
            # Sequential — yfinance rate-limits aggressive bulk pulls and we'd
            # rather take a few seconds longer than return half-empty responses.
            for c in ccys:
                try:
                    results[c] = fx_index_history(c, period) or []
                except Exception:
                    results[c] = []
            self._send_json(200, {"period": period, "indexes": results})
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
        if parsed.path == "/api/mpt-runs":
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            view = (q.get("view") or [""])[0].strip()
            self._send_json(200, {"runs": list_mpt_runs(view)})
            return
        if parsed.path == "/api/risk-free-history":
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            ccy = (q.get("ccy") or ["USD"])[0].strip() or "USD"
            lb = (q.get("lookback") or ["3Y"])[0].strip() or "3Y"
            try:
                self._send_json(200, _risk_free_history(ccy, lb))
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path.startswith("/api/mpt-runs/"):
            from urllib.parse import parse_qs, unquote
            rid = unquote(parsed.path[len("/api/mpt-runs/"):])
            view = (parse_qs(parsed.query).get("view") or [""])[0].strip()
            run = load_mpt_run(view, rid)
            if not run:
                self._send_json(404, {"error": "run not found"})
                return
            self._send_json(200, {"run": run})
            return
        if parsed.path == "/api/weight-presets":
            from urllib.parse import parse_qs
            view = (parse_qs(parsed.query).get("view") or [""])[0].strip()
            self._send_json(200, list_weight_presets(view))
            return
        if parsed.path == "/api/analytics-cache":
            from urllib.parse import parse_qs
            view = (parse_qs(parsed.query).get("view") or [""])[0].strip()
            self._send_json(200, {"cache": get_analytics_cache(view)})
            return
        if parsed.path == "/api/export-xlsx":
            # Build a multi-sheet workbook from every saved portfolio on disk.
            # See xlsx_export.py for the per-sheet contract — keep it in sync
            # with the Export-button comment in the topbar HTML when extending.
            try:
                import xlsx_export
            except ImportError as exc:
                self._send_json(
                    500,
                    {"error": f"openpyxl not installed: {exc}. Run: pip install openpyxl"},
                )
                return
            try:
                # Load every view with rows. list_views() returns metadata
                # only; we re-read each view to grab the rows payload.
                meta = list_views().get("views") or {}
                full: dict[str, dict] = {}
                for name in meta:
                    if not name or name == _CURRENT_KEY:
                        continue
                    full[name] = load_view(name)
                if not full:
                    self._send_json(404, {"error": "no saved portfolios to export"})
                    return
                blob = xlsx_export.build_workbook(
                    full,
                    analytics_runner=analyze_portfolios_multi,
                    period="1Y",
                    display_ccy="USD",
                )
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                fname = f"portfolio_tracker_export_{stamp}.xlsx"
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(blob)
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

        if parsed.path == "/api/column-views":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                raw = upsert_column_view(
                    str(payload.get("name") or ""),
                    payload.get("columns") or [],
                )
                self._send_json(200, {
                    "custom": raw.get("custom_views") or {},
                    "active": raw.get("active_view") or "Default",
                })
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/column-views/active":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                raw = set_active_column_view(str(payload.get("name") or ""))
                self._send_json(200, {"active": raw.get("active_view")})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
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

        if parsed.path == "/api/portfolio/rename":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                old = str(payload.get("old") or "").strip()
                new = str(payload.get("new") or "").strip()
                if not old or not new:
                    self._send_json(400, {"error": "old and new names required"})
                    return
                if old == new:
                    self._send_json(200, {"ok": True, "watchlists": load_watchlists()})
                    return
                # Rename watchlist first (it validates collisions); then view payload.
                wls = rename_watchlist(old, new)
                try:
                    rename_view(old, new)
                except ValueError:
                    # View-side collision: roll back the watchlist rename.
                    rename_watchlist(new, old)
                    raise
                self._send_json(200, {"ok": True, "watchlists": wls})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
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
                display_ccy = str(payload.get("display_ccy") or "USD").strip().upper()
                if not isinstance(rows, list) or not isinstance(weights, dict):
                    self._send_json(400, {"error": "rows[] and weights{} required"})
                    return
                result = analyze_portfolio(rows, weights, period, display_ccy=display_ccy)
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
                display_ccy = str(payload.get("display_ccy") or "USD").strip().upper()
                if not isinstance(rows, list) or not isinstance(weight_sets, dict) or not weight_sets:
                    self._send_json(400, {"error": "rows[] and weight_sets{name: weights} required"})
                    return
                results = analyze_portfolios_multi(rows, weight_sets, period, display_ccy=display_ccy)
                self._send_json(200, {"results": results} if "error" not in results else results)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/efficient-frontier":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                rows = payload.get("rows") or []
                if not isinstance(rows, list) or not rows:
                    self._send_json(400, {"error": "rows[] required"})
                    return
                result = compute_efficient_frontier(
                    rows,
                    lookback=str(payload.get("lookback") or "3Y"),
                    frequency=str(payload.get("frequency") or "weekly"),
                    display_ccy=str(payload.get("display_ccy") or "USD"),
                    rf=float(payload.get("rf") or 0.04),
                    budget=str(payload.get("budget") or "standard"),
                    current_weights=payload.get("current_weights") or {},
                    diversified=bool(payload.get("diversified") or False),
                )
                self._send_json(200, result)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/mpt-runs":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                view = str(payload.get("view") or "").strip()
                run = payload.get("run") or {}
                if not view or not isinstance(run, dict):
                    self._send_json(400, {"error": "view and run{} required"})
                    return
                saved = save_mpt_run(view, run)
                self._send_json(200, {"run": saved})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/weight-presets":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                view = str(payload.get("view") or "").strip()
                name = str(payload.get("name") or "").strip()
                weights = payload.get("weights") or {}
                rename_from = payload.get("rename_from")
                set_active = bool(payload.get("set_active", True))
                out = upsert_weight_preset(
                    view, name, weights,
                    rename_from=(str(rename_from).strip() if rename_from else None),
                    set_active=set_active,
                )
                self._send_json(200, out)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/weight-presets/active":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                view = str(payload.get("view") or "").strip()
                name = payload.get("name")
                out = set_active_weight_preset(view, (str(name) if name is not None else None))
                self._send_json(200, out)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/analytics-cache":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                view = str(payload.get("view") or "").strip()
                key = str(payload.get("key") or "").strip()
                body = payload.get("payload") or {}
                out = upsert_analytics_cache(view, key, body)
                self._send_json(200, {"cache": out})
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
        if parsed.path.startswith("/api/mpt-runs/"):
            from urllib.parse import parse_qs, unquote
            rid = unquote(parsed.path[len("/api/mpt-runs/"):])
            view = (parse_qs(parsed.query).get("view") or [""])[0].strip()
            try:
                delete_mpt_run(view, rid)
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/weight-presets":
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            view = (q.get("view") or [""])[0].strip()
            name = (q.get("name") or [""])[0].strip()
            try:
                out = delete_weight_preset(view, name)
                self._send_json(200, out)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/analytics-cache":
            from urllib.parse import parse_qs
            view = (parse_qs(parsed.query).get("view") or [""])[0].strip()
            try:
                clear_analytics_cache(view)
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path.startswith("/api/column-views/"):
            from urllib.parse import unquote
            name = unquote(parsed.path[len("/api/column-views/"):])
            try:
                raw = delete_column_view(name)
                self._send_json(200, {
                    "custom": raw.get("custom_views") or {},
                    "active": raw.get("active_view") or "Default",
                })
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
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
