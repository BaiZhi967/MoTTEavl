"""M4-T10：RunCommand 消费者（HTTP 202 ≠ 送达；真实投递与上游 ack）。

- 提交：dedupe_key 去重（同键返回既有命令）；批准绑定 request_hash、
  session、expected_session_revision 与过期时间。
- 消费（Worker 侧）：CAS queued→delivered→acknowledged；错 session/
  revision/request_hash → rejected；过期 → expired；投递结果不可证
  （断连/重启）→ delivery_unknown，绝不重复投递危险批准。
- 人工干预（消息/批准/拒绝/中断）标记 intervention 并写入 Run 事件，
  进入比较条件（M4-G18）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4

from motte_contracts.run import RunCommandStatus

INTERVENTION_TYPES = frozenset({
    "user_message", "approve", "reject", "interrupt", "file_edit",
})


class CommandError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def request_hash_of(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def submit_command(
    service: Any,
    run_id: str,
    *,
    kind: str = "user_message",
    content: str | None = None,
    payload: dict[str, Any] | None = None,
    case_id: str | None = None,
    session_id: str | None = None,
    dedupe_key: str | None = None,
    expires_at: datetime | None = None,
    expected_session_revision: int | None = None,
    actor: str = "operator",
) -> dict[str, Any]:
    """持久提交（HTTP 202 语义）：只落 queued，不承诺送达。"""
    commands = service.store.commands
    if dedupe_key:
        for existing in commands.list_for_run(run_id):
            if existing.get("dedupe_key") == dedupe_key:
                return existing
    record = {
        "id": f"cmd-{uuid4().hex}",
        "run_id": run_id,
        "type": kind,
        "status": "queued",
        "content": content,
        "payload": dict(payload or {}),
        "case_id": case_id,
        "session_id": session_id,
        "dedupe_key": dedupe_key,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "expected_session_revision": expected_session_revision,
        "request_hash": request_hash_of(payload or {}),
        "intervention": kind in INTERVENTION_TYPES,
        "created_at": datetime.now(UTC).isoformat(),
        "actor": actor,
    }
    created = commands.create(record)
    service.emit_run_event(run_id, "command_submitted", {
        "command_id": created["id"], "kind": kind,
        "dedupe_key": dedupe_key, "intervention": created.get("intervention", False),
    })
    return created


def _transition(commands: Any, command: dict[str, Any], status: str, changes: dict[str, Any]) -> dict[str, Any]:
    return commands.transition(
        command["id"], expected_revision=command["revision"],
        expected_status=command["status"], status=status, changes=changes,
    )


def _expired(command: dict[str, Any], now: datetime) -> bool:
    expires_at = command.get("expires_at")
    if not expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return now > deadline


def deliver_pending(
    service: Any,
    run_id: str,
    transport: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    session_bindings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """消费 queued 命令：真实投递（transport 回调）→ 上游 ack。

    transport(command) 返回 {"acked": bool, "reason": str?}；抛异常视为
    投递结果不可证（delivery_unknown，例如 Worker 重启窗口）。
    session_bindings: {"session_id": revision} —— 当前活跃会话与 revision。
    """
    commands = service.store.commands
    bindings = session_bindings or {}
    results: list[dict[str, Any]] = []
    for command in commands.list_for_run(run_id):
        if command.get("status") != "queued":
            continue
        now = datetime.now(UTC)
        if _expired(command, now):
            updated = _transition(commands, command, "expired", {
                "expired_at": now.isoformat(),
            })
            results.append({"command_id": command["id"], "outcome": "expired"})
            service.emit_run_event(run_id, "command_expired", {
                "command_id": command["id"],
            })
            continue
        session_id = command.get("session_id")
        if session_id is not None and session_id not in bindings:
            updated = _transition(commands, command, "rejected", {
                "rejected_at": now.isoformat(),
                "error": {"class": "command", "message": "session is not active"},
            })
            del updated
            results.append({"command_id": command["id"], "outcome": "rejected_session"})
            service.emit_run_event(run_id, "command_rejected", {
                "command_id": command["id"], "reason": "session_not_active",
            })
            continue
        expected_revision = command.get("expected_session_revision")
        if (
            session_id is not None and expected_revision is not None
            and int(bindings.get(session_id, 0)) != int(expected_revision)
        ):
            _transition(commands, command, "rejected", {
                "rejected_at": now.isoformat(),
                "error": {"class": "command", "message": "session revision mismatch"},
            })
            results.append({"command_id": command["id"], "outcome": "rejected_revision"})
            service.emit_run_event(run_id, "command_rejected", {
                "command_id": command["id"], "reason": "session_revision_mismatch",
            })
            continue
        delivered = _transition(commands, command, "delivered", {
            "delivered_at": now.isoformat(),
        })
        try:
            ack = transport(delivered)
        except Exception as error:  # noqa: BLE001 - 投递不可证：不重试危险操作
            _transition(commands, delivered, "delivery_unknown", {
                "failed_at": datetime.now(UTC).isoformat(),
                "error": {"class": "delivery", "message": str(error)[:512]},
            })
            results.append({"command_id": command["id"], "outcome": "delivery_unknown"})
            service.emit_run_event(run_id, "command_delivery_unknown", {
                "command_id": command["id"],
            })
            continue
        if ack.get("acked"):
            _transition(commands, delivered, "acknowledged", {
                "acknowledged_at": datetime.now(UTC).isoformat(),
                "payload": {**delivered.get("payload", {}), "upstream_ack": True},
            })
            results.append({"command_id": command["id"], "outcome": "acknowledged"})
            service.emit_run_event(run_id, "command_acknowledged", {
                "command_id": command["id"], "kind": delivered.get("type"),
                "intervention": delivered.get("intervention", False),
            })
        else:
            _transition(commands, delivered, "failed", {
                "failed_at": datetime.now(UTC).isoformat(),
                "error": {"class": "upstream", "message": str(ack.get("reason") or "upstream rejected")[:512]},
            })
            results.append({"command_id": command["id"], "outcome": "upstream_rejected"})
            service.emit_run_event(run_id, "command_failed", {
                "command_id": command["id"], "reason": str(ack.get("reason") or "")[:256],
            })
    return results


def recover_delivered_without_ack(service: Any, run_id: str) -> list[str]:
    """重启恢复：delivered 但无 ack → delivery_unknown（不重复投递）。"""
    commands = service.store.commands
    recovered: list[str] = []
    for command in commands.list_for_run(run_id):
        if command.get("status") == "delivered":
            _transition(commands, command, "delivery_unknown", {
                "failed_at": datetime.now(UTC).isoformat(),
                "error": {"class": "delivery", "message": "worker restarted before ack"},
            })
            recovered.append(command["id"])
            service.emit_run_event(run_id, "command_delivery_unknown", {
                "command_id": command["id"], "reason": "worker_restart",
            })
    return recovered


def intervention_summary(service: Any, run_id: str) -> dict[str, Any]:
    """人工干预进入比较条件（M4-G18）：Run 证据里可查询的干预清单。"""
    commands = service.store.commands
    interventions = [
        command for command in commands.list_for_run(run_id)
        if command.get("intervention")
    ]
    return {
        "count": len(interventions),
        "kinds": sorted({str(command.get("type")) for command in interventions}),
        "command_ids": [command["id"] for command in interventions],
    }


def synthetic_app_server_transport(command: dict[str, Any]) -> dict[str, Any]:
    """合成 app-server RPC（M4-T10 离线证据）：按 fixture 协议子集 ack。

    真实 codex app-server 会话（initialize/thread/turn + codex/event）由
    `motte_harness.codex_app_server` 的 spawn 传输承载；合成传输只用于
    离线消费语义验证，证据级别单独标注。
    """
    kind = command.get("type")
    if kind == "interrupt":
        return {"acked": True, "reason": "thread/stopped accepted"}
    if kind in ("user_message", "approve", "reject"):
        return {"acked": True, "reason": f"turn/create accepted ({kind})"}
    return {"acked": False, "reason": f"unsupported command kind: {kind}"}


__all__ = [
    "CommandError",
    "RunCommandStatus",
    "INTERVENTION_TYPES",
    "deliver_pending",
    "intervention_summary",
    "recover_delivered_without_ack",
    "request_hash_of",
    "submit_command",
    "synthetic_app_server_transport",
]
