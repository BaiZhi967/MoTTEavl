"""Standalone JSON schemas for the public contracts and status enumerations."""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import TypeAdapter

from .dataset import DatasetVersion
from .errors import ContractError, ErrorEnvelope, ExecutionError
from .events import TraceEvent
from .evaluation import (
    AgentResult,
    ArtifactEntry,
    EvidenceCoverage,
    EvidenceRef,
    EvaluatorSpec,
    FrozenObservation,
    InvocationRecord,
    MetricResult,
    ObservedUsage,
    ProcessRecord,
    TerminationRecord,
    ToolCallRecord,
    WorkspaceSnapshot,
)
from .evidence import Artifact, Observation, Score, ScoreSet, ScoringPass
from .messages import Contract, ModelRequest, ModelResponse, StreamEvent
from .model import (IdentityEvidence, IdentityPolicy, IdentityResult, IdentityVerdict,
                    ModelProfile, ParameterProfile, ReasoningProfile)
from .report import ReportCase, ReportCost, ReportSummary, RunReport
from .run import (AttemptStatus, CaseAttempt, CaseRun, EvaluationDescriptor,
                  ExecutionSpec, ResolvedManifest, Run, RunCommand, RunCommandStatus,
                  RunStatus)
from .scenario import Case, ScenarioSpec

_PUBLIC: tuple[type[Contract], ...] = (
    ModelRequest, ModelResponse, StreamEvent, ModelProfile, ReasoningProfile,
    ParameterProfile, IdentityEvidence, IdentityResult, ScenarioSpec, Case,
    Run, CaseRun, CaseAttempt, ExecutionSpec, EvaluationDescriptor, ResolvedManifest,
    RunCommand, TraceEvent, Artifact, Observation, Score, ScoringPass, ScoreSet,
    ReportCase, ReportCost, ReportSummary, RunReport, ContractError,
    ExecutionError, ErrorEnvelope, DatasetVersion,
    FrozenObservation, EvidenceRef, EvidenceCoverage, ArtifactEntry, EvaluatorSpec,
    MetricResult, AgentResult, InvocationRecord, ObservedUsage, TerminationRecord,
    ToolCallRecord, WorkspaceSnapshot, ProcessRecord,
)
_ENUMS: tuple[type[Enum], ...] = (
    RunStatus, AttemptStatus, RunCommandStatus, IdentityPolicy, IdentityVerdict,
)


def dump_json_schema() -> dict[str, Any]:
    schemas: dict[str, Any] = {contract.__name__: contract.model_json_schema() for contract in _PUBLIC}
    schemas.update({enum.__name__: TypeAdapter(enum).json_schema() for enum in _ENUMS})
    return schemas
