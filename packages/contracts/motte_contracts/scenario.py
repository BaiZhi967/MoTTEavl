from typing import Any

from pydantic import Field

from .messages import Contract


class ScenarioSpec(Contract):
    id: str
    version: int | str
    mode: str
    dataset: str
    # M4：runtime 驱动的场景（CLI 原生认证 / runner 配置模型）可以不声明
    # 平台模型；provider 驱动路径仍在 prepare_run 强制模型解析。
    model: str | None = None
    agent: str | None = None
    skills: list[str] = Field(default_factory=list)
    harness: str | None = None
    sandbox: dict[str, Any] | None = None
    evaluators: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    parameter_policy: str = "strict"


class Case(Contract):
    case_id: str
    input: Any
    expected: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
