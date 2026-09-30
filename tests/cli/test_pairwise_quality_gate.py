"""Existing versioned Gate command has identical local/server policy semantics."""
import json
import pytest
from motte_cli import main as cli
from motte_sdk import MotteClient
from motte_sdk.comparisons import ComparisonService
from tests.api.test_pairwise_quality_gate import environment
from tests.sdk.sync_asgi import SyncASGITransport
from tests.evaluators.test_pairwise_quality_gate import policy, rule


@pytest.mark.parametrize('mode', ['local', 'server'])
def test_pairwise_policy_cli_rejects_missing_threshold_and_evaluates_missing_source(mode, tmp_path, monkeypatch, capsys):
    store, client = environment()
    monkeypatch.setattr(cli, '_comparison_service', lambda args: ComparisonService(store))
    monkeypatch.setattr(cli.remote, 'build_client', lambda args: MotteClient(
        'http://testserver', transport=SyncASGITransport(client.app)))
    flags = ['--mode', mode] + (['--api-url', 'http://testserver'] if mode == 'server' else [])
    path = tmp_path / 'policy.json'
    bad = policy([rule(threshold=None)])
    path.write_text(json.dumps(bad))
    assert cli.main(['gate', 'policy-publish', '--policy', '@'+str(path), *flags]) == 2
    assert json.loads(capsys.readouterr().err)['error']['code'] == 'PAIRWISE_POLICY_INVALID'
    path.write_text(json.dumps(policy()))
    assert cli.main(['gate', 'policy-publish', '--policy', '@'+str(path), *flags]) == 0
    assert json.loads(capsys.readouterr().out)['policy_id'] == 'explicit'
    assert cli.main(['gate', 'evaluate', '--run', 'run', '--pass', 'pass', '--policy', 'explicit@1', *flags]) == 5
    result = json.loads(capsys.readouterr().out)
    assert result['decision'] == 'insufficient_evidence'
    assert cli.main(['gate', 'evaluate', '--run', 'run', '--policy', 'explicit@1', '--baseline', 'missing', *flags]) == 2
    assert json.loads(capsys.readouterr().err)['error']['code'] == 'PAIRWISE_POLICY_UNSUPPORTED'
