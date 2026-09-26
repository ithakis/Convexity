"""convexity.migrate — pre-rename state must survive the upgrade untouched."""

from convexity import migrate

OLD = migrate._OLD


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
