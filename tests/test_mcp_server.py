"""`convexity mcp` (mcp_server.py): the JSON-RPC protocol, discovery of the
app that owns this data folder, and the two write rules that keep a change in
the right place (a stale rev is retried once; a portfolio switch never is).

The app side is a stub HTTP server: the tools are thin clients of routes the
rest of the suite already covers."""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from convexity import mcp_server as m
from convexity import paths


class _Stub(BaseHTTPRequestHandler):
    routes: dict = {}
    posts: list = []
    data_dir = ""

    def log_message(self, *a):
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            return self._send(200, {"ok": True, "version": "t", "data_dir": self.data_dir})
        r = self.routes.get(("GET", path))
        return self._send(*(r() if callable(r) else r)) if r else self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).posts.append((self.path, body, self.headers.get("Content-Type")))
        r = self.routes.get(("POST", self.path))
        return self._send(*(r(body) if callable(r) else r)) if r else self._send(404, {})


@pytest.fixture
def app(monkeypatch):
    """A stub app answering for this test's data folder, found by discovery."""
    _Stub.routes, _Stub.posts = {}, []
    _Stub.data_dir = str(paths.data_dir())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(m, "PORTS", [srv.server_address[1]])
    monkeypatch.delenv("CONVEXITY_URL", raising=False)
    yield _Stub
    srv.shutdown()
    srv.server_close()


def _call(client, name, args=None):
    reply = m.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                      "params": {"name": name, "arguments": args or {}}}, client)  # fmt: skip
    res = reply["result"]
    text = res["content"][0]["text"]
    return res["isError"], (text if res["isError"] else json.loads(text))


# ------------------------------------------------------------------ protocol


def test_initialize_negotiates_the_version():
    c = m.Client()
    r = m.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26"}}, c)["result"]  # fmt: skip
    assert r["protocolVersion"] == "2025-03-26"
    assert r["capabilities"] == {"tools": {"listChanged": False}}
    assert r["serverInfo"]["name"] == "convexity"
    odd = m.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                    "params": {"protocolVersion": "1999-01-01"}}, c)["result"]  # fmt: skip
    assert odd["protocolVersion"] == m.PROTOCOL_VERSIONS[0]


def test_tools_list_schemas_are_well_formed():
    tools = m.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, m.Client())
    tools = tools["result"]["tools"]
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)) and len(names) >= 20
    for t in tools:
        s = t["inputSchema"]
        assert s["type"] == "object" and isinstance(s["properties"], dict)
        assert set(s.get("required", [])) <= set(s["properties"])
        assert t["description"] and "readOnlyHint" in t["annotations"]
    ann = {t["name"]: t["annotations"] for t in tools}
    assert ann["delete_portfolio"]["destructiveHint"] is True
    assert ann["list_books"]["readOnlyHint"] is True


def test_notifications_get_no_reply_and_unknowns_are_errors():
    c = m.Client()
    assert m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, c) is None
    r = m.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"}, c)
    assert r["error"]["code"] == -32601
    r = m.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "no_such_tool"}}, c)  # fmt: skip
    assert r["error"]["code"] == -32602


def test_stdout_carries_only_protocol_frames(monkeypatch):
    out = io.StringIO()
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        "not json",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "",
    ]
    monkeypatch.setattr("sys.stdout", io.StringIO())
    m.serve(io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert replies[1]["error"]["code"] == -32700
    assert len(replies) == 2


# ----------------------------------------------------------------- discovery


def test_finds_only_the_app_of_this_data_folder(app):
    base, _ = m.Client().find()
    assert base and base.startswith("http://127.0.0.1:")
    app.data_dir = "/somewhere/else"
    base, seen = m.Client().find()
    assert base is None and seen[0]["data_dir"] == "/somewhere/else"
    err, text = _call(m.Client(), "list_books")
    assert err and "start_app" in text


def test_status_when_nothing_runs(monkeypatch):
    monkeypatch.setattr(m, "PORTS", [])
    monkeypatch.delenv("CONVEXITY_URL", raising=False)
    err, out = _call(m.Client(), "status")
    assert not err and out["running"] is False and "start_app" in out["hint"]


def test_convexity_url_must_be_local(monkeypatch):
    monkeypatch.setenv("CONVEXITY_URL", "http://example.com:8765")
    err, text = _call(m.Client(), "status")
    assert err and "127.0.0.1" in text


def test_posts_send_json(app):
    app.routes[("GET", "/api/watchlists")] = (200, {"watchlists": {"Tech": "AAPL"}})
    app.routes[("POST", "/api/portfolio/rename")] = (200, {"ok": True})
    err, out = _call(m.Client(), "rename_portfolio", {"old": "Tech", "new": "Big Tech"})
    assert not err and out == {"renamed": "Tech", "to": "Big Tech"}
    path, body, ctype = app.posts[0]
    assert ctype == "application/json"  # the request-origin guard requires it
    assert body == {"old": "Tech", "new": "Big Tech"}


# ------------------------------------------------------------ My Investments


def _books(app, active="b1"):
    app.routes[("GET", "/api/investments/books")] = (200, {
        "enabled": True, "active": active,
        "books": [{"id": "main", "name": "Sam"}, {"id": "b1", "name": "Robin"}]})  # fmt: skip
    app.routes[("GET", "/api/prefs")] = (200, {"default_ccy": "EUR"})


def test_stale_rev_is_retried_once_with_a_fresh_page(app):
    _books(app)
    revs = iter([4, 5])
    app.routes[("GET", "/api/investments")] = lambda: (
        200, {"rev": next(revs), "book_id": "b1", "headline": {}, "positions": []})  # fmt: skip
    calls = []

    def apply(body):
        calls.append(body)
        if len(calls) == 1:
            return 409, {"error": "StaleRev", "message": "Your investments changed in another "
                         "window. Reload to see the latest."}  # fmt: skip
        return 200, {"rev": 6, "book_id": "b1", "headline": {"value": 1.0}, "positions": []}

    app.routes[("POST", "/api/investments/import/apply")] = apply
    entry = {"date": "2025-01-02", "type": "deposit", "amount": 100}
    err, out = _call(m.Client(), "add_entries", {"entries": [entry]})
    assert not err and out["rev"] == 6 and out["book"] == "Robin"
    assert [c["base_rev"] for c in calls] == [4, 5]
    assert all(c["book_id"] == "b1" and c["ccy"] == "EUR" for c in calls)


def test_a_portfolio_switch_is_never_retried(app):
    _books(app)
    app.routes[("GET", "/api/investments")] = (200, {"rev": 1, "book_id": "b1"})
    calls = []

    def apply(body):
        calls.append(body)
        return 409, {"error": "BookMoved", "message": "The portfolio showing changed in another "
                     "window, so nothing was saved."}  # fmt: skip

    app.routes[("POST", "/api/investments/import/apply")] = apply
    err, text = _call(m.Client(), "add_entries", {"entries": [{"date": "2025-01-02",
                                                               "type": "deposit"}]})  # fmt: skip
    assert err and "nothing was saved" in text and len(calls) == 1


def test_naming_another_book_switches_to_it_first(app):
    _books(app, active="main")

    def show(body):
        _books(app, active=body["id"])
        return 200, {}

    app.routes[("POST", "/api/investments/books/show")] = show
    app.routes[("GET", "/api/investments")] = (200, {"rev": 2, "book_id": "b1", "entries": [
        {"id": "e_1", "date": "2025-01-02", "type": "buy", "symbol": "AAPL", "qty": 1.0,
         "price": 100.0, "batch": "b_x"}]})  # fmt: skip
    err, out = _call(m.Client(), "list_entries", {"book": "robin"})
    assert not err and out["book"] == "Robin" and out["count"] == 1
    assert "batch" not in out["entries"][0]
    err, text = _call(m.Client(), "list_entries", {"book": "Nobody"})
    assert err and "Sam" in text


def test_cli_routes_mcp(monkeypatch):
    from convexity import cli

    seen = []
    monkeypatch.setattr(m, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["mcp"]) == 0 and seen == [[]]
