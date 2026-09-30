"""Reject raw pairwise numeric coercion before Pydantic can erase it."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from motte_contracts.gates import GateRule, GatePolicyVersion


@pytest.mark.parametrize('metric', ['pairwise_challenger_score', 'pairwise_challenger_score@1'])
@pytest.mark.parametrize('threshold', [True, False, '0.4', Decimal('0.4')])
def test_pairwise_rule_checks_raw_threshold_before_float_parsing(metric, threshold):
    with pytest.raises(ValidationError):
        GateRule(rule_id='quality', kind='metric_threshold', metric_id=metric,
                 metric_version='1', operator='gte', threshold=threshold)


@pytest.mark.parametrize('minimum', [True, '1', Decimal('1')])
def test_pairwise_coverage_checks_raw_number_before_float_parsing(minimum):
    with pytest.raises(ValidationError):
        GateRule(rule_id='coverage', kind='coverage', metric_id='pairwise_challenger_score@1',
                 min_coverage=minimum)


def test_legacy_policy_golden_serialization_and_hash_unchanged():
    old = GatePolicyVersion(policy_id='legacy', version='1', created_by='fixture', reason='golden',
                            rules=(GateRule(rule_id='accuracy', kind='metric_threshold',
                                            metric_id='accuracy', operator='gte', threshold=.5),))
    assert old.content_hash() == 'sha256:c4eac633193a236639e4da9f4254c4bd11592f6a18a5fd36dbc28007aab798ef'
    assert old.rules[0].model_dump()['metric_version'] is None
    assert GateRule(rule_id='old-coercion', kind='metric_threshold', metric_id='accuracy',
                    operator='gte', threshold='0.5').threshold == .5
