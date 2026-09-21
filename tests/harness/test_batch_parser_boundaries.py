import json

import pytest

from motte_harness.parsers.claude import parse_claude_batch
from motte_harness.parsers.codex import parse_codex_exec_events
from motte_sdk.cli_runtime import CliRuntimeCaseExecutor
from motte_sdk.pi_runtime import RuntimeCaseWorkspace


@pytest.mark.parametrize('events', [
    [{'type': 'turn.completed'}, {'type': 'turn.completed'}],
    [{'type': 'turn.failed'}, {'type': 'turn.completed'}],
    [{'type': 'thread.started', 'thread_id': 'a'},
     {'type': 'thread.started', 'thread_id': 'b'}, {'type': 'turn.completed'}],
    [{'type': 'turn.completed'}, {'type': 'item.completed',
      'item': {'type': 'agent_message', 'text': 'late'}}],
])
def test_conflicting_lifecycle_never_claims_complete_success(events):
    parsed = parse_codex_exec_events('\n'.join(map(json.dumps, events)))
    assert parsed['coverage'] == 'partial'
    assert parsed['status'] != 'final'


@pytest.mark.parametrize('value', [[], {}, None, 42])
def test_malformed_discriminators_keep_bounded_partial_evidence(value):
    parsed = parse_codex_exec_events(json.dumps({'type': 'item.completed', 'item': {'type': value}}))
    assert parsed['coverage'] == 'partial'
    parsed = parse_claude_batch(json.dumps({'type': 'result', 'subtype': value, 'result': 'ok'}))
    assert parsed['status'] == 'insufficient'


@pytest.mark.parametrize('usage,total', [
    ({'input_tokens': 17}, None), ({'output_tokens': 0}, None),
    ({'input_tokens': -10, 'output_tokens': 1}, None),
    ({'input_tokens': True, 'output_tokens': 1}, None),
    ({'input_tokens': 0, 'output_tokens': 0}, 0),
    ({'input_tokens': 17, 'output_tokens': 3}, 20),
])
def test_partial_or_invalid_usage_remains_unknown_in_observation(tmp_path, monkeypatch, usage, total):
    monkeypatch.setenv('ARTIFACT_ROOT', str(tmp_path / 'artifacts'))
    parsed = parse_codex_exec_events(json.dumps({'type': 'turn.completed', 'usage': usage}))
    executor = CliRuntimeCaseExecutor({'id': 'r'}, backend='codex-cli')
    workspace = RuntimeCaseWorkspace('r', 'c', anchor=tmp_path / 'workspace')
    observation = executor._capture(workspace, {'case_id': 'c'}, parsed,
                                    {'status': 'exited'}, workspace.snapshot(), 's', 'op')
    assert observation['usage']['total_tokens'] == total


@pytest.mark.parametrize('cost', [-1, float('nan'), float('inf'), True])
def test_invalid_native_cost_is_unknown(cost):
    parsed = parse_claude_batch(json.dumps({'type': 'result', 'subtype': 'success',
                                          'result': 'ok', 'total_cost_usd': cost}))
    assert parsed['cost_usd'] is None
