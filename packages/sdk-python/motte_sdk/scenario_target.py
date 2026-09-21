"""builtin-agent 的 Scenario Target adapter（M5-T05）。

把 M5-T03a 的 case-owned Builtin 会话适配成 TargetSession：一次 Case 一个
运行时实例，messages / 已执行 call_id / 预算跨 send 保留。工具不经文件系统
workspace，而是走执行器提供的**受控工具桥**，因此权限与可见投影都由 fixture
一侧决定，Skill/目标无法自扩权限。
"""
from __future__ import annotations

from typing import Any, Mapping


class SkillInjectionError(RuntimeError):
    """冻结的 Skill 注入声明不可用：具名拒绝，绝不静默丢掉 Skill 内容。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.error_class = code


def skill_injection_declaration(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """Run 冻结快照里的注入声明；没有就返回 None（无 Skill 的普通 Run）。"""
    from motte_skill.injection import SKILL_INJECTION_SNAPSHOT_KEY

    snapshots = (manifest or {}).get("resource_snapshots")
    if not isinstance(snapshots, Mapping):
        return None
    declaration = snapshots.get(SKILL_INJECTION_SNAPSHOT_KEY)
    return dict(declaration) if isinstance(declaration, Mapping) else None


def compose_declared_system_prompt(
    declaration: Mapping[str, Any] | None, *, mode: str = "legacy-json",
) -> str | None:
    """把冻结声明渲染成实际送入模型的 system prompt（顺序与 hash 都来自声明）。"""
    from motte_agent.builtin_react import LEGACY_SYSTEM_PROMPT, NATIVE_SYSTEM_PROMPT
    from motte_skill.injection import InjectionError, compose_agent_system_prompt

    if declaration is None:
        return None
    base = NATIVE_SYSTEM_PROMPT if mode == "native-tool" else LEGACY_SYSTEM_PROMPT
    try:
        return compose_agent_system_prompt(declaration, base_prompt=base)
    except InjectionError as error:
        raise SkillInjectionError(error.code, str(error)) from error


def _budget_config(manifest: Mapping[str, Any]) -> dict[str, Any]:
    config = dict((manifest.get("agent_config") or {}).get("budget") or {})
    budget = manifest.get("budget")
    if isinstance(budget, Mapping):
        for key, value in budget.items():
            if key in {"wall_time_sec", "max_steps", "max_tool_calls"} and key not in config:
                config[key] = value
    return config


class BuiltinTargetSession:
    """TargetPort 实现：begin / send / observe / interrupt / close。

    on_event 把运行时的模型/工具调用边界事件交给执行器的调用账本（R6）：目标
    自己看到的事件与平台侧证据是同一份事实，不在两处各记一套。
    """

    def __init__(
        self, manifest: Mapping[str, Any], tools: Mapping[str, Any],
        *, on_event: Any = None,
    ) -> None:
        import time

        from motte_agent.builtin_react import BuiltinReActRuntime
        from motte_agent.budget import ExecutionBudget
        from motte_sdk.agent_backend import build_agent_provider

        self._time = time
        self.manifest = dict(manifest)
        config = dict(manifest.get("agent_config") or {})
        self.mode = config.get("mode", "legacy-json")
        # M5-T07：声明的 Skill 必须真的有冻结注入声明；只写 refs 却拿不到声明
        # 是创建期错误，不能在这里静默降级成"没有 Skill"。
        declaration = skill_injection_declaration(manifest)
        if manifest.get("skills") and declaration is None:
            raise SkillInjectionError(
                "SCENARIO_SKILL_INJECTION_MISSING",
                "manifest declares skills but carries no frozen skill injection declaration; "
                "the Run must be created through the resolved manifest path",
            )
        self.skill_injection = declaration
        system_prompt = compose_declared_system_prompt(declaration, mode=self.mode)
        provider = build_agent_provider(dict(manifest))
        self._runtime = BuiltinReActRuntime(
            provider.provider.complete,
            dict(tools),
            system_prompt=system_prompt,
            model=(manifest.get("provider") or {}).get("model") or "scenario-target",
            mode=self.mode,
            budget=ExecutionBudget.from_config(_budget_config(manifest)),
            event_sink=on_event if callable(on_event) else None,
        )

    def begin(self) -> dict[str, Any]:
        session = self._runtime.begin()
        return {"state": session.state, "session_id": session.session_id}

    def send(self, message: str, *, deadline: float | None = None) -> dict[str, Any]:
        """执行一个 turn；deadline 是单调时钟上的**绝对执行期限**。

        剩余期限直接交给运行时执行（模型调用与工具执行都在它之内），返回后再
        复核一次：期限已过或运行时报告 per_call_timeout 时，先真正中断，再
        如实报告本轮超时与停止确认结果。
        """
        started = self._time.monotonic()
        outcome = self._runtime.send(message, deadline=deadline)
        reason = outcome.get("termination_reason")
        overshoot = deadline is not None and self._time.monotonic() > deadline
        timed_out = reason == "per_call_timeout" or (
            overshoot and reason in (None, "final_answer")
        )
        if timed_out:
            stop = self.interrupt("send_deadline")
            return {
                "output": None, "termination_reason": "per_call_timeout",
                "timeout": True, "stopped": stop["confirmed"], "interrupt": stop,
                "elapsed_ms": round((self._time.monotonic() - started) * 1000, 3),
            }
        return {
            "output": outcome.get("final_output"),
            "termination_reason": reason,
            "detail": outcome.get("termination_detail"),
            "turn": outcome.get("turn"),
        }

    def observe(self) -> dict[str, Any]:
        """运行时观察快照：runtime 返回的是**扁平** session 字段（F16）。

        这里把它投影成 TargetPort 的稳定形状，不再假装存在嵌套 session。
        """
        observed = self._runtime.observe()
        return {
            "session_id": observed.get("session_id"),
            "state": observed.get("state"),
            "begun": observed.get("begun"),
            "closed": observed.get("closed"),
            "cancelled": observed.get("cancelled"),
            "turns": observed.get("turns"),
            "steps": observed.get("steps"),
            "tool_calls": observed.get("tool_calls"),
            "termination_reason": observed.get("termination_reason"),
            "termination_detail": observed.get("termination_detail"),
            "final_output": observed.get("final_output"),
            "budget": observed.get("budget"),
            "usage": observed.get("usage"),
        }

    def interrupt(self, reason: str) -> dict[str, Any]:
        """请求中断并**按事实**报告停止确认：只有已观测到 terminated/closed 才算确认。"""
        self._runtime.interrupt()
        observed = self.observe()
        state = observed.get("state")
        return {
            "reason": reason, "confirmed": state in {"terminated", "closed"},
            "state": state,
        }

    def close(self) -> dict[str, Any]:
        self._runtime.close()
        return {"state": self.observe().get("state")}


def open_builtin_session(context: Mapping[str, Any]) -> BuiltinTargetSession:
    return BuiltinTargetSession(
        context["manifest"], context.get("tools") or {},
        on_event=context.get("on_event"),
    )


def builtin_agent_capabilities(_manifest: Mapping[str, Any]) -> Any:
    from motte_scenario.targets import TargetCapabilities

    return TargetCapabilities(
        kind="builtin-agent",
        multi_turn=True,
        # 平台侧的 fixture 工具桥实现四种 mode；目标自身的调用只走 real。
        tool_modes=("real", "mock", "replay", "deny"),
        tools=(),
        interrupt=True,
        # M5-T07：消费者把**冻结的注入声明**渲染进真实 Agent 请求（system prompt）。
        # 声明缺失但 manifest 声明了 skills 时开会话直接具名拒绝。
        skill_injection=True,
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
