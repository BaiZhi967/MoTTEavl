"""Explicit pairwise policy and pure Gate semantics; public ledger trust is tested separately."""
from copy import deepcopy
from decimal import Decimal

import pytest

from motte_contracts.gates import GatePolicyVersion, GateRule
from motte_eval import gates

BASE = 'pairwise_challenger_score'
METRIC = BASE + '@1'


def lite(**changes):
    return {'metric': METRIC, 'op': 'gte', 'threshold': 0.4, **changes}


def rule(**changes):
    return {'rule_id': 'quality', 'kind': 'metric_threshold', 'metric_id': BASE,
            'metric_version': '1', 'operator': 'gte', 'threshold': 0.4, **changes}


def policy(rules=None, **changes):
    return {'policy_id': 'explicit', 'version': '1', 'rules': rules or [rule()],
            'created_by': 'fixture', 'reason': 'test-only threshold', **changes}


def validate(value):
    return gates.validate_pairwise_quality_policy(value)


@pytest.mark.parametrize('threshold', [0, 1, 0.4, 0.6])
@pytest.mark.parametrize('shape', ['lite', 'raw', 'typed'])
def test_explicit_pairwise_policies_are_valid_without_mutation(threshold, shape):
    value = lite(threshold=threshold) if shape == 'lite' else policy([rule(threshold=threshold)])
    if shape == 'typed':
        value = GatePolicyVersion.model_validate(value)
    before = deepcopy(value)
    assert validate(value) is None
    assert value == before


@pytest.mark.parametrize('shape', ['lite', 'raw'])
@pytest.mark.parametrize('threshold', [None, True, False, '0.4', Decimal('0.4'), float('nan'),
                                       float('inf'), -float('inf'), -0.01, 1.01, 10**1000])
def test_raw_thresholds_are_not_coerced_or_defaulted(shape, threshold):
    value = lite(threshold=threshold) if shape == 'lite' else policy([rule(threshold=threshold)])
    with pytest.raises(gates.GateInputError):
        validate(value)


@pytest.mark.parametrize('shape', ['lite', 'raw'])
def test_threshold_is_required(shape):
    value = lite() if shape == 'lite' else policy()
    del (value if shape == 'lite' else value['rules'][0])['threshold']
    with pytest.raises(gates.GateInputError):
        validate(value)


@pytest.mark.parametrize('changes', [
    {'metric': 'accuracy'}, {'metric': BASE}, {'metric': BASE, 'metric_version': '1'},
    {'metric': BASE + '@2'}, {'metric_version': '2'}, {'metric_version': 1},
    {'op': None}, {'op': 'lte'}, {'op': 'gt'}, {'op': 'lt'},
    {'severity': 'warn'}, {'missing_policy': 'diagnostic_skip'}, {'missing_policy': 'ignore'},
    {'required_coverage': 0.9}, {'required_coverage': True}, {'required_coverage': '1'},
    {'require_comparable': True}, {'require_comparable': 'false'}, {'diagnostic': True},
    {'baseline_id': None}, {'baseline_run_id': 'baseline'}, {'k': 1}, {'pass@k': 1},
    {'paired_inference': True}, {'unknown_flag': True},
])
def test_lite_rejects_implicit_or_unsupported_policy(changes):
    with pytest.raises(gates.GateInputError):
        validate(lite(**changes))


@pytest.mark.parametrize('field', ['metric', 'op'])
def test_lite_missing_identity_or_operator_cannot_fall_back(field):
    value = lite()
    del value[field]
    with pytest.raises(gates.GateInputError):
        validate(value)


@pytest.mark.parametrize('changes', [
    {'metric_version': None}, {'metric_version': '2'}, {'metric_version': 1},
    {'metric_id': 'accuracy'}, {'metric_id': BASE + '@2'},
    {'metric_id': METRIC, 'metric_version': '2'}, {'operator': 'lte'},
    {'operator': None}, {'severity': 'warn'}, {'missing_policy': 'diagnostic_skip'},
    {'kind': 'cost'}, {'kind': 'baseline_delta', 'max_regression': 0.1},
    {'kind': 'critical_case', 'critical_case_ids': ['a']},
    {'kind': 'paired_inference'}, {'metric_id': 'pass@k'},
    {'required_comparability': 'comparable'}, {'max_regression': 0.1},
    {'critical_case_ids': ['hidden-baseline-case']},
])
def test_versioned_rejects_wrong_identity_quality_rule_or_baseline_mode(changes):
    with pytest.raises(gates.GateInputError):
        validate(policy([rule(**changes)]))


def test_qualified_rule_identity_does_not_need_a_second_version_field():
    assert validate(policy([rule(metric_id=METRIC, metric_version=None)])) is None
    assert validate(lite(required_coverage=1.0, severity='block', missing_policy='fail_closed')) is None


@pytest.mark.parametrize('extra', [
    {'rule_id': 'cov', 'kind': 'coverage', 'min_coverage': 1.0},
    {'rule_id': 'cov', 'kind': 'coverage', 'metric_id': METRIC, 'min_coverage': 0.9},
    {'rule_id': 'cov', 'kind': 'coverage', 'metric_id': METRIC, 'min_coverage': True},
    {'rule_id': 'cov', 'kind': 'coverage', 'metric_id': METRIC, 'min_coverage': 1., 'min_samples': 0},
    {'rule_id': 'cov', 'kind': 'coverage', 'metric_id': METRIC, 'min_coverage': 1., 'min_samples': True},
    {'rule_id': 'delta', 'kind': 'baseline_delta', 'metric_id': 'accuracy', 'max_regression': 0.1},
    {'rule_id': 'critical', 'kind': 'critical_case', 'critical_case_ids': ['a']},
    {'rule_id': 'old', 'kind': 'metric_threshold', 'metric_id': 'accuracy', 'operator': 'gte', 'threshold': .4},
])
def test_ambiguous_coverage_and_other_quality_modes_are_rejected(extra):
    with pytest.raises(gates.GateInputError):
        validate(policy([rule(), extra]))


def test_explicit_pair_coverage_and_non_quality_blockers_are_retained():
    extras = [
        {'rule_id': 'cov', 'kind': 'coverage', 'metric_id': METRIC, 'min_coverage': 1.0, 'min_samples': 2},
        {'rule_id': 'safety', 'kind': 'safety_marker'},
        {'rule_id': 'cost', 'kind': 'cost', 'metric_id': 'cost.total_usd', 'operator': 'lte', 'threshold': 10},
        {'rule_id': 'latency', 'kind': 'latency', 'metric_id': 'latency.p95', 'operator': 'lte', 'threshold': 3},
    ]
    assert validate(policy([rule(), *extras])) is None
    with pytest.raises(gates.GateInputError):
        validate(policy(extras))


@pytest.mark.parametrize('value', [None, [], {}, {'rules': []}, policy(diagnostic=True)])
def test_malformed_or_diagnostic_policy_cannot_validate(value):
    with pytest.raises(gates.GateInputError):
        validate(value)


@pytest.mark.parametrize('changes', [{'threshold': True}, {'threshold': '0.4'}, {'threshold': float('nan')},
                                    {'metric_version': None}, {'operator': 'lte'}, {'severity': 'warn'}])
def test_unchecked_typed_rules_are_revalidated(changes):
    parsed = GatePolicyVersion.model_validate(policy())
    corrupt = parsed.model_copy(update={'rules': (parsed.rules[0].model_copy(update=changes),)})
    with pytest.raises(gates.GateInputError):
        validate(corrupt)


@pytest.mark.parametrize('extra', [
    {'rule_id': 'disguised', 'kind': 'cost', 'metric_id': 'pass_at_k', 'threshold': .5},
    {'rule_id': 'disguised', 'kind': 'latency', 'metric_id': 'paired_difference@1', 'threshold': .5},
])
def test_unsupported_metrics_cannot_hide_in_non_quality_rules(extra):
    with pytest.raises(gates.GateInputError):
        validate(policy([rule(), extra]))


@pytest.mark.parametrize('changes', [{'min_samples': 2}, {'min_coverage': 1.0}, {'kind': []}])
def test_threshold_does_not_silently_accept_coverage_fields_or_malformed_kind(changes):
    with pytest.raises(gates.GateInputError):
        validate(policy([rule(**changes)]))


@pytest.mark.parametrize('field', ['threshold', 'operator', 'metric_id', 'metric_version'])
def test_typed_policy_retains_explicit_field_presence(field):
    parsed = GatePolicyVersion.model_validate(policy())
    fields = parsed.rules[0].model_fields_set - {field}
    copied = GateRule.model_construct(_fields_set=fields, **parsed.rules[0].model_dump())
    with pytest.raises(gates.GateInputError):
        validate(parsed.model_copy(update={'rules': (copied,)}))


@pytest.mark.parametrize('shape', ['typed_policy', 'raw_policy_with_typed_rule'])
@pytest.mark.parametrize('field,value', [
    ('severity', 'warn'), ('missing_policy', 'diagnostic_skip'),
    ('required_comparability', 'partial'), ('max_regression', .1),
    ('critical_case_ids', ('hidden-case',)), ('min_samples', 2), ('min_coverage', 1.),
])
def test_typed_current_rule_values_cannot_hide_in_unset_fields(shape, field, value):
    parsed = GatePolicyVersion.model_validate(policy())
    original = parsed.rules[0]
    assert field not in original.model_fields_set
    changed = GateRule.model_construct(
        _fields_set=original.model_fields_set, **{**original.model_dump(), field: value},
    )
    value = (parsed.model_copy(update={'rules': (changed,)})
             if shape == 'typed_policy' else policy([changed]))
    before = deepcopy(value)
    with pytest.raises(gates.GateInputError):
        validate(value)
    assert value == before


def test_typed_current_policy_diagnostic_cannot_hide_in_unset_fields():
    parsed = GatePolicyVersion.model_validate(policy())
    assert 'diagnostic' not in parsed.model_fields_set
    changed = GatePolicyVersion.model_construct(
        _fields_set=parsed.model_fields_set,
        **{**parsed.model_dump(), 'rules': parsed.rules, 'diagnostic': True},
    )
    with pytest.raises(gates.GateInputError):
        validate(changed)
    assert changed.diagnostic is True


def test_typed_coverage_value_requires_explicit_presence():
    coverage = GateRule(rule_id='coverage', kind='coverage', metric_id=METRIC, min_coverage=1.)
    changed = GateRule.model_construct(
        _fields_set=coverage.model_fields_set - {'min_coverage'}, **coverage.model_dump(),
    )
    with pytest.raises(gates.GateInputError):
        validate(policy([rule(), changed]))


def test_validation_never_enables_the_current_lite_quality_gate():
    candidate = {'metric_values': {BASE: 1.0, METRIC: 1.0}, 'coverage': 1.0}
    assert validate(lite()) is None
    assert gates.evaluate_gate(lite(), candidate)['passed'] is False


# Task9b pure evaluation fixtures validate data semantics. Only ComparisonService
# can establish ledger ownership and exact persistent qualification provenance.
def quality(**changes):
    from motte_contracts.hashing import canonical_hash
    from motte_contracts.pairwise_quality import PairwiseQualitySnapshot
    roles = [{'case_id': f'case-{i}', 'pair_id': f'pair-{i}',
              'challenger_candidate_id': f'candidate-c-{i}', 'reference_candidate_id': f'candidate-r-{i}',
              'challenger_attempt_id': f'attempt-c-{i}', 'reference_attempt_id': f'attempt-r-{i}',
              'candidate_input_sha256': {f'candidate-c-{i}': canonical_hash(['c', i]),
                                         f'candidate-r-{i}': canonical_hash(['r', i])}}
             for i in range(3)]
    return PairwiseQualitySnapshot.seal({
        'source_pass_id': 'pass-1', 'job_id': 'job-1',
        'plan_sha256': canonical_hash('plan'), 'ledger_sha256': canonical_hash('ledger'),
        'roles_sha256': canonical_hash(roles), 'roles': roles,
        'planned_pairs': 3, 'valid_pairs': 3, 'planned_calls': 3, 'settled_calls': 3,
        'pair_values': {'pair-0': 1., 'pair-1': .5, 'pair-2': 0.},
        'value': .5, 'coverage': 1., 'reasons': [], **changes,
    }).model_dump(mode='json')


def qualification(**changes):
    from motte_contracts.hashing import canonical_hash
    return {'required': True, 'gate_eligible': True, 'experimental': False,
            'pass_id': 'pass-1', 'judge_pass_id': 'pass-1', 'mode': 'pairwise',
            'reason': 'verified_calibration_source', 'binding': {
                'judge_spec_sha256': canonical_hash('spec'), 'rubric_id': 'rubric', 'rubric_version': '1',
                'rubric_sha256': canonical_hash('rubric'), 'model': 'fixture-model',
                'provider_snapshot_sha256': canonical_hash('provider'), 'calibration_id': 'calibration',
                'calibration_version': '1', 'calibration_content_sha256': canonical_hash('calibration'),
                'policy_sha256': canonical_hash('qualification-policy'), 'report_id': 'calibration-report',
                'report_sha256': canonical_hash('report'), 'qualification_id': 'qualification',
            }, **changes}


def snapshot(**changes):
    return {'pairwise_quality': quality(), 'scoring_pass_id': 'pass-1',
            'report_schema': 'report-pairwise-v1', 'metric_registry_version': 'metric-registry@2',
            'metric_values': {METRIC: 1., 'accuracy': 1., 'cost.total_usd': 1.},
            'coverage': 0., 'counts': {'selected': 100, 'scored': 100},
            'judge_qualification': qualification(), **changes}


def full_evaluate(candidate=None, *, rules=None, evidence=None, **options):
    from motte_contracts.comparison import RunReportRef
    candidate = snapshot() if candidate is None else candidate
    ref = RunReportRef(run_id='run-1', scoring_pass_id='pass-1', report_schema='report-pairwise-v1',
                       evidence_hash='sha256:' + '1' * 64)
    return gates.evaluate_gate_policy(
        GatePolicyVersion.model_validate(policy(rules)), candidate_ref=ref,
        candidate_snapshot=candidate, candidate_run_status='completed',
        candidate_evidence={'judge_qualification': {'candidate': candidate.get('judge_qualification')},
                            **(evidence or {})}, **options)


@pytest.mark.parametrize('threshold,decision,code', [(.4, 'pass', 0), (.5, 'pass', 0), (.6, 'quality_fail', 1)])
def test_complete_half_score_has_explicit_threshold_result(threshold, decision, code):
    candidate = snapshot()
    assert gates.resolve_pairwise_gate_metric(candidate) == (True, .5, '')
    result = gates.evaluate_gate(lite(threshold=threshold), candidate)
    assert result['passed'] is (code == 0)
    assert result['schema'] == 'gate-lite@3'
    assert result['policy'] == lite(threshold=threshold)
    assert result['pairwise_quality']['value'] == .5
    full = full_evaluate(candidate, rules=[rule(threshold=threshold)])
    assert full.decision.value == decision
    assert gates.gate_exit_code(full.decision) == code
    assert next(row for row in full.rule_results if row.rule_id == 'quality').status == ('pass' if code == 0 else 'fail')


@pytest.mark.parametrize('alteration', ['missing', 'unqualified', 'flag_only', 'wrong_pass', 'wrong_mode', 'experimental', 'partial'])
def test_unqualified_or_incomplete_overrides_high_score(alteration):
    candidate = snapshot()
    if alteration == 'missing':
        candidate.pop('judge_qualification')
    elif alteration == 'unqualified':
        candidate['judge_qualification']['gate_eligible'] = False
    elif alteration == 'flag_only':
        candidate['judge_qualification'].pop('binding')
    elif alteration == 'wrong_pass':
        candidate['judge_qualification']['pass_id'] = 'other-pass'
    elif alteration == 'wrong_mode':
        candidate['judge_qualification']['mode'] = 'single'
    elif alteration == 'experimental':
        candidate['judge_qualification']['experimental'] = True
    else:
        candidate['pairwise_quality'] = quality(valid_pairs=2, settled_calls=2, value=None,
            coverage=2/3, pair_values={'pair-0': 1., 'pair-1': .5, 'pair-2': None}, reasons=['missing_call'])
    for threshold in (.4, .6):
        assert not gates.evaluate_gate(lite(threshold=threshold), candidate)['passed']
        assert full_evaluate(candidate, rules=[rule(threshold=threshold)]).decision.value == 'insufficient_evidence'


@pytest.mark.parametrize('alteration', ['absent', 'digest', 'source', 'schema', 'registry', 'boolean', 'empty'])
def test_resolver_ignores_bare_values_and_rejects_bad_typed_evidence(alteration):
    candidate = snapshot()
    if alteration == 'absent':
        candidate.pop('pairwise_quality')
    elif alteration == 'digest':
        candidate['pairwise_quality']['value'] = 1.
    elif alteration == 'source':
        candidate['scoring_pass_id'] = 'other-pass'
    elif alteration == 'schema':
        candidate['report_schema'] = 'report-v1'
    elif alteration == 'registry':
        candidate['metric_registry_version'] = 'metric-registry@1'
    elif alteration == 'boolean':
        candidate['pairwise_quality']['value'] = True
    else:
        candidate['pairwise_quality'] = quality(roles=[], roles_sha256=__import__(
            'motte_contracts.hashing', fromlist=['canonical_hash']).canonical_hash([]), planned_pairs=0,
            valid_pairs=0, planned_calls=0, settled_calls=0, pair_values={}, value=None,
            coverage=None, reasons=['empty_plan'])
    assert gates.resolve_pairwise_gate_metric(candidate)[0] is False
    assert not gates.evaluate_gate(lite(), candidate)['passed']
    assert full_evaluate(candidate).decision.value == 'insufficient_evidence'


def test_pairwise_coverage_uses_planned_pairs():
    coverage = {'rule_id': 'coverage', 'kind': 'coverage', 'metric_id': METRIC,
                'min_coverage': 1., 'min_samples': 3}
    assert full_evaluate(rules=[rule(), coverage]).decision.value == 'pass'
    assert gates.evaluate_gate(lite(), snapshot())['passed']
    coverage['min_samples'] = 4
    result = full_evaluate(rules=[rule(), coverage])
    assert result.decision.value == 'insufficient_evidence'
    assert 'samples 3' in result.rule_results[1].reason


@pytest.mark.parametrize('changes', [{'threshold': None}, {'threshold': True}, {'threshold': '0.4'},
                                    {'metric': BASE}, {'metric': BASE+'@2'}, {'op': 'lte'}])
def test_evaluation_revalidates_explicit_policy(changes):
    with pytest.raises(gates.GateInputError):
        gates.evaluate_gate(lite(**changes), snapshot())
    typed = GatePolicyVersion.model_validate(policy())
    damaged = typed.model_copy(update={'rules': (typed.rules[0].model_copy(update={'threshold': True}),)})
    from tests.evaluators.test_regression_gate import _ref
    with pytest.raises(gates.GateInputError):
        gates.evaluate_gate_policy(damaged, candidate_ref=_ref(), candidate_snapshot=snapshot(),
                                   candidate_run_status='completed')


def test_unsupported_comparison_and_manual_quality_fail_closed():
    from tests.evaluators.test_regression_gate import _ref
    with pytest.raises(gates.GateInputError, match='unsupported'):
        gates.evaluate_gate(lite(), snapshot(), comparison={'eligible': True})
    with pytest.raises(gates.GateInputError, match='unsupported'):
        full_evaluate(baseline_ref=_ref())
    manual = snapshot(pairwise_quality=quality(valid_pairs=0, settled_calls=3, value=None, coverage=0.,
        pair_values={f'pair-{i}': None for i in range(3)}, reasons=['manual_pairwise_quality_unsupported']))
    assert full_evaluate(manual).decision.value == 'insufficient_evidence'


def test_other_rules_can_still_block_quality_pass():
    assert full_evaluate(rules=[rule(), {'rule_id': 'safety', 'kind': 'safety_marker'}],
                         evidence={'safety_markers': ['violation']}).decision.value == 'safety_block'
    assert full_evaluate(rules=[rule(), {'rule_id': 'cost', 'kind': 'cost', 'metric_id': 'cost.total_usd',
                                      'operator': 'lte', 'threshold': .5}]).decision.value == 'quality_fail'


def test_full_identity_binds_quality_policy_definition_and_qualification():
    from motte_contracts.hashing import canonical_hash
    from motte_contracts.metrics import lookup_metric
    result = full_evaluate()
    assert result.result_semantics_hash == canonical_hash({
        'engine': 'gate-engine@3', 'rule_registry': 'gate-rules@1',
        'metric_registry': 'metric-registry@2', 'statistical_policy': 'statistical_policy@1',
        'judge_source': 'calibration-binding@1', 'pairwise_algorithm': 'pairwise-quality@1',
        'metric_definition_sha256': canonical_hash(lookup_metric(METRIC, registry_version='metric-registry@2').model_dump()),
    })
    changed = snapshot(pairwise_quality=quality(ledger_sha256=canonical_hash('other ledger')))
    assert full_evaluate(changed).gate_result_id != result.gate_result_id
    changed = snapshot()
    changed['judge_qualification']['binding']['report_sha256'] = canonical_hash('other report')
    assert full_evaluate(changed).gate_result_id != result.gate_result_id
    assert full_evaluate(rules=[rule(threshold=.6)]).gate_result_id != result.gate_result_id


def test_legacy_gate_hashes_and_accuracy_unchanged():
    from tests.evaluators.test_regression_gate import _ref, _snapshot, _policy
    result = gates.evaluate_gate({'metric': 'accuracy', 'op': 'gte', 'threshold': .7},
                                {'metric_values': {'accuracy': .8}, 'coverage': .8})
    assert result['schema'] == 'gate-lite@2'
    assert result['conclusion_hash'] == 'sha256:a5b738dfbc5dd42c8e4c6a0cfdf2698bf1f06294b989adfacd658e3a9777a110'
    full = gates.evaluate_gate_policy(_policy((GateRule(rule_id='acc', kind='metric_threshold',
        metric_id='accuracy', operator='gte', threshold=.7),)), candidate_ref=_ref(),
        candidate_snapshot=_snapshot(), candidate_run_status='completed')
    assert full.gate_result_id == 'sha256:7661e869a98de8e18e3e8e453fb466c188a286ec3b9ad886b799475ac1f34cb8'


@pytest.mark.parametrize('malformed', [True, 42, 'copied metric', [], [1]])
def test_malformed_typed_section_is_insufficient_without_crashing(malformed):
    candidate = snapshot(pairwise_quality=malformed)
    assert not gates.evaluate_gate(lite(), candidate)['passed']
    assert full_evaluate(candidate).decision.value == 'insufficient_evidence'


def test_conflicting_nested_selected_reference_is_not_silently_replaced():
    candidate = snapshot(ref={'run_id': 'other', 'scoring_pass_id': 'other',
                              'report_schema': 'report-pairwise-v1', 'evidence_hash': 'sha256:other'})
    assert full_evaluate(candidate).decision.value == 'insufficient_evidence'


def test_resolver_supports_a_typed_section_without_mutating_it():
    from motte_contracts.pairwise_quality import PairwiseQualitySnapshot
    section = PairwiseQualitySnapshot.model_validate(quality())
    candidate = snapshot(pairwise_quality=section)
    before = section.model_dump()
    assert gates.evaluate_gate(lite(), candidate)['passed']
    assert full_evaluate(candidate).decision.value == 'pass'
    assert section.model_dump() == before


def test_selected_role_change_changes_gate_identity_even_at_the_same_overall_value():
    from motte_contracts.hashing import canonical_hash
    roles = quality()['roles']
    for binding in roles:
        for suffix in ('candidate_id', 'attempt_id'):
            challenger, reference = 'challenger_'+suffix, 'reference_'+suffix
            binding[challenger], binding[reference] = binding[reference], binding[challenger]
    changed = snapshot(pairwise_quality=quality(roles=roles, roles_sha256=canonical_hash(roles),
        pair_values={'pair-0': 0., 'pair-1': .5, 'pair-2': 1.}))
    assert gates.evaluate_gate(lite(), changed)['conclusion_hash'] != gates.evaluate_gate(lite(), snapshot())['conclusion_hash']
    assert full_evaluate(changed).gate_result_id != full_evaluate().gate_result_id


def test_versioned_input_identity_explicitly_includes_the_sealed_quality_content_hash():
    from motte_contracts.hashing import canonical_hash
    from motte_contracts.metrics import lookup_metric
    candidate = snapshot()
    result = full_evaluate(candidate)
    assert result.evaluation_input_hash == canonical_hash({
        'policy': GatePolicyVersion.model_validate(policy()).content_hash(),
        'candidates': [ref.model_dump() for ref in result.candidates], 'baseline': None,
        'statistical_policy': 'statistical_policy@1',
        'judge_qualification': {'candidate': candidate['judge_qualification']},
        'pairwise_quality_content_sha256': candidate['pairwise_quality']['content_sha256'],
        'pairwise_quality_input_sha256': canonical_hash(candidate['pairwise_quality']),
        'metric_definition_sha256': canonical_hash(lookup_metric(METRIC, registry_version='metric-registry@2').model_dump()),
        'qualification_source_sha256': canonical_hash(candidate['judge_qualification']['binding']),
    })


@pytest.mark.parametrize('field,value', [('coverage', 'not-a-number'), ('planned_pairs', 'not-a-count'),
                                        ('coverage', {}), ('planned_pairs', True)])
def test_invalid_count_or_coverage_cannot_crash_or_coerce_the_gate(field, value):
    candidate = snapshot()
    candidate['pairwise_quality'][field] = value
    assert not gates.evaluate_gate(lite(), candidate)['passed']
    coverage = {'rule_id': 'coverage', 'kind': 'coverage', 'metric_id': METRIC,
                'min_coverage': 1., 'min_samples': 3}
    assert full_evaluate(candidate, rules=[rule(), coverage]).decision.value == 'insufficient_evidence'


def test_unchecked_policy_nested_rule_shape_is_revalidated_at_evaluation():
    from tests.evaluators.test_regression_gate import _ref
    raw = GatePolicyVersion.model_construct(**policy(rules=[rule(threshold=True)]))
    with pytest.raises(gates.GateInputError):
        gates.evaluate_gate_policy(raw, candidate_ref=_ref(), candidate_snapshot=snapshot(),
                                   candidate_run_status='completed')


def test_valid_unchecked_nested_policy_is_rebuilt_before_rule_evaluation():
    from motte_contracts.comparison import RunReportRef
    raw = GatePolicyVersion.model_construct(**policy())
    candidate = snapshot()
    result = gates.evaluate_gate_policy(raw, candidate_ref=RunReportRef(
        run_id='run-1', scoring_pass_id='pass-1', report_schema='report-pairwise-v1',
        evidence_hash='sha256:'+'1'*64), candidate_snapshot=candidate,
        candidate_run_status='completed', candidate_evidence={
            'judge_qualification': {'candidate': candidate['judge_qualification']}})
    assert result.gate_result_id == full_evaluate(candidate).gate_result_id
