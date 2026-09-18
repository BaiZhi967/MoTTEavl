from fastapi.testclient import TestClient
from motte_contracts.report import RunReport
from motte_contracts.run import Run

from apps.api.app.main import create_app
from apps.worker.motte_worker.reporting import WorkerReporter
from apps.worker.motte_worker.runtime import WorkerLoop
from motte_storage.run_store import InMemoryRunStore


def test_core_run_api_json_validates_against_public_contracts():
    app = create_app(InMemoryRunStore())
    client = TestClient(app)
    created = client.post(
        "/api/v1/runs",
        json={"scenario_version": "replay@1", "case_ids": ["case-1"]},
    )
    assert created.status_code == 202
    parsed = Run.model_validate(created.json())
    assert parsed.schema_version == 2
    assert parsed.revision >= 1

    fixture = {"case-1": {"output": {"n": 1}, "expected": {"n": 1}}}
    queued = client.post(
        f"/api/v1/runs/{parsed.id}/replay", json={"cases": fixture}
    )
    Run.model_validate(queued.json())
    completed = WorkerLoop(
        app.state.run_service, reporter=WorkerReporter(enabled=False)
    ).claim_and_execute(parsed.id)
    assert completed is not None

    current = client.get(f"/api/v1/runs/{parsed.id}")
    validated = Run.model_validate(current.json())
    assert validated.status.value == "completed"
    assert validated.current_scoring_pass_id is not None

    report = client.get(f"/api/v1/runs/{parsed.id}/report")
    validated_report = RunReport.model_validate(report.json())
    assert validated_report.scoring_pass_id == validated.current_scoring_pass_id


def test_report_can_select_an_immutable_scoring_pass():
    app = create_app(InMemoryRunStore())
    client = TestClient(app)
    run = client.post(
        "/api/v1/runs", json={"scenario_version": "replay@1", "case_ids": ["case-1"]}
    ).json()
    fixture = {"case-1": {"output": 1, "expected": 1}}
    client.post(f"/api/v1/runs/{run['id']}/replay", json={"cases": fixture})
    first = WorkerLoop(
        app.state.run_service, reporter=WorkerReporter(enabled=False)
    ).claim_and_execute(run["id"])
    first_pass = first["current_scoring_pass_id"]

    rescored = client.post(f"/api/v1/runs/{run['id']}/rescore").json()
    second_pass = rescored["current_scoring_pass_id"]
    assert second_pass != first_pass

    passes = client.get(f"/api/v1/runs/{run['id']}/scoring-passes").json()
    assert [item["id"] for item in passes["items"]] == [first_pass, second_pass]
    selected = client.get(
        f"/api/v1/runs/{run['id']}/report", params={"scoring_pass_id": first_pass}
    )
    assert RunReport.model_validate(selected.json()).scoring_pass_id == first_pass


def test_core_openapi_responses_are_not_empty_schemas():
    document = create_app(InMemoryRunStore()).openapi()
    expected = {
        ("/api/v1/runs", "post", "202"): "Run",
        ("/api/v1/runs", "get", "200"): "RunListResponse",
        ("/api/v1/runs/{run_id}", "get", "200"): "Run",
        ("/api/v1/runs/{run_id}/report", "get", "200"): "RunReport",
        ("/api/v1/runs/{run_id}/commands", "get", "200"): "RunCommandListResponse",
        ("/api/v1/runs/{run_id}/scoring-passes", "get", "200"): "ScoringPassListResponse",
    }
    for (path, method, status), schema_name in expected.items():
        schema = document["paths"][path][method]["responses"][status]["content"][
            "application/json"
        ]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{schema_name}"}
