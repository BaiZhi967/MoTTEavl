"""Offline public API/CLI → ordinary Worker, frozen restart and operator retry."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_cli.main import main
from motte_sdk import MotteClient
from motte_sdk.experiments import ExperimentService
from motte_sdk.service import RunService
from motte_storage.factory import create_run_store
from motte_storage.resource_store import SQLiteResourceStore
from tests.sdk.sync_asgi import SyncASGITransport
from tests.sdk.test_experiment_scenario_skill import seed_resources, suite_spec


def local_environment(tmp_path, monkeypatch):
    path = tmp_path / "workflow.db"
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MOTTE_SCENARIO_FIXTURE_ROOT", str(tmp_path / "fixtures"))
    store = create_run_store(str(path))
    resources = SQLiteResourceStore(str(path))
    seed_resources(resources)
    return path, store, resources


def no_recompile(*args, **kwargs):
    raise AssertionError("replay/retry must not compile or query current resources")


@pytest.mark.parametrize("create_via", ["api", "local-cli", "server-cli"])
def test_skill_matrix_api_cli_preview_create_recovery_and_retry(
    tmp_path, monkeypatch, capsys, create_via,
):
    path, store, resources = local_environment(tmp_path, monkeypatch)
    app = create_app(store, resources)
    client = TestClient(app)
    monkeypatch.setattr("motte_cli.remote.build_client", lambda args: MotteClient(
        "http://testserver", transport=SyncASGITransport(app), retries=0,
    ))
    import motte_sdk.skill_ablation as ablation
    real_plan = ablation.plan_skill_ablation
    plans = []

    def plan(**kwargs):
        plans.append(kwargs)
        return real_plan(**kwargs)

    monkeypatch.setattr(ablation, "plan_skill_ablation", plan)
    payload = suite_spec()
    modes = {"local-cli": ["--mode", "local", "--db", str(path)],
             "server-cli": ["--mode", "server", "--api-url", "http://testserver"]}

    def cli(command, *, mode="local-cli", args=(), spec=True):
        code = main(["experiment", command, *args, *modes[mode],
                     *(["--spec", json.dumps(payload)] if spec else [])])
        captured = capsys.readouterr()
        assert code == 0, captured.err
        return json.loads(captured.out)

    response = client.post("/api/v1/experiments/preview", json=payload)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert cli("preview") == cli("preview", mode="server-cli") == preview
    assert preview["cell_count"] == 12
    assert preview["max_potential_calls"] == 384
    assert len(plans) == 6  # Three compilations, two model groups each.
    assert store.experiments.list_specs() == []
    assert store.runs.list() == []
    if create_via == "api":
        response = client.post("/api/v1/experiments", json={
            **payload, "_preview_hash": preview["preview_hash"],
        })
        assert response.status_code == 202, response.text
        created = response.json()
    else:
        created = cli("create", mode=create_via, args=("--preview-hash", preview["preview_hash"]))
    assert created["failed"] == []
    assert created["allocated"] == 12
    assert len(store.runs.list()) == 12
    assert len(plans) == 8
    frozen_cells = deepcopy(store.experiments.list_cells("workflow-matrix", "1"))
    initial_ids = {cell["cell_id"] for cell in frozen_cells}
    assert initial_ids == {cell["cell_id"] for cell in preview["cells"]}
    assert len({cell["run_id"] for cell in frozen_cells}) == 12

    # From this point only the frozen persisted inputs are available.
    monkeypatch.setattr("motte_sdk.resolve.prepare_run", no_recompile)
    monkeypatch.setattr(ablation, "plan_skill_ablation", no_recompile)
    for repository in (resources.models, resources.skills, resources.workflows,
                       resources.fixtures, resources.providers, resources.scenarios):
        monkeypatch.setattr(repository, "get", no_recompile)
    replay = client.post("/api/v1/experiments", json={
        **payload, "_preview_hash": preview["preview_hash"],
    })
    assert replay.status_code == 202, replay.text
    assert cli("create", args=("--preview-hash", preview["preview_hash"]))["allocated"] == 0
    restarted_store = create_run_store(str(path))
    runs = RunService(restarted_store)
    service = ExperimentService(restarted_store, runs, resources=None)
    assert service.allocate("workflow-matrix", "1")["allocated"] == 0
    assert len(restarted_store.runs.list()) == 12

    requests = []

    def synthetic_provider(manifest):
        def complete(request):
            requests.append((manifest["model"], manifest.get("skill_arm"), request))
            return {"content": '{"action":"final","answer":"done"}'}
        return SimpleNamespace(provider=SimpleNamespace(complete=complete))

    monkeypatch.setattr("motte_sdk.agent_backend.build_agent_provider", synthetic_provider)
    worker = WorkerLoop(runs, reporter=WorkerReporter(enabled=False))
    for cell in frozen_cells:
        finished = worker.claim_and_execute(cell["run_id"])
        assert finished["status"] == "completed", finished
        view = runs.get_run(cell["run_id"])
        assert len(view["scores"]) == 2
        assert all(score["passed"] is True for score in view["scores"])
        assert restarted_store.external_jobs.jobs_for_run(cell["run_id"]) == []
    assert len(requests) == 12 * 2 * 2  # two workflow sends, not an extra arm dimension
    for model, arm, request in requests:
        assert request.model == model
        if arm == "no-skill":
            assert "Frozen guard" not in request.system
        else:
            from motte_contracts.identity import canonical_sha256
            from tests.skill.test_skill_injection_fixture import sections_of

            assert "Frozen guard " + arm[-1] in request.system
            manifest = next(cell["prepared_run"]["manifest"] for cell in frozen_cells
                            if cell["prepared_run"]["manifest"]["model"] == model
                            and cell["prepared_run"]["manifest"]["skill_arm"] == arm)
            entry = manifest["resource_snapshots"]["skill_injection"]["declaration"]["entries"][0]
            header, rendered = sections_of(request.system)[0]
            assert canonical_sha256({"skill": header.split()[0], "rendered": rendered,
                                     "position": 0}) == entry["rendered_hash"]
    assert restarted_store.experiments.list_cells("workflow-matrix", "1") == frozen_cells

    chosen = next(cell for cell in frozen_cells if
                  cell["prepared_run"]["manifest"]["skill_arm"] == "skill-v2")
    original = deepcopy(restarted_store.runs.get(chosen["run_id"]))
    # Public local CLI operator retry creates exactly one superseding Run.
    code = main(["experiment", "retry-cell", chosen["cell_id"], "--db", str(path),
                 "--reason", "offline operator retry"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    retry = json.loads(captured.out)
    assert len(restarted_store.runs.list()) == 13
    child = restarted_store.runs.get(retry["run_id"])
    assert child["parent_run_id"] == chosen["run_id"]
    for field in ("manifest", "requested_manifest", "case_ids", "scenario_version"):
        assert child[field] == original[field]
    assert worker.claim_and_execute(child["id"])["status"] == "completed"
    assert restarted_store.runs.get(chosen["run_id"]) == original
    after = restarted_store.experiments.list_cells("workflow-matrix", "1")
    assert len(after) == 12
    for before, saved in zip(frozen_cells, after, strict=True):
        assert {k: v for k, v in before.items() if k != "superseding_run_ids"} == {
            k: v for k, v in saved.items() if k != "superseding_run_ids"
        }
        assert saved["superseding_run_ids"] == ([child["id"]] if saved["cell_id"] == chosen["cell_id"] else [])
    assert preview["cell_count"] == 12 and preview["max_potential_calls"] == 384
    assert len(plans) == 8


@pytest.mark.parametrize("crash_after_run", [False, True])
def test_skill_partial_allocation_restart_uses_frozen_cells(tmp_path, monkeypatch, crash_after_run):
    path, store, resources = local_environment(tmp_path, monkeypatch)
    service = ExperimentService(store, RunService(store), resources=resources)
    real_create = service._create_cell_run

    def crash(*args):
        if crash_after_run:
            real_create(*args)
        raise SystemExit("simulated process exit")

    monkeypatch.setattr(service, "_create_cell_run", crash)
    with pytest.raises(SystemExit, match="simulated"):
        service.create(suite_spec())
    assert len(store.runs.list()) == int(crash_after_run)
    frozen = deepcopy(store.experiments.list_cells("workflow-matrix", "1"))
    assert len(frozen) == 12
    monkeypatch.setattr("motte_sdk.resolve.prepare_run", no_recompile)
    monkeypatch.setattr("motte_sdk.skill_ablation.plan_skill_ablation", no_recompile)
    store = create_run_store(str(path))
    restarted = ExperimentService(store, RunService(store), resources=None)
    resumed = restarted.allocate("workflow-matrix", "1")
    assert resumed["failed"] == []
    assert len(store.runs.list()) == 12
    for cell in store.experiments.list_cells("workflow-matrix", "1"):
        before = next(item for item in frozen if item["cell_id"] == cell["cell_id"])
        assert cell["prepared_run"] == before["prepared_run"]
        assert store.runs.get(cell["run_id"])["manifest"] == before["prepared_run"]["manifest"]
    assert restarted.allocate("workflow-matrix", "1")["allocated"] == 0
    assert len(store.runs.list()) == 12


@pytest.mark.parametrize("cancel_via", ["api", "cli"])
def test_skill_cancel_api_restart_and_retry_preserves_initial_audit(
    tmp_path, monkeypatch, capsys, cancel_via,
):
    path, store, resources = local_environment(tmp_path, monkeypatch)
    app = create_app(store, resources)
    client = TestClient(app)
    response = client.post("/api/v1/experiments", json=suite_spec())
    assert response.status_code == 202, response.text
    created = response.json()
    before = deepcopy(store.experiments.list_cells("workflow-matrix", "1"))
    if cancel_via == "api":
        response = client.post("/api/v1/experiments/workflow-matrix/cancel", json={"version": "1"})
        assert response.status_code == 200, response.text
        cancelled = response.json()
    else:
        code = main(["experiment", "cancel", "workflow-matrix", "--db", str(path),
                     "--version", "1"])
        captured = capsys.readouterr()
        assert code == 0, captured.err
        cancelled = json.loads(captured.out)
    assert len(cancelled["cancelled_runs"]) == 12
    assert all(run["status"] == "cancelled" for run in store.runs.list())
    monkeypatch.setattr("motte_sdk.resolve.prepare_run", no_recompile)
    monkeypatch.setattr("motte_sdk.skill_ablation.plan_skill_ablation", no_recompile)
    restarted_store = create_run_store(str(path))
    service = ExperimentService(restarted_store, RunService(restarted_store), resources=None)
    assert service.allocate("workflow-matrix", "1")["allocated"] == 0
    cell_id = created["cells"][0]["cell_id"]
    parent = deepcopy(restarted_store.runs.get(created["cells"][0]["run_id"]))
    response = client.post(f"/api/v1/experiments/cells/{cell_id}/retry",
                           json={"reason": "retry only this cancelled Cell"})
    assert response.status_code == 200, response.text
    retry = response.json()
    child = restarted_store.runs.get(retry["run_id"])
    assert child["manifest"] == parent["manifest"]
    assert child["parent_run_id"] == parent["id"]
    assert restarted_store.runs.get(parent["id"]) == parent
    assert len(restarted_store.runs.list()) == 13
    for cell in restarted_store.experiments.list_cells("workflow-matrix", "1"):
        initial = next(item for item in before if item["cell_id"] == cell["cell_id"])
        assert cell["prepared_run"] == initial["prepared_run"]
        assert cell["run_id"] == initial["run_id"]
        assert cell["allocation_status"] == "cancelled"


def test_scenario_api_cli_worker_freezes_fixture_and_keeps_gold_out_of_requests(
    tmp_path, monkeypatch, capsys,
):
    from tests.sdk.test_experiment_scenario_skill import publish_fixture_workflow

    path, store, resources = local_environment(tmp_path, monkeypatch)
    fixture = publish_fixture_workflow(resources)
    payload = suite_spec("scenario")
    payload["suite_config"].update(workflow_ref="greeting@2", agent_mode="native-tool")
    monkeypatch.setattr("motte_sdk.skill_ablation.plan_skill_ablation", no_recompile)
    client = TestClient(create_app(store, resources))
    preview = client.post("/api/v1/experiments/preview", json=payload)
    assert preview.status_code == 200, preview.text
    code = main(["experiment", "preview", "--mode", "local", "--db", str(path),
                 "--spec", json.dumps(payload)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert json.loads(captured.out) == preview.json()
    assert preview.json()["max_potential_calls"] == 4 * 2 * 8 * 2
    assert store.runs.list() == []
    created = client.post("/api/v1/experiments", json={
        **payload, "_preview_hash": preview.json()["preview_hash"],
    })
    assert created.status_code == 202, created.text
    assert created.json()["failed"] == []
    assert len(store.runs.list()) == 4
    requests = []

    def provider(manifest):
        def complete(request):
            requests.append(request)
            return {"content": "done", "tool_calls": []}
        return SimpleNamespace(provider=SimpleNamespace(complete=complete))

    monkeypatch.setattr("motte_sdk.agent_backend.build_agent_provider", provider)
    monkeypatch.setattr("motte_sdk.resolve.prepare_run", no_recompile)
    worker = WorkerLoop(RunService(store), reporter=WorkerReporter(enabled=False))
    for cell in created.json()["cells"]:
        run = store.runs.get(cell["run_id"])
        assert run["manifest"]["fixture_snapshot"]["frozen-order@1"]["record"] == fixture
        assert worker.claim_and_execute(cell["run_id"])["status"] == "completed"
    assert len(requests) == 4 * 2 * 2
    assert all("private expected result" not in repr(request) for request in requests)


@pytest.mark.parametrize("budget,max_cells", [(383, 12), (384, 11)])
def test_skill_api_cli_budget_rejection_has_no_partial_publication(
    tmp_path, monkeypatch, capsys, budget, max_cells,
):
    path, store, resources = local_environment(tmp_path, monkeypatch)
    payload = suite_spec(budget_policy={"max_total_calls": budget}, max_cells=max_cells)
    client = TestClient(create_app(store, resources))
    response = client.post("/api/v1/experiments", json=payload)
    assert response.status_code == 422, response.text
    code = main(["experiment", "create", "--db", str(path), "--spec", json.dumps(payload)])
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.err)["error"]["code"] == "EXPERIMENT_INVALID"
    assert store.experiments.list_specs() == []
    assert store.experiments.list_cells("workflow-matrix", "1") == []
    assert store.runs.list() == []
