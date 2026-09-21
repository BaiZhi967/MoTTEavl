"""Typed API boundary models built from the public contracts."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from motte_contracts.evidence import ScoringPass
from motte_contracts.run import ReplayCase, Run, RunCommand
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrictAPIModel(APIModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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
    content: str | None = Field(default=None, min_length=1, max_length=16000)
    kind: Literal['user_message', 'approve', 'reject', 'interrupt'] = 'user_message'
    payload: dict[str, str] = Field(default_factory=dict)
    case_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    dedupe_key: str = Field(min_length=1, max_length=256)
    expected_session_revision: int = Field(ge=1, strict=True)

    @model_validator(mode='after')
    def command_shape(self):
        if self.kind == 'user_message':
            if not self.content or not self.content.strip() or self.payload:
                raise ValueError('message requires text and empty payload')
        elif self.content is not None:
            raise ValueError('content is only valid for user_message')
        if self.kind in {'approve', 'reject'}:
            if set(self.payload) != {'approval_id', 'request_hash'} or any(
                not value or len(value) > 256 for value in self.payload.values()
            ):
                raise ValueError('approval requires approval_id and request_hash')
        elif self.payload:
            raise ValueError('unexpected command payload')
        return self


class RunListResponse(APIModel):
    items: list[Run]
    total: int = Field(ge=0)


class RunCommandListResponse(APIModel):
    items: list[RunCommand]
    total: int = Field(ge=0)


class ScoringPassListResponse(APIModel):
    items: list[ScoringPass]
    total: int = Field(ge=0)


class DatasetProfileSummary(APIModel):
    name: str
    count: int = Field(ge=0)
    strategy: str | None = None
    case_ids_sha256: str | None = None


class DatasetSummary(APIModel):
    name: str
    version: str
    contract_version: int | None = None
    dataset_fingerprint: str | None = None
    cases: int = Field(ge=0)
    cases_sha256: str | None = None
    suite: str | None = None
    scorer: str | None = None
    source: str | None = None
    revision: str | None = None
    license_status: str | None = None
    profiles: list[DatasetProfileSummary] = Field(default_factory=list)


class DatasetSummaryListResponse(APIModel):
    items: list[DatasetSummary]
    total: int = Field(ge=0)


class DatasetSourceSummary(APIModel):
    id: str
    label: str
    tier: str
    status: str
    distribution_scope: str
    stable_eligible: bool
    revision: str | None = None
    license_ids: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)
    official_comparability: str
    blocker_count: int = Field(ge=0)


class DatasetSourceListResponse(APIModel):
    items: list[DatasetSourceSummary]
    total: int = Field(ge=0)


class DirectLlmImportRequest(APIModel):
    content: str | None = None
    builtin: str | None = None
    name: str | None = None
    version: str | None = None
    license: str | None = None
    scorer: str | None = None
    source: str | None = None

    @model_validator(mode="after")
    def valid_optional_text(self) -> "DirectLlmImportRequest":
        for field_name in ("builtin", "name", "license", "scorer", "source"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        return self


class DirectLlmImportResponse(APIModel):
    imported: str
    scenario: str
    suite: str
    scorer: str
    cases: int = Field(ge=1)
    source: str
    source_sha256: str
    cases_sha256: str
    dataset_fingerprint: str


class DirectLlmOverviewRun(APIModel):
    id: str
    status: str
    created_at: str | None = None
    accuracy: float | None = None


class DirectLlmOverviewItem(APIModel):
    scenario: str
    dataset: str
    suite: str
    contract_version: int = Field(ge=1)
    dataset_fingerprint: str | None = None
    profiles: list[DatasetProfileSummary] = Field(default_factory=list)
    eval: dict[str, Any]
    provenance: dict[str, Any]
    cases: int = Field(ge=0)
    runs: list[DirectLlmOverviewRun] = Field(default_factory=list)


class DirectLlmOverviewResponse(APIModel):
    items: list[DirectLlmOverviewItem]
    total: int = Field(ge=0)


class DirectLlmAllSelection(StrictAPIModel):
    mode: Literal["all"]


class DirectLlmIdsSelection(StrictAPIModel):
    mode: Literal["ids"]
    case_ids: list[str] = Field(min_length=1)

    @field_validator("case_ids")
    @classmethod
    def distinct_nonempty_case_ids(cls, case_ids: list[str]) -> list[str]:
        if any(not case_id.strip() for case_id in case_ids):
            raise ValueError("case_ids must contain non-empty strings")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_ids must be unique")
        return case_ids


class DirectLlmRandomSelection(StrictAPIModel):
    mode: Literal["random"]
    count: int = Field(gt=0)
    seed: str | None = Field(default=None, pattern=r"^[0-9a-f]{8,64}$")


class DirectLlmProfileSelection(StrictAPIModel):
    mode: Literal["profile"]
    profile: str = Field(min_length=1)

    @field_validator("profile")
    @classmethod
    def nonempty_profile(cls, profile: str) -> str:
        if not profile.strip():
            raise ValueError("profile must be a non-empty string")
        return profile


DirectLlmCaseSelection = Annotated[
    DirectLlmAllSelection
    | DirectLlmIdsSelection
    | DirectLlmRandomSelection
    | DirectLlmProfileSelection,
    Field(discriminator="mode"),
]


class DirectLlmRunRequest(StrictAPIModel):
    scenario: str | None = Field(default=None, min_length=1)
    model: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    reasoning_level: str | None = Field(default=None, min_length=1)
    case_selection: DirectLlmCaseSelection | None = None
    dataset_name: str | None = Field(default=None, min_length=1)
    dataset_version: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def scenario_or_legacy_dataset(self) -> "DirectLlmRunRequest":
        if self.scenario is None and self.dataset_name is None:
            raise ValueError("scenario is required")
        return self


class AgentTasksImportRequest(APIModel):
    """Agent 文件任务数据集导入：cases 为 JSON 数组。"""

    content: str = Field(min_length=2)
    name: str = Field(min_length=1)
    version: str | None = Field(default=None, min_length=1)


class AgentBudgetRequest(APIModel):
    max_steps: int | None = Field(default=None, gt=0, le=64)
    max_tool_calls: int | None = Field(default=None, gt=0, le=256)
    wall_time_sec: float | None = Field(default=None, gt=0, le=3600.0)
    per_call_timeout_sec: float | None = Field(default=None, gt=0, le=600.0)
    total_token_limit: int | None = Field(default=None, gt=0)
    observed_cost_limit: float | None = Field(default=None, gt=0)


class AgentTasksRunRequest(StrictAPIModel):
    scenario: str = Field(min_length=1)
    model: str = Field(min_length=1)
    mode: Literal["native-tool", "legacy-json"] = "native-tool"
    budget: AgentBudgetRequest | None = None
    case_selection: DirectLlmCaseSelection | None = None


class AgentTasksDryRunResponse(APIModel):
    scenario: str
    dataset: str
    mode: str
    prompt_version: str
    selected_cases: int = Field(ge=1)
    backend: str
    budget: dict[str, Any]


class DirectLlmDryRunResponse(APIModel):
    scenario: str
    dataset: str
    contract_version: int = Field(ge=1)
    plugin_version: str
    selected_count: int = Field(ge=1)
    case_ids_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profile: str | None = None
    max_input_tokens_upper_bound: int | None = Field(default=None, ge=0)
    max_output_tokens: int = Field(ge=0)
    max_total_tokens_upper_bound: int | None = Field(default=None, ge=0)
    context_window: int | None = Field(default=None, gt=0)
    estimation_method: str | None = None
    estimated_cost_upper_bound: float | None = Field(
        default=None,
        ge=0,
        description="Conservative estimated upper bound; never an exact billed cost.",
    )
    price_table_version: str | None = None
    currency: str | None = None
    estimated: Literal[True] = True


class WorkflowValidationResponse(APIModel):
    """Workflow 纯预检结果：结构、条件、预算与目标要求都可编译时才 ok。

    executed 恒为 False：预检不发布版本、不创建 Run、不调用模型。
    """

    ok: bool
    publishable: bool
    executed: bool
    workflow_id: str
    version: str
    ref: str
    schema_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    step_count: int = Field(ge=1)
    top_level_step_ids: list[str] = Field(default_factory=list)
    condition_count: int = Field(ge=0)
    target_requirements: dict[str, Any]
    limits: dict[str, Any]
    fixture_refs: list[dict[str, Any]] = Field(default_factory=list)
    defaulted_fields: list[str] = Field(default_factory=list)


class WorkflowDiagnostic(APIModel):
    """一条转换诊断：稳定 code + 严重度 + 旧 DSL 路径 + 可读原因。"""

    code: str
    severity: str
    path: str
    message: str


class WorkflowConversionResponse(APIModel):
    """旧 DSL 只读转换报告：不发布、不执行（runs_executed 恒为 0）。"""

    publishable: bool
    published: bool
    executed: bool
    runs_executed: int = Field(ge=0)
    blocking_codes: list[str] = Field(default_factory=list)
    diagnostics: list[WorkflowDiagnostic] = Field(default_factory=list)
    mapping: list[dict[str, Any]] = Field(default_factory=list)
    workflow_draft: dict[str, Any]
    fixture_draft: dict[str, Any]
    source: dict[str, Any] = Field(default_factory=dict)
    candidate: dict[str, Any] | None = None


class ScenarioTargetSummary(APIModel):
    """一个注册目标的能力声明；available=False 表示只有声明、不可执行。"""

    kind: str
    available: bool
    multi_turn: bool | None = None
    tool_modes: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    interrupt: bool | None = None
    skill_injection: bool | None = None
    evidence: list[str] = Field(default_factory=list)


class ScenarioTargetListResponse(APIModel):
    items: list[ScenarioTargetSummary] = Field(default_factory=list)
    total: int = Field(ge=0)
    available: bool


class SkillValidationResponse(APIModel):
    """Skill 纯静态校验结果。

    validation_scope 只有 static：不运行入口、不执行 fixture、不调用模型；
    resource_bytes_verified=False 表示内容寻址的资源字节核验属于另一个作用域，
    不能把读 manifest 说成已执行（M5-A10）。
    """

    ok: bool
    executed: bool
    validation_scope: Literal["static"] = "static"
    resource_bytes_verified: bool
    skill_id: str
    version: str
    ref: str
    kind: str
    lifecycle: str
    schema_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    executable: bool
    dependency_refs: list[dict[str, Any]] = Field(default_factory=list)
    resource_paths: list[str] = Field(default_factory=list)
    fixture_refs: list[dict[str, Any]] = Field(default_factory=list)
    requested_permissions: dict[str, Any] = Field(default_factory=dict)
    defaulted_fields: list[str] = Field(default_factory=list)


class ResourcePublication(APIModel):
    id: str = Field(min_length=1)
    dataset: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    dataset_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    receipt: dict[str, Any]
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1)
    entrypoint: str = Field(min_length=1)
    published_at: str = Field(min_length=1)


class ResourcePublicationListResponse(APIModel):
    items: list[ResourcePublication]
    total: int = Field(ge=0)
