"""M4-T05：runtime session 记录与启动恢复。

先持久化启动 token/operation，再 spawn；保存进程创建身份（命令行 +
创建时间）。重启后只观察原进程或标记 needs_review——绝不自动重放可能
已经发生副作用的操作（M4-G11/A09）；PID 复用靠命令行 + 创建时间判定，
无法确认时保守拒绝。持久化是真 CAS：revision 过期即拒绝（不静默改写），
落盘经临时文件原子替换（M4 review R15）。
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .process import identity_matches, pid_alive, process_identity

SESSION_VERSION = 1
SESSION_STORE_ROOT = Path("var/runtime-sessions")


class SessionCasError(RuntimeError):
    """并发写冲突：文件里的 revision 比写入方预期的新。"""


def session_path(
    anchor: str | Path, run_id: str, case_id: str, session_id: str,
) -> Path:
    """session 记录位置（anchor/run_id/case_id/session_id.json，无路径穿越）。"""
    for label, value in (("run_id", run_id), ("case_id", case_id), ("session_id", session_id)):
        if not isinstance(value, str) or not value or "/" in value or "\\" in value \
                or value in {".", ".."}:
            raise ValueError(f"{label} must be a single safe path component: {value!r}")
    return Path(anchor) / run_id / case_id / f"{session_id}.json"


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
    """CAS 持久化（临时文件 + 原子替换；并发写按 revision 拒绝）。

    写入方必须携带 ``record["revision"] = 现有 revision + 1``；携带的
    revision 落后于文件即抛 :class:`SessionCasError`，绝不静默改写。
    """
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
        current_revision = int(existing.get("revision") or 0)
        if int(record.get("revision") or 0) != current_revision + 1:
            raise SessionCasError(
                f"session revision conflict: file at {current_revision}, "
                f"write claims {record.get('revision')}"
            )
    record.setdefault("revision", 1)
    if int(record.get("revision") or 1) < 1:
        raise ValueError("session revision must be >= 1")
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
    temporary = target.with_name(f"{target.name}.tmp-{uuid4().hex[:8]}")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, target)
    return record


def next_revision(record: dict[str, Any]) -> int:
    return int(record.get("revision") or 0) + 1


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
        "revision": next_revision(record),
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
        "revision": next_revision(record),
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


def recover_runtime_sessions(
    anchor: str | Path = SESSION_STORE_ROOT,
) -> list[dict[str, Any]]:
    """扫描 session 锚目录，对每个未终结记录给出恢复判定（只观察，不重放）。

    供 Worker/CLI 启动时调用：needs_review 的 run/case 由运维人工裁决；
    本函数不修改任何记录、不接触任何进程。
    """
    root = Path(anchor)
    if not root.is_dir():
        return []
    findings: list[dict[str, Any]] = []
    for record_path in sorted(root.glob("*/*/*.json")):
        record = load_session(record_path)
        if record is None or record.get("state") == "terminal":
            continue
        findings.append({
            "session_id": record.get("session_id"),
            "run_id": record.get("run_id"),
            "case_id": record.get("case_id"),
            "backend": record.get("backend"),
            "state": record.get("state"),
            "record_path": str(record_path),
            **recover_session(record),
        })
    return findings
