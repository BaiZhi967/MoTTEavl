"""M4-T05：runtime session 记录与启动恢复。

先持久化启动 token/operation，再 spawn；保存进程创建身份（命令行 +
创建时间）。重启后只观察原进程或标记 needs_review——绝不自动重放可能
已经发生副作用的操作（M4-G11/A09）；PID 复用靠命令行一致性判定，无法
确认时保守拒绝。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .process import identity_matches, pid_alive, process_identity

SESSION_VERSION = 1


def new_session_record(
    *,
    run_id: str,
    case_id: str,
    attempt_id: str,
    operation_id: str | None = None,
    backend: str,
    argv: list[str],
    workspace: str | None = None,
    config_hash: str | None = None,
) -> dict[str, Any]:
    """spawn 前构造的启动记录（start_token 先落盘再启动）。"""
    return {
        "schema_version": SESSION_VERSION,
        "session_id": f"sess-{uuid4().hex[:16]}",
        "run_id": run_id,
        "case_id": case_id,
        "attempt_id": attempt_id,
        "operation_id": operation_id or f"op-{uuid4().hex[:16]}",
        "backend": backend,
        "start_token": f"start-{uuid4().hex}",
        "argv": [str(part) for part in argv],
        "workspace": workspace,
        "config_hash": config_hash,
        "state": "prepared",
        "pid": None,
        "identity": None,
        "created_at": datetime.now(UTC).isoformat(),
        "spawned_at": None,
        "terminal_at": None,
        "terminal_status": None,
        "cleanup": None,
    }


def persist_session(path: str | Path, record: dict[str, Any]) -> dict[str, Any]:
    """CAS 持久化（整文件原子替换；并发写通过 revision 拒绝）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] | None = None
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
    if existing is not None:
        if existing.get("session_id") != record.get("session_id"):
            raise ValueError("session file belongs to another session")
        if int(existing.get("revision") or 0) + 1 != int(record.get("revision") or 1):
            record = {**record, "revision": int(existing.get("revision") or 0) + 1}
    record.setdefault("revision", 1)
    target.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )
    return record


def load_session(path: str | Path) -> dict[str, Any] | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if record.get("schema_version") != SESSION_VERSION:
        return None
    return record


def mark_spawned(
    record: dict[str, Any], pid: int, *, path: str | Path,
) -> dict[str, Any]:
    """spawn 成功后立刻记录进程身份（PID 复用防护基线）。"""
    updated = {
        **record,
        "state": "spawned",
        "pid": int(pid),
        "identity": process_identity(int(pid)),
        "spawned_at": datetime.now(UTC).isoformat(),
    }
    return persist_session(path, updated)


def mark_terminal(
    record: dict[str, Any], status: str, *, path: str | Path,
    cleanup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updated = {
        **record,
        "state": "terminal",
        "terminal_status": status,
        "terminal_at": datetime.now(UTC).isoformat(),
        "cleanup": cleanup,
    }
    return persist_session(path, updated)


def recover_session(record: dict[str, Any]) -> dict[str, Any]:
    """Worker 重启后的恢复判定：只观察，不重放（M4-A09）。

    - 进程仍活着且身份匹配 → observed（外进程继续，平台只观察）；
    - 进程不在 → 原操作结果不可证 → needs_review；
    - PID 被复用（身份不匹配）→ needs_review，且绝不杀该 PID。
    """
    if record.get("state") == "terminal":
        return {
            "action": "already_terminal",
            "status": record.get("terminal_status"),
            "replayed": False,
        }
    pid = record.get("pid")
    if pid is None:
        return {
            "action": "never_spawned",
            "status": "prepared",
            "replayed": False,
        }
    if not pid_alive(int(pid)):
        return {
            "action": "needs_review",
            "status": "process_gone",
            "reason": "original process is gone; outcome cannot be proven",
            "replayed": False,
        }
    if not identity_matches(record.get("identity") or {}, int(pid)):
        return {
            "action": "needs_review",
            "status": "pid_reused",
            "reason": "pid now belongs to a different process; refusing to touch it",
            "replayed": False,
        }
    return {
        "action": "observe",
        "status": "process_alive",
        "pid": int(pid),
        "replayed": False,
    }
