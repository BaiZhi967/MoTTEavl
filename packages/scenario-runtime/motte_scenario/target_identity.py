"""目标身份解析（M5-T05）。

放在 scenario-runtime 里，让 executor 不必依赖 SDK；SDK 的 scenario_backend
从这里复用同一实现，避免两份"目标是谁"的判断。
"""
from __future__ import annotations

from typing import Any, Mapping


class TargetIdentityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def target_kind_of(manifest: Mapping[str, Any]) -> str:
    """解析目标身份：runtime 名字，或内置 Agent 的 builtin-agent。

    两种声明同时出现是配置冲突（不允许猜）；都没有时明确失败。
    """
    runtime = manifest.get("runtime")
    agent = manifest.get("agent")
    if runtime and agent:
        raise TargetIdentityError(
            "SCENARIO_TARGET_AMBIGUOUS",
            "a scenario run declares both manifest.runtime and manifest.agent",
        )
    if runtime:
        name = str(runtime).partition("@")[0]
        if not name:
            raise TargetIdentityError(
                "SCENARIO_TARGET_INVALID", f"runtime reference is not name@version: {runtime!r}"
            )
        return name
    if agent:
        return str(agent).partition("@")[0]
    raise TargetIdentityError(
        "SCENARIO_TARGET_REQUIRED",
        "scenario runs require a target: manifest.runtime or manifest.agent",
    )
