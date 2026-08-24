from __future__ import annotations


def test_cli_dispatches_serve_without_reparsing_server_options(monkeypatch) -> None:
    from cadscene.cli import main as cli_main

    monkeypatch.setattr(
        cli_main.serve_viewer,
        "main",
        lambda argv: 23 if argv == ["--port", "9001"] else 99,
    )

    assert cli_main.main(["serve", "--port", "9001"]) == 23


def test_cli_dispatches_doctor_without_reparsing_doctor_options(monkeypatch) -> None:
    from cadscene.cli import doctor, main as cli_main

    monkeypatch.setattr(
        doctor,
        "main",
        lambda argv: 17 if argv == ["--json"] else 99,
    )

    assert cli_main.main(["doctor", "--json"]) == 17


def test_cli_rejects_an_unknown_or_missing_command(capsys) -> None:
    from cadscene.cli import main as cli_main

    assert cli_main.main([]) == 2
    assert cli_main.main(["unknown"]) == 2
    assert "serve" in capsys.readouterr().err
