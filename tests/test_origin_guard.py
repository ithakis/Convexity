"""The request-origin guard at the top of Handler.do_GET/do_POST/do_DELETE.

Any web page open in the user's browser can send requests to 127.0.0.1. A
text/plain POST is a CORS "simple request" (no preflight), and a DNS-rebinding
page is same-origin with us, so without the guard such a page could rename or
delete saved portfolios, start refresh jobs that spend API quota, or read the
user's holdings. See CLAUDE.md §18. test_keys.py covers the key routes.
"""

import http.server
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from convexity import persistence, server

APP_JS = Path(server.__file__).parent / "static" / "app.js"

# Every state-changing route. The guard runs before dispatch, so bodies need
# not be valid for the 403 cases.
POST_PATHS = [
    "/api/watchlists",
    "/api/quotes-stream",
    "/api/column-views",
    "/api/column-views/builtin-heat",
    "/api/column-views/builtin-ack",
    "/api/column-views/active",
    "/api/views/Book",
    "/api/portfolio/rename",
    "/api/last-view",
    "/api/portfolio-analytics",
    "/api/portfolio-analytics-multi",
    "/api/efficient-frontier",
    "/api/mpt-runs",
    "/api/weight-presets",
    "/api/weight-presets/active",
    "/api/analytics-cache",
    "/api/refresh-job",
    "/api/refresh-job/rj_x/cancel",
    "/api/news-rescore",
    "/api/model-download",
    "/api/reference-pack",
    "/api/keys",
    "/api/keys/test",
    "/api/no-such-route",
]
DELETE_PATHS = [
    "/api/views/Book",
    "/api/weight-presets?view=Book&name=p",
    "/api/analytics-cache?view=Book",
    "/api/column-views/Mine",
    "/api/watchlists?name=Book",
    "/api/no-such-route",
]

EVIL_POST_HEADERS = [
    {"Content-Type": "text/plain"},  # simple request, no preflight
    {"Content-Type": "application/x-www-form-urlencoded"},  # an HTML form
    {"Content-Type": "multipart/form-data; boundary=x"},
    {},  # no content type at all
    {"Content-Type": "application/json", "Origin": "https://evil.example"},
    {"Content-Type": "application/json", "Origin": "null"},  # sandboxed iframe / file://
    {"Content-Type": "application/json", "Origin": "http://127.0.0.1:1"},  # another local app
    {"Content-Type": "application/json", "Host": "evil.example"},  # DNS rebinding
    {"Content-Type": "application/json", "Host": "evil.example:8765"},
]
EVIL_DELETE_HEADERS = [
    {"Origin": "https://evil.example"},
    {"Origin": "http://127.0.0.1:1"},
    {"Host": "evil.example"},
]


@pytest.fixture(scope="module")
def srv():
    # One server for the module: shutdown() waits out a poll interval, which
    # at the 0.5 s default cost two minutes over ~250 parametrised cases.
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _req(base, method, path, headers=None, body=None):
    req = urllib.request.Request(base + path, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.mark.parametrize("headers", EVIL_POST_HEADERS)
@pytest.mark.parametrize("path", POST_PATHS)
def test_cross_origin_post_refused(srv, path, headers):
    code, body = _req(srv, "POST", path, headers, b'{"name": "x", "entries": "AAPL"}')
    assert code == 403, (path, headers, code)
    assert json.loads(body) == {"error": "cross-origin request refused"}


@pytest.mark.parametrize("headers", EVIL_DELETE_HEADERS)
@pytest.mark.parametrize("path", DELETE_PATHS)
def test_cross_origin_delete_refused(srv, path, headers):
    assert _req(srv, "DELETE", path, headers)[0] == 403


@pytest.mark.parametrize(
    "path",
    ["/", "/static/app.js", "/api/watchlists", "/api/views", "/api/views/Book", "/api/health"],
)
def test_rebinding_get_refused(srv, path):
    """A rebound page is same-origin, so without the Host check it could read
    the user's holdings from /api/watchlists and /api/views."""
    assert _req(srv, "GET", path, {"Host": "evil.example:8765"})[0] == 403


def test_nothing_changes_on_refused_requests(srv):
    persistence.upsert_watchlist("Book", "AAPL, MSFT")
    same = {"Content-Type": "application/json", "Origin": srv}
    # A foreign page tries to overwrite, rename and delete it.
    _req(
        srv,
        "POST",
        "/api/watchlists",
        {"Content-Type": "text/plain"},
        b'{"name": "Book", "entries": "XXX"}',
    )
    _req(
        srv,
        "POST",
        "/api/portfolio/rename",
        {**same, "Origin": "https://evil.example"},
        b'{"old": "Book", "new": "Gone"}',
    )
    _req(srv, "DELETE", "/api/watchlists?name=Book", {"Host": "evil.example"})
    wl = persistence.load_watchlists()
    assert wl.get("Book") == "AAPL, MSFT" and "Gone" not in wl
    persistence.delete_watchlist("Book")


def test_same_origin_requests_still_work(srv):
    """What the app's own page sends: JSON, Origin = this server, loopback
    Host (127.0.0.1 or localhost)."""
    same = {"Content-Type": "application/json", "Origin": srv}
    code, _ = _req(
        srv,
        "POST",
        "/api/watchlists",
        same,
        json.dumps({"name": "Book", "entries": "AAPL"}).encode(),
    )
    assert code == 200
    code, _ = _req(
        srv,
        "POST",
        "/api/portfolio/rename",
        same,
        json.dumps({"old": "Book", "new": "Book2"}).encode(),
    )
    assert code == 200
    assert "Book2" in persistence.load_watchlists()
    port = srv.rsplit(":", 1)[1]
    code, _ = _req(
        srv,
        "POST",
        "/api/last-view",
        {
            "Content-Type": "application/json; charset=utf-8",
            "Host": f"localhost:{port}",
            "Origin": f"http://localhost:{port}",
        },
        b'{"name": "Book2"}',
    )
    assert code == 200
    assert _req(srv, "DELETE", "/api/watchlists?name=Book2", {"Origin": srv})[0] == 200
    assert "Book2" not in persistence.load_watchlists()
    # Non-browser local clients (curl, the desktop readiness probe) send no Origin.
    assert _req(srv, "GET", "/api/health")[0] == 200
    assert (
        _req(
            srv,
            "POST",
            "/api/refresh-job/rj_none/cancel",
            {"Content-Type": "application/json"},
            b"{}",
        )[0]
        == 404
    )


def test_every_frontend_post_sends_json():
    """The guard 403s any POST that is not application/json, so every POST the
    page makes must declare it — and nothing may use sendBeacon, which cannot."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "sendBeacon" not in src and "XMLHttpRequest" not in src
    posts = [m.start() for m in re.finditer(r'method:\s*"POST"', src)]
    assert len(posts) >= 25  # sanity: the regex still finds them
    missing = []
    for pos in posts:
        call = src.rfind("fetch(", 0, pos)
        window = src[call : pos + 400]
        end = window.find("})")  # close of the options object
        opts = window[: end if end > 0 else len(window)]
        if "application/json" not in opts:
            missing.append(src.count("\n", 0, pos) + 1)
    assert not missing, f"app.js POSTs without Content-Type application/json at lines {missing}"


@pytest.mark.parametrize("body", [b"[1, 2]", b"null", b'"x"', b"{bad", b"\xff\xfe"])
@pytest.mark.parametrize("path", POST_PATHS)
def test_malformed_body_is_a_400_on_every_route(srv, path, body):
    """do_POST parses the body before dispatch: inside a route, its own
    `except Exception` turned a non-object body into a 500 with a logged
    traceback. Fixed text only, so nothing from the request is echoed."""
    code, resp = _req(srv, "POST", path, {"Content-Type": "application/json"}, body)
    assert code == 400, (path, body, code)
    assert json.loads(resp)["error"] in (
        "request body is not valid JSON",
        "request body must be a JSON object",
    )


def test_oversized_body_is_refused_unread(srv):
    import http.client

    c = http.client.HTTPConnection("127.0.0.1", int(srv.rsplit(":", 1)[1]), timeout=5)
    c.putrequest("POST", "/api/watchlists")
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(server._MAX_BODY_BYTES + 1))
    c.endheaders()  # no body follows: an unbounded read would hang here
    r = c.getresponse()
    assert r.status == 400 and json.loads(r.read()) == {"error": "request body too large"}
    c.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"phases": [], "entries": "AAPL"},
        {"phases": ["bogus"], "entries": "AAPL"},
        {"phases": ["quotes"], "entries": "", "view": 5},
        {"phases": ["quotes"], "entries": "", "view": {"a": 1}, "days": "abc"},
        {"phases": ["quotes"], "entries": ["AAPL"]},
    ],
)
def test_refresh_job_rejects_bad_fields_instead_of_crashing(srv, payload):
    """Wrong types used to raise inside the handler: the connection dropped
    with no answer at all."""
    code, _ = _req(
        srv,
        "POST",
        "/api/refresh-job",
        {"Content-Type": "application/json"},
        json.dumps(payload).encode(),
    )
    assert code == 400, (payload, code)


def test_lookback_clamp_survives_infinity():
    from convexity import news_sentiment as ns

    assert ns._clamp_lookback(float("inf")) == ns._DEFAULT_LOOKBACK_DAYS
    assert ns._clamp_lookback(float("nan")) == ns._DEFAULT_LOOKBACK_DAYS
    assert (ns._clamp_lookback(1e300), ns._clamp_lookback(-4)) == (30, 1)
