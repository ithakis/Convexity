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


def test_secret_order_env_then_config(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    assert helpers._load_local_secret("FINNHUB_API_KEY") == ""
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text(json.dumps({"finnhub_api_key": " cfg-value "}))
    assert helpers._load_local_secret("FINNHUB_API_KEY") == "cfg-value"

    monkeypatch.setenv("FINNHUB_API_KEY", "env-value")
    assert helpers._load_local_secret("FINNHUB_API_KEY") == "env-value"


def test_bad_config_is_ignored(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    paths.config_file().parent.mkdir(parents=True)
    paths.config_file().write_text("{not json")
    assert helpers._config_secret("NVIDIA_API_KEY") == ""


def test_model_dir_is_the_data_dir_unless_overridden(tmp_path, monkeypatch):
    monkeypatch.delenv("MLSENT_MODEL_DIR", raising=False)
    assert ml_sentiment.model_dir() == paths.models_dir() / ml_sentiment.ARTIFACT_VERSION
    monkeypatch.setenv("MLSENT_MODEL_DIR", str(tmp_path / "x"))
    assert ml_sentiment.model_dir() == tmp_path / "x"


def test_symbol_db_lives_in_the_data_folder():
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
