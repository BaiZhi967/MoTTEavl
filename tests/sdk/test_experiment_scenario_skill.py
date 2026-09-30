"""Typed Workflow Cells retain standalone inputs and existing matrix identities."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from motte_contracts.experiment import ExperimentSpec, FactorAssignment, compute_cell_id
from motte_contracts.hashing import canonical_hash
from motte_sdk import experiment_assemblers as assemblers
from motte_sdk.experiments import ExperimentError, ExperimentService, _expand_matrix
from motte_sdk.resolve import prepare_run
from motte_sdk.service import RunService
from motte_sdk.skill_ablation import ArmSpec, arm_manifests, plan_skill_ablation
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

PUBLISHED = "2026-09-30T00:00:00Z"


def seed_resources(resources):
    resources.providers.put({
        "name": "local", "kind": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
        "max_retries": 1,
    })
    for model in ("model-a", "model-b"):
        resources.models.put({
            "id": model, "provider": "local", "model": model, "capabilities": {},
            "supports_tools": True, "max_output_tokens": 2048,
            "lifecycle": "published", "published_at": PUBLISHED,
        })
    resources.publish_workflow({
        "workflow_id": "greeting", "version": "1", "published_at": PUBLISHED,
        "steps": [
            {"step_id": "first", "kind": "send_message", "message": "Say done"},
            {"step_id": "second", "kind": "send_message", "message": "Say done again"},
        ],
        "limits": {"max_total_steps": 10, "max_turns": 3, "wall_time_sec": 30},
        "target_requirements": {"multi_turn": True, "min_turns": 2},
    })
    resources.scenarios.put({
        "name": "greeting", "version": "1",
        "evaluator": {"evaluator_id": "greeting-policy", "version": "1", "config": {
            "metrics": [{"metric_id": "done", "kind": "response-policy",
                         "source": "final_output", "require": [{"text": "done"}]}],
        }},
    })
    for version in ("1", "2"):
        resources.publish_skill({
            "skill_id": "cancel-guard", "version": version, "kind": "instruction",
            "instruction": f"Frozen guard {version}", "injection_mode": "system-prompt",
            "lifecycle": "published", "published_at": PUBLISHED,
        })


def environment():
    resources = InMemoryResourceStore()
    seed_resources(resources)
    store = InMemoryRunStore()
    return store, resources, ExperimentService(store, RunService(store), resources=resources)


def suite_spec(kind="skill", **overrides):
    config = {
        "kind": kind, "workflow_ref": "greeting@1", "agent_mode": "legacy-json",
        "cases": [{"case_id": "case-2", "business_id": "order-1"}, "case-1"],
        "execution_budget": {"max_steps": 8},
    }
    factors = {"model_profile": ["model-a", "model-b"]}
    if kind == "skill":
        config["budget_policy"] = "same-execution-budget"
        factors["skill_version"] = ["no-skill", "cancel-guard@1", "cancel-guard@2"]
    else:
        config["target"] = "builtin-agent"
    return {
        "experiment_id": "workflow-matrix", "version": "1",
        "task_ref": {"suite": kind, "scenario_version": "greeting@1"},
        "suite_config": config, "factors": factors, "repeats": 2,
        "budget_policy": {"max_total_calls": 384}, "max_cells": 12,
        "created_by": "test", "reason": "offline typed matrix", **overrides,
    }


def assert_empty(store):
    assert store.experiments.list_specs() == []
    assert store.experiments.list_cells("workflow-matrix") == []
    assert store.runs.list() == []
    assert store.external_jobs._jobs == {}


def requested_base(model, *, budget=None, mode="legacy-json"):
    budget = budget or {"max_steps": 8}
    return {
        "model": model, "workflow": "greeting@1", "agent": "builtin-agent@1",
        "agent_config": {"mode": mode, "budget": deepcopy(budget)},
        "budget": deepcopy(budget), "cases": {"case-2": {"business_id": "order-1"}, "case-1": {}},
    }


def group_key(assignment):
    non_skill = FactorAssignment(values=tuple(
        value for value in assignment.values if value.factor != "skill_version"
    ))
    return canonical_hash(non_skill.canonical_payload())


def test_skill_matrix_has_twelve_existing_cell_identities():
    store, _resources, service = environment()
    spec = ExperimentSpec.model_validate(suite_spec())
    preview = service.preview(spec.model_dump(mode="json"))
    assert spec.cell_count() == preview["cell_count"] == len(preview["cells"]) == 12
    assert [cell["cell_id"] for cell in preview["cells"]] == [
        compute_cell_id(spec.experiment_id, spec.version, assignment, repeat)
        for assignment, repeat in _expand_matrix(spec)
    ]
    assert len({cell["cell_id"] for cell in preview["cells"]}) == 12
    assert set(Counter(tuple(sorted(cell["factor_assignment"].items()))
                       for cell in preview["cells"]).values()) == {2}
    assert_empty(store)


def test_skill_matrix_budget_counts_each_cell_once():
    store, _resources, service = environment()
    for limit, max_cells, violation in [(384, 12, None), (383, 12, "BUDGET_EXCEEDED"),
                                        (384, 11, "MATRIX_TOO_LARGE")]:
        payload = suite_spec(budget_policy={"max_total_calls": limit}, max_cells=max_cells)
        preview = service.preview(payload)
        assert preview["max_potential_calls"] == 12 * 2 * 8 * 2 == 384
        assert preview["case_count"] == 2
        assert [v["code"] for v in preview["violations"]] == ([violation] if violation else [])
        if violation:
            with pytest.raises(ExperimentError, match=violation):
                service.create(payload)
        assert_empty(store)


@pytest.mark.parametrize("policy", ["same-execution-budget", "same-total-budget"])
def test_skill_group_plan_and_selected_arm_match_standalone(monkeypatch, policy):
    store, resources, service = environment()
    import motte_sdk.skill_ablation as ablation

    planned = []
    real_plan = ablation.plan_skill_ablation
    real_get = resources.skills.get

    def recording_plan(**kwargs):
        planned.append(deepcopy(kwargs))
        return real_plan(**kwargs)

    def get(name, version):
        assert name != "no-skill"
        return real_get(name, version)

    monkeypatch.setattr(ablation, "plan_skill_ablation", recording_plan)
    monkeypatch.setattr(resources.skills, "get", get)
    payload = suite_spec()
    payload["suite_config"]["budget_policy"] = policy
    spec = ExperimentSpec.model_validate(payload)
    compiled = service._compile(spec, include_prepared=True)
    assert len(planned) == 2
    assert all([a.arm_id for a in item["arms"]] == ["no-skill", "skill-v1", "skill-v2"]
               for item in planned)
    observed_groups = {}
    for (assignment, repeat), cell in zip(_expand_matrix(spec), compiled["cells"], strict=True):
        values = assignment.as_dict()
        key = group_key(assignment)
        base = requested_base(values["model_profile"])
        plan = plan_skill_ablation(
            base_manifest=base, experiment_ref=f"workflow-matrix@1/{key}",
            arms=[ArmSpec("no-skill"), *[
                ArmSpec(f"skill-v{v}", (f"cancel-guard@{v}",), real_get("cancel-guard", v))
                for v in ("1", "2")
            ]], budget_policy=policy, case_keys=["case-2", "case-1"],
        )
        arm_id = {"no-skill": "no-skill", "cancel-guard@1": "skill-v1",
                  "cancel-guard@2": "skill-v2"}[values["skill_version"]]
        requested = arm_manifests(base, plan)[arm_id]
        expected, cases = prepare_run("greeting@1", requested, ["case-2", "case-1"], resources)
        frozen = compiled["prepared_runs"][cell["cell_id"]]
        assert frozen["requested_manifest"] == requested
        assert frozen["manifest"] == expected
        assert frozen["case_ids"] == cases == ["case-2", "case-1"]
        assert "skill_snapshot" not in frozen["manifest"]
        manifest = deepcopy(frozen["manifest"])
        manifest.pop("skills")
        manifest.pop("skill_arm")
        manifest["resource_snapshots"].pop("skill_injection", None)
        assert manifest == observed_groups.setdefault(key, manifest)
        assert frozen["resource_hashes"]["workflow"] == canonical_hash(expected["workflow_snapshot"])
        assert frozen["call_bound"]["max_calls"] == 32
        assert repeat in (0, 1)
    assert_empty(store)


@pytest.mark.parametrize("axis", [
    ["no-skill", "cancel-guard@1", "cancel-guard@2"],
    ["cancel-guard@1", "no-skill", "cancel-guard@2"],
    ["cancel-guard@1", "cancel-guard@2", "no-skill"],
])
def test_skill_ref_order_has_deterministic_mapping_and_preview_identity(axis):
    store, _resources, service = environment()
    payload = suite_spec()
    payload["factors"]["skill_version"] = axis
    original = service.preview(payload)
    created = service.create(payload, expected_preview_hash=original["preview_hash"])
    assert len(store.runs.list()) == 12
    assert created["failed"] == []
    for cell in created["cells"]:
        value = cell["factor_assignment"]["skill_version"]
        run = store.runs.get(cell["run_id"])
        assert run["manifest"]["skill_arm"] == (
            "no-skill" if value == "no-skill" else "skill-v" + value.rsplit("@", 1)[1]
        )
    swapped = deepcopy(payload)
    swapped["factors"]["skill_version"] = ["no-skill", "cancel-guard@2", "cancel-guard@1"]
    changed = service._compile(ExperimentSpec.model_validate(swapped), include_prepared=True)
    assert changed["preview_hash"] != original["preview_hash"]
    for cell in changed["cells"]:
        if cell["factor_assignment"]["skill_version"] == "cancel-guard@2":
            assert changed["prepared_runs"][cell["cell_id"]]["manifest"]["skill_arm"] == "skill-v1"
    with pytest.raises(ValueError, match="immutable"):
        service.create(swapped)
    assert len(store.runs.list()) == 12


@pytest.mark.parametrize("mode", ["native-tool", "legacy-json"])
def test_scenario_cell_preserves_every_execution_budget_and_business_id(mode):
    store, resources, service = environment()
    payload = suite_spec("scenario")
    budget = {"max_steps": 8, "max_tool_calls": 7, "wall_time_sec": 20,
              "per_call_timeout_sec": 3, "max_output_tokens": 512}
    payload["suite_config"].update(agent_mode=mode, execution_budget=budget)
    created = service.create(payload)
    assert created["failed"] == []
    for cell in created["cells"]:
        run = store.runs.get(cell["run_id"])
        requested = requested_base(cell["factor_assignment"]["model_profile"], budget=budget, mode=mode)
        requested["parameters"] = {"max_output_tokens": 512}
        expected, cases = prepare_run("greeting@1", requested, ["case-2", "case-1"], resources)
        assert run["manifest"] == expected
        assert run["case_ids"] == cases
        assert run["manifest"]["evaluation"]["scorer_id"] == "greeting-policy"


@pytest.mark.parametrize("change", ["unknown-target", "fanout", "hidden-gold", "factor", "budget",
                                     "unknown-retries", "selected-cases", "scenario"])
def test_invalid_scenario_inputs_leave_no_executable_objects(change, monkeypatch):
    store, resources, service = environment()
    payload = suite_spec("scenario")
    if change == "unknown-target":
        payload["suite_config"]["target"] = "unproved-agent"
    elif change == "fanout":
        real_get = resources.workflows.get
        monkeypatch.setattr(resources.workflows, "get", lambda *args: {
            **real_get(*args), "targets": ["builtin-agent", "builtin-agent"],
        })
    elif change == "hidden-gold":
        payload["suite_config"]["cases"][0]["gold"] = "hidden solution"
    elif change == "factor":
        payload["factors"]["runtime_version"] = ["unproved@1"]
    elif change == "budget":
        payload["suite_config"]["execution_budget"] = {}
    elif change == "unknown-retries":
        provider = resources.providers.get("local")
        provider.pop("max_retries")
        resources.providers.put(provider)
    elif change == "selected-cases":
        payload["selected_case_keys"] = ["different-case"]
    elif change == "scenario":
        payload["task_ref"]["scenario_version"] = "missing@1"
    for operation in (service.preview, service.create):
        with pytest.raises((ExperimentError, ValidationError)):
            operation(payload)
        assert_empty(store)


def test_skill_non_skill_drift_during_compile_is_rejected(monkeypatch):
    store, resources, service = environment()
    real_prepare = __import__("motte_sdk.resolve", fromlist=["prepare_run"]).prepare_run
    index = 0

    def drifting_prepare(*args, **kwargs):
        nonlocal index
        index += 1
        provider = resources.providers.get("local")
        resources.providers.put({**provider, "base_url": f"http://127.0.0.1:{9000 + index}/v1"})
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr("motte_sdk.resolve.prepare_run", drifting_prepare)
    with pytest.raises(ExperimentError, match="CONDITION_DRIFT"):
        service.create(suite_spec())
    assert_empty(store)


def test_scenario_resource_drift_invalidates_preview_before_persistence():
    store, resources, service = environment()
    payload = suite_spec("scenario")
    preview = service.preview(payload)
    resources.providers.put({**resources.providers.get("local"), "base_url": "http://changed.invalid/v1"})
    with pytest.raises(ExperimentError, match="PREVIEW_STALE"):
        service.create(payload, expected_preview_hash=preview["preview_hash"])
    assert_empty(store)


def test_session_max_steps_is_cumulative_across_workflow_sends(monkeypatch):
    from motte_sdk.scenario_target import BuiltinTargetSession

    store, _resources, service = environment()
    payload = suite_spec("scenario")
    payload["suite_config"]["execution_budget"]["max_steps"] = 1
    result = service.create(payload)
    assert result["failed"] == []
    requests = []

    def complete(request):
        requests.append(request)
        return {"content": '{"action":"final","answer":"done"}'}

    monkeypatch.setattr("motte_sdk.agent_backend.build_agent_provider", lambda manifest:
                        SimpleNamespace(provider=SimpleNamespace(complete=complete)))
    run = store.runs.get(result["cells"][0]["run_id"])
    session = BuiltinTargetSession(run["manifest"], {})
    session.begin()
    assert session.send("first")["termination_reason"] == "final_answer"
    assert session.send("second")["termination_reason"] == "max_steps"
    assert len(requests) == 1
    assert service.preview(payload)["max_potential_calls"] == 4 * 2 * 1 * 2


def test_skill_snapshot_cannot_drift_between_group_plan_and_cell_freeze(monkeypatch):
    from motte_skill.versions import SkillVersion, skill_content_hash

    store, resources, service = environment()
    real_get = resources.skills.get
    real_prepare = __import__("motte_sdk.resolve", fromlist=["prepare_run"]).prepare_run
    tamper = False

    def get(name, version):
        record = real_get(name, version)
        if tamper and version == "1":
            changed = SkillVersion.model_validate({**record, "instruction": "Replaced content",
                                                   "content_hash": None})
            record = {**changed.model_dump(mode="json"), "content_hash": skill_content_hash(changed)}
        return record

    def prepare(*args, **kwargs):
        nonlocal tamper
        tamper = bool(args[1].get("skills"))
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(resources.skills, "get", get)
    monkeypatch.setattr("motte_sdk.resolve.prepare_run", prepare)
    with pytest.raises(ExperimentError, match="SKILL_CONTENT_DRIFT"):
        service.create(suite_spec())
    assert_empty(store)


@pytest.mark.parametrize("entrypoint", ["scenario", "skill-groups"])
def test_direct_assembler_entrypoints_reject_unconsumed_or_outside_matrix_factors(entrypoint):
    from motte_contracts.experiment import FactorValue

    store, resources, _service = environment()
    payload = suite_spec("scenario" if entrypoint == "scenario" else "skill")
    spec = ExperimentSpec.model_validate(payload)
    assignment = FactorAssignment(values=(FactorValue(factor="model_profile", value="model-bogus"),))
    with pytest.raises(ExperimentError, match="FACTOR_ASSIGNMENT_INVALID"):
        if entrypoint == "scenario":
            assemblers.assemble_scenario_cell(spec, assignment, resources=resources, store=store)
        else:
            assemblers.prepare_skill_groups(spec, [assignment], resources=resources, store=store)
    assert_empty(store)


def test_skill_native_loader_is_rejected_before_any_cell_is_published():
    store, resources, service = environment()
    resources.publish_skill({
        "skill_id": "native-only", "version": "1", "kind": "instruction",
        "instruction": "Needs a native loader", "injection_mode": "native-loader",
        "published_at": PUBLISHED,
    })
    payload = suite_spec()
    payload["factors"]["skill_version"] = ["no-skill", "native-only@1", "cancel-guard@2"]
    with pytest.raises(ExperimentError, match="SKILL_EXECUTION_UNSUPPORTED"):
        service.create(payload)
    assert_empty(store)


def test_skill_executable_is_rejected_without_a_proved_builtin_delivery_path():
    from tests.skill.test_skill_injection_fixture import published_executable
    from motte_skill.content_store import ContentAddressedMemoryStore

    store, resources, service = environment()
    resources.content_store = ContentAddressedMemoryStore()
    published_executable(resources)
    payload = suite_spec()
    payload["factors"]["skill_version"] = ["no-skill", "runner@1", "cancel-guard@2"]
    with pytest.raises(ExperimentError, match="SKILL_EXECUTION_UNSUPPORTED"):
        service.create(payload)
    assert_empty(store)


@pytest.mark.parametrize("change", ["backend", "cases", "budget", "arm", "hash", "missing"])
def test_frozen_typed_allocation_validates_inputs_without_resolving_current_resources(change):
    store, _resources, service = environment()
    spec = ExperimentSpec.model_validate(suite_spec())
    compiled = service._compile(spec, include_prepared=True)
    assignment, repeat = _expand_matrix(spec)[0]
    cell = service._cell_payload(spec, assignment, repeat)
    frozen = deepcopy(compiled["prepared_runs"][cell["cell_id"]])
    if change == "backend":
        frozen["manifest"]["execution"]["backend_id"] = "builtin-agent"
    elif change == "cases":
        frozen["case_ids"] = ["unrequested"]
    elif change == "budget":
        frozen["manifest"]["agent_config"]["budget"]["max_steps"] = 64
    elif change == "arm":
        frozen["manifest"]["skill_arm"] = "skill-v2"
    elif change == "hash":
        frozen["resource_hashes"]["workflow"] = "sha256:wrong"
    cell["prepared_run"] = frozen if change != "missing" else None
    store.experiments.publish_spec_and_cells(spec.model_dump(mode="json"), [cell])
    restarted = ExperimentService(store, RunService(store), resources=None)
    with pytest.raises(ExperimentError, match="FROZEN_INPUT"):
        restarted.allocate(spec.experiment_id, spec.version)
    assert store.runs.list() == []
    assert store.external_jobs._jobs == {}


def publish_fixture_workflow(resources):
    from motte_contracts.fixture import FixtureSpec, fixture_content_hash

    spec = FixtureSpec.model_validate({
        "fixture_id": "frozen-order", "version": 1, "kind": "json",
        "initial_data": {"order": {"status": "active", "gold": "private expected result"}},
        "visible_fields": ["order"], "isolation": "per_case", "cleanup": "delete_owned",
        "published_at": PUBLISHED,
    })
    fixture = resources.publish_fixture({**spec.model_dump(mode="json"),
                                         "content_hash": fixture_content_hash(spec)})
    workflow = resources.workflows.get("greeting", "1")
    resources.publish_workflow({**workflow, "version": "2", "fixture_refs": [{
        "fixture_id": "frozen-order", "version": 1, "kind": "json",
        "content_hash": fixture["content_hash"],
    }]})
    return fixture


def test_scenario_fixture_actual_hash_and_content_match_standalone():
    store, resources, service = environment()
    fixture = publish_fixture_workflow(resources)
    payload = suite_spec("scenario")
    payload["suite_config"]["workflow_ref"] = "greeting@2"
    created = service.create(payload)
    assert created["failed"] == []
    for cell in store.experiments.list_cells("workflow-matrix", "1"):
        frozen = cell["prepared_run"]
        expected, cases = prepare_run("greeting@1", frozen["requested_manifest"],
                                     ["case-2", "case-1"], resources)
        assert frozen["manifest"] == expected
        assert frozen["case_ids"] == cases
        snapshot = expected["fixture_snapshot"]
        assert snapshot["frozen-order@1"]["record"] == fixture
        assert frozen["resource_hashes"]["fixture_snapshot"] == canonical_hash(snapshot)


def test_fixture_content_cannot_change_behind_its_claimed_pinned_hash(monkeypatch):
    store, resources, service = environment()
    publish_fixture_workflow(resources)
    real_get = resources.fixtures.get

    def get(*args):
        fixture = real_get(*args)
        fixture["initial_data"]["order"]["status"] = "changed"
        return fixture

    monkeypatch.setattr(resources.fixtures, "get", get)
    payload = suite_spec("scenario")
    payload["suite_config"]["workflow_ref"] = "greeting@2"
    with pytest.raises(ExperimentError, match="FIXTURE_CONTENT_HASH_MISMATCH"):
        service.create(payload)
    assert_empty(store)


def test_scenario_assembler_cannot_silently_drop_a_skill_factor():
    store, resources, _service = environment()
    spec = ExperimentSpec.model_validate(suite_spec())
    assignment = _expand_matrix(spec)[-1][0]
    with pytest.raises(ExperimentError, match="SCENARIO_CONFIG_REQUIRED"):
        assemblers.assemble_scenario_cell(spec, assignment, resources=resources, store=store)
    assert_empty(store)


def compiled_cells(service, spec, compiled):
    """Persisted JSON values deliberately do not retain shared snapshot aliases."""
    import json

    cells = []
    for assignment, repeat in _expand_matrix(spec):
        cell = service._cell_payload(spec, assignment, repeat)
        cell.update(prepared_run=compiled["prepared_runs"][cell["cell_id"]],
                    preview_hash=compiled["preview_hash"], preflight_mode="preview_bound")
        cells.append(cell)
    return json.loads(json.dumps(cells))


@pytest.mark.parametrize("corruption", ["provider", "workflow", "skill-spoof"])
@pytest.mark.parametrize("operation", ["allocate", "retry-cell", "recover-run", "retry-run"])
def test_frozen_executable_content_drift_is_rejected_without_new_runs(corruption, operation):
    from motte_sdk.experiments import deterministic_run_id

    store, _resources, service = environment()
    spec = ExperimentSpec.model_validate(suite_spec())
    compiled = service._compile(spec, include_prepared=True)
    cells = compiled_cells(service, spec, compiled)
    target = next(cell for cell in cells if
                  cell["prepared_run"]["manifest"].get("skill_arm") == "skill-v1")
    donor = next(cell for cell in cells if
                 cell["prepared_run"]["manifest"].get("skill_arm") == "skill-v2")
    original = deepcopy(target["prepared_run"])

    def corrupt(manifest):
        if corruption == "provider":
            manifest["provider"].update(base_url="http://changed.invalid/v1", model="wrong-model")
        elif corruption == "workflow":
            manifest["workflow_snapshot"]["steps"][0]["message"] = "UNFROZEN WORKFLOW CONTENT"
        else:
            injection = deepcopy(donor["prepared_run"]["manifest"]["resource_snapshots"]["skill_injection"])
            injection["content_hash"] = original["resource_hashes"]["skill_injection"]
            manifest["resource_snapshots"]["skill_injection"] = injection

    if operation != "allocate":
        run_manifest = deepcopy(original["manifest"])
        if operation.endswith("run"):
            corrupt(run_manifest)
        run_id = deterministic_run_id(target["cell_id"])
        service.run_service.create_run(
            scenario_version=original["scenario_version"], manifest=run_manifest,
            case_ids=original["case_ids"], requested_manifest=original["requested_manifest"],
            run_id=run_id,
        )
        if operation == "recover-run":
            target["allocation_status"] = "allocating"
        else:
            target.update(allocation_status="allocated", run_id=run_id)
    if operation in {"allocate", "retry-cell"}:
        corrupt(target["prepared_run"]["manifest"])
    store.experiments.publish_spec_and_cells(spec.model_dump(mode="json"), cells)
    original_runs = deepcopy(store.runs.list())
    original_cells = deepcopy(store.experiments.list_cells(spec.experiment_id, spec.version))
    restarted = ExperimentService(store, RunService(store), resources=None)
    with pytest.raises(ExperimentError, match="FROZEN_INPUT"):
        if operation.startswith("retry"):
            restarted.retry_cell(target["cell_id"], reason="retry frozen input")
        else:
            restarted.allocate(spec.experiment_id, spec.version)
    assert store.runs.list() == original_runs
    assert store.experiments.list_cells(spec.experiment_id, spec.version) == original_cells
    assert store.external_jobs._jobs == {}


@pytest.mark.parametrize("operation", ["allocate", "retry"])
def test_frozen_preview_identity_rejects_validly_recompiled_current_inputs(operation):
    from motte_sdk.experiments import deterministic_run_id

    store, resources, service = environment()
    spec = ExperimentSpec.model_validate(suite_spec())
    approved = service._compile(spec, include_prepared=True)
    resources.providers.put({**resources.providers.get("local"),
                             "base_url": "http://changed.invalid/v1"})
    different = service._compile(spec, include_prepared=True)
    assert approved["preview_hash"] != different["preview_hash"]
    # Even correctly recomputed resource digests cannot rebind the original preview.
    different["preview_hash"] = approved["preview_hash"]
    cells = compiled_cells(service, spec, different)
    target = cells[0]
    if operation == "retry":
        original = approved["prepared_runs"][target["cell_id"]]
        run_id = deterministic_run_id(target["cell_id"])
        service.run_service.create_run(
            scenario_version=original["scenario_version"], manifest=original["manifest"],
            case_ids=original["case_ids"], requested_manifest=original["requested_manifest"],
            run_id=run_id,
        )
        target.update(allocation_status="allocated", run_id=run_id)
    store.experiments.publish_spec_and_cells(spec.model_dump(mode="json"), cells)
    before = deepcopy(store.runs.list())
    restarted = ExperimentService(store, RunService(store), resources=None)
    with pytest.raises(ExperimentError, match="FROZEN_INPUT"):
        if operation == "retry":
            restarted.retry_cell(target["cell_id"], reason="cannot adopt new resources")
        else:
            restarted.allocate(spec.experiment_id, spec.version)
    assert store.runs.list() == before
