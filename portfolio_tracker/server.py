"""HTTP server — routes, static file serving, and main() entrypoint."""

from __future__ import annotations

import json
import logging
import math
import mimetypes
import socket
import threading
import warnings
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
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

from portfolio_tracker.analytics import (
    _bulk_close,
    analyze_portfolio,
    analyze_portfolios_multi,
)
from portfolio_tracker.fetcher import fetch_detail, fetch_one, fetch_portfolio
from portfolio_tracker.frontier import (
    _risk_free_history,
    compute_efficient_frontier,
)
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
    delete_mpt_run,
    delete_view,
    delete_watchlist,
    delete_weight_preset,
    get_analytics_cache,
    list_mpt_runs,
    list_views,
    list_weight_presets,
    load_column_views,
    load_mpt_run,
    load_view,
    load_watchlists,
    rename_view,
    rename_watchlist,
    save_mpt_run,
    save_view,
    set_active_column_view,
    set_active_weight_preset,
    set_last_view,
    upsert_analytics_cache,
    upsert_column_view,
    upsert_watchlist,
    upsert_weight_preset,
)
from portfolio_tracker.resolver import _ordered_resolve

_STATIC_DIR = Path(__file__).parent / "static"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
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
        if parsed.path == "/api/mpt-runs":
            q = parse_qs(parsed.query)
            view = (q.get("view") or [""])[0].strip()
            self._send_json(200, {"runs": list_mpt_runs(view)})
            return
        if parsed.path == "/api/risk-free-history":
            q = parse_qs(parsed.query)
            ccy = (q.get("ccy") or ["USD"])[0].strip() or "USD"
            lb = (q.get("lookback") or ["3Y"])[0].strip() or "3Y"
            try:
                self._send_json(200, _risk_free_history(ccy, lb))
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path.startswith("/api/mpt-runs/"):
            rid = unquote(parsed.path[len("/api/mpt-runs/"):])
            view = (parse_qs(parsed.query).get("view") or [""])[0].strip()
            run = load_mpt_run(view, rid)
            if not run:
                self._send_json(404, {"error": "run not found"})
                return
            self._send_json(200, {"run": run})
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

        if parsed.path == "/api/news-refresh":
            if _ns is None:
                self._send_json(503, {"error": "news_sentiment module not available"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                symbols = [str(s).strip().upper() for s in (payload.get("symbols") or []) if s]
                result = _ns.refresh_sentiment(symbols)
                self._send_json(200, result)
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
                                    "type": "row", "row": row,
                                    "done": done, "total": total,
                                }))
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
            name = unquote(parsed.path[len("/api/views/"):])
            try:
                delete_view(name)
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if parsed.path.startswith("/api/mpt-runs/"):
            rid = unquote(parsed.path[len("/api/mpt-runs/"):])
            view = (parse_qs(parsed.query).get("view") or [""])[0].strip()
            try:
                delete_mpt_run(view, rid)
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


def main() -> None:
    port = _pick_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}/"
    print("=" * 60)
    print(f"  Portfolio Tracker running at {url}")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    threading.Timer(0.8, lambda: subprocess.run(
        ["open", "-a", "Google Chrome", url], check=False
    )).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
