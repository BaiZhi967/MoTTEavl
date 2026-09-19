"""BuiltinReAct：内置 ReAct agent 循环（M1 双模式版本）。

两种显式模式，不静默降级：
- ``legacy-json``：模型经 system prompt 约定以 JSON 回复
  ``{"action": "tool", "tool": <name>, "input": <arg>}`` 或
  ``{"action": "final", "answer": <value>}``。M1 起 assistant 决策完整进入
  消息历史（prompt 语义版本 builtin-react-legacy@2）。
- ``native-tool``：规范工具声明 + canonical ``tool_calls`` / ``tool_call_id``；
  一次响应的多个工具调用按声明顺序执行，重复 call_id 拒绝二次执行。

工具结果以 observation / tool 消息回灌；预算（步数 / 工具次数 / 单调时钟 /
单次调用期限 / 可观察 token 与 cost）每轮与每次调用前检查，停止原因具体化。
- 单次调用期限在运行时以线程期限强制：到期即停止循环（终止原因
  per_call_timeout）；被放弃的底层调用由 Provider 传输超时兜底。
- ``max_output_tokens`` 预算直接进入 ModelRequest（Provider 侧再约束上限）。
- ``AgentFatalError``（副作用后证据边界失败）不被当作工具错误回灌，直接中止。
``run(prompt)`` 保留 M0 兼容返回；新链路使用 ``run_agent`` 拿完整结果。
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from motte_contracts.messages import Message, ModelRequest

from .budget import ExecutionBudget
from .errors import AgentFatalError
from .native_tools import (
    TOOL_DECLARATIONS,
    parse_tool_arguments,
    validate_tool_arguments,
)
from .runtime import AgentRuntime

LEGACY_SYSTEM_PROMPT = (
    "You are a ReAct agent. Reply with a single JSON object, nothing else:\n"
    '{"action": "tool", "tool": "<name>", "input": <value>} to call a tool, or\n'
    '{"action": "final", "answer": <value>} to give the final answer.\n'
    "Available tools: {tools}"
)

NATIVE_SYSTEM_PROMPT = (
    "You are a ReAct agent working inside a sandboxed case workspace. "
    "Call the provided tools to inspect and create files. "
    "When the task is complete, reply with a plain-text final answer (no tool call)."
)

PROMPT_VERSIONS = {
    "legacy-json": "builtin-react-legacy@2",
    "native-tool": "builtin-react-native@1",
}

# 兼容入口（旧测试 / 旧调用方）：不带版本号时的默认 system prompt。
DEFAULT_SYSTEM_PROMPT = LEGACY_SYSTEM_PROMPT

LegacyComplete = Callable[[ModelRequest], dict[str, Any]]


class BuiltinReActRuntime(AgentRuntime):
    def __init__(
        self,
        complete: LegacyComplete,
        tools: dict[str, Callable[[Any], Any]] | None = None,
        *,
        max_steps: int = 8,
        system_prompt: str | None = None,
        model: str = "builtin-react",
        mode: str = "legacy-json",
        budget: ExecutionBudget | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        declared_tools: tuple[str, ...] | None = None,
    ) -> None:
        if mode not in PROMPT_VERSIONS:
            raise ValueError(f"unsupported agent mode: {mode!r}")
        self._complete = complete
        self.tools = dict(tools or {})
        self._system_prompt = system_prompt
        self.model = model
        self.mode = mode
        # 步数预算只有一份事实源：显式 budget 优先；兼容入口的 max_steps 参数
        # 在未提供 budget 时才生效（#15：不允许出现第二套步数上限）。
        if budget is not None:
            self.budget = budget
        else:
            self.budget = ExecutionBudget(max_steps=max_steps)
        self.max_steps = self.budget.max_steps
        self._event_sink = event_sink
        self._should_cancel = should_cancel
        self.declared_tools = declared_tools or tuple(sorted(self.tools))
        self.events: list[dict[str, Any]] = []
        self._legacy_call_seq = 0

    # ------------------------------------------------------------ M0 兼容入口

    def run(self, prompt: str) -> dict[str, Any]:
        outcome = self.run_agent(prompt)
        legacy_status = "completed" if outcome["termination_reason"] == "final_answer" else "budget_exceeded"
        return {"status": legacy_status, "answer": outcome["final_output"], "steps": outcome["steps"]}

    # ------------------------------------------------------------ M1 主入口

    def run_agent(self, prompt: str) -> dict[str, Any]:
        """完整执行一个 case；返回终止原因、用量与消息历史等证据。"""
        self.events = []
        context: list[Message] = [Message(role="user", content=prompt)]
        executed_call_ids: set[str] = set()
        termination_reason: str | None = None
        detail: str | None = None
        final_output: Any = None
        step = 0

        while termination_reason is None:
            step += 1
            stop = self.budget.step_allowed(step)
            if stop is not None:
                termination_reason, detail = stop, f"blocked before step {step}"
                break
            self._record("step_started", step=step)
            if self._cancelled():
                termination_reason = "cancelled"
                break

            envelope = self._call_model(context, step)
            if envelope is None:
                # _call_model 已记录具体终止原因（单次调用期限 / 证据故障）
                termination_reason = self._last_call_stop or "error"
                break
            self.budget.record_usage(envelope.get("usage"), envelope.get("cost"))
            stop = self.budget._wall_or_usage_stop()
            if stop is not None:
                self._record("model_response", step=step, finish_reason=envelope.get("finish_reason"))
                termination_reason, detail = stop, "observed after model response"
                break

            if self.mode == "native-tool":
                tool_calls = envelope.get("tool_calls") or []
                if not tool_calls:
                    final_output = envelope.get("content", "")
                    self._record("final_answer", step=step, answer=final_output)
                    termination_reason = "final_answer"
                    break
                context.append(Message(
                    role="assistant", content=envelope.get("content", ""),
                    tool_calls=[dict(call) for call in tool_calls],
                ))
                for call in tool_calls:
                    if self._cancelled():
                        termination_reason = "cancelled"
                        break
                    stop = self._run_native_tool_call(context, call, step, executed_call_ids)
                    if stop is not None:
                        termination_reason, detail = stop, "stopped during tool calls"
                        break
                if termination_reason is not None:
                    break
                continue

            # legacy-json 模式
            decision = self._parse_decision(envelope.get("content", ""))
            assistant_content = envelope.get("content", "")
            if decision is None:
                self._record(
                    "invalid_response", step=step,
                    content=str(assistant_content)[:2000],
                )
                # M1：assistant 原文完整入史（即使畸形），观察以 user 消息回灌
                context.append(Message(role="assistant", content=str(assistant_content)))
                context.append(Message(
                    role="user",
                    content="observation: invalid response; reply with a single JSON object",
                ))
                if step >= self.budget.max_steps:
                    termination_reason, detail = "max_steps", "invalid responses exhausted budget"
                continue
            context.append(Message(role="assistant", content=str(assistant_content)))
            if decision.get("action") == "final":
                final_output = decision.get("answer")
                self._record("final_answer", step=step, answer=final_output)
                termination_reason = "final_answer"
                break
            # 模型响应返回后、工具执行前再次检查取消（#3：不允许取消后仍执行写入）
            if self._cancelled():
                termination_reason = "cancelled"
                detail = "cancelled between model response and tool execution"
                break
            stop = self._run_legacy_tool(context, decision, step)
            if stop is not None:
                termination_reason, detail = stop, "stopped during tool call"
                break
            if step >= self.budget.max_steps:
                termination_reason, detail = "max_steps", "budget exhausted"
                break

        if termination_reason is None:  # pragma: no cover - 循环内必置位
            termination_reason = "max_steps"
        self._record(
            "terminated", reason=termination_reason, detail=detail, steps=step,
        )
        return {
            "final_output": final_output,
            "termination_reason": termination_reason,
            "termination_detail": detail,
            "steps": step,
            "tool_calls": self.budget.tool_calls_made(),
            "events": list(self.events),
            "messages": [self._message_view(message) for message in context],
            "usage": self.budget.observed_usage(),
            "budget": self.budget.enforcement_report(),
            "prompt_version": PROMPT_VERSIONS[self.mode],
        }

    # ---------------------------------------------------------------- 内部

    def _call_model(self, context: list[Message], step: int) -> dict[str, Any] | None:
        request = ModelRequest(
            model=self.model,
            messages=list(context),
            system=self._build_system_prompt(),
            tools=[TOOL_DECLARATIONS[name] for name in self.declared_tools
                   if name in TOOL_DECLARATIONS] if self.mode == "native-tool" else [],
            # 输出预算进入请求（#4）；Provider 侧 ceiling 校验兜底
            **({"max_output_tokens": self.budget.max_output_tokens}
               if self.budget.max_output_tokens is not None else {}),
        )
        self._record("model_request", step=step,
                     messages=len(context), tools=len(request.tools),
                     **({"max_output_tokens": self.budget.max_output_tokens}
                        if self.budget.max_output_tokens is not None else {}))
        envelope = self._dispatch_model_call(request, step)
        if envelope is None:
            return None  # 终止原因已记录（per_call_timeout）
        self._record("model_response", step=step,
                     finish_reason=envelope.get("finish_reason"),
                     usage=envelope.get("usage") or None)
        self._last_call_stop = None
        return envelope

    def _dispatch_model_call(self, request: ModelRequest, step: int) -> dict[str, Any] | None:
        """执行一次模型调用；配置了单次调用期限时用线程期限强制（#4）。

        到期后循环立即停止（终止原因 per_call_timeout）；被放弃的底层调用由
        Provider 传输超时兜底回收，其结果被丢弃。
        """
        deadline = self.budget.per_call_deadline()
        if deadline is None:
            return self._complete(request)
        import time as _time

        box: dict[str, Any] = {}

        def runner() -> None:
            try:
                box["envelope"] = self._complete(request)
            except BaseException as error:  # noqa: BLE001 - 线程边界内原样传递
                box["error"] = error

        thread = threading.Thread(target=runner, daemon=True, name="agent-model-call")
        thread.start()
        remaining = deadline - _time.monotonic()
        thread.join(timeout=max(0.0, remaining))
        if thread.is_alive():
            self._record("model_call_timeout", step=step,
                         timeout_sec=self.budget.per_call_timeout_sec)
            self._last_call_stop = "per_call_timeout"
            self.budget.per_call_timeout_enforced = True
            return None
        if "error" in box:
            raise box["error"]
        self.budget.per_call_timeout_enforced = True
        return box.get("envelope")

    def _run_native_tool_call(
        self, context: list[Message], call: dict[str, Any], step: int,
        executed_call_ids: set[str],
    ) -> str | None:
        call_id = str(call.get("id") or "")
        name = str(call.get("name") or "")
        arguments, parse_error = parse_tool_arguments(call.get("arguments"))
        if not call_id:
            self._record("tool_denied", step=step, tool=name or "(none)",
                         reason="missing_call_id")
            context.append(Message(role="tool", content="tool denied: missing call_id",
                                   tool_call_id=call_id or None))
            return None
        if call_id in executed_call_ids:
            # 重复 call_id：不再执行，直接回灌拒绝（无二次副作用）
            self._record("tool_denied", step=step, tool=name, call_id=call_id,
                         reason="duplicate_call_id")
            context.append(Message(
                role="tool", content=f"tool denied: duplicate call_id {call_id}",
                tool_call_id=call_id,
            ))
            return None
        executed_call_ids.add(call_id)
        if parse_error or arguments is None:
            self._record("tool_denied", step=step, tool=name, call_id=call_id,
                         reason="invalid_arguments", error=parse_error)
            context.append(Message(
                role="tool", content=f"tool arguments rejected: {parse_error}",
                tool_call_id=call_id,
            ))
            return None
        stop = self.budget.tool_call_allowed()
        if stop is not None:
            return stop
        if name not in self.tools:
            self._record("tool_denied", step=step, tool=name, call_id=call_id,
                         reason="unknown_tool")
            context.append(Message(
                role="tool", content=f"tool not available: {name}", tool_call_id=call_id,
            ))
            return None
        declaration = TOOL_DECLARATIONS.get(name, {}).get("function", {})
        schema = declaration.get("parameters") if isinstance(declaration, dict) else None
        violations = validate_tool_arguments(arguments, schema or {}) if schema else []
        if violations:
            self._record("tool_denied", step=step, tool=name, call_id=call_id,
                         reason="arguments_schema_mismatch", violations=violations)
            context.append(Message(
                role="tool", content="tool arguments rejected: " + "; ".join(violations),
                tool_call_id=call_id,
            ))
            return None
        outcome = self._execute_tool(name, arguments, call_id, step, context)
        return outcome

    def _run_legacy_tool(self, context: list[Message], decision: dict[str, Any], step: int) -> str | None:
        name = str(decision.get("tool", ""))
        handler = self.tools.get(name)
        argument = decision.get("input")
        stop = self.budget.tool_call_allowed()
        if stop is not None:
            return stop
        if handler is None:
            self._record("tool_denied", step=step, tool=name)
            context.append(Message(
                role="user", content=f"observation: tool not available: {name}"
            ))
            return None
        # legacy 模式也生成唯一 call_id（#13）：tool_call/tool_error/tool_result 匹配
        self._legacy_call_seq += 1
        call_id = f"legacy-{step}-{self._legacy_call_seq}"
        self.budget.record_tool_call()
        self._record("tool_call", step=step, tool=name, call_id=call_id, input=argument)
        try:
            result = handler(argument)
        except AgentFatalError:
            raise  # 证据边界失败不是工具失败：中止整个执行
        except Exception as error:  # 工具失败作为 observation 回灌，不终止循环
            self._record("tool_error", step=step, tool=name, call_id=call_id,
                         error=str(error))
            context.append(Message(role="user", content=f"observation: tool error: {error}"))
            return None
        self._record("tool_result", step=step, tool=name, call_id=call_id,
                     result=self._bounded_result(result))
        context.append(Message(role="user", content=f"observation: {self._bounded_result(result)}"))
        return None

    def _execute_tool(
        self, name: str, arguments: dict[str, Any], call_id: str, step: int,
        context: list[Message],
    ) -> str | None:
        self.budget.record_tool_call()
        self._record("tool_call", step=step, tool=name, call_id=call_id, arguments=arguments)
        try:
            result = self.tools[name](arguments)
        except AgentFatalError:
            raise  # 证据边界失败不是工具失败：中止整个执行
        except Exception as error:  # 工具失败回灌，不终止循环
            self._record("tool_error", step=step, tool=name, call_id=call_id, error=str(error))
            context.append(Message(
                role="tool", content=f"tool error: {error}", tool_call_id=call_id,
            ))
            return None
        text = self._bounded_result(result)
        self._record("tool_result", step=step, tool=name, call_id=call_id, result=text)
        context.append(Message(role="tool", content=text, tool_call_id=call_id))
        return None

    def _cancelled(self) -> bool:
        if self._should_cancel is None:
            return False
        try:
            return bool(self._should_cancel())
        except Exception:  # noqa: BLE001 - 取消探测失败按未取消处理
            return False

    def _build_system_prompt(self) -> str:
        if self.mode == "native-tool":
            return self._system_prompt or NATIVE_SYSTEM_PROMPT
        template = self._system_prompt or LEGACY_SYSTEM_PROMPT
        tools = ", ".join(self.declared_tools) or "(none)"
        return template.replace("{tools}", tools)

    @staticmethod
    def _bounded_result(result: Any) -> str:
        text = result if isinstance(result, str) else json.dumps(
            result, ensure_ascii=False, default=str
        )
        if len(text) > 4000:
            return text[:4000] + f"... [truncated {len(text) - 4000} chars]"
        return text

    @staticmethod
    def _message_view(message: Message) -> dict[str, Any]:
        return {
            "role": message.role,
            "content": message.content if isinstance(message.content, str)
            else str(message.content),
            **({"tool_calls": message.tool_calls} if message.tool_calls else {}),
            **({"tool_call_id": message.tool_call_id} if message.tool_call_id else {}),
        }

    @staticmethod
    def _parse_decision(content: Any) -> dict[str, Any] | None:
        if not isinstance(content, str):
            return None
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            return None
        if isinstance(value, dict) and value.get("action") in {"tool", "final"}:
            return value
        return None

    _last_call_stop: str | None = None

    def _record(self, event_type: str, **payload: Any) -> None:
        event = {"type": event_type, **payload}
        self.events.append(event)
        if self._event_sink is not None:
            try:
                self._event_sink(event)
            except Exception:  # noqa: BLE001 - 证据通道故障不阻断执行
                pass
