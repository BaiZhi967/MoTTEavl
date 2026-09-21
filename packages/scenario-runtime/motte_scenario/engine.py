"""有界 Workflow 引擎（M5-T03b / M5-G02 / M5-G05 / M5-G06）。

一次 Case 一个引擎实例：它按声明顺序执行七类步骤，共用**一份**全局预算
（步数 / turn 数 / 单调时钟期限），子层（branch、loop）不能重置预算。

关键语义：

* 每个动作**在分派前**核验剩余步数/turn/时间，并把真实剩余期限交给 Target
  与工具；不是返回后再检查耗时（M5-G06）。
* 正常 final answer 只结束**当前 turn**，session 回到可 send 状态；只有
  Workflow 完成、显式 close、取消、不可恢复错误或 Case 预算耗尽才结束。
* 失败政策 stop_case 立即停止业务；continue_for_evidence 只允许继续只读
  检查与清理，后续有副作用的动作被记为 skipped，不再次下单/改文件。全局
  `WorkflowVersion.failure_policy` 与步骤声明取并集：步骤可以额外声明继续
  取证，但不能把全局的 continue_for_evidence 降级（冻结快照里契约默认值
  `stop_case` 与显式声明不可区分，取更严格的一方）。
* 断言求值走受限条件编译器；缺失字段是明确错误（不是 false）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from .conditions import ConditionError, compile_condition, evaluate_condition

#: 综合状态：流程是否走完、是否被预算截断、是否需要人工复核。
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_BUDGET_EXCEEDED = "budget_exceeded"
STATUS_NEEDS_REVIEW = "needs_review"

STATUSES = (
    STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED,
    STATUS_BUDGET_EXCEEDED, STATUS_NEEDS_REVIEW,
)

STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

#: 产生业务副作用的步骤类型：进入 evidence-only 模式后不再执行。
SIDE_EFFECT_KINDS = frozenset({"invoke_fixture_tool", "trigger_fixture_event"})

#: 失败政策取值（与 WorkflowVersion / StepSpec 的 Literal 一致）。
FAILURE_STOP_CASE = "stop_case"
FAILURE_CONTINUE_FOR_EVIDENCE = "continue_for_evidence"


def _workflow_failure_policy(workflow: Any) -> str:
    """读取已编译 Workflow 的**全局**失败政策（冻结快照里的那一份）。"""
    version = getattr(workflow, "workflow", None)
    policy = getattr(version, "failure_policy", None)
    if policy is None:
        policy = getattr(workflow, "failure_policy", None)
    return policy if policy == FAILURE_CONTINUE_FOR_EVIDENCE else FAILURE_STOP_CASE


class EngineError(RuntimeError):
    """引擎无法继续；code 供结构化错误映射。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FixturePort(Protocol):
    """引擎需要的 fixture 能力（由 fixtures 模块实现，本模块不复制实现）。"""

    @property
    def state(self) -> Mapping[str, Any]: ...

    def snapshot(self) -> tuple[dict[str, Any], str]: ...

    def invoke_tool(self, name: str, arguments: Mapping[str, Any], mode: str) -> Any: ...

    def apply_event(self, event: str, payload: Mapping[str, Any]) -> Any: ...


class TargetPort(Protocol):
    """引擎需要的 Target 能力（一个 Case 一个实例）。"""

    def begin(self) -> dict[str, Any]: ...

    def send(self, message: str, *, deadline: float | None = None) -> dict[str, Any]: ...

    def observe(self) -> dict[str, Any]: ...

    def interrupt(self, reason: str) -> dict[str, Any]: ...

    def close(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class StepEvidence:
    """单个步骤的一次执行证据。

    容器步骤（branch/loop）按**后序**记录：只有子树跑完才能判定容器是否成功，
    因此 children 的证据序（seq）小于父步骤。depth 标明嵌套层级，未选中的
    branch 子树不产生任何证据。
    """

    seq: int
    step_id: str
    kind: str
    status: str
    detail: str | None = None
    attempts: int = 1
    assertions: tuple[dict[str, Any], ...] = ()
    duration_ms: float = 0.0
    depth: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "step_id": self.step_id,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
            "attempts": self.attempts,
            "assertions": [dict(item) for item in self.assertions],
            "duration_ms": self.duration_ms,
            "depth": self.depth,
        }


@dataclass
class WorkflowOutcome:
    status: str
    reason: str | None
    steps: tuple[StepEvidence, ...]
    checkpoints: dict[str, Any]
    step_count: int
    turns: int
    tool_calls: int
    final_output: Any = None
    state: dict[str, Any] = field(default_factory=dict)
    needs_review: bool = False
    interrupt: dict[str, Any] | None = None

    @property
    def completed(self) -> bool:
        return self.status == STATUS_COMPLETED

    def step(self, step_id: str) -> StepEvidence | None:
        for item in self.steps:
            if item.step_id == step_id:
                return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "steps": [item.as_dict() for item in self.steps],
            "checkpoints": self.checkpoints,
            "step_count": self.step_count,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "final_output": self.final_output,
            "state": self.state,
            "needs_review": self.needs_review,
            "interrupt": self.interrupt,
        }


class WorkflowEngine:
    """执行一个 CompiledWorkflow 的一次 Case 尝试。

    limits 来自已发布的 WorkflowVersion；全局 deadline 是单调时钟期限，
    一旦建立就不再延长，子层不能重置。
    """

    def __init__(
        self,
        workflow: Any,
        fixture: FixturePort,
        target: TargetPort,
        *,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        now: Callable[[], float] = time.monotonic,
        run_id: str | None = None,
        case_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        self.workflow = workflow
        self.fixture = fixture
        self.target = target
        #: 全局失败政策：步骤没有（或无法证明）显式覆盖时以它为准。
        self.failure_policy = _workflow_failure_policy(workflow)
        limits = workflow.limits
        self.max_total_steps = int(limits.max_total_steps)
        self.max_turns = int(limits.max_turns)
        self.wall_time_sec = float(limits.wall_time_sec)
        self._now = now
        self._started = now()
        self._deadline = self._started + self.wall_time_sec
        self._event_sink = event_sink
        self._cancel_check = cancel_check
        self.run_id = run_id
        self.case_id = case_id
        self.attempt_id = attempt_id
        self.steps_evidence: list[StepEvidence] = []
        self.checkpoints: dict[str, Any] = {}
        self.step_results: dict[str, Any] = {}
        self.tool_results: dict[str, Any] = {}
        self.step_count = 0
        self.turns = 0
        self.tool_calls = 0
        self._seq = 0
        self._evidence_only = False
        self._evidence_only_reason: str | None = None
        self._terminal: tuple[str, str | None] | None = None
        self._needs_review = False
        self._interrupt: dict[str, Any] | None = None

    # ------------------------------------------------------------- 预算与时钟

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._now())

    def _budget_stop(self, *, kind: str) -> tuple[str, str | None] | None:
        if self._cancelled():
            return STATUS_CANCELLED, "cancelled before step dispatch"
        if self.step_count >= self.max_total_steps:
            return STATUS_BUDGET_EXCEEDED, (
                f"max_total_steps={self.max_total_steps} exhausted before step dispatch"
            )
        if kind == "send_message" and self.turns >= self.max_turns:
            return STATUS_BUDGET_EXCEEDED, (
                f"max_turns={self.max_turns} exhausted before step dispatch"
            )
        if self.remaining_seconds() <= 0:
            return STATUS_BUDGET_EXCEEDED, (
                f"wall_time_sec={self.wall_time_sec} exhausted before step dispatch"
            )
        return None

    def _cancelled(self) -> bool:
        if self._cancel_check is None:
            return False
        try:
            return bool(self._cancel_check())
        except Exception:  # noqa: BLE001 - 取消探测失败按未取消处理
            return False

    # ---------------------------------------------------------------- 事件

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self._event_sink is None:
            return
        identity = {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "attempt_id": self.attempt_id,
            "step_seq": self._seq,
        }
        try:
            self._event_sink({"type": event_type, **identity, **payload})
        except Exception:  # noqa: BLE001 - 证据通道故障不阻断执行
            pass

    # ---------------------------------------------------------------- 上下文

    def _context(self) -> dict[str, Any]:
        return {
            "state": dict(self.fixture.state or {}),
            "tool_results": dict(self.tool_results),
            "step_results": dict(self.step_results),
        }

    def _evaluate(self, conditions: Any) -> tuple[list[dict[str, Any]], bool]:
        """求值一组断言；缺失字段是明确错误，不当作 false。"""
        outcomes: list[dict[str, Any]] = []
        satisfied = True
        for condition in conditions or ():
            compiled = compile_condition(condition)
            try:
                outcome = evaluate_condition(compiled, self._context())
            except ConditionError as error:
                outcomes.append({
                    "condition": compiled.describe(), "satisfied": False,
                    "error": error.code, "reason": str(error),
                })
                satisfied = False
                continue
            outcomes.append(outcome.as_dict())
            if not outcome.satisfied:
                satisfied = False
        return outcomes, satisfied

    # ---------------------------------------------------------------- 主循环

    def run(self) -> WorkflowOutcome:
        begin = self._begin_target()
        if begin is not None:
            return self._outcome(begin)
        try:
            self._run_sequence(list(self.workflow.top_level_steps), depth=1)
        finally:
            close = self._close_target()
        if close is not None and self._terminal is None:
            self._terminal = close
        return self._outcome(self._terminal)

    def _outcome(self, terminal: tuple[str, str | None] | None) -> WorkflowOutcome:
        if terminal is None:
            terminal = self._completion_verdict()
        status, reason = terminal
        if self._evidence_only and status == STATUS_COMPLETED:
            status, reason = STATUS_FAILED, self._evidence_only_reason
        if self._needs_review and status == STATUS_COMPLETED:
            status, reason = STATUS_NEEDS_REVIEW, reason or "stop could not be confirmed"
        return WorkflowOutcome(
            status=status,
            reason=reason,
            steps=tuple(self.steps_evidence),
            checkpoints=dict(self.checkpoints),
            step_count=self.step_count,
            turns=self.turns,
            tool_calls=self.tool_calls,
            final_output=self.step_results.get("__final_output__"),
            state=dict(self.fixture.state or {}),
            needs_review=self._needs_review or status == STATUS_NEEDS_REVIEW,
            interrupt=self._interrupt,
        )

    def _begin_target(self) -> tuple[str, str | None] | None:
        try:
            self.target.begin()
        except Exception as error:  # noqa: BLE001 - Target 起不来就是 Case 失败
            self._emit("target_begin_failed", error=f"{type(error).__name__}: {error}")
            return STATUS_FAILED, f"target begin failed: {type(error).__name__}: {error}"
        return None

    def _close_target(self) -> tuple[str, str | None] | None:
        try:
            self.target.close()
        except Exception as error:  # noqa: BLE001 - close 失败是资源未知，保留复核
            self._needs_review = True
            self._emit("target_close_failed", error=f"{type(error).__name__}: {error}")
            return STATUS_NEEDS_REVIEW, f"target close failed: {type(error).__name__}: {error}"
        return None

    def _completion_verdict(self) -> tuple[str, str | None]:
        assertions, satisfied = self._evaluate(self.workflow.completion_assertions)
        if not assertions:
            return STATUS_COMPLETED, None
        self._emit("completion_assertions", assertions=assertions)
        if satisfied:
            return STATUS_COMPLETED, None
        return STATUS_FAILED, "completion assertions failed"

    def _run_sequence(self, steps: list[Any], *, depth: int) -> None:
        for step in steps:
            if self._terminal is not None:
                return
            self._run_step(step, depth=depth)

    # ---------------------------------------------------------------- 单步

    def _run_step(self, step: Any, *, depth: int) -> None:
        stop = self._budget_stop(kind=step.kind)
        if stop is not None:
            self._terminal = stop
            self._emit("step_blocked", step_id=step.step_id, kind=step.kind, reason=stop[1])
            return
        self.step_count += 1
        self._seq += 1
        seq = self._seq
        started = self._now()
        deadline = self._step_deadline(step)
        self._emit(
            "step_start", step_id=step.step_id, kind=step.kind, depth=depth,
            remaining_sec=self.remaining_seconds(),
        )
        if self._evidence_only and self._has_side_effect(step):
            self._record(seq, step, STEP_SKIPPED, depth,
                         "evidence-only after failure: side-effecting step not executed")
            return
        assertions: list[dict[str, Any]] = []
        try:
            status, detail, assertions = self._dispatch(step, deadline=deadline, depth=depth)
        except EngineError as error:
            status, detail = STEP_FAILED, f"{error.code}: {error}"
        except ConditionError as error:
            status, detail = STEP_FAILED, f"{error.code}: {error}"
        except Exception as error:  # noqa: BLE001 - 业务动作异常如实记为失败
            # 结构化错误码必须活着进入证据：上层需要按码分流，而不是解析文本。
            code = getattr(error, "code", None)
            prefix = code if isinstance(code, str) and code else type(error).__name__
            status, detail = STEP_FAILED, f"{prefix}: {error}"
        duration_ms = round((self._now() - started) * 1000, 3)
        self._record(seq, step, status, depth, detail, assertions, duration_ms)
        self._emit(
            "step_end", step_id=step.step_id, kind=step.kind, status=status,
            detail=detail, duration_ms=duration_ms,
        )
        if status == STEP_FAILED:
            self._handle_failure(step, detail)

    def _step_deadline(self, step: Any) -> float:
        timeout = getattr(step, "timeout_sec", None)
        if timeout is None:
            return self._deadline
        return min(self._deadline, self._now() + float(timeout))

    def _has_side_effect(self, step: Any) -> bool:
        if step.kind == "send_message":
            return True
        if step.kind == "invoke_fixture_tool":
            return getattr(step, "tool_mode", "real") != "deny"
        return step.kind in SIDE_EFFECT_KINDS

    def _record(
        self, seq: int, step: Any, status: str, depth: int, detail: str | None,
        assertions: list[dict[str, Any]] | None = None, duration_ms: float = 0.0,
    ) -> None:
        self.steps_evidence.append(StepEvidence(
            seq=seq, step_id=step.step_id, kind=step.kind, status=status,
            detail=detail, assertions=tuple(assertions or ()), duration_ms=duration_ms,
            depth=depth,
        ))

    def effective_failure_policy(self, step: Any) -> str:
        """一步失败后的处理政策：步骤声明与全局声明的并集。

        步骤显式声明 continue_for_evidence 时继续取证；全局声明
        continue_for_evidence 时同样继续——契约把步骤默认值写成 stop_case，
        冻结快照无法区分"显式 stop_case"与"契约默认值"，因此不允许用步骤
        默认值悄悄取消全局政策（继续取证本身只开放只读检查，不会放大权限）。
        """
        if getattr(step, "failure_policy", None) == FAILURE_CONTINUE_FOR_EVIDENCE:
            return FAILURE_CONTINUE_FOR_EVIDENCE
        return self.failure_policy

    def _handle_failure(self, step: Any, detail: str | None) -> None:
        if self._terminal is not None:
            # 分派内部已经给出了更精确的终局（例如停止未确认 → needs_review），
            # 通用失败路径不得把它降级成普通失败。
            return
        if self.effective_failure_policy(step) == FAILURE_CONTINUE_FOR_EVIDENCE:
            if not self._evidence_only:
                self._evidence_only = True
                self._evidence_only_reason = (
                    f"step {step.step_id!r} failed and only read-only evidence may continue"
                )
            return
        self._terminal = (STATUS_FAILED, detail or f"step {step.step_id!r} failed")

    # ---------------------------------------------------------------- 分派

    def _dispatch(
        self, step: Any, *, deadline: float, depth: int,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        kind = step.kind
        if kind == "send_message":
            return self._dispatch_send(step, deadline)
        if kind == "invoke_fixture_tool":
            return self._dispatch_tool(step, deadline)
        if kind == "assert":
            assertions, satisfied = self._evaluate(step.assertions)
            return (
                STEP_SUCCEEDED if satisfied else STEP_FAILED,
                None if satisfied else "assertion failed",
                assertions,
            )
        if kind == "checkpoint":
            return self._dispatch_checkpoint(step)
        if kind == "branch":
            return self._dispatch_branch(step, depth)
        if kind == "loop":
            return self._dispatch_loop(step, depth)
        if kind == "trigger_fixture_event":
            return self._dispatch_event(step, deadline)
        raise EngineError("WORKFLOW_STEP_UNSUPPORTED", f"unsupported step kind {kind!r}")

    def _dispatch_send(
        self, step: Any, deadline: float,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        message = step.message
        reference = getattr(step, "input_ref", None)
        if reference is not None:
            produced = self.step_results.get(reference)
            if produced is None:
                raise EngineError(
                    "WORKFLOW_INPUT_MISSING",
                    f"step {step.step_id!r} references {reference!r} which produced no output",
                )
            text = produced.get("output") if isinstance(produced, dict) else produced
            if isinstance(text, str) and text:
                message = message + "\n" + text
        self.turns += 1
        self._emit("target_send", step_id=step.step_id, turn=self.turns,
                   remaining_sec=self.remaining_seconds())
        result = self.target.send(message, deadline=deadline)
        if not isinstance(result, dict):
            raise EngineError(
                "WORKFLOW_TARGET_RESPONSE_INVALID",
                f"target send returned {type(result).__name__}, expected an object",
            )
        self.step_results[step.step_id] = {
            "status": "sent",
            "output": result.get("output"),
            "termination_reason": result.get("termination_reason"),
        }
        self.step_results["__final_output__"] = result.get("output")
        reason = result.get("termination_reason")
        if reason == "per_call_timeout" or result.get("timeout") is True:
            return self._timeout_outcome(step)
        # TargetPort 没有自带期限强制时（或实现忽略了 deadline），返回后同样
        # 复核期限：越过期限的一轮不能算成功（F05）。
        overrun = self._deadline_overrun(step, deadline)
        if overrun is not None:
            return STEP_FAILED, overrun, []
        if reason in ("max_steps", "max_tool_calls", "wall_time", "token_limit", "cost_limit"):
            return STEP_FAILED, f"target budget stop: {reason}", []
        if reason == "cancelled":
            self._terminal = (STATUS_CANCELLED, "target reported cancellation")
            return STEP_FAILED, "target reported cancellation", []
        if reason == "error":
            return STEP_FAILED, f"target error: {result.get('detail') or 'unknown'}", []
        assertions, satisfied = self._evaluate(step.assertions)
        if not satisfied:
            return STEP_FAILED, "assertion failed after send", assertions
        return STEP_SUCCEEDED, None, assertions

    def _timeout_outcome(
        self, step: Any,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        """超时后必须确认副作用已停止；无法确认则保留资源并转复核。"""
        outcome = self._interrupt_target(f"step_timeout:{step.step_id}")
        if not bool(outcome.get("confirmed")):
            self._needs_review = True
            self._terminal = (
                STATUS_NEEDS_REVIEW,
                f"step {step.step_id!r} timed out and the target could not confirm it stopped",
            )
        else:
            self._terminal = (
                STATUS_FAILED, f"step {step.step_id!r} exceeded its deadline and was stopped",
            )
        return STEP_FAILED, f"step {step.step_id!r} deadline exceeded", []

    def _interrupt_target(self, reason: str) -> dict[str, Any]:
        try:
            outcome = self.target.interrupt(reason)
        except Exception as error:  # noqa: BLE001 - 中断失败即停止未确认
            outcome = {"confirmed": False, "error": f"{type(error).__name__}: {error}"}
        if not isinstance(outcome, dict):
            outcome = {"confirmed": False, "detail": str(outcome)}
        self._interrupt = {"reason": reason, **outcome}
        self._emit("target_interrupt", reason=reason, outcome=outcome)
        return self._interrupt

    def _dispatch_tool(
        self, step: Any, deadline: float,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        """受控工具的分派边界：分派前核验期限，返回后复核期限（F05）。

        工具是进程内调用，无法中途抢断；因此"返回后复核"不是用来替代期限，
        而是保证**越过期限的副作用不会被记成成功**：动作确实发生了，如实写入
        工具结果，但这一步失败，Workflow 不可能 completed。
        """
        if self._now() >= deadline:
            self._emit("step_deadline_expired", step_id=step.step_id, kind=step.kind,
                       phase="pre_dispatch", remaining_sec=self.remaining_seconds())
            return self._timeout_outcome(step)
        self.tool_calls += 1
        self._emit("fixture_tool_call", step_id=step.step_id, tool=step.tool,
                   mode=step.tool_mode, remaining_sec=self.remaining_seconds())
        result = self.fixture.invoke_tool(step.tool, step.arguments, step.tool_mode)
        self.tool_results[step.step_id] = result
        self.step_results[step.step_id] = {"status": "succeeded", "result": result}
        overrun = self._deadline_overrun(step, deadline)
        if overrun is not None:
            return STEP_FAILED, overrun, []
        assertions, satisfied = self._evaluate(step.assertions)
        if not satisfied:
            return STEP_FAILED, "assertion failed after fixture tool", assertions
        return STEP_SUCCEEDED, None, assertions

    def _deadline_overrun(self, step: Any, deadline: float) -> str | None:
        """动作返回后复核期限；越过期限返回具名失败原因，否则 None。

        全局 wall_time 同时耗尽时终局是 budget_exceeded（与分派前的预算检查
        同一语义）；只有步骤自己的期限被越过时，终局由失败政策决定。
        """
        overrun_sec = self._now() - deadline
        if overrun_sec <= 0:
            return None
        self._emit("step_deadline_exceeded", step_id=step.step_id, kind=step.kind,
                   phase="post_action", overrun_sec=round(overrun_sec, 6))
        if self._now() >= self._deadline:
            self._terminal = (
                STATUS_BUDGET_EXCEEDED,
                f"wall_time_sec={self.wall_time_sec} exhausted during step "
                f"{step.step_id!r}",
            )
            return self._terminal[1]
        return (
            f"step {step.step_id!r} exceeded its deadline "
            f"({round(overrun_sec, 6)}s over); the workflow must not report success"
        )

    def _dispatch_checkpoint(
        self, step: Any,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        snapshot, content_hash = self.fixture.snapshot()
        payload = {
            "step_id": step.step_id,
            "label": getattr(step, "label", None),
            "content_hash": content_hash,
            "state": snapshot,
        }
        self.checkpoints[step.step_id] = payload
        self.step_results[step.step_id] = {
            "status": "succeeded", "content_hash": content_hash,
        }
        self._emit("checkpoint", step_id=step.step_id, content_hash=content_hash,
                   label=getattr(step, "label", None))
        assertions, satisfied = self._evaluate(step.assertions)
        if not satisfied:
            return STEP_FAILED, "checkpoint assertion failed", assertions
        return STEP_SUCCEEDED, None, assertions

    def _dispatch_branch(
        self, step: Any, depth: int,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        compiled = compile_condition(step.when)
        decision = evaluate_condition(compiled, self._context())
        chosen = list(step.then_steps) if decision.satisfied else list(step.else_steps)
        self._emit("branch_decision", step_id=step.step_id,
                   condition=compiled.describe(), taken=decision.satisfied)
        self._run_sequence(chosen, depth=depth + 1)
        if self._terminal is not None:
            return STEP_FAILED, self._terminal[1], [decision.as_dict()]
        return STEP_SUCCEEDED, None, [decision.as_dict()]

    def _dispatch_loop(
        self, step: Any, depth: int,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        outcome_records: list[dict[str, Any]] = []
        for index in range(int(step.max_iterations)):
            if self._terminal is not None:
                break
            if step.until is not None:
                until = evaluate_condition(compile_condition(step.until), self._context())
                if until.satisfied:
                    outcome_records.append({"iteration": index, "until": until.as_dict()})
                    break
            if self.step_count >= self.max_total_steps:
                self._terminal = (
                    STATUS_BUDGET_EXCEEDED,
                    f"max_total_steps={self.max_total_steps} exhausted inside loop {step.step_id!r}",
                )
                break
            self._emit("loop_iteration", step_id=step.step_id, iteration=index + 1)
            self._run_sequence(list(step.body), depth=depth + 1)
            outcome_records.append({"iteration": index + 1})
        if self._terminal is not None:
            return STEP_FAILED, self._terminal[1], outcome_records
        return STEP_SUCCEEDED, None, outcome_records

    def _dispatch_event(
        self, step: Any, deadline: float,
    ) -> tuple[str, str | None, list[dict[str, Any]]]:
        if self._now() >= deadline:
            self._emit("step_deadline_expired", step_id=step.step_id, kind=step.kind,
                       phase="pre_dispatch", remaining_sec=self.remaining_seconds())
            return self._timeout_outcome(step)
        result = self.fixture.apply_event(step.event, step.payload)
        self.step_results[step.step_id] = {"status": "succeeded", "event": step.event}
        visible = result if isinstance(result, (str, int, float, bool, type(None))) else None
        self._emit("fixture_event", step_id=step.step_id, event=step.event, result=visible)
        overrun = self._deadline_overrun(step, deadline)
        if overrun is not None:
            return STEP_FAILED, overrun, []
        assertions, satisfied = self._evaluate(step.assertions)
        if not satisfied:
            return STEP_FAILED, "assertion failed after fixture event", assertions
        return STEP_SUCCEEDED, None, assertions
