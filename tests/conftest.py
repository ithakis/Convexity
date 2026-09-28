"""Every test runs against a throwaway data folder — never the real one.

``CONVEXITY_HOME`` is set at import time, before any test module imports
``convexity``: several modules bind their data paths at import
(persistence._VIEWS_FILE, news_sentiment._PERSIST_FILE, ...), and the package
``__init__`` runs the data migration. With ``CONVEXITY_HOME`` set and no
``CONVEXITY_LEGACY_*`` variable, that migration touches nothing (migrate.py's
gate), so a test run can never move the real checkout's state files.

The ML model's legacy fallback (~/.convexity/ml_model) is still *read* when
present, exactly as before 1.14, so the model tests run locally.

The autouse fixture then gives each test its own folder for everything that
resolves paths lazily (paths.data_dir(), ml_sentiment.model_dir(), keys).
"""

import os
import tempfile

import pytest

for _var in ("CONVEXITY_LEGACY_ROOT", "CONVEXITY_LEGACY_HOME"):
    os.environ.pop(_var, None)
os.environ["CONVEXITY_HOME"] = tempfile.mkdtemp(prefix="convexity-test-home-")
# The symbol DB's legacy fallback would otherwise open the checkout's real
# symbol_db.sqlite (SQLite touches its -shm sidecar even read-only). No test
# depends on its contents — CI has none.
os.environ["PORTFOLIO_SYMBOL_DB"] = os.path.join(
    os.environ["CONVEXITY_HOME"], "no-symbol-db.sqlite"
)
# A server booted by a test (start_server) would otherwise start the first-run
# model download from GitHub — no real network in tests. test_model_fetch.py
# drives model_fetch directly against a local server.
os.environ["CONVEXITY_MODEL_DOWNLOAD"] = "0"
os.environ.pop("CONVEXITY_MODEL_URL", None)
# Same for the daily reference pack; test_reference_pack.py turns it on
# against a local stub.
os.environ["CONVEXITY_REFERENCE_PACK"] = "0"
os.environ.pop("CONVEXITY_REFERENCE_URL", None)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    home = tmp_path / "convexity-home"
    monkeypatch.setenv("CONVEXITY_HOME", str(home))
    monkeypatch.delenv("CONVEXITY_LEGACY_ROOT", raising=False)
    monkeypatch.delenv("CONVEXITY_LEGACY_HOME", raising=False)
    yield home
