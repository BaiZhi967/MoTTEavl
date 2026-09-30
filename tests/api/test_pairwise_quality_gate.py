"""Public policy boundaries never trust submitted score/qualification copies."""
import pytest
from fastapi.testclient import TestClient
from apps.api.app.main import create_app
from motte_sdk import MotteClient, ValidationError
from motte_sdk.comparisons import ComparisonService, ComparisonError
from motte_storage.run_store import InMemoryRunStore
from tests.sdk.sync_asgi import SyncASGITransport
from tests.sdk.test_m6_comparison_service import make_run, append_pass, score_row
from tests.evaluators.test_pairwise_quality_gate import lite, policy, rule, snapshot


def environment():
    store = InMemoryRunStore()
    make_run(store, 'run', case_ids=['case'])
    append_pass(store, 'run', 'pass', [score_row('case', passed=True)])
    return store, TestClient(create_app(store))


@pytest.mark.parametrize('changes', [{'threshold': None}, {'threshold': True}, {'threshold': '0.4'},
                                    {'metric_version': None}, {'metric_id': 'pairwise_challenger_score@2'},
                                    {'operator': 'lte'}, {'severity': 'warn'}])
def test_publication_rejects_bad_pairwise_policy_before_generic_coercion(changes):
    store, client = environment()
    payload = policy([rule(**changes)])
    response = client.post('/api/v1/gate-policies', json=payload)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'PAIRWISE_POLICY_INVALID'
    assert store.gate_store.list_policies() == []
    with MotteClient('http://testserver', transport=SyncASGITransport(client.app)) as sdk:
        with pytest.raises(ValidationError) as error:
            sdk.publish_gate_policy(payload)
        assert error.value.code == 'PAIRWISE_POLICY_INVALID'


def test_public_gates_require_authoritative_pairwise_source_even_with_client_copies():
    store, client = environment()
    response = client.post('/api/v1/gate-policies', json=policy())
    assert response.status_code == 201, response.text
    copied = snapshot(scoring_pass_id='pass')
    result = client.post('/api/v1/gates', json={'run_id': 'run', 'scoring_pass_id': 'pass',
        'policy': lite(), **copied})
    assert result.status_code == 200, result.text
    assert result.json()['passed'] is False
    result = client.post('/api/v1/gates/versioned', json={'run_id': 'run', 'scoring_pass_id': 'pass',
        'policy_id': 'explicit', 'policy_version': '1', **copied})
    assert result.status_code == 200, result.text
    assert result.json()['decision'] == 'insufficient_evidence'
    with MotteClient('http://testserver', transport=SyncASGITransport(client.app)) as sdk:
        result = sdk.evaluate_gate_versioned('run', 'explicit', '1', scoring_pass_id='pass')
        assert result.decision == 'insufficient_evidence'
        assert result.raw['exit_code'] == 5


@pytest.mark.parametrize('field', ['baseline_run_id', 'baseline_snapshot_id'])
def test_lite_baseline_arguments_are_rejected_before_lookup(field):
    _, client = environment()
    result = client.post('/api/v1/gates', json={'run_id': 'run', 'policy': lite(), field: 'missing'})
    assert result.status_code == 422, result.text
    assert result.json()['error']['code'] == 'PAIRWISE_POLICY_UNSUPPORTED'


def test_versioned_baseline_and_stored_policy_bypass_are_rejected():
    store, client = environment()
    assert client.post('/api/v1/gate-policies', json=policy()).status_code == 201
    result = client.post('/api/v1/gates/versioned', json={'run_id': 'run', 'policy_id': 'explicit',
        'policy_version': '1', 'baseline_id': 'missing', 'allowed_factors': ['judge', 'model']})
    assert result.status_code == 422
    assert result.json()['error']['code'] == 'PAIRWISE_POLICY_UNSUPPORTED'
    bad = policy([rule(threshold=True)], policy_id='unchecked')
    store.gate_store.put_policy(bad)
    with pytest.raises(ComparisonError) as error:
        ComparisonService(store).evaluate_gate_versioned(run_id='run', policy_id='unchecked', policy_version='1')
    assert error.value.code == 'PAIRWISE_POLICY_INVALID'


def test_lite_missing_threshold_returns_actionable_error():
    _, client = environment()
    value = lite()
    del value['threshold']
    result = client.post('/api/v1/gates', json={'run_id': 'run', 'policy': value})
    assert result.status_code == 422
    assert result.json()['error']['code'] == 'PAIRWISE_POLICY_INVALID'
    assert 'threshold' in result.json()['error']['message']


def test_unqualified_complete_subject_pairwise_cannot_pass():
    from tests.sdk.test_pairwise_quality import completed_quality
    store, client = environment()
    _, job, transport = completed_quality(store, ((.5,),))
    assert client.post('/api/v1/gate-policies', json=policy()).status_code == 201
    summary = ComparisonService(store).candidate_summary(job['run_id'], scoring_pass_id=job['reserved_pass_id'])
    assert summary['pairwise_quality']['value'] == .5
    before = len(transport.calls)
    for endpoint, fields in [('/api/v1/gates', {'policy': lite()}),
                             ('/api/v1/gates/versioned', {'policy_id': 'explicit', 'policy_version': '1'})]:
        response = client.post(endpoint, json={'run_id': job['run_id'],
            'scoring_pass_id': job['reserved_pass_id'], **fields})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result.get('passed') is False if endpoint.endswith('/gates') else result['decision'] == 'insufficient_evidence'
    assert len(transport.calls) == before


def test_public_pairwise_comparison_keeps_absolute_values_without_comparison_eligibility():
    from tests.sdk.test_pairwise_quality import completed_quality
    store, client = environment()
    _, baseline, base_transport = completed_quality(store, ((1.,),))
    _, candidate, candidate_transport = completed_quality(store, ((0.,),))
    params = {'baseline': baseline['run_id'], 'candidate': candidate['run_id'],
              'baseline_pass': baseline['reserved_pass_id'], 'candidate_pass': candidate['reserved_pass_id']}
    result = client.get('/api/v1/comparisons', params=params)
    assert result.status_code == 200, result.text
    expected = {'eligible': False, 'reason': 'pairwise_baseline_comparison_unsupported',
                'baseline_value': 1., 'candidate_value': 0.}
    assert result.json()['pairwise_comparison'] == expected
    assert result.json()['metric_eligibility']['pairwise_challenger_score@1'] is False
    with MotteClient('http://testserver', transport=SyncASGITransport(client.app)) as sdk:
        assert sdk.compare(baseline['run_id'], candidate['run_id'],
            baseline_pass=baseline['reserved_pass_id'], candidate_pass=candidate['reserved_pass_id']).raw == result.json()
    legacy = client.get('/api/v1/comparisons', params={'baseline': 'run', 'candidate': 'run'})
    assert legacy.status_code == 200
    assert 'pairwise_comparison' not in legacy.json()
    assert len(base_transport.calls) == len(candidate_transport.calls) == 1


@pytest.mark.parametrize('rules', [1, True, 'not rules', {'metric_id': 'accuracy'}])
def test_policy_family_detection_preserves_malformed_policy_rejection(rules):
    _, client = environment()
    result = client.post('/api/v1/gate-policies', json=policy(rules=rules))
    assert result.status_code == 422
    assert result.json()['error']['code'] == 'GATE_POLICY_INVALID'
