"""convexity.migrate — user data must survive every upgrade path untouched.

Tmp dirs only: ``conftest.py`` points CONVEXITY_HOME at a per-test folder and
clears CONVEXITY_LEGACY_*, so nothing here can reach the real checkout.
"""

import hashlib
import os

import pytest

from convexity import migrate, paths

OLD = migrate._OLD


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------ step 1 (v1.13)

def test_moves_legacy_state_and_home_dir(tmp_path):
    root, home = tmp_path / "repo", tmp_path / "home"
    root.mkdir()
    (home / f".{OLD}" / "ml_model").mkdir(parents=True)
    (root / f".{OLD}_views.json").write_text('{"views": {"A": {}}}')
    (root / f".{OLD}_news.json").write_text("{}")

    assert migrate.run(root, home) == 3
    assert (root / ".convexity_views.json").read_text() == '{"views": {"A": {}}}'
    assert (root / ".convexity_news.json").exists()
    assert (home / ".convexity" / "ml_model").is_dir()
    assert not (root / f".{OLD}_views.json").exists()
    # Idempotent: a second run is a no-op.
    assert migrate.run(root, home) == 0


def test_never_overwrites_existing_new_file(tmp_path):
    (tmp_path / f".{OLD}_views.json").write_text("old")
    (tmp_path / ".convexity_views.json").write_text("new")
    assert migrate.run(tmp_path, tmp_path) == 0
    assert (tmp_path / ".convexity_views.json").read_text() == "new"
    assert (tmp_path / f".{OLD}_views.json").read_text() == "old"


# ------------------------------------------------------- step 2 (v1.14)

@pytest.fixture
def legacy(tmp_path):
    root, home, data = tmp_path / "repo", tmp_path / "home", tmp_path / "data"
    root.mkdir()
    (root / ".convexity_views.json").write_text('{"views": {"Tech": {}}}')
    (root / ".convexity_watchlists.json").write_text('{"Tech": "AAPL, MSFT"}')
    os.chmod(root / ".convexity_watchlists.json", 0o600)
    (root / "symbol_db.sqlite").write_bytes(b"SQLite format 3\x00" + b"x" * 100)
    (root / "symbol_db.sqlite-shm").write_bytes(b"\x00" * 32)
    (root / "symbol_db.sqlite-wal").write_bytes(b"")
    (root / ".finnhub_key").write_text("k")
    model = home / ".convexity" / "ml_model" / "mlsent-v1.1"
    model.mkdir(parents=True)
    (model / "model.lgbm.txt").write_text("tree")
    (model / "meta.json").write_text("{}")
    return root, home, data


def test_copy_verify_remove(legacy):
    root, home, data = legacy
    views_sha = _sha(root / ".convexity_views.json")
    rep = migrate.migrate_to_data_dir(root, home, data)
    assert not rep.errors and not rep.conflicts
    assert _sha(data / "state" / "views.json") == views_sha
    assert (data / "state" / "watchlists.json").stat().st_mode & 0o777 == 0o600
    assert (data / "symbol_db.sqlite").read_bytes().startswith(b"SQLite")
    assert (data / "models" / "mlsent-v1.1" / "model.lgbm.txt").read_text() == "tree"
    # sources gone, empty legacy dirs pruned, key files never touched
    assert not (root / ".convexity_views.json").exists()
    assert not (root / "symbol_db.sqlite").exists()
    assert not (root / "symbol_db.sqlite-shm").exists()
    assert not (home / ".convexity").exists()
    assert (root / ".finnhub_key").read_text() == "k"
    assert not [p for p in data.rglob("*") if migrate._TMP_TAG in p.name]
    # idempotent
    rep2 = migrate.migrate_to_data_dir(root, home, data)
    assert rep2.moved == 0 and not rep2.errors


def test_dry_run_changes_nothing(legacy):
    root, home, data = legacy
    before = sorted(p.name for p in root.iterdir())
    rep = migrate.migrate_to_data_dir(root, home, data, dry_run=True)
    assert len(rep.planned) == 4  # views, watchlists, symbol_db, model dir
    assert sorted(p.name for p in root.iterdir()) == before
    assert not data.exists()


def test_never_overwrites_a_differing_destination(legacy):
    root, home, data = legacy
    (data / "state").mkdir(parents=True)
    (data / "state" / "views.json").write_text("newer data")
    rep = migrate.migrate_to_data_dir(root, home, data)
    assert len(rep.conflicts) == 1
    assert (data / "state" / "views.json").read_text() == "newer data"
    assert (root / ".convexity_views.json").exists()  # source kept


def test_identical_destination_removes_source(legacy):
    """Crash between placing the copy and removing the source."""
    root, home, data = legacy
    (data / "state").mkdir(parents=True)
    (data / "state" / "views.json").write_bytes((root / ".convexity_views.json").read_bytes())
    rep = migrate.migrate_to_data_dir(root, home, data)
    assert any("views" in d for d in rep.deduped)
    assert not (root / ".convexity_views.json").exists()


def test_leftover_temp_from_dead_run_is_swept(legacy):
    root, home, data = legacy
    (data / "state").mkdir(parents=True)
    dead = data / "state" / f".views.json{migrate._TMP_TAG}999999"
    dead.write_text("half a copy")
    stage = data / "models"
    (stage / f".mlsent-v1.1{migrate._TMP_TAG}999999").mkdir(parents=True)
    rep = migrate.migrate_to_data_dir(root, home, data)
    assert not rep.errors
    assert not dead.exists()
    assert not [p for p in data.rglob("*") if migrate._TMP_TAG in p.name]
    assert (data / "state" / "views.json").read_text() == '{"views": {"Tech": {}}}'


def test_bad_copy_keeps_source(legacy, monkeypatch):
    root, home, data = legacy
    real = migrate._copy_to_temp

    def corrupt(src, tmp):
        real(src, tmp)
        with open(tmp, "ab") as fh:
            fh.write(b"!")

    monkeypatch.setattr(migrate, "_copy_to_temp", corrupt)
    rep = migrate.migrate_to_data_dir(root, home, data)
    assert rep.errors and rep.moved == 0
    assert (root / ".convexity_views.json").exists()
    assert (home / ".convexity" / "ml_model" / "mlsent-v1.1" / "meta.json").exists()
    assert not (data / "state" / "views.json").exists()
    assert not (data / "models" / "mlsent-v1.1").exists()  # no partial model


def test_source_changed_mid_migration_is_kept(legacy, monkeypatch):
    root, home, data = legacy
    real = migrate._place_no_clobber

    def place_then_write(tmp, dst):
        real(tmp, dst)
        if dst.name == "views.json":
            (root / ".convexity_views.json").write_text("written by an old process")

    monkeypatch.setattr(migrate, "_place_no_clobber", place_then_write)
    rep = migrate.migrate_to_data_dir(root, home, data)
    assert any("views" in k for k in rep.kept)
    assert (root / ".convexity_views.json").read_text() == "written by an old process"


def test_live_sqlite_writer_is_skipped(legacy):
    root, home, data = legacy
    (root / "symbol_db.sqlite-wal").write_bytes(b"pending")
    rep = migrate.migrate_to_data_dir(root, home, data)
    assert rep.skipped and (root / "symbol_db.sqlite").exists()
    assert not (data / "symbol_db.sqlite").exists()


def test_auto_run_is_gated_by_convexity_home(legacy, monkeypatch):
    """CONVEXITY_HOME alone (tests, dev runs) must never migrate anything."""
    root, home, data = legacy
    monkeypatch.setenv("CONVEXITY_HOME", str(data))
    assert migrate._legacy_sources() == (None, None)
    migrate.run()
    assert (root / ".convexity_views.json").exists() and not data.exists()
    # Naming only the root does not drag the real ~/.convexity along.
    monkeypatch.setenv("CONVEXITY_LEGACY_ROOT", str(root))
    assert migrate._legacy_sources() == (root, None)
    migrate.run()
    assert (data / "state" / "views.json").exists()
    assert (home / ".convexity" / "ml_model" / "mlsent-v1.1").is_dir()
    monkeypatch.setenv("CONVEXITY_LEGACY_HOME", str(home))
    migrate.run()
    assert (data / "models" / "mlsent-v1.1" / "meta.json").exists()


def test_cli_dry_run(legacy, monkeypatch, capsys):
    root, home, data = legacy
    monkeypatch.setenv("CONVEXITY_HOME", str(data))
    monkeypatch.setenv("CONVEXITY_LEGACY_ROOT", str(root))
    monkeypatch.setenv("CONVEXITY_LEGACY_HOME", str(home))
    assert migrate._main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would migrate" in out and not data.exists()
    assert paths.data_dir() == data


def test_only_an_app_launch_migrates(monkeypatch):
    """A plain `import convexity` (scripts, python -c, pytest) never migrates."""
    import sys

    import convexity
    cases = [
        (["python", "-m", "convexity"], ["-m"], True),
        (["python", "-m", "convexity.desktop"], ["-m"], True),
        (["python", "-m", "convexity.migrate", "--dry-run"], ["-m"], False),
        (["python", "scripts/check_dependency_manifests.py"], ["scripts/check_dependency_manifests.py"], False),
        (["python", "-c", "import convexity"], ["-c"], False),
        (["/venv/bin/python", "/venv/bin/convexity"], ["/venv/bin/convexity"], True),
        (["pythonw", "/env/Scripts/convexity-app.exe"], ["/env/Scripts/convexity-app.exe"], True),
        (["python", "dashboard.py"], ["dashboard.py"], True),
        (["python", "-m", "pytest"], ["-m"], False),
    ]
    for orig, argv, want in cases:
        monkeypatch.setattr(sys, "orig_argv", orig)
        monkeypatch.setattr(sys, "argv", argv)
        assert convexity._launched_as_app() is want, orig
