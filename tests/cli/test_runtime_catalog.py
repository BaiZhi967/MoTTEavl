import json

from motte_cli.main import main


def test_cli_catalog_reports_actual_interactive_runtime_version(monkeypatch, capsys):
    monkeypatch.setattr('motte_harness.compatibility.probed_readiness', lambda name: {
        'installed': False, 'protocol_ready': False, 'execution_ready': False, 'reasons': {},
    })
    assert main(['runtime', '--json', 'list']) == 0
    items = json.loads(capsys.readouterr().out)['items']
    current = next(item for item in items if item['name'] == 'codex-app-server')
    assert current['version'] == '2'
    assert current['interactive'] is True
