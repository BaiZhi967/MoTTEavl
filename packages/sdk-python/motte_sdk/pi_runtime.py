"""M4-T04：Pi runtime 的平台纵向执行器（pi-agent@1 sample backend）。

每个 CaseAttempt 独立 session/workspace：bridge 进程按 Case 生命周期创建与
销毁，session_id 每次全新（显式续跑才允许绑定旧 session——当前版本不提供
续跑，续跑诉求走 retry 子 Run）。事件经 RunService 持久 trace（带
source_seq/parser_version 对账）；模型/工具调用经 InvocationRecord；产物经
ArtifactStore；Observation 沿用 M1 冻结契约（usage 只在真实传输原生回报
时上报，scripted 保持未上报）。

M4 review 落实（R01/R07/R09/R14/R18）：

- 模型传输显式二分：``native_settings.script`` → scripted（离线）；
  ``native_settings.provider`` → http（真实模型，key 经环境名下发）。
- 预检具名拒绝：profile 声明了平台无法兑现的约束（credential_refs、
  空 tools 交集、pinned workspace 不可用）在 spawn 前失败，不静默忽略。
- 预算是有效配置：max_steps / max_tool_calls / total_timeout 编译进
  bridge session；预算终止进入 Observation.termination（不冒充 final）。
- 启动前版本门：bridge 报告的 SDK 版本与快照 pinned upstream 不符即
  fail closed（RUNTIME_VERSION_DRIFT）。
- 异常路径也尽量采集证据：断连/协议失败保留 workspace 快照与产物。
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
# 预算终止是独立词表：预算耗尽不冒充 final_answer，也不是取消
_BUDGET_TERMINATION_REASONS = {
    "max_steps": "max_steps",
    "max_tool_calls": "max_tool_calls",
}

# 真实模型 provider 配置允许的字段（名称，不是秘密原文）
_PROVIDER_FIELDS = ("api", "provider", "base_url", "api_key_env")
_PROVIDER_APIS = (
    "openai-completions", "openai-responses", "anthropic-messages",
    "google-generative-ai", "mistral-conversations",
)


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

    def __init__(self, run_id: str, case_id: str, *, anchor: str | Path | None = None) -> None:
        self.anchor = Path(anchor) if anchor is not None else _workspace_anchor()
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
        self.snapshot = self.manifest.get("runtime_snapshot") or {}
        settings = self.profile.get("native_settings") or {}
        self.settings = settings
        self.script = settings.get("script")
        provider = settings.get("provider")
        self.provider_config = provider if isinstance(provider, dict) else None
        self.model_id = settings.get("model") or "scripted-1"
        self.system_prompt = settings.get("system_prompt") or ""
        self._service = service
        self._attempt_id: str | None = None
        self._bridge_path = bridge_path
        self._node = node_binary

    def bind_service(self, service: Any) -> None:
        self._service = service

    def run_build_gate(self) -> None:
        """dispatcher build 阶段的公共预检（每 Run 一次，具名失败）。"""
        self._preflight()

    # ------------------------------------------------------------ 预检（R07/R18）

    def _preflight(self) -> dict[str, Any]:
        """把声明编译为有效配置；无法兑现的约束具名拒绝（不静默忽略）。"""
        from .execution_backends import ExecutionBackendError

        from motte_contracts.runtime import validate_runtime_budgets

        try:
            budgets = validate_runtime_budgets("pi-agent@1", self.profile.get("budgets") or {})
            if "max_steps" not in budgets and "max_steps" in self.settings:
                budgets.update(validate_runtime_budgets(
                    "pi-agent@1", {"max_steps": self.settings["max_steps"]},
                ))
        except ValueError as error:
            raise ExecutionBackendError("RUNTIME_BUDGET_INVALID", str(error)) from error

        if self.profile.get("credential_refs"):
            # Pi bridge 当前没有向子进程注入凭据的受控通道；声明了凭据
            # 却不落实等于静默降级认证——具名拒绝（M4 review R07/A13）。
            raise ExecutionBackendError(
                "RUNTIME_CREDENTIALS_UNRESOLVED",
                "pi runtime has no credential channel to the bridge process; "
                "credential_refs cannot be honored and are rejected at preflight",
            )

        declared = self.snapshot.get("tools")
        declared_tools = declared if isinstance(declared, list) else list(PI_TOOLS)
        allowed_tools = [tool for tool in PI_TOOLS if tool in declared_tools]
        if not allowed_tools:
            raise ExecutionBackendError(
                "RUNTIME_TOOLS_EMPTY",
                "runtime snapshot declares no tools intersecting the pi bridge sandbox",
            )

        if self.provider_config is not None:
            if self.script is not None:
                raise ExecutionBackendError(
                    "RUNTIME_MODEL_CONFIG_REQUIRED",
                    "native_settings cannot carry both script (scripted transport) "
                    "and provider (http transport)",
                )
            unknown = sorted(set(self.provider_config) - set(_PROVIDER_FIELDS))
            if unknown:
                raise ExecutionBackendError(
                    "RUNTIME_MODEL_CONFIG_REQUIRED",
                    f"provider config has unknown fields: {unknown}",
                )
            if self.provider_config.get("api") not in _PROVIDER_APIS:
                raise ExecutionBackendError(
                    "RUNTIME_MODEL_CONFIG_REQUIRED",
                    f"provider api must be one of {sorted(_PROVIDER_APIS)}",
                )
        elif not isinstance(self.script, list) or not self.script:
            raise ExecutionBackendError(
                "RUNTIME_MODEL_CONFIG_REQUIRED",
                "pi runtime requires runtime_profile.native_settings.script "
                "(scripted steps) or provider (http transport)",
            )

        try:
            total_timeout = float(
                budgets.get("total_timeout")
                or os.environ.get("MOTTE_PI_TOTAL_TIMEOUT")
                or "900"
            )
            validate_runtime_budgets("pi-agent@1", {"total_timeout": total_timeout})
            idle_timeout = float(os.environ.get("MOTTE_PI_IDLE_TIMEOUT", "120"))
            validate_runtime_budgets("pi-agent@1", {"total_timeout": idle_timeout})
        except (TypeError, ValueError) as error:
            raise ExecutionBackendError(
                "RUNTIME_BUDGET_INVALID", "budgets.total_timeout must be a number"
            ) from error

        pinned = self.snapshot.get("upstream_version") or ""
        # upstream_version 形如 "@mariozechner/pi-agent-core@0.73.1"
        expected_sdk = pinned.rpartition("@")[2] if "@" in pinned else ""

        return {
            "tools": allowed_tools,
            "max_steps": int(
                budgets.get("max_steps")
                or self.settings.get("max_steps")
                or 16
            ),
            "max_tool_calls": (
                int(budgets["max_tool_calls"])
                if budgets.get("max_tool_calls") is not None else None
            ),
            "total_timeout": total_timeout,
            "idle_timeout": idle_timeout,
            "workspace": dict(self.profile.get("workspace") or {}),
            "expected_sdk": expected_sdk,
        }

    def _workspace_for(self, case_id: str, workspace_decl: dict[str, Any]):
        if workspace_decl.get("source") == "pinned-path":
            root = workspace_decl.get("root")
            if not isinstance(root, str) or not root.strip():
                raise ValueError("pinned-path workspace requires a non-empty root")
            anchor = Path(root)
            try:
                anchor.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise ValueError(f"pinned workspace root is not usable: {error}") from error
            return RuntimeCaseWorkspace(self.run["id"], case_id, anchor=anchor)
        return RuntimeCaseWorkspace(self.run["id"], case_id)

    # ------------------------------------------------------------ 公共入口

    def invoke(self, case_id: str) -> dict[str, Any]:
        from motte_sdk.agent_tasks import selected_agent_cases_from_snapshot

        effective = self._preflight()

        cases = selected_agent_cases_from_snapshot(self.run)
        case = next((item for item in cases if item["case_id"] == case_id), None)
        if case is None:
            raise KeyError(case_id)

        if self._service is not None:
            from .execution_backends import ExecutionBackendError

            attempts = [item for item in self._service.store.attempts.list_open(self.run['id'])
                        if item.get('case_id') == case_id and item.get('status') == 'dispatching']
            if len(attempts) != 1:
                raise ExecutionBackendError('RUNTIME_ATTEMPT_UNRESOLVED', 'expected one dispatched CaseAttempt')
            self._attempt_id = attempts[0]['id']

        from motte_agent.pi import PiBridgeError, PiBridgeSession

        workspace = self._workspace_for(case_id, effective["workspace"])
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
                        {
                            "case_id": case_id, "session_id": session_id,
                            "operation_id": operation_id,
                            # 事件对账字段：bridge 原生 seq + parser 版本（R14）。
                            "source_seq": event.get("seq"),
                            "parser_version": PI_PARSER_VERSION,
                            **payload,
                        },
                    )
                    if stored is not None and isinstance(stored.get("seq"), int):
                        event_refs.append(EvidenceRef(
                            kind="event", run_id=self.run["id"], locator=str(stored["seq"]),
                        ))

        budgets = {"max_steps": effective["max_steps"]}
        if effective["max_tool_calls"] is not None:
            budgets["max_tool_calls"] = effective["max_tool_calls"]
        session = PiBridgeSession(
            run_id=self.run["id"],
            case_id=case_id,
            session_id=session_id,
            operation_id=operation_id,
            workspace=workspace.root,
            model_config={"id": self.model_id, "name": self.model_id},
            responses=self.script,
            provider_config=self.provider_config,
            tools=effective["tools"],
            system_prompt=self.system_prompt,
            budgets=budgets,
            bridge_path=self._bridge_path,
            node_binary=self._node,
            idle_timeout=effective["idle_timeout"],
            total_timeout=effective["total_timeout"],
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
        pending_error: BaseException | None = None
        try:
            session.start()
            self._enforce_sdk_version(session, effective["expected_sdk"])
            watcher.start()
            try:
                outcome = session.run(case["input"])
            finally:
                stop_watch.set()
        except BaseException as error:  # noqa: BLE001 - 异常路径也要采集与清理
            pending_error = error
            if isinstance(error, PiBridgeError) and error.code == "PI_BRIDGE_TIMEOUT":
                # 总时限到期：终止已下发，证据照常冻结（M4 review R09）。
                outcome = {
                    "status": "timeout", "final_output": None,
                    "steps": 0, "tool_calls": len(tool_records),
                    "usage": {"reported": False, "source": "bridge-timeout"},
                    "failure": "total timeout exceeded",
                }
            else:
                outcome = {
                    "status": "error", "final_output": None, "steps": 0,
                    "tool_calls": len(tool_records),
                    "usage": {"reported": False, "source": "bridge-error"},
                }
        finally:
            stop_watch.set()
            if watcher.is_alive():
                watcher.join(timeout=1)
            try:
                stopped = session.close()
            except Exception as error:  # noqa: BLE001
                stopped = {"status": "failed", "residual": [], "error": str(error)}

        outcome["operation_id"] = operation_id
        outcome["process_stop"] = stopped
        if not stopped or stopped.get('status') != 'stopped':
            from .execution_backends import ExecutionBackendError

            error = ExecutionBackendError(
                'RUNTIME_STOP_UNCONFIRMED',
                f'Pi bridge stop unconfirmed; retained workspace {workspace.root}; review required',
            )
            error.quarantine = True
            error.evidence = {'process_stop': stopped, 'workspace': str(workspace.root),
                              'attempt_id': self._attempt_id, 'session_id': session_id,
                              'operation_id': operation_id}
            if self._service is not None:
                self._service.emit_run_event(self.run['id'], 'runtime_stop_unconfirmed', {
                    'case_id': case_id,
                    **error.evidence,
                })
            raise error
        self._log_invocations(case_id, session_id, outcome)
        try:
            observation = self._capture(workspace, case, outcome, before, event_refs, tool_records)
        finally:
            if not stopped or stopped.get("status") != "stopped":
                cleanup = {"status": "failed", "residual": [str(workspace.root)],
                           "error": "bridge stop unconfirmed; workspace retained"}
            else:
                try:
                    cleanup = workspace.cleanup()
                except BaseException as error:  # noqa: BLE001
                    cleanup = {"status": "failed", "residual": [], "error": str(error)}

        duration_ms = round((monotonic() - started) * 1000, 3)
        termination_reason = self._termination_reason(outcome)
        envelope = {
            "agent": {
                "final_output": outcome.get("final_output"),
                "termination_reason": termination_reason,
                "termination_detail": (
                    outcome.get("budget_stop_reason")
                    or outcome.get("failure")
                ),
                "steps": outcome.get("steps", 0),
                "tool_calls": outcome.get("tool_calls", 0),
                "duration_ms": duration_ms,
                "runtime": {
                    "backend": f"{PI_BACKEND_ID}@{PI_BACKEND_VERSION}",
                    "session_id": session_id,
                    "operation_id": operation_id,
                    "parser_version": PI_PARSER_VERSION,
                    "process_stop": stopped,
                    "model_control": "runner-configured",
                    "transport": outcome.get("transport") or (
                        "http" if self.provider_config else "scripted"
                    ),
                    "usage_source": (outcome.get("usage") or {}).get("source"),
                    "config_hash": self.profile.get("config_hash"),
                },
            },
            "observation": observation,
            "events": [],
            "artifacts_captured": len(observation.get("artifact_refs") or []),
            "capture_errors": list(
                (observation.get("coverage") or {}).get("missing") or []
            ),
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

    # ------------------------------------------------------------ 版本门（R18）

    def _enforce_sdk_version(self, session: Any, expected: str) -> None:
        """bridge 报告的 SDK 版本与快照 pinned upstream 不符即 fail closed。"""
        if not expected:
            return
        observed = getattr(session, "sdk_version", None)
        if observed != expected:
            from .execution_backends import ExecutionBackendError

            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
            raise ExecutionBackendError(
                "RUNTIME_VERSION_DRIFT",
                f"pi sdk version drift: bridge reported {observed!r}, "
                f"pinned {expected!r} (fail closed; no matrix relaxation)",
            )

    @staticmethod
    def _termination_reason(outcome: dict[str, Any]) -> str:
        budget_reason = outcome.get("budget_stop_reason")
        if budget_reason in _BUDGET_TERMINATION_REASONS:
            return _BUDGET_TERMINATION_REASONS[budget_reason]
        if outcome.get("interrupted"):
            return "cancelled"
        return _TERMINATION_REASONS.get(outcome.get("status"), "error")

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
                    "operation_id": outcome.get("operation_id"),
                    "model_control": "runner-configured",
                    "transport": outcome.get("transport") or "scripted",
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
        stopped = outcome.get("process_stop") or {}
        stop_confirmed = stopped.get("status") == "stopped"
        if not stop_confirmed:
            errors.append("bridge stop unconfirmed; workspace evidence may still change")
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
            "attempt_id": self._attempt_id,
            "final_output": outcome.get("final_output"),
            "termination": {
                "reason": self._termination_reason(outcome),
                "detail": outcome.get("budget_stop_reason") or outcome.get("failure"),
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
                "cost_total": usage.get("observed_cost"),
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
                "complete": stop_confirmed and bool(before.get("complete", True) and after.get("complete", True)),
                "before_hashes": dict(before.get("hashes") or {}),
                "after_hashes": dict(after.get("hashes") or {}),
            },
            "processes": [{"label": "pi-bridge", "exit_code": stopped.get("exit_code"),
                           "status": "exited" if stop_confirmed else "unknown"}],
        }
        observation["evidence_hash"] = observation_evidence_hash(observation)
        observation["recorded_at"] = datetime.now(UTC).isoformat()
        return observation
