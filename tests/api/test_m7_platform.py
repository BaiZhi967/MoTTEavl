"""M7 服务端共享契约测试：幂等创建、capabilities、SSE gap/snapshot、安全与维护屏障。

对应协议 docs/protocols/sdk-and-migration.md frozen@1 的 §1.2/§1.3/§2/§6/§9/§10。
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from motte_storage.platform import platform_for
from motte_storage.run_store import SQLiteRunStore

from apps.api.app.main import create_app


def _sqlite_app(tmp_path, **kwargs):
    store = SQLiteRunStore(tmp_path / "m7-platform.db")
    return create_app(store=store, **kwargs), store


def _create_body(**overrides):
    body = {
        "scenario_version": "replay@1",
        "manifest": {
            "replay_fixture": {"case-1": {"output": "ok", "expected": "ok"}},
        },
        "case_ids": ["case-1"],
    }
    body.update(overrides)
    return body


# ------------------------------------------------------------------ capabilities


def test_capabilities_endpoint_shape(tmp_path):
    app, _store = _sqlite_app(tmp_path)
    client = TestClient(app)
    response = client.get("/api/v1/capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "motteavl"
    assert payload["api_version"] == "v1"
    for feature in (
        "idempotent_run_create", "sse_cursor", "sse_gap", "events_snapshot",
        "maintenance_mode", "gate_export",
    ):
        assert payload["features"][feature] is True


# ------------------------------------------------------------------- idempotency


def test_run_create_idempotent_same_key_same_body(tmp_path):
    app, _store = _sqlite_app(tmp_path)
    client = TestClient(app)
    body = _create_body(request_key="payment-safe-1")
    first = client.post("/api/v1/runs", json=body)
    assert first.status_code == 202
    run_id = first.json()["id"]
    second = client.post("/api/v1/runs", json=body)
    assert second.status_code == 200
    assert second.json()["id"] == run_id
    assert second.json()["idempotent_replay"] is True
    assert second.headers.get("Idempotent-Replay") == "true"
    # 只创建了一个 Run
    listing = client.get("/api/v1/runs")
    assert listing.json()["total"] == 1


def test_run_create_idempotent_same_key_different_body_conflict(tmp_path):
    app, _store = _sqlite_app(tmp_path)
    client = TestClient(app)
    first = client.post("/api/v1/runs", json=_create_body(request_key="key-2"))
    assert first.status_code == 202
    changed = _create_body(request_key="key-2")
    changed["case_ids"] = ["case-1", "case-2"]
    changed["manifest"]["replay_fixture"]["case-2"] = {"output": "ok", "expected": "ok"}
    conflict = client.post("/api/v1/runs", json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "REQUEST_KEY_CONFLICT"
    # 冲突不创建第二个 Run
    assert client.get("/api/v1/runs").json()["total"] == 1


def test_run_create_without_request_key_still_creates_each_time(tmp_path):
    app, _store = _sqlite_app(tmp_path)
    client = TestClient(app)
    assert client.post("/api/v1/runs", json=_create_body()).status_code == 202
    assert client.post("/api/v1/runs", json=_create_body()).status_code == 202
    assert client.get("/api/v1/runs").json()["total"] == 2


def test_run_create_idempotency_survives_registry_loss(tmp_path):
    """注册表丢失后，同 key 同 body 仍命中同一确定性 run_id（runs 主键兜底）。"""
    db_path = tmp_path / "m7-platform.db"
    store = SQLiteRunStore(db_path)
    app = create_app(store=store)
    client = TestClient(app)
    body = _create_body(request_key="key-3")
    run_id = client.post("/api/v1/runs", json=body).json()["id"]
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM motte_request_keys")
        connection.commit()
    replay = client.post("/api/v1/runs", json=body)
    assert replay.status_code == 200
    assert replay.json()["id"] == run_id


# ------------------------------------------------------------------------ SSE


def _stream(client, run_id, headers=None, params=None):
    with client.stream(
        "GET", f"/api/v1/runs/{run_id}/events", headers=headers or {}, params=params or {}
    ) as response:
        assert response.status_code == 200
        return "".join(response.iter_text())


def test_sse_invalid_last_event_id_rejected(tmp_path):
    app, store = _sqlite_app(tmp_path)
    client = TestClient(app)
    run = store.runs.create(
        {"id": "run-sse-bad", "status": "queued", "revision": 1,
         "scenario_version": "replay@1", "manifest": {}, "case_ids": []},
        event={"run_id": "run-sse-bad", "type": "queued", "status": "queued"},
    )
    response = client.get(
        f"/api/v1/runs/{run['id']}/events", headers={"Last-Event-ID": "not-a-number"}
    )
    assert response.status_code == 400


def test_sse_gap_frame_emitted_when_prefix_pruned(tmp_path):
    app, store = _sqlite_app(tmp_path)
    client = TestClient(app)
    run = store.runs.create(
        {"id": "run-gap", "status": "queued", "revision": 1,
         "scenario_version": "replay@1", "manifest": {}, "case_ids": []},
        event={"run_id": "run-gap", "type": "queued", "status": "queued"},
    )
    store.events.append({"run_id": "run-gap", "type": "model_response", "payload": {}})
    store.events.append({"run_id": "run-gap", "type": "score", "payload": {}})
    # 终态让流在发完事件后关闭（否则测试流永不结束）
    current = store.runs.get(run["id"])
    store.runs.save({**current, "status": "completed"})
    # 模拟事件清理：删除 seq=1，使 after=0 的重连面对 first_seq=2 的缺口
    with sqlite3.connect(tmp_path / "m7-platform.db") as connection:
        connection.execute("DELETE FROM trace_events WHERE run_id = 'run-gap' AND seq = 1")
        connection.commit()
    stream_text = _stream(client, run["id"])
    assert "event: motte-gap" in stream_text
    gap_line = next(
        line for line in stream_text.splitlines() if line.startswith("data: {\"type\": \"gap\"")
        or line.startswith("data: {\"type\":\"gap\"")
    )
    gap = json.loads(gap_line.removeprefix("data: "))
    assert gap == {"type": "gap", "after": 0, "next_seq": 2, "partial": True}
    # 缺口后正常续传 seq=2
    assert "id: 2" in stream_text


def test_events_snapshot_endpoint(tmp_path):
    app, store = _sqlite_app(tmp_path)
    client = TestClient(app)
    store.runs.create(
        {"id": "run-snap", "status": "queued", "revision": 1,
         "scenario_version": "replay@1", "manifest": {}, "case_ids": []},
        event={"run_id": "run-snap", "type": "queued", "status": "queued"},
    )
    store.events.append({"run_id": "run-snap", "type": "score", "payload": {"a": 1}})
    response = client.get("/api/v1/runs/run-snap/events/snapshot", params={"after": 0})
    assert response.status_code == 200
    payload = response.json()
    assert [event["seq"] for event in payload["events"]] == [1, 2]
    assert payload["last_seq"] == 2
    assert payload["run_status"] == "queued"
    assert payload["partial"] is False
    limited = client.get(
        "/api/v1/runs/run-snap/events/snapshot", params={"after": 0, "limit": 1}
    ).json()
    assert len(limited["events"]) == 1
    missing = client.get("/api/v1/runs/run-nope/events/snapshot")
    assert missing.status_code == 404


# --------------------------------------------------------------------- security


def test_security_rejects_untrusted_host(tmp_path):
    app, _store = _sqlite_app(tmp_path)
    client = TestClient(app)
    blocked = client.post(
        "/api/v1/runs", json=_create_body(), headers={"Host": "evil.example"}
    )
    assert blocked.status_code == 400
    assert blocked.json()["error"]["code"] == "HOST_REJECTED"
    allowed = client.get("/health")
    assert allowed.status_code == 200


def test_security_rejects_cross_site_origin_on_writes(tmp_path):
    app, _store = _sqlite_app(tmp_path)
    client = TestClient(app)
    blocked = client.post(
        "/api/v1/runs", json=_create_body(),
        headers={"Origin": "http://evil.example"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "ORIGIN_REJECTED"
    # 同源（loopback host）Origin 不受影响
    ok = client.post(
        "/api/v1/runs", json=_create_body(), headers={"Origin": "http://localhost:5173"}
    )
    assert ok.status_code == 202


def test_security_rejects_cross_site_origin_with_local_host_rebinding(tmp_path):
    """DNS rebinding：Origin 是恶意域，即使解析到本机也被拒。"""
    app, _store = _sqlite_app(tmp_path)
    client = TestClient(app)
    blocked = client.post(
        "/api/v1/runs", json=_create_body(),
        headers={"Host": "localhost:8000", "Origin": "http://rebind.attacker"},
    )
    assert blocked.status_code == 403


def test_security_bearer_token_enforced_with_exemptions(tmp_path):
    app, _store = _sqlite_app(tmp_path, api_token="secret-token")
    client = TestClient(app)
    anonymous = client.post("/api/v1/runs", json=_create_body())
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "AUTH_REQUIRED"
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/capabilities").status_code == 200
    authorized = client.post(
        "/api/v1/runs", json=_create_body(),
        headers={"Authorization": "Bearer secret-token"},
    )
    assert authorized.status_code == 202
    wrong = client.post(
        "/api/v1/runs", json=_create_body(), headers={"Authorization": "Bearer nope"}
    )
    assert wrong.status_code == 401


# ------------------------------------------------------------------ maintenance


def test_maintenance_mode_blocks_api_writes_and_worker_claims(tmp_path):
    app, store = _sqlite_app(tmp_path)
    client = TestClient(app)
    created = client.post("/api/v1/runs", json=_create_body())
    assert created.status_code == 202
    run_id = created.json()["id"]

    begin = client.post("/api/v1/maintenance/begin")
    assert begin.status_code == 200
    assert begin.json()["active"] is True

    blocked = client.post("/api/v1/runs", json=_create_body())
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "MAINTENANCE_MODE"
    reads_ok = client.get(f"/api/v1/runs/{run_id}")
    assert reads_ok.status_code == 200
    status = client.get("/api/v1/maintenance")
    assert status.json()["active"] is True

    # Worker 在维护窗口内不领取（哪怕 run 处于 queued）
    from motte_sdk.service import RunService
    from apps.worker.motte_worker.runtime import WorkerLoop

    service = RunService(store)
    current = store.runs.get(run_id)
    if current["status"] != "queued":
        store.runs.save({**current, "status": "queued"})

    loop = WorkerLoop(service, execution_lock_held=True, scoring_jobs=None)
    assert loop.run_once() is None
    assert store.runs.get(run_id)["status"] == "queued"

    end = client.post("/api/v1/maintenance/end")
    assert end.json()["active"] is False
    assert client.post("/api/v1/runs", json=_create_body()).status_code == 202


def test_worker_restore_guard_blocks_claims(tmp_path):
    app, store = _sqlite_app(tmp_path)
    client = TestClient(app)
    created = client.post("/api/v1/runs", json=_create_body())
    run_id = created.json()["id"]
    platform_for(store).meta.set("restored_from_backup", "manifest-x")

    from apps.worker.motte_worker.runtime import WorkerLoop
    from motte_sdk.service import RunService

    loop = WorkerLoop(RunService(store), execution_lock_held=True, scoring_jobs=None)
    assert loop.run_once() is None
    assert store.runs.get(run_id)["status"] == "queued"


# ------------------------------------------------------------ dispatcher guard


def test_dispatcher_refuses_claim_for_imported_runs(tmp_path):
    """带 import_source 的 Run 即使状态为 queued 也不被领取（协议 §5.3）。"""
    from motte_sdk.dispatcher import RunDispatcher
    from motte_sdk.service import RunService

    store = SQLiteRunStore(tmp_path / "m7-imported.db")
    store.runs.create(
        {
            "id": "run-imported-1", "status": "queued", "revision": 1,
            "scenario_version": "replay@1",
            "manifest": {
                "provider": {"fixture": {"case-1": {"output": "ok"}}},
                "execution": {"backend_id": "replay"},
                "import_source": {"system": "legacy", "record_id": "r1",
                                  "content_hash": "sha256:x", "import_id": "imp-1"},
            },
            "case_ids": ["case-1"],
        },
        event={"run_id": "run-imported-1", "type": "queued", "status": "queued"},
    )
    dispatcher = RunDispatcher(RunService(store))
    assert dispatcher.claim() is None
    assert dispatcher.claim("run-imported-1") is None
    assert store.runs.get("run-imported-1")["status"] == "queued"
