"""Actual SQLite API/Worker + supervised subprocess, no model or credentials."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_contracts.agent_tasks import normalize_agent_tasks_dataset, scenario_for_agent_tasks
from motte_sdk.resolve import prepare_run
from motte_sdk.runtime_backends import publish_canonical_runtime_versions
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import SQLiteRunStore


FAKE_SERVER = '''import json,sys,pathlib,time
if '--version' in sys.argv:
    print('codex-cli 0.155.1'); sys.exit(0)
from jsonschema import Draft7Validator
schema=json.loads(pathlib.Path(SCHEMA_PATH).read_text())
methods={'initialize':'Initialize','account/login/start':'LoginAccount','thread/start':'ThreadStart','turn/start':'TurnStart',
    'turn/steer':'TurnSteer','turn/interrupt':'TurnInterrupt',
    'item/commandExecution/requestApproval':'CommandExecutionRequestApproval',
    'item/fileChange/requestApproval':'FileChangeRequestApproval'}
client_methods={}
def validate(name,payload):
    Draft7Validator({**schema,'$ref':'#/definitions/'+name}).validate(payload)
def read():
    x=json.loads(input())
    if x.get('method') in methods:
        client_methods[x['id']]=methods[x['method']]
        validate(methods[x['method']]+'Params',x.get('params',{}))
    return x
def emit(x):
    if 'result' in x: validate(client_methods[x['id']]+'Response',x['result'])
    elif x.get('method') in methods: validate(methods[x['method']]+'Params',x['params'])
    elif x.get('method') in ('turn/completed','serverRequest/resolved'):
        validate('TurnCompletedNotification' if x['method']=='turn/completed' else 'ServerRequestResolvedNotification',x['params'])
    print(json.dumps(x),flush=True)
def reply(req,result): emit({'id':req['id'],'result':result})
init=read(); assert init['method']=='initialize'
reply(init,{'userAgent':'fake-0.155.1','codexHome':'controlled','platformFamily':'windows','platformOs':'windows'})
assert read()=={'method':'initialized'}
req=read()
if req['method']=='account/login/start':
    import os
    assert 'CODEX_API_KEY' not in os.environ
    assert req['params']=={'type':'apiKey','apiKey':'synthetic-fixture-credential'}
    reply(req,{'type':'apiKey'})
    req=read()
elif EXPECT_LOGIN:
    raise AssertionError('configured credential was never delivered')
assert req['method']=='thread/start'
reply(req,{'thread':{'id':'native-thread','cliVersion':'0.155.1','createdAt':1,'updatedAt':1,
    'cwd':req['params']['cwd'],'ephemeral':True,'modelProvider':'openai','preview':'','projectId':None,
    'sessionId':'native-session','source':'appServer','status':{'type':'idle'},'turns':[]},
    'model':req['params']['model'],'modelProvider':'openai',
    'cwd':req['params']['cwd'],'approvalPolicy':req['params']['approvalPolicy'],'sandbox':{'type':'readOnly'},
    'approvalsReviewer':'user','instructionSources':[]})
req=read(); assert req['method']=='turn/start'
mode=req['params']['input'][0]['text']
turn={'id':'native-turn','items':[],'status':'inProgress','error':None}
reply(req,{'turn':turn})
base={'threadId':'native-thread','turnId':'native-turn'}
if mode in ('approve','reject','cancel','interrupt','file'):
    item={'id':'item','type':'commandExecution','command':'write answer.txt','cwd':str(pathlib.Path.cwd()),
        'status':'inProgress','commandActions':[],'processId':None,'aggregatedOutput':None,'exitCode':None,'durationMs':None}
    if mode=='file': item={'id':'item','type':'fileChange','status':'inProgress','changes':[{'path':'answer.txt','kind':{'type':'add'},'diff':'+approved'}]}
    emit({'method':'item/started','params':{**base,'item':item}})
    method='item/fileChange/requestApproval' if mode=='file' else 'item/commandExecution/requestApproval'
    params={**base,'itemId':'item','startedAtMs':1}
    if mode!='file': params.update(command='write answer.txt',cwd=str(pathlib.Path.cwd()))
    emit({'id':7,'method':method,'params':params})
req=read()
if 'method' in req:
    if req['method']=='turn/interrupt':
        assert req['params']=={**base}
        reply(req,{})
        turn['status']='interrupted'
    else:
        assert req['method']=='turn/steer' and req['params']['expectedTurnId']=='native-turn'
        reply(req,{'turnId':'native-turn'})
        pathlib.Path('answer.txt').write_text(req['params']['input'][0]['text'])
        turn['status']='completed'
else:
    assert req['id']==7 and req['result']['decision'] in ('accept','decline')
    if req['result']['decision']=='accept': pathlib.Path('answer.txt').write_text('approved')
    emit({'method':'serverRequest/resolved','params':{'threadId':'native-thread','requestId':7}})
    time.sleep(.15)
    item.update(status='completed')
    if mode!='file': item.update(exitCode=0,aggregatedOutput='done')
    emit({'method':'item/completed','params':{**base,'item':item}})
    turn['status']='completed'
emit({'method':'item/completed','params':{**base,'item':{'id':'answer','type':'agentMessage','text':'done','phase':'final_answer'}}})
emit({'method':'turn/completed','params':{'threadId':'native-thread','turn':turn}})
time.sleep(20)
'''


def setup(tmp_path, monkeypatch, mode, *, credential_refs=None):
    monkeypatch.setenv('ARTIFACT_ROOT', str(tmp_path / 'artifacts'))
    monkeypatch.setenv('MOTTE_PI_WORKSPACE_ROOT', str(tmp_path / 'workspaces'))
    monkeypatch.setenv('MOTTE_RUNTIME_SESSION_ROOT', str(tmp_path / 'sessions'))
    binary = tmp_path / 'fake_codex.py'
    schema_path = Path(__file__).resolve().parents[1] / 'fixtures/harness/codex-app-server-0.155.1.schema.json'
    binary.write_text(f'SCHEMA_PATH = {str(schema_path)!r}\nEXPECT_LOGIN = {bool(credential_refs)!r}\n' + FAKE_SERVER, encoding='utf-8')
    resources = InMemoryResourceStore()
    publish_canonical_runtime_versions(resources)
    dataset = normalize_agent_tasks_dataset({'name': 'interactive', 'version': '1', 'suite': 'agent-tasks',
        'cases': [{'case_id': 'c', 'input': mode, 'fixture': {},
            'expected': {'files': {'answer.txt': {'mode': 'contains', 'expected': 'approved'}}}}]})
    resources.datasets.put(dataset)
    resources.scenarios.put(scenario_for_agent_tasks(dataset))
    manifest, ids = prepare_run('interactive@1', {'runtime': 'codex-app-server@2',
        'runtime_profile': {'runtime': 'codex-app-server@2',
            'native_settings': {'model': 'fixture', 'binary': str(binary)},
            'credential_refs': credential_refs or [],
            'budgets': {'total_timeout': 15, 'idle_timeout': 10}},
        'runtime_accept_unenforced_tools': True}, [], resources=resources)
    service = RunService(SQLiteRunStore(tmp_path / 'runs.db'))
    run = service.create_run('interactive@1', manifest, ids)
    client = TestClient(create_app(SQLiteRunStore(tmp_path / 'runs.db'), resource_store=resources))
    worker_service = RunService(SQLiteRunStore(tmp_path / 'runs.db'))
    return service, run, client, WorkerLoop(worker_service)


def active_session(client, run_id, *, approval=True):
    until = monotonic() + 8
    while monotonic() < until:
        response = client.get(f'/api/v1/runs/{run_id}/sessions')
        assert response.status_code == 200, response.text
        items = response.json()['items']
        if items and items[0]['state'] == 'active' and (not approval or items[0]['pending_approvals']):
            return items[0]
        sleep(.025)
    pytest.fail('Worker did not expose an active native session')


@pytest.mark.parametrize('mode', ['approve', 'reject', 'file', 'message', 'interrupt', 'cancel'])
def test_api_worker_real_command_and_cleanup(tmp_path, monkeypatch, mode):
    service, run, client, worker = setup(tmp_path, monkeypatch, mode)
    run_id = run['id']
    with ThreadPoolExecutor(1) as pool:
        execution = pool.submit(worker.claim_and_execute)
        session = active_session(client, run_id, approval=mode != 'message')
        if mode == 'cancel':
            response = client.post(f'/api/v1/runs/{run_id}/cancel', json={'reason': 'test'})
            assert response.status_code == 200
        else:
            kind = 'approve' if mode == 'file' else 'user_message' if mode == 'message' else mode
            payload = {}
            if kind in {'approve', 'reject'}:
                approval = session['pending_approvals'][0]
                payload = {key: approval[key] for key in ('approval_id', 'request_hash')}
            body = {'kind': kind, 'case_id': 'c', 'session_id': session['session_id'],
                'expected_session_revision': session['control_revision'], 'dedupe_key': 'decision', 'payload': payload}
            if mode == 'message':
                body['content'] = 'approved by steer'
            if payload:
                wrong = client.post(f'/api/v1/runs/{run_id}/messages', json={**body,
                    'dedupe_key': 'wrong', 'payload': {**payload, 'request_hash': 'wrong'}})
                assert wrong.status_code == 409
            response = client.post(f'/api/v1/runs/{run_id}/messages', json=body)
            assert response.status_code == 202, response.text
        finished = execution.result(timeout=20)
    assert finished['status'] == ('cancelled' if mode in {'cancel', 'interrupt'} else 'completed'), finished
    if mode != 'cancel':
        commands = client.get(f'/api/v1/runs/{run_id}/commands').json()['items']
        assert len(commands) == 1
        assert commands[0]['status'] == 'acknowledged', commands
        assert commands[0]['ack_evidence']['kind'] == (
            'interrupt_requested' if mode == 'interrupt' else 'message_accepted' if mode == 'message' else 'request_resolved')
    stored_session = service.store.runtime_sessions.list_for_run(run_id)[0]
    assert stored_session['state'] == 'terminal'
    assert stored_session['cleanup']['confirmed'] is True
    attempt = service.store.attempts.list_for_run(run_id)[0]
    assert stored_session['attempt_id'] == attempt['id']
    if mode != 'cancel':
        scoring = service.store.scoring_passes.current(run_id)
        assert scoring['summary']['interventions']['count'] == 1
        assert scoring['summary']['interventions']['entries'][0]['effect'] == 'effective'
    if mode not in {'cancel', 'interrupt'}:
        observation = service.store.case_runs.list_for_run(run_id)[0]['result']['observation']
        assert observation['attempt_id'] == attempt['id']
        if mode != 'reject':
            assert any(a['path'] == 'answer.txt' and a['available'] for a in observation['artifact_refs'])
        assert observation['usage']['reported'] is False
    if mode == 'message':
        from motte_sdk.comparisons import ComparisonService

        frozen = scoring['summary']['interventions']
        reference = ComparisonService(service.store).report_ref(run_id)
        service.rescore(run_id)
        rescored = service.store.scoring_passes.current(run_id)
        assert rescored['summary']['interventions'] == frozen
        assert ComparisonService(service.store).report_ref(run_id).evidence_hash == reference.evidence_hash
        assert ComparisonService(service.store).report_ref(run_id,
            scoring_pass_id=scoring['id']) == reference


def test_native_config_preparation_failure_cleans_workspace(tmp_path, monkeypatch):
    import motte_sdk.codex_app_server_runtime as runtime

    service, run, _, worker = setup(tmp_path, monkeypatch, 'message')
    roots = []

    def broken_environment(*args, **kwargs):
        roots.append(kwargs['workspace'])
        raise RuntimeError('synthetic configuration failure')

    monkeypatch.setattr(runtime, 'prepare_native_environment', broken_environment)
    worker.claim_and_execute()
    assert roots and all(not root.exists() for root in roots)
    assert service.store.runtime_sessions.list_for_run(run['id']) == []


def test_resolution_and_terminal_in_same_frame_batch_remains_unknown(tmp_path, monkeypatch):
    # A resolved notification also means cleanup; it never proves accept execution.
    import sys
    module = sys.modules[__name__]
    altered = FAKE_SERVER.replace("time.sleep(.15)", "pass")
    # Emit resolution and terminal in one flushed write so the consumer observes both.
    altered = altered.replace(
        "emit({'method':'serverRequest/resolved','params':{'threadId':'native-thread','requestId':7}})",
        "resolution={'method':'serverRequest/resolved','params':{'threadId':'native-thread','requestId':7}}")
    altered = altered.replace("emit({'method':'turn/completed','params':{'threadId':'native-thread','turn':turn}})",
        "print(json.dumps(resolution)+'\\n'+json.dumps({'method':'turn/completed','params':{'threadId':'native-thread','turn':turn}}),flush=True)")
    monkeypatch.setattr(module, 'FAKE_SERVER', altered)
    service, run, client, worker = setup(tmp_path, monkeypatch, 'approve')
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.claim_and_execute)
        session = active_session(client, run['id'])
        approval = session['pending_approvals'][0]
        response = client.post(f"/api/v1/runs/{run['id']}/messages", json={
            'kind': 'approve', 'case_id': 'c', 'session_id': session['session_id'],
            'expected_session_revision': session['control_revision'], 'dedupe_key': 'decision',
            'payload': {k: approval[k] for k in ('approval_id', 'request_hash')}})
        assert response.status_code == 202
        future.result(timeout=20)
    command = service.store.commands.list_for_run(run['id'])[0]
    assert command['status'] == 'delivery_unknown'
    assert command.get('ack_evidence') is None


@pytest.mark.parametrize('fault', ['rpc_error', 'disconnect', 'sandbox_drift', 'version_drift'])
def test_failure_does_not_forge_command_ack_or_retry(tmp_path, monkeypatch, fault):
    import sys
    altered = FAKE_SERVER
    marker = tmp_path / 'initial-turn-written'
    altered = altered.replace("req=read(); assert req['method']=='turn/start'",
        f"req=read(); assert req['method']=='turn/start'; pathlib.Path({str(marker)!r}).write_text('dispatched')")
    if fault == 'rpc_error':
        altered = altered.replace("reply(req,{'turnId':'native-turn'})", "emit({'id':req['id'],'error':{'code':-32000,'message':'turn ended'}})")
    elif fault == 'disconnect':
        altered = altered.replace("reply(req,{'turnId':'native-turn'})", "sys.exit(9)")
    elif fault == 'sandbox_drift':
        altered = altered.replace("'sandbox':{'type':'readOnly'}", "'sandbox':{'type':'dangerFullAccess'}")
    else:
        altered = altered.replace("print('codex-cli 0.155.1')", "print('codex-cli 0.148.0')")
    monkeypatch.setattr(sys.modules[__name__], 'FAKE_SERVER', altered)
    service, run, client, worker = setup(tmp_path, monkeypatch, 'message')
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.claim_and_execute)
        if fault in {'rpc_error', 'disconnect'}:
            session = active_session(client, run['id'], approval=False)
            response = client.post(f"/api/v1/runs/{run['id']}/messages", json={
                'kind': 'user_message', 'content': 'approved', 'case_id': 'c',
                'session_id': session['session_id'], 'expected_session_revision': session['control_revision'],
                'dedupe_key': 'one'})
            assert response.status_code == 202
        result = future.result(timeout=20)
    if fault in {'rpc_error', 'disconnect'}:
        command = service.store.commands.list_for_run(run['id'])[0]
        assert command['status'] == ('failed' if fault == 'rpc_error' else 'delivery_unknown')
        assert command.get('ack_evidence') is None
    else:
        if fault == 'version_drift':
            assert result['status'] == 'unsupported'
        else:
            rows = service.store.case_runs.list_for_run(run['id'])
            assert rows[0]['result']['error']['message'] == 'effective native sandbox/cwd/approval reviewer drift'
            assert rows[0]['result']['observation']['termination']['reason'] != 'final_answer'
        assert service.store.commands.list_for_run(run['id']) == []
        assert not marker.exists(), 'must refuse before initial model turn is sent'


@pytest.mark.parametrize('echo_error', [False, True, 'settings'])
def test_explicit_dummy_credential_uses_private_login_and_never_persists_value(tmp_path, monkeypatch, echo_error):
    import json
    import sys
    if echo_error == 'settings':
        altered = FAKE_SERVER.replace("'instructionSources':[]", "'instructionSources':['synthetic-fixture-credential/instructions.md']")
        altered = altered.replace("'sandbox':{'type':'readOnly'}",
            "'sandbox':{'type':'readOnly','metadata':{'synthetic-fixture-credential':'native-echo'}}")
        monkeypatch.setattr(sys.modules[__name__], 'FAKE_SERVER', altered)
    elif echo_error:
        altered = FAKE_SERVER.replace("reply(req,{'turnId':'native-turn'})",
            "emit({'id':req['id'],'error':{'code':-32000,'message':'synthetic-fixture-credential','data':{'synthetic-fixture-credential':'echo'}}})")
        monkeypatch.setattr(sys.modules[__name__], 'FAKE_SERVER', altered)
    monkeypatch.setenv('CODEX_API_KEY', 'synthetic-fixture-credential')
    service, run, client, worker = setup(tmp_path, monkeypatch, 'message', credential_refs=['CODEX_API_KEY'])
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.claim_and_execute)
        session = active_session(client, run['id'], approval=False)
        response = client.post(f"/api/v1/runs/{run['id']}/messages", json={
            'kind': 'user_message', 'content': 'approved', 'case_id': 'c', 'session_id': session['session_id'],
            'expected_session_revision': session['control_revision'], 'dedupe_key': 'one'})
        assert response.status_code == 202
        assert future.result(timeout=20)['status'] == 'completed'
    persisted = json.dumps({'run': service.get_run(run['id']),
        'commands': client.get(f"/api/v1/runs/{run['id']}/commands").json(),
        'sessions': service.store.runtime_sessions.list_for_run(run['id']),
        'events': service.store.events.list_for_run(run['id'])})
    assert 'synthetic-fixture-credential' not in persisted
    native = service.store.runtime_sessions.list_for_run(run['id'])[0]['native_config']
    assert native['reproducibility'] == 'partial'
    assert native['credentials'] == [{'ref': 'CODEX_API_KEY', 'present': True}]


@pytest.mark.parametrize('native_id', ['native-thread', 'native-turn'])
def test_configured_credential_in_native_identity_is_rejected_not_rewritten(tmp_path, monkeypatch, native_id):
    import json
    import sys
    secret = 'synthetic-fixture-credential'
    altered = FAKE_SERVER.replace(native_id, secret)
    altered = altered.replace("base={'threadId':", "sys.exit(0)\nbase={'threadId':")
    monkeypatch.setattr(sys.modules[__name__], 'FAKE_SERVER', altered)
    monkeypatch.setenv('CODEX_API_KEY', secret)
    service, run, _, worker = setup(tmp_path, monkeypatch, 'message', credential_refs=['CODEX_API_KEY'])
    worker.claim_and_execute()
    session = service.store.runtime_sessions.list_for_run(run['id'])[0]
    key = 'native_thread_id' if native_id == 'native-thread' else 'active_turn_id'
    assert session[key] is None, 'secret-bearing identity must be rejected before persistence'
    assert secret not in json.dumps(session)
    assert session['cleanup']['confirmed'] is True
    assert service.store.commands.list_for_run(run['id']) == []


@pytest.mark.parametrize('before_item', [False, True])
@pytest.mark.parametrize('changed_field', ['command', 'cwd'])
def test_changed_native_proposal_cannot_consume_original_approval(tmp_path, monkeypatch, before_item, changed_field):
    import sys
    altered = FAKE_SERVER.replace("emit({'id':7,'method':method,'params':params})",
        f"emit({{'id':7,'method':method,'params':params}}); item[{changed_field!r}]='different dangerous action'; emit({{'method':'item/started','params':{{**base,'item':item}}}})")
    if before_item:
        altered = altered.replace("    emit({'method':'item/started','params':{**base,'item':item}})\n", '')
    monkeypatch.setattr(sys.modules[__name__], 'FAKE_SERVER', altered)
    service, run, client, worker = setup(tmp_path, monkeypatch, 'approve')
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.claim_and_execute)
        session = active_session(client, run['id'])
        approval = session['pending_approvals'][0]
        response = client.post(f"/api/v1/runs/{run['id']}/messages", json={
            'kind': 'approve', 'case_id': 'c', 'session_id': session['session_id'],
            'expected_session_revision': session['control_revision'], 'dedupe_key': 'one',
            'payload': {k: approval[k] for k in ('approval_id', 'request_hash')}})
        assert response.status_code == 202
        until = monotonic() + 4
        command = None
        while monotonic() < until:
            command = service.store.commands.list_for_run(run['id'])[0]
            if command['status'] not in {'queued', 'delivered'}:
                break
            sleep(.02)
        client.post(f"/api/v1/runs/{run['id']}/cancel", json={'reason': 'end test'})
        future.result(timeout=20)
    assert command['status'] == 'rejected', command


def test_late_native_output_is_audit_only(tmp_path, monkeypatch):
    import sys
    old = "emit({'method':'turn/completed','params':{'threadId':'native-thread','turn':turn}})"
    new = "print(json.dumps({'method':'turn/completed','params':{'threadId':'native-thread','turn':turn}})+'\\n'+json.dumps({'method':'item/completed','params':{**base,'item':{'id':'answer','type':'agentMessage','text':'late forged final'}}}),flush=True)"
    monkeypatch.setattr(sys.modules[__name__], 'FAKE_SERVER', FAKE_SERVER.replace(old, new))
    service, run, client, worker = setup(tmp_path, monkeypatch, 'message')
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.claim_and_execute)
        session = active_session(client, run['id'], approval=False)
        client.post(f"/api/v1/runs/{run['id']}/messages", json={
            'kind': 'user_message', 'content': 'approved', 'case_id': 'c', 'session_id': session['session_id'],
            'expected_session_revision': session['control_revision'], 'dedupe_key': 'one'})
        future.result(timeout=20)
    assert service.store.case_runs.list_for_run(run['id'])[0]['result']['agent']['final_output'] == 'done'
