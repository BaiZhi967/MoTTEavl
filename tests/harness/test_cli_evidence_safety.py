import json

import pytest

from motte_harness.parsers.codex import parse_codex_exec_events
from motte_sdk.cli_runtime import CliRuntimeCaseExecutor
from motte_sdk.execution_backends import ExecutionBackendError
from motte_sdk.pi_runtime import RuntimeCaseWorkspace
from motte_storage.artifacts import ArtifactStore


@pytest.mark.parametrize('kind,fields', [
    ('mcp_tool_call', {'server': 'local', 'tool': 'remove', 'arguments': {'path': 'locked.txt'}}),
    ('file_change', {'changes': [{'path': 'locked.txt', 'kind': 'delete'}]}),
    ('web_search', {'query': 'private query'}),
])
def test_native_tools_survive_parser_to_observation(tmp_path, monkeypatch, kind, fields):
    monkeypatch.setenv('ARTIFACT_ROOT', str(tmp_path / 'artifacts'))
    stream = '\n'.join(json.dumps(e) for e in [
        {'type': 'item.started', 'item': {'id': 'tool-1', 'type': kind, **fields}},
        {'type': 'item.completed', 'item': {'id': 'tool-1', 'type': kind,
                                            'status': 'completed', **fields}},
        {'type': 'turn.completed'},
    ])
    parsed = parse_codex_exec_events(stream)
    executor = CliRuntimeCaseExecutor({'id': 'run-1'}, backend='codex-cli')
    workspace = RuntimeCaseWorkspace('run-1', 'case-1', anchor=tmp_path / 'ws')
    observation = executor._capture(workspace, {'case_id': 'case-1'}, parsed,
        {'status': 'exited'}, workspace.snapshot(), 'session', 'operation')
    assert len(observation['tool_calls']) == 1
    assert observation['tool_calls'][0]['tool_name'] == kind
    assert observation['tool_calls'][0]['call_id'] == 'tool-1'
    assert observation['coverage']['complete'] is True


@pytest.mark.parametrize('item', [{'type': 'future_tool', 'id': 'x'}, None])
def test_unknown_item_downgrades_trajectory(item):
    parsed = parse_codex_exec_events(json.dumps({'type': 'item.completed', 'item': item})
                                     + '\n' + json.dumps({'type': 'turn.completed'}))
    assert parsed['tool_trajectory'] == 'partial'


@pytest.mark.parametrize('item', [
    {'id': 'x', 'type': 'file_change'},
    {'id': 'x', 'type': 'command_execution', 'command': 'false', 'status': 'in_progress'},
    {'id': 'x', 'type': 'mcp_tool_call', 'server': 'local'},
    {'id': 'x', 'type': 'file_change', 'status': []},
    {'id': 'x', 'type': 'file_change', 'status': 'completed', 'changes': [{'path': 'x', 'kind': {}}]},
])
def test_malformed_completed_item_is_not_successful_or_complete(item):
    parsed = parse_codex_exec_events(json.dumps({'type': 'item.completed', 'item': item})
                                     + '\n' + json.dumps({'type': 'turn.completed'}))
    assert parsed['tool_calls'][0]['status'] != 'succeeded'
    assert parsed['tool_trajectory'] == 'partial'


def test_raw_evidence_cannot_be_overwritten_by_task_or_later_capture(tmp_path, monkeypatch):
    root = tmp_path / 'artifacts'
    monkeypatch.setenv('ARTIFACT_ROOT', str(root))
    executor = CliRuntimeCaseExecutor({'id': 'run-1'}, backend='codex-cli')
    workspace = RuntimeCaseWorkspace('run-1', 'case-1', anchor=tmp_path / 'ws')
    before = workspace.snapshot()
    workspace.materialize_fixture({'raw-stdout': 'replacement', 'raw-stderr': 'replacement'})
    parsed, refs = executor._freeze_raw_evidence({}, 'original', 'diagnostic', 'case-1', [])
    executor._capture(workspace, {'case_id': 'case-1'}, parsed,
                      {'status': 'exited'}, before, 'session', 'operation', refs)
    executor._freeze_raw_evidence({}, 'later stdout', 'later stderr', 'case-1', [])
    assert ArtifactStore(root).read_bytes(refs[0].locator) == b'original'
    assert ArtifactStore(root).read_bytes(refs[1].locator) == b'diagnostic'


@pytest.mark.parametrize('budgets', [{'total_timeout': float('nan')}, {'max_cost_usd': 0},
                                    {'idle_timeout': 0}, {'alien': 1}])
def test_cli_preflight_rejects_unenforceable_budgets(budgets):
    executor = CliRuntimeCaseExecutor({'id': 'r', 'manifest': {
        'runtime_profile': {'budgets': budgets}}}, backend='codex-cli')
    with pytest.raises(ExecutionBackendError):
        executor._preflight()


@pytest.mark.parametrize('value', ['nan', 'inf', '0', '-1', ''])
def test_cli_preflight_rejects_invalid_environment_timeouts(monkeypatch, value):
    monkeypatch.setenv('MOTTE_CLI_TOTAL_TIMEOUT', value)
    with pytest.raises(ExecutionBackendError):
        CliRuntimeCaseExecutor({'id': 'r'}, backend='codex-cli')._preflight()

def test_dispatch_records_actual_case_attempt_id(tmp_path, monkeypatch):
    import motte_harness.session as sessions
    from tests.integration.test_external_runtime_slice import _cli_manifest, _dispatch, _fake_cli

    anchor = tmp_path / 'sessions'
    monkeypatch.setenv('MOTTE_RUNTIME_SESSION_ROOT', str(anchor))
    monkeypatch.setattr(sessions, 'SESSION_STORE_ROOT', anchor)
    binary = _fake_cli(tmp_path, 'codex-cli')
    service, finished = _dispatch(tmp_path, monkeypatch, 'codex-cli',
        _cli_manifest('codex-cli', binary), 'attempt-identity')
    assert finished['status'] == 'completed'
    attempt = service.store.attempts.list_for_run(finished['id'])[0]
    record = sessions.load_session(next(anchor.glob('*/*/*.json')))
    assert record['attempt_id'] == attempt['id']
    case = service.store.case_runs.list_for_run(finished['id'])[0]
    assert case['result']['observation']['attempt_id'] == attempt['id']


def test_unconfirmed_callback_retains_workspace_for_review(tmp_path, monkeypatch):
    import motte_harness.codex
    from tests.integration.test_external_runtime_slice import _cli_manifest, _dispatch, _fake_cli

    anchor = tmp_path / 'sessions'
    monkeypatch.setenv('MOTTE_RUNTIME_SESSION_ROOT', str(anchor))
    def incomplete(self, *args, **kwargs):
        return {'process': {'status': 'exited', 'exit_code': 0, 'truncated': True,
                            'detail': 'reader_drain_timeout;callback_unconfirmed'},
                'stdout': '', 'stderr': '', 'parsed': None}
    monkeypatch.setattr(motte_harness.codex.CodexHarness, 'run_batch', incomplete)
    binary = _fake_cli(tmp_path, 'codex-cli')
    service, finished = _dispatch(tmp_path, monkeypatch, 'codex-cli',
        _cli_manifest('codex-cli', binary), 'callback-uncertain')
    assert finished['status'] == 'needs_review'
    assert (tmp_path / 'ws' / finished['id'] / 'case-good' / 'seed.txt').exists()
    assert service.store.attempts.list_open(finished['id'])[0]['status'] == 'indeterminate'
