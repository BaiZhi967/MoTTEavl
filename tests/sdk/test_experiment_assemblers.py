"""Read-only assembly boundary, frozen legacy parity and exact Skill axis."""

from importlib import import_module

import pytest
from pydantic import ValidationError

from motte_contracts.experiment import ExperimentSpec
from motte_sdk.agent_tasks import persist_agent_tasks_dataset
from motte_sdk.experiments import ExperimentError, ExperimentService, _expand_matrix
from motte_sdk.resolve import prepare_run
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

from tests.contract.test_experiment_suite_config import suite_spec


def environment():
    store = InMemoryRunStore()
    resources = InMemoryResourceStore()
    resources.providers.put({
        "name": "local", "kind": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
    })
    resources.models.put({
        "id": "model-a", "provider": "local", "model": "synthetic", "capabilities": {},
        "supports_tools": True, "max_output_tokens": 2048,
        "lifecycle": "published", "published_at": "2026-09-29T00:00:00Z",
    })
    persist_agent_tasks_dataset({
        "name": "dataset", "version": "1", "cases": [
            {"case_id": case, "input": "Write done", "fixture": {}}
            for case in ("case-1", "case-2")
        ],
    }, resources, version="1")
    for version in ("9", "2"):
        resources.publish_skill({
            "skill_id": "helper", "version": version, "kind": "instruction",
            "instruction": f"Instruction {version}", "injection_mode": "system-prompt",
            "lifecycle": "published", "published_at": "2026-09-29T00:00:00Z",
        })
    service = ExperimentService(store, RunService(store), resources=resources)
    return store, resources, service


def assert_empty(store):
    assert store.experiments.list_specs() == []
    assert store.experiments.list_cells("legacy") == []
    assert store.runs.list() == []


def test_legacy_assembler_delegates_standalone_preparation_and_proves_existing_bound():
    assembler = import_module("motte_sdk.experiment_assemblers")
    store, resources, service = environment()
    payload = suite_spec()
    del payload["suite_config"]
    payload["task_ref"]["suite"] = "agent-tasks"
    payload["selected_case_keys"] = ["case-2", "case-1"]
    payload["controlled_conditions"] = {"agent_mode": "native-tool"}
    spec = ExperimentSpec.model_validate(payload)
    assignment, _ = _expand_matrix(spec)[0]
    assembled = assembler.assemble_experiment_cell(spec, assignment, resources=resources, store=store)
    requested = service._build_manifest(spec, {"factor_assignment": assignment.model_dump()})
    manifest, cases = prepare_run("dataset@1", requested, [], resources)
    assert assembled.scenario_version == "dataset@1"
    assert assembled.requested_manifest == requested
    assert assembled.manifest == manifest
    # Managed standalone selection canonicalizes IDs; freeze that actual order.
    assert assembled.case_ids == tuple(cases) == ("case-1", "case-2")
    assert assembled.resource_hashes["model_profile"] == (
        manifest["resource_snapshots"]["model_profile"]["content_hash"]
    )
    assert assembled.call_bound.max_calls == 16
    assert assembled.call_bound.execution_boundary == "builtin-agent@1"
    assert assembled.call_bound.controlled_retries == {"provider": 0}
    assert_empty(store)


def test_skill_axis_requires_one_no_skill_and_two_distinct_published_refs(monkeypatch):
    assembler = import_module("motte_sdk.experiment_assemblers")
    store, resources, _service = environment()
    lookups = []
    real_get = resources.skills.get

    def get(*keys):
        lookups.append(keys)
        assert keys[0] != "no-skill", "the no-skill literal must bypass ordinary resource lookup"
        return real_get(*keys)

    monkeypatch.setattr(resources.skills, "get", get)
    payload = suite_spec("skill")
    payload["factors"]["skill_version"] = ["helper@9", "no-skill", "helper@2"]
    spec = ExperimentSpec.model_validate(payload)
    arms = assembler.resolve_skill_axis(spec, resources=resources)
    assert [(arm.arm_id, arm.skill_ref) for arm in arms] == [
        ("no-skill", None), ("skill-v1", "helper@9"), ("skill-v2", "helper@2"),
    ]
    assert arms[0].snapshot is None
    assert arms[1].snapshot["version"] == "9"
    assert lookups == [("helper", "9"), ("helper", "2")]
    assert_empty(store)


@pytest.mark.parametrize("axis", [
    ["helper@9", "helper@2"], ["no-skill", "no-skill", "helper@2"],
    ["no-skill", "helper@9", "helper@9"], ["no-skill", "helper@9"],
    ["no-skill", "helper@9", "helper@2", "helper@3"],
    ["no-skill", "helper", "helper@2"], ["no-skill", "helper@latest", "helper@2"],
    ["no-skill", "helper@1", "helper@2"],
])
def test_invalid_skill_axis_leaves_no_spec_cell_or_run(axis):
    store, _resources, service = environment()
    payload = suite_spec("skill")
    payload["factors"]["skill_version"] = axis
    for operation in (service.preview, service.create):
        with pytest.raises((ValidationError, ExperimentError)):
            operation(payload)
    assert_empty(store)


def test_missing_published_skill_reports_the_resource_error_before_unimplemented_branch():
    store, _resources, service = environment()
    payload = suite_spec("skill")
    payload["factors"]["skill_version"] = ["no-skill", "helper@1", "helper@2"]
    for operation in (service.preview, service.create):
        with pytest.raises(ExperimentError) as error:
            operation(payload)
        assert error.value.code == "SKILL_NOT_PUBLISHED"
        assert "helper@1" in str(error.value)
    assert_empty(store)


@pytest.mark.parametrize("kind", ["ceval"])
def test_unimplemented_typed_suites_fail_closed_before_persistence(kind):
    store, _resources, service = environment()
    for operation in (service.preview, service.create):
        with pytest.raises(ExperimentError) as error:
            operation(suite_spec(kind))
        assert error.value.code == "SUITE_ASSEMBLER_UNIMPLEMENTED"
    assert_empty(store)


def test_skill_assembler_requires_compile_time_internal_group_context():
    assembler = import_module("motte_sdk.experiment_assemblers")
    store, resources, _ = environment()
    spec = ExperimentSpec.model_validate(suite_spec("skill"))
    assignment, _ = _expand_matrix(spec)[0]
    with pytest.raises(ExperimentError) as error:
        assembler.assemble_experiment_cell(spec, assignment, resources=resources, store=store)
    assert error.value.code == "SKILL_GROUP_REQUIRED"
    assert_empty(store)


def test_legacy_manifest_builder_cannot_bypass_unimplemented_typed_branch():
    assembler = import_module("motte_sdk.experiment_assemblers")
    store, _resources, _ = environment()
    spec = ExperimentSpec.model_validate(suite_spec())
    assignment, _ = _expand_matrix(spec)[0]
    with pytest.raises(ExperimentError) as error:
        assembler.build_legacy_requested_manifest(spec, assignment)
    assert error.value.code == "SUITE_ASSEMBLER_UNIMPLEMENTED"
    assert_empty(store)


def test_assembler_rejects_assignment_outside_declared_matrix():
    from motte_contracts.experiment import FactorAssignment, FactorValue

    assembler = import_module("motte_sdk.experiment_assemblers")
    store, resources, _ = environment()
    payload = suite_spec()
    del payload["suite_config"]
    payload["task_ref"]["suite"] = "agent-tasks"
    spec = ExperimentSpec.model_validate(payload)
    with pytest.raises(ExperimentError) as error:
        assembler.assemble_experiment_cell(
            spec, FactorAssignment(values=(FactorValue(factor="model_profile", value="other"),)),
            resources=resources, store=store,
        )
    assert error.value.code == "FACTOR_ASSIGNMENT_INVALID"
    assert_empty(store)


def test_skill_preflight_rejects_deprecated_published_versions():
    from motte_skill.versions import deprecate_skill

    store, resources, service = environment()
    deprecate_skill(
        resources.skills, "helper", "2", reason="superseded", deprecated_at="2026-09-30T00:00:00Z",
    )
    for operation in (service.preview, service.create):
        with pytest.raises(ExperimentError) as error:
            operation(suite_spec("skill"))
        assert error.value.code == "SKILL_NOT_PUBLISHED"
        assert "deprecated" in str(error.value)
    assert_empty(store)


@pytest.mark.parametrize("operation", ["allocate", "retry"])
@pytest.mark.parametrize("kind", ["scenario", "ceval"])
def test_stored_frozen_cell_cannot_bypass_typed_suite_guard(operation, kind):
    assembler = import_module("motte_sdk.experiment_assemblers")
    store, resources, service = environment()
    payload = suite_spec(kind)
    legacy = {key: value for key, value in payload.items() if key != "suite_config"}
    legacy["task_ref"] = {"suite": "agent-tasks", "scenario_version": "dataset@1"}
    legacy_spec = ExperimentSpec.model_validate(legacy)
    assignment, _ = _expand_matrix(legacy_spec)[0]
    assembled = assembler.assemble_experiment_cell(
        legacy_spec, assignment, resources=resources, store=store,
    )
    typed = ExperimentSpec.model_validate(payload)
    cell = service._cell_payload(typed, assignment, 0)
    cell["prepared_run"] = {
        "manifest": assembled.manifest, "requested_manifest": assembled.requested_manifest,
        "case_ids": list(assembled.case_ids),
    }
    initial_count = 0
    if operation == "retry":
        original = service.run_service.create_run(
            scenario_version=assembled.scenario_version, manifest=assembled.manifest,
            case_ids=list(assembled.case_ids), requested_manifest=assembled.requested_manifest,
        )
        cell.update(allocation_status="allocated", run_id=original["id"])
        initial_count = 1
    # Model a historical/injected stored Cell; the public create path never allows it.
    store.experiments.publish_spec_and_cells(typed.model_dump(mode="json"), [cell])
    with pytest.raises(ExperimentError) as error:
        if operation == "allocate":
            service.allocate(typed.experiment_id, typed.version)
        else:
            service.retry_cell(cell["cell_id"], reason="test fail-closed replay")
    assert error.value.code == ("FROZEN_INPUT_INVALID" if kind == "scenario"
                                else "SUITE_ASSEMBLER_UNIMPLEMENTED")
    assert len(store.runs.list()) == initial_count
