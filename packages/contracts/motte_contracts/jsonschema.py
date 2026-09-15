from typing import Any

from .messages import ModelRequest, ModelResponse, StreamEvent
from .model import ModelProfile, ReasoningProfile, ParameterProfile
from .scenario import ScenarioSpec, Case
from .run import Run, CaseRun, ResolvedManifest
from .events import TraceEvent
from .evidence import Artifact, Observation, Score
from .dataset import DatasetVersion

_PUBLIC = [
    ModelRequest,
    ModelResponse,
    StreamEvent,
    ModelProfile,
    ReasoningProfile,
    ParameterProfile,
    ScenarioSpec,
    Case,
    Run,
    CaseRun,
    ResolvedManifest,
    TraceEvent,
    Artifact,
    Observation,
    Score,
    DatasetVersion,
]


def dump_json_schema() -> dict[str, Any]:
    return {c.__name__: c.model_json_schema() for c in _PUBLIC}  # type: ignore[attr-defined]
