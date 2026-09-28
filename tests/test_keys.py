"""Settings -> API keys (src/convexity/keys.py, POST/GET /api/keys).

The contract under test: a key value is written to config.json (0600, merged,
atomic) and sent to the provider in a header — and appears nowhere else. No
test touches the real network: the Test buttons are pointed at a local stub.
"""

import http.server
import io
import json
import os
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from convexity import finnhub_adapter as fh
from convexity import helpers, keys, logbuf, news_sentiment as ns, paths, server

SENTINEL = "SENTINELkey0123456789abcdefXYZ"


@pytest.fixture(autouse=True)
def _no_ambient_keys(tmp_path, monkeypatch):
    """No env keys, and no legacy key file found by walking up from the
    checkout (a developer's real .finnhub_key would otherwise be picked up)."""
    for var in ("FINNHUB_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    fake = tmp_path / "pkg" / "a" / "b" / "c" / "d" / "e" / "f" / "helpers.py"
    fake.parent.mkdir(parents=True)
    monkeypatch.setattr(helpers, "__file__", str(fake))
    monkeypatch.setattr(helpers, "_CONFIG_WARNED", [])
    monkeypatch.setattr(keys, "_MALFORMED_WARNED", [])
    # Module globals are restored after each test by monkeypatch.
    monkeypatch.setattr(ns, "FINNHUB_API_KEY", "")
    monkeypatch.setattr(ns, "NVIDIA_API_KEY", "")
    monkeypatch.setattr(ns, "_nvidia_client", None)
    monkeypatch.setattr(ns, "_schedule_persist", lambda: None)
    monkeypatch.setattr(fh, "FINNHUB_API_KEY", "")
    saved = dict(ns._LLM_STATUS)
    yield
    ns._LLM_STATUS.clear()
    ns._LLM_STATUS.update(saved)


def _cfg():
    return paths.config_file()


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


# ----------------------------- storage --------------------------------------


def test_save_creates_0600_and_reloads_modules():
    st = keys.save("nvidia", SENTINEL)
    assert json.loads(_cfg().read_text()) == {"nvidia_api_key": SENTINEL}
    if os.name == "posix":
        assert _mode(_cfg()) == 0o600
    assert st["nvidia"] == {
        "set": True,
        "source": "config",
        "in_config": True,
        "env_var": "NVIDIA_API_KEY",
        "env_overrides": False,
    }
    assert st["finnhub"]["set"] is False
    assert ns.NVIDIA_API_KEY == SENTINEL
    assert SENTINEL not in json.dumps(st)


def test_merge_keeps_other_fields_and_other_key():
    _cfg().parent.mkdir(parents=True, exist_ok=True)
    _cfg().write_text(json.dumps({"finnhub_api_key": "fh-old", "theme": "dark", "n": [1, 2]}))
    os.chmod(_cfg(), 0o644)
    keys.save("nvidia", "nv-new")
    data = json.loads(_cfg().read_text())
    assert data == {
        "finnhub_api_key": "fh-old",
        "theme": "dark",
        "n": [1, 2],
        "nvidia_api_key": "nv-new",
    }
    if os.name == "posix":
        assert _mode(_cfg()) == 0o600  # tightened on rewrite
    keys.clear("finnhub")
    assert json.loads(_cfg().read_text()) == {
        "theme": "dark",
        "n": [1, 2],
        "nvidia_api_key": "nv-new",
    }
    assert fh.FINNHUB_API_KEY == "" and ns.FINNHUB_API_KEY == ""
    assert not list(_cfg().parent.glob("config.json.*.tmp"))


def test_padded_key_is_stripped():
    keys.save("finnhub", "  \t" + SENTINEL + " \n")
    assert json.loads(_cfg().read_text())["finnhub_api_key"] == SENTINEL
    assert fh.FINNHUB_API_KEY == SENTINEL


@pytest.mark.parametrize(
    "bad", ["", "   ", "\n\t", "abc def", "abc\ndef", "a\x00b", "x" * 600, None, 42]
)
def test_invalid_keys_rejected_without_echo(bad):
    with pytest.raises(keys.InvalidKey) as ei:
        keys.save("nvidia", bad)
    if isinstance(bad, str) and bad.strip():
        assert bad.strip() not in str(ei.value)
    assert not _cfg().exists()


def test_unknown_provider():
    with pytest.raises(keys.InvalidKey):
        keys.save("openai", SENTINEL)


def test_double_save_is_idempotent():
    keys.save("nvidia", SENTINEL)
    before = os.stat(_cfg()).st_mtime_ns
    keys.save("nvidia", SENTINEL)
    assert os.stat(_cfg()).st_mtime_ns == before
    assert json.loads(_cfg().read_text()) == {"nvidia_api_key": SENTINEL}


@pytest.mark.parametrize("body", ["{not json", "[1, 2]", '"a string"', ""])
def test_malformed_config_is_refused_and_untouched(body, capsys):
    _cfg().parent.mkdir(parents=True, exist_ok=True)
    _cfg().write_text(body)
    with pytest.raises(keys.ConfigMalformed):
        keys.save("nvidia", SENTINEL)
    with pytest.raises(keys.ConfigMalformed):
        keys.clear("nvidia")
    assert _cfg().read_text() == body
    err = capsys.readouterr().err
    assert err.count("[keys] refusing to write") == 1  # logged once
    assert SENTINEL not in err
    assert keys.status()["config_ok"] is False


def test_env_var_wins_and_is_reported(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "from-env")
    st = keys.save("nvidia", SENTINEL)
    assert st["nvidia"]["source"] == "env"
    assert st["nvidia"]["env_overrides"] is True
    assert ns.NVIDIA_API_KEY == "from-env"
    st = keys.clear("nvidia")
    assert st["nvidia"] == {
        "set": True,
        "source": "env",
        "in_config": False,
        "env_var": "NVIDIA_API_KEY",
        "env_overrides": False,
    }


def test_legacy_file_still_read(tmp_path):
    (tmp_path / "pkg" / "a" / ".nvidia_key").write_text("legacy-val\n")
    st = keys.status()
    assert st["nvidia"]["source"] == "legacy" and st["nvidia"]["set"] is True
    keys.save("nvidia", SENTINEL)
    assert keys.status()["nvidia"]["source"] == "config"
    keys.clear("nvidia")
    assert keys.status()["nvidia"]["source"] == "legacy"
    assert (tmp_path / "pkg" / "a" / ".nvidia_key").exists()  # never touched


# ----------------------------- reload ---------------------------------------


def test_new_nvidia_key_clears_rejected_state_and_client():
    ns._llm_record(False, "key rejected (HTTP 401)", permanent=True)
    ns._nvidia_client = object()
    assert ns._llm_blocked()
    keys.save("nvidia", SENTINEL)
    assert ns._nvidia_client is None
    assert not ns._llm_blocked()
    st = ns.llm_status()
    assert st["ok"] is None and st["error"] is None


def test_same_key_reload_keeps_llm_status():
    keys.save("nvidia", SENTINEL)
    ns._llm_record(True)
    keys.reload_all()
    assert ns.llm_status()["ok"] is True


def test_finnhub_key_change_drops_adapter_cache():
    fh._FH_CACHE["x"] = (0, 1e9, fh._MISS)
    keys.save("finnhub", SENTINEL)
    assert "x" not in fh._FH_CACHE


def test_reload_rearms_malformed_warning(capsys):
    _cfg().parent.mkdir(parents=True, exist_ok=True)
    _cfg().write_text("{broken")
    helpers._config_secret("NVIDIA_API_KEY")
    helpers._config_secret("NVIDIA_API_KEY")
    assert capsys.readouterr().err.count("[config] ignoring") == 1
    keys.reload_all()  # the reload re-reads and says so again
    assert capsys.readouterr().err.count("[config] ignoring") == 1


# ----------------------------- Test buttons ---------------------------------


class _Stub:
    """Local provider stub: records every request's path, headers and body."""

    def __init__(self, code=200, delay=0.0):
        self.code, self.delay, self.seen = code, delay, []
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                stub.seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if stub.delay:
                    time.sleep(stub.delay)
                try:
                    self.send_response(stub.code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"c": 1}')
                except OSError:
                    pass

            do_GET = do_POST = _answer

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def stub(monkeypatch):
    made = []
    monkeypatch.setattr(helpers, "_FH_LIMITER", helpers._RateLimiter(60, "fh-test"))
    monkeypatch.setattr(helpers, "_NV_LIMITER", helpers._RateLimiter(40, "nv-test"))

    def make(code=200, delay=0.0):
        s = _Stub(code, delay)
        made.append(s)
        monkeypatch.setattr(keys, "_FINNHUB_BASE", s.base + "/api/v1/")
        monkeypatch.setattr(keys, "_NVIDIA_BASE", s.base + "/v1")
        return s

    yield make
    for s in made:
        s.close()


@pytest.mark.parametrize(
    "code,expected",
    [
        (200, "ok"),
        (401, "rejected"),
        (403, "rejected"),
        (429, "rate_limited"),
        (410, "unavailable"),
        (503, "unavailable"),
        (400, "error"),
    ],
)
@pytest.mark.parametrize("provider", ["finnhub", "nvidia"])
def test_check_classifies_and_keeps_key_out_of_url(stub, provider, code, expected):
    s = stub(code)
    keys.save(provider, SENTINEL)
    res = keys.check(provider)
    assert res["status"] == expected and res["http"] == code
    assert SENTINEL not in json.dumps(res)
    (req,) = s.seen
    assert SENTINEL not in req["path"]
    if provider == "finnhub":
        assert req["path"] == "/api/v1/quote?symbol=AAPL"
        assert req["headers"]["X-Finnhub-Token"] == SENTINEL
    else:
        assert req["path"] == "/v1/chat/completions"
        assert req["headers"]["Authorization"] == f"Bearer {SENTINEL}"
        body = json.loads(req["body"])
        assert body["max_tokens"] == 1 and body["model"] == ns._MODEL


def test_check_no_key_makes_no_call(stub):
    s = stub()
    assert keys.check("nvidia")["status"] == "no_key"
    assert s.seen == []


def test_check_network_error(stub, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(keys, "_FINNHUB_BASE", f"http://127.0.0.1:{port}/")
    keys.save("finnhub", SENTINEL)
    res = keys.check("finnhub")
    assert res["status"] == "network_error" and SENTINEL not in json.dumps(res)


def test_check_timeout(stub, monkeypatch):
    stub(200, delay=1.5)
    monkeypatch.setattr(keys, "_NV_TIMEOUT_S", 0.3)
    keys.save("nvidia", SENTINEL)
    assert keys.check("nvidia")["status"] == "network_error"


def test_check_full_budget_reports_instead_of_blocking(stub):
    s = stub()
    lim = helpers._FH_LIMITER
    for _ in range(lim.max):
        lim.acquire()
    keys.save("finnhub", SENTINEL)
    assert keys.check("finnhub")["status"] == "rate_limited"
    assert s.seen == []


# ----------------------------- the leak test --------------------------------


def test_key_never_leaves_config_json(stub, monkeypatch):
    """POST a sentinel key through the real Handler, then read every route
    that could plausibly carry it, stdout/stderr and the log ring: only
    config.json ever holds it."""
    stub(401)
    out, err = io.StringIO(), io.StringIO()
    # A tee like the one start_server installs, over our own buffers, so both
    # the console text and the Settings -> Logs ring are captured.
    monkeypatch.setattr(sys, "stdout", logbuf._Tee(out, "stdout"))
    monkeypatch.setattr(sys, "stderr", logbuf._Tee(err, "stderr"))
    since = logbuf.read(since=0, limit=1)["last_seq"]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    bodies = []

    def call(method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body, code = r.read().decode(), r.status
        except urllib.error.HTTPError as e:
            body, code = e.read().decode(), e.code
        bodies.append(body)
        return code, body

    def setkey(provider, key):
        return call("POST", "/api/keys", {"provider": provider, "action": "set", "key": key})[0]

    try:
        assert setkey("nvidia", "  " + SENTINEL + "  ") == 200
        assert setkey("finnhub", SENTINEL) == 200
        assert setkey("finnhub", SENTINEL) == 200  # double submit
        assert setkey("nvidia", SENTINEL + " x") == 400  # inner whitespace
        assert setkey("bogus", SENTINEL) == 400
        assert setkey("nvidia", "") == 400
        assert (
            call("POST", "/api/keys", {"provider": "nvidia", "action": "zap", "key": SENTINEL})[0]
            == 400
        )
        assert call("POST", "/api/keys", [SENTINEL])[0] == 400
        code, body = call("POST", "/api/keys/test", {"provider": "nvidia"})
        assert code == 200 and json.loads(body)["status"] == "rejected"
        call("POST", "/api/keys/test", {"provider": "finnhub"})
        st = json.loads(call("GET", "/api/keys")[1])
        assert st["nvidia"]["set"] and st["finnhub"]["set"]
        h = json.loads(call("GET", "/api/health")[1])
        assert h["nvidia_key_set"] is True and h["finnhub_key_set"] is True
        call("GET", "/api/runtime-status")
        call("GET", "/api/logs?since=0&limit=4000")
        # malformed config.json -> 409 and the file is left alone
        good = _cfg().read_text()
        _cfg().write_text("{oops")
        assert setkey("nvidia", SENTINEL + "2") == 409
        call("GET", "/api/keys")
        assert _cfg().read_text() == "{oops"
        _cfg().write_text(good)
        assert call("POST", "/api/keys", {"provider": "nvidia", "action": "clear"})[0] == 200
        assert call("POST", "/api/keys", {"provider": "finnhub", "action": "clear"})[0] == 200
        h = json.loads(call("GET", "/api/health")[1])
        assert h["nvidia_key_set"] is False and h["finnhub_key_set"] is False
        call("GET", "/api/logs?since=0&limit=4000")
    finally:
        srv.shutdown()
        srv.server_close()

    for b in bodies:
        assert SENTINEL not in b
    ring = json.dumps(logbuf.read(since=since, limit=4000))
    assert "[keys] NVIDIA NIM key saved" in ring  # the tee really captured
    assert "/api/keys" in ring
    assert SENTINEL not in ring
    assert SENTINEL not in out.getvalue() and SENTINEL not in err.getvalue()
    assert json.loads(_cfg().read_text()) == {}


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/plain"},  # no-preflight simple POST
        {"Content-Type": "application/json", "Origin": "https://evil.example"},
        {"Content-Type": "application/json", "Origin": "http://127.0.0.1:1"},  # other local port
        {"Content-Type": "application/json", "Host": "evil.example"},  # DNS rebinding
    ],
)
def test_key_routes_refuse_cross_origin(headers):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        for path, body in (
            ("/api/keys", {"provider": "nvidia", "action": "set", "key": SENTINEL}),
            ("/api/keys/test", {"provider": "nvidia"}),
        ):
            req = urllib.request.Request(
                base + path, data=json.dumps(body).encode(), method="POST", headers=headers
            )
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(req, timeout=10)
            assert ei.value.code == 403
        # the page's own same-origin request still works
        ok = urllib.request.Request(
            base + "/api/keys",
            method="POST",
            data=json.dumps({"provider": "nvidia", "action": "set", "key": SENTINEL}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
        )
        assert urllib.request.urlopen(ok, timeout=10).status == 200
    finally:
        srv.shutdown()
        srv.server_close()
    assert json.loads(_cfg().read_text()) == {"nvidia_api_key": SENTINEL}
