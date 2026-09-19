"""builtin-agent@1 执行后端：Case 级装配、调用日志与冻结 Observation。

每次模型/工具调用经 InvocationRecord（prepared -> dispatching -> settled）
持久化边界；运行事件经 RunService 进入持久 trace；终止后采集工作区产物、
构建冻结 Observation 并写入 ArtifactStore。隐藏断言（expected/forbidden_paths）
从不进入模型输入或工具可见空间。
"""
from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from motte_contracts.agent_tasks import (
    AGENT_BACKEND_ID,
    AGENT_BACKEND_VERSION,
    AGENT_TOOLS,
)
from motte_contracts.evaluation import (
    ArtifactEntry,
    EvidenceRef,
    FrozenObservation,
    ToolCallRecord,
    observation_evidence_hash,
)
from motte_trace.redaction import redact


class AgentBackendError(ValueError):
    """Agent manifest 校验失败；code 供创建期结构化错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_agent_manifest(manifest: dict[str, Any]) -> None:
    agent = manifest.get("agent")
    if agent != f"{AGENT_BACKEND_ID}@{AGENT_BACKEND_VERSION}":
        raise AgentBackendError(
            "AGENT_MANIFEST_INVALID",
            f"agent backend requires manifest.agent == {AGENT_BACKEND_ID}@{AGENT_BACKEND_VERSION}",
        )
    config = manifest.get("agent_config")
    if not isinstance(config, dict):
        raise AgentBackendError("AGENT_MANIFEST_INVALID", "agent runs require agent_config")
    from motte_contracts.agent_tasks import AGENT_MODES

    mode = config.get("mode")
    if mode not in AGENT_MODES:
        raise AgentBackendError(
            "AGENT_MODE_UNSUPPORTED",
            f"agent mode must be one of {', '.join(AGENT_MODES)}; got {mode!r}",
        )
    tools = config.get("tools") or list(AGENT_TOOLS)
    unknown_tools = [tool for tool in tools if tool not in AGENT_TOOLS]
    if unknown_tools:
        raise AgentBackendError(
            "AGENT_TOOLS_UNSUPPORTED",
            f"agent tool set allows only {', '.join(AGENT_TOOLS)}; got {unknown_tools}",
        )
    provider = manifest.get("provider")
    if not isinstance(provider, dict) or not provider.get("kind"):
        raise AgentBackendError(
            "PROVIDER_CONFIG_INVALID", "agent runs require a resolved provider snapshot"
        )
    snapshot = manifest.get("benchmark_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("suite") != "agent-tasks":
        raise AgentBackendError(
            "AGENT_SNAPSHOT_INVALID", "agent runs require an agent-tasks snapshot"
        )
    from motte_agent.budget import BudgetConfigError, ExecutionBudget

    try:
        ExecutionBudget.from_config(config.get("budget") or {})
    except BudgetConfigError as error:
        raise AgentBackendError("AGENT_BUDGET_INVALID", str(error)) from error


def build_agent_provider(manifest: dict[str, Any]):
    """构造 HTTP case provider；凭据/传输参数来自已解析 provider 快照。"""
    from motte_provider.config import build_case_provider

    provider_config = manifest["provider"]
    projected = {"cases": deepcopy(manifest.get("cases") or {})}
    return build_case_provider(provider_config, projected.get("cases"))


def _workspace_root(run_id: str, case_id: str) -> Path:
    base = Path(os.environ.get("MOTTE_AGENT_WORKSPACE_ROOT", "var/agent-workspaces"))
    return base / run_id / case_id


def _artifact_store():
    from motte_storage.artifacts import ArtifactStore

    return ArtifactStore(Path(os.environ.get("ARTIFACT_ROOT", "var/artifacts")))


def _guess_media_type(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith((".md", ".markdown")):
        return "text/markdown"
    if lowered.endswith(".csv"):
        return "text/csv"
    if lowered.endswith(".txt") or not Path(lowered).suffix:
        return "text/plain"
    return "application/octet-stream"


def _merged_budget(run_budget: dict[str, Any], case_limits: dict[str, Any]) -> dict[str, Any]:
    """Case 限只能收紧（取更小值），不能放宽 run 级预算。"""
    merged = dict(run_budget)
    for key, value in (case_limits or {}).items():
        if key not in merged or merged[key] is None:
            merged[key] = value
            continue
        try:
            merged[key] = min(merged[key], value)
        except TypeError:
            merged[key] = value
    return merged


def _workspace_tools(workspace) -> dict[str, Any]:
    def read_file(arguments):  # noqa: ANN001
        return workspace.read_text(arguments["path"])

    def write_file(arguments):  # noqa: ANN001
        return workspace.write_text(arguments["path"], arguments["content"])

    def list_files(arguments):  # noqa: ANN001
        files = workspace.list_files(arguments.get("prefix", ""))
        return "\n".join(files) if files else "(empty)"

    return {
        "read_file": read_file,
        "write_file": write_file,
        "list_files": list_files,
    }


def _bounded_events(events: list[dict[str, Any]], limit: int = 500) -> list[dict[str, Any]]:
    bounded = [redact(deepcopy(event)) for event in events[:limit]]
    if len(events) > limit:
        bounded.append({"type": "events_truncated", "dropped": len(events) - limit})
    return bounded


class _CaseEvidence:
    """一个 Case 执行期收集的证据引用（事件 seq / invocation id）。"""

    def __init__(self) -> None:
        self.event_refs: list[EvidenceRef] = []
        self.invocation_refs: list[EvidenceRef] = []


class AgentCaseExecutor:
    """一个 Case 的执行器：workspace、runtime、调用日志与 Observation 采集。"""

    def __init__(self, run: dict[str, Any], *, provider_complete, service: Any = None) -> None:
        self.run = run
        self.manifest = run.get("manifest") or {}
        self.config = self.manifest.get("agent_config") or {}
        self.mode = self.config.get("mode", "legacy-json")
        self.provider_complete = provider_complete
        self._service = service
        self._model_name = (self.manifest.get("provider") or {}).get("model") or "agent-model"

    def bind_service(self, service: Any) -> None:
        """Dispatcher 在执行前接入 RunService（事件 / 调用日志 / 取消探测）。"""
        self._service = service

    # ------------------------------------------------------------ 公共入口

    def invoke(self, case_id: str) -> dict[str, Any]:
        from motte_sdk.agent_tasks import selected_agent_cases_from_snapshot

        cases = selected_agent_cases_from_snapshot(self.run)
        case = next((item for item in cases if item["case_id"] == case_id), None)
        if case is None:
            raise KeyError(case_id)

        from motte_agent.builtin_react import BuiltinReActRuntime
        from motte_agent.budget import ExecutionBudget
        from motte_sandbox.workspace import CaseWorkspace, WorkspacePolicyError, WorkspaceQuotas

        budget_config = _merged_budget(self.config.get("budget") or {}, case.get("limits") or {})
        budget = ExecutionBudget.from_config(budget_config)
        workspace = CaseWorkspace(_workspace_root(self.run["id"], case_id), quotas=WorkspaceQuotas())
        workspace.materialize_fixture(case.get("fixture") or {})
        # fixture 属预置文件：before 快照在物化后取，forbidden 判定不含平台写入
        before = workspace.snapshot()

        evidence = _CaseEvidence()
        state = {"step": 0}

        def complete_with_logging(request):  # noqa: ANN001
            state["step"] += 1
            invocation = self._log_invocation(case_id, "model", state["step"], {
                "messages": len(request.messages),
                "tools": len(request.tools),
                "system": bool(request.system),
            })
            try:
                envelope = self.provider_complete(request)
            except Exception as error:  # noqa: BLE001 - settled failed + re-raise
                self._settle_invocation(invocation, "failed", {"error": str(error)})
                raise
            self._settle_invocation(invocation, "succeeded", {
                "finish_reason": envelope.get("finish_reason"),
                "usage": envelope.get("usage"),
                "tool_calls": len(envelope.get("tool_calls") or []),
            })
            return envelope

        tools = _workspace_tools(workspace)

        def tool_with_logging(name: str, handler):  # noqa: ANN001
            def wrapped(arguments):  # noqa: ANN001
                invocation = self._log_invocation(
                    case_id, "tool", state["step"], {"arguments": arguments},
                    tool_name=name,
                )
                try:
                    result = handler(arguments)
                except Exception as error:  # noqa: BLE001
                    self._settle_invocation(invocation, "failed", {"error": str(error)})
                    raise
                self._settle_invocation(invocation, "succeeded", {
                    "result": str(result)[:512],
                })
                return result

            return wrapped

        allowed_tools = self.config.get("tools") or list(AGENT_TOOLS)
        runtime = BuiltinReActRuntime(
            complete_with_logging,
            {name: tool_with_logging(name, handler)
             for name, handler in tools.items() if name in allowed_tools},
            model=self._model_name,
            mode=self.mode,
            budget=budget,
            event_sink=self._event_sink(case_id, evidence),
            should_cancel=self._cancel_check(),
        )

        started = monotonic()
        try:
            outcome = runtime.run_agent(case["input"])
        except WorkspacePolicyError as error:
            outcome = {
                "final_output": None,
                "termination_reason": "error",
                "termination_detail": f"workspace policy: {error.code}: {error}",
                "steps": 0, "tool_calls": budget.tool_calls_made(),
                "events": list(runtime.events), "messages": [],
                "usage": budget.observed_usage(),
                "budget": budget.enforcement_report(),
            }
        duration_ms = round((monotonic() - started) * 1000, 3)

        observation, capture_errors = self._capture(
            workspace, case, outcome, before, evidence,
        )
        cleanup = workspace.cleanup()
        return {
            "agent": {
                "final_output": outcome.get("final_output"),
                "termination_reason": outcome.get("termination_reason"),
                "termination_detail": outcome.get("termination_detail"),
                "steps": outcome.get("steps", 0),
                "tool_calls": outcome.get("tool_calls", 0),
                "prompt_version": self.config.get("prompt_version"),
                "mode": self.mode,
                "duration_ms": duration_ms,
                "budget": outcome.get("budget") or {},
            },
            "observation": observation,
            "events": _bounded_events(outcome.get("events") or []),
            "artifacts_captured": len(observation.get("artifact_refs") or []),
            "capture_errors": capture_errors,
            "cleanup": cleanup,
        }

    # ------------------------------------------------------------ 证据通道

    def _cancel_check(self):
        service = self._service

        def check() -> bool:
            if service is None:
                return False
            try:
                run = service.store.runs.get(self.run["id"])
            except Exception:  # noqa: BLE001 - 探测失败按未取消处理
                return False
            return bool(run and run.get("cancellation"))

        return check

    def _event_sink(self, case_id: str, evidence: _CaseEvidence):
        service = self._service

        def sink(event: dict[str, Any]) -> None:
            if service is None:
                return
            payload = redact(deepcopy(event))
            payload.pop("type", None)
            stored = service.emit_run_event(
                self.run["id"], str(event.get("type") or "agent_event"),
                {"case_id": case_id, **payload},
            )
            if stored is not None and isinstance(stored.get("seq"), int):
                evidence.event_refs.append(EvidenceRef(
                    kind="event", run_id=self.run["id"], locator=str(stored["seq"]),
                ))

        return sink

    def _invocations(self):
        store = getattr(self._service, "store", None) if self._service is not None else None
        return getattr(store, "invocations", None) if store is not None else None

    def _log_invocation(
        self, case_id: str, kind: str, step: int, summary: dict[str, Any], *,
        tool_name: str | None = None,
    ) -> dict[str, Any] | None:
        invocations = self._invocations()
        if invocations is None:
            return None
        record = invocations.create({
            "id": f"inv-{uuid4().hex}",
            "run_id": self.run["id"],
            "case_id": case_id,
            "kind": kind,
            "step": max(1, int(step)),
            "status": "prepared",
            "tool_name": tool_name,
            "model": self._model_name if kind == "model" else None,
            "request_summary": redact(deepcopy(summary)),
            "prepared_at": datetime.now(UTC).isoformat(),
        })
        return invocations.transition(
            record["id"], expected_revision=record["revision"],
            expected_status="prepared", status="dispatching",
            changes={"dispatched_at": datetime.now(UTC).isoformat()},
        )

    def _settle_invocation(
        self, invocation: dict[str, Any] | None, outcome: str, summary: dict[str, Any],
    ) -> None:
        invocations = self._invocations()
        if invocations is None or invocation is None:
            return
        invocations.transition(
            invocation["id"], expected_revision=invocation["revision"],
            expected_status="dispatching", status="settled",
            changes={
                "outcome": outcome,
                "result_summary": redact(deepcopy(summary)),
                "settled_at": datetime.now(UTC).isoformat(),
            },
        )

    # ------------------------------------------------------------ Observation

    def _capture(
        self, workspace, case: dict[str, Any], outcome: dict[str, Any],
        before: dict[str, Any], evidence: _CaseEvidence,
    ) -> tuple[dict[str, Any], list[str]]:
        after = workspace.snapshot()
        artifacts: list[dict[str, Any]] = []
        errors: list[str] = []
        artifact_store = _artifact_store()
        before_files = set(before.get("files") or [])
        after_files = set(after.get("files") or [])
        for rel in sorted(after_files):
            artifact_id = f"agent/{self.run['id']}/{case['case_id']}/{rel}"
            try:
                data = (workspace.root / rel).read_bytes()
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
            except Exception as error:  # noqa: BLE001 - 采集失败降 coverage，不吞证据
                errors.append(f"{rel}: {type(error).__name__}: {error}")
                artifacts.append({
                    "artifact_id": artifact_id, "path": rel, "available": False,
                })

        events = outcome.get("events") or []
        result_ids = {
            event.get("call_id") for event in events if event.get("type") == "tool_result"
        }
        tool_calls = [
            {
                "call_id": str(event.get("call_id") or f"legacy-{index}"),
                "tool_name": str(event.get("tool") or "unknown"),
                "arguments": redact(deepcopy(event.get("arguments") or event.get("input") or {})),
                "status": (
                    "succeeded" if event.get("call_id") in result_ids
                    else "denied" if event.get("type") == "tool_denied"
                    else "failed"
                ),
                "step": int(event.get("step", 1)),
            }
            for index, event in enumerate(events) if event.get("type") == "tool_call"
        ]
        usage = outcome.get("usage") or {}
        capture_complete = (
            bool(before.get("complete", True)) and bool(after.get("complete", True))
            and not errors
        )
        observation_payload: dict[str, Any] = {
            "observation_id": f"obs-{uuid4().hex}",
            "run_id": self.run["id"],
            "case_id": case["case_id"],
            "attempt_id": None,
            "final_output": outcome.get("final_output"),
            "termination": {
                "reason": outcome.get("termination_reason", "error"),
                "detail": outcome.get("termination_detail"),
            },
            "event_refs": [ref.model_dump() for ref in evidence.event_refs],
            "artifact_refs": artifacts,
            "coverage": {
                "complete": capture_complete,
                "events_captured": len(events),
                "artifacts_captured": len([a for a in artifacts if a.get("available")]),
                "artifacts_expected": len(after_files),
                "missing": errors,
            },
            "usage": {
                "reported": bool(usage.get("reported")),
                "total_tokens": usage.get("total_tokens"),
                "cost_total": usage.get("observed_cost") or None,
            },
            "tool_calls": tool_calls,
            "workspace": {
                "before": sorted(before_files),
                "after": sorted(after_files),
                "complete": bool(
                    before.get("complete", True) and after.get("complete", True)
                ),
            },
            "processes": [],
        }
        observation_payload["evidence_hash"] = observation_evidence_hash(observation_payload)
        observation_payload["recorded_at"] = datetime.now(UTC).isoformat()
        return observation_payload, errors
