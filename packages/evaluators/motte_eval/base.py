from dataclasses import dataclass


@dataclass(frozen=True)
class EvaluationResult:
    passed: bool
    value: float = 0
    evidence: dict | None = None
