"""Synthetic data only. Tests strict parsing, entire-file validation and version safety."""
import hashlib
import json
from copy import deepcopy
from decimal import Decimal

import pytest

from motte_contracts.gsm8k import (digest, final_number, import_official_jsonl,
                                   is_benchmark, is_benchmark_scenario, run_selected_count,
                                   scenario_for, scenario_name, scope_of, select_cases, validate_dataset)
from motte_storage.resource_store import InMemoryResourceStore, SQLiteResourceStore, ResourceConflictError


@pytest.mark.parametrize('text,expected', [('#### 1,234.50','1234.50'), ('work 9\n#### -0.5\n','-0.5'),
    ('#### +.25','.25'), ('#### -0','0'), ('#### 123456789012345678901234567890.123','123456789012345678901234567890.123')])
def test_final_decimal(text, expected):
    assert final_number(text) == Decimal(expected)


@pytest.mark.parametrize('text', ['#### NaN', '#### Infinity', '#### 1,23', '#### 1234,567',
    '#### 1e3', '#### $1', '#### 1/2', 'answer 42', '#### 1\nextra', '#### 1 units',
    '#### --1', '#### １', '#### 1.', '', None, ' #### 1'])
def test_no_guessing_or_malformed_numbers(text):
    assert final_number(text) is None


def raw_source(n=25):
    return ('\n'.join(json.dumps({'question':f'SYNTHETIC {i}', 'answer':'SYNTHETIC reasoning\n#### 1'})
                      for i in range(n)) + '\n').encode()


def imported(raw=None):
    return import_official_jsonl(raw or raw_source(), name='synthetic', version='1',
                                 revision='synthetic', license_id='synthetic-only', synthetic=True)


def imported_full(raw=None, name='synthetic-full'):
    return import_official_jsonl(raw or raw_source(), name=name, version='1',
                                 revision='synthetic', license_id='synthetic-only', synthetic=True,
                                 scope='full')


def test_selection_source_hash_and_entire_file_validation():
    raw = raw_source()
    record = imported(raw)
    assert record == imported(raw)
    assert record['provenance']['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert record['provenance']['source_line_count'] == 25
    assert len(record['cases']) == 20
    assert record['cases'][-1]['input'] == 'SYNTHETIC 19'
    assert record['cases_sha256'] == digest(record['cases'])
    for invalid in (raw_source(19), raw+b'not JSON\n', raw+b'\n', b'\xff',
                    raw+b'{"question":"bad","answer":"no final"}\n'):
        with pytest.raises(ValueError):
            imported(invalid)
    with pytest.raises(ValueError, match='pinned'):
        import_official_jsonl(raw, name='official', version='1', revision='main', license_id='MIT')


@pytest.mark.parametrize('backend', ['memory','sqlite'])
def test_benchmark_versions_immutable_generic_resources_still_upsert(tmp_path, backend):
    store = InMemoryResourceStore() if backend == 'memory' else SQLiteResourceStore(tmp_path/'resources.db')
    record = imported()
    assert store.datasets.put(record) == store.datasets.put(record)
    changed = deepcopy(record)
    changed['provenance']['revision'] = 'different'
    with pytest.raises(ResourceConflictError):
        store.datasets.put(changed)
    with pytest.raises(ResourceConflictError):
        store.datasets.put({'name':'synthetic','version':'1'})
    with pytest.raises(ResourceConflictError):
        store.datasets.delete('synthetic','1')
    scenario = scenario_for(record, name='smoke', version='1')
    store.scenarios.put(scenario)
    with pytest.raises(ResourceConflictError):
        store.scenarios.put({**scenario,'dataset':'other@1'})
    generic = {'name':'generic','version':'1','anything':1}
    assert store.datasets.put(generic) == store.datasets.put(generic)
    with pytest.raises(ResourceConflictError):
        store.datasets.put({'name':'generic','version':'1','anything':2})
    other = {'name':'other','version':'1','benchmark':{'id':'other-benchmark'}}
    store.datasets.put(other)
    with pytest.raises(ResourceConflictError):
        store.datasets.put({**other, 'anything':2})
    with pytest.raises(ResourceConflictError):
        store.datasets.delete('other','1')
    invalid = deepcopy(record)
    invalid['version'] = '2'
    invalid['cases'][1]['case_id'] = invalid['cases'][0]['case_id']
    invalid['cases_sha256'] = digest(invalid['cases'])
    with pytest.raises(ValueError, match='duplicate'):
        store.datasets.put(invalid)


def test_full_scope_selects_every_row_and_pins_the_count():
    raw = raw_source(25)
    record = imported_full(raw)
    assert scope_of(record) == 'full'
    assert is_benchmark(record) is True
    assert len(record['cases']) == 25
    assert record['benchmark']['id'] == 'gsm8k-full'
    assert record['benchmark']['selection'] == 'all-rows-in-file-order'
    assert record['benchmark']['selected_count'] == 25 == record['provenance']['source_line_count']
    assert record['cases'][-1]['case_id'] == 'gsm8k-test-0024'
    assert record['cases'][-1]['metadata'] == {'source_line': 25}
    # 全量没有 20 题下限：短文件照样全取
    assert len(imported_full(raw_source(3))['cases']) == 3
    assert imported_full(raw) == imported_full(raw)


def test_full_scope_rejects_truncated_or_mismatched_records():
    record = imported_full()
    # 题数被截断（例如误用冒烟选择）→ 与记录里的 selected_count 不符
    truncated = deepcopy(record)
    truncated['cases'] = truncated['cases'][:20]
    truncated['cases_sha256'] = digest(truncated['cases'])
    with pytest.raises(ValueError, match='selected count'):
        validate_dataset(truncated)
    # 行数对不上（全量必须覆盖整个源文件）
    short_source = deepcopy(record)
    short_source['provenance']['source_line_count'] = 30
    with pytest.raises(ValueError, match='provenance'):
        validate_dataset(short_source)
    # 未知 scope
    with pytest.raises(ValueError, match='scope'):
        import_official_jsonl(raw_source(), name='synthetic', version='1', revision='synthetic',
                              license_id='synthetic-only', synthetic=True, scope='everything')


def test_scope_helpers_drive_scenario_names_and_detection():
    smoke, full = imported(), imported_full(name='synthetic')
    assert scenario_name('gsm8k-test', 'smoke') == 'gsm8k-test-smoke'
    assert scenario_name('gsm8k-test', 'full') == 'gsm8k-test-full'
    with pytest.raises(ValueError, match='scope'):
        scenario_name('gsm8k-test', 'everything')
    assert is_benchmark_scenario('gsm8k-test-smoke') is True
    assert is_benchmark_scenario('gsm8k-test-full') is True
    assert is_benchmark_scenario('gsm8k-test') is False
    assert is_benchmark_scenario(None) is False
    assert is_benchmark({'benchmark': {'id': 'other-benchmark'}}) is False
    assert scope_of(smoke) == 'smoke' and scope_of(full) == 'full'
    # 场景继承数据集的 benchmark 描述（含 full 的实际题数），供运行时分母使用
    scenario = scenario_for(full, name='synthetic-full', version='1')
    assert scenario['benchmark'] == full['benchmark']
    assert scenario['benchmark']['selected_count'] == 25
    assert scenario['dataset'] == 'synthetic@1'


def test_two_scopes_of_the_same_source_coexist_on_distinct_versions():
    store = InMemoryResourceStore()
    store.datasets.put(imported_full(name='gsm8k-test', raw=raw_source(25)))
    assert store.datasets.get('gsm8k-test', '1')['benchmark']['id'] == 'gsm8k-full'
    # 同 name@version 换 scope（选中集不同）→ 冲突，必须换版本号
    smoke_v1 = import_official_jsonl(raw_source(25), name='gsm8k-test', version='1',
                                     revision='synthetic', license_id='synthetic-only', synthetic=True)
    with pytest.raises(ResourceConflictError):
        store.datasets.put(smoke_v1)
    smoke_v2 = {**smoke_v1, 'version': '2'}
    store.datasets.put(smoke_v2)
    assert [(d['version'], d['benchmark']['id'], len(d['cases'])) for d in store.datasets.list()] == [
        ('1', 'gsm8k-full', 25), ('2', 'gsm8k-20', 20)]
    assert scope_of(smoke_v2) == 'smoke'


def test_select_cases_all_ids_and_random_are_deterministic():
    dataset = imported_full(raw_source(25))  # 25 题：gsm8k-test-0000 .. 0024
    every = select_cases(dataset)
    assert every['mode'] == 'all' and every['count'] == 25 and every['seed'] is None
    assert every['case_ids'][0] == 'gsm8k-test-0000' and every['case_ids'][-1] == 'gsm8k-test-0024'
    assert every['case_ids_sha256'] == digest(every['case_ids'])
    # 显式指定：去重后按数据集顺序输出，与传入顺序无关
    picked = select_cases(dataset, {'mode': 'ids',
                                    'case_ids': ['gsm8k-test-0007', 'gsm8k-test-0001', 'gsm8k-test-0007']})
    assert picked['mode'] == 'ids' and picked['case_ids'] == ['gsm8k-test-0001', 'gsm8k-test-0007']
    assert picked['count'] == 2 and picked['seed'] is None
    # 随机：同一 seed 必得同一子集，不同 seed 基本不同；省略 seed 时生成并回填
    first = select_cases(dataset, {'mode': 'random', 'count': 5, 'seed': 'deadbeef'})
    assert first == select_cases(dataset, {'mode': 'random', 'count': 5, 'seed': 'deadbeef'})
    assert first['count'] == 5 and first['seed'] == 'deadbeef'
    assert sorted(first['case_ids']) == first['case_ids']  # 结果按数据集顺序
    assert first['case_ids'] != select_cases(dataset, {'mode': 'random', 'count': 5, 'seed': 'feedface'})['case_ids']
    generated = select_cases(dataset, {'mode': 'random', 'count': 3})
    assert generated['seed'] and len(generated['seed']) == 16 and generated['count'] == 3
    assert select_cases(dataset, {'mode': 'random', 'count': 25})['count'] == 25


@pytest.mark.parametrize('selection,message', [
    ({'mode': 'everything'}, 'unsupported'),
    ({'mode': 'ids'}, 'case_ids'),
    ({'mode': 'ids', 'case_ids': []}, 'case_ids'),
    ({'mode': 'ids', 'case_ids': ['gsm8k-test-9999']}, 'not in dataset'),
    ({'mode': 'ids', 'case_ids': [42]}, 'not in dataset'),
    ({'mode': 'random', 'count': 0}, 'between 1 and 25'),
    ({'mode': 'random', 'count': 26}, 'between 1 and 25'),
    ({'mode': 'random', 'count': True}, 'between 1 and 25'),
    ({'mode': 'random', 'count': 3, 'seed': 'zz'}, 'seed'),
    ('all', 'must be an object'),
])
def test_select_cases_rejects_bad_input(selection, message):
    dataset = imported_full(raw_source(25))
    with pytest.raises(ValueError, match=message):
        select_cases(dataset, selection)


def test_run_selected_count_prefers_run_level_subset_then_falls_back():
    # 运行级子集：分母是子集题数
    assert run_selected_count({'selected_count': 1319, 'run_selection': {'mode': 'random', 'count': 100}}) == 100
    # 旧运行（无 run_selection）回落数据集级 selected_count
    assert run_selected_count({'selected_count': 20}) == 20
    # 非法的运行级计数不生效，继续回落
    assert run_selected_count({'run_selection': {'mode': 'all', 'count': 0}, 'selected_count': 20}) == 20
    assert run_selected_count({}) is None and run_selected_count(None) is None
