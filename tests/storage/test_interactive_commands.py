from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from motte_storage.integrity import RunConflictError
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore


@pytest.fixture(params=['memory', 'sqlite', 'postgres'])
def stores(request, tmp_path):
    if request.param == 'postgres':
        return request.getfixturevalue('pg_interactive_stores')
    if request.param == 'memory':
        store = InMemoryRunStore()
        return store, store
    return SQLiteRunStore(tmp_path / 'commands.db'), SQLiteRunStore(tmp_path / 'commands.db')


@pytest.fixture
def pg_interactive_stores():
    import os
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from motte_storage.pg_audit_store import PgCommands
    from motte_storage.postgres import _PgRuns, _PgTraceEvents
    from motte_storage.run_store import RunStore

    dsn = os.environ.get('MOTTE_R4_PG_DSN') or os.environ.get('MOTTE_PG_DSN')
    if not dsn:
        pytest.skip('PostgreSQL integration DSN unavailable; no PG concurrency claim')
    schema = 'm4_commands_' + uuid4().hex
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        connection.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        connection.execute('CREATE TABLE runs (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload JSONB NOT NULL)')
        connection.execute('CREATE TABLE trace_events (run_id TEXT NOT NULL, seq INTEGER NOT NULL, payload JSONB NOT NULL, PRIMARY KEY(run_id,seq))')
        connection.execute('CREATE TABLE run_commands (id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), status TEXT NOT NULL, revision INTEGER NOT NULL, payload JSONB NOT NULL)')
        connection.execute('CREATE TABLE runtime_sessions (session_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), state TEXT NOT NULL, revision INTEGER NOT NULL, payload JSONB NOT NULL)')
        connection.execute("CREATE UNIQUE INDEX commands_dedupe ON run_commands(run_id,(payload->>'dedupe_key')) WHERE payload->>'dedupe_key' IS NOT NULL")
    isolated = make_conninfo(dsn, options=f'-c search_path={schema} -c statement_timeout=15000')
    def build():
        return RunStore(runs=_PgRuns(isolated), case_runs=None, events=_PgTraceEvents(isolated),
            scores=None, commands=PgCommands(isolated))
    try:
        yield build(), build()
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def seed(store):
    store.runs.create({'id': 'r', 'status': 'running', 'case_ids': ['c']})
    return store.runtime_sessions.create({
        'session_id': 's', 'run_id': 'r', 'case_id': 'c', 'attempt_id': 'a',
        'start_token': 'epoch', 'worker_token': 'w', 'operation_id': 'op',
        'state': 'active', 'control_revision': 1, 'native_thread_id': 'thread',
        'active_turn_id': 'turn', 'pending_approvals': {'approval': {
            'approval_id': 'approval', 'request_hash': 'hash', 'state': 'pending',
            'expires_at': (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }},
    })


def record(command_id='cmd', key='key', **changes):
    return dict({
        'id': command_id, 'run_id': 'r', 'case_id': 'c', 'session_id': 's',
        'type': 'approve', 'expected_session_revision': 1, 'dedupe_key': key,
        'intent_hash': 'intent', 'payload': {'approval_id': 'approval', 'request_hash': 'hash'},
        'expires_at': (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
    }, **changes)


def test_atomic_dedupe_and_audit(stores):
    left, right = stores
    seed(left)
    def submit(pair):
        store, command_id = pair
        return store.commands.create_or_get(record(command_id), event={'type': 'command_submitted'})
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit, [(left, 'x'), (right, 'y')]))
    assert sum(created for _, created in results) == 1
    assert results[0][0]['id'] == results[1][0]['id']
    assert len(left.commands.list_for_run('r')) == 1
    assert len(left.events.list_for_run('r')) == 1
    with pytest.raises(RunConflictError, match='intent'):
        right.commands.create_or_get(record('z', intent_hash='changed'))


def test_claim_reserves_one_native_approval(stores):
    left, right = stores
    seed(left)
    for store, cmd in [(left, 'x'), (right, 'y')]:
        store.commands.create_or_get(record(cmd, cmd))
    def claim(pair):
        store, cmd = pair
        try:
            return store.commands.claim(cmd, expected_revision=1, session_id='s',
                expected_control_revision=1, worker_token='w', now=datetime.now(UTC))
        except RunConflictError:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, [(left, 'x'), (right, 'y')]))
    assert sum(result is not None for result in results) == 1
    approval = left.runtime_sessions.get('s')['pending_approvals']['approval']
    assert approval['state'] == 'claimed'
    assert approval['claimed_command_id'] in {'x', 'y'}


def test_stale_cancel_expiry_and_identity_fail_closed(stores):
    store, other = stores
    session = seed(store)
    command, _ = store.commands.create_or_get(record())
    with pytest.raises(ValueError):
        store.commands.transition(command['id'], expected_revision=1,
            expected_status='queued', status='rejected', changes={'session_id': 'other'})
    session = store.runtime_sessions.transition('s', expected_revision=session['revision'],
        expected_state='active', changes={'state': 'terminal'})
    with pytest.raises(RunConflictError):
        other.commands.claim('cmd', expected_revision=1, session_id='s',
            expected_control_revision=1, worker_token='w', now=datetime.now(UTC))
    with pytest.raises(ValueError):
        store.runtime_sessions.transition('s', expected_revision=session['revision'],
            expected_state='terminal', changes={'case_id': 'other'})


def test_approval_action_identity_cannot_be_rewritten(stores):
    store, _ = stores
    session = seed(store)
    approvals = session['pending_approvals']
    approvals['approval']['request_hash'] = 'new action'
    with pytest.raises(ValueError, match='approval'):
        store.runtime_sessions.transition('s', expected_revision=1, expected_state='active',
            changes={'pending_approvals': approvals})


def test_claim_at_exact_expiry_and_cancel_grants_nothing(stores):
    store, _ = stores
    seed(store)
    command, _ = store.commands.create_or_get(record())
    with pytest.raises(RunConflictError, match='expired'):
        store.commands.claim(command['id'], expected_revision=1, session_id='s',
            expected_control_revision=1, worker_token='w', now=datetime.fromisoformat(command['expires_at']))
    run = store.runs.get('r')
    store.runs.update({**run, 'cancellation': {'reason': 'cancel'}},
        expected_revision=run['revision'], expected_status='running')
    with pytest.raises(RunConflictError, match='cancelling'):
        store.commands.claim(command['id'], expected_revision=1, session_id='s',
            expected_control_revision=1, worker_token='w', now=datetime.now(UTC))
    assert store.commands.get(command['id'])['status'] == 'queued'
    assert store.runtime_sessions.get('s')['pending_approvals']['approval']['state'] == 'pending'


def test_sqlite_receipt_dedupe_across_actual_processes(tmp_path):
    import subprocess
    import sys
    import json

    path = tmp_path / 'shared.db'
    store = SQLiteRunStore(path)
    seed(store)
    payload = record()
    script = '''import json,sys
from motte_storage.run_store import SQLiteRunStore
store=SQLiteRunStore(sys.argv[1])
record=json.loads(sys.argv[2]); record['id']=sys.argv[3]
result,created=store.commands.create_or_get(record,event={'type':'command_submitted'})
print(json.dumps({'id':result['id'],'created':created}))
'''
    def launch(identity):
        result = subprocess.run([sys.executable, '-c', script, str(path), json.dumps(payload), identity],
            capture_output=True, text=True, timeout=15, check=True)
        return json.loads(result.stdout)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(launch, ['first', 'second']))
    assert results[0]['id'] == results[1]['id']
    assert sum(row['created'] for row in results) == 1
    assert len(store.events.list_for_run('r')) == 1
