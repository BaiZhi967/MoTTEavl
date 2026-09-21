import os
import subprocess
import sys
import time
import json
import pytest

from motte_harness.session import new_session_record, persist_session, session_path


def test_session_cas_serializes_independent_processes(tmp_path):
    record = new_session_record(run_id='r', case_id='c', attempt_id='a',
                                backend='codex-cli', argv=['codex'])
    path = session_path(tmp_path, 'r', 'c', record['session_id'])
    persist_session(path, record)
    script = tmp_path / 'writer.py'
    script.write_text('''
import os, sys, time
from pathlib import Path
from motte_harness.session import load_session, persist_session, SessionCasError
path = Path(sys.argv[1])
record = load_session(path)
record.update(revision=2, state=sys.argv[2])
replace = os.replace
def slow_replace(source, target):
    time.sleep(0.4)
    replace(source, target)
os.replace = slow_replace
Path(sys.argv[3]).touch()
while not Path(sys.argv[4]).exists():
    time.sleep(0.005)
try:
    persist_session(path, record)
    print('won')
except SessionCasError:
    print('conflict')
''', encoding='utf-8')
    gate = tmp_path / 'gate'
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(sys.path)
    processes = [subprocess.Popen([sys.executable, str(script), str(path), state,
                                   str(tmp_path / state), str(gate)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for state in ('spawned', 'terminal')]
    deadline = time.monotonic() + 10
    while not all((tmp_path / state).exists() for state in ('spawned', 'terminal')):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    gate.touch()
    output = [process.communicate(timeout=10) for process in processes]
    assert all(process.returncode == 0 for process in processes), output
    assert sorted(stdout.decode().strip() for stdout, _ in output) == ['conflict', 'won']


@pytest.mark.parametrize("corruption", [None, "json", "schema", "identity", "pid"])
@pytest.mark.parametrize("attempt_state", ["prepared", "dispatching"])
def test_worker_consumes_sessions_before_safe_replay(tmp_path, monkeypatch, corruption, attempt_state):
    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_sdk.service import RunService
    from motte_storage.run_store import SQLiteRunStore
    from tests.runtime.test_worker_loop import REPLAY_MANIFEST

    anchor = tmp_path / 'sessions'
    monkeypatch.setenv('MOTTE_RUNTIME_SESSION_ROOT', str(anchor))
    service = RunService(SQLiteRunStore(tmp_path / 'runs.db'))
    run = service.create_run('replay@1', REPLAY_MANIFEST, case_ids=['case-1'])
    claimed = service.store.runs.claim(run['id'])
    if attempt_state == "prepared":
        # Crash at the earlier boundary: a prepared CaseAttempt plus session
        # evidence can still hide a spawn before its bookkeeping completed.
        attempt = service.store.attempts.begin(
            {'run_id': run['id'], 'case_id': 'case-1', 'attempt_no': 1},
            expected_run_revision=claimed['revision'], expected_run_status='preparing',
        )
    else:
        attempt = service._begin_case_attempt(claimed, 'case-1')
    record = new_session_record(run_id=run['id'], case_id='case-1', attempt_id=attempt['id'],
                                backend='codex-cli', argv=['codex'])
    path = session_path(anchor, run['id'], 'case-1', record['session_id'])
    persist_session(path, record)
    if corruption:
        broken = {**record}
        if corruption == "schema":
            broken["schema_version"] = 999
        if corruption == "identity":
            broken["run_id"] = "wrong-owner"
        if corruption == "pid":
            broken["pid"] = "invalid-pid"
        path.write_text("{broken" if corruption == "json" else json.dumps(broken), encoding="utf-8")
    requeued = WorkerLoop(service).recover_interrupted()
    assert requeued == []
    recovered = service.store.runs.get(run['id'])
    assert recovered['status'] == 'needs_review'
    finding = recovered['error']['details']['runtime_sessions'][0]
    if not corruption:
        assert finding['attempt_id'] == attempt['id']
    assert finding['replayed'] is False
    assert finding['action'] == 'needs_review'

def test_cas_refuses_to_overwrite_corrupted_session(tmp_path):
    import pytest
    from motte_harness.session import SessionCasError

    path = tmp_path / 'broken.json'
    path.write_text('{broken', encoding='utf-8')
    record = new_session_record(run_id='r', case_id='c', attempt_id='a',
                                backend='codex-cli', argv=['codex'])
    with pytest.raises(SessionCasError):
        persist_session(path, record)
    assert path.read_text(encoding='utf-8') == '{broken'


def test_terminal_session_cannot_revert_to_spawned(tmp_path):
    import pytest
    from motte_harness.session import SessionCasError, mark_terminal

    record = new_session_record(run_id='r', case_id='c', attempt_id='a',
                                backend='codex-cli', argv=['codex'])
    path = tmp_path / 'terminal.json'
    record = persist_session(path, record)
    record = mark_terminal(record, 'completed', path=path)
    with pytest.raises(SessionCasError):
        persist_session(path, {**record, 'revision': 3, 'state': 'spawned'})
