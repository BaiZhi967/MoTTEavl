"""Session bindings, expiry, atomic receipt and restart semantics (zero-model)."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_sdk.commands import CommandError, intervention_summary, submit_command
from motte_sdk.service import RunService
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore


def bound_service(store):
    service = RunService(store)
    run = service.create_run('interactive@1', {'execution': {'backend_id': 'codex-app-server',
        'backend_version': '2', 'capabilities': {'interactive': True, 'safe_to_repeat': False}}}, ['c'])
    store.runs.update({**run, 'status': 'running'}, expected_revision=run['revision'], expected_status='queued')
    session = store.runtime_sessions.create({'session_id': 's', 'run_id': run['id'], 'case_id': 'c',
        'attempt_id': 'a', 'start_token': 'start', 'operation_id': 'op', 'worker_token': 'w',
        'state': 'active', 'native_thread_id': 'thread', 'active_turn_id': 'turn'})
    return service, run['id'], session


def submit(service, run_id, **changes):
    args = {'kind': 'user_message', 'content': 'hi', 'case_id': 'c', 'session_id': 's',
        'dedupe_key': 'key', 'expected_session_revision': 1}
    args.update(changes)
    return submit_command(service, run_id, **args)


def test_commands_dedupe_keep_deadline_and_reject_changed_intent():
    service, run_id, _ = bound_service(InMemoryRunStore())
    first = submit(service, run_id)
    second = submit(service, run_id, expires_at=datetime.now(UTC) + timedelta(minutes=2))
    assert first == second
    with pytest.raises(CommandError, match='intent'):
        submit(service, run_id, content='changed')
    assert len(service.store.commands.list_for_run(run_id)) == 1
    assert len([e for e in service.store.events.list_for_run(run_id) if e['type'] == 'command_submitted']) == 1


@pytest.mark.parametrize('change', [
    {'case_id': 'wrong'}, {'session_id': 'wrong'}, {'expected_session_revision': 2},
    {'expires_at': datetime(2020, 1, 1, tzinfo=UTC)}, {'expires_at': datetime(2020, 1, 1)},
    {'case_id': None}, {'kind': 'approve', 'content': None, 'payload': {'approval_id': 'a', 'request_hash': 'h'}},
])
def test_invalid_binding_and_expiry_never_create_command(change):
    service, run_id, _ = bound_service(InMemoryRunStore())
    with pytest.raises(CommandError):
        submit(service, run_id, **change)
    assert service.store.commands.list_for_run(run_id) == []


def test_worker_restart_never_replays_claimed_or_queued_commands(tmp_path):
    path = tmp_path / 'recovery.db'
    service, run_id, _ = bound_service(SQLiteRunStore(path))
    first = submit(service, run_id)
    second = submit(service, run_id, dedupe_key='queued')
    claimed = service.store.commands.claim(first['id'], expected_revision=1, session_id='s',
        expected_control_revision=1, worker_token='w', now=datetime.now(UTC))
    assert claimed['status'] == 'delivered'
    reopened = RunService(SQLiteRunStore(path))
    WorkerLoop(reopened).recover_interrupted()
    assert reopened.store.commands.get(first['id'])['status'] == 'delivery_unknown'
    assert reopened.store.commands.get(second['id'])['status'] == 'rejected'
    assert reopened.store.runtime_sessions.get('s')['state'] == 'abandoned'
    WorkerLoop(reopened).recover_interrupted()
    assert reopened.store.commands.get(first['id'])['revision'] == 3
    assert intervention_summary(reopened, run_id)['possible'] is True


def test_noninteractive_409_unimplemented_501_and_invalid_dto_422():
    application = create_app(store=InMemoryRunStore())
    client = TestClient(application)
    service = application.state.run_service
    body = {'kind': 'user_message', 'content': 'hi', 'case_id': 'c', 'session_id': 's',
        'expected_session_revision': 1, 'dedupe_key': 'key'}
    for interactive, version, expected in [(False, '1', 409), (True, '1', 501), (True, '2', 409)]:
        run = service.create_run('interactive@1', {'execution': {'backend_id': 'codex-app-server',
            'backend_version': version, 'capabilities': {'interactive': interactive}}}, ['c'])
        response = client.post(f"/api/v1/runs/{run['id']}/messages", json=body)
        assert response.status_code == expected, response.text
        assert service.store.commands.list_for_run(run['id']) == []
    response = client.post(f"/api/v1/runs/{run['id']}/messages", json={'content': 'unbound'})
    assert response.status_code == 422
