from fastapi.testclient import TestClient
from apps.api.app.main import create_app
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore


def test_create_run_returns_queued_run():
    response = TestClient(create_app(InMemoryRunStore())).post("/api/v1/runs", json={"scenario_version": "json_extract@1"})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_create_run_rejects_unsupported_capability():
    store = InMemoryRunStore()
    response = TestClient(create_app(store)).post("/api/v1/runs", json={"scenario_version": "vision@1"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_CAPABILITY_UNSUPPORTED"
    assert store.runs.list() == []


def test_api_reopens_durable_repository(tmp_path):
    path = tmp_path / "api.db"
    first = TestClient(create_app(SQLiteRunStore(path)))
    created = first.post("/api/v1/runs", json={"scenario_version": "replay@1"}).json()
    second = TestClient(create_app(SQLiteRunStore(path)))
    assert second.get(f"/api/v1/runs/{created['id']}").json()["status"] == "queued"


def test_replay_run_completes_through_api(tmp_path):
    from motte_sdk.replay_run import ReplayProvider

    app = create_app(SQLiteRunStore(tmp_path / "api.db"))
    client = TestClient(app)
    run = client.post("/api/v1/runs", json={"scenario_version": "replay@1"}).json()
    fixture = {"case-1": {"output": {"name": "Ada"}, "expected": {"name": "Ada"}}}
    response = client.post(f"/api/v1/runs/{run['id']}/replay", json={"cases": fixture})
    assert response.status_code == 200
    assert response.json()["scores"] == [{"case_id": "case-1", "passed": True}]


def test_retry_endpoint_creates_child_run_for_cancelled_run():
    store = InMemoryRunStore()
    client = TestClient(create_app(store))
    run = client.post("/api/v1/runs", json={"scenario_version": "replay@1"}).json()
    cancelled = client.post(f"/api/v1/runs/{run['id']}/cancel", json={"reason": "operator request"})
    assert cancelled.json()["cancellation"] == {"reason": "operator request"}
    retried = client.post(f"/api/v1/runs/{run['id']}/retry")
    assert retried.status_code == 200
    child = retried.json()
    assert child["parent_run_id"] == run["id"]
    assert child["status"] == "queued"


def test_retry_endpoint_rejects_completed_run(tmp_path):
    client = TestClient(create_app(SQLiteRunStore(tmp_path / "api.db")))
    run = client.post("/api/v1/runs", json={"scenario_version": "replay@1"}).json()
    client.post(f"/api/v1/runs/{run['id']}/replay", json={"cases": {"case-1": {"output": 1, "expected": 1}}})
    conflict = client.post(f"/api/v1/runs/{run['id']}/retry")
    assert conflict.status_code == 409
