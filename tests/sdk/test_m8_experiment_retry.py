"""Experiment retries keep executable frozen inputs, never a fresh selector shell."""

from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from motte_contracts.experiment import ExperimentSpec, FactorAssignment
from motte_sdk.direct_llm import import_builtin_dataset
from motte_sdk.experiments import ExperimentError, ExperimentService, deterministic_run_id
from motte_sdk.service import RunService
from motte_storage.run_store import InMemoryRunStore
from tests.sdk.test_m8_native_agent_experiments import native_agent_environment, native_agent_spec


def retry_environment(suite):
    store, resources, service = native_agent_environment()
    payload = native_agent_spec(factors={"model_profile": ["agent-a"]})
    if suite == "direct-llm":
        imported = import_builtin_dataset("direct-llm-exact-answer", resources=resources)
        payload.update({
            "task_ref": {"suite": suite, "scenario_version": "direct-llm-exact-answer@1"},
            "selected_case_keys": [],
            "controlled_conditions": {"max_output_tokens": 512},
        })
        assert imported
    created = service.create(payload)
    assert created["failed"] == []
    cell = store.experiments.get_cell(created["cells"][0]["cell_id"])
    return store, resources, service, cell


@pytest.mark.parametrize("suite", ["direct-llm", "agent-tasks"])
@pytest.mark.parametrize("legacy_cell", [False, True])
def test_retry_preserves_frozen_execution_and_selection_without_current_resources(
    suite, legacy_cell,
):
    store, _resources, service, cell = retry_environment(suite)
    original = store.runs.get(cell["run_id"])
    assert original["case_ids"]
    if legacy_cell:
        # Existing persisted Cells predate prepared_run. Recreate that public
        # storage shape without reaching into repository implementation details.
        legacy_store = InMemoryRunStore()
        legacy_store.experiments.put_spec(store.experiments.get_spec(
            cell["experiment_id"], cell["experiment_version"],
        ))
        historical = {key: value for key, value in cell.items() if key != "prepared_run"}
        legacy_store.experiments.put_cell(historical)
        legacy_store.runs.create(original)
        store = legacy_store
    # A restart without access to current resources must still retry the pinned
    # execution. Re-resolution would either fail or silently select new inputs.
    service = ExperimentService(store, RunService(store), resources=None)
    original_before = deepcopy(store.runs.get(cell["run_id"]))
    retried = service.retry_cell(cell["cell_id"], reason="retry the frozen configuration")
    child = store.runs.get(retried["run_id"])
    assert child["case_ids"] == original["case_ids"]
    assert child["manifest"] == original["manifest"]
    assert child["requested_manifest"] == original["requested_manifest"]
    assert child["scenario_version"] == original["scenario_version"]
    assert child["parent_run_id"] == original["id"]
    assert child["id"] == deterministic_run_id(cell["cell_id"]) + "-r1"
    assert child["status"] == "queued"
    assert store.runs.get(original["id"]) == original_before
    assert store.experiments.get_cell(cell["cell_id"])["superseding_run_ids"] == [child["id"]]


def test_native_retry_is_consumed_by_the_ordinary_worker(tmp_path, monkeypatch):
    from apps.worker.motte_worker.reporting import WorkerReporter
    from apps.worker.motte_worker.runtime import WorkerLoop

    monkeypatch.setenv("MOTTE_AGENT_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    store, _resources, service, cell = retry_environment("agent-tasks")
    requests = []

    def scripted_provider(manifest):
        def complete(request):
            requests.append(request)
            if len(requests) % 2:
                return {"content": "", "tool_calls": [{
                    "id": f"write-{len(requests)}", "name": "write_file",
                    "arguments": json.dumps({"path": "result.txt", "content": "done"}),
                }]}
            return {"content": "done", "tool_calls": []}

        return SimpleNamespace(provider=SimpleNamespace(complete=complete))

    monkeypatch.setattr("motte_sdk.agent_backend.build_agent_provider", scripted_provider)
    service.run_service.cancel(cell["run_id"], reason="retry on an independent Run")
    parent_before = deepcopy(store.runs.get(cell["run_id"]))
    retried = service.retry_cell(cell["cell_id"], reason="operator requested")
    worker = WorkerLoop(service.run_service, reporter=WorkerReporter(enabled=False))
    finished = worker.claim_and_execute(retried["run_id"])
    assert finished["status"] == "completed", finished.get("error")
    view = service.run_service.get_run(retried["run_id"])
    assert {row["case_id"] for row in view["scores"]} == {"task-1", "task-2"}
    assert all(row["passed"] is True for row in view["scores"])
    assert len(requests) == 4
    assert all(request.tools for request in requests)
    assert len(store.scoring_passes.list_for_run(retried["run_id"])) == 1
    assert store.runs.get(cell["run_id"]) == parent_before


def test_retry_cannot_bypass_the_unsupported_harbor_experiment_gate():
    from pathlib import Path

    from motte_sdk import terminalbench as tb

    store = InMemoryRunStore()
    runs = RunService(store)
    service = ExperimentService(store, runs)
    spec = ExperimentSpec.model_validate(native_agent_spec(
        task_ref={"suite": tb.SUITE, "scenario_version": tb.SCENARIO_VERSION},
    ))
    assignment = FactorAssignment(values=())
    cell = service._cell_payload(spec, assignment, 0)
    run_id = deterministic_run_id(cell["cell_id"])
    task_root = Path(__file__).resolve().parents[1] / "fixtures/benchmarks/harbor/tasks"
    record = tb.prepare_terminal_bench_dataset(
        store, task_root=str(task_root), source_id="retry-fixture", dataset_revision="1",
    )
    inputs = tb.build_run_inputs(
        record=record, run_id=run_id, job_id=f"job-{run_id}",
        profile=tb.terminal_bench_profile(n_trials=2),
    )
    runs.create_run(
        tb.SCENARIO_VERSION, inputs["manifest"], inputs["case_ids"], run_id=run_id,
        requested_manifest={"benchmark": tb.BENCHMARK_ID},
    )
    store.experiments.put_spec(spec.model_dump(mode="json"))
    store.experiments.put_cell(cell)
    assert store.experiments.claim_cell(cell["cell_id"])
    store.experiments.complete_cell(cell["cell_id"], run_id)
    original = deepcopy(store.runs.get(run_id))
    with pytest.raises(ExperimentError, match="SUITE_UNSUPPORTED"):
        service.retry_cell(cell["cell_id"], reason="unsupported frozen Run")
    assert store.runs.list() == [original]
    assert store.experiments.get_cell(cell["cell_id"])["superseding_run_ids"] == []


def test_retry_refuses_a_missing_initial_run_without_persisting_a_child():
    store, _resources, _service, cell = retry_environment("agent-tasks")
    missing_parent_store = InMemoryRunStore()
    missing_parent_store.experiments.put_spec(store.experiments.get_spec(
        cell["experiment_id"], cell["experiment_version"],
    ))
    missing_parent_store.experiments.put_cell(cell)
    service = ExperimentService(missing_parent_store, RunService(missing_parent_store))
    with pytest.raises(ExperimentError, match="RUN_NOT_FOUND"):
        service.retry_cell(cell["cell_id"], reason="missing frozen evidence")
    assert missing_parent_store.runs.list() == []
    assert missing_parent_store.experiments.get_cell(cell["cell_id"]) == cell
