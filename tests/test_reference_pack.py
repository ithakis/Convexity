"""reference_pack.py — the pack format and its validators (and, from the
app-side fetch on, the download itself against a local HTTP stub)."""

import gzip
import json

import pytest

from convexity import reference_pack as rp

MV = "mlsent-v1.1"


def _history(n=3, **extra):
    recs = [
        {
            "date": "2026-09-2%d" % (i % 9),
            "symbol": "AAPL",
            "market_score": 0.01 * i,
            "market_tier": "no_edge",
            "market_model": MV,
            "fwd_1d": None,
            **extra,
        }
        for i in range(n)
    ]
    return {"schema": rp.SCHEMA_VERSION, "records": recs}


def _anchor(n=3):
    return {
        "schema": rp.SCHEMA_VERSION,
        "model_version": MV,
        "as_of": "2026-09-28",
        "window_days": 90,
        "rows": [["2026-09-28", "MSFT", 0.1 * i] for i in range(n)],
    }


def make_pack(d, history=None, anchor=None, model_version=MV, date="2026-09-28"):
    """Write a valid pack into ``d``; returns the manifest. Used by the
    fetch, anchor and Track record tests too."""
    d.mkdir(parents=True, exist_ok=True)
    blobs = {
        rp.HISTORY: rp.gzip_json(history or _history()),
        rp.ANCHOR: rp.gzip_json(anchor or _anchor()),
    }
    for k, v in blobs.items():
        (d / k).write_bytes(v)
    h = history or _history()
    a = anchor or _anchor()
    m = {
        "schema_version": rp.SCHEMA_VERSION,
        "model_version": model_version,
        "date": date,
        "rows": {"history": len(h["records"]), "anchor": len(a["rows"])},
        "files": {k: {"sha256": rp.sha256(v), "bytes": len(v)} for k, v in blobs.items()},
    }
    (d / rp.MANIFEST).write_text(json.dumps(m))
    return m


def test_a_valid_pack_round_trips(tmp_path):
    make_pack(tmp_path)
    m, h, a = rp.load_dir(tmp_path, MV)
    assert m["date"] == "2026-09-28" and len(h) == 3 and len(a) == 3


def test_gzip_is_deterministic():
    assert rp.gzip_json({"a": [1, 2]}) == rp.gzip_json({"a": [1, 2]})


@pytest.mark.parametrize("field", ["headline", "summary", "url", "text"])
def test_any_text_field_makes_the_history_invalid(field):
    with pytest.raises(rp.PackError, match="unexpected field"):
        rp.validate_history(_history(**{field: "a headline"}), MV)


def test_other_model_version_is_rejected(tmp_path):
    make_pack(tmp_path, model_version="mlsent-v9")
    with pytest.raises(rp.PackError, match="scored by"):
        rp.load_dir(tmp_path, MV)


def test_wrong_schema_is_rejected(tmp_path):
    m = make_pack(tmp_path)
    m["schema_version"] = 99
    (tmp_path / rp.MANIFEST).write_text(json.dumps(m))
    with pytest.raises(rp.PackError, match="schema_version"):
        rp.load_dir(tmp_path, MV)


def test_bad_hash_is_rejected(tmp_path):
    m = make_pack(tmp_path)
    m["files"][rp.ANCHOR]["sha256"] = "0" * 64
    (tmp_path / rp.MANIFEST).write_text(json.dumps(m))
    with pytest.raises(rp.PackError, match="checksum mismatch"):
        rp.load_dir(tmp_path, MV)


def test_truncated_gzip_is_rejected():
    blob = rp.gzip_json(_anchor(200))
    with pytest.raises(rp.PackError, match="truncated|gzip"):
        rp.gunzip_json(blob[: len(blob) // 2])


def test_gzip_bomb_hits_the_cap():
    bomb = gzip.compress(b"[" + b"0," * 500_000 + b"0]")
    assert len(bomb) < 10_000
    with pytest.raises(rp.PackError, match="cap"):
        rp.gunzip_json(bomb, max_bytes=100_000)


def test_non_finite_numbers_are_rejected():
    a = _anchor()
    a["rows"][0][2] = float("nan")
    with pytest.raises(rp.PackError):
        rp.validate_anchor(a, MV)


# ------------------------------------------------------------ app-side fetch
import http.server  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402

from convexity import paths  # noqa: E402


class _Stub:
    """Serves ``files`` {name: bytes} on 127.0.0.1; counts requests.
    ``lie_length`` sends a Content-Length larger than the body (truncation)."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.hits: list[str] = []
        self.lie_length: dict[str, int] = {}
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                name = self.path.rsplit("/", 1)[-1]
                stub.hits.append(name)
                data = stub.files.get(name)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                n = stub.lie_length.get(name, len(data))
                if n is not None:  # None: no Content-Length, body until close
                    self.send_header("Content-Length", str(n))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/pack/"

    def publish(self, tmp, **kw):
        src = tmp / "src"
        m = make_pack(src, **kw)
        self.files = {p.name: p.read_bytes() for p in src.iterdir()}
        return m


def _today():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture()
def stub(monkeypatch):
    s = _Stub()
    monkeypatch.setenv("CONVEXITY_REFERENCE_PACK", "1")
    monkeypatch.setenv("CONVEXITY_REFERENCE_URL", s.url)
    monkeypatch.setattr(rp, "_BACKOFF_S", 0.01)
    rp.reset_for_tests()
    yield s
    rp.reset_for_tests()
    s.httpd.shutdown()
    s.httpd.server_close()


def _fetch(force=False):
    rp.start(force=force)
    return rp.wait(10)


def test_good_pack_installs_and_feeds_the_readers(stub, tmp_path):
    stub.publish(tmp_path, date=_today(), anchor=_anchor(250))
    st = _fetch()
    assert st["state"] == "installed", st
    assert (paths.reference_dir() / rp.MANIFEST).exists()
    assert len(rp.anchor_scores(MV)) == 250
    assert len(rp.anchor_scores(MV, exclude=("2026-09-28", "MSFT"))) == 0
    assert len(rp.history_records()) == 3
    assert rp.status()["in_use"] is True
    assert rp.anchor_scores("mlsent-v9") == []


def test_fresh_copy_is_not_fetched_again(stub, tmp_path):
    stub.publish(tmp_path, date=_today())
    _fetch()
    n = len(stub.hits)
    assert rp.start()["state"] in ("installed", "up_to_date")
    rp.wait(5)
    assert len(stub.hits) == n
    # "Check now" does ask, but an unchanged manifest downloads no data file.
    _fetch(force=True)
    assert stub.hits[n:] == [rp.MANIFEST]
    assert rp.status()["state"] == "up_to_date"


def test_disabled_makes_no_request_and_hides_the_pack(stub, tmp_path):
    stub.publish(tmp_path, date=_today(), anchor=_anchor(250))
    _fetch()
    rp.set_enabled(False)
    n = len(stub.hits)
    assert _fetch(force=True)["state"] == "disabled"
    assert len(stub.hits) == n
    assert rp.anchor_scores(MV) == [] and rp.history_records() == []
    assert json.loads(paths.state_file("reference_pack").read_text()) == {"enabled": False}
    rp.set_enabled(True)
    rp.wait(5)
    assert len(rp.anchor_scores(MV)) == 250


def test_env_switch_forces_it_off(stub, monkeypatch, tmp_path):
    stub.publish(tmp_path, date=_today())
    monkeypatch.setenv("CONVEXITY_REFERENCE_PACK", "0")
    assert _fetch(force=True)["state"] == "disabled" and stub.hits == []


def _fails_and_keeps_previous(stub, tmp_path, mutate, reason, after=None):
    stub.publish(tmp_path, date=_today(), anchor=_anchor(250))
    _fetch()
    before = (paths.reference_dir() / rp.ANCHOR).read_bytes()
    mutate(stub)
    st = _fetch(force=True)
    assert st["state"] == "failed" and re.search(reason, st["error"]), st
    if after:
        after()
    assert (paths.reference_dir() / rp.ANCHOR).read_bytes() == before
    assert len(rp.anchor_scores(MV)) == 250  # the previous copy stays in use


import re  # noqa: E402


def _new_manifest(stub, **changes):
    m = json.loads(stub.files[rp.MANIFEST])
    for k, v in changes.items():
        m[k] = v
    stub.files[rp.MANIFEST] = json.dumps(m).encode()


def test_bad_hash_fails_and_keeps_previous(stub, tmp_path):
    def mutate(s):
        s.files[rp.ANCHOR] = rp.gzip_json(_anchor(251))
        m = json.loads(s.files[rp.MANIFEST])
        m["date"] = "2026-09-29"
        m["files"][rp.ANCHOR]["bytes"] = len(s.files[rp.ANCHOR])
        s.files[rp.MANIFEST] = json.dumps(m).encode()

    _fails_and_keeps_previous(stub, tmp_path, mutate, "checksum mismatch")


def _serve_oversize(s, monkeypatch):
    """The manifest claims a small file (under the cap); 5000 bytes arrive."""
    monkeypatch.setattr(rp, "MAX_FILE_BYTES", 400)
    s.files[rp.ANCHOR] = os.urandom(5000)
    m = json.loads(s.files[rp.MANIFEST])
    m["date"] = "2026-09-29"
    m["files"][rp.ANCHOR]["bytes"] = 300
    s.files[rp.MANIFEST] = json.dumps(m).encode()


_CAP = rp.MAX_FILE_BYTES


def test_oversize_fails(stub, tmp_path, monkeypatch):
    _fails_and_keeps_previous(
        stub,
        tmp_path,
        lambda s: _serve_oversize(s, monkeypatch),
        "too large",
        after=lambda: monkeypatch.setattr(rp, "MAX_FILE_BYTES", _CAP),
    )


def test_oversize_without_content_length_fails(stub, tmp_path, monkeypatch):
    def mutate(s):
        _serve_oversize(s, monkeypatch)
        s.lie_length[rp.ANCHOR] = None  # no Content-Length: only the stream cap stops it

    _fails_and_keeps_previous(
        stub,
        tmp_path,
        mutate,
        "exceeded",
        after=lambda: monkeypatch.setattr(rp, "MAX_FILE_BYTES", _CAP),
    )


def test_wrong_schema_fails(stub, tmp_path):
    _fails_and_keeps_previous(
        stub, tmp_path, lambda s: _new_manifest(s, schema_version=2), "schema_version"
    )


def test_wrong_model_version_fails(stub, tmp_path):
    _fails_and_keeps_previous(
        stub, tmp_path, lambda s: _new_manifest(s, model_version="mlsent-v2"), "scored by"
    )


def test_truncated_gzip_fails(stub, tmp_path):
    def mutate(s):
        blob = rp.gzip_json(_anchor(260))[:-30]
        s.files[rp.ANCHOR] = blob
        m = json.loads(s.files[rp.MANIFEST])
        m["files"][rp.ANCHOR] = {"sha256": rp.sha256(blob), "bytes": len(blob)}
        s.files[rp.MANIFEST] = json.dumps(m).encode()

    _fails_and_keeps_previous(stub, tmp_path, mutate, "truncated|gzip")


def test_truncated_transfer_fails(stub, tmp_path):
    def mutate(s):
        s.lie_length[rp.HISTORY] = len(s.files[rp.HISTORY]) + 100
        _new_manifest(s, date="2026-09-29")

    _fails_and_keeps_previous(stub, tmp_path, mutate, "truncated|network")


def test_offline_fails_quietly(stub, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("CONVEXITY_REFERENCE_URL", f"http://127.0.0.1:{port}/x/")
    st = _fetch()
    assert st["state"] == "failed" and "network" in st["error"]
    assert rp.anchor_scores(MV) == [] and rp.history_records() == []


def test_not_published_yet_is_a_404_not_a_crash(stub):
    st = _fetch()
    assert st["state"] == "failed" and "404" in st["error"]


def test_plain_http_to_a_real_host_is_refused(stub, monkeypatch):
    monkeypatch.setenv("CONVEXITY_REFERENCE_URL", "http://example.com/pack/")
    st = _fetch()
    assert st["state"] == "failed" and "https required" in st["error"]


def test_old_pack_is_not_used(stub, tmp_path):
    stub.publish(tmp_path, date="2026-01-02", anchor=_anchor(250))
    assert _fetch()["state"] == "installed"
    assert rp.anchor_scores(MV) == [] and rp.status()["installed"]["stale"] is True


def test_reference_pack_routes(stub, tmp_path):
    from convexity import server

    stub.publish(tmp_path, date=_today())
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:

        def post(body, ctype="application/json"):
            req = urllib.request.Request(
                base + "/api/reference-pack",
                data=json.dumps(body).encode(),
                method="POST",
                headers={"Content-Type": ctype},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None

        assert post({"enabled": False}, ctype="text/plain")[0] == 403
        assert rp.enabled() is True
        code, st = post({"enabled": False})
        assert code == 200 and st["enabled"] is False
        code, st = post({"enabled": True})
        assert code == 200 and st["enabled"] is True
        rp.wait(5)
        code, _ = post({"action": "refresh"})
        assert code == 202
        rp.wait(5)
        assert post({"what": 1})[0] == 400
        with urllib.request.urlopen(base + "/api/reference-pack", timeout=10) as r:
            st = json.loads(r.read())
        assert st["installed"]["date"] == _today() and st["in_use"] is True
        with urllib.request.urlopen(base + "/api/runtime-status", timeout=30) as r:
            assert "reference" in json.loads(r.read())
    finally:
        httpd.shutdown()
        httpd.server_close()
        time.sleep(0)
