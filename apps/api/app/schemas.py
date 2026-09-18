"""Typed API boundary models built from the public contracts."""
from __future__ import annotations

from typing import Any

from motte_contracts.evidence import ScoringPass
from motte_contracts.run import ReplayCase, Run, RunCommand
from pydantic import BaseModel, ConfigDict, Field, field_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateRunRequest(APIModel):
    scenario_version: str = Field(min_length=1)
    manifest: dict[str, Any] = Field(default_factory=dict)
    case_ids: list[str] = Field(default_factory=list)

    @field_validator("case_ids")
    @classmethod
    def distinct_case_ids(cls, case_ids: list[str]) -> list[str]:
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_ids must be unique")
        return case_ids


class CancelRunRequest(APIModel):
    reason: str | None = None


class ReplayRunRequest(APIModel):
    cases: dict[str, ReplayCase]

    @field_validator("cases")
    @classmethod
    def valid_fixture(cls, cases: dict[str, ReplayCase]) -> dict[str, ReplayCase]:
        if not cases or any(not case_id for case_id in cases):
            raise ValueError("replay fixture must contain non-empty case ids")
        return cases


class RunMessageRequest(APIModel):
    content: str = Field(min_length=1)


class RunListResponse(APIModel):
    items: list[Run]
    total: int = Field(ge=0)


class RunCommandListResponse(APIModel):
    items: list[RunCommand]
    total: int = Field(ge=0)


class ScoringPassListResponse(APIModel):
    items: list[ScoringPass]
    total: int = Field(ge=0)
