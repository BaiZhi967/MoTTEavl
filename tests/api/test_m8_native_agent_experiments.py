"""Two native-tool experiment configurations through API, Dispatcher and scoring."""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_sdk.dispatcher import RunDispatcher
from tests.sdk.test_m8_native_agent_experiments import native_agent_environment, native_agent_spec


def test_native_agent_experiment_executes_two_configurations_without_paid_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_AGENT_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    store, resources, _service = native_agent_environment()
    application = create_app(store, resources)
    client = TestClient(application)
    requests = []

    def scripted_provider(manifest):
        # A fresh backend instance is created for each Run. Each case takes one
        # real native write_file step and one final-answer step.
        call_index = 0

        def complete(request):
            nonlocal call_index
            requests.append((manifest["provider"]["model"], request))
            call_index += 1
            if call_index % 2:
                return {"content": "", "tool_calls": [{
                    "id": f"write-{call_index}", "name": "write_file",
                    "arguments": json.dumps({"path": "result.txt", "content": "done"}),
                }]}
            return {"content": "done", "tool_calls": []}

        return SimpleNamespace(provider=SimpleNamespace(complete=complete))

    monkeypatch.setattr("motte_sdk.agent_backend.build_agent_provider", scripted_provider)
    payload = native_agent_spec()
    preview = client.post("/api/v1/experiments/preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["violations"] == []
    assert preview.json()["max_potential_calls"] == 32
    assert requests == []
    assert store.runs.list() == []
    created = client.post("/api/v1/experiments", json={
        **payload, "_preview_hash": preview.json()["preview_hash"],
    })
    assert created.status_code == 202, created.text
    assert created.json()["failed"] == []
    assert requests == []
    cells = created.json()["cells"]
    assert len(cells) == 2
    dispatcher = RunDispatcher(application.state.run_service)
    for cell in cells:
        run_id = cell["run_id"]
        finished = dispatcher.dispatch(run_id)
        assert finished["status"] == "completed", finished.get("error")
        view = client.get(f"/api/v1/runs/{run_id}").json()
        assert view["manifest"]["agent_config"]["mode"] == "native-tool"
        assert len(view["scores"]) == 2
        assert all(score["passed"] is True for score in view["scores"])
        assert {score["case_id"] for score in view["scores"]} == {"task-1", "task-2"}
        passes = client.get(f"/api/v1/runs/{run_id}/scoring-passes").json()
        assert passes["total"] == 1
        for case in ("task-1", "task-2"):
            artifact = client.get(
                f"/api/v1/runs/{run_id}/cases/{case}/artifacts/content?path=result.txt"
            )
            assert artifact.status_code == 200, artifact.text
            assert artifact.json()["content"] == "done"
            assert artifact.json()["sha256_matches"] is True
    assert len(requests) == 8
    assert {model for model, _request in requests} == {"agent-a", "agent-b"}
    assert all(request.tools for _model, request in requests)
    assert dispatcher.claim() is None
    replay = client.post("/api/v1/experiments", json=payload)
    assert replay.status_code == 202, replay.text
    assert len(store.runs.list()) == 2
    assert len(requests) == 8


def test_native_agent_unsupported_model_rejects_entire_api_matrix():
    store, resources, _service = native_agent_environment(
        second_model_overrides={"supports_tools": False},
    )
    client = TestClient(create_app(store, resources))
    for endpoint in ("/api/v1/experiments/preview", "/api/v1/experiments"):
        response = client.post(endpoint, json=native_agent_spec())
        assert response.status_code == 422, response.text
        assert "does not support tools" in response.text
    assert store.experiments.list_specs() == []
    assert store.experiments.list_cells("m8-native") == []
    assert store.runs.list() == []
