from typing import Any
from .messages import Contract


class Run(Contract):
    id: str
    scenario_id: str
    status: str = "queued"
    manifest: dict[str, Any] = {}


class CaseRun(Contract):
    run_id: str
    case_id: str
    status: str = "queued"


class ResolvedManifest(Contract):
    model: dict[str, Any]
    dataset: dict[str, Any]
    seed: int | None = None
    budget: dict[str, Any] = {}
