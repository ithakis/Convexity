#!/usr/bin/env python3
"""End-to-end server smoke test — used by CI to prove the app actually runs.

Starts the real HTTP server (the same start_server() the browser and
desktop apps both use), hits a handful of real routes over HTTP, and
asserts on the responses. This is deliberately NOT a unit test — it
exercises the actual request-handling path (routing, JSON encoding,
static file serving) the way a real client would, which is what "the app
is working" should mean in CI, not just "the modules import."

Exits non-zero (with a traceback) on any failure, before ever reaching
shutdown_server() — see the comment above that call for why.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Make `convexity` importable regardless of cwd/invocation style
# (running `python scripts/smoke_test_server.py` puts scripts/ on sys.path,
# not the repo root).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from convexity.server import shutdown_server, start_server  # noqa: E402


def _get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


def main() -> None:
    server, port = start_server()
    base = f"http://127.0.0.1:{port}"

    try:
        # Readiness: poll until the server actually accepts a connection
        # (start_server() returns as soon as the socket is bound, not once
        # serve_forever's accept loop is live — see desktop.py's identical
        # readiness-probe rationale).
        deadline = time.monotonic() + 15.0
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                status, body = _get(f"{base}/")
                assert status == 200, f"GET / returned {status}"
                assert b"<html" in body.lower() or b"<!doctype" in body.lower(), (
                    "GET / did not return HTML"
                )
                break
            except (urllib.error.URLError, ConnectionError) as exc:
                last_exc = exc
                time.sleep(0.1)
        else:
            raise RuntimeError(f"server never became ready: {last_exc}")
        print(f"[smoke] GET / -> 200 ({len(body)} bytes)")

        # Real JSON API routes that touch only local state (persistence.py) —
        # exercises routing + JSON serialization deterministically, no
        # network involved, so these are hard failures.
        for path in ("/api/watchlists", "/api/views"):
            status, body = _get(f"{base}{path}")
            assert status == 200, f"GET {path} returned {status}"
            data = json.loads(body)  # raises if the response isn't valid JSON
            print(f"[smoke] GET {path} -> 200 ({type(data).__name__})")

        # Static assets referenced by index.html must actually be servable.
        for path in ("/static/app.js", "/static/style.css"):
            status, body = _get(f"{base}{path}")
            assert status == 200, f"GET {path} returned {status}"
            assert len(body) > 0, f"GET {path} returned an empty body"
            print(f"[smoke] GET {path} -> 200 ({len(body)} bytes)")

        # /api/fx-rates hits yfinance -> Yahoo Finance over the network.
        # Best-effort only: a Yahoo rate-limit or CI network hiccup here is
        # not a Portfolio Tracker bug, so it must not fail the build — but a
        # pass is still useful signal that the network fetch path works.
        try:
            status, body = _get(f"{base}/api/fx-rates?base=USD", timeout=15.0)
            json.loads(body)
            print(f"[smoke] GET /api/fx-rates -> {status} (best-effort, network)")
        except Exception as exc:
            print(f"[smoke] GET /api/fx-rates skipped/failed (non-fatal, network-dependent): {exc}")

        print("[smoke] all checks passed")
    except Exception:
        # Don't let a failure get swallowed by shutdown_server()'s os._exit —
        # close the server plainly and let the traceback/non-zero exit
        # propagate normally so CI reports the real failure.
        server.shutdown()
        server.server_close()
        raise

    # Only reached on success — shutdown_server()'s os._exit(0) is fine here
    # since every assertion above already passed.
    shutdown_server(server)


if __name__ == "__main__":
    sys.exit(main())
