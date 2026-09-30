"""Bounded Agent experiment modes reuse the standalone preparation contract."""

from __future__ import annotations

from copy import deepcopy

import pytest

from motte_sdk.agent_tasks import DEFAULT_RUN_BUDGET, persist_agent_tasks_dataset
from motte_sdk.experiments import ExperimentError, ExperimentService
from motte_sdk.resolve import ManifestResolutionError, prepare_run
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore


def native_agent_environment(*, second_model_overrides=None):
    store = InMemoryRunStore()
    resources = InMemoryResourceStore()
    resources.providers.put({
        "name": "local", "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:9/v1",
    })
    for model in ("agent-a", "agent-b"):
        resources.models.put({
            "id": model, "provider": "local", "model": model,
            "capabilities": {}, "supports_tools": True, "max_output_tokens": 2048,
            "lifecycle": "published", "published_at": "2026-09-29T00:00:00Z",
            **((second_model_overrides or {}) if model == "agent-b" else {}),
        })
    persist_agent_tasks_dataset({
        "name": "m8-native-agent", "version": "1", "cases": [
            {"case_id": case, "input": "Write result.txt containing done.", "fixture": {},
             "expected": {"files": {"result.txt": {"mode": "exact", "expected": "done"}}}}
            for case in ("task-1", "task-2")
        ],
    }, resources, version="1")
    runs = RunService(store)
    return store, resources, ExperimentService(store, runs, resources=resources)


def native_agent_spec(**overrides):
    return {
        "experiment_id": "m8-native", "version": "1",
        "task_ref": {"suite": "agent-tasks", "scenario_version": "m8-native-agent@1"},
        "factors": {"model_profile": ["agent-a", "agent-b"]},
        "selected_case_keys": ["task-2", "task-1"],
        "controlled_conditions": {"agent_mode": "native-tool", "max_output_tokens": 512},
        "budget_policy": {"max_total_calls": 32},
        "created_by": "tester", "reason": "offline bounded Agent experiment",
        **overrides,
    }


@pytest.mark.parametrize("mode", ["native-tool", "legacy-json", None])
def test_agent_mode_matches_standalone_frozen_configuration(mode):
    store, resources, service = native_agent_environment()
    payload = native_agent_spec(controlled_conditions={
        "agent_mode": mode, "max_output_tokens": 512,
    })
    preview = service.preview(payload)
    assert preview["violations"] == []
    assert preview["max_potential_calls"] == 32
    assert store.runs.list() == []
    created = service.create(payload)
    assert created["failed"] == []
    assert len(created["cells"]) == 2
    for cell in created["cells"]:
        run = store.runs.get(cell["run_id"])
        requested = {
            "model": run["requested_manifest"]["model"],
            "agent": {"mode": mode or "legacy-json"},
            "parameters": {"max_output_tokens": 512},
            "case_selection": {"mode": "ids", "case_ids": ["task-2", "task-1"]},
        }
        standalone, case_ids = prepare_run("m8-native-agent@1", requested, [], resources)
        assert run["requested_manifest"] == requested
        assert run["case_ids"] == case_ids
        assert run["manifest"] == standalone
        config = run["manifest"]["agent_config"]
        assert config["mode"] == (mode or "legacy-json")
        assert config["prompt_version"] == (
            "builtin-react-native@1" if mode == "native-tool" else "builtin-react-legacy@2"
        )
        assert config["tools"] == ["list_files", "read_file", "write_file"]
        assert config["budget"] == DEFAULT_RUN_BUDGET


def test_native_agent_budget_counts_every_selected_case_and_model_step():
    store, _resources, service = native_agent_environment()
    payload = native_agent_spec(budget_policy={"max_total_calls": 31})
    preview = service.preview(payload)
    assert preview["max_potential_calls"] == 32
    assert [item["code"] for item in preview["violations"]] == ["BUDGET_EXCEEDED"]
    with pytest.raises(ExperimentError, match="BUDGET_EXCEEDED"):
        service.create(payload)
    assert store.experiments.list_specs() == []
    assert store.experiments.list_cells("m8-native") == []
    assert store.runs.list() == []


@pytest.mark.parametrize("mode", ["auto", "native", "", True, 1])
def test_invalid_agent_mode_fails_before_persistence(mode):
    store, _resources, service = native_agent_environment()
    payload = native_agent_spec(controlled_conditions={"agent_mode": mode})
    for operation in (service.preview, service.create):
        with pytest.raises(ExperimentError, match="agent_mode must be") as error:
            operation(payload)
        assert error.value.code == "CONTROLLED_CONDITION_INVALID"
    assert store.experiments.list_specs() == []
    assert store.experiments.list_cells("m8-native") == []
    assert store.runs.list() == []


@pytest.mark.parametrize("suite", ["direct-llm", "gsm8k"])
def test_agent_mode_cannot_be_silently_ignored_by_other_suites(suite):
    store, _resources, service = native_agent_environment()
    payload = native_agent_spec(task_ref={"suite": suite, "scenario_version": "unused@1"})
    for operation in (service.preview, service.create):
        with pytest.raises(ExperimentError, match="agent_mode is only valid for agent-tasks"):
            operation(payload)
    assert store.experiments.list_specs() == []
    assert store.runs.list() == []


@pytest.mark.parametrize("change", [{"supports_tools": False}, {"lifecycle": "draft"}])
def test_native_agent_model_preflight_matches_standalone_and_leaves_no_partial_matrix(change):
    store, resources, service = native_agent_environment(second_model_overrides=change)
    with pytest.raises(ManifestResolutionError) as standalone_error:
        prepare_run("m8-native-agent@1", {
            "model": "agent-b", "agent": {"mode": "native-tool"},
        }, [], resources)
    payload = native_agent_spec()
    for operation in (service.preview, service.create):
        with pytest.raises(ExperimentError) as error:
            operation(payload)
        assert error.value.code == standalone_error.value.code
        assert str(standalone_error.value) in str(error.value)
    assert store.experiments.list_specs() == []
    assert store.experiments.list_cells("m8-native") == []
    assert store.runs.list() == []


@pytest.mark.parametrize("factor", ["runtime_version", "prompt_version", "skill_version"])
def test_native_agent_keeps_unimplemented_variant_factors_fail_closed(factor):
    store, _resources, service = native_agent_environment()
    payload = native_agent_spec(factors={"model_profile": ["agent-a"], factor: ["variant@1"]})
    for operation in (service.preview, service.create):
        with pytest.raises(ExperimentError, match="FACTOR_UNSUPPORTED"):
            operation(payload)
    assert store.experiments.list_specs() == []
    assert store.runs.list() == []


def test_native_agent_mode_does_not_enable_other_suites():
    store, _resources, service = native_agent_environment()
    payload = deepcopy(native_agent_spec())
    payload["task_ref"]["suite"] = "harbor"
    with pytest.raises(ExperimentError, match="SUITE_UNSUPPORTED"):
        service.create(payload)
    assert store.experiments.list_specs() == []
    assert store.runs.list() == []
