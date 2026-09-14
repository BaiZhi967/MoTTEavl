from typing import Any
from .messages import Contract

class Artifact(Contract):
    id: str
    kind: str
    uri: str
    sha256: str | None = None

class Observation(Contract):
    name: str
    value: Any
    source: str | None = None

class Score(Contract):
    evaluator: str
    value: float
    passed: bool | None = None
    details: dict[str, Any] = {}
