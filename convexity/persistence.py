"""JSON file CRUD for views, watchlists, weight presets, MPT runs, column views, and analytics cache."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from convexity.helpers import _dedupe_rows_by_symbol, _json_default, _repo_root


def _atomic_write(path: Path, body: str) -> None:
    """Write via a temp file in the same directory + os.replace.

    Every state file here was a bare ``write_text``, and the readers catch
    ``JSONDecodeError`` and return ``{}`` — so a write interrupted midway makes
    EVERY SAVED PORTFOLIO SILENTLY DISAPPEAR rather than erroring. That was
    survivable while writes only ever happened right after an interactive
    build; a background refresh job writing views unattended, in a process
    whose quit path is ``os._exit(0)`` (server.shutdown_server), widens the
    window a lot. os.replace is atomic on the same filesystem, which is why
    the temp file must be created beside the target, not in /tmp.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# ----------------------------- File paths -----------------------------------

_WATCHLISTS_FILE = _repo_root() / ".convexity_watchlists.json"
_VIEWS_FILE = _repo_root() / ".convexity_views.json"
_LEGACY_SESSION_FILE = _repo_root() / ".convexity_session.json"
_MPT_FILE = _repo_root() / ".convexity_mpt.json"
_COLUMN_VIEWS_FILE = _repo_root() / ".convexity_column_views.json"

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
                _atomic_write(path, json.dumps(views, indent=2) + "\n")
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
    _atomic_write(_views_path(), body + "\n")


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
            # Counted the way load_view returns them, so a view saved with
            # repeated symbols before the dedupe doesn't report phantom rows.
            "row_count": len(_dedupe_rows_by_symbol(rows)) if isinstance(rows, list) else 0,
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
    if isinstance(entry.get("rows"), list):
        # Heals views written before save_view deduped (e.g. a 20-row, 16-name
        # "Data Center Builders"): the table, analytics and the Excel export all
        # read rows through here. The file itself is fixed on the next save.
        entry = {**entry, "rows": _dedupe_rows_by_symbol(entry["rows"])}
    return entry


def save_view(name: str, entries: str, rows: list, *, set_last: bool = True) -> dict:
    clean_name = (name or "").strip() or _CURRENT_KEY
    payload = {
        "entries": str(entries or "").strip(),
        # One row per symbol. The client can hand us repeats (build()'s
        # streaming append racing a refresh job's row patches), and this is the
        # single write path for rows, so it is where they stop — a persisted
        # repeat re-breaks analytics on every later load.
        "rows": _dedupe_rows_by_symbol(rows),
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
        _atomic_write(watchlists_path, payload + "\n")
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
    _atomic_write(Path(_MPT_FILE), body + "\n")


_MPT_MAX_RUNS = 3  # per-portfolio run history depth (user request)


def _as_run_list(val) -> list[dict]:
    """Normalize the on-disk per-view value to a newest-first list of run dicts.

    Tolerates the legacy single-dict format (pre-history) and any stray non-dict
    entries, so an old ``.convexity_mpt.json`` upgrades transparently.
    """
    if isinstance(val, list):
        return [r for r in val if isinstance(r, dict)]
    if isinstance(val, dict):
        return [val]
    return []


def get_mpt_runs(view_name: str) -> list[dict]:
    """The portfolio's saved Optimize runs, newest-first (≤ ``_MPT_MAX_RUNS``)."""
    with _MPT_LOCK:
        raw = _read_mpt_raw()
    return _as_run_list((raw.get("runs") or {}).get((view_name or "").strip()))


def get_last_mpt_run(view_name: str) -> dict | None:
    """The most-recent saved Optimize run for a portfolio, or None.

    Restored on Optimize-tab open. Tolerates both the current list format and the
    legacy single-dict format (returns the newest entry).
    """
    runs = get_mpt_runs(view_name)
    return runs[0] if runs else None


def save_mpt_run(view_name: str, run: dict) -> dict:
    """Push ``run`` onto the portfolio's history (newest-first, capped at 3).

    If the incoming run's ``params`` match the current newest entry's, the newest
    is *replaced* rather than duplicated (so re-running an identical config doesn't
    fill the list with near-copies). Overflow beyond ``_MPT_MAX_RUNS`` is dropped.
    """
    clean_view = (view_name or "").strip()
    if not clean_view:
        raise ValueError("view name required")
    rid = run.get("id") or ("run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
    payload = {**run, "id": rid, "saved_at": datetime.now(timezone.utc).isoformat()}
    with _MPT_LOCK:
        raw = _read_mpt_raw()
        runs_map = raw.get("runs") if isinstance(raw.get("runs"), dict) else {}
        history = _as_run_list(runs_map.get(clean_view))
        new_params = run.get("params")
        if history and new_params and history[0].get("params") == new_params:
            history[0] = payload           # dedupe identical params → replace newest
        else:
            history.insert(0, payload)
        runs_map[clean_view] = history[:_MPT_MAX_RUNS]
        raw["runs"] = runs_map
        _write_mpt_raw(raw)
    return payload


# ----------------------------- Column views (Pass D) ------------------------

_BUILTIN_COLUMN_VIEW_ALIASES = {
    "IB View": "Fundamentals",
    "Trader View": "Momentum",
}
_BUILTIN_COLUMN_VIEW_NAMES = {"Default", "Fundamentals", "Momentum"}
# Valid per-column color-coding ("heat") modes. Persisted per view (both
# built-in overrides and custom views carry their own map) so, e.g.,
# EV/EBITDA can be percentile-colored in Fundamentals but min-max elsewhere.
# "percentile" is retained only so legacy stored overrides still validate on
# read; the frontend migrates it to "quantile" and never writes it anew.
_HEAT_MODES = {"off", "on", "percentile", "quantile", "2c", "minmax"}


def _normalize_builtin_column_view_name(name: str) -> str:
    clean = (name or "Default").strip() or "Default"
    return _BUILTIN_COLUMN_VIEW_ALIASES.get(clean, clean)


def _sanitize_heat(heat) -> dict:
    """Keep only {column_key: valid_mode} pairs — defends the render path
    against a malformed payload writing a nonsense color mode."""
    if not isinstance(heat, dict):
        return {}
    out: dict[str, str] = {}
    for key, mode in heat.items():
        if isinstance(key, str) and isinstance(mode, str) and mode in _HEAT_MODES:
            out[key] = mode
    return out


def _column_views_path() -> Path:
    return Path(_COLUMN_VIEWS_FILE)


def _read_column_views_raw() -> dict:
    path = _column_views_path()
    empty = {"custom_views": {}, "builtin_overrides": {}, "active_view": "Default"}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
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
            "heat": _sanitize_heat(entry.get("heat")),
            "created_at": entry.get("created_at"),
        }
    # Built-in overrides: per-view deltas from the factory definition. columns
    # is present only when the user reordered/added/removed columns; heat holds
    # only the explicit color-mode overrides (never the factory defaults — so a
    # future factory change still propagates to any view the user hasn't frozen).
    bo_raw = data.get("builtin_overrides") if isinstance(data.get("builtin_overrides"), dict) else {}
    builtin_overrides: dict[str, dict] = {}
    for name, entry in bo_raw.items():
        clean_name = _normalize_builtin_column_view_name(str(name))
        if clean_name not in _BUILTIN_COLUMN_VIEW_NAMES or not isinstance(entry, dict):
            continue
        ov: dict = {}
        cols = entry.get("columns")
        if isinstance(cols, list) and cols:
            ov["columns"] = [str(c) for c in cols if isinstance(c, (str, int))]
        heat = _sanitize_heat(entry.get("heat"))
        if heat:
            ov["heat"] = heat
        # `acked` = the user pressed Save on the "Modified" pill: keep the edit,
        # stop flagging it. Only meaningful alongside a real delta, so it is read
        # last and never on its own — an entry carrying nothing but `acked` still
        # collapses to "no override", preserving the empty-entry-disappears rule.
        if ov and entry.get("acked"):
            ov["acked"] = True
        if ov:
            builtin_overrides[clean_name] = ov
    return {"custom_views": cleaned, "builtin_overrides": builtin_overrides, "active_view": active}


def _write_column_views_raw(raw: dict) -> None:
    body = json.dumps(raw, ensure_ascii=True, indent=2, sort_keys=True)
    _atomic_write(_column_views_path(), body + "\n")


def load_column_views() -> dict:
    with _COLUMN_VIEWS_LOCK:
        return _read_column_views_raw()


def upsert_column_view(name: str, columns: list, heat=None) -> dict:
    """Upsert a view definition. A built-in name (Default/Fundamentals/
    Momentum) is routed to the builtin_overrides store — the built-in views
    are now editable in place, not replaced — while any other name creates or
    updates a normal custom view. Both carry an optional per-view `heat` map."""
    clean_name = _normalize_builtin_column_view_name(name)
    if not (name or "").strip():
        raise ValueError("view name required")
    if not isinstance(columns, list) or not columns:
        raise ValueError("columns must be a non-empty list")
    clean_cols = [str(c) for c in columns if isinstance(c, (str, int))]
    clean_heat = _sanitize_heat(heat)
    with _COLUMN_VIEWS_LOCK:
        raw = _read_column_views_raw()
        if clean_name in _BUILTIN_COLUMN_VIEW_NAMES:
            # A fresh entry, so any prior `acked` is dropped: editing the columns
            # again after acknowledging must re-arm the "Modified" pill.
            ov: dict = {"columns": clean_cols}
            if clean_heat:
                ov["heat"] = clean_heat
            raw["builtin_overrides"][clean_name] = ov
        else:
            existing = raw["custom_views"].get(clean_name) or {}
            raw["custom_views"][clean_name] = {
                "columns": clean_cols,
                "heat": clean_heat,
                "created_at": existing.get("created_at") or datetime.now(timezone.utc).isoformat(),
            }
        _write_column_views_raw(raw)
        return raw


def set_builtin_view_heat(name: str, key: str, mode: str) -> dict:
    """Set one color-mode override on a built-in view without disturbing its
    columns (used by the modal's live color controls, which apply immediately
    even when the user hasn't reordered any columns)."""
    clean_name = _normalize_builtin_column_view_name(name)
    if clean_name not in _BUILTIN_COLUMN_VIEW_NAMES:
        raise ValueError(f"'{clean_name}' is not a built-in view")
    if not isinstance(key, str) or not key or mode not in _HEAT_MODES:
        raise ValueError("invalid heat key/mode")
    with _COLUMN_VIEWS_LOCK:
        raw = _read_column_views_raw()
        ov = raw["builtin_overrides"].get(clean_name) or {}
        heat = dict(ov.get("heat") or {})
        heat[key] = mode
        ov["heat"] = heat
        # Unlike upsert_column_view this mutates an existing entry, so `acked`
        # has to be cleared by hand — otherwise a colour change made after the
        # user pressed Save would silently skip the "Modified" pill.
        ov.pop("acked", None)
        raw["builtin_overrides"][clean_name] = ov
        _write_column_views_raw(raw)
        return raw


def set_builtin_view_acked(name: str, acked: bool = True) -> dict:
    """Mark a built-in's override as acknowledged (or un-acknowledge it).

    The built-in views are editable in place and every edit is already saved, so
    the "Modified" pill is purely informational: it says "this no longer matches
    the factory layout". Acking is the user answering "yes, that's deliberate" —
    the edit stays exactly as it is and only the flag goes away. Reverting to
    factory is still delete_column_view(), and Settings offers that per preset
    for as long as an override exists, acked or not.

    A no-op when the view has no override: there is nothing to acknowledge, and
    writing a lone `acked` would be dropped on the next read anyway.
    """
    clean_name = _normalize_builtin_column_view_name(name)
    if clean_name not in _BUILTIN_COLUMN_VIEW_NAMES:
        raise ValueError(f"'{clean_name}' is not a built-in view")
    with _COLUMN_VIEWS_LOCK:
        raw = _read_column_views_raw()
        ov = raw["builtin_overrides"].get(clean_name)
        if not ov:
            return raw
        if acked:
            ov["acked"] = True
        else:
            ov.pop("acked", None)
        raw["builtin_overrides"][clean_name] = ov
        _write_column_views_raw(raw)
        return raw


def delete_column_view(name: str) -> dict:
    """Delete a custom view, OR — for a built-in name — reset it to factory by
    dropping its override entry. Both are 'forget the stored thing for this
    name'; a built-in can never be truly removed, only reset."""
    clean_name = _normalize_builtin_column_view_name(name)
    with _COLUMN_VIEWS_LOCK:
        raw = _read_column_views_raw()
        if clean_name in _BUILTIN_COLUMN_VIEW_NAMES:
            raw["builtin_overrides"].pop(clean_name, None)
        else:
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
