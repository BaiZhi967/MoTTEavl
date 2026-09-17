"""Synthetic fixtures only: no official GSM8K content, network or model calls."""
import json

from motte_contracts.gsm8k import import_official_jsonl, scenario_for
from motte_sdk.benchmark import resolve_benchmark_manifest
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore


def synthetic_resources():
    raw = ('\n'.join(json.dumps({'question': f'SYNTHETIC problem {i}: compute one plus zero.',
                                'answer': 'SYNTHETIC gold reasoning\n#### 1'})
                     for i in range(25)) + '\n').encode()
    resources = InMemoryResourceStore()
    dataset = import_official_jsonl(raw, name='synthetic', version='1', revision='synthetic',
                                    license_id='synthetic-test-only', synthetic=True)
    resources.datasets.put(dataset)
    scenario = scenario_for(dataset, name='smoke', version='1')
    resources.scenarios.put(scenario)
    return resources, scenario


def test_cancel_before_worker_emits_terminal_event_and_never_calls():
    from apps.worker.motte_worker.runtime import WorkerLoop

    resources, scenario = synthetic_resources()
    manifest = resolve_benchmark_manifest(scenario, {}, resources)
    calls = []
    service = RunService(InMemoryRunStore(), provider=lambda cid: calls.append(cid))
    run = service.create_run('smoke@1', manifest, list(manifest['cases']))
    result = service.cancel(run['id'], reason='operator cancelled before worker')
    assert result['status'] == 'cancelled'
    assert len(result['scores']) == 20
    assert all(s['outcome'] == 'not_attempted' and not s['attempted'] for s in result['scores'])
    assert [e['type'] for e in service.events(run['id'])] == ['queued', 'cancelled']
    assert service.events(run['id'])[-1]['reason'] == 'operator cancelled before worker'
    assert WorkerLoop(service).claim_and_execute() is None
    assert calls == []
    assert service.rescore(run['id'])['scores'] == result['scores']


def test_any_call_failure_marks_run_failed_but_transient_error_continues():
    resources, scenario = synthetic_resources()
    manifest = resolve_benchmark_manifest(scenario, {}, resources)
    service = RunService(InMemoryRunStore())
    run = service.create_run('smoke@1', manifest, list(manifest['cases']))
    calls = []

    class RateError(Exception):
        error_class = 'rate_limit'

    def invoke(case_id):
        calls.append(case_id)
        if case_id == 'gsm8k-test-0005':
            raise RateError('synthetic busy')
        return {'content': '#### 1'}

    result = service.execute(run['id'], provider=invoke)
    assert result['status'] == 'failed'
    assert len(calls) == 20
    assert sum(s['outcome'] == 'correct' for s in result['scores']) == 19
    assert sum(s['outcome'] == 'call_failed' for s in result['scores']) == 1
    assert result['scores'][5]['attempted'] is True
    assert result['scores'][5]['responded'] is False
    assert service.rescore(run['id'])['scores'] == result['scores']
    assert len(calls) == 20


def test_systemic_failure_preserves_partial_scores_and_strict_report():
    from apps.api.app.main import _build_report
    resources, scenario = synthetic_resources()
    manifest = resolve_benchmark_manifest(scenario, {}, resources)
    service = RunService(InMemoryRunStore())
    run = service.create_run('smoke@1', manifest, list(manifest['cases']))
    calls = []
    def invoke(cid):
        calls.append(cid)
        if len(calls) == 4:
            return {"error": {"class": "auth", "message": "synthetic denied"}}
        return {"content": ['#### 1', '#### 2', 'no final answer'][len(calls)-1],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                "cost": {"total": 0, "price_table_version": "synthetic-free"}}
    result = service.execute(run['id'], provider=invoke)
    assert result['status'] == 'failed' and len(calls) == 4
    assert [s['outcome'] for s in result['scores'][:4]] == ['correct','wrong_answer','parse_failure','call_failed']
    report = _build_report(result)
    assert report['summary']['not_attempted'] == 16
    assert report['summary']['pass_rate'] == 1/20
    assert report['summary']['attempted'] == 4
    assert report['summary']['responded'] == 3
    assert report['summary']['completion'] == 3/20
    assert report['cost']['total'] == 0
    assert report['usage']['prompt_tokens'] == 6
    assert service.rescore(run['id'])['scores'] == result['scores']
    assert len(calls) == 4


def test_cli_api_worker_parity_and_no_gold_leakage(tmp_path, capsys, monkeypatch):
    from motte_cli.main import main
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_storage.resource_store import SQLiteResourceStore
    from motte_storage.run_store import SQLiteRunStore
    import motte_provider.config as config
    from motte_provider.openai_compatible import CaseDrivenProvider, OpenAICompatibleProvider
    from motte_provider.transport import HTTPTransport

    source = tmp_path / 'synthetic.jsonl'
    source.write_text('\n'.join(json.dumps({'question': f'SYNTHETIC {i}: compute one.',
        'answer': 'PRIVATE SYNTHETIC GOLD REASONING\n#### 1'}) for i in range(25)), encoding='utf-8')
    db = str(tmp_path / 'runs.db')
    argv = ['benchmark','import','--file',str(source),'--name','synthetic','--version','1',
            '--revision','synthetic','--license','synthetic-only','--synthetic','--db',db]
    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out) == first
    resources = SQLiteResourceStore(db)
    resources.providers.put({'name':'local','kind':'openai_compatible','base_url':'https://offline.invalid', 'max_retries':9})
    resources.models.put({'id':'m','provider':'local','model':'synthetic-model','parameters':{'max_output_tokens':3}})
    spec = {'scenario_version':first['scenario'], 'manifest':{'model':'m'}}
    assert main(['run','--spec',json.dumps(spec),'--db',db]) == 0
    cli_run = json.loads(capsys.readouterr().out)
    service = RunService(SQLiteRunStore(db))
    client = TestClient(create_app(service.store, resources))
    response = client.post('/api/v1/runs', json=spec)
    assert response.status_code == 202, response.text
    api_run = response.json()
    assert api_run['manifest'] == cli_run['manifest']
    assert api_run['case_ids'] == cli_run['case_ids']
    assert len(api_run['case_ids']) == 20
    assert api_run['manifest']['provider']['max_retries'] == 0
    assert api_run['manifest']['provider']['parameters']['max_output_tokens'] == 1024
    # Later resource edits do not affect either run.
    resources.models.put({'id':'m','provider':'local','model':'changed'})
    captured = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self):
            return json.dumps({'choices':[{'message':{'content':'#### 1'},'finish_reason':'stop'}],
                               'usage':{'prompt_tokens':2,'completion_tokens':3}}).encode()
    def opener(request, *, timeout):
        captured.append(json.loads(request.data))
        return Response()
    def factory(provider, cases, **kwargs):
        assert provider['model'] == 'synthetic-model'
        return CaseDrivenProvider(OpenAICompatibleProvider(
            HTTPTransport(provider['base_url'], None, opener=opener, max_retries=provider['max_retries']),
            provider['model'], parameters=provider['parameters']), cases)
    monkeypatch.setattr(config, 'build_case_provider', factory)
    result = WorkerLoop(service).claim_and_execute()
    assert result['status'] == 'completed' and len(captured) == 20
    assert all(s['passed'] for s in result['scores'])
    wire = json.dumps(captured)
    assert 'PRIVATE SYNTHETIC GOLD' not in wire and 'expected' not in wire and 'provenance' not in wire
    assert all(body['max_tokens'] == 1024 for body in captured)
    assert service.rescore(result['id'])['scores'] == result['scores']
    assert len(captured) == 20


def test_restart_skips_durable_results_and_preserves_stop_marker(tmp_path):
    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_storage.run_store import SQLiteRunStore
    resources, scenario = synthetic_resources()
    manifest = resolve_benchmark_manifest(scenario, {}, resources)
    path = tmp_path / 'restart.db'
    service = RunService(SQLiteRunStore(path))
    run = service.create_run('smoke@1', manifest, list(manifest['cases']))
    calls = []
    def interrupted(cid):
        calls.append(cid)
        if len(calls) == 3:
            raise KeyboardInterrupt('synthetic crash')
        return {'content':'#### 1'}
    import pytest
    with pytest.raises(KeyboardInterrupt):
        service.execute(run['id'], provider=interrupted)
    reopened = RunService(SQLiteRunStore(path))
    assert WorkerLoop(reopened).recover_interrupted() == [run['id']]
    resumed = []
    result = reopened.execute(run['id'], provider=lambda cid: resumed.append(cid) or {'content':'#### 1'})
    assert len(resumed) == 18 and result['status'] == 'completed'
    assert resumed[0] == run['case_ids'][2]
    # Simulate crash just after durable systemic failure row, before terminal update.
    second = reopened.create_run('smoke@1', manifest, list(manifest['cases']))
    reopened._transition(second['id'], 'preparing')
    reopened._transition(second['id'], 'running')
    reopened.store.case_runs.upsert({'run_id':second['id'],'case_id':second['case_ids'][0],
        'result':{'error':{'class':'auth','message':'synthetic'}}, 'stop_run':True})
    WorkerLoop(reopened).recover_interrupted()
    result = reopened.execute(second['id'], provider=lambda _: pytest.fail('must not call after durable stop'))
    assert result['status'] == 'failed'
    assert sum(s['outcome']=='not_attempted' for s in result['scores']) == 19
