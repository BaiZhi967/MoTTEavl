from fastapi.testclient import TestClient
from apps.api.app.main import create_app
from motte_storage.repositories import InMemoryRepository, SQLiteRepository


def test_create_run_returns_queued_run():
    response = TestClient(create_app(InMemoryRepository())).post("/api/v1/runs", json={"scenario_version": "json_extract@1"})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_create_run_rejects_unsupported_capability():
    repository = InMemoryRepository()
    response = TestClient(create_app(repository)).post("/api/v1/runs", json={"scenario_version": "vision@1"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_CAPABILITY_UNSUPPORTED"
    assert repository.list() == []


def test_api_reopens_durable_repository(tmp_path):
    path = tmp_path / "api.db"
    first = TestClient(create_app(SQLiteRepository(path)))
    created = first.post("/api/v1/runs", json={"scenario_version": "replay@1"}).json()
    second = TestClient(create_app(SQLiteRepository(path)))
    assert second.get(f"/api/v1/runs/{created['id']}").json()["status"] == "queued"
