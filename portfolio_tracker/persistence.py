"""JSON file CRUD for views, watchlists, weight presets, MPT runs, column views, and analytics cache."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from portfolio_tracker.helpers import _json_default, _repo_root

# ----------------------------- File paths -----------------------------------

_WATCHLISTS_FILE = _repo_root() / ".portfolio_tracker_watchlists.json"
_VIEWS_FILE = _repo_root() / ".portfolio_tracker_views.json"
_LEGACY_SESSION_FILE = _repo_root() / ".portfolio_tracker_session.json"
_MPT_FILE = _repo_root() / ".portfolio_tracker_mpt.json"
_COLUMN_VIEWS_FILE = _repo_root() / ".portfolio_tracker_column_views.json"

# ----------------------------- Locks ----------------------------------------

_VIEWS_LOCK = threading.Lock()
_WATCHLISTS_LOCK = threading.Lock()
_MPT_LOCK = threading.Lock()
_COLUMN_VIEWS_LOCK = threading.Lock()

_CURRENT_KEY = "__current__"


# ----------------------------- Views ----------------------------------------

def _watchlists_path() -> Path:
    return Path(_WATCHLISTS_FILE)


def _views_path() -> Path:
    return Path(_VIEWS_FILE)


def _read_views_raw() -> dict:
    path = _views_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data
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


def mark_view_stale(name: str, entries: str) -> bool:
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
    with _MPT_LOCK:
        raw_m = _read_mpt_raw()
        runs_map = raw_m.get("runs") if isinstance(raw_m.get("runs"), dict) else {}
        if clean_name in runs_map:
            runs_map.pop(clean_name, None)
            raw_m["runs"] = runs_map
            _write_mpt_raw(raw_m)


# ----------------------------- Weight presets (per-portfolio) ----------------

def _normalize_preset_weights(weights_in) -> dict[str, float]:
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
    entry = load_view(view_name) or {}
    presets = entry.get("weight_presets")
    if not isinstance(presets, list):
        presets = []
    return {"presets": presets, "active": entry.get("active_weight_preset")}


def upsert_weight_preset(view_name: str, preset_name: str, weights: dict,
                         *, rename_from: str | None = None,
                         set_active: bool = True) -> dict:
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

_ANALYTICS_CACHE_MAX_PER_VIEW = 8


def get_analytics_cache(view_name: str) -> dict:
    entry = load_view(view_name) or {}
    cache = entry.get("analytics_cache")
    return cache if isinstance(cache, dict) else {}


def upsert_analytics_cache(view_name: str, key: str, payload: dict) -> dict:
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


# ----------------------------- Watchlists -----------------------------------

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


# ----------------------------- MPT runs -------------------------------------

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

        def _keep(r: dict) -> bool:
            if not isinstance(r, dict):
                return False
            if r.get("id") == rid:
                return False
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


# ----------------------------- Column views (Pass D) ------------------------

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
