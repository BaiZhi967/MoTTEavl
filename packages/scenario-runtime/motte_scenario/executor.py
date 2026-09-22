"""T05 装配层：把一个 Workflow 接进既有 CaseAttempt / Observation 链路。

一个 CaseAttempt 装配一个 FixtureInstance + 一个 TargetSession + 一个引擎
实例；流程步骤不新建调度器，也不产生伪 Trial。事件、调用日志、取消探测与
Artifact 全部走既有 RunService / ArtifactStore 通道。

真值隔离：目标只能看到 fixture 的业务可见投影（visible_state /
visible_tool_result），checker 真值、gold 与隐藏断言留在 PrivateTruth 的私有根，
永不进入事件、产物或工具结果。工具模式（real / mock / replay / deny）各自消费
**自己的**来源：mock 走显式模拟实现、replay 消费冻结记录，未配置支持即具名
拒绝，绝不回退到真实 handler，目标自身调用也不再把 real 写死。

冻结证据（R6）：执行结束前把 checkpoint 业务状态与有序业务动作日志物化成
Artifact，并组装成既有 FrozenObservation 契约（owner / step / artifact
sha256 / schema digest 全部齐备）。评分只读这一份冻结记录与它声明的产物，
从不读取活动 fixture；workflow-observation@1 仍是人读的运行摘要，
frozen_observation 才是评分输入。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Mapping
from uuid import uuid4

from motte_contracts.evaluation import (
    FrozenObservation,
    WorkflowActionLog,
    observation_evidence_hash,
    workflow_schema_digest,
)
from motte_contracts.fixture import FixtureSpec
from motte_contracts.workflow import TOOL_MODES, workflow_content_hash

from .compiler import compile_workflow
from .engine import (
    EvidenceBoundaryError,
    STATUS_NEEDS_REVIEW,
    WorkflowEngine,
    WorkflowOutcome,
)
from .fixtures import CleanupReport, FixtureRuntime
from .state import (
    hidden_field_paths,
    minimal_visible_fields,
    project_visible_value,
    state_content_hash,
    write_json_state,
)
from .targets import target_adapter

#: M5-T07：冻结注入声明在 Run 快照里的键（与 motte_skill.injection 同一常量）。
try:  # pragma: no cover - 常量本身在 motte_skill 里定义
    from motte_skill.injection import SKILL_INJECTION_SNAPSHOT_KEY
except ImportError:  # pragma: no cover - skill-runtime 不可用时仍可执行旧 Run
    SKILL_INJECTION_SNAPSHOT_KEY = "skill_injection"

#: 需要 fixture 的步骤类型：声明了它们却没有固定 Fixture 时，创建期与执行期
#: 必须给出同一个结论（R6 第 5 条：不能先接受、执行时才说缺 primary binding）。
FIXTURE_TOUCHING_STEP_KINDS = frozenset({"invoke_fixture_tool", "trigger_fixture_event"})

#: 业务动作日志的证据 id 与监控范围：指标配置里用 log/scope 引用它们。
ACTION_LOG_EVIDENCE_ID = "action-log"
ACTION_LOG_MONITORED_SCOPE = ("business_actions",)

_STATE_SCHEMA: dict[str, Any] = {"type": "object"}

#: 综合状态 → 冻结 Observation 的终止原因（契约词表）。
_TERMINATION_BY_STATUS = {
    "completed": "final_answer",
    "failed": "invalid_state",
    "cancelled": "cancelled",
    "budget_exceeded": "wall_time",
    "needs_review": "error",
}

_CODE_PREFIX = re.compile(r"\b[A-Z][A-Z0-9_]{3,}:")


def workflow_requires_fixture(snapshot: Mapping[str, Any] | None) -> bool:
    """这份 Workflow 快照是否需要固定 Fixture（创建期与执行期共用同一判断）。"""
    if not isinstance(snapshot, Mapping):
        return False
    if snapshot.get("fixture_refs"):
        return True
    stack: list[Any] = list(snapshot.get("steps") or [])
    while stack:
        step = stack.pop()
        if not isinstance(step, Mapping):
            continue
        if str(step.get("kind")) in FIXTURE_TOUCHING_STEP_KINDS:
            return True
        for key in ("then_steps", "else_steps", "body"):
            nested = step.get(key)
            if isinstance(nested, (list, tuple)):
                stack.extend(nested)
    return False


def _attach_cleanups(error: BaseException, cleanups: list[dict[str, Any]]) -> None:
    """把清理结论挂到原异常上：原错误与清理结果都不丢（R1 验收要求）。"""
    try:
        setattr(error, "cleanups", cleanups)
    except Exception:  # noqa: BLE001 - 极少数异常不允许设置属性
        pass
    if cleanups:
        try:
            error.add_note(
                "scenario fixture cleanup: "
                + json.dumps(cleanups, ensure_ascii=False, default=str)
            )
        except Exception:  # noqa: BLE001 - 备注只是补充证据
            pass


class ScenarioToolError(RuntimeError):
    """fixture 工具无法执行；code 供结构化错误映射。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        #: 结构化错误码要活着进入 Run 错误体（service._fail 读 error_class）。
        self.error_class = code


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def _safe_component(value: Any) -> str:
    """产物 id 的一段安全路径组件；形状被改写时追加内容摘要，保证唯一。"""
    text = str(value)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.") or "x"
    if slug != text or len(slug) > 64:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        slug = slug[:48] + "-" + digest
    return slug


def _termination(outcome: WorkflowOutcome) -> tuple[str, str | None]:
    """冻结 Observation 的终止事实；业务断言失败与执行异常不混为一谈。"""
    detail = outcome.reason
    text = (detail or "").lower()
    reason = _TERMINATION_BY_STATUS.get(outcome.status, "error")
    if "per_call_timeout" in text or "exceeded its deadline" in text or "timed out" in text:
        reason = "per_call_timeout"
    elif "max_tool_calls" in text:
        reason = "max_tool_calls"
    elif "max_steps" in text:
        reason = "max_steps"
    elif outcome.status == "budget_exceeded" or "wall_time" in text:
        reason = "wall_time"
    elif outcome.status == "failed":
        crashed = any(
            "error:" in (step.detail or "").lower()
            or "exception" in (step.detail or "").lower()
            or _CODE_PREFIX.search(step.detail or "") is not None
            for step in outcome.steps
        )
        reason = "error" if crashed else "invalid_state"
    return reason, detail


def _capture_complete(outcome: WorkflowOutcome) -> bool:
    """证据是否完整：停止未确认 / 需要复核时现场不可信，一律 incomplete。

    incomplete 的冻结证据不允许支撑任何否定结论（no-side-effect 只能
    insufficient），也不会被当成"已确认无副作用"。
    """
    interrupt = outcome.interrupt
    unconfirmed = isinstance(interrupt, Mapping) and interrupt.get("confirmed") is False
    return not (outcome.needs_review or outcome.status == STATUS_NEEDS_REVIEW or unconfirmed)


class ScenarioLedger:
    """一个 Case 的调用账本：工具/模型 Invocation + 有序业务动作日志。

    账本是**证据**，不是第二套状态机：写入失败不影响业务执行，但不会假装调用
    边界已经落库。
    """

    def __init__(
        self, *, run_id: str, case_id: str, attempt_id: str,
        service: Any = None, model: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.case_id = case_id
        self.attempt_id = attempt_id
        self.service = service
        self.model = model
        self.step = 1
        self.ordinal = 0
        self.events = 0
        self.event_refs: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.invocation_errors: list[str] = []
        self._pending_models: list[dict[str, Any]] = []

    # ------------------------------------------------------------ 事件通道

    def observe(self, event: Mapping[str, Any]) -> None:
        """目标运行时的观察事件：推进当前步骤、记录模型调用账本。"""
        self.events += 1
        step_seq = event.get("step_seq")
        if isinstance(step_seq, int) and step_seq >= 1:
            self.step = step_seq
        event_type = str(event.get("type") or "")
        step = event.get("step")
        runtime_step = step if isinstance(step, int) and step >= 1 else self.step
        if event_type == "model_request":
            pending = self._invocation(
                kind="model", step=runtime_step, tool_name=None,
                summary={"messages": event.get("messages"), "tools": event.get("tools")},
            )
            self._pending_models.append(pending or {})
        elif event_type in ("model_response", "model_call_timeout"):
            pending = self._pending_models.pop(0) if self._pending_models else None
            outcome = "succeeded" if event_type == "model_response" else "failed"
            summary = (
                {"finish_reason": event.get("finish_reason"), "usage": event.get("usage")}
                if event_type == "model_response"
                else {"reason": "per_call_timeout"}
            )
            self._settle(pending, outcome, summary)
        elif event_type == "turn_deadline_exceeded":
            while self._pending_models:
                self._settle(self._pending_models.pop(0), "failed",
                             {"reason": "per_call_timeout"})

    def note_event_ref(self, seq: Any) -> None:
        if isinstance(seq, int) and seq >= 1:
            self.event_refs.append({
                "kind": "event", "run_id": self.run_id, "locator": str(seq),
            })

    # ------------------------------------------------------------ 工具调用

    def tool_started(
        self, *, tool: str, mode: str, arguments: Mapping[str, Any], source: str,
    ) -> dict[str, Any]:
        self.ordinal += 1
        return {
            "call_id": "scenario-" + self.case_id + "-" + str(self.ordinal),
            "tool": tool,
            "mode": mode,
            "arguments": dict(arguments),
            "source": source,
            "step": self.step,
            "invocation": self._invocation(
                kind="tool", step=self.step, tool_name=tool,
                summary={"mode": mode, "source": source, "arguments": dict(arguments)},
            ),
        }

    def tool_finished(
        self, handle: Mapping[str, Any], *, status: str, result: Any = None,
        error: str | None = None,
    ) -> None:
        """记录一次工具调用的结局：Invocation settle + ToolCallRecord + 动作记录。"""
        call_status = {"succeeded": "succeeded", "denied": "denied"}.get(status, "failed")
        self._settle(
            handle.get("invocation"), "succeeded" if call_status == "succeeded" else "failed",
            {"status": status, **({"error": error} if error else {})},
        )
        tool_name = str(handle.get("tool") or "")
        self.calls.append({
            "call_id": str(handle.get("call_id")),
            "tool_name": tool_name,
            "arguments": dict(handle.get("arguments") or {}),
            "status": call_status,
            "step": max(1, int(handle.get("step") or 1)),
        })
        self.actions.append({
            "seq": len(self.actions) + 1,
            "step": max(1, int(handle.get("step") or 1)),
            "action": tool_name,
            "target": _business_target(handle.get("arguments") or {}),
            "status": call_status,
            "call_id": str(handle.get("call_id")),
            "detail": json.dumps(
                {"mode": handle.get("mode"), "source": handle.get("source"),
                 "arguments": dict(handle.get("arguments") or {}),
                 **({"error": error} if error else {})},
                ensure_ascii=False, sort_keys=True, default=str,
            )[:500],
        })

    # ------------------------------------------------------------ 冻结输入

    def tool_call_records(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.calls]

    def action_log(self) -> dict[str, Any]:
        return {"actions": [dict(item) for item in self.actions]}

    # ------------------------------------------------------------ Invocation

    def _invocations(self) -> Any:
        store = getattr(self.service, "store", None) if self.service is not None else None
        return getattr(store, "invocations", None) if store is not None else None

    def _invocation(
        self, *, kind: str, step: int, tool_name: str | None,
        summary: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        invocations = self._invocations()
        if invocations is None:
            return None
        try:
            record = invocations.create({
                "id": "inv-" + uuid4().hex,
                "run_id": self.run_id,
                "case_id": self.case_id,
                "attempt_id": self.attempt_id,
                "kind": kind,
                "step": max(1, int(step)),
                "status": "prepared",
                "tool_name": tool_name,
                "model": self.model if kind == "model" else None,
                "request_summary": {k: v for k, v in summary.items() if v is not None},
                "prepared_at": datetime.now(UTC).isoformat(),
            })
            return invocations.transition(
                record["id"], expected_revision=record["revision"],
                expected_status="prepared", status="dispatching",
                changes={"dispatched_at": datetime.now(UTC).isoformat()},
            )
        except Exception as error:  # noqa: BLE001 - no action without durable boundary
            detail = "prepared/dispatching: " + type(error).__name__ + ": " + str(error)
            self.invocation_errors.append(detail)
            raise EvidenceBoundaryError(
                "INVOCATION_PERSISTENCE_FAILED", detail,
            ) from error

    def _settle(
        self, invocation: Mapping[str, Any] | None, outcome: str,
        summary: Mapping[str, Any],
    ) -> None:
        invocations = self._invocations()
        if invocations is None or invocation is None:
            return
        try:
            invocations.transition(
                invocation["id"], expected_revision=invocation["revision"],
                expected_status="dispatching", status="settled",
                changes={
                    "outcome": outcome,
                    "result_summary": dict(summary),
                    "settled_at": datetime.now(UTC).isoformat(),
                },
            )
        except Exception as error:  # noqa: BLE001 - dispatching remains indeterminate
            detail = "settle: " + type(error).__name__ + ": " + str(error)
            self.invocation_errors.append(detail)
            raise EvidenceBoundaryError("INVOCATION_SETTLE_FAILED", detail) from error


def _business_target(arguments: Mapping[str, Any]) -> str | None:
    for key in ("target", "order_id", "id", "path", "resource"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@dataclass
class FixtureBinding:
    """一个已装配的 fixture：实例、所有者令牌与可见投影入口。"""

    key: str
    spec: FixtureSpec
    instance: Any
    owner_token: str
    cleanup: CleanupReport | None = None


class NullFixturePort:
    """没有固定 Fixture 的流程端口：状态恒为空，业务工具/事件一律具名拒绝。"""

    def __init__(self) -> None:
        #: 目标拿不到任何业务工具（无 fixture 就没有允许的工具）。
        self.tools: dict[str, Any] = {}

    @property
    def state(self) -> Mapping[str, Any]:
        return {}

    def snapshot(self) -> tuple[dict[str, Any], str]:
        return {}, state_content_hash({"kind": "none"})

    def invoke_tool(
        self, name: str, arguments: Mapping[str, Any], mode: str, *, source: str = "step",
    ) -> Any:
        raise ScenarioToolError(
            "SCENARIO_FIXTURE_REQUIRED",
            "workflow invoked fixture tool " + repr(name) + " without any pinned fixture",
        )

    def apply_event(self, event: str, payload: Mapping[str, Any]) -> Any:
        raise ScenarioToolError(
            "SCENARIO_FIXTURE_REQUIRED",
            "workflow triggered fixture event " + repr(event) + " without any pinned fixture",
        )


class FixturePortAdapter:
    """把 FixtureRuntime 投影成引擎需要的 FixturePort。

    工具与事件都必须显式实现：没有实现的工具/事件具名拒绝，绝不静默返回
    成功，也绝不把 checker 真值当作业务数据返回。每次调用都登记 Invocation
    与有序业务动作记录（真实副作用证据）。
    """

    def __init__(
        self,
        runtime: FixtureRuntime,
        binding: FixtureBinding,
        *,
        tools: Mapping[str, Callable[..., Any]] | None = None,
        mock_tools: Mapping[str, Callable[..., Any]] | None = None,
        replay_records: Mapping[str, Mapping[str, Any]] | None = None,
        events: Mapping[str, Callable[..., Any]] | None = None,
        tool_log: list[dict[str, Any]] | None = None,
        ledger: ScenarioLedger | None = None,
        gateway: Mapping[str, Any] | None = None,
    ) -> None:
        self.runtime = runtime
        self.binding = binding
        #: M5-T07：冻结注入计划的**唯一执行网关**（None = 旧 Run，没有 Skill 声明）。
        self.gateway = dict(gateway) if gateway else None
        #: 真实实现；只有 mode=real 才会被调用。
        self.tools = dict(tools or {})
        #: 显式模拟实现；mock 模式只走这里，绝不回退 real。
        self.mock_tools = dict(mock_tools or {})
        #: 冻结回放记录：{tool: {"result": ..., "state": ...}}。
        self.replay_records = {
            str(key): dict(value) for key, value in (replay_records or {}).items()
        }
        self.events = dict(events or {})
        self.tool_log = tool_log if tool_log is not None else []
        self.ledger = ledger or ScenarioLedger(run_id="-", case_id="-", attempt_id="-")

    # ------------------------------------------------------------ 引擎接口

    @property
    def state(self) -> Mapping[str, Any]:
        return self.runtime.visible_state(
            self.binding.instance, owner_token=self.binding.owner_token,
        )

    def snapshot(self) -> tuple[dict[str, Any], str]:
        snapshot = self.runtime.snapshot(
            self.binding.instance, owner_token=self.binding.owner_token,
        )
        state = self.runtime.visible_state(
            self.binding.instance, owner_token=self.binding.owner_token,
        )
        return state, snapshot.content_hash

    def invoke_tool(
        self, name: str, arguments: Mapping[str, Any], mode: str, *, source: str = "step",
    ) -> Any:
        """按 mode 分派一次工具调用；每个 mode 只消费自己的来源。"""
        call_arguments = dict(arguments or {})
        handle = self.ledger.tool_started(
            tool=name, mode=mode, arguments=call_arguments, source=source,
        )
        try:
            result = self._invoke(name, call_arguments, mode)
        except BaseException as error:
            denied = _is_denied(error)
            code = getattr(error, "code", None) or type(error).__name__
            detail = str(code) + ": " + str(error)
            self._log(name, mode, "denied" if denied else "failed", None,
                      handle=handle, source=source, error=detail)
            self.ledger.tool_finished(
                handle, status="denied" if denied else "failed", error=detail,
            )
            raise
        self._log(name, mode, "succeeded", result, handle=handle, source=source)
        self.ledger.tool_finished(handle, status="succeeded", result=result)
        return result

    def _gateway_check(self, name: str, mode: str) -> None:
        """有效权限在这里落地：Skill 不能让 deny/mock/replay 升级成 real。

        网关消费的是创建期冻结、并在此重建校验过的注入计划；请求比授权更强的
        模式（或未授权的工具）都具名拒绝，绝不静默降级成"实际还是跑了"。
        """
        gateway = self.gateway
        if not gateway:
            return
        from motte_skill.injection import InjectionError, select_for_execution

        try:
            select_for_execution(gateway["plan"], {name: mode})
        except InjectionError as error:
            raise ScenarioToolError(error.code, str(error)) from error

    def _invoke(self, name: str, arguments: dict[str, Any], mode: str) -> Any:
        self._gateway_check(name, mode)
        if mode == "deny":
            raise ScenarioToolError("SCENARIO_TOOL_DENIED", "tool denied by policy: " + name)
        if mode not in TOOL_MODES:
            raise ScenarioToolError(
                "SCENARIO_TOOL_MODE_UNKNOWN",
                "fixture tool " + repr(name) + " was requested with unknown mode " + repr(mode),
            )
        # allowed_tools 校验由 fixture 负责（tool_not_allowed）；权限拒绝同样要留证据。
        self.runtime.visible_tool_result(
            self.binding.instance, owner_token=self.binding.owner_token, tool=name,
        )
        if mode == "replay":
            # replay 不重新执行业务动作：只消费冻结记录。
            return self._replay(name)
        handler = (self.tools if mode == "real" else self.mock_tools).get(name)
        if handler is None:
            raise ScenarioToolError(
                self._missing_code(mode),
                "fixture tool " + repr(name) + " has no " + mode + " implementation; "
                + mode + " mode never falls back to the real handler",
            )
        visible_state = dict(self.state)
        outcome = handler(visible_state, dict(arguments))
        new_state, result = self._split_outcome(outcome)
        if new_state is not None:
            self._write_state(new_state)
        return self._project(name, result)

    @staticmethod
    def _missing_code(mode: str) -> str:
        return (
            "SCENARIO_TOOL_NOT_IMPLEMENTED" if mode == "real"
            else "SCENARIO_TOOL_MOCK_MISSING"
        )

    def _replay(self, name: str) -> Any:
        """消费一条冻结回放记录；没有记录就拒绝，绝不回退到真实 handler。"""
        record = self.replay_records.get(name)
        if record is None:
            raise ScenarioToolError(
                "SCENARIO_TOOL_REPLAY_MISSING",
                "fixture tool " + repr(name) + " has no frozen replay record; "
                "replay mode never re-executes the business action",
            )
        if "result" not in record:
            raise ScenarioToolError(
                "SCENARIO_TOOL_REPLAY_INVALID",
                "replay record for " + repr(name) + " must carry a 'result' field",
            )
        state = record.get("state")
        if state is not None:
            if not isinstance(state, Mapping):
                raise ScenarioToolError(
                    "SCENARIO_TOOL_REPLAY_INVALID",
                    "replay record state for " + repr(name) + " must be an object",
                )
            self._write_state(dict(state))
        return self._project(name, record["result"])

    def apply_event(self, event: str, payload: Mapping[str, Any]) -> Any:
        handler = self.events.get(event)
        if handler is None:
            raise ScenarioToolError(
                "SCENARIO_EVENT_NOT_IMPLEMENTED",
                "fixture event " + repr(event) + " has no implementation "
                "in this execution environment",
            )
        outcome = handler(dict(self.state), dict(payload))
        new_state, result = self._split_outcome(outcome)
        if new_state is not None:
            self._write_state(new_state)
        return self._project(event, result)

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _split_outcome(outcome: Any) -> tuple[dict[str, Any] | None, Any]:
        """handler 返回 (新状态, 结果) 时写回状态；只返回结果时状态不变。"""
        if isinstance(outcome, tuple) and len(outcome) == 2:
            new_state, result = outcome
            if new_state is not None and not isinstance(new_state, Mapping):
                raise ScenarioToolError(
                    "SCENARIO_TOOL_RESULT_INVALID", "handler returned a non-mapping state"
                )
            return (dict(new_state) if new_state is not None else None), result
        return None, outcome

    def _write_state(self, new_state: dict[str, Any]) -> None:
        if self.binding.spec.kind != "json":
            raise ScenarioToolError(
                "SCENARIO_TOOL_STATE_UNSUPPORTED",
                self.binding.spec.kind + " fixtures do not accept in-process state writes",
            )
        write_json_state(self.binding.instance.state_path, new_state)

    def _project(self, name: str, result: Any) -> Any:
        """工具结果只带最小业务可见字段：隐藏键（含嵌套 dict/list）一律剔除。"""
        if isinstance(result, Mapping):
            projected = minimal_visible_fields(result)
            if not projected:
                raise ScenarioToolError(
                    "SCENARIO_TOOL_RESULT_EMPTY",
                    "fixture tool " + repr(name) + " returned no business-visible field",
                )
        else:
            projected = project_visible_value(result)
        leaked = hidden_field_paths(projected)
        if leaked:
            # 纵深防御：投影之后还有隐藏键，说明有一条可见通道没按同一规则过滤。
            raise ScenarioToolError(
                "SCENARIO_HIDDEN_VALUE_LEAK",
                "fixture tool " + repr(name) + " result still carries hidden fields: "
                + ", ".join(leaked),
            )
        return projected

    def _log(
        self, name: str, mode: str, status: str, result: Any, *,
        handle: Mapping[str, Any] | None = None, source: str = "step",
        error: str | None = None,
    ) -> None:
        entry = {
            "tool": name, "mode": mode, "status": status,
            "result": result if isinstance(result, (str, int, float, bool, type(None))) else None,
            "fixture": self.binding.key,
        }
        if handle is not None:
            entry.update({
                "call_id": handle.get("call_id"),
                "arguments": dict(handle.get("arguments") or {}),
                "source": source,
                "step": handle.get("step"),
            })
        if error is not None:
            entry["error"] = error
        if self.ledger.invocation_errors:
            entry["invocation_error"] = self.ledger.invocation_errors[-1]
        self.tool_log.append(entry)


def _is_denied(error: BaseException) -> bool:
    """权限交集拒绝（而不是实现缺失/执行异常）才算 denied。"""
    return getattr(error, "code", None) in {"SCENARIO_TOOL_DENIED", "tool_not_allowed"}


@dataclass
class ScenarioCaseResult:
    workflow_ref: str
    outcome: WorkflowOutcome
    observation: dict[str, Any]
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    cleanups: list[dict[str, Any]] = field(default_factory=list)
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    target_kind: str = ""
    #: 评分输入：既有 FrozenObservation 契约（artifact / owner / hash 齐备）。
    frozen_observation: dict[str, Any] | None = None

    def envelope(self) -> dict[str, Any]:
        return {
            "scenario": {
                "workflow_ref": self.workflow_ref,
                "target_kind": self.target_kind,
                "status": self.outcome.status,
                "reason": self.outcome.reason,
                "turns": self.outcome.turns,
                "step_count": self.outcome.step_count,
                "tool_calls": self.outcome.tool_calls,
                "needs_review": self.outcome.needs_review,
                "interrupt": self.outcome.interrupt,
            },
            "observation": self.observation,
            "frozen_observation": self.frozen_observation,
            "artifacts_captured": len(self.artifacts),
            "tool_log": self.tool_log,
            "cleanup": self.cleanups,
        }


class ScenarioCaseExecutor:
    """一个 Run 的场景执行器；每个 Case 独立装配，互不共享状态。"""

    def __init__(
        self,
        run: dict[str, Any],
        *,
        fixture_anchor: str | None = None,
        tool_handlers: Mapping[str, Callable[..., Any]] | None = None,
        mock_handlers: Mapping[str, Callable[..., Any]] | None = None,
        replay_records: Mapping[str, Mapping[str, Any]] | None = None,
        event_handlers: Mapping[str, Callable[..., Any]] | None = None,
        service: Any = None,
    ) -> None:
        import os

        self.run = run
        self.manifest = run.get("manifest") or {}
        self.fixture_anchor = fixture_anchor or os.environ.get(
            "MOTTE_SCENARIO_FIXTURE_ROOT", "var/scenario-fixtures",
        )
        #: 真实 handler：只有 real 模式会用它。
        self.tool_handlers = dict(tool_handlers or {})
        #: 显式模拟实现：mock 模式专用（绝不回调真实 handler）。
        self.mock_handlers = dict(mock_handlers or {})
        #: 冻结回放记录：replay 模式专用（不重新执行业务动作）。
        self.replay_records = {
            str(key): dict(value) for key, value in (replay_records or {}).items()
        }
        self.event_handlers = dict(event_handlers or {})
        self._service = service
        #: 冻结的 Skill 注入网关（惰性解析一次；篡改即具名拒绝）。
        self._gateway: dict[str, Any] | None = None
        self._gateway_resolved = False

    def bind_service(self, service: Any) -> None:
        self._service = service

    # ------------------------------------------------------------ 公共入口

    def invoke(self, case_id: str) -> dict[str, Any]:
        compiled = compile_workflow(self.manifest["workflow_snapshot"])
        attempt_id = self._attempt_id(case_id)
        owner_token = uuid4().hex
        runtime = FixtureRuntime(anchor=self.fixture_anchor)
        # 逐实例移交清理所有权：每个 prepare 成功就立刻进入 bindings，后面的
        # prepare 失败时已完成实例仍然会被清理（F14），不再是"全局成功才赋值"。
        bindings: list[FixtureBinding] = []
        tool_log: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        ledger = ScenarioLedger(
            run_id=str(self.run.get("id")), case_id=case_id, attempt_id=attempt_id,
            service=self._service, model=self._model_name(),
        )
        target_started = False
        try:
            self._prepare_fixtures(runtime, case_id, attempt_id, owner_token, bindings)
            port = self._fixture_port(runtime, bindings, ledger, tool_log)
            target = self._open_target(port, case_id, ledger, events)
            target_started = True
            engine = WorkflowEngine(
                compiled, port, target,
                event_sink=lambda event: self._emit(events, case_id, event, ledger),
                cancel_check=self._cancel_check(),
                run_id=str(self.run.get("id")),
                case_id=case_id,
                attempt_id=attempt_id,
            )
            outcome = engine.run()
            observation, frozen, artifacts = self._freeze(
                compiled, case_id, attempt_id, outcome, ledger,
            )
        except BaseException as error:
            # 装配/执行抛错时目标状态未知：保留现场，原错误与清理结论都要留住。
            _attach_cleanups(error, self._release(runtime, bindings, retained=target_started))
            raise
        cleanups = self._release(
            runtime, bindings, retained=self._retention_required(outcome),
        )
        result = ScenarioCaseResult(
            workflow_ref=compiled.ref,
            outcome=outcome,
            observation=observation,
            artifacts=artifacts,
            cleanups=cleanups,
            tool_log=tool_log,
            target_kind=self._target_kind(),
            frozen_observation=frozen,
        )
        return result.envelope()

    @staticmethod
    def _retention_required(outcome: WorkflowOutcome) -> bool:
        """停止是否无法确认：needs_review / 未确认中断一律保留现场（F08）。"""
        if outcome.status == STATUS_NEEDS_REVIEW or outcome.needs_review:
            return True
        interrupt = outcome.interrupt
        return isinstance(interrupt, Mapping) and interrupt.get("confirmed") is False

    def _release(
        self, runtime: FixtureRuntime, bindings: list[FixtureBinding], *, retained: bool,
    ) -> list[dict[str, Any]]:
        return [
            self._cleanup(runtime, binding, interrupted=retained) for binding in bindings
        ]

    # ------------------------------------------------------------ 装配

    def _model_name(self) -> str | None:
        provider = self.manifest.get("provider")
        if isinstance(provider, Mapping) and isinstance(provider.get("model"), str):
            return str(provider["model"])
        model = self.manifest.get("model")
        return str(model) if isinstance(model, str) else None

    def _attempt_id(self, case_id: str) -> str:
        """真实 CaseAttempt 身份；没有活动 attempt 的直调场景退回合成身份。"""
        service = self._service
        store = getattr(service, "store", None) if service is not None else None
        attempts = getattr(store, "attempts", None)
        fallback = str(self.run.get("id")) + ":" + case_id + ":1"
        if attempts is not None:
            try:
                open_attempts = [
                    item for item in attempts.list_open(str(self.run.get("id")))
                    if item.get("case_id") == case_id
                ]
            except Exception:  # noqa: BLE001 - 身份解析失败不阻断执行
                open_attempts = []
            if open_attempts:
                return str(open_attempts[-1].get("id") or fallback)
        return fallback

    def _target_kind(self) -> str:
        from .target_identity import target_kind_of

        return target_kind_of(self.manifest)

    def _skill_gateway(self) -> dict[str, Any] | None:
        """冻结注入声明 → 执行网关；无声明返回 None（普通 Run 行为不变）。"""
        if self._gateway_resolved:
            return self._gateway
        self._gateway_resolved = True
        snapshots = self.manifest.get("resource_snapshots")
        declaration = (
            snapshots.get(SKILL_INJECTION_SNAPSHOT_KEY)
            if isinstance(snapshots, Mapping) else None
        )
        if not declaration:
            return None
        from motte_skill.injection import InjectionError, plan_from_declaration

        try:
            plan = plan_from_declaration(declaration)
        except InjectionError as error:
            raise ScenarioToolError(error.code, str(error)) from error
        self._gateway = {
            "plan": plan,
            "plan_hash": plan.plan_hash,
            "tool_modes": dict(plan.effective_permissions.tool_modes),
            "tools": list(plan.effective_permissions.tools),
            "network": plan.effective_permissions.network,
        }
        return self._gateway

    def _fixture_port(
        self,
        runtime: FixtureRuntime,
        bindings: list[FixtureBinding],
        ledger: ScenarioLedger,
        tool_log: list[dict[str, Any]],
    ) -> Any:
        if not bindings:
            return NullFixturePort()
        return FixturePortAdapter(
            runtime, bindings[0], tools=self.tool_handlers,
            mock_tools=self.mock_handlers, replay_records=self.replay_records,
            events=self.event_handlers, tool_log=tool_log, ledger=ledger,
            gateway=self._skill_gateway(),
        )

    def _open_target(
        self, port: Any, case_id: str, ledger: ScenarioLedger,
        events: list[dict[str, Any]] | None = None,
    ) -> Any:
        """目标拿到的是**受控工具桥**：每一次调用都经过 fixture 的权限与投影。

        桥上的 mode 来自冻结的有效权限（manifest.target_snapshot.tool_modes），
        不再固定 real：声明里没有 real 时，目标自身调用也不允许走 real。
        """
        kind = self._target_kind()
        adapter = target_adapter(kind)
        mode = self._target_tool_mode()

        def bridge(name: str) -> Any:
            def call(arguments: Mapping[str, Any] | None = None) -> Any:
                return port.invoke_tool(name, arguments or {}, mode, source="target")
            return call

        def on_event(event: Mapping[str, Any]) -> None:
            if events is None:
                ledger.observe(event)
                return
            self._target_event(events, case_id, dict(event), ledger)

        return adapter.open_session({
            "manifest": self.manifest,
            "run": self.run,
            "case_id": case_id,
            "tools": {name: bridge(name) for name in sorted(getattr(port, "tools", {}))},
            # 目标运行时的事件（模型/工具调用边界）进入同一个调用账本。
            "on_event": on_event,
        })

    def _target_tool_mode(self) -> str:
        """目标自身工具调用的有效 mode（消费冻结声明，不猜、不写死 real）。

        判定顺序：
        1. 冻结声明包含 real → real（显式授权）；
        2. 声明里没有 real 且只有一个模式 → 用该模式（整场 mock/replay）；
        3. 声明里没有 real 又不止一个模式 → 有效 mode 无法确定，具名拒绝。
        旧 manifest 完全没有声明时沿用历史默认 real，不改变既有行为。
        """
        gateway = self._skill_gateway()
        if gateway is not None:
            granted = {
                str(mode) for mode in (gateway.get("tool_modes") or {}).values()
            }
            if not granted:
                return "deny"
            from motte_skill.injection import TOOL_MODE_STRENGTH

            # 混合授权取**最保守**的模式：目标调用不允许借桥升级成 real。
            return max(
                sorted(granted),
                key=lambda mode: TOOL_MODE_STRENGTH.get(mode, 99),
            )
        declared = self._declared_tool_modes()
        if not declared:
            return "real"
        unknown = sorted(set(declared) - set(TOOL_MODES))
        if unknown:
            raise ScenarioToolError(
                "SCENARIO_TOOL_MODE_UNKNOWN",
                "target_snapshot.tool_modes declares unsupported modes: " + ", ".join(unknown),
            )
        if "real" in declared:
            return "real"
        if len(declared) == 1:
            return declared[0]
        raise ScenarioToolError(
            "SCENARIO_TOOL_MODE_AMBIGUOUS",
            "target_snapshot does not grant real execution and declares several modes "
            "(" + ", ".join(declared) + "); the effective target tool mode cannot be proven",
        )

    def _declared_tool_modes(self) -> tuple[str, ...]:
        snapshot = self.manifest.get("target_snapshot")
        if not isinstance(snapshot, Mapping):
            return ()
        modes = snapshot.get("tool_modes")
        if not isinstance(modes, (list, tuple, set, frozenset)):
            return ()
        ordered: list[str] = []
        for mode in modes:
            text = str(mode)
            if text not in ordered:
                ordered.append(text)
        return tuple(ordered)

    def _fixture_required(self) -> bool:
        return workflow_requires_fixture(self.manifest.get("workflow_snapshot"))

    def _prepare_fixtures(
        self,
        runtime: FixtureRuntime,
        case_id: str,
        attempt_id: str,
        owner_token: str,
        bindings: list[FixtureBinding],
    ) -> None:
        """逐个 prepare 并把清理所有权立刻交给调用方（F14）。

        没有 fixture 的纯消息流程走 NullFixturePort；但 Workflow 一旦声明了
        fixture（引用或 fixture 工具/事件步骤），缺失固定 Fixture 必须在这里
        具名拒绝——与创建期校验同一判断。
        """
        snapshots = self.manifest.get("fixture_snapshot") or {}
        if not snapshots:
            if self._fixture_required():
                raise ScenarioToolError(
                    "SCENARIO_FIXTURE_REQUIRED",
                    "the workflow declares fixture tools but the run pins no fixture snapshot",
                )
            return
        for key, record in sorted(snapshots.items()):
            spec = FixtureSpec.model_validate(record["record"])
            instance = runtime.prepare(
                spec,
                run_id=str(self.run.get("id")),
                case_id=case_id,
                attempt_id=attempt_id,
                owner_token=owner_token,
                business_id=self._business_id(case_id, spec),
            )
            # 先登记再继续：下一个 prepare 失败时这个实例已经被接管。
            bindings.append(FixtureBinding(
                key=key, spec=spec, instance=instance, owner_token=owner_token,
            ))

    def _business_id(self, case_id: str, spec: FixtureSpec) -> str | None:
        """业务 ID 只作为 fixture 内部标签；实例根已按 run/case/attempt 隔离，
        因此两个 Case 用同一业务 ID 也不会共享状态。"""
        cases = self.manifest.get("cases") or {}
        case = cases.get(case_id) if isinstance(cases, dict) else None
        if isinstance(case, Mapping):
            business = case.get("business_id")
            if isinstance(business, str) and business:
                return business
        return spec.fixture_id

    def _cleanup(
        self, runtime: FixtureRuntime, binding: FixtureBinding, *, interrupted: bool = False,
    ) -> dict[str, Any]:
        """清理一个实例；interrupted 表示停止未确认，此时保留现场与审计证据。"""
        try:
            report = runtime.cleanup(
                binding.instance, owner_token=binding.owner_token, interrupted=interrupted,
            )
            binding.cleanup = report
            record = report.to_record()
            record["evidence_path"] = report.evidence_path
            return record
        except BaseException as error:  # noqa: BLE001 - 清理失败如实上报
            return {
                "instance_id": binding.instance.instance_id,
                "status": "unknown",
                "interrupted": interrupted,
                "error": type(error).__name__ + ": " + str(error),
            }

    # ------------------------------------------------------------ 证据

    def _emit(
        self, sink: list[dict[str, Any]], case_id: str, event: dict[str, Any],
        ledger: ScenarioLedger | None = None,
    ) -> None:
        """引擎事件：进入内存证据、调用账本与既有 Run 事件通道。"""
        sink.append(event)
        if ledger is not None:
            ledger.observe(event)
        self._persist_event(case_id, str(event.get("type") or "scenario_event"), event, ledger)

    def _target_event(
        self, sink: list[dict[str, Any]], case_id: str, event: dict[str, Any],
        ledger: ScenarioLedger,
    ) -> None:
        """目标运行时事件（模型/工具调用边界）：同样落进账本与 Run 事件通道。"""
        sink.append({**event, "source": "target"})
        ledger.observe(event)
        self._persist_event(case_id, "target_" + str(event.get("type") or "event"), event, ledger)

    def _persist_event(
        self, case_id: str, event_type: str, event: Mapping[str, Any],
        ledger: ScenarioLedger | None,
    ) -> None:
        service = self._service
        if service is None:
            return
        payload = {key: value for key, value in event.items() if key != "type"}
        try:
            stored = service.emit_run_event(
                self.run["id"], event_type, {"case_id": case_id, **payload},
            )
        except Exception as error:  # noqa: BLE001 - evidence boundary
            raise EvidenceBoundaryError(
                "EVENT_PERSISTENCE_FAILED",
                f"event persistence failed: {type(error).__name__}: {error}",
            ) from error
        if not isinstance(stored, Mapping) or not isinstance(stored.get("seq"), int):
            raise EvidenceBoundaryError(
                "EVENT_PERSISTENCE_FAILED", "event persistence failed: no durable event returned",
            )
        if ledger is not None:
            ledger.note_event_ref(stored.get("seq"))

    def _cancel_check(self) -> Callable[[], bool]:
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

    def _freeze(
        self,
        compiled: Any,
        case_id: str,
        attempt_id: str,
        outcome: WorkflowOutcome,
        ledger: ScenarioLedger,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """冻结 Observation：checkpoint 以 Artifact + hash + owner + step 落盘。

        返回 (人读运行摘要, 评分用 FrozenObservation, artifact 清单)。评分输入
        只包含冻结产物与它们的内容 hash，任何隐藏真值都不进入这两个结构。
        """
        run_id = str(self.run.get("id"))
        complete = _capture_complete(outcome)
        store = self._artifact_store()
        seq_by_step = {step.step_id: step.seq for step in outcome.steps}
        fixture_id = self._primary_fixture_id()
        #: 人读 envelope 里的 checkpoint 产物清单（artifacts_captured 语义不变）。
        artifacts: list[dict[str, Any]] = []
        #: 冻结 Observation 声明的全部产物（checkpoint + 业务动作日志）。
        frozen_artifacts: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        for step_id, payload in sorted(outcome.checkpoints.items()):
            state = payload.get("state") or {}
            data = _canonical_json(state)
            sha256 = hashlib.sha256(data).hexdigest()
            artifact_id = (
                "scenario/" + _safe_component(run_id) + "/" + _safe_component(case_id)
                + "/" + _safe_component(step_id) + ".json"
            )
            if store is not None:
                store.put_bytes(artifact_id, data, kind="workflow-checkpoint",
                                media_type="application/json")
            frozen_artifacts.append({
                "artifact_id": artifact_id,
                "step_id": step_id,
                "label": payload.get("label"),
                "content_hash": payload.get("content_hash"),
                "sha256": sha256,
                "size_bytes": len(data),
                "owner": {"run_id": run_id, "case_id": case_id},
                "available": store is not None,
            })
            artifacts.append(dict(frozen_artifacts[-1]))
            evidence.append({
                "schema_version": 1,
                "evidence_id": str(step_id),
                "evidence_kind": "state",
                "owner": {
                    "run_id": run_id, "case_id": case_id, "attempt_id": attempt_id,
                    "fixture_id": fixture_id,
                },
                "step": max(1, int(seq_by_step.get(str(step_id), 1))),
                "artifact_id": artifact_id,
                "artifact_sha256": sha256,
                "media_type": "application/json",
                "schema_id": "workflow.state@1",
                "payload_schema": dict(_STATE_SCHEMA),
                "schema_sha256": workflow_schema_digest(_STATE_SCHEMA),
                "complete": complete,
                "monitored_scope": [],
            })
        evidence.append(self._action_log_evidence(
            run_id, case_id, attempt_id, fixture_id, ledger, complete, store,
            frozen_artifacts,
        ))
        payload = {
            "schema_version": 1,
            "observation_id": "scenario/" + run_id + "/" + case_id + "/" + attempt_id,
            "run_id": run_id,
            "case_id": case_id,
            "attempt_id": attempt_id,
            "final_output": outcome.final_output,
            "termination": {"reason": _termination(outcome)[0], "detail": outcome.reason},
            "event_refs": list(ledger.event_refs),
            "artifact_refs": [
                {
                    "artifact_id": item["artifact_id"],
                    "path": item["artifact_id"],
                    "media_type": "application/json",
                    "size_bytes": item["size_bytes"],
                    "sha256": item["sha256"],
                    "available": item["available"],
                }
                for item in frozen_artifacts
            ],
            "coverage": {
                "complete": complete,
                "events_captured": len(ledger.event_refs) or ledger.events,
                "artifacts_captured": len(frozen_artifacts),
                "artifacts_expected": len(outcome.checkpoints),
                "missing": [],
            },
            "usage": None,
            "tool_calls": ledger.tool_call_records(),
            "workspace": None,
            "processes": [],
            "workflow_evidence": evidence,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        # 先按契约规范化再算 hash：FrozenObservation 的 dump 会补齐默认字段
        # （redacted / truncated / recorded_at 等），对未规范化的字典取 hash 会
        # 与评分端的重算结果不一致。
        payload["evidence_hash"] = "sha256:" + "0" * 64  # 占位：下面按规范化结果重算
        canonical = FrozenObservation.model_validate(payload).model_dump(mode="json")
        canonical["evidence_hash"] = observation_evidence_hash(canonical)
        frozen = FrozenObservation.model_validate(canonical).model_dump(mode="json")
        observation = {
            "schema": "workflow-observation@1",
            "workflow_ref": compiled.ref,
            "workflow_content_hash": compiled.content_hash,
            "status": outcome.status,
            "reason": outcome.reason,
            "steps": [item.as_dict() for item in outcome.steps],
            "checkpoints": {
                step_id: {"content_hash": item.get("content_hash"),
                          "label": item.get("label")}
                for step_id, item in sorted(outcome.checkpoints.items())
            },
            "final_state": outcome.state,
            "final_output": outcome.final_output,
            "turns": outcome.turns,
            "step_count": outcome.step_count,
            "tool_calls": outcome.tool_calls,
            "needs_review": outcome.needs_review,
            "workflow_content_digest": workflow_content_hash(compiled.snapshot),
            "observation_id": frozen["observation_id"],
            "evidence_hash": frozen["evidence_hash"],
        }
        return observation, frozen, artifacts

    def _primary_fixture_id(self) -> str | None:
        snapshots = self.manifest.get("fixture_snapshot") or {}
        if not isinstance(snapshots, Mapping) or not snapshots:
            return None
        key, record = sorted(snapshots.items())[0]
        if isinstance(record, Mapping) and record.get("fixture_id"):
            return str(record["fixture_id"])
        return str(key).partition("@")[0]

    def _action_log_evidence(
        self,
        run_id: str,
        case_id: str,
        attempt_id: str,
        fixture_id: str | None,
        ledger: ScenarioLedger,
        complete: bool,
        store: Any,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """有序业务动作日志（含被拒绝的越权尝试），作为 action_log 证据冻结。"""
        data = _canonical_json(ledger.action_log())
        sha256 = hashlib.sha256(data).hexdigest()
        artifact_id = (
            "scenario/" + _safe_component(run_id) + "/" + _safe_component(case_id)
            + "/" + ACTION_LOG_EVIDENCE_ID + ".json"
        )
        if store is not None:
            store.put_bytes(artifact_id, data, kind="workflow-action-log",
                            media_type="application/json")
        artifacts.append({
            "artifact_id": artifact_id,
            "step_id": ACTION_LOG_EVIDENCE_ID,
            "label": "business actions",
            "content_hash": "sha256:" + sha256,
            "sha256": sha256,
            "size_bytes": len(data),
            "owner": {"run_id": run_id, "case_id": case_id},
            "available": store is not None,
        })
        schema = WorkflowActionLog.model_json_schema()
        return {
            "schema_version": 1,
            "evidence_id": ACTION_LOG_EVIDENCE_ID,
            "evidence_kind": "action_log",
            "owner": {
                "run_id": run_id, "case_id": case_id, "attempt_id": attempt_id,
                "fixture_id": fixture_id,
            },
            "step": max(1, int(ledger.step)),
            "artifact_id": artifact_id,
            "artifact_sha256": sha256,
            "media_type": "application/json",
            "schema_id": "workflow.action_log@1",
            "payload_schema": schema,
            "schema_sha256": workflow_schema_digest(schema),
            "complete": complete,
            "monitored_scope": list(ACTION_LOG_MONITORED_SCOPE),
        }

    def _artifact_store(self) -> Any:
        import os

        try:
            from motte_storage.artifacts import ArtifactStore
        except ImportError:  # pragma: no cover - storage 始终可用
            return None
        return ArtifactStore(os.environ.get("ARTIFACT_ROOT", "var/artifacts"))


__all__ = [
    "ACTION_LOG_EVIDENCE_ID",
    "ACTION_LOG_MONITORED_SCOPE",
    "FIXTURE_TOUCHING_STEP_KINDS",
    "FixtureBinding",
    "FixturePortAdapter",
    "NullFixturePort",
    "ScenarioCaseExecutor",
    "ScenarioCaseResult",
    "ScenarioLedger",
    "ScenarioToolError",
    "workflow_requires_fixture",
]
