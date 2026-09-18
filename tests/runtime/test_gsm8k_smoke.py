"""Synthetic fixtures only: no official GSM8K content, network or model calls."""
import json

import pytest

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
            '--revision','synthetic','--license','synthetic-only','--scope','smoke',
            '--synthetic','--db',db]
    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out) == first
    resources = SQLiteResourceStore(db)
    resources.providers.put({'name':'local','kind':'openai_compatible','base_url':'https://offline.invalid', 'max_retries':9})
    resources.models.put({'id':'m','provider':'local','model':'synthetic-model','max_output_tokens':2048})
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


def test_cli_benchmark_run_selects_subset_and_reasoning_level(tmp_path, capsys):
    """CLI：--case-ids / --random N --seed / --reasoning-level 都进 manifest，错误参数退出 2。"""
    from motte_cli.main import main
    from motte_storage.resource_store import SQLiteResourceStore

    source = tmp_path / 'synthetic.jsonl'
    source.write_text('\n'.join(json.dumps({'question': f'SYNTHETIC {i}: compute one.',
        'answer': '#### 1'}) for i in range(25)), encoding='utf-8')
    db = str(tmp_path / 'runs.db')
    assert main(['benchmark','import','--file',str(source),'--revision','synthetic','--license',
                 'synthetic-only','--scope','smoke','--synthetic','--db',db]) == 0
    scenario = json.loads(capsys.readouterr().out)['scenario']
    resources = SQLiteResourceStore(db)
    resources.providers.put({'name':'local','kind':'openai_compatible','base_url':'https://offline.invalid'})
    resources.models.put({'id':'reasoner','provider':'local','model':'synthetic-reasoner',
                          'max_output_tokens':4096,
                          'reasoning':{'supported':True,'levels':['low','high'],
                                       'control':'{"reasoning_effort": reasoningLevel}',
                                       'default_level':'low'}})

    assert main(['benchmark','run','--scenario',scenario,'--model','reasoner',
                 '--case-ids','gsm8k-test-0003, gsm8k-test-0001','--reasoning-level','high',
                 '--db',db]) == 0
    run = json.loads(capsys.readouterr().out)
    assert run['case_ids'] == ['gsm8k-test-0001','gsm8k-test-0003']
    assert run['manifest']['benchmark_provenance']['run_selection'] == {
        'mode':'ids','count':2,'seed':None}
    assert run['manifest']['provider']['reasoning_level'] == 'high'

    assert main(['benchmark','run','--scenario',scenario,'--model','reasoner',
                 '--random','5','--seed','deadbeef','--db',db]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert len(replay['case_ids']) == 5
    assert replay['manifest']['benchmark_provenance']['run_selection'] == {
        'mode':'random','count':5,'seed':'deadbeef'}
    assert main(['benchmark','run','--scenario',scenario,'--model','reasoner',
                 '--random','5','--seed','deadbeef','--db',db]) == 0
    assert json.loads(capsys.readouterr().out)['case_ids'] == replay['case_ids']

    # 未知题目 / 越界 N / 种子脱离 random / 未知思考强度：都是退出码 2 的配置错误
    def failure(argv):
        assert main(argv) == 2
        return json.loads(capsys.readouterr().err.strip().splitlines()[-1])['error']

    base = ['benchmark','run','--scenario',scenario,'--model','reasoner']
    assert 'not in dataset' in failure([*base,'--case-ids','nope','--db',db])['message']
    assert 'between 1 and 20' in failure([*base,'--random','999','--db',db])['message']
    assert failure([*base,'--seed','deadbeef','--db',db])['code'] == 'CONTRACT_INVALID'
    assert failure([*base,'--reasoning-level','unsupported','--db',db])['code'] == 'MODEL_CONFIG_INVALID'


def test_benchmark_run_rejects_ceiling_below_preset(tmp_path, capsys):
    """Preset max_output_tokens=1024 overrides profile defaults but never the hard ceiling."""
    from motte_cli.main import main
    from fastapi.testclient import TestClient
    from apps.api.app.main import create_app
    from motte_storage.resource_store import SQLiteResourceStore
    from motte_storage.run_store import SQLiteRunStore

    source = tmp_path / 'synthetic.jsonl'
    source.write_text('\n'.join(json.dumps({'question': f'SYNTHETIC {i}: compute one.',
        'answer': '#### 1'}) for i in range(25)), encoding='utf-8')
    db = str(tmp_path / 'runs.db')
    assert main(['benchmark','import','--file',str(source),'--name','synthetic','--version','1',
                 '--revision','synthetic','--license','synthetic-only','--scope','smoke',
                 '--synthetic','--db',db]) == 0
    scenario = json.loads(capsys.readouterr().out)['scenario']
    resources = SQLiteResourceStore(db)
    resources.providers.put({'name':'local','kind':'openai_compatible','base_url':'https://offline.invalid'})
    resources.models.put({'id':'small','provider':'local','model':'synthetic-model','max_output_tokens':3})
    resources.models.put({'id':'default-only','provider':'local','model':'synthetic-model',
                          'parameters':{'max_output_tokens':2048}})

    # CLI benchmark run: hard ceiling wins over the preset request.
    assert main(['benchmark','run','--scenario',scenario,'--model','small','--db',db]) == 2
    rejected = json.loads(capsys.readouterr().err)
    assert rejected == {'error': {'code': 'RUN_CONFIG_INVALID',
                                  'message': 'max_output_tokens exceeds model ceiling 3'}}

    # CLI run --spec shares the same preflight.
    spec = {'scenario_version':scenario, 'manifest':{'model':'small'}}
    assert main(['run','--spec',json.dumps(spec),'--db',db]) == 2
    assert json.loads(capsys.readouterr().err) == rejected

    # API parity: same rejection before any paid call.
    client = TestClient(create_app(RunService(SQLiteRunStore(db)).store, resources))
    response = client.post('/api/v1/runs', json=spec)
    assert response.status_code == 422, response.text
    assert response.json()['error'] == rejected['error']

    # Profile parameters are defaults: they may sit below the preset and are then overridden.
    assert main(['benchmark','run','--scenario',scenario,'--model','default-only','--db',db]) == 0
    run = json.loads(capsys.readouterr().out)
    assert run['manifest']['provider']['parameters']['max_output_tokens'] == 1024
    assert run['manifest']['provider']['max_output_tokens'] == 2048


def test_full_scope_run_uses_recorded_case_count_as_denominator():
    """全量 scope：运行分母取数据集记录里的题数（不再是写死的 20）。"""
    from apps.api.app.main import _build_report
    raw = ('\n'.join(json.dumps({'question': f'SYNTHETIC full {i}: compute one.',
                                'answer': 'SYNTHETIC gold reasoning\n#### 1'})
                     for i in range(30)) + '\n').encode()
    resources = InMemoryResourceStore()
    dataset = import_official_jsonl(raw, name='synthetic-full', version='1', revision='synthetic',
                                    license_id='synthetic-test-only', synthetic=True, scope='full')
    resources.datasets.put(dataset)
    scenario = scenario_for(dataset, name='synthetic-full-full', version='1')
    resources.scenarios.put(scenario)
    manifest = resolve_benchmark_manifest(scenario, {}, resources)
    assert manifest['benchmark_provenance']['selected_count'] == 30
    assert manifest['benchmark_provenance']['selection'] == 'all-rows-in-file-order'
    service = RunService(InMemoryRunStore())
    run = service.create_run('synthetic-full-full@1', manifest, list(manifest['cases']))
    assert len(run['case_ids']) == 30
    calls = []
    result = service.execute(run['id'], provider=lambda cid: calls.append(cid) or {'content': '#### 1'})
    assert result['status'] == 'completed' and len(calls) == 30
    assert len(result['scores']) == 30 and all(s['passed'] for s in result['scores'])
    report = _build_report(result)
    assert report['summary']['selected'] == 30
    assert report['summary']['pass_rate'] == 1.0
    assert report['summary']['completion'] == 1.0
    assert report['summary']['denominator'] == 'selected_cases'
    assert service.rescore(run['id'])['scores'] == result['scores']


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


def test_run_subset_selection_pins_ids_seed_and_denominator():
    """运行级子集：只跑选中题、只投影选中 prompt、分母是子集题数，且随机种子可复现。"""
    from apps.api.app.main import _build_report
    raw = ('\n'.join(json.dumps({'question': f'SYNTHETIC subset {i}: compute one.',
                                'answer': 'SYNTHETIC gold reasoning\n#### 1'})
                     for i in range(30)) + '\n').encode()
    resources = InMemoryResourceStore()
    dataset = import_official_jsonl(raw, name='synthetic-subset', version='1', revision='synthetic',
                                    license_id='synthetic-test-only', synthetic=True, scope='full')
    resources.datasets.put(dataset)
    scenario = scenario_for(dataset, name='synthetic-subset-full', version='1')
    resources.scenarios.put(scenario)

    explicit = resolve_benchmark_manifest(
        scenario, {'case_selection': {'mode': 'ids',
                                      'case_ids': ['gsm8k-test-0003', 'gsm8k-test-0001']}}, resources)
    assert list(explicit['cases']) == ['gsm8k-test-0001', 'gsm8k-test-0003']
    assert explicit['benchmark_provenance']['run_selection'] == {'mode': 'ids', 'count': 2, 'seed': None}
    assert explicit['case_selection'] == {'mode': 'ids', 'count': 2, 'seed': None}
    # 数据集本身没被改动（仍 30 题），只有本运行投影了子集
    assert len(explicit['benchmark_snapshot']['dataset']['cases']) == 30
    assert explicit['benchmark_provenance']['selected_count'] == 30

    random_manifest = resolve_benchmark_manifest(
        scenario, {'case_selection': {'mode': 'random', 'count': 6, 'seed': 'deadbeef'}}, resources)
    assert random_manifest['benchmark_provenance']['run_selection'] == {
        'mode': 'random', 'count': 6, 'seed': 'deadbeef'}
    assert list(random_manifest['cases']) == sorted(random_manifest['cases'])  # 数据集顺序
    again = resolve_benchmark_manifest(
        scenario, {'case_selection': {'mode': 'random', 'count': 6, 'seed': 'deadbeef'}}, resources)
    assert list(again['cases']) == list(random_manifest['cases'])

    service = RunService(InMemoryRunStore())
    run = service.create_run('synthetic-subset-full@1', random_manifest, list(random_manifest['cases']))
    assert len(run['case_ids']) == 6
    calls = []
    result = service.execute(run['id'], provider=lambda cid: calls.append(cid) or {'content': '#### 1'})
    assert result['status'] == 'completed' and len(calls) == 6
    report = _build_report(result)
    assert report['summary']['selected'] == 6 and report['summary']['pass_rate'] == 1.0
    assert report['summary']['scored'] == 6


def test_benchmark_run_rejects_case_ids_argument_and_unknown_ids():
    """子集只能走 manifest.case_selection；未知 id / 非 benchmark 场景带 case_selection 都被拒绝。"""
    from motte_sdk.resolve import ManifestResolutionError, prepare_run
    resources, scenario = synthetic_resources()
    manifest = resolve_benchmark_manifest(scenario, {}, resources)
    with pytest.raises(ManifestResolutionError, match='case_selection'):
        prepare_run('smoke@1', {}, list(manifest['cases'])[:3], resources)
    with pytest.raises(ManifestResolutionError, match='not in dataset'):
        prepare_run('smoke@1', {'case_selection': {'mode': 'ids', 'case_ids': ['nope']}}, [], resources)
    with pytest.raises(ManifestResolutionError, match='only supported for benchmark'):
        prepare_run('direct-llm@1', {'case_selection': {'mode': 'all'}}, [], resources)
