"""M4-T04：Pi runtime 的平台纵向执行器（pi-agent@1 sample backend）。

每个 CaseAttempt 独立 session/workspace：bridge 进程按 Case 生命周期创建与
销毁，session_id 每次全新（显式续跑才允许绑定旧 session——当前版本不提供
续跑，续跑诉求走 retry 子 Run）。事件经 RunService 持久 trace；模型/工具
调用经 InvocationRecord；产物经 ArtifactStore；Observation 沿用 M1 冻结
契约（usage 对 scripted model 诚实保持未上报）。
"""
from __future__ import annotations

import hashlib
import os
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from motte_contracts.evaluation import (
    ArtifactEntry,
    EvidenceRef,
    observation_evidence_hash,
)
from motte_trace.redaction import redact_secrets as redact

PI_BACKEND_ID = "pi-agent"
PI_BACKEND_VERSION = "1"
PI_PARSER_VERSION = "pi-jsonl-v2"
PI_TOOLS = ("read_file", "write_file", "list_files")

# bridge 终态 → FrozenObservation.termination.reason 契约词表
_TERMINATION_REASONS = {
    "completed": "final_answer",
    "cancelled": "cancelled",
    "error": "error",
    "timeout": "wall_time",
}


def _workspace_anchor() -> Path:
    return Path(os.environ.get("MOTTE_PI_WORKSPACE_ROOT", "var/pi-workspaces"))


def _safe_component(value: str, label: str) -> str:
    if (
        not isinstance(value, str) or not value
        or value in {".", ".."} or "/" in value or "\\" in value
        or value.startswith(".") or len(value) > 128
    ):
        raise ValueError(f"{label} must be a single safe path component: {value!r}")
    return value


class RuntimeCaseWorkspace:
    """Pi Case 工作区（Python 侧快照/清理）。

    执行期边界在 bridge 侧（tools.mjs：相对路径 only、symlink 拒绝、配额）。
    Python 侧负责 fixture 物化与证据快照：物化/快照路径做 lstat symlink
    检查与配额，不做 fd 链竞态防护（采集是运行后的单线程快照；POSIX fd 链
    沙箱见 motte_sandbox，Windows 限制已在验证记录单列）。
    """

    def __init__(self, run_id: str, case_id: str) -> None:
        self.anchor = _workspace_anchor()
        self.root = self.anchor / _safe_component(run_id, "run_id") \
            / _safe_component(case_id, "case_id")
        self.root.mkdir(parents=True, exist_ok=True)
        self._created = True

    def materialize_fixture(self, fixture: dict[str, str]) -> None:
        from motte_sandbox.workspace import WorkspacePolicyError, validate_relative_path

        for raw_path, content in (fixture or {}).items():
            try:
                path = validate_relative_path(raw_path)
            except WorkspacePolicyError as error:
                raise ValueError(f"fixture path rejected: {error}") from error
            target = self.root / Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                raise ValueError(f"fixture path already occupied: {path}")
            target.write_text(content, encoding="utf-8")

    @staticmethod
    def _walk_files(root: Path) -> list[str]:
        found: list[str] = []
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirnames[:] = [d for d in dirnames if not (current_path / d).is_symlink()]
            for name in filenames:
                full = current_path / name
                if full.is_symlink():
                    continue
                found.append(full.relative_to(root).as_posix())
        return sorted(found)

    def snapshot(self) -> dict[str, Any]:
        hashes: dict[str, str] = {}
        complete = True
        for rel in self._walk_files(self.root):
            data = (self.root / rel).read_bytes()
            hashes[rel] = hashlib.sha256(data).hexdigest()
        return {"files": sorted(hashes), "hashes": hashes, "complete": complete}

    def cleanup(self) -> dict[str, Any]:
        import shutil as _shutil

        if not self.root.exists():
            return {"status": "noop", "residual": []}
        if self.root.is_symlink() or not self.root.is_dir():
            return {"status": "failed", "residual": [str(self.root)],
                    "error": "workspace root is not a controlled directory"}
        try:
            resolved = self.root.resolve()
            anchor_resolved = self.anchor.resolve()
            if anchor_resolved not in resolved.parents:
                return {"status": "failed", "residual": [str(self.root)],
                        "error": "workspace root escaped the anchor"}
            _shutil.rmtree(self.root)
            return {"status": "ok", "residual": []}
        except OSError as error:
            return {"status": "failed", "residual": [str(self.root)], "error": str(error)}


def _artifact_store():
    from motte_storage.artifacts import ArtifactStore

    return ArtifactStore(Path(os.environ.get("ARTIFACT_ROOT", "var/artifacts")))


class PiRuntimeCaseExecutor:
    """一个 Case 的 Pi 执行器：bridge session、事件、调用日志与 Observation。"""

    def __init__(
        self,
        run: dict[str, Any],
        *,
        service: Any = None,
        bridge_path: str | Path | None = None,
        node_binary: str | None = None,
    ) -> None:
        self.run = run
        self.manifest = run.get("manifest") or {}
        self.profile = self.manifest.get("runtime_profile") or {}
        settings = self.profile.get("native_settings") or {}
        self.script = settings.get("script")
        self.model_id = settings.get("model") or "scripted-1"
        self.system_prompt = settings.get("system_prompt") or ""
        self.max_steps = int(settings.get("max_steps") or 16)
        self._service = service
        self._bridge_path = bridge_path
        self._node = node_binary

    def bind_service(self, service: Any) -> None:
        self._service = service

    # ------------------------------------------------------------ 公共入口

    def invoke(self, case_id: str) -> dict[str, Any]:
        from motte_sdk.agent_tasks import selected_agent_cases_from_snapshot

        if not isinstance(self.script, list) or not self.script:
            raise ValueError(
                "pi runtime requires runtime_profile.native_settings.script "
                "(scripted model steps) for offline execution"
            )
        cases = selected_agent_cases_from_snapshot(self.run)
        case = next((item for item in cases if item["case_id"] == case_id), None)
        if case is None:
            raise KeyError(case_id)

        from motte_agent.pi import PiBridgeSession

        workspace = RuntimeCaseWorkspace(self.run["id"], case_id)
        workspace.materialize_fixture(case.get("fixture") or {})
        before = workspace.snapshot()

        session_id = f"pi-session-{uuid4().hex[:16]}"
        operation_id = f"pi-op-{uuid4().hex[:16]}"
        event_refs: list[EvidenceRef] = []
        tool_records: list[dict[str, Any]] = []
        outputs: list[str] = []

        def on_event(event: dict[str, Any]) -> None:
            kind = event.get("type")
            if kind in ("session_event", "output", "tool_call", "tool_result",
                        "interrupted", "finished"):
                if kind == "output":
                    outputs.append(event.get("text") or "")
                if kind == "tool_call":
                    tool_records.append({
                        "call_id": event.get("call_id"),
                        "tool_name": event.get("name"),
                        "arguments": deepcopy(event.get("arguments") or {}),
                        "status": "dispatched",
                    })
                if kind == "tool_result":
                    for record in tool_records:
                        if record["call_id"] == event.get("call_id"):
                            record["status"] = "failed" if event.get("is_error") else "succeeded"
                service = self._service
                if service is not None:
                    payload = redact(deepcopy({
                        key: value for key, value in event.items()
                        if key not in ("run_id", "case_id", "session_id", "operation_id", "type")
                    }))
                    stored = service.emit_run_event(
                        self.run["id"], f"pi_{kind}",
                        {"case_id": case_id, "session_id": session_id, **payload},
                    )
                    if stored is not None and isinstance(stored.get("seq"), int):
                        event_refs.append(EvidenceRef(
                            kind="event", run_id=self.run["id"], locator=str(stored["seq"]),
                        ))

        session = PiBridgeSession(
            run_id=self.run["id"],
            case_id=case_id,
            session_id=session_id,
            operation_id=operation_id,
            workspace=workspace.root,
            model_config={"id": self.model_id, "name": f"scripted:{self.model_id}"},
            responses=self.script,
            tools=list(PI_TOOLS),
            system_prompt=self.system_prompt,
            budgets={"max_steps": self.max_steps},
            bridge_path=self._bridge_path,
            node_binary=self._node,
            idle_timeout=float(os.environ.get("MOTTE_PI_IDLE_TIMEOUT", "120")),
            total_timeout=float(os.environ.get("MOTTE_PI_TOTAL_TIMEOUT", "900")),
            event_callback=on_event,
        )

        stop_watch = threading.Event()

        def watch_cancellation() -> None:
            service = self._service
            if service is None:
                return
            while not stop_watch.wait(0.2):
                try:
                    current = service.store.runs.get(self.run["id"])
                except Exception:  # noqa: BLE001 - 探测失败按未取消处理
                    continue
                if current and current.get("cancellation"):
                    session.interrupt()
                    return

        watcher = threading.Thread(target=watch_cancellation, daemon=True, name="pi-cancel-watch")
        started = monotonic()
        outcome: dict[str, Any]
        cleanup: dict[str, Any] | None = None
        capture_errors: list[str] = []
        pending_error: BaseException | None = None
        try:
            session.start()
            watcher.start()
            try:
                outcome = session.run(case["input"])
            finally:
                stop_watch.set()
                session.close()
            self._log_invocations(case_id, session_id, outcome)
            observation = self._capture(workspace, case, outcome, before, event_refs, tool_records)
        except BaseException as error:  # noqa: BLE001 - 异常路径也要采集与清理
            pending_error = error
            observation = {}
            outcome = {
                "status": "error", "final_output": None, "steps": 0, "tool_calls": 0,
                "usage": {"reported": False, "source": "scripted-model"},
            }
        finally:
            stop_watch.set()
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                cleanup = workspace.cleanup()
            except BaseException as error:  # noqa: BLE001
                cleanup = {"status": "failed", "residual": [], "error": str(error)}

        duration_ms = round((monotonic() - started) * 1000, 3)
        envelope = {
            "agent": {
                "final_output": outcome.get("final_output"),
                "termination_reason": (
                    "cancelled" if outcome.get("interrupted")
                    else outcome.get("status") or "error"
                ),
                "termination_detail": outcome.get("failure"),
                "steps": outcome.get("steps", 0),
                "tool_calls": outcome.get("tool_calls", 0),
                "duration_ms": duration_ms,
                "runtime": {
                    "backend": f"{PI_BACKEND_ID}@{PI_BACKEND_VERSION}",
                    "session_id": session_id,
                    "operation_id": operation_id,
                    "parser_version": PI_PARSER_VERSION,
                    "model_control": "runner-configured",
                    "usage_source": (outcome.get("usage") or {}).get("source"),
                    "config_hash": self.profile.get("config_hash"),
                },
            },
            "observation": observation,
            "events": [],
            "artifacts_captured": len(observation.get("artifact_refs") or []),
            "capture_errors": capture_errors,
            "cleanup": cleanup,
        }
        if pending_error is not None:
            if not getattr(pending_error, "quarantine", False):
                pending_error.evidence = {**envelope, "error": {
                    "class": type(pending_error).__name__,
                    "message": str(pending_error),
                }}
            raise pending_error
        return envelope

    # ------------------------------------------------------------ 调用日志

    def _log_invocations(
        self, case_id: str, session_id: str, outcome: dict[str, Any],
    ) -> None:
        store = getattr(self._service, "store", None) if self._service else None
        invocations = getattr(store, "invocations", None) if store else None
        if invocations is None:
            return
        try:
            model_invocation = invocations.create({
                "id": f"inv-{uuid4().hex}",
                "run_id": self.run["id"],
                "case_id": case_id,
                "kind": "model",
                "step": 1,
                "status": "settled",
                "tool_name": None,
                "model": self.model_id,
                "request_summary": redact({
                    "runtime": f"{PI_BACKEND_ID}@{PI_BACKEND_VERSION}",
                    "session_id": session_id,
                    "model_control": "runner-configured",
                    "metering_source": (outcome.get("usage") or {}).get("source"),
                }),
                "prepared_at": datetime.now(UTC).isoformat(),
                "dispatched_at": datetime.now(UTC).isoformat(),
                "outcome": "succeeded" if outcome.get("status") == "completed" else "failed",
                "result_summary": {
                    "usage_reported": (outcome.get("usage") or {}).get("reported", False),
                    "steps": outcome.get("steps", 0),
                },
                "settled_at": datetime.now(UTC).isoformat(),
            })
            del model_invocation
        except Exception:  # noqa: BLE001 - 调用日志失败不吞执行结果
            pass

    # ------------------------------------------------------------ Observation

    def _capture(
        self,
        workspace,  # noqa: ANN001
        case: dict[str, Any],
        outcome: dict[str, Any],
        before: dict[str, Any],
        event_refs: list[EvidenceRef],
        tool_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from motte_sdk.agent_backend import _guess_media_type, _read_nofollow

        after = workspace.snapshot()
        artifacts: list[dict[str, Any]] = []
        errors: list[str] = []
        artifact_store = _artifact_store()
        before_files = set(before.get("files") or [])
        after_files = set(after.get("files") or [])
        for rel in sorted(after_files):
            artifact_id = f"pi/{self.run['id']}/{case['case_id']}/{rel}"
            try:
                data = _read_nofollow(workspace.root / rel)
                artifact_store.put_bytes(
                    artifact_id, data, kind="case-artifact",
                    media_type=_guess_media_type(rel),
                )
                artifacts.append({
                    "artifact_id": artifact_id, "path": rel,
                    "media_type": _guess_media_type(rel),
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "available": True, "truncated": False,
                })
            except Exception as error:  # noqa: BLE001 - 采集失败降 coverage
                errors.append(f"{rel}: {type(error).__name__}: {error}")
                artifacts.append({
                    "artifact_id": artifact_id, "path": rel, "available": False,
                })
        usage = outcome.get("usage") or {}
        observation: dict[str, Any] = {
            "observation_id": f"obs-{uuid4().hex}",
            "run_id": self.run["id"],
            "case_id": case["case_id"],
            "attempt_id": None,
            "final_output": outcome.get("final_output"),
            "termination": {
                "reason": _TERMINATION_REASONS.get(
                    "cancelled" if outcome.get("interrupted") else outcome.get("status"),
                    "error",
                ),
                "detail": outcome.get("failure"),
            },
            "event_refs": [ref.model_dump() for ref in event_refs],
            "artifact_refs": artifacts,
            "coverage": {
                "complete": bool(before.get("complete", True)) and bool(after.get("complete", True)) and not errors,
                "events_captured": len(event_refs),
                "artifacts_captured": len([a for a in artifacts if a.get("available")]),
                "artifacts_expected": len(after_files),
                "missing": errors,
            },
            "usage": {
                "reported": bool(usage.get("reported")),
                "total_tokens": usage.get("total_tokens"),
                "cost_total": usage.get("observed_cost") or None,
            },
            "tool_calls": [
                {
                    "call_id": str(record.get("call_id") or f"legacy-{index}"),
                    "tool_name": str(record.get("tool_name") or "unknown"),
                    "arguments": redact(deepcopy(record.get("arguments") or {})),
                    "status": record.get("status", "failed"),
                    "step": 1,
                }
                for index, record in enumerate(tool_records)
            ],
            "workspace": {
                "before": sorted(before_files),
                "after": sorted(after_files),
                "complete": bool(before.get("complete", True) and after.get("complete", True)),
                "before_hashes": dict(before.get("hashes") or {}),
                "after_hashes": dict(after.get("hashes") or {}),
            },
            "processes": [],
        }
        observation["evidence_hash"] = observation_evidence_hash(observation)
        observation["recorded_at"] = datetime.now(UTC).isoformat()
        return observation
