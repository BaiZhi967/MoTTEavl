from typing import Any

from pydantic import Field

from .messages import Contract


class ScenarioSpec(Contract):
    id: str
    version: int | str
    mode: str
    dataset: str
    model: str
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
