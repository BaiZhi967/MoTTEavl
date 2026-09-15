"""BuiltinReAct：内置 ReAct agent 循环。

协议：模型经 system prompt 约定以 JSON 回复
  {"action": "tool", "tool": <name>, "input": <arg>} 或
  {"action": "final", "answer": <value>}
工具结果以 observation 回灌；步数预算用尽返回 budget_exceeded。
全程记录事件（step_started/tool_call/tool_error/final_answer/budget_exceeded）。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from motte_contracts.messages import Message, ModelRequest

from .runtime import AgentRuntime

DEFAULT_SYSTEM_PROMPT = (
    "You are a ReAct agent. Reply with a single JSON object, nothing else:\n"
    '{"action": "tool", "tool": "<name>", "input": <value>} to call a tool, or\n'
    '{"action": "final", "answer": <value>} to give the final answer.\n'
    "Available tools: {tools}"
)


class BuiltinReActRuntime(AgentRuntime):
    def __init__(
        self,
        complete: Callable[[ModelRequest], dict[str, Any]],
        tools: dict[str, Callable[[Any], Any]] | None = None,
        *,
        max_steps: int = 8,
        system_prompt: str | None = None,
    ) -> None:
        self._complete = complete
        self.tools = dict(tools or {})
        self.max_steps = max_steps
        self._system_prompt = system_prompt
        self.events: list[dict[str, Any]] = []

    def run(self, prompt: str) -> dict[str, Any]:
        self.events = []
        context = [Message(role="user", content=prompt)]
        for step in range(1, self.max_steps + 1):
            self._record("step_started", step=step)
            envelope = self._complete(
                ModelRequest(model="builtin-react", messages=list(context), system=self._build_system_prompt())
            )
            decision = self._parse_decision(envelope.get("content", ""))
            if decision is None:
                self._record("invalid_response", step=step, content=envelope.get("content", ""))
                context.append(
                    Message(role="user", content="observation: invalid response; reply with a single JSON object")
                )
                continue
            if decision.get("action") == "final":
                self._record("final_answer", step=step, answer=decision.get("answer"))
                return {"status": "completed", "answer": decision.get("answer"), "steps": step}
            observation = self._run_tool(decision, step)
            context.append(Message(role="user", content=f"observation: {observation}"))
        self._record("budget_exceeded", steps=self.max_steps)
        return {"status": "budget_exceeded", "answer": None, "steps": self.max_steps}

    # ------------------------------------------------------------------ 内部

    def _build_system_prompt(self) -> str:
        template = self._system_prompt or DEFAULT_SYSTEM_PROMPT
        tools = ", ".join(sorted(self.tools)) or "(none)"
        return template.replace("{tools}", tools)

    def _run_tool(self, decision: dict[str, Any], step: int) -> str:
        name = str(decision.get("tool", ""))
        handler = self.tools.get(name)
        argument = decision.get("input")
        if handler is None:
            self._record("tool_denied", step=step, tool=name)
            return f"tool not available: {name}"
        self._record("tool_call", step=step, tool=name, input=argument)
        try:
            result = handler(argument)
        except Exception as error:  # 工具失败作为 observation 回灌，不终止循环
            self._record("tool_error", step=step, tool=name, error=str(error))
            return f"tool error: {error}"
        self._record("tool_result", step=step, tool=name, result=result)
        return result

    @staticmethod
    def _parse_decision(content: str) -> dict[str, Any] | None:
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            return None
        if isinstance(value, dict) and value.get("action") in {"tool", "final"}:
            return value
        return None

    def _record(self, event_type: str, **payload: Any) -> None:
        self.events.append({"type": event_type, **payload})
