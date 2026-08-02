"""HTTP server — routes, static file serving, and main() entrypoint."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import socket
import sys
import threading
import time
import warnings
import subprocess
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# Suppress noisy warnings BEFORE importing yfinance / pandas.
warnings.filterwarnings("ignore")
try:
    from pandas.errors import Pandas4Warning  # type: ignore
    warnings.simplefilter("ignore", Pandas4Warning)
except Exception:
    pass
warnings.simplefilter("ignore", DeprecationWarning)
warnings.simplefilter("ignore", FutureWarning)

_orig_showwarning = warnings.showwarning

def _showwarning_filter(message, category, filename, lineno, file=None, line=None):
    if "site-packages" in str(filename):
        return
    _orig_showwarning(message, category, filename, lineno, file, line)

warnings.showwarning = _showwarning_filter
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

from portfolio_tracker import __version__, __version_date__, __version_display__
from portfolio_tracker import envcheck, logbuf
from portfolio_tracker.analytics import (
    analyze_portfolio,
    analyze_portfolios_multi,
)
from portfolio_tracker import jobs
from portfolio_tracker.fetcher import fetch_detail, fetch_portfolio, range_history
from portfolio_tracker.fetcher import stream_quotes as fetcher_stream_quotes
# NB: portfolio_tracker.frontier (and its numba/mpt dependency, ~1.7s to
# import) is imported lazily at its two call sites below — the MPT/Optimize
# feature is on-demand, so keeping it off the module-load path shaves that
# cost off desktop-app startup. See the _configure_chromium note in desktop.py.
from portfolio_tracker.fx import fx_index_history, fx_rates
from portfolio_tracker.helpers import SUPPORTED_FX, _json_default, _safe_json

try:
    from portfolio_tracker import news_sentiment as _ns
except ImportError:
    _ns = None
from portfolio_tracker.persistence import (
    _CURRENT_KEY,
    clear_analytics_cache,
    delete_column_view,
    delete_view,
    delete_watchlist,
    delete_weight_preset,
    get_analytics_cache,
    get_mpt_runs,
    list_views,
    list_weight_presets,
    load_column_views,
    load_view,
    load_watchlists,
    rename_view,
    rename_watchlist,
    save_mpt_run,
    save_view,
    set_active_column_view,
    set_builtin_view_acked,
    set_builtin_view_heat,
    set_active_weight_preset,
    set_last_view,
    upsert_analytics_cache,
    upsert_column_view,
    upsert_watchlist,
    upsert_weight_preset,
)

_STATIC_DIR = Path(__file__).parent / "static"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        msg = format % args
        if "/api/" in msg or msg.startswith('"GET / '):
            print(f"[{self.log_date_time_string()}] {msg}")

    def _read_json(self) -> dict:
        """Read + JSON-parse the request body, returning ``{}`` for an empty
        body. Collapses the Content-Length read + ``json.loads(... or b"{}")``
        that every POST branch repeated verbatim. Malformed JSON raises
        json.JSONDecodeError (a ValueError subclass) exactly as the inline code
        did, so each caller's existing except-clauses map it to the same
        400/500 response — external behavior is unchanged."""
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _begin_ndjson(self) -> None:
        """Headers shared by every NDJSON streaming route."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    # ----------------------------- refresh jobs -----------------------------

    def _handle_refresh_job_create(self) -> None:
        try:
            payload = self._read_json()
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})
            return
        scope = "all" if payload.get("scope") == "all" else "current"
        phases = payload.get("phases") or ["quotes", "news"]
        days = int(payload.get("days") or 7)
        on_conflict = "supersede" if payload.get("on_conflict") == "supersede" else "reject"
        context = payload.get("context") if isinstance(payload.get("context"), dict) else None

        if scope == "all":
            # load_watchlists() IS the set of saved portfolios. Ordered
            # most-recently-saved first so the user's likely-current tab lands
            # first; the client-supplied view/entries/context are ignored.
            watch = load_watchlists()
            meta = (list_views() or {}).get("views") or {}
            names = sorted(watch.keys(),
                           key=lambda n: (meta.get(n) or {}).get("saved_at") or "",
                           reverse=True)
            entries_by_view = {n: watch[n] for n in names}
            context = None
        else:
            view = (payload.get("view") or "").strip() or "__current__"
            entries_by_view = {view: str(payload.get("entries") or "")}
        if not any(v.strip() for v in entries_by_view.values()):
            self._send_json(400, {"error": "nothing to refresh"})
            return

        job, outcome = jobs.submit(scope=scope, phases=phases, days=days,
                                   entries_by_view=entries_by_view, context=context,
                                   on_conflict=on_conflict)
        if outcome == "rejected":
            # 409 + the running job's id: the client attaches to it rather than
            # starting a second one. See the single-flight note in jobs.py.
            self._send_json(409, {"error": "a refresh is already running",
                                  "job_id": job.id, "state": job.state,
                                  "snapshot": job.snapshot()})
            return
        self._send_json(202, {"job_id": job.id, "state": job.state, "outcome": outcome})

    def _handle_refresh_job_stream(self, job_id: str, since: int) -> None:
        job = jobs.get(job_id)
        if job is None:
            self._send_json(404, {"error": "unknown job"})
            return
        self._begin_ndjson()
        replay, dropped = job.events_since(since)
        try:
            # Connection frames carry seq 0 and are never replayed; the client
            # must only advance its cursor on nonzero seqs.
            self.wfile.write(_safe_json({
                "type": "hello", "seq": 0, "job": job.snapshot(),
                "dropped": dropped, "replay_from": since,
                "last_seq": job.snapshot()["last_seq"],
            }))
            for frame in replay:
                self.wfile.write(_safe_json(frame))
                since = max(since, frame["seq"])
            self.wfile.flush()
            terminal_seen = any(f["type"] in ("done", "error") or
                                (f["type"] == "job" and f.get("state") in ("cancelled",))
                                for f in replay)
            while not terminal_seen:
                frames = job.wait(since, timeout=15.0)
                if not frames:
                    self.wfile.write(_safe_json({"type": "ping", "seq": 0}))
                    self.wfile.flush()
                    if job.is_terminal():
                        break
                    continue
                for frame in frames:
                    self.wfile.write(_safe_json(frame))
                    since = max(since, frame["seq"])
                    if frame["type"] in ("done", "error") or (
                            frame["type"] == "job" and frame.get("state") == "cancelled"):
                        terminal_seen = True
                self.wfile.flush()
            self.wfile.write(_safe_json({"type": "end", "seq": 0}))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # A dead client is NOT a cancel — the job keeps running and the
            # client reattaches at its last seq. (Contrast /api/news-refresh,
            # which silenced writes but kept burning API quota unattended.)
            return

    def _serve_static(self, rel_path: str) -> bool:
        fpath = (_STATIC_DIR / rel_path).resolve()
        if not fpath.is_relative_to(_STATIC_DIR.resolve()):
            return False
        if not fpath.is_file():
            return False
        body = fpath.read_bytes()
        ct = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        if ct in ("text/css", "application/javascript", "text/javascript"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            fpath = _STATIC_DIR / "index.html"
            body = fpath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path.startswith("/static/"):
            rel = parsed.path[len("/static/"):]
            if self._serve_static(rel):
                return
            self.send_response(404)
            self.end_headers()
            return
        if parsed.path == "/api/health":
            # env_ok is find_spec-only (see envcheck._importable) — no imports,
            # no I/O — so it is safe on this hot, every-page-load route. It is
            # what drives the "a required package is missing" banner; the
            # expensive detail lives on /api/runtime-status.
            self._send_json(200, {
                "ok": True, "ts": datetime.now(timezone.utc).isoformat(),
                "version": __version__, "version_date": __version_date__,
                "env_ok": envcheck.status()["ok"],
            })
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
                "builtin_overrides": raw.get("builtin_overrides") or {},
                "active": raw.get("active_view") or "Default",
            })
            return
        if parsed.path.startswith("/api/views/"):
            name = unquote(parsed.path[len("/api/views/"):])
            view = load_view(name)
            # Backfill news_sentiment on row payloads from the rehydrated
            # in-memory cache. Views saved before sentiment was fetched
            # carry news_sentiment: None on disk; pulling from the disk-
            # backed news cache lets the NS column render on launch
            # without forcing the user to open the News tab first.
            if _ns is not None and isinstance(view, dict):
                rows = view.get("rows")
                if isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        if row.get("news_sentiment"):
                            continue
                        sym = str(row.get("symbol") or "").strip().upper()
                        if not sym:
                            continue
                        cached = _ns.get_cached_sentiment(sym)
                        if cached:
                            row["news_sentiment"] = cached
            self._send_json(200, {"view": view, "name": name})
            return
        if parsed.path == "/api/fx-rates":
            q = parse_qs(parsed.query)
            base = (q.get("base") or ["USD"])[0].strip().upper() or "USD"
            try:
                self._send_json(200, fx_rates(base))
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/fx-index":
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
            q = parse_qs(parsed.query)
            period = (q.get("period") or ["1y"])[0].strip() or "1y"
            ccys = [c.strip().upper() for c in (q.get("ccys") or [",".join(SUPPORTED_FX)])[0].split(",") if c.strip()]
            ccys = [c for c in ccys if c in SUPPORTED_FX]
            results: dict[str, list] = {}
            for c in ccys:
                try:
                    results[c] = fx_index_history(c, period) or []
                except Exception:
                    results[c] = []
            self._send_json(200, {"period": period, "indexes": results})
            return
        if parsed.path == "/api/detail":
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
        if parsed.path == "/api/history":
            # Intraday half of the chart granularity ladder (1M/3M/6M). Kept off
            # /api/detail on purpose: the modal must open instantly on the daily
            # payload, and this is fetched lazily only when the user picks one of
            # those tabs. `bench` is comma-separated because the client already
            # knows which overlays it's showing and which sector ETF applies —
            # asking it saves a Ticker.info round-trip here.
            q = parse_qs(parsed.query)
            sym = (q.get("symbol") or [""])[0].strip().upper()
            rng = (q.get("range") or [""])[0].strip().upper()
            bench = [b for b in (q.get("bench") or [""])[0].split(",") if b.strip()]
            if not sym or not rng:
                self._send_json(400, {"error": "symbol and range required"})
                return
            try:
                self._send_json(200, range_history(sym, rng, bench))
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/refresh-job/current":
            # One cheap call on page load answers "is something running that I
            # should reattach to?" — which is what lets a job survive a full
            # browser reload, not just a tab switch.
            j = jobs.current()
            self._send_json(200, {"job": j.snapshot() if j else None})
            return
        if (parsed.path.startswith("/api/refresh-job/")
                and parsed.path.endswith("/stream")):
            job_id = parsed.path[len("/api/refresh-job/"):-len("/stream")]
            q = parse_qs(parsed.query)
            try:
                since = int((q.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            self._handle_refresh_job_stream(job_id, since)
            return
        if parsed.path == "/api/mpt-runs":
            # Last-3 run history per portfolio: `last` (newest, restored on open)
            # plus `runs` (the ≤3 list rendered under Apply to Portfolio).
            q = parse_qs(parsed.query)
            view = (q.get("view") or [""])[0].strip()
            runs = get_mpt_runs(view)
            self._send_json(200, {"last": (runs[0] if runs else None), "runs": runs})
            return
        if parsed.path == "/api/risk-free-history":
            q = parse_qs(parsed.query)
            ccy = (q.get("ccy") or ["USD"])[0].strip() or "USD"
            lb = (q.get("lookback") or ["3Y"])[0].strip() or "3Y"
            try:
                from portfolio_tracker.frontier import _risk_free_history
                self._send_json(200, _risk_free_history(ccy, lb))
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/weight-presets":
            view = (parse_qs(parsed.query).get("view") or [""])[0].strip()
            self._send_json(200, list_weight_presets(view))
            return
        if parsed.path == "/api/analytics-cache":
            view = (parse_qs(parsed.query).get("view") or [""])[0].strip()
            self._send_json(200, {"cache": get_analytics_cache(view)})
            return
        if parsed.path == "/api/news-sentiment":
            if _ns is None:
                self._send_json(503, {"error": "news_sentiment module not available"})
                return
            q = parse_qs(parsed.query)
            symbols = [s.strip().upper() for s in (q.get("symbols") or [""])[0].split(",") if s.strip()]
            if not symbols:
                self._send_json(400, {"error": "symbols required"})
                return
            try:
                result = _ns.get_portfolio_sentiment(symbols)
                self._send_json(200, {"sentiment": result, "status": _ns.status()})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/news-market":
            if _ns is None:
                self._send_json(503, {"error": "news_sentiment module not available"})
                return
            try:
                result = _ns.get_market_sentiment()
                articles = _ns.fetch_market_news()
                self._send_json(200, {
                    "sentiment": result,
                    "articles": articles,
                    "status": _ns.status(),
                })
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/news-tape":
            if _ns is None:
                self._send_json(503, {"error": "news_sentiment module not available"})
                return
            q = parse_qs(parsed.query)
            symbols = [s.strip().upper() for s in (q.get("symbols") or [""])[0].split(",") if s.strip()]
            try:
                # Cache-only read — safe to call on every panel open.
                self._send_json(200, {"articles": _ns.get_cached_articles(symbols)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/logs":
            # Backend console tail for Settings -> Logs. Poll-based: the client
            # passes the last seq it rendered and gets only what is newer, so
            # an open panel costs one small response every ~1.5s and nothing at
            # all when closed.
            qs = parse_qs(parsed.query)
            try:
                since = int((qs.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            try:
                limit = max(1, min(4000, int((qs.get("limit") or ["1000"])[0])))
            except ValueError:
                limit = 1000
            self._send_json(200, logbuf.read(since=since, limit=limit))
            return
        if parsed.path == "/api/runtime-status":
            # Cheap counterpart to /api/news-diagnostics: "is the ML model
            # running, and if not, why" plus the dependency self-check — with no
            # history load and no price fetch. Settings -> Models & Data calls
            # this on open; the News tab's heavy diagnostics panel keeps using
            # compute_diagnostics().
            #
            # Deliberately NOT folded into /api/health: runtime_status() calls
            # ml_sentiment._load(), which imports lightgbm/scipy/sklearn, reads a
            # 2**18-float32 idf vector and constructs a Booster. /api/health runs
            # on every page load, so that cost belongs on an opt-in route where
            # it is paid once and then cached process-wide by _STATE.
            try:
                from portfolio_tracker import ml_sentiment as _ml
                ml = _ml.runtime_status()
            except Exception as exc:
                # Same shape as compute_diagnostics()'s except-branch so the
                # frontend renders one thing regardless of which route served it.
                ml = {"available": False, "reason": f"{type(exc).__name__}: {exc}",
                      "model_dir": "", "model_dir_exists": False, "version": ""}
            keys = {}
            if _ns is not None:
                try:
                    st = _ns.status()
                    # Booleans only — never serve key material to the frontend.
                    keys = {
                        "finnhub_key_set": bool(st.get("finnhub_key_set")),
                        "nvidia_key_set": bool(st.get("nvidia_key_set")),
                        "llm_model": getattr(_ns, "_MODEL", ""),
                        "lexicon_available": bool(st.get("lexicon_available")),
                    }
                except Exception:
                    keys = {}
            self._send_json(200, {
                "ml": ml,
                "env": envcheck.status(),
                "keys": keys,
                "version": __version__,
                "version_date": __version_date__,
            })
            return
        if parsed.path == "/api/news-diagnostics":
            if _ns is None:
                self._send_json(503, {"error": "news_sentiment module not available"})
                return
            try:
                self._send_json(200, _ns.compute_diagnostics())
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/news-articles":
            if _ns is None:
                self._send_json(503, {"error": "news_sentiment module not available"})
                return
            q = parse_qs(parsed.query)
            sym = (q.get("symbol") or [""])[0].strip().upper()
            if not sym:
                self._send_json(400, {"error": "symbol required"})
                return
            try:
                sentiment = _ns.get_news_sentiment(sym)
                articles = _ns.fetch_company_news(sym)
                self._send_json(200, {"symbol": sym, "articles": articles, "sentiment": sentiment})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/export-xlsx":
            try:
                from portfolio_tracker import xlsx_export
            except ImportError as exc:
                self._send_json(500, {"error": f"openpyxl not installed: {exc}. Run: pip install openpyxl"})
                return
            try:
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
                    full, analytics_runner=analyze_portfolios_multi,
                    period="1Y", display_ccy="USD",
                )
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                fname = f"portfolio_tracker_export_{stamp}.xlsx"
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
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
            try:
                payload = self._read_json()
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
            try:
                payload = self._read_json()
                entries = payload.get("entries") or []
                rows = fetch_portfolio([str(e) for e in entries])
                self._send_json(200, {"rows": rows})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/column-views":
            try:
                payload = self._read_json()
                raw = upsert_column_view(
                    str(payload.get("name") or ""),
                    payload.get("columns") or [],
                    payload.get("heat"),
                )
                self._send_json(200, {
                    "custom": raw.get("custom_views") or {},
                    "builtin_overrides": raw.get("builtin_overrides") or {},
                    "active": raw.get("active_view") or "Default",
                })
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/column-views/builtin-heat":
            # Live per-column color-mode toggle on a built-in view. Kept
            # separate from the full upsert above so a heat-only change never
            # freezes the view's columns to a factory-equal override.
            try:
                payload = self._read_json()
                raw = set_builtin_view_heat(
                    str(payload.get("name") or ""),
                    str(payload.get("key") or ""),
                    str(payload.get("mode") or ""),
                )
                self._send_json(200, {
                    "custom": raw.get("custom_views") or {},
                    "builtin_overrides": raw.get("builtin_overrides") or {},
                    "active": raw.get("active_view") or "Default",
                })
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/column-views/builtin-ack":
            # "Modified · Save" on the column bar. The edit is already saved (it
            # was saved the moment it was made); this only records that the user
            # meant it, so the pill stops nagging. Reverting to factory stays
            # DELETE /api/column-views/<name>.
            try:
                payload = self._read_json()
                raw = set_builtin_view_acked(
                    str(payload.get("name") or ""),
                    bool(payload.get("acked", True)),
                )
                self._send_json(200, {
                    "custom": raw.get("custom_views") or {},
                    "builtin_overrides": raw.get("builtin_overrides") or {},
                    "active": raw.get("active_view") or "Default",
                })
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/column-views/active":
            try:
                payload = self._read_json()
                raw = set_active_column_view(str(payload.get("name") or ""))
                self._send_json(200, {"active": raw.get("active_view")})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path.startswith("/api/views/"):
            name = unquote(parsed.path[len("/api/views/"):])
            try:
                payload = self._read_json()
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
            try:
                payload = self._read_json()
                old = str(payload.get("old") or "").strip()
                new = str(payload.get("new") or "").strip()
                if not old or not new:
                    self._send_json(400, {"error": "old and new names required"})
                    return
                if old == new:
                    self._send_json(200, {"ok": True, "watchlists": load_watchlists()})
                    return
                wls = rename_watchlist(old, new)
                try:
                    rename_view(old, new)
                except ValueError:
                    rename_watchlist(new, old)
                    raise
                self._send_json(200, {"ok": True, "watchlists": wls})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/last-view":
            try:
                payload = self._read_json()
                set_last_view(str(payload.get("name") or ""))
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/portfolio-analytics":
            try:
                payload = self._read_json()
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
            try:
                payload = self._read_json()
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
            # NDJSON stream: {type:"progress"|"done"|"error"} messages. The client
            # renders a real pct/ETA bar and can cancel (AbortController) — when it
            # disconnects, the next write() raises and we close the generator, which
            # stops the 8-core bootstrap/cloud compute at the next chunk boundary.
            try:
                payload = self._read_json()
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
                return
            rows = payload.get("rows") or []
            if not isinstance(rows, list) or not rows:
                self._send_json(400, {"error": "rows[] required"})
                return

            def _fnum(key, default):
                try:
                    v = payload.get(key)
                    return float(v) if v is not None else default
                except (TypeError, ValueError):
                    return default

            # Accept real JSON booleans AND the string forms "false"/"0" some
            # clients send (bool("false") is truthy, which would wrongly force
            # fully-invested).
            _fi = payload.get("fully_invested", True)
            fully_invested = str(_fi).strip().lower() not in ("false", "0", "no", "")
            _bounds = payload.get("bounds")
            bounds = _bounds if isinstance(_bounds, dict) else None
            budget = str(payload.get("budget") or payload.get("cloud_budget") or "standard")

            from portfolio_tracker.frontier import compute_efficient_frontier_stream

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            gen = compute_efficient_frontier_stream(
                rows,
                lookback=str(payload.get("lookback") or "3Y"),
                display_ccy=str(payload.get("display_ccy") or "USD"),
                rf=_fnum("rf", 0.04), alpha=_fnum("alpha", 0.95),
                fully_invested=fully_invested, bounds=bounds,
                w_min=_fnum("w_min", 0.0), w_max=_fnum("w_max", 1.0),
                cov_model=str(payload.get("cov_model") or "ledoit"),
                haircut=_fnum("haircut", 0.25), budget=budget,
                current_weights=payload.get("current_weights") or {},
            )
            try:
                for msg in gen:
                    self.wfile.write(_safe_json(msg))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                gen.close()  # client cancelled — stop pulling ⇒ compute halts
            except Exception as exc:
                try:
                    self.wfile.write(_safe_json({"type": "error", "error": str(exc)}))
                    self.wfile.flush()
                except Exception:
                    pass
            return

        if parsed.path == "/api/mpt-runs":
            try:
                payload = self._read_json()
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
            try:
                payload = self._read_json()
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
            try:
                payload = self._read_json()
                view = str(payload.get("view") or "").strip()
                name = payload.get("name")
                out = set_active_weight_preset(view, (str(name) if name is not None else None))
                self._send_json(200, out)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/analytics-cache":
            try:
                payload = self._read_json()
                view = str(payload.get("view") or "").strip()
                key = str(payload.get("key") or "").strip()
                body = payload.get("payload") or {}
                out = upsert_analytics_cache(view, key, body)
                self._send_json(200, {"cache": out})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/news-refresh":
            if _ns is None:
                self._send_json(503, {"error": "news_sentiment module not available"})
                return
            try:
                payload = self._read_json()
                symbols = [str(s).strip().upper() for s in (payload.get("symbols") or []) if s]
                context = payload.get("context") if isinstance(payload.get("context"), dict) else None
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
                return
            # NDJSON staged stream (mirrors /api/quotes-stream): market lands
            # first, then constituents by weight — the panel fills as results
            # arrive instead of blocking for the whole refresh.
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            write_lock = threading.Lock()
            aborted = threading.Event()

            def _emit(kind: str, body: dict) -> None:
                if aborted.is_set():
                    return
                try:
                    with write_lock:
                        self.wfile.write(_safe_json({"type": kind, **body}))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    aborted.set()

            try:
                _emit("start", {"total": len(symbols)})
                result = _ns.refresh_sentiment(symbols, context=context,
                                               progress_cb=_emit)
                _emit("done", result)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                _emit("error", {"error": str(exc)})
            return

        if parsed.path == "/api/quotes-stream":
            try:
                payload = self._read_json()
                entries = payload.get("entries") or []
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
                return

            self._begin_ndjson()
            # Body extracted to fetcher.stream_quotes so this route, the legacy
            # blocking path and the refresh job all run one implementation.
            # gen.close() on disconnect is the pattern /api/efficient-frontier
            # already uses: it raises GeneratorExit inside stream_quotes, whose
            # finally de-queues every pending symbol. The old inline version's
            # `return` left a `with ThreadPoolExecutor` block, which JOINED all
            # in-flight futures — so an abandoned 150-symbol build kept hitting
            # Yahoo for another ~30 s.
            gen = fetcher_stream_quotes([str(e) for e in entries])
            try:
                for msg in gen:
                    self.wfile.write(_safe_json(msg))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                gen.close()
            return

        if parsed.path == "/api/refresh-job":
            self._handle_refresh_job_create()
            return

        if parsed.path.startswith("/api/refresh-job/") and parsed.path.endswith("/cancel"):
            job_id = parsed.path[len("/api/refresh-job/"):-len("/cancel")]
            if jobs.cancel(job_id):
                self._send_json(200, {"ok": True, "state": "cancelled"})
            elif jobs.get(job_id) is None:
                self._send_json(404, {"error": "unknown job"})
            else:
                self._send_json(200, {"ok": False, "state": jobs.get(job_id).state})
            return

        if parsed.path == "/api/news-rescore":
            # Deliberately NOT a job: cache-only, sub-50 ms, no network and no
            # LLM. Changing the News window must repaint immediately.
            if _ns is None:
                self._send_json(503, {"error": "news_sentiment module not available"})
                return
            try:
                payload = self._read_json()
                symbols = [str(s).strip().upper() for s in (payload.get("symbols") or []) if s]
                days = int(payload.get("days") or 7)
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
                return
            try:
                self._send_json(200, _ns.rescore_window(symbols, days))
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        self.send_response(404)
        self.end_headers()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/views/"):
            name = unquote(parsed.path[len("/api/views/"):])
            try:
                delete_view(name)
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path == "/api/weight-presets":
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
            view = (parse_qs(parsed.query).get("view") or [""])[0].strip()
            try:
                clear_analytics_cache(view)
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path.startswith("/api/column-views/"):
            name = unquote(parsed.path[len("/api/column-views/"):])
            try:
                raw = delete_column_view(name)
                self._send_json(200, {
                    "custom": raw.get("custom_views") or {},
                    "builtin_overrides": raw.get("builtin_overrides") or {},
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


def start_server() -> tuple[ThreadingHTTPServer, int]:
    """Bind the dashboard server on a free loopback port and begin serving on
    a daemon background thread. Returns (server, port). Callers should stop
    it via shutdown_server() — shared by browser-mode main() and the desktop
    app (portfolio_tracker/desktop.py), which both need the same port-pick +
    construction but manage their own lifecycle.
    """
    # Tee stdout/stderr into the in-memory ring FIRST, so the Settings -> Logs
    # console captures startup output too (the ml_sentiment load line in
    # particular). Idempotent; harmless in browser mode where a terminal
    # already exists.
    logbuf.install()
    # Dependency self-check, immediately after the tee so the report is visible
    # in Settings -> Logs as well as the terminal. This is the guardrail for the
    # v1.10.0 failure: environment.yml declared lightgbm/scikit-learn but the
    # installed `pt` env was never re-solved, so the ML model was dead in the
    # shipped app with no signal beyond one easily-missed line. Now a stale env
    # announces itself at boot and via env_ok on /api/health (which the frontend
    # turns into a banner). find_spec-only, so this costs no measurable time.
    _missing = envcheck.check()
    if _missing:
        print(envcheck.format_report(_missing), file=sys.stderr)

    def _warm_ml() -> None:
        # Load the ML sentiment artifact off the request path. The load pulls in
        # lightgbm + scipy + sklearn and takes ~3 s; doing it lazily meant the
        # first news refresh raced against it. Warming here (rather than in
        # main()) is deliberate: the desktop app calls start_server() straight
        # from its boot worker and never runs main(), which is exactly why
        # main()'s _warm_optimizer has never warmed numba for the desktop app.
        # Best-effort — _load() never raises, and a failure just leaves the
        # model unavailable with a real reason on /api/runtime-status.
        try:
            from portfolio_tracker import ml_sentiment as _mls
            _mls.available()
        except Exception:
            pass

    threading.Thread(target=_warm_ml, name="pt-warm-ml", daemon=True).start()
    port = _pick_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    # Per-connection handler threads (socketserver.ThreadingMixIn) default to
    # non-daemon, which blocks process exit on a stuck/long-lived connection
    # (e.g. an open NDJSON stream) even after shutdown()+server_close(). Mark
    # them daemon so a graceful quit can never hang the process.
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="pt-httpd", daemon=True).start()
    return server, port


def shutdown_server(server: ThreadingHTTPServer) -> None:
    """Stop the server and force-terminate the process immediately.

    fetch_one/analytics/news_sentiment all use module-level
    concurrent.futures.ThreadPoolExecutor pools for yfinance/NIM calls;
    ThreadPoolExecutor registers an atexit hook that joins any in-flight work
    before a normal interpreter shutdown can complete, which can stall
    process exit for as long as the slowest pending network call takes. The
    existing browser-mode launcher (Launch Dashboard.command) already avoids
    this by killing the process outright (SIGTERM's default disposition is
    an unclean, instant stop — no different from SIGKILL here, since nothing
    catches it); os._exit() gives the same guarantee from inside the process
    itself so a desktop-app quit can never hang. The OS reclaims the socket
    and threads either way.
    """
    # Cancel background refresh jobs and give an in-flight save_view a moment
    # to land. Persistence writes are atomic, but os._exit(0) below would
    # otherwise abandon one mid-write and leave an orphan temp file beside it.
    try:
        jobs.shutdown(1.5)
    except Exception:
        pass
    server.shutdown()
    server.server_close()
    sys.stdout.flush()
    os._exit(0)


def main() -> None:
    server, port = start_server()
    url = f"http://localhost:{port}/"
    print("=" * 60)
    print(f"  Portfolio _App v{__version_display__} running at {url}")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    def _open_browser() -> None:
        if sys.platform == "darwin":
            subprocess.run(["open", "-a", "Google Chrome", url], check=False)
        elif sys.platform == "win32":
            os.startfile(url)  # noqa: S606 - local dashboard URL, not user input
        else:
            webbrowser.open(url)

    def _warm_optimizer() -> None:
        # Import mpt off the startup path so the first Optimize click never pays
        # the one-time numba JIT compile (~7 s) — it warms in the background here
        # while the user reads their dashboard. Best-effort; failure is harmless.
        try:
            import importlib
            importlib.import_module("portfolio_tracker.mpt")  # import triggers _warm_jit()
        except Exception:
            pass

    threading.Timer(0.8, _open_browser).start()
    threading.Thread(target=_warm_optimizer, name="pt-warm-mpt", daemon=True).start()
    try:
        # serve_forever() now runs on a background thread (see start_server);
        # block the main thread on an interruptible sleep so Ctrl+C still
        # works the same as when serve_forever() ran here directly.
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nShutting down...")
        shutdown_server(server)  # os._exit(0) — deliberate fast/clean exit
    except Exception:
        # An unexpected exception (not a normal Ctrl+C) should still surface
        # its traceback and a non-zero exit code — os._exit(0) would swallow
        # both, silently masking a crash from anything checking the exit
        # status. Close the server without force-killing the interpreter.
        server.shutdown()
        server.server_close()
        raise
