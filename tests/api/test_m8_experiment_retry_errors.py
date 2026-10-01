"""Public experiment retry errors remain structured and side-effect free."""

from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_storage.run_store import InMemoryRunStore
from tests.sdk.test_m8_native_agent_experiments import native_agent_environment, native_agent_spec


@pytest.mark.parametrize(("state", "status", "code"), [
    ("missing-cell", 404, "CELL_NOT_FOUND"),
    ("missing-spec", 404, "EXPERIMENT_NOT_FOUND"),
    ("missing-run", 404, "RUN_NOT_FOUND"),
    ("unallocated", 422, "CELL_NOT_ALLOCATED"),
    ("unsupported", 422, "SUITE_UNSUPPORTED"),
])
def test_public_retry_maps_domain_errors_without_creating_a_child(state, status, code):
    source, resources, service = native_agent_environment()
    created = service.create(native_agent_spec(factors={"model_profile": ["agent-a"]}))
    cell = source.experiments.get_cell(created["cells"][0]["cell_id"])
    spec = source.experiments.get_spec(cell["experiment_id"], cell["experiment_version"])
    store = InMemoryRunStore()
    if state == "unsupported":
        spec["task_ref"] = {
            "suite": "terminal-bench-harbor", "scenario_version": "terminal-bench-harbor@1",
        }
        # Even a stored Cell pointing at an existing Run cannot open an
        # unsupported experiment path through the retry endpoint.
        store.runs.create(source.runs.get(cell["run_id"]))
    if state == "unallocated":
        cell.update({"run_id": None, "allocation_status": "pending"})
    if state != "missing-spec":
        store.experiments.put_spec(spec)
    if state != "missing-cell":
        store.experiments.put_cell(cell)
    before_runs = deepcopy(store.runs.list())
    before_cells = deepcopy(store.experiments.list_cells(cell["experiment_id"]))
    client = TestClient(create_app(store, resources), raise_server_exceptions=False)
    response = client.post(
        f"/api/v1/experiments/cells/{cell['cell_id']}/retry",
        json={"reason": "retry frozen execution"},
    )
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["message"]
    assert store.runs.list() == before_runs
    assert store.experiments.list_cells(cell["experiment_id"]) == before_cells
