"""External limits are not transport proof. All fixtures are synthetic and offline."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from motte_contracts.experiment import ExperimentSpec
from motte_sdk.experiment_assemblers import _legacy_call_bound
from motte_sdk.experiments import ExperimentError, ExperimentService, _expand_matrix
from motte_sdk.service import RunService
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore
from tests.sdk.test_m8_experiment_retry import retry_environment
from tests.sdk.test_m8_native_agent_experiments import native_agent_environment, native_agent_spec


PROFILES = [
    ("harbor-claude-turns", {"max_turns": 1}),
    ("harbor-claude-budget", {"max_budget_usd": 0.01}),
    ("harbor-task-trials", {"task_count": 1, "n_trials": 1}),
    ("harbor-oracle", {"n_trials": 1}),
    ("claude-cli", {"total_timeout": 1}),
    ("codex-cli", {"total_timeout": 1}),
    ("pi-agent", {"max_steps": 1, "stream_fn_calls": 1}),
    ("opaque-runtime", {"max_steps": 1, "provider_transport": 0}),
]


def external_manifest(native, profile, limits):
    """Model historical stored evidence, never an accepted public manifest input."""
    manifest = deepcopy(native)
    if profile.startswith("harbor-"):
        manifest["execution"] = {"backend_id": "external-benchmark", "backend_version": "1"}
        manifest["external_benchmark"] = {
            "adapter_id": "terminal-bench-harbor", "runner_version": "harbor-0.23.0",
            "profile": {"agent_id": "oracle" if profile.endswith("oracle") else "claude-code",
                        **limits},
            "retry_policy": {"runner": 0, "provider_transport": 0, "operator": 0},
        }
    else:
        manifest["runtime"] = profile + "@1"
        manifest["runtime_profile"] = {"runtime": profile + "@1", "budgets": limits}
        manifest["execution"] = {"backend_id": profile, "backend_version": "1"}
    return manifest


def state(store, experiment_id, version):
    return deepcopy({
        "specs": store.experiments.list_specs(),
        "cells": store.experiments.list_cells(experiment_id, version),
        "runs": store.runs.list(),
        "jobs": store.external_jobs._jobs,
    })


@pytest.fixture
def no_execution(monkeypatch):
    """No process/provider transport may be reached by any rejection test."""
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        pytest.fail("external execution must not be reached")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    monkeypatch.setattr("httpx.Client.send", forbidden)
    yield calls
    assert calls == []


@pytest.mark.parametrize("profile,limits", PROFILES)
@pytest.mark.parametrize("operation", ["preview", "create"])
def test_external_limits_reject_public_requests_without_writes(profile, limits, operation, no_execution):
    store, _resources, service = native_agent_environment()
    payload = native_agent_spec(factors={"model_profile": ["agent-a"]})
    if profile.startswith("harbor-"):
        payload["task_ref"] = {
            "suite": "terminal-bench-harbor", "scenario_version": "terminal-bench-harbor@1",
        }
        payload["trials_per_run"] = 1
        payload["selected_case_keys"] = ["task-1"]
        payload["controlled_conditions"] = {"execution_profile": profile, **limits}
        expected = "SUITE_UNSUPPORTED"
    else:
        payload["factors"]["runtime_version"] = [profile + "@1"]
        payload["controlled_conditions"] = limits
        expected = "FACTOR_UNSUPPORTED"
    before = state(store, payload["experiment_id"], payload["version"])
    with pytest.raises(ExperimentError) as error:
        getattr(service, operation)(payload)
    assert error.value.code == expected
    assert state(store, payload["experiment_id"], payload["version"]) == before


@pytest.mark.parametrize("profile,limits", PROFILES)
def test_legacy_formula_cannot_promote_external_caps_to_a_bound(profile, limits, no_execution):
    store, _resources, _service, cell = retry_environment("agent-tasks")
    original = store.runs.get(cell["run_id"])
    spec = ExperimentSpec.model_validate(store.experiments.get_spec(
        cell["experiment_id"], cell["experiment_version"],
    ))
    with pytest.raises(ExperimentError) as error:
        _legacy_call_bound(spec, external_manifest(original["manifest"], profile, limits),
                           tuple(original["case_ids"]))
    assert error.value.code == "EXPERIMENT_BUDGET_UNPROVABLE"


@pytest.mark.parametrize("profile,limits", PROFILES)
@pytest.mark.parametrize("operation", ["allocate", "retry"])
@pytest.mark.parametrize("frozen_cell", [False, True])
def test_stored_external_runtime_cannot_borrow_native_spec_on_replay(
    profile, limits, operation, frozen_cell, no_execution, monkeypatch,
):
    source, _resources, _service, cell = retry_environment("agent-tasks")
    original = source.runs.get(cell["run_id"])
    original["manifest"] = external_manifest(original["manifest"], profile, limits)
    store = InMemoryRunStore()
    spec = source.experiments.get_spec(cell["experiment_id"], cell["experiment_version"])
    store.experiments.put_spec(spec)
    if frozen_cell:
        cell["prepared_run"]["manifest"] = deepcopy(original["manifest"])
    else:
        cell.pop("prepared_run", None)
    store.runs.create(original)
    store.experiments.put_cell(cell)
    service = ExperimentService(store, RunService(store), resources=None)
    before = state(store, cell["experiment_id"], cell["experiment_version"])
    # Refreezing Trial identity is a future supported-profile interface, not a
    # route around admission. This must fail before refreeze or Run creation.
    monkeypatch.setattr(service.run_service, "_refreeze_trial_identity",
                        lambda *a, **kw: pytest.fail("unproved profile reached refreeze"))
    with pytest.raises(ExperimentError) as error:
        if operation == "retry":
            service.retry_cell(cell["cell_id"], reason="must reject unsupported stored profile")
        else:
            service.allocate(cell["experiment_id"], cell["experiment_version"])
    assert error.value.code == "EXPERIMENT_BUDGET_UNPROVABLE"
    assert state(store, cell["experiment_id"], cell["experiment_version"]) == before


@pytest.mark.parametrize("field", ["manifest", "call_bound", "execution_profile", "retry_policy"])
def test_external_proof_or_manifest_is_not_a_public_experiment_input(field, no_execution):
    store, _resources, service = native_agent_environment()
    payload = native_agent_spec(factors={"model_profile": ["agent-a"]})
    payload[field] = {"max_calls": 0, "runtime": "opaque-runtime@1"}
    before = state(store, payload["experiment_id"], payload["version"])
    for operation in (service.preview, service.create):
        with pytest.raises(ValidationError):
            operation(payload)
    assert state(store, payload["experiment_id"], payload["version"]) == before


@pytest.mark.parametrize("limit", [
    "max_turns", "max_budget_usd", "task_count", "n_trials", "total_timeout",
    "max_steps", "stream_fn_calls", "provider_transport",
])
def test_scalar_external_limits_cannot_be_silently_ignored(limit, no_execution):
    store, _resources, service = native_agent_environment()
    payload = native_agent_spec(controlled_conditions={limit: 1})
    before = state(store, payload["experiment_id"], payload["version"])
    for operation in (service.preview, service.create):
        with pytest.raises(ExperimentError, match="CONTROLLED_CONDITION_INVALID"):
            operation(payload)
    assert state(store, payload["experiment_id"], payload["version"]) == before


@pytest.mark.parametrize("declaration", [
    {"runtime": "opaque@1"},
    {"runtime_profile": {"budgets": {"max_steps": 1}}},
    {"runtime_snapshot": {"model_control": "runner-configured"}},
    {"agent": "claude-cli@1"},
    {"execution": {"backend_id": "opaque-runtime", "backend_version": "1"}},
    {"execution": {"backend_id": "builtin-agent", "backend_version": "unknown"}},
])
def test_external_declaration_cannot_hide_behind_native_identity(declaration, no_execution):
    store, _resources, _service, cell = retry_environment("agent-tasks")
    original = store.runs.get(cell["run_id"])
    spec = ExperimentSpec.model_validate(store.experiments.get_spec(
        cell["experiment_id"], cell["experiment_version"],
    ))
    with pytest.raises(ExperimentError, match="EXPERIMENT_BUDGET_UNPROVABLE"):
        _legacy_call_bound(spec, {**original["manifest"], **declaration}, tuple(original["case_ids"]))


def test_pending_external_cell_rejects_whole_recovery_before_any_claim(no_execution):
    source, _resources, _service, original_cell = retry_environment("agent-tasks")
    spec = ExperimentSpec.model_validate(source.experiments.get_spec(
        original_cell["experiment_id"], original_cell["experiment_version"],
    ))
    payload = spec.model_dump(mode="json")
    payload["repeats"] = 2
    spec = ExperimentSpec.model_validate(payload)
    store = InMemoryRunStore()
    service = ExperimentService(store, RunService(store), resources=None)
    cells = []
    for assignment, repeat in _expand_matrix(spec):
        cell = service._cell_payload(spec, assignment, repeat)
        cell["prepared_run"] = deepcopy(original_cell["prepared_run"])
        if repeat == 1:
            cell["prepared_run"]["manifest"] = external_manifest(
                cell["prepared_run"]["manifest"], "pi-agent", {"max_steps": 1},
            )
        cells.append(cell)
    store.experiments.publish_spec_and_cells(payload, cells)
    before = state(store, spec.experiment_id, spec.version)
    with pytest.raises(ExperimentError, match="EXPERIMENT_BUDGET_UNPROVABLE"):
        service.allocate(spec.experiment_id, spec.version)
    assert state(store, spec.experiment_id, spec.version) == before
    assert store.runs.list() == []


@pytest.mark.parametrize("profile,limits", PROFILES)
@pytest.mark.parametrize("frozen_cell", [False, True])
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_rejected_external_create_replay_does_not_bind_request_key(
    profile, limits, frozen_cell, backend, tmp_path, no_execution,
):
    import sqlite3

    source, _resources, _service, cell = retry_environment("agent-tasks")
    original = source.runs.get(cell["run_id"])
    original["manifest"] = external_manifest(original["manifest"], profile, limits)
    if frozen_cell:
        cell["prepared_run"]["manifest"] = deepcopy(original["manifest"])
    else:
        cell.pop("prepared_run", None)
    path = tmp_path / "replay.db"
    store = InMemoryRunStore() if backend == "memory" else SQLiteRunStore(path)
    spec = source.experiments.get_spec(cell["experiment_id"], cell["experiment_version"])
    store.experiments.publish_spec_and_cells(spec, [cell])
    store.runs.create(original)
    service = ExperimentService(store, RunService(store), resources=None)
    key = "experiment:create:rejected-replay"
    service._requests.bind("unrelated-existing", "unrelated-hash", "unrelated-run")
    preserved = service._requests.get("unrelated-existing")

    def snapshot():
        if backend == "sqlite":
            # Includes the durable registry and all Run/Cell/Job/superseding
            # rows, rather than only observing the test's original Run.
            with sqlite3.connect(path) as connection:
                return tuple(connection.iterdump())
        return state(store, cell["experiment_id"], cell["experiment_version"])

    before = snapshot()
    assert service._requests.get(key) is None
    with pytest.raises(ExperimentError, match="EXPERIMENT_BUDGET_UNPROVABLE"):
        service.create(spec, request_key="rejected-replay")
    if backend == "sqlite":
        reopened = SQLiteRunStore(path)
        service = ExperimentService(reopened, RunService(reopened), resources=None)
    assert service._requests.get(key) is None
    assert service._requests.get("unrelated-existing") == preserved
    assert snapshot() == before


@pytest.mark.parametrize("suite", ["native", "ceval"])
def test_supported_create_replay_binds_new_key_without_resource_resolution(suite, no_execution):
    if suite == "native":
        store, _resources, _service, cell = retry_environment("agent-tasks")
        payload = store.experiments.get_spec(cell["experiment_id"], cell["experiment_version"])
    else:
        from tests.sdk.test_experiment_ceval import environment, suite_spec

        store, _resources, service, _dataset = environment()
        payload = suite_spec()
        service.create(payload)
        payload = store.experiments.get_spec(payload["experiment_id"], payload["version"])
    before = state(store, payload["experiment_id"], payload["version"])
    service = ExperimentService(store, RunService(store), resources=None)
    result = service.create(payload, request_key="supported-replay")
    assert result["created"] is False and result["allocated"] == 0
    binding = service._requests.get("experiment:create:supported-replay")
    assert binding["run_id"] == payload["experiment_id"] + "@" + payload["version"]
    service.create(payload, request_key="supported-replay")
    with pytest.raises(ExperimentError, match="REQUEST_KEY_CONFLICT"):
        service.create({**payload, "reason": "different content"}, request_key="supported-replay")
    assert service._requests.get("experiment:create:supported-replay") == binding
    assert state(store, payload["experiment_id"], payload["version"]) == before
