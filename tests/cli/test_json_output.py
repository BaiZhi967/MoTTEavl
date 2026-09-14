from motte_cli.main import main


def test_cli_json_output(capsys):
    assert main(["doctor", "--json"]) == 0
    assert '"status": "ok"' in capsys.readouterr().out
