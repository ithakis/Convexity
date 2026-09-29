"""Tests for convexity.model_fetch — the first-run Market read model download.

Everything runs against a local HTTP server on 127.0.0.1 and a throwaway data
folder; no test touches the network or the real ~/.convexity. The pinned
SHA-256 is monkeypatched to each test tarball's hash — the code path is the
production one, only the trust anchor is swapped.
"""

import hashlib
import http.server
import io
import re
import socket
import tarfile
import threading
import time
from pathlib import Path

import pytest

from convexity import ml_sentiment as ms
from convexity import model_fetch as mfetch
from convexity import paths

VER = mfetch.MODEL_VERSION


# ------------------------------------------------------------------ fixtures
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # No legacy ~/.convexity lookup, no auto-disable, fast retries, clean state.
    monkeypatch.setenv("CONVEXITY_LEGACY_HOME", str(tmp_path / "legacy-home"))
    monkeypatch.delenv("MLSENT_MODEL_DIR", raising=False)
    monkeypatch.delenv("CONVEXITY_MODEL_DOWNLOAD", raising=False)
    monkeypatch.delenv("CONVEXITY_MODEL_URL", raising=False)
    monkeypatch.setattr(mfetch, "_BACKOFF_S", 0.01)
    monkeypatch.setattr(mfetch, "_TIMEOUT_S", 5.0)
    mfetch.wait(10)
    with mfetch._LOCK:
        mfetch._STATUS.update(
            state="idle",
            bytes=0,
            total=None,
            error="",
            started_at=None,
            finished_at=None,
            source="",
        )
    ms.reset_for_tests()
    yield
    mfetch.wait(10)
    ms.reset_for_tests()


class _Server:
    """Serves ``routes[path] = (status, bytes)`` and counts requests."""

    def __init__(self):
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.hits: list[str] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append(self.path)
                code, body = outer.routes.get(self.path, (404, b"nope"))
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path="/m.tar.gz"):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def server():
    s = _Server()
    yield s
    s.close()


def _files(extra: dict | None = None) -> dict:
    files = {f: f"content of {f}\n".encode() for f in mfetch.ARTIFACT_FILES}
    files.update(extra or {})
    return files


def _tar(members, top=True) -> bytes:
    """members: list of (name, bytes | ('sym'|'lnk'|'dir'|'fifo', target))."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tf:
        if top:
            d = tarfile.TarInfo(VER)
            d.type = tarfile.DIRTYPE
            tf.addfile(d)
        for name, data in members:
            ti = tarfile.TarInfo(name)
            if isinstance(data, tuple):
                kind, target = data
                ti.type = {
                    "sym": tarfile.SYMTYPE,
                    "lnk": tarfile.LNKTYPE,
                    "dir": tarfile.DIRTYPE,
                    "fifo": tarfile.FIFOTYPE,
                }[kind]
                ti.linkname = target
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return raw.getvalue()


def _good_tar() -> bytes:
    return _tar([(f"{VER}/{n}", b) for n, b in _files().items()])


def _serve(server, monkeypatch, body: bytes, pin: bytes | None = None, code=200):
    server.routes["/m.tar.gz"] = (code, body)
    monkeypatch.setenv("CONVEXITY_MODEL_URL", server.url())
    monkeypatch.setattr(
        mfetch, "MODEL_SHA256", hashlib.sha256(pin if pin is not None else body).hexdigest()
    )


def _run() -> dict:
    mfetch.start()
    return mfetch.wait(30)


def _models() -> Path:
    return paths.models_dir()


def _leftovers() -> list[str]:
    m = _models()
    return sorted(p.name for p in m.iterdir()) if m.exists() else []


# ------------------------------------------------------------------ pins
def test_pins_are_consistent():
    assert mfetch.MODEL_VERSION == ms.ARTIFACT_VERSION
    assert mfetch.MODEL_URL.startswith("https://github.com/ithakis/Convexity/releases/download/")
    assert mfetch.MODEL_URL.endswith(f"/{VER}.tar.gz")
    assert re.fullmatch(r"[0-9a-f]{64}", mfetch.MODEL_SHA256)


def test_sha_not_overridable_by_env(monkeypatch):
    before = mfetch.MODEL_SHA256
    monkeypatch.setenv("CONVEXITY_MODEL_SHA256", "0" * 64)
    import importlib

    importlib.reload(mfetch)
    try:
        assert before == mfetch.MODEL_SHA256
    finally:
        importlib.reload(mfetch)


def test_pack_is_deterministic(tmp_path):
    src = tmp_path / "bundle"
    src.mkdir()
    for n, b in _files().items():
        (src / n).write_bytes(b)
    a = mfetch.pack(src, tmp_path / "a.tar.gz")
    time.sleep(1.1)  # a different wall clock must not change the bytes
    (src / "meta.json").touch()
    b = mfetch.pack(src, tmp_path / "b.tar.gz")
    assert a == b
    with tarfile.open(tmp_path / "a.tar.gz") as tf:
        names = tf.getnames()
        assert names[0] == VER
        assert sorted(names[1:]) == [f"{VER}/{f}" for f in sorted(mfetch.ARTIFACT_FILES)]
        assert all(m.uid == 0 and m.mtime == 0 and not m.uname for m in tf.getmembers())


# ------------------------------------------------------------------ happy path
def test_download_installs_and_market_read_becomes_available(server, monkeypatch, tmp_path):
    """The whole path with a real (tiny) LightGBM artifact: missing model ->
    download -> verify -> extract -> reload -> available()."""
    from tests.test_ml_sentiment import build_tiny_artifact

    src = build_tiny_artifact(tmp_path / "bundle")[0]
    tarball = tmp_path / "release.tar.gz"
    sha = mfetch.pack(src, tarball)
    body = tarball.read_bytes()
    _serve(server, monkeypatch, body)
    assert hashlib.sha256(body).hexdigest() == sha

    assert ms.available() is False  # boot state: missing, cached
    st = _run()
    assert st["state"] == "installed", st
    assert ms.available() is True  # reload() cleared the cached failure
    for f in mfetch.ARTIFACT_FILES:
        assert (mfetch.target_dir() / f).read_bytes() == (src / f).read_bytes()
    assert _leftovers() == [VER]
    rs = ms.runtime_status()
    assert rs["available"] and rs["download"]["state"] == "installed"


def test_not_needed_only_when_complete(server, monkeypatch):
    _serve(server, monkeypatch, _good_tar())
    t = mfetch.target_dir()
    t.mkdir(parents=True)
    for f in mfetch.ARTIFACT_FILES:
        (t / f).write_bytes(b"user's own copy")
    assert mfetch.start()["state"] == "not_needed"
    assert server.hits == []
    assert (t / "meta.json").read_bytes() == b"user's own copy"


@pytest.mark.parametrize("present", [[], ["meta.json", "tier_cuts.json"]])
def test_incomplete_dir_is_set_aside_and_downloaded(server, monkeypatch, present):
    """An emptied or half-copied folder used to count as installed: nothing
    downloaded and Settings offered no Retry. Its contents are kept, not deleted."""
    t = mfetch.target_dir()
    t.mkdir(parents=True)
    for f in present:
        (t / f).write_bytes(b"partial")
    assert mfetch.needed() is True
    rs = ms.runtime_status()
    assert rs["model_missing"] is True and rs["model_dir_exists"] is True

    _serve(server, monkeypatch, _good_tar())
    assert _run()["state"] == "installed"
    assert mfetch.complete(t)
    assert (t / "meta.json").read_bytes() == _files()["meta.json"]
    aside = [p for p in _models().iterdir() if p.name.startswith(f"{VER}.incomplete-")]
    assert len(aside) == 1
    assert sorted(p.name for p in aside[0].iterdir()) == sorted(present)
    assert ms.runtime_status()["model_missing"] is False


def test_incomplete_dir_untouched_when_download_fails(monkeypatch):
    t = mfetch.target_dir()
    t.mkdir(parents=True)
    (t / "meta.json").write_bytes(b"partial")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("CONVEXITY_MODEL_URL", f"http://127.0.0.1:{port}/m.tar.gz")
    assert _run()["state"] == "failed"
    assert (t / "meta.json").read_bytes() == b"partial"  # only set aside on success
    assert "model download failed" in ms.runtime_status()["reason"]


def test_explicit_model_dir_is_never_downloaded_into(server, monkeypatch, tmp_path):
    _serve(server, monkeypatch, _good_tar())
    monkeypatch.setenv("MLSENT_MODEL_DIR", str(tmp_path / "mine"))
    assert mfetch.start()["state"] == "not_needed"
    assert server.hits == []


def test_disabled_by_env_but_retry_forces(server, monkeypatch):
    _serve(server, monkeypatch, _good_tar())
    monkeypatch.setenv("CONVEXITY_MODEL_DOWNLOAD", "0")
    assert mfetch.start()["state"] == "disabled"
    assert server.hits == []
    mfetch.start(force=True)
    assert mfetch.wait(30)["state"] == "installed"


def test_single_flight(server, monkeypatch):
    _serve(server, monkeypatch, _good_tar())
    gate = threading.Event()
    real = mfetch._download

    def slow(url, dest):
        gate.wait(10)
        return real(url, dest)

    monkeypatch.setattr(mfetch, "_download", slow)
    a = mfetch.start()
    b = mfetch.start(force=True)
    assert a["state"] == b["state"] == "downloading"
    gate.set()
    assert mfetch.wait(30)["state"] == "installed"
    assert len(server.hits) == 1


# ------------------------------------------------------------------ failures
def test_checksum_mismatch_rejected(server, monkeypatch):
    good = _good_tar()
    tampered = _tar([(f"{VER}/{n}", b + b"x") for n, b in _files().items()])
    _serve(server, monkeypatch, tampered, pin=good)
    st = _run()
    assert st["state"] == "failed" and "checksum mismatch" in st["error"]
    assert not mfetch.target_dir().exists()
    assert _leftovers() == []
    assert len(server.hits) == 1  # never retried
    assert "download failed: checksum mismatch" in ms.runtime_status()["reason"]


@pytest.mark.parametrize(
    "members,why",
    [
        ([(f"{VER}/../evil.txt", b"x")], "traversal"),
        ([("../evil.txt", b"x")], "traversal"),
        ([("/tmp/evil.txt", b"x")], "absolute"),
        ([(f"{VER}/model.lgbm.txt", ("sym", "/etc/passwd"))], "link"),
        ([(f"{VER}/model.lgbm.txt", ("lnk", "/etc/passwd"))], "link"),
        ([(f"{VER}/idf.npy", ("fifo", ""))], "special"),
        ([(f"{VER}/evil.py", b"x")], "unexpected file"),
        ([(f"{VER}/sub", ("dir", ""))], "unexpected directory"),
        ([(f"other/{mfetch.ARTIFACT_FILES[0]}", b"x")], "unexpected file"),
    ],
)
def test_unsafe_archive_rejected(server, monkeypatch, tmp_path, members, why):
    body = _tar([(f"{VER}/{n}", b) for n, b in _files().items()] + members)
    _serve(server, monkeypatch, body)
    st = _run()
    assert st["state"] == "failed", st
    assert why in st["error"]
    assert not mfetch.target_dir().exists()
    assert _leftovers() == []
    assert not (paths.data_dir() / "evil.txt").exists()
    assert not (_models().parent / "evil.txt").exists()


def test_incomplete_archive_rejected(server, monkeypatch):
    files = _files()
    files.pop("idf.npy")
    _serve(server, monkeypatch, _tar([(f"{VER}/{n}", b) for n, b in files.items()]))
    st = _run()
    assert st["state"] == "failed" and "missing idf.npy" in st["error"]
    assert _leftovers() == []


def test_not_a_tarball(server, monkeypatch):
    _serve(server, monkeypatch, b"<html>not found</html>")
    st = _run()
    assert st["state"] == "failed" and "unreadable" in st["error"]
    assert _leftovers() == []


def test_offline_degrades_cleanly(monkeypatch):
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("CONVEXITY_MODEL_URL", f"http://127.0.0.1:{port}/m.tar.gz")
    st = _run()
    assert st["state"] == "failed" and "network" in st["error"]
    assert _leftovers() == []
    rs = ms.runtime_status()
    assert rs["available"] is False and "model download failed" in rs["reason"]


def test_http_404_not_retried(server, monkeypatch):
    monkeypatch.setenv("CONVEXITY_MODEL_URL", server.url("/missing"))
    st = _run()
    assert st["state"] == "failed" and "HTTP 404" in st["error"]
    assert len(server.hits) == 1


def test_http_503_retried_then_fails(server, monkeypatch):
    _serve(server, monkeypatch, b"busy", code=503)
    st = _run()
    assert st["state"] == "failed" and "HTTP 503" in st["error"]
    assert len(server.hits) == mfetch._ATTEMPTS


def test_oversize_rejected(server, monkeypatch):
    monkeypatch.setattr(mfetch, "MAX_DOWNLOAD_BYTES", 100)
    _serve(server, monkeypatch, _good_tar())
    st = _run()
    assert st["state"] == "failed" and "too large" in st["error"]
    assert _leftovers() == []


@pytest.mark.parametrize(
    "url", ["http://example.com/m.tar.gz", "ftp://127.0.0.1/m", "file:///etc/passwd", "not a url"]
)
def test_insecure_urls_refused(monkeypatch, url):
    monkeypatch.setenv("CONVEXITY_MODEL_URL", url)
    st = _run()
    assert st["state"] == "failed" and "refusing" in st["error"]


def test_stale_leftovers_swept(server, monkeypatch):
    m = _models()
    m.mkdir(parents=True)
    dead = 999_999_9
    (m / f".{VER}.{dead}.part").write_bytes(b"half")
    (m / f".{VER}.{dead}.staging").mkdir()
    (m / "unrelated").mkdir()
    _serve(server, monkeypatch, _good_tar())
    assert _run()["state"] == "installed"
    assert _leftovers() == [VER, "unrelated"]


def test_retry_route(server, monkeypatch):
    """POST /api/model-download (the Settings Retry button) -> 202 + status."""
    from convexity import server as srv
    import json
    import urllib.request

    _serve(server, monkeypatch, _good_tar())
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/api/model-download",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 202
            assert json.loads(r.read())["state"] in ("downloading", "installed")
        assert mfetch.wait(30)["state"] == "installed"
    finally:
        httpd.shutdown()
        httpd.server_close()
