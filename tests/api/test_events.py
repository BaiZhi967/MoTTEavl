import json

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_storage.run_store import SQLiteRunStore

FIXTURE = {
    "case-1": {"output": {"n": 1}, "expected": {"n": 1}},
    "case-2": {"output": {"n": 2}, "expected": {"n": 2}},
}

FULL_EVENT_TYPES = [
    "queued",
    "preparing",
    "running",
    "model_response",
    "model_response",
    "collecting",
    "scoring",
    "score",
    "score",
    "completed",
]


def _completed_client(tmp_path):
    client = TestClient(create_app(SQLiteRunStore(tmp_path / "api.db")))
    run = client.post("/api/v1/runs", json={"scenario_version": "replay@1", "case_ids": list(FIXTURE)}).json()
    client.post(f"/api/v1/runs/{run['id']}/replay", json={"cases": FIXTURE})
    return client, run["id"]


def _sse_events(text):
    events = []
    for block in text.strip().split("\n\n"):
        data = [line[len("data: "):] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            events.append(json.loads(data[0]))
    return events


def test_sse_streams_full_persisted_event_trace(tmp_path):
    client, run_id = _completed_client(tmp_path)
    response = client.get(f"/api/v1/runs/{run_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert [event["type"] for event in events] == FULL_EVENT_TYPES
    assert [event["seq"] for event in events] == list(range(1, len(FULL_EVENT_TYPES) + 1))


def test_sse_resumes_after_seq_query_param(tmp_path):
    client, run_id = _completed_client(tmp_path)
    response = client.get(f"/api/v1/runs/{run_id}/events?after=7")
    events = _sse_events(response.text)
    assert [event["seq"] for event in events] == [8, 9, 10]
    assert "id: 8" in response.text


def test_sse_resumes_from_last_event_id_header(tmp_path):
    client, run_id = _completed_client(tmp_path)
    response = client.get(f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "5"})
    events = _sse_events(response.text)
    assert [event["seq"] for event in events] == [6, 7, 8, 9, 10]


def test_sse_returns_404_for_unknown_run(tmp_path):
    client = TestClient(create_app(SQLiteRunStore(tmp_path / "api.db")))
    assert client.get("/api/v1/runs/run-404/events").status_code == 404
