"""convexity.paths — user data lives in a per-user folder, never beside the code."""

import json
from pathlib import Path

from convexity import helpers, ml_sentiment, paths, persistence, symbol_db


def test_override_and_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVEXITY_HOME", str(tmp_path / "h"))
    assert paths.data_dir() == tmp_path / "h"
    assert paths.state_file("views") == tmp_path / "h" / "state" / "views.json"
    assert paths.models_dir() == tmp_path / "h" / "models"
    assert paths.logs_dir() == tmp_path / "h" / "logs"
    assert paths.config_file() == tmp_path / "h" / "config.json"
    assert paths.symbol_db_file() == tmp_path / "h" / "symbol_db.sqlite"
    assert not (tmp_path / "h").exists()  # resolving a path never creates it


def test_platform_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("CONVEXITY_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert paths.data_dir() == tmp_path / "Library" / "Application Support" / "Convexity"
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert paths.data_dir() == tmp_path / ".local" / "share" / "convexity"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.data_dir() == tmp_path / "xdg" / "convexity"
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert paths.data_dir() == tmp_path / "Roaming" / "Convexity"


def test_state_files_are_in_the_data_dir_not_the_package():
    pkg = Path(persistence.__file__).resolve().parent
    for f in (
        persistence._VIEWS_FILE,
        persistence._WATCHLISTS_FILE,
        persistence._MPT_FILE,
        persistence._COLUMN_VIEWS_FILE,
    ):
        assert f.parent.name == "state"
        assert pkg not in f.parents and pkg.parent not in f.parents


def test_watchlist_save_lands_in_the_data_dir(tmp_path, monkeypatch):
    target = paths.state_file("watchlists")  # per-test CONVEXITY_HOME
    monkeypatch.setattr(persistence, "_WATCHLISTS_FILE", target)
    persistence.upsert_watchlist("Demo", "AAPL, MSFT")
    assert json.loads(target.read_text())["Demo"] == "AAPL, MSFT"


def test_secret_order_env_then_config_then_legacy(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    # legacy walk-up file: point the walk at a fake package dir
    pkg = tmp_path / "repo" / "convexity"
    pkg.mkdir(parents=True)
    (tmp_path / "repo" / ".finnhub_key").write_text("legacy-value\n")
    monkeypatch.setattr(helpers, "__file__", str(pkg / "helpers.py"))
    paths._WARNED.clear()
    assert helpers._load_local_secret("FINNHUB_API_KEY", ".finnhub_key") == "legacy-value"
    err = capsys.readouterr().err
    assert "legacy key file .finnhub_key" in err and "legacy-value" not in err

    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text(json.dumps({"finnhub_api_key": " cfg-value "}))
    assert helpers._load_local_secret("FINNHUB_API_KEY", ".finnhub_key") == "cfg-value"

    monkeypatch.setenv("FINNHUB_API_KEY", "env-value")
    assert helpers._load_local_secret("FINNHUB_API_KEY", ".finnhub_key") == "env-value"


def test_bad_config_is_ignored(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text("{not json")
    assert helpers._config_secret("NVIDIA_API_KEY") == ""


def test_model_dir_prefers_data_dir_then_logged_legacy(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MLSENT_MODEL_DIR", raising=False)
    monkeypatch.setenv("CONVEXITY_LEGACY_HOME", str(tmp_path / "oldhome"))
    ver = ml_sentiment.ARTIFACT_VERSION
    new = paths.models_dir() / ver
    assert ml_sentiment.model_dir() == new  # neither exists: name the new place
    legacy = tmp_path / "oldhome" / ".convexity" / "ml_model" / ver
    legacy.mkdir(parents=True)
    paths._WARNED.clear()
    assert ml_sentiment.model_dir() == legacy
    assert "legacy ML model dir" in capsys.readouterr().err
    new.mkdir(parents=True)
    assert ml_sentiment.model_dir() == new
    monkeypatch.setenv("MLSENT_MODEL_DIR", str(tmp_path / "x"))
    assert ml_sentiment.model_dir() == tmp_path / "x"


def test_symbol_db_prefers_data_dir_then_legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("PORTFOLIO_SYMBOL_DB", raising=False)
    monkeypatch.setattr(symbol_db, "_DB_PATH", paths.symbol_db_file())
    monkeypatch.setenv("CONVEXITY_LEGACY_ROOT", str(tmp_path / "repo"))
    (tmp_path / "repo").mkdir()
    assert symbol_db.db_path() == paths.symbol_db_file()
    (tmp_path / "repo" / "symbol_db.sqlite").write_bytes(b"")
    assert symbol_db.db_path() == tmp_path / "repo" / "symbol_db.sqlite"
    # the builder never writes to the legacy place
    assert symbol_db.write_path() == paths.symbol_db_file()
    symbol_db.init_db(symbol_db.write_path())
    assert symbol_db.db_path() == paths.symbol_db_file()


def test_malformed_config_is_logged_once(monkeypatch, capsys):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setattr(helpers, "_CONFIG_WARNED", [])
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text('{"finnhub_api_key": ')
    assert helpers._config_secret("FINNHUB_API_KEY") == ""
    assert helpers._config_secret("NVIDIA_API_KEY") == ""
    err = capsys.readouterr().err
    assert err.count("[config] ignoring") == 1
