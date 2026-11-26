"""`convexity mcp`: a Model Context Protocol server, so Claude (Claude Code,
Claude Desktop, any MCP client) can read and drive the running app.

Design, and why:

- **A client of the app's own HTTP API, never a second writer.** Every tool is
  a call to the server already running on 127.0.0.1 (browser mode or the
  desktop window). The app keeps its locks, its rev checks, its journal and
  undo, and its request-origin guard (POSTs here send ``Content-Type:
  application/json`` and no ``Origin``, like any local non-browser client).
  Importing the modules and writing the JSON files from this process instead
  would race the app: its locks are in-process only.
- **It talks only to the app that owns this data folder.** Discovery probes
  127.0.0.1:8765-8780 and takes the server whose ``/api/health`` ``data_dir``
  equals ``paths.data_dir()`` here (so ``CONVEXITY_HOME`` picks a test server,
  and a dev server on 8766 is never mistaken for the owner's app).
  ``CONVEXITY_URL`` overrides it.
- **stdlib only.** MCP over stdio is newline-delimited JSON-RPC 2.0; the
  handful of methods a tools-only server answers (initialize, ping,
  tools/list, tools/call) fit in this file, so the install gains no
  dependency (the SDK brings ~15). stdout carries protocol frames and nothing
  else: ``sys.stdout`` is pointed at stderr for the process, so a stray
  ``print`` can never corrupt the stream.
- **Nothing extra is fetched or spent.** News and sentiment tools are
  cache-only, like the page; quota is spent only by ``refresh`` (the same job
  as the Refresh button) and ``search_companies`` (its AI step), on request.

Replies are trimmed (no chart series, no 252-close sparklines): a tool answer
lands in the model's context, so it carries what a reader needs and says how
to ask for more.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from convexity import __version__, paths

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
PORTS = range(8765, 8781)
_TIMEOUT = 180.0  # a cold My Investments page or analytics run takes ~30 s

INSTRUCTIONS = """\
Convexity is the owner's local portfolio dashboard. These tools drive the app
that is running on this computer through its own API, so every change shows
in the open window and can be undone there (My Investments has Undo).

Two kinds of portfolio:
- Portfolios page: watchlist-style baskets ("portfolios"): a name and a list of
  tickers, with quote rows, weightings and analytics. Tools: list_portfolios,
  get_portfolio, save_portfolio, portfolio_analytics, refresh.
- My Investments: the owner's REAL books, replayed from a ledger of dated
  entries (buy, sell, dividend, deposit, withdrawal, fee, split). Several
  books may exist (one per person); exactly one is showing, and every read
  and write acts on the showing one. Tools: list_books, show_book,
  create_book, book_overview, list_entries, add_entries, delete_entry,
  undo, redo.

If a tool says the app isn't running, call start_app. Prefer reading before
writing; never delete a portfolio or an entry the user did not ask to remove.
"""

# The row fields get_portfolio returns unless all_fields is set.
ROW_FIELDS = ("symbol", "name", "price", "currency", "pct_1d", "pct_1m", "pct_ytd", "pct_1y",
              "delta_ath", "market_cap", "sector", "quote_type", "pe_ratio", "dividend_yield",
              "analyst_rating", "target_upside_pct", "error")  # fmt: skip


# The holding fields book_overview returns (the page's own names).
POSITION_FIELDS = ("symbol", "name", "exchange", "shares", "price", "quote_ccy", "price_date",
                   "avg_price", "value", "weight", "month_pct", "return_pct", "profit_held",
                   "profit_sold", "dividends", "dividends_est", "profit", "profit_fx",
                   "note")  # fmt: skip


class AppError(Exception):
    """A refusal or failure worth showing the model as is."""


# --------------------------------------------------------------------- client


class Client:
    """The running app's HTTP API, found once and re-found after a failure."""

    def __init__(self):
        self.base: str | None = None

    @staticmethod
    def _health(base: str, timeout: float = 0.6) -> dict | None:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
            return body if isinstance(body, dict) and body.get("ok") else None
        except (OSError, ValueError):
            return None

    def find(self) -> tuple[str | None, list[dict]]:
        """(the base URL of this data folder's app or None, every app seen)."""
        env = os.environ.get("CONVEXITY_URL", "").strip().rstrip("/")
        if env:
            host = urllib.parse.urlparse(env).hostname
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise AppError("CONVEXITY_URL must point at this computer (127.0.0.1).")
            return (env if self._health(env) else None), []
        want = os.path.realpath(paths.data_dir())
        seen = []
        for port in PORTS:
            base = f"http://127.0.0.1:{port}"
            # A closed port refuses at once; only a listening one waits, and a
            # server busy booting (the first-run model download) answers slowly.
            h = self._health(base, 1.5)
            if h is None:
                continue
            seen.append({"port": port, "version": h.get("version"), "data_dir": h.get("data_dir")})
            if h.get("data_dir") and os.path.realpath(h["data_dir"]) == want:
                return base, seen
        return None, seen

    def url(self) -> str:
        if self.base and self._health(self.base, 1.0):
            return self.base
        self.base, seen = self.find()
        if self.base:
            return self.base
        other = f" (other Convexity servers found: {seen})" if seen else ""
        raise AppError(
            "Convexity isn't running for this data folder "
            f"({paths.data_dir()}){other}. Call start_app to start it."
        )

    def call(self, method: str, path: str, body=None, query: dict | None = None,
             timeout: float = _TIMEOUT):  # fmt: skip
        url = self.url() + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data, headers = None, {}
        if method == "POST":
            data = json.dumps(body if body is not None else {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                err = json.loads(text)
            except ValueError:
                err = {"error": text[:300]}
            raise AppError(_err_text(err, exc.code)) from None
        except OSError as exc:
            self.base = None
            raise AppError(f"The app didn't answer ({type(exc).__name__}).") from None
        return json.loads(text) if text.strip() else {}

    def stream(self, path: str, deadline: float):
        """NDJSON frames from ``path`` until the stream ends or ``deadline``."""
        req = urllib.request.Request(self.url() + path)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                for line in r:
                    if line.strip():
                        yield json.loads(line)
                    if time.time() > deadline:
                        return
        except (OSError, ValueError):
            return


def _err_text(err: dict, status: int) -> str:
    msg = err.get("message") or err.get("error") or "request failed"
    return f"{msg} (HTTP {status})"


# ------------------------------------------------------------------ helpers


def _round(x, nd=4):
    if isinstance(x, float):
        return round(x, nd)
    if isinstance(x, dict):
        return {k: _round(v, nd) for k, v in x.items()}
    if isinstance(x, list):
        return [_round(v, nd) for v in x]
    return x


def _drop(d: dict, *keys) -> dict:
    return {k: v for k, v in d.items() if k not in keys}


def _wait_job(c: Client, job_id: str, wait_s: float) -> dict:
    """Follow a refresh job's replayable stream until it ends or ``wait_s``.
    Counts are server-authoritative on every counted frame, so the last one
    seen is the job's state; a terminal frame names how it ended."""
    job, saved, problems = {"id": job_id, "state": "running"}, [], []
    for f in c.stream(f"/api/refresh-job/{job_id}/stream?since=0", time.time() + wait_s):
        kind = f.get("type")
        if kind == "hello" and isinstance(f.get("job"), dict):
            job = {k: f["job"].get(k) for k in ("id", "scope", "state", "phases", "views",
                                                  "counts")}  # fmt: skip
        if isinstance(f.get("counts"), dict):
            job["counts"] = f["counts"]
        if kind == "phase" and f.get("phase"):
            job["phase"] = f["phase"]
        if kind == "view_saved":
            saved.append(f.get("view"))
        elif kind == "phase" and f.get("saved") is False:
            problems.append({"view": f.get("view"), "not_saved": f.get("reason")})
        elif kind == "item" and f.get("outcome") == "failed":
            problems.append({"symbol": f.get("symbol"), "failed": f.get("phase")})
        elif kind == "done":
            job["state"] = "done"
        elif kind == "error":
            job["state"], job["error"] = "error", f.get("error") or f.get("message")
        elif kind == "cancelled" or (kind == "job" and f.get("state") == "cancelled"):
            job["state"] = "cancelled"
    out = {"job": job}
    if job["state"] == "running":
        out["hint"] = "Still running; job_status shows its progress."
    if saved:
        out["saved_views"] = saved
    if problems:
        out["problems"] = problems[:30]
    return out


def _default_ccy(c: Client) -> str | None:
    """The app's display currency (Settings), so a page here is the one the
    window shows and its cached computation is reused, not a second one."""
    try:
        return c.call("GET", "/api/prefs").get("default_ccy")
    except AppError:
        return None


def _book_page(c: Client, ccy=None, fresh=False) -> dict:
    ccy = ccy or _default_ccy(c)
    q = {"ccy": ccy} if ccy else {}
    if not fresh:
        page = c.call("GET", "/api/investments", query={**q, "cached": "1"})
        if not page.get("nocache"):
            return page
    return c.call("GET", "/api/investments", query=q)


def _summarise_book(page: dict, entries_too=False) -> dict:
    keep = {"book_id": page.get("book_id"), "rev": page.get("rev"), "ccy": page.get("ccy"),
            "base_ccy": page.get("base_ccy"), "computed_at": page.get("computed_at"),
            "stale": page.get("stale"), "empty": page.get("empty"),
            "warnings": page.get("warnings"), "splits_pending": page.get("splits_pending"),
            "undo_label": page.get("undo_label"), "redo_label": page.get("redo_label")}  # fmt: skip
    h = page.get("headline") or {}
    keep["headline"] = _drop(h, "series", "spark", "bench_series")
    keep["positions"] = [
        {k: p.get(k) for k in POSITION_FIELDS if k in p} for p in page.get("positions") or []
    ]
    keep["closed"] = page.get("closed")
    if entries_too:
        keep["entries"] = page.get("entries")
    return _round({k: v for k, v in keep.items() if v not in (None, [], {})})


def _showing_book(c: Client) -> dict:
    st = c.call("GET", "/api/investments/books")
    active = st.get("active") if st.get("enabled") else "main"
    row = next((b for b in st.get("books", []) if b.get("id") == active), {})
    return {"id": active, "name": row.get("name"), "enabled": st.get("enabled"),
            "books": st.get("books")}  # fmt: skip


def _pick_book(c: Client, book: str | None) -> dict:
    """Show ``book`` (an id or a name) if it isn't showing; return the showing book."""
    cur = _showing_book(c)
    if not book:
        return cur
    rows = cur["books"] or []
    hit = next((b for b in rows if b["id"] == book), None) or next(
        (b for b in rows if str(b.get("name", "")).casefold() == book.casefold()), None
    )
    if hit is None:
        raise AppError(f"No My Investments portfolio is called {book!r}. "
                       f"There are: {[b['name'] for b in rows]}.")  # fmt: skip
    if hit["id"] != cur["id"]:
        c.call("POST", "/api/investments/books/show", {"id": hit["id"]})
        cur = _showing_book(c)
        cur["switched"] = True
    return cur


def _book_write(c: Client, path: str, body: dict, ccy=None) -> dict:
    """One change of the showing book, with the rev and book id it needs; a
    stale rev (the window changed it meanwhile) is re-read and tried once more."""
    ccy = ccy or _default_ccy(c)
    for attempt in (1, 2):
        page = _book_page(c, ccy, fresh=attempt == 2)
        try:
            out = c.call("POST", path, {**body, "base_rev": page.get("rev"),
                                        "book_id": page.get("book_id"), "ccy": ccy})  # fmt: skip
            return _summarise_book(out)
        except AppError as exc:
            # Only a stale rev is retried. "The portfolio showing changed" is
            # never: a retry would land the change in another person's book.
            if attempt == 2 or "Your investments changed" not in str(exc):
                raise
    raise AppError("unreachable")


# -------------------------------------------------------------------- tools
# Each tool: name -> (handler, description, input schema, annotations).

TOOLS: dict[str, tuple] = {}


def tool(name: str, description: str, props: dict | None = None, required=(), *,
         read_only=True, destructive=False):  # fmt: skip
    def deco(fn):
        schema = {"type": "object", "properties": props or {}, "additionalProperties": False}
        if required:
            schema["required"] = list(required)
        ann = {"readOnlyHint": read_only, "openWorldHint": False}
        if not read_only:
            ann["destructiveHint"] = destructive
        TOOLS[name] = (fn, description, schema, ann)
        return fn

    return deco


S = {"type": "string"}
N = {"type": "number"}
B = {"type": "boolean"}
CCY = {"type": "string", "description": "Display currency, e.g. EUR or USD (default: the app's)."}
PERIOD = {"type": "string", "enum": ["1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y", "MAX"]}


@tool("status", "Is Convexity running for this data folder; its version, port, data "
      "folder, which API keys are set (booleans), and any refresh running.")  # fmt: skip
def t_status(c: Client, a: dict):
    base, seen = c.find()
    if not base:
        return {"running": False, "data_dir": str(paths.data_dir()), "other_servers": seen,
                "hint": "Call start_app to start it."}  # fmt: skip
    c.base = base
    h = c.call("GET", "/api/health")
    job = c.call("GET", "/api/refresh-job/current").get("job")
    return {"running": True, "url": base, "version": h.get("version"),
            "data_dir": h.get("data_dir"), "finnhub_key_set": h.get("finnhub_key_set"),
            "nvidia_key_set": h.get("nvidia_key_set"), "news_read_ok": h.get("llm_ok"),
            "refresh_running": job}  # fmt: skip


@tool("start_app", "Start Convexity if it isn't running: the desktop window (window: true, "
      "the default) or the background server only (window: false, open the printed URL in a "
      "browser). Waits until it answers.", {"window": B}, read_only=False)  # fmt: skip
def t_start_app(c: Client, a: dict):
    base, _ = c.find()
    if base:
        return {"already_running": True, "url": base}
    log = paths.logs_dir()
    log.mkdir(parents=True, exist_ok=True)
    mod = ["-c", "from convexity.desktop import main; main()"] if a.get("window", True) else [
        "-m", "convexity"]  # fmt: skip
    with open(log / "mcp-start.log", "ab") as fh:
        subprocess.Popen([sys.executable, *mod], stdin=subprocess.DEVNULL, stdout=fh,
                         stderr=fh, start_new_session=True, env=os.environ.copy())  # fmt: skip
    deadline = time.time() + 60
    while time.time() < deadline:
        base, _ = c.find()
        if base:
            c.base = base
            return {"started": True, "url": base, "window": a.get("window", True)}
        time.sleep(1.0)
    raise AppError(f"Convexity didn't answer within 60 s; see {log / 'mcp-start.log'}.")


@tool("logs", "The app's recent console lines (Settings -> Logs), newest last.",
      {"limit": {"type": "integer", "minimum": 1, "maximum": 400},
       "include_http": B})  # fmt: skip
def t_logs(c: Client, a: dict):
    out = c.call("GET", "/api/logs", query={"since": 0, "limit": 4000})
    lines = [x for x in out.get("lines", []) if a.get("include_http") or not x.get("http")]

    def when(ts):
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
        except (TypeError, ValueError):
            return ""

    return [f"{when(x.get('ts'))} {x.get('text', '')}" for x in lines[-int(a.get("limit", 80)) :]]


# ---------------------------------------------------------- Portfolios page


@tool("list_portfolios", "Every portfolio on the Portfolios page: its tickers (as typed), "
      "row count, when its quotes were saved, and which one opens on launch.")  # fmt: skip
def t_list_portfolios(c: Client, a: dict):
    views = c.call("GET", "/api/views")
    wl = c.call("GET", "/api/watchlists").get("watchlists", {})
    vm = views.get("views", {})
    out = []
    for name in dict.fromkeys([*wl, *vm]):
        if name.startswith("__"):
            continue
        v = vm.get(name, {})
        out.append({"name": name, "entries": wl.get(name) or v.get("entries"),
                    "rows": v.get("row_count"), "saved_at": v.get("saved_at"),
                    "stale": v.get("stale"), "whatif": bool(v.get("whatif"))})  # fmt: skip
    return {"portfolios": out, "opens_on_launch": views.get("last_view")}


@tool("get_portfolio", "One Portfolios-page portfolio's saved quote rows (price, returns, "
      "valuation, analysts). all_fields returns every column the app has.",
      {"name": S, "all_fields": B}, ["name"])  # fmt: skip
def t_get_portfolio(c: Client, a: dict):
    v = c.call("GET", "/api/views/" + urllib.parse.quote(a["name"], safe="")).get("view") or {}
    if not v:
        raise AppError(f"No portfolio called {a['name']!r}.")
    rows = v.get("rows") or []
    if not a.get("all_fields"):
        rows = [{k: r.get(k) for k in ROW_FIELDS if r.get(k) is not None} for r in rows]
    else:
        rows = [_drop(r, "sparkline", "rs_rank", "news_sentiment") for r in rows]
    return _round({"name": a["name"], "entries": v.get("entries"), "saved_at": v.get("saved_at"),
                   "rows": rows})  # fmt: skip


@tool("save_portfolio", "Create or replace a Portfolios-page portfolio: a name and its tickers "
      "or company names, comma-separated (\"AAPL, microsoft, SAP.DE\"). With refresh (the "
      "default) its quotes are fetched and saved, like Build Dashboard.",
      {"name": S, "entries": S, "refresh": B}, ["name", "entries"], read_only=False)  # fmt: skip
def t_save_portfolio(c: Client, a: dict):
    out = c.call("POST", "/api/watchlists", {"name": a["name"], "entries": a["entries"]})
    res = {"saved": a["name"], "entries_changed": out.get("entries_changed")}
    if a.get("refresh", True):
        res["refresh"] = _start_refresh(c, {"portfolio": a["name"], "phases": ["quotes"],
                                            "wait_seconds": 120})  # fmt: skip
    return res


@tool("rename_portfolio", "Rename a Portfolios-page portfolio.", {"old": S, "new": S},
      ["old", "new"], read_only=False)  # fmt: skip
def t_rename_portfolio(c: Client, a: dict):
    c.call("POST", "/api/portfolio/rename", {"old": a["old"], "new": a["new"]})
    return {"renamed": a["old"], "to": a["new"]}


@tool("delete_portfolio", "Delete a Portfolios-page portfolio (its tickers, saved rows and "
      "runs). Only when the user asked for this one to go; there is no undo.",
      {"name": S}, ["name"], read_only=False, destructive=True)  # fmt: skip
def t_delete_portfolio(c: Client, a: dict):
    if a["name"] not in c.call("GET", "/api/watchlists").get("watchlists", {}):
        raise AppError(f"No portfolio called {a['name']!r}.")
    c.call("DELETE", "/api/watchlists", query={"name": a["name"]})
    return {"deleted": a["name"]}


@tool("weight_presets", "A portfolio's saved weight sets (name -> weights), and the active one.",
      {"name": S}, ["name"])  # fmt: skip
def t_weight_presets(c: Client, a: dict):
    return _round(c.call("GET", "/api/weight-presets", query={"view": a["name"]}))


@tool("portfolio_analytics", "Risk and return of a Portfolios-page portfolio over a period: "
      "return, volatility, Sharpe/Sortino/Calmar, max drawdown, beta/R2/tracking error against "
      "the benchmarks, sector/country/currency exposure, concentration, contributions and the "
      "analysts' consensus. weighting: \"equal\" (default), a saved weight set's name, or "
      "weights {symbol: number}.",
      {"name": S, "period": PERIOD, "display_ccy": CCY,
       "weighting": {"anyOf": [S, {"type": "object", "additionalProperties": N}]}},
      ["name"])  # fmt: skip
def t_portfolio_analytics(c: Client, a: dict):
    v = c.call("GET", "/api/views/" + urllib.parse.quote(a["name"], safe="")).get("view") or {}
    rows = [r for r in v.get("rows") or [] if not r.get("error")]
    if not rows:
        raise AppError(f"{a['name']!r} has no saved quotes; call refresh for it first.")
    w = a.get("weighting") or "equal"
    label = w if isinstance(w, str) else "custom"
    if isinstance(w, str) and w != "equal":
        presets = c.call("GET", "/api/weight-presets", query={"view": a["name"]})
        hit = next((p for p in presets.get("presets", []) if p.get("name") == w), None)
        if hit is None:
            raise AppError(f"No weight set {w!r}; there are "
                           f"{[p.get('name') for p in presets.get('presets', [])]}.")  # fmt: skip
        w = hit.get("weights") or {}
    weights = {} if w == "equal" else w
    body = {"rows": rows, "weight_sets": {label: weights}, "period": a.get("period", "1Y")}
    if a.get("display_ccy"):
        body["display_ccy"] = a["display_ccy"]
    out = c.call("POST", "/api/portfolio-analytics-multi", body)
    res = (out.get("results") or out).get(label) or out
    if isinstance(res, dict) and res.get("error"):
        raise AppError(str(res["error"]))
    res = _drop(res, "series")
    for b in (res.get("benchmarks") or {}).values():
        if isinstance(b, dict):
            b.pop("series", None)
    an = res.get("analyst")
    if isinstance(an, dict):
        an["holdings"] = [_drop(h, "dist") for h in an.get("holdings") or []]
    return _round(res)


@tool("refresh", "Refresh quotes and/or news (the Refresh button): one portfolio (portfolio: "
      "its name), all of them (scope: all), or the showing My Investments book's prices and FX "
      "(my_investments: true). News reads spend the owner's Finnhub and NVIDIA quota. Waits up to wait_seconds "
      "(default 90), then reports progress; job_status follows it later.",
      {"portfolio": S, "scope": {"type": "string", "enum": ["current", "all"]},
       "my_investments": B, "phases": {"type": "array", "items": {"enum": ["quotes", "news"]}},
       "days": {"type": "integer", "enum": [3, 7, 14, 30]},
       "wait_seconds": {"type": "number", "minimum": 0, "maximum": 600}},
      read_only=False)  # fmt: skip
def t_refresh(c: Client, a: dict):
    return _start_refresh(c, a)


def _start_refresh(c: Client, a: dict) -> dict:
    if a.get("my_investments"):
        book = _showing_book(c)
        # My Investments' own refresh (jobs.BOOK_VIEW): its prices and FX only,
        # no news quota; "__book__" is the Portfolios tab's mirror, not this.
        body = {"view": "__investments__", "book_id": book["id"]}
    elif a.get("scope") == "all":
        body = {"scope": "all", "phases": a.get("phases") or ["quotes", "news"]}
    else:
        name = a.get("portfolio")
        wl = c.call("GET", "/api/watchlists").get("watchlists", {})
        if name not in wl:
            raise AppError(f"No portfolio called {name!r}; give portfolio, scope: all or "
                           "my_investments: true.")  # fmt: skip
        body = {"scope": "current", "view": name, "entries": wl[name],
                "phases": a.get("phases") or ["quotes", "news"]}  # fmt: skip
    if a.get("days"):
        body["days"] = a["days"]
    try:
        started = c.call("POST", "/api/refresh-job", body)
        job_id = started.get("job_id")
    except AppError as exc:
        if "already running" not in str(exc):
            raise
        snap = c.call("GET", "/api/refresh-job/current").get("job") or {}
        return {"already_running": snap, "hint": "Wait for it (job_status) and try again."}
    return _wait_job(c, job_id, float(a.get("wait_seconds", 90)))


@tool("job_status", "The refresh that is running now, if any (progress counts and phase).")
def t_job_status(c: Client, a: dict):
    return {"job": c.call("GET", "/api/refresh-job/current").get("job")}


@tool("cancel_refresh", "Cancel the running refresh.", read_only=False)
def t_cancel_refresh(c: Client, a: dict):
    job = c.call("GET", "/api/refresh-job/current").get("job")
    if not job:
        return {"cancelled": False, "reason": "nothing is running"}
    return c.call("POST", f"/api/refresh-job/{job['id']}/cancel", {})


# --------------------------------------------------------------- research


@tool("search_companies", "Company search, as in the app: free text (\"European banks with "
      "P/E under 10\", \"semiconductor ETFs\"). Uses the AI step (NVIDIA quota) when the rules "
      "can't read the query.",
      {"q": S, "limit": {"type": "integer", "minimum": 1, "maximum": 25},
       "offset": {"type": "integer", "minimum": 0}}, ["q"])  # fmt: skip
def t_search(c: Client, a: dict):
    out = c.call("POST", "/api/search", {"q": a["q"], "limit": a.get("limit", 10),
                                         "offset": a.get("offset", 0)})  # fmt: skip
    return _round(out)


@tool("lookup_symbol", "Find tickers by name or ticker in the symbol pack (\"siemens\", "
      "\"EQQB\"): the exact Yahoo tickers to use in portfolios and ledger entries.",
      {"q": S}, ["q"])  # fmt: skip
def t_lookup(c: Client, a: dict):
    return c.call("GET", "/api/investments/lookup", query={"q": a["q"]})


@tool(
    "symbol_detail",
    "One company's detail (the stock window's data): profile, valuation, "
    "fundamentals, analysts. Price history is left out.",
    {"symbol": S},
    ["symbol"],
)
def t_detail(c: Client, a: dict):
    d = c.call("GET", "/api/detail", query={"symbol": a["symbol"]})

    def trim(x):
        # Price and volume histories are thousands of points: named, not sent.
        if isinstance(x, dict):
            return {k: trim(v) for k, v in x.items()}
        if isinstance(x, list):
            return [trim(v) for v in x] if len(x) <= 24 else f"({len(x)} points omitted)"
        return x

    return _round(trim(d))


@tool("news_sentiment", "The cached News read and Market read per ticker (no new reads: run "
      "refresh with phases [news] for that).",
      {"symbols": {"type": "array", "items": S}, "days": {"type": "integer",
                                                         "enum": [3, 7, 14, 30]}},
      ["symbols"])  # fmt: skip
def t_news(c: Client, a: dict):
    q = {"symbols": ",".join(a["symbols"]), "days": a.get("days")}
    return _round(c.call("GET", "/api/news-sentiment", query=q))


# ------------------------------------------------------------ My Investments


@tool("list_books", "The My Investments portfolios (one per person, e.g. \"Sam\"), which one "
      "is showing, and their size.")  # fmt: skip
def t_list_books(c: Client, a: dict):
    return c.call("GET", "/api/investments/books", query={"facts": "1"})


@tool("show_book", "Switch My Investments to another portfolio (by name or id); every other "
      "My Investments tool then acts on it.", {"book": S}, ["book"], read_only=False)  # fmt: skip
def t_show_book(c: Client, a: dict):
    cur = _pick_book(c, a["book"])
    return {"showing": cur["name"], "id": cur["id"], "switched": cur.get("switched", False)}


@tool("create_book", "Create a new, empty My Investments portfolio and show it (turns on "
      "Multiple portfolios if it is off). Then add its history with add_entries.",
      {"name": {"type": "string", "maxLength": 20}}, ["name"], read_only=False)  # fmt: skip
def t_create_book(c: Client, a: dict):
    st = c.call("GET", "/api/investments/books")
    if not st.get("enabled"):
        c.call("POST", "/api/investments/books/enabled", {"on": True})
    c.call("POST", "/api/investments/books/add", {"name": a["name"]})
    cur = _showing_book(c)
    return {"created": cur["name"], "id": cur["id"], "showing": True}


@tool("rename_book", "Rename a My Investments portfolio.", {"book": S, "name": S},
      ["book", "name"], read_only=False)  # fmt: skip
def t_rename_book(c: Client, a: dict):
    rows = _showing_book(c)["books"] or []
    hit = next((b for b in rows if a["book"] in (b["id"], b["name"])), None)
    if hit is None:
        raise AppError(f"No My Investments portfolio {a['book']!r}.")
    c.call("POST", "/api/investments/books/rename", {"id": hit["id"], "name": a["name"]})
    return {"renamed": hit["name"], "to": a["name"]}


@tool("book_overview", "A My Investments portfolio as the page shows it: value, cash, profit, "
      "dividends, TWR/MWR per period vs the benchmark, and every holding (shares, price, value, "
      "weight, average cost, profit). book: a name or id (switches to it); default the showing "
      "one. fresh: recompute now instead of the last computed page.",
      {"book": S, "ccy": CCY, "fresh": B})  # fmt: skip
def t_book_overview(c: Client, a: dict):
    cur = _pick_book(c, a.get("book"))
    out = _summarise_book(_book_page(c, a.get("ccy"), fresh=bool(a.get("fresh"))))
    return {"book": cur["name"], **({"switched": True} if cur.get("switched") else {}), **out}


@tool(
    "list_entries",
    "The ledger of a My Investments portfolio (newest first): each entry's "
    "id, date, type, symbol, shares, price, amount, fee, currency and note.",
    {"book": S, "symbol": S, "limit": {"type": "integer", "minimum": 1, "maximum": 500}},
)
def t_list_entries(c: Client, a: dict):
    cur = _pick_book(c, a.get("book"))
    page = _book_page(c)
    rows = page.get("entries") or []
    if a.get("symbol"):
        rows = [e for e in rows if e.get("symbol") == a["symbol"].upper()]
    keep = ("id", "date", "type", "symbol", "qty", "price", "amount", "fee", "tax", "ccy",
            "fx_rate", "note")  # fmt: skip
    rows = [{k: e[k] for k in keep if e.get(k) not in (None, "")} for e in rows]
    return {"book": cur["name"], "rev": page.get("rev"), "count": len(rows),
            "entries": rows[: int(a.get("limit", 200))]}  # fmt: skip


ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "description": "YYYY-MM-DD, not in the future"},
        "type": {"enum": ["buy", "sell", "dividend", "deposit", "withdrawal", "fee", "split"]},
        "symbol": {
            "type": "string",
            "description": "Yahoo ticker (lookup_symbol); for buy, sell, dividend, split",
        },  # fmt: skip
        "qty": {"type": "number", "description": "shares (buy, sell)"},
        "price": {"type": "number", "description": "price per share in ccy (buy, sell)"},
        "fee": {"type": "number"},
        "amount": {"type": "number", "description": "money (dividend, deposit, withdrawal, fee)"},
        "tax": {"type": "number", "description": "withholding tax on a dividend"},
        "ratio": {"type": "number", "description": "split ratio, 10 for 10-for-1"},
        "ccy": {
            "type": "string",
            "description": "currency of price/amount (default: the "
            "listing's, or the book's base for cash)",
        },  # fmt: skip
        "fx_rate": {"type": "number", "description": "ccy -> base rate the broker used"},
        "qty_basis": {
            "enum": ["trade", "current"],
            "description": "trade (default): shares as "
            "on the contract note, later splits applied; current: today's shares",
        },
        "opening": {
            "type": "boolean",
            "description": "a buy that records a holding already "
            "owned: performance starts at its value that day",
        },  # fmt: skip
        "note": {"type": "string"},
    },
    "required": ["date", "type"],
}


@tool("add_entries", "Add ledger entries to the showing My Investments portfolio, in one step "
      "that one Undo takes back. The ledger is replayed first: an oversell or an unknown ticker "
      "is refused and nothing is written. Money never goes below zero: what a buy needs beyond "
      "the cash is booked as an implied deposit.",
      {"entries": {"type": "array", "items": ENTRY_SCHEMA, "minItems": 1, "maxItems": 500},
       "book": S}, ["entries"], read_only=False)  # fmt: skip
def t_add_entries(c: Client, a: dict):
    cur = _pick_book(c, a.get("book"))
    rows = [{**e, "source": "import"} for e in a["entries"]]
    out = _book_write(c, "/api/investments/import/apply", {"entries": rows})
    return {"book": cur["name"], "added": len(rows), **out}


@tool("delete_entry", "Delete one ledger entry (by id, from list_entries) from the showing "
      "My Investments portfolio. Undo brings it back.", {"id": S, "book": S}, ["id"],
      read_only=False, destructive=True)  # fmt: skip
def t_delete_entry(c: Client, a: dict):
    cur = _pick_book(c, a.get("book"))
    return {"book": cur["name"], **_book_write(c, "/api/investments/entries/delete",
                                               {"id": a["id"]})}  # fmt: skip


@tool("undo", "Undo the last change to the showing My Investments portfolio.", {"book": S},
      read_only=False)  # fmt: skip
def t_undo(c: Client, a: dict):
    cur = _pick_book(c, a.get("book"))
    return {"book": cur["name"], **_book_write(c, "/api/investments/undo", {})}


@tool("redo", "Redo the last undone change to the showing My Investments portfolio.",
      {"book": S}, read_only=False)  # fmt: skip
def t_redo(c: Client, a: dict):
    cur = _pick_book(c, a.get("book"))
    return {"book": cur["name"], **_book_write(c, "/api/investments/redo", {})}


# ------------------------------------------------------------------ protocol


def _result(text: str, is_error=False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle(msg: dict, client: Client) -> dict | None:
    """One JSON-RPC message in, its response out (None for a notification)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None  # notifications/initialized, notifications/cancelled, ...

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok(
            {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "convexity", "title": "Convexity", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": d, "inputSchema": s, "annotations": an}
                             for n, (_, d, s, an) in TOOLS.items()]})  # fmt: skip
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {name!r}"}}  # fmt: skip
        fn = TOOLS[name][0]
        try:
            out = fn(client, params.get("arguments") or {})
            return ok(_result(json.dumps(out, ensure_ascii=False, default=str)))
        except AppError as exc:
            return ok(_result(str(exc), True))
        except Exception as exc:  # a bug here must not take the session down
            print(f"[mcp] {name} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return ok(_result(f"{name} failed ({type(exc).__name__}: {exc}).", True))
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}  # fmt: skip


def serve(stdin=None, stdout=None) -> int:
    """The stdio loop. Protocol frames go to the real stdout only."""
    out = stdout or sys.stdout
    sys.stdout = sys.stderr  # any print from here on lands on stderr
    inp = stdin or sys.stdin
    client = Client()
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "parse error"}}  # fmt: skip
        else:
            msgs = msg if isinstance(msg, list) else [msg]
            replies = [r for m in msgs if isinstance(m, dict) for r in [handle(m, client)] if r]
            reply = (replies if isinstance(msg, list) else replies[0]) if replies else None
        if reply is not None:
            out.write(json.dumps(reply, ensure_ascii=False) + "\n")
            out.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv or [])
    if args and args[0] in ("-h", "--help"):
        print(
            "usage: convexity mcp\n\n"
            "Model Context Protocol server over stdio, for Claude Code / Claude Desktop.\n"
            "Register it with:  claude mcp add --scope user convexity -- convexity mcp\n"
            f"It drives the Convexity app running for {paths.data_dir()}."
        )
        return 0
    return serve()
