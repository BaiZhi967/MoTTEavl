"""Strict suite configuration and historical ExperimentSpec identity."""

from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from motte_contracts.experiment import (
    ExperimentSpec, ScenarioExperimentCase, ScenarioExperimentConfig, SkillExperimentConfig,
)


def legacy_spec():
    return {
        "experiment_id": "legacy", "version": "1",
        "task_ref": {"suite": "direct-llm", "scenario_version": "dataset@1"},
        "factors": {"model_profile": ["model-a"]},
        "budget_policy": {"max_total_calls": 50},
        "created_by": "tester", "reason": "legacy snapshot",
    }


def suite_spec(kind="scenario"):
    payload = legacy_spec()
    suite = "ceval-external" if kind == "ceval" else kind
    payload["task_ref"] = {"suite": suite, "scenario_version": "dataset@1"}
    if kind == "ceval":
        config = {
            "kind": "ceval", "dataset_revision": "revision-1", "scope": "smoke",
            "split": "val", "few_shot": 0, "few_shot_split": "dev", "seed": 7,
            "execution_profile": "bounded-opencompass@1",
        }
    else:
        config = {
            "kind": kind, "workflow_ref": "workflow@1", "cases": ["case-2", "case-1"],
            "agent_mode": "legacy-json", "execution_budget": {"max_steps": 3},
        }
        if kind == "scenario":
            config["target"] = "builtin-agent"
        else:
            config["budget_policy"] = "same-execution-budget"
            payload["factors"]["skill_version"] = ["no-skill", "helper@9", "helper@2"]
    payload["suite_config"] = config
    return payload


@pytest.mark.parametrize("kind", ["ceval", "scenario", "skill"])
def test_typed_suite_configuration_parses_and_roundtrips(kind):
    parsed = ExperimentSpec.model_validate(suite_spec(kind))
    assert parsed.suite_config.kind == kind
    assert ExperimentSpec.model_validate_json(parsed.model_dump_json()) == parsed
    assert parsed.model_dump(mode="json")["suite_config"]["kind"] == kind


def test_absent_suite_config_preserves_legacy_serialization_and_content_hash():
    parsed = ExperimentSpec.model_validate(legacy_spec())
    assert "suite_config" not in parsed.model_dump()
    assert "suite_config" not in parsed.model_dump(mode="json")
    assert "suite_config" not in json.loads(parsed.model_dump_json())
    assert parsed.content_hash() == (
        "sha256:4d03d7c960d77679629f47a7531fbc4e10cc0b898fca2399b86ec007e3b631bb"
    )
    assert ExperimentSpec.model_validate({**legacy_spec(), "suite_config": None}) == parsed


@pytest.mark.parametrize("kind", ["ceval", "scenario", "skill"])
@pytest.mark.parametrize("key", ["manifest", "call_bound", "max_calls", "arms", "unexpected"])
def test_public_config_forbids_manifests_bound_claims_and_legacy_arm_lists(kind, key):
    payload = suite_spec(kind)
    payload["suite_config"][key] = {} if key != "arms" else ["no-skill", "helper@1"]
    with pytest.raises(ValidationError, match=key):
        ExperimentSpec.model_validate(payload)


@pytest.mark.parametrize("kind", ["ceval", "scenario", "skill"])
def test_config_discriminator_must_match_task_suite(kind):
    payload = suite_spec(kind)
    payload["task_ref"]["suite"] = "direct-llm"
    with pytest.raises(ValidationError, match="suite_config.*suite"):
        ExperimentSpec.model_validate(payload)


@pytest.mark.parametrize("kind", ["ceval", "scenario", "skill"])
def test_structured_configuration_cannot_silently_drop_scalar_conditions(kind):
    payload = suite_spec(kind)
    payload["controlled_conditions"] = {"max_output_tokens": 512}
    with pytest.raises(ValidationError, match="controlled_conditions"):
        ExperimentSpec.model_validate(payload)


@pytest.mark.parametrize("kind,field", [
    ("ceval", "dataset_revision"), ("ceval", "execution_profile"),
    ("scenario", "execution_budget"), ("skill", "execution_budget"),
])
def test_revision_profile_and_execution_budget_are_required(kind, field):
    payload = suite_spec(kind)
    del payload["suite_config"][field]
    with pytest.raises(ValidationError, match=field):
        ExperimentSpec.model_validate(payload)


@pytest.mark.parametrize("revision", ["latest", "", "unknown", " main "])
def test_ceval_revision_must_be_fixed(revision):
    payload = suite_spec("ceval")
    payload["suite_config"]["dataset_revision"] = revision
    with pytest.raises(ValidationError, match="dataset_revision"):
        ExperimentSpec.model_validate(payload)


@pytest.mark.parametrize("change", [
    {"scope": "anything"}, {"split": "training"}, {"few_shot": True},
    {"few_shot": 33}, {"seed": True}, {"few_shot_split": "val"},
    {"execution_profile": "latest"}, {"execution_profile": {"max_calls": 1}},
])
def test_ceval_scalar_types_and_selection_match_standalone_constraints(change):
    payload = suite_spec("ceval")
    payload["suite_config"].update(change)
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(payload)


@pytest.mark.parametrize("change", [
    {"workflow_ref": "workflow@latest"}, {"agent_mode": "auto"},
    {"target": "external-runtime"}, {"cases": ["case-1", "case-1"]},
    {"cases": []}, {"cases": [""]}, {"cases": [True]},
    {"execution_budget": {}}, {"execution_budget": {"max_steps": True}},
    {"execution_budget": {"max_steps": 65}},
    {"execution_budget": {"max_steps": 3, "max_calls": 1}},
    {"execution_budget": {"max_steps": 3, "per_call_timeout_enforced": True}},
])
def test_scenario_configuration_has_only_bounded_builtin_agent_request_fields(change):
    payload = suite_spec()
    payload["suite_config"].update(change)
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(payload)


def test_scenario_case_payload_preserves_standalone_business_identity():
    payload = suite_spec()
    payload["suite_config"]["cases"] = [
        {"case_id": "case-2", "business_id": "order-1"}, "case-1",
    ]
    parsed = ExperimentSpec.model_validate(payload)
    assert parsed.suite_config.cases[0].business_id == "order-1"
    assert parsed.suite_config.cases[1] == "case-1"
    for bad in (
        [{"case_id": "case-1", "input": "unconsumed"}],
        ["case-1", {"case_id": "case-1", "business_id": "order-1"}],
    ):
        payload["suite_config"]["cases"] = bad
        with pytest.raises(ValidationError):
            ExperimentSpec.model_validate(payload)


@pytest.mark.parametrize("config_type", [ScenarioExperimentConfig, SkillExperimentConfig])
@pytest.mark.parametrize("container_type", [set, frozenset])
def test_workflow_configs_reject_unordered_case_containers(config_type, container_type):
    kind = "scenario" if config_type is ScenarioExperimentConfig else "skill"
    payload = suite_spec(kind)["suite_config"]
    payload["cases"] = container_type(["case-2", "case-1"])
    with pytest.raises(ValidationError, match="cases.*|ordered"):
        config_type.model_validate(payload)


@pytest.mark.parametrize("config_type", [ScenarioExperimentConfig, SkillExperimentConfig])
@pytest.mark.parametrize("container_type", [list, tuple])
def test_workflow_configs_preserve_ordered_mixed_cases_and_json_roundtrip(config_type, container_type):
    kind = "scenario" if config_type is ScenarioExperimentConfig else "skill"
    payload = suite_spec(kind)["suite_config"]
    payload["cases"] = container_type([
        ScenarioExperimentCase(case_id="case-2", business_id="order-1"), "case-1",
    ])
    parsed = config_type.model_validate(payload)
    assert parsed.cases == (
        ScenarioExperimentCase(case_id="case-2", business_id="order-1"), "case-1",
    )
    assert config_type.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize("axis", [
    ["helper@9", "helper@2"], ["no-skill", "no-skill", "helper@2"],
    ["no-skill", "helper@9", "helper@9"], ["no-skill", "helper@9"],
    ["no-skill", "helper@9", "helper@2", "helper@3"],
    ["no-skill", "helper", "helper@2"], ["no-skill", "helper@latest", "helper@2"],
])
def test_skill_axis_requires_exactly_one_no_skill_and_two_distinct_fixed_refs(axis):
    payload = suite_spec("skill")
    payload["factors"]["skill_version"] = axis
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(payload)


@pytest.mark.parametrize("factor", ["model_profile", "skill_version"])
def test_skill_requires_both_explicit_factors(factor):
    payload = suite_spec("skill")
    del payload["factors"][factor]
    with pytest.raises(ValidationError, match=factor):
        ExperimentSpec.model_validate(payload)


@pytest.mark.parametrize("factor", ["runtime_version", "prompt_version", "reasoning_level"])
def test_skill_does_not_consume_unimplemented_factors(factor):
    payload = suite_spec("skill")
    payload["factors"][factor] = ["variant@1"]
    with pytest.raises(ValidationError, match=factor):
        ExperimentSpec.model_validate(payload)


def test_skill_matrix_count_and_cell_identity_algorithm_remain_unchanged():
    from motte_contracts.experiment import compute_cell_id
    from motte_sdk.experiments import _expand_matrix

    payload = suite_spec("skill")
    payload["factors"]["model_profile"] = ["model-a", "model-b"]
    payload["repeats"] = 2
    spec = ExperimentSpec.model_validate(payload)
    matrix = _expand_matrix(spec)
    assert spec.cell_count() == len(matrix) == 12
    ids = {compute_cell_id(spec.experiment_id, spec.version, assignment, repeat)
           for assignment, repeat in matrix}
    assert len(ids) == 12
    changed = deepcopy(payload)
    changed["suite_config"]["agent_mode"] = "native-tool"
    changed_spec = ExperimentSpec.model_validate(changed)
    assert spec.content_hash() != changed_spec.content_hash()
    assert ids == {compute_cell_id(spec.experiment_id, spec.version, assignment, repeat)
                   for assignment, repeat in _expand_matrix(changed_spec)}
