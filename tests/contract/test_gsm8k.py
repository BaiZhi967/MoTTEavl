"""Synthetic data only. Tests strict parsing, entire-file validation and version safety."""
import hashlib
import json
from copy import deepcopy
from decimal import Decimal

import pytest

from motte_contracts.gsm8k import digest, final_number, import_official_jsonl, scenario_for
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
    store.datasets.put({'name':'generic','version':'1','anything':1})
    store.datasets.put({'name':'generic','version':'1','anything':2})
    assert store.datasets.get('generic','1')['anything'] == 2
    store.datasets.put({'name':'other','version':'1','benchmark':{'id':'other-benchmark'}})
    store.datasets.put({'name':'other','version':'1','benchmark':{'id':'other-benchmark'},'anything':2})
    assert store.datasets.delete('other','1') is True
    invalid = deepcopy(record)
    invalid['version'] = '2'
    invalid['cases'][1]['case_id'] = invalid['cases'][0]['case_id']
    invalid['cases_sha256'] = digest(invalid['cases'])
    with pytest.raises(ValueError, match='duplicate'):
        store.datasets.put(invalid)
