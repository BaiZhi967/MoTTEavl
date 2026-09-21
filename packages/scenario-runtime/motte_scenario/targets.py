"""Scenario Target 抽象与能力声明（M5-T03b / M5-G07）。

Target 是"被驱动的业务目标"：内置 Agent、或某个外部 runtime。它**不是**
Workflow，也不是 Run 调度器。本模块只负责：

* 声明一个 Target 实际具备的能力（multi_turn / tool modes / interrupt /
  Skill injection / evidence）；
* 把 Workflow 的 target_requirements 与这些能力比对，不满足即拒绝。

关键约束：M4 的 interactive 命令通道**不等于**通用多轮业务会话。没有注册
Target adapter 的 runtime 一律不可用，绝不用 capability 布尔值代替真实
消费者（M5 执行计划第 2 节；M5-A09）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


class TargetCapabilityError(ValueError):
    """目标能力不满足 Workflow 要求；code 供创建期结构化错误映射。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TargetCapabilities:
    """一个 Target 实际声明并**可实现**的能力。"""

    kind: str
    multi_turn: bool
    tool_modes: tuple[str, ...]
    tools: tuple[str, ...] = ()
    interrupt: bool = False
    skill_injection: bool = False
    evidence: tuple[str, ...] = ("events",)
    #: 能力来源：adapter（真实实现）或 declared（仅声明，不可执行）。
    source: str = "adapter"


class TargetSession(Protocol):
    """一个 Case 一个实例的 Target 会话；send 之间保留上下文与预算。"""

    def begin(self) -> dict[str, Any]: ...

    def send(self, message: str) -> dict[str, Any]: ...

    def observe(self) -> dict[str, Any]: ...

    def interrupt(self, reason: str) -> dict[str, Any]: ...

    def close(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TargetAdapter:
    """Target adapter 的注册条目：能力 + 会话工厂。"""

    kind: str
    capabilities: Callable[[Mapping[str, Any]], TargetCapabilities]
    open_session: Callable[[Mapping[str, Any]], TargetSession]


_ADAPTERS: dict[str, TargetAdapter] = {}


def register_target_adapter(adapter: TargetAdapter, *, replace: bool = False) -> TargetAdapter:
    if adapter.kind in _ADAPTERS and not replace:
        raise ValueError(f"scenario target adapter already registered: {adapter.kind}")
    _ADAPTERS[adapter.kind] = adapter
    return adapter


def unregister_target_adapter(kind: str) -> None:
    _ADAPTERS.pop(kind, None)


def registered_target_adapters() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def target_adapter(kind: str) -> TargetAdapter:
    try:
        return _ADAPTERS[kind]
    except KeyError:
        known = ", ".join(registered_target_adapters()) or "(none)"
        raise TargetCapabilityError(
            "SCENARIO_TARGET_UNSUPPORTED",
            f"no scenario target adapter is registered for {kind!r} (known: {known}); "
            "an interactive command channel is not a multi-turn business session",
        ) from None


def target_capabilities(kind: str, manifest: Mapping[str, Any]) -> TargetCapabilities:
    return target_adapter(kind).capabilities(manifest)


def require_capabilities(
    requirements: Mapping[str, Any], capabilities: TargetCapabilities,
) -> None:
    """把 Workflow 的 target_requirements 与 Target 能力比对。

    任何不满足都是明确的创建期错误；不静默降级、不改选其他 Target。
    """
    if not isinstance(requirements, Mapping):
        raise TargetCapabilityError(
            "SCENARIO_TARGET_REQUIREMENTS_INVALID", "target_requirements must be an object"
        )
    if capabilities.source != "adapter":
        raise TargetCapabilityError(
            "SCENARIO_TARGET_UNSUPPORTED",
            f"target {capabilities.kind!r} only declares capabilities; no implementation is registered",
        )
    if requirements.get("multi_turn") and not capabilities.multi_turn:
        raise TargetCapabilityError(
            "SCENARIO_TARGET_MULTI_TURN_UNSUPPORTED",
            f"target {capabilities.kind!r} cannot serve multiple send turns",
        )
    min_turns = requirements.get("min_turns") or 1
    if isinstance(min_turns, int) and min_turns > 1 and not capabilities.multi_turn:
        raise TargetCapabilityError(
            "SCENARIO_TARGET_MULTI_TURN_UNSUPPORTED",
            f"workflow needs at least {min_turns} turns but {capabilities.kind!r} is single-turn",
        )
    required_modes = set(requirements.get("tool_modes") or ("real",))
    missing_modes = sorted(required_modes - set(capabilities.tool_modes))
    if missing_modes:
        raise TargetCapabilityError(
            "SCENARIO_TARGET_TOOL_MODE_UNSUPPORTED",
            f"target {capabilities.kind!r} does not implement tool modes: "
            + ", ".join(missing_modes),
        )
    required_tools = set(requirements.get("required_tools") or ())
    missing_tools = sorted(required_tools - set(capabilities.tools))
    if missing_tools:
        raise TargetCapabilityError(
            "SCENARIO_TARGET_TOOL_UNSUPPORTED",
            f"target {capabilities.kind!r} does not expose tools: " + ", ".join(missing_tools),
        )
    if requirements.get("interrupt") and not capabilities.interrupt:
        raise TargetCapabilityError(
            "SCENARIO_TARGET_INTERRUPT_UNSUPPORTED",
            f"target {capabilities.kind!r} cannot be interrupted",
        )
    if requirements.get("skill_injection") and not capabilities.skill_injection:
        raise TargetCapabilityError(
            "SCENARIO_TARGET_SKILL_INJECTION_UNSUPPORTED",
            f"target {capabilities.kind!r} cannot receive skill injection",
        )
    required_evidence = set(requirements.get("evidence") or ())
    missing_evidence = sorted(required_evidence - set(capabilities.evidence))
    if missing_evidence:
        raise TargetCapabilityError(
            "SCENARIO_TARGET_EVIDENCE_UNSUPPORTED",
            f"target {capabilities.kind!r} cannot provide evidence: "
            + ", ".join(missing_evidence),
        )


def describe_targets() -> list[dict[str, Any]]:
    """给 API/CLI 的能力清单：只列出真实注册的 adapter。"""
    described: list[dict[str, Any]] = []
    for kind in registered_target_adapters():
        adapter = _ADAPTERS[kind]
        try:
            capabilities = adapter.capabilities({})
        except Exception:  # noqa: BLE001 - 描述失败不掩盖注册事实
            described.append({"kind": kind, "available": False})
            continue
        described.append({
            "kind": kind,
            "available": capabilities.source == "adapter",
            "multi_turn": capabilities.multi_turn,
            "tool_modes": list(capabilities.tool_modes),
            "tools": list(capabilities.tools),
            "interrupt": capabilities.interrupt,
            "skill_injection": capabilities.skill_injection,
            "evidence": list(capabilities.evidence),
        })
    return described
