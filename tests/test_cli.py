"""The `convexity` command (cli.py): server by default, builder subcommands."""

from convexity import cli


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
