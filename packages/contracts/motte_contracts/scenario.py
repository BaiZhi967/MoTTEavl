from typing import Any
from .messages import Contract


class ScenarioSpec(Contract):
    id: str
    version: int | str
    mode: str
    dataset: str
    model: str
    agent: str | None = None
    skills: list[str] = []
    harness: str | None = None
    sandbox: dict[str, Any] | None = None
    evaluators: list[str] = []
    limits: dict[str, Any] = {}
    parameter_policy: str = "strict"


class Case(Contract):
    case_id: str
    input: Any
    expected: Any | None = None
    metadata: dict[str, Any] = {}
