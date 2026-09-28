"""The `convexity` command (cli.py): server by default, `build-symbols` subcommand."""

from convexity import cli
from convexity import symbol_db as sdb


def test_no_arguments_runs_the_server(monkeypatch):
    import convexity.server as server

    calls = []
    monkeypatch.setattr(server, "main", lambda: calls.append("served"))
    assert cli.main([]) == 0
    assert calls == ["served"]


def test_unknown_command_is_an_error(monkeypatch, capsys):
    import convexity.server as server

    monkeypatch.setattr(
        server, "main", lambda: (_ for _ in ()).throw(AssertionError("server started"))
    )
    assert cli.main(["serve-please"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_help_lists_build_symbols(capsys):
    assert cli.main(["--help"]) == 0
    assert "build-symbols" in capsys.readouterr().out


def test_build_symbols_rejects_an_unknown_source(tmp_path, capsys):
    db = tmp_path / "syms.sqlite"
    assert cli.main(["build-symbols", "--sources", "nope", "--db", str(db)]) == 2
    assert "unknown source" in capsys.readouterr().err
    assert not db.exists()


def test_build_symbols_writes_the_requested_db(tmp_path, monkeypatch):
    def stub_source(session):
        yield sdb.SymbolRow(ticker="AAPL", name="Apple Inc.", exchange="NASDAQ")
        yield sdb.SymbolRow(ticker="MSFT", name="Microsoft Corporation", exchange="NASDAQ")

    monkeypatch.setattr(sdb, "SOURCES", {"stub": stub_source})
    db = tmp_path / "syms.sqlite"
    assert cli.main(["build-symbols", "--sources", "stub", "--db", str(db)]) == 0
    assert sdb.db_stats(db)["total"] == 2


def test_default_db_is_the_data_folder_one(tmp_path, monkeypatch):
    """No --db: the builder writes symbol_db.write_path() (the data folder),
    never the legacy checkout location."""
    target = tmp_path / "data" / "symbol_db.sqlite"
    monkeypatch.delenv("PORTFOLIO_SYMBOL_DB", raising=False)
    monkeypatch.setattr(sdb, "_DB_PATH", target)
    monkeypatch.setattr(sdb, "SOURCES", {"stub": lambda session: iter(())})
    assert cli.main(["build-symbols", "--sources", "stub"]) == 0
    assert target.exists()
