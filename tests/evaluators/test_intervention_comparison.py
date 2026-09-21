from motte_contracts.comparison import ComparisonPolicy
from motte_eval.comparison import _compare_invariants


def test_effective_and_unknown_interventions_change_comparison_conditions():
    baseline = {'interventions': {'condition_hash': 'empty', 'possible': False}}
    candidate = {'interventions': {'condition_hash': 'steer', 'possible': False}}
    reasons, _ = _compare_invariants(baseline, candidate, ComparisonPolicy())
    assert any('INTERVENTION_CHANGED' in reason for reason in reasons)
    reasons, allowed = _compare_invariants(baseline, candidate,
        ComparisonPolicy(allowed_factors=('intervention',)))
    assert not reasons
    assert any('intervention' in reason for reason in allowed)
    candidate['interventions'] = {'condition_hash': 'empty', 'possible': True}
    reasons, _ = _compare_invariants(baseline, candidate, ComparisonPolicy())
    assert any('INTERVENTION_UNKNOWN' in reason for reason in reasons)
