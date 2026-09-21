"""builtin-agent 的 Scenario Target adapter（M5-T05）。

把 M5-T03a 的 case-owned Builtin 会话适配成 TargetSession：一次 Case 一个
运行时实例，messages / 已执行 call_id / 预算跨 send 保留。工具不经文件系统
workspace，而是走执行器提供的**受控工具桥**，因此权限与可见投影都由 fixture
一侧决定，Skill/目标无法自扩权限。
"""
from __future__ import annotations

from typing import Any, Mapping


def _budget_config(manifest: Mapping[str, Any]) -> dict[str, Any]:
    config = dict((manifest.get("agent_config") or {}).get("budget") or {})
    budget = manifest.get("budget")
    if isinstance(budget, Mapping):
        for key, value in budget.items():
            if key in {"wall_time_sec", "max_steps", "max_tool_calls"} and key not in config:
                config[key] = value
    return config


class BuiltinTargetSession:
    """TargetPort 实现：begin / send / observe / interrupt / close。"""

    def __init__(self, manifest: Mapping[str, Any], tools: Mapping[str, Any]) -> None:
        import time

        from motte_agent.builtin_react import BuiltinReActRuntime
        from motte_agent.budget import ExecutionBudget
        from motte_sdk.agent_backend import build_agent_provider

        self._time = time
        self.manifest = dict(manifest)
        config = dict(manifest.get("agent_config") or {})
        self.mode = config.get("mode", "legacy-json")
        provider = build_agent_provider(dict(manifest))
        self._runtime = BuiltinReActRuntime(
            provider.provider.complete,
            dict(tools),
            model=(manifest.get("provider") or {}).get("model") or "scenario-target",
            mode=self.mode,
            budget=ExecutionBudget.from_config(_budget_config(manifest)),
        )

    def begin(self) -> dict[str, Any]:
        session = self._runtime.begin()
        return {"state": session.state, "session_id": session.session_id}

    def send(self, message: str, *, deadline: float | None = None) -> dict[str, Any]:
        started = self._time.monotonic()
        outcome = self._runtime.send(message)
        overshoot = deadline is not None and self._time.monotonic() > deadline
        if overshoot and outcome.get("termination_reason") == "final_answer":
            # 一步跨越了期限：先真正中断，再如实报告本轮超时。
            stop = self.interrupt("send_deadline")
            return {
                "output": None, "termination_reason": "per_call_timeout",
                "timeout": True, "stopped": stop["confirmed"], "interrupt": stop,
                "elapsed_ms": round((self._time.monotonic() - started) * 1000, 3),
            }
        return {
            "output": outcome.get("final_output"),
            "termination_reason": outcome.get("termination_reason"),
            "detail": outcome.get("termination_detail"),
            "turn": outcome.get("turn"),
        }

    def observe(self) -> dict[str, Any]:
        observed = self._runtime.observe()
        return {
            "session": observed.get("session"),
            "turns": (observed.get("session") or {}).get("turns"),
            "budget": observed.get("budget"),
        }

    def interrupt(self, reason: str) -> dict[str, Any]:
        self._runtime.interrupt()
        observed = self.observe()
        state = (observed.get("session") or {}).get("state")
        return {
            "reason": reason, "confirmed": state in {"terminated", "closed"},
            "state": state,
        }

    def close(self) -> dict[str, Any]:
        self._runtime.close()
        return {"state": (self.observe().get("session") or {}).get("state")}


def open_builtin_session(context: Mapping[str, Any]) -> BuiltinTargetSession:
    return BuiltinTargetSession(context["manifest"], context.get("tools") or {})


def builtin_agent_capabilities(_manifest: Mapping[str, Any]) -> Any:
    from motte_scenario.targets import TargetCapabilities

    return TargetCapabilities(
        kind="builtin-agent",
        multi_turn=True,
        # 平台侧的 fixture 工具桥实现四种 mode；目标自身的调用只走 real。
        tool_modes=("real", "mock", "replay", "deny"),
        tools=(),
        interrupt=True,
        # T07 未交付：不做 Skill 注入声明。
        skill_injection=False,
        evidence=("events", "invocations", "artifacts", "usage"),
    )


def install_builtin_target_adapter() -> None:
    from motte_scenario.targets import TargetAdapter, register_target_adapter

    register_target_adapter(
        TargetAdapter(
            kind="builtin-agent",
            capabilities=builtin_agent_capabilities,
            open_session=open_builtin_session,
        ),
        replace=True,
    )


install_builtin_target_adapter()
