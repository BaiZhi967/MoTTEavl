"""Typed report view bound to one scoring pass."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from .evidence import Score
from .messages import Contract
from .run import EvaluationDescriptor, RunStatus


class ReportSummary(Contract):
    cases: int = Field(ge=0, strict=True)
    scored: int = Field(ge=0, strict=True)
    passed: int = Field(ge=0, strict=True)
    failed: int = Field(ge=0, strict=True)
    pass_rate: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    selected: int | None = Field(default=None, ge=0, strict=True)
    judged: int | None = Field(default=None, ge=0, strict=True)
    denominator: str | None = None
    correct: int | None = Field(default=None, ge=0, strict=True)
    wrong_answer: int | None = Field(default=None, ge=0, strict=True)
    no_expectation: int | None = Field(default=None, ge=0, strict=True)
    parse_failure: int | None = Field(default=None, ge=0, strict=True)
    call_failed: int | None = Field(default=None, ge=0, strict=True)
    not_attempted: int | None = Field(default=None, ge=0, strict=True)
    attempted: int | None = Field(default=None, ge=0, strict=True)
    responded: int | None = Field(default=None, ge=0, strict=True)
    completion: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    attempt_rate: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    accuracy: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    scorer_version: str | None = None


class ReportCost(Contract):
    total: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    price_table_versions: list[str] = Field(default_factory=list)
    known_cases: int | None = Field(default=None, ge=0, strict=True)
    unknown_cases: int | None = Field(default=None, ge=0, strict=True)


class ReportCase(Contract):
    case_id: str
    result: Any = None
    outcome: str | None = None


class RunReport(Contract):
    schema_version: int = Field(default=2, ge=1, strict=True)
    run_id: str
    scenario_version: str
    status: RunStatus
    generated_at: datetime
    scoring_pass_id: str | None = None
    evaluation: EvaluationDescriptor | None = None
    summary: ReportSummary
    cost: ReportCost
    scores: list[Score] = Field(default_factory=list)
    cases: list[ReportCase] = Field(default_factory=list)
    benchmark: dict[str, Any] | None = None
    usage: dict[str, int] | None = None

    @model_validator(mode="after")
    def bind_scored_report(self) -> RunReport:
        if self.schema_version >= 2 and self.scores and self.scoring_pass_id is None:
            raise ValueError("v2 scored reports require scoring_pass_id")
        if any(score.scoring_pass_id not in (None, self.scoring_pass_id) for score in self.scores):
            raise ValueError("report score belongs to a different scoring pass")
        return self
