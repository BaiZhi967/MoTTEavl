"""M4-T10：RunCommand 消费者（去重/过期/绑定/恢复，合成 RPC 证据）。

主断言 test_commands_ack_expiry_and_no_repeat_approval：重复命令去重；
过期/错 session/revision 不匹配拒绝；重启后 delivered 无 ack →
delivery_unknown，不重复危险批准；干预进入 Run 证据。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from motte_sdk.commands import (
    deliver_pending,
    intervention_summary,
    recover_delivered_without_ack,
    request_hash_of,
    submit_command,
    synthetic_app_server_transport,
)
from motte_sdk.service import RunService
from motte_storage.run_store import InMemoryRunStore


def _service() -> RunService:
    return RunService(InMemoryRunStore())


def _run(service: RunService, interactive: bool = True) -> str:
    manifest = {
        "execution": {
            "backend_id": "codex-app-server", "backend_version": "1",
            "capabilities": {"interactive": interactive, "safe_to_repeat": False},
            "execution_mode": "sample",
        },
        "runtime": "codex-app-server@1",
    }
    run = service.create_run("interactive@1", manifest, ["case-1"])
    return run["id"]


def test_commands_ack_expiry_and_no_repeat_approval():
    service = _service()
    run_id = _run(service)

    # 重复 dedupe_key：幂等返回同一命令
    first = submit_command(
        service, run_id, kind="user_message", content="hi",
        session_id="sess-1", dedupe_key="msg-1",
    )
    second = submit_command(
        service, run_id, kind="user_message", content="hi",
        session_id="sess-1", dedupe_key="msg-1",
    )
    assert first["id"] == second["id"]

    # 正常投递：CAS queued→delivered→acknowledged（合成 RPC）
    results = deliver_pending(
        service, run_id, synthetic_app_server_transport,
        session_bindings={"sess-1": 3},
    )
    outcomes = {item["command_id"]: item["outcome"] for item in results}
    assert outcomes[first["id"]] == "acknowledged"
    commands = {c["id"]: c for c in service.store.commands.list_for_run(run_id)}
    assert commands[first["id"]]["status"] == "acknowledged"

    # 过期命令：提交时已过 expires_at → expired，不投递
    stale = submit_command(
        service, run_id, kind="approve", content="ok",
        session_id="sess-1", dedupe_key="approve-stale",
        payload={"tool_request_hash": request_hash_of({"tool": "shell"})},
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    results = deliver_pending(
        service, run_id, synthetic_app_server_transport,
        session_bindings={"sess-1": 3},
    )
    outcomes = {item["command_id"]: item["outcome"] for item in results}
    assert outcomes[stale["id"]] == "expired"

    # 错 session / revision 不匹配：旧批准不能授权新请求
    wrong_session = submit_command(
        service, run_id, kind="approve", content="ok",
        session_id="sess-ghost", dedupe_key="approve-ghost",
    )
    old_revision = submit_command(
        service, run_id, kind="approve", content="ok",
        session_id="sess-1", dedupe_key="approve-old",
        expected_session_revision=1,
        payload={"tool_request_hash": request_hash_of({"tool": "shell", "args": "rm -rf"})},
    )
    results = deliver_pending(
        service, run_id, synthetic_app_server_transport,
        session_bindings={"sess-1": 3},
    )
    outcomes = {item["command_id"]: item["outcome"] for item in results}
    assert outcomes[wrong_session["id"]] == "rejected_session"
    assert outcomes[old_revision["id"]] == "rejected_revision"

    # 重启恢复：delivered 无 ack → delivery_unknown，不重复投递
    pending = submit_command(
        service, run_id, kind="interrupt", content="stop",
        session_id="sess-1", dedupe_key="interrupt-1",
    )
    commands = service.store.commands
    commands.transition(
        pending["id"], expected_revision=pending["revision"],
        expected_status="queued", status="delivered",
        changes={"delivered_at": datetime.now(UTC).isoformat()},
    )
    recovered = recover_delivered_without_ack(service, run_id)
    assert pending["id"] in recovered
    # 再跑消费者：delivery_unknown 是终态，不重发
    results = deliver_pending(
        service, run_id, synthetic_app_server_transport,
        session_bindings={"sess-1": 3},
    )
    assert all(item["command_id"] != pending["id"] for item in results)

    # 干预进入 Run 证据（比较条件输入）
    summary = intervention_summary(service, run_id)
    assert summary["count"] >= 1
    assert "approve" in summary["kinds"] or "interrupt" in summary["kinds"]
    events = service.store.events.list_for_run(run_id)
    assert any(event["type"] == "command_acknowledged" for event in events)


def test_transport_failure_is_delivery_unknown_not_retry():
    service = _service()
    run_id = _run(service)
    command = submit_command(
        service, run_id, kind="user_message", content="x",
        session_id="sess-1", dedupe_key="m-1",
    )

    def broken_transport(_command):
        raise ConnectionError("app-server vanished")

    results = deliver_pending(service, run_id, broken_transport, session_bindings={"sess-1": 1})
    assert results[0]["outcome"] == "delivery_unknown"
    stored = {c["id"]: c for c in service.store.commands.list_for_run(run_id)}
    assert stored[command["id"]]["status"] == "delivery_unknown"


def test_upstream_rejection_maps_to_failed():
    service = _service()
    run_id = _run(service)
    command = submit_command(
        service, run_id, kind="user_message", content="x",
        session_id="sess-1", dedupe_key="m-2",
    )

    def rejecting_transport(_command):
        return {"acked": False, "reason": "thread closed"}

    results = deliver_pending(service, run_id, rejecting_transport, session_bindings={"sess-1": 1})
    assert results[0]["outcome"] == "upstream_rejected"
    stored = {c["id"]: c for c in service.store.commands.list_for_run(run_id)}
    assert stored[command["id"]]["status"] == "failed"


def test_app_server_transport_version_gate():
    from motte_harness.codex_app_server import CodexAppServerTransport
    from motte_harness.compatibility import CompatibilityError

    with pytest.raises(CompatibilityError) as raised:
        CodexAppServerTransport(version_output="codex-cli 0.148.0 (drifted)")
    assert raised.value.code == "CODEX_APP_SERVER_VERSION_DRIFT"


def test_non_interactive_run_messages_rejected_at_api():
    from fastapi.testclient import TestClient

    from apps.api.app.main import create_app

    application = create_app(store=InMemoryRunStore())
    client = TestClient(application)
    service = application.state.run_service
    run_id = _run(service, interactive=False)
    response = client.post(f"/api/v1/runs/{run_id}/messages", json={"content": "hi"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMAND_UNSUPPORTED"

    run_id2 = _run(service, interactive=True)
    response = client.post(
        f"/api/v1/runs/{run_id2}/messages",
        json={"content": "hi", "kind": "user_message", "session_id": "s1",
               "dedupe_key": "api-1"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    listed = client.get(f"/api/v1/runs/{run_id2}/commands").json()
    assert any(item["id"] == body["command_id"] for item in listed["items"])
