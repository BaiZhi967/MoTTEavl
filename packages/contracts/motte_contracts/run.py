"""Run lifecycle, execution and scoring contracts.

A v1 stored run can be projected into ``Run`` with schema_version=1 and
revision=0. New manifests must also be validated with ``ResolvedManifest``;
``Run.manifest`` stays a dict so historical snapshots remain readable.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field, StrictBool, field_validator, model_validator

from .errors import ExecutionError
from .evidence import Score, ScoringPass
from .external_job import ExecutionMode
from .messages import Contract


class RunStatus(str, Enum):
    queued = "queued"
    preparing = "preparing"
    running = "running"
    collecting = "collecting"
    scoring = "scoring"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    unsupported = "unsupported"
    profile_stale = "profile_stale"
    needs_review = "needs_review"


class AttemptStatus(str, Enum):
    prepared = "prepared"
    dispatching = "dispatching"
    succeeded = "succeeded"
    failed = "failed"
    indeterminate = "indeterminate"


class RunCommandStatus(str, Enum):
    queued = "queued"
    delivered = "delivered"
    acknowledged = "acknowledged"
    failed = "failed"
    # M4-T10：HTTP 202 只表示持久接收；投递被拒、过期、重启后结果不可证
    # 分别进入终态，绝不回退或重复投递危险批准。
    rejected = "rejected"
    expired = "expired"
    delivery_unknown = "delivery_unknown"


class ExecutionSpec(Contract):
    backend_id: str = Field(min_length=1)
    backend_version: str = Field(min_length=1)
    config_hash: str | None = None
    capabilities: dict[str, StrictBool] = Field(default_factory=dict)
    # 分派模式由 backend 注册表决定；旧 manifest 缺省为 sample（M2-T01）。
    execution_mode: ExecutionMode = ExecutionMode.sample


class EvaluationDescriptor(Contract):
    benchmark_id: str = Field(min_length=1)
    benchmark_version: str = Field(min_length=1)
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    scorer_id: str = Field(min_length=1)
    scorer_version: str = Field(min_length=1)


class ReplayCase(Contract):
    output: Any
    expected: Any = None


class ResolvedManifest(Contract):
    schema_version: int = Field(default=2, ge=2, strict=True)
    execution: ExecutionSpec
    evaluation: EvaluationDescriptor | None = None
    provider: dict[str, Any] | None = None
    resource_snapshots: dict[str, Any] = Field(default_factory=dict)
    model: str | dict[str, Any] | None = None
    dataset: str | dict[str, Any] | None = None
    seed: int | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    cases: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    reasoning_level: str | None = None
    case_selection: dict[str, Any] | None = None
    price_table_version: str | None = None
    benchmark_snapshot: dict[str, Any] | None = None
    benchmark_provenance: dict[str, Any] | None = None
    replay_fixture: dict[str, ReplayCase] | None = None
    agent: str | None = None
    agent_config: dict[str, Any] | None = None
    skills: list[str] = Field(default_factory=list)
    harness: str | None = None
    sandbox: dict[str, Any] | None = None
    # M4：外部 runtime 驱动的 Run（pi-agent / claude-cli / codex-cli …）
    runtime: str | None = None
    runtime_profile: dict[str, Any] | None = None
    runtime_snapshot: dict[str, Any] | None = None
    # tool_control.enforcement=not-enforced 的 runtime 需要显式确认才允许运行
    runtime_accept_unenforced_tools: bool = False


class CaseRun(Contract):
    run_id: str
    case_id: str
    status: str | None = None
    ordinal: int | None = Field(default=None, ge=1, strict=True)
    attempt_id: str | None = None
    result: Any = None
    expected: Any = None
    outcome: str | None = None
    stop_run: bool = False
    error: ExecutionError | None = None


class CaseAttempt(Contract):
    id: str
    run_id: str
    case_id: str
    status: AttemptStatus = AttemptStatus.prepared
    revision: int = Field(default=1, ge=1, strict=True)
    attempt_no: int = Field(default=1, ge=1, strict=True)
    execution_token: str | None = None
    execution_backend_id: str | None = None
    idempotency_key: str | None = None
    prepared_at: datetime | None = None
    dispatched_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any = None
    error: ExecutionError | None = None


class Run(Contract):
    schema_version: int = Field(default=2, ge=1, strict=True)
    id: str
    scenario_version: str
    revision: int = Field(default=1, ge=0, strict=True)
    status: RunStatus = RunStatus.queued
    model: str | None = None
    manifest: dict[str, Any] = Field(default_factory=dict)
    requested_manifest: dict[str, Any] | None = None
    case_ids: list[str] = Field(default_factory=list)
    parent_run_id: str | None = None
    current_scoring_pass_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cases: list[CaseRun] = Field(default_factory=list)
    scores: list[Score] = Field(default_factory=list)
    scoring_pass: ScoringPass | None = None
    cancellation: dict[str, Any] | None = None
    error: ExecutionError | None = None
    rescored: bool | None = None

    @field_validator("case_ids")
    @classmethod
    def distinct_case_ids(cls, case_ids: list[str]) -> list[str]:
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_ids must be unique")
        return case_ids

    @model_validator(mode="after")
    def validate_revision(self) -> Run:
        if self.schema_version >= 2 and self.revision < 1:
            raise ValueError("v2 runs must start at revision 1")
        return self


class RunCommand(Contract):
    id: str
    run_id: str
    type: str = "user_message"
    status: RunCommandStatus = RunCommandStatus.queued
    revision: int = Field(default=1, ge=1, strict=True)
    content: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    execution_token: str | None = None
    # M4-T10：投递绑定（session/case/dedupe/过期/期望 revision/请求 hash）
    case_id: str | None = None
    session_id: str | None = None
    dedupe_key: str | None = None
    expires_at: datetime | None = None
    expected_session_revision: int | None = Field(default=None, ge=1, strict=True)
    request_hash: str | None = None
    intent_hash: str | None = None
    worker_token: str | None = None
    ack_evidence: dict[str, Any] | None = None
    # 人工干预标记：进入 Run 证据与比较条件（M4-G18）
    intervention: bool = False
    # 提交者（审计）：operator / api / worker
    actor: str | None = None
    created_at: datetime | None = None
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    failed_at: datetime | None = None
    rejected_at: datetime | None = None
    expired_at: datetime | None = None
    error: ExecutionError | None = None
