"""Evidence and immutable scoring-pass contracts."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

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
    # Legacy aggregate scores have evaluator/value but no case_id; per-case scores
    # require case_id when attached to a ScoreSet.
    case_id: str | None = None
    evaluator: str | None = None
    value: float | None = Field(default=None, allow_inf_nan=False)
    passed: bool | None = Field(default=None, strict=True)
    outcome: str | None = None
    attempted: bool | None = Field(default=None, strict=True)
    responded: bool | None = Field(default=None, strict=True)
    judged: bool | None = Field(default=None, strict=True)
    scorer: str | None = None
    scorer_version: str | None = None
    error_class: str | None = None
    parsed: str | None = None
    scoring_pass_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ScoreSet(Contract):
    run_id: str
    scoring_pass_id: str
    scores: list[Score] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_case_scores(self) -> ScoreSet:
        case_ids = [score.case_id for score in self.scores]
        if any(not case_id for case_id in case_ids) or len(case_ids) != len(set(case_ids)):
            raise ValueError("score set requires one score per distinct case_id")
        if any(score.scoring_pass_id not in (None, self.scoring_pass_id) for score in self.scores):
            raise ValueError("score belongs to a different scoring pass")
        return self


class ScoringPass(Contract):
    id: str
    run_id: str
    scorer_id: str
    scorer_version: str
    created_at: datetime | None = None
    source: str | None = None
    source_run_revision: int | None = Field(default=None, ge=0, strict=True)
    source_snapshot_hash: str | None = None
    previous_pass_id: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    scores: list[Score] = Field(default_factory=list)

    @model_validator(mode="after")
    def matches_scores(self) -> ScoringPass:
        ScoreSet(run_id=self.run_id, scoring_pass_id=self.id, scores=self.scores)
        return self
