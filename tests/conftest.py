"""Every test runs against a throwaway data folder — never the real one.

``CONVEXITY_HOME`` is set at import time, before any test module imports
``convexity``: several modules bind their data paths at import
(persistence._VIEWS_FILE, news_sentiment._PERSIST_FILE, ...).

The autouse fixture then gives each test its own folder for everything that
resolves paths lazily (paths.data_dir(), ml_sentiment.model_dir(), keys).
"""

import os
import tempfile
from urllib.parse import urlsplit

import pytest

os.environ["CONVEXITY_HOME"] = tempfile.mkdtemp(prefix="convexity-test-home-")
# A server booted by a test (start_server) would otherwise start the first-run
# model download from GitHub — no real network in tests. test_model_fetch.py
# drives model_fetch directly against a local server.
os.environ["CONVEXITY_MODEL_DOWNLOAD"] = "0"
os.environ.pop("CONVEXITY_MODEL_URL", None)
# Same for the daily reference pack; test_reference_pack.py turns it on
# against a local stub.
os.environ["CONVEXITY_REFERENCE_PACK"] = "0"
os.environ.pop("CONVEXITY_REFERENCE_URL", None)
# Nor the notification scheduler (notify.py): its passes are jobs, and a
# stray one would race test_jobs.py's single-flight assertions.
os.environ["CONVEXITY_NOTIFY"] = "0"


def pytest_collection_modifyitems(items):
    """A parametrized case is named after its values unless given ids, and
    `pytest -v` prints each name as one log line: an 8 MB payload in a name
    froze CI's runner for 12 days (test_importer). Refuse long names."""
    long = [it.nodeid[:120] + "..." for it in items if len(it.nodeid) > 300]
    if long:
        raise pytest.UsageError(f"test ids over 300 chars (pass ids=): {long[:3]}")


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    home = tmp_path / "convexity-home"
    monkeypatch.setenv("CONVEXITY_HOME", str(home))
    # Yahoo's live Lookup (names the symbol pack lacks) is network: off
    # unless a test stubs it with its own answer.
    from convexity import symbol_db

    monkeypatch.setattr(symbol_db, "yahoo_lookup", lambda *a, **k: [])
    # My Investments' parallel FX prefetch is network too: off (the tests
    # stub the FX paths the page reads).
    from convexity import fx

    monkeypatch.setattr(fx, "prefetch_usd_series", lambda *a, **k: None)
    # The Attention rail's earnings / ex-dividend calendar: no Yahoo (a
    # failed call, so nothing is cached); test_attention.py stubs its own.
    from convexity import attention

    monkeypatch.setattr(attention, "fetch_events", lambda s: None)
    # capm's process memory (betas, learned home lines) follows the folder.
    from convexity import capm

    monkeypatch.setattr(capm, "_LINES_DISK", None)
    monkeypatch.setattr(capm, "_LINE_LEARNED", {})
    capm._MEM.clear()
    # Last line of defence: yfinance's transport (curl_cffi) answers as if
    # offline for anything but loopback. Since 6 Oct a stray live fetch ran
    # the full analytics on real Yahoo data on GitHub's runners only (a
    # sandbox without Yahoo fails it at once), and the test job froze the
    # runner with no log. A test that wants Yahoo stubs the function above it.
    from curl_cffi import requests as curl_requests

    real_request = curl_requests.Session.request

    def _offline(self, method, url, *a, **k):
        host = urlsplit(str(url)).hostname or ""
        if host in ("127.0.0.1", "localhost", "::1"):
            return real_request(self, method, url, *a, **k)
        raise curl_requests.exceptions.ConnectionError(f"no network in tests: {host}")

    monkeypatch.setattr(curl_requests.Session, "request", _offline)
    yield home
