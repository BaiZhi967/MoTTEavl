from motte_cli.main import main


def test_cli_json_output(capsys):
    assert main(["doctor", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"status": "ok"' in output
    assert '"harnesses"' in output


def test_cli_doctor_reports_harness_installations(capsys):
    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "claude:" in output
    assert "codex:" in output
    assert "pi-bridge:" in output
