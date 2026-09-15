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


def test_rescore_endpoint_recomputes_scores_without_new_model_calls(tmp_path):
    client = TestClient(create_app(SQLiteRunStore(tmp_path / "api.db")))
    run = client.post("/api/v1/runs", json={"scenario_version": "replay@1", "case_ids": ["case-1"]}).json()
    fixture = {"case-1": {"output": {"n": 1}, "expected": {"n": 1}}}
    client.post(f"/api/v1/runs/{run['id']}/replay", json={"cases": fixture})
    rescored = client.post(f"/api/v1/runs/{run['id']}/rescore").json()
    assert rescored["rescored"] is True
    assert rescored["scores"] == [{"case_id": "case-1", "passed": True}]
    model_responses = [
        event
        for event in client.app.state.run_service.events(run["id"])
        if event["type"] == "model_response"
    ]
    assert len(model_responses) == 1


def test_create_run_rejects_invalid_provider_config_before_any_paid_call():
    store = InMemoryRunStore()
    client = TestClient(create_app(store))
    response = client.post(
        "/api/v1/runs",
        json={
            "scenario_version": "direct-llm@1",
            "manifest": {
                "provider": {
                    "kind": "openai_compatible",
                    "base_url": "https://api.example.test/v1",
                    "model": "m",
                    "parameters": {"logit_bias": 0},
                }
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_PARAMETER"
    assert store.runs.list() == []


def test_create_run_accepts_valid_openai_compatible_provider_config():
    client = TestClient(create_app(InMemoryRunStore()))
    response = client.post(
        "/api/v1/runs",
        json={
            "scenario_version": "direct-llm@1",
            "manifest": {
                "provider": {
                    "kind": "openai_compatible",
                    "base_url": "https://api.example.test/v1",
                    "model": "m",
                    "parameters": {"temperature": 0.3},
                }
            },
            "case_ids": ["case-1"],
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_direct_llm_run_requires_provider_manifest():
    store = InMemoryRunStore()
    response = TestClient(create_app(store)).post(
        "/api/v1/runs", json={"scenario_version": "direct-llm@1"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROVIDER_REQUIRED"
    assert store.runs.list() == []


def test_provider_reference_is_resolved_before_run_is_queued():
    from motte_storage.resource_store import InMemoryResourceStore

    resources = InMemoryResourceStore()
    resources.providers.put({
        "name": "local", "kind": "openai_compatible", "model": "m",
        "base_url": "http://localhost:8001/v1",
    })
    client = TestClient(create_app(InMemoryRunStore(), resource_store=resources))
    response = client.post("/api/v1/runs", json={
        "scenario_version": "direct-llm@1", "manifest": {"provider": "local"},
        "case_ids": ["case-1"],
    })
    assert response.status_code == 202
    assert response.json()["manifest"]["provider"]["model"] == "m"
    resources.providers.put({"name": "local", "model": "changed"})
    assert client.get(f"/api/v1/runs/{response.json()['id']}").json()["manifest"]["provider"]["model"] == "m"


def test_create_run_rejects_unknown_scenario_version():
    store = InMemoryRunStore()
    response = TestClient(create_app(store)).post(
        "/api/v1/runs", json={"scenario_version": "missing@99"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SCENARIO_NOT_FOUND"
    assert store.runs.list() == []


def test_create_run_rejects_nested_plaintext_credentials():
    store = InMemoryRunStore()
    client = TestClient(create_app(store))
    response = client.post(
        "/api/v1/runs",
        json={
            "scenario_version": "direct-llm@1",
            "manifest": {
                "provider": {
                    "kind": "openai_compatible",
                    "base_url": "https://api.example.test/v1",
                    "model": "m",
                    "api_key": "sk-review-secret",
                }
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CREDENTIALS_REJECTED"
    assert store.runs.list() == []


def test_messages_endpoint_accepts_active_run_message():
    client = TestClient(create_app(InMemoryRunStore()))
    run = client.post("/api/v1/runs", json={"scenario_version": "replay@1"}).json()
    response = client.post(f"/api/v1/runs/{run['id']}/messages", json={"content": "continue"})
    assert response.status_code == 202
    assert response.json()["run_id"] == run["id"]
