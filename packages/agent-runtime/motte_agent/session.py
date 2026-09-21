"""Case 级 Builtin ReAct 会话状态容器（M5-T03a）。

``BuiltinSession`` 是**一个 Case 的会话状态唯一所有者**：消息历史、已执行
工具 call_id 集合、``ExecutionBudget`` 实例、事件序号与累计步数/轮数。运行时
自身不再保存这些状态——同一会话内的多次 ``send`` 读写的是同一个容器，因此
多轮业务 Target 能在 Case 内保留上下文；而两个会话之间没有任何共享可变状态
（所有容器字段一律 ``default_factory``，不存在跨会话共享的默认值）。

生命周期：

- 运行时 ``begin()`` 建立一个会话（每个运行时只允许一次），状态 ``active``；
- 一个 turn 以正常 final answer 结束时会话回到可 ``send`` 状态；
- 任何其它终止原因（max_steps / max_tool_calls / wall_time / token_limit /
  cost_limit / cancelled / per_call_timeout / error）把会话置为 ``terminated``，
  此后 ``send`` 被拒绝；预算在容器内累计，重新进入循环不会重新武装；
- ``close()`` 幂等；关闭后 ``send`` 被拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from motte_contracts.messages import Message

from .budget import ExecutionBudget

# ---------------------------------------------------------------- 会话状态

STATE_IDLE = "idle"
STATE_ACTIVE = "active"
STATE_TERMINATED = "terminated"
STATE_CLOSED = "closed"

SESSION_STATES = (STATE_IDLE, STATE_ACTIVE, STATE_TERMINATED, STATE_CLOSED)

FINAL_ANSWER = "final_answer"

# 终止原因词表与既有 Builtin loop 完全一致：只有 final_answer 结束当前 turn；
# 其余原因结束整个 Session（Case 预算耗尽 / 取消 / 不可恢复错误）。
TERMINAL_REASONS = (
    "max_steps", "max_tool_calls", "wall_time", "token_limit",
    "cost_limit", "cancelled", "per_call_timeout", "error",
)
TERMINATION_REASONS = (FINAL_ANSWER, *TERMINAL_REASONS)


class SessionStateError(RuntimeError):
    """会话状态不允许当前操作（未 begin / 已关闭 / 已终止）。"""

    code = "AGENT_SESSION_STATE"

    def __init__(
        self,
        message: str,
        *,
        state: str = STATE_IDLE,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.reason = reason


def message_view(message: Message) -> dict[str, Any]:
    """消息证据视图；与既有 ``run_agent`` 输出的字段逐字段一致。"""
    return {
        "role": message.role,
        "content": message.content if isinstance(message.content, str)
        else str(message.content),
        **({"tool_calls": message.tool_calls} if message.tool_calls else {}),
        **({"tool_call_id": message.tool_call_id} if message.tool_call_id else {}),
    }


@dataclass
class BuiltinSession:
    """一个 Case 独占的 Builtin ReAct 会话状态容器（运行时经它读写）。"""

    budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    session_id: str = field(default_factory=lambda: uuid4().hex)
    messages: list[Message] = field(default_factory=list)
    executed_call_ids: set[str] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)
    event_seq: int = 0
    steps: int = 0
    turns: int = 0
    legacy_call_seq: int = 0
    state: str = STATE_IDLE
    begun: bool = False
    closed: bool = False
    cancelled: bool = False
    termination_reason: str | None = None
    termination_detail: str | None = None
    final_output: Any = None

    # ------------------------------------------------------------ 生命周期

    def activate(self) -> None:
        """标记会话已建立（只由运行时 ``begin()`` 调用）。"""
        if self.begun:
            raise SessionStateError(
                f"session already begun: {self.session_id}",
                state=self.state, reason=self.termination_reason,
            )
        self.begun = True
        self.state = STATE_ACTIVE

    def send_refusal(self) -> str | None:
        """当前状态是否拒绝 ``send``；返回人类可读原因或 None（可发送）。"""
        if not self.begun:
            return "session has not begun; call begin() before send()"
        if self.cancelled:
            return "session was interrupted; send() is refused"
        if self.closed:
            return "session is closed; send() is refused"
        if self.state != STATE_ACTIVE:
            return (
                f"session is terminal ({self.termination_reason or self.state}); "
                "send() is refused"
            )
        return None

    def cancel(self) -> None:
        """取消会话：不再执行任何待办业务动作（含写入工具）。"""
        self.cancelled = True
        if self.termination_reason is None:
            self.termination_reason = "cancelled"
        if not self.closed:
            self.state = STATE_TERMINATED

    def close(self) -> bool:
        """关闭会话；幂等，返回本次调用是否真的关闭。"""
        if self.closed:
            return False
        self.closed = True
        if self.state != STATE_TERMINATED:
            self.state = STATE_CLOSED
        return True

    def record_termination(
        self, reason: str, detail: str | None, final_output: Any,
    ) -> None:
        """记录一个 turn 的终止；只有 final answer 保留会话可 ``send``。"""
        self.termination_reason = reason
        self.termination_detail = detail
        self.final_output = final_output
        if self.closed:
            return
        if reason == FINAL_ANSWER and not self.cancelled:
            self.state = STATE_ACTIVE
        else:
            self.state = STATE_TERMINATED

    def mark_failed(self, error: BaseException) -> None:
        """不可恢复错误（含证据边界失败）：结束会话但不伪造终止事件。"""
        self.termination_reason = "error"
        self.termination_detail = f"{type(error).__name__}: {error}"
        if not self.closed:
            self.state = STATE_TERMINATED

    # ------------------------------------------------------------ 状态写入

    def record_event(self, event_type: str, **payload: Any) -> dict[str, Any]:
        """记录一个运行事件并递增事件序号（事件载荷保持既有形状）。"""
        self.event_seq += 1
        event = {"type": event_type, **payload}
        self.events.append(event)
        return event

    def note_legacy_call(self, step: int) -> str:
        """生成 legacy 模式的唯一 call_id（跨 turn 单调，不重复）。"""
        self.legacy_call_seq += 1
        return f"legacy-{step}-{self.legacy_call_seq}"

    # ------------------------------------------------------------ 证据视图

    def message_views(self) -> list[dict[str, Any]]:
        return [message_view(message) for message in self.messages]

    def view(self) -> dict[str, Any]:
        """会话状态快照（不含事件与消息正文，见运行时 ``observe``）。"""
        return {
            "session_id": self.session_id,
            "state": self.state,
            "begun": self.begun,
            "closed": self.closed,
            "cancelled": self.cancelled,
            "turns": self.turns,
            "steps": self.steps,
            "tool_calls": self.budget.tool_calls_made(),
            "event_seq": self.event_seq,
            "message_count": len(self.messages),
            "executed_call_ids": sorted(self.executed_call_ids),
            "termination_reason": self.termination_reason,
            "termination_detail": self.termination_detail,
            "final_output": self.final_output,
        }
