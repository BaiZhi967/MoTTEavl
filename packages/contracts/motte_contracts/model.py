from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field, field_validator, model_validator

from .messages import Contract


class IdentityPolicy(str, Enum):
    report_only = "report_only"
    require_reported = "require_reported"
    require_match = "require_match"


class IdentityVerdict(str, Enum):
    exact_match = "exact_match"
    alias_match = "alias_match"
    unreported = "unreported"
    mismatch = "mismatch"
    not_evaluated = "not_evaluated"


class IdentityEvidence(Contract):
    source: str | None = Field(default=None, min_length=1)
    reported_value: str | None = None
    path: str | None = None
    alias_map_version: str | None = None
    matched_alias: str | None = None
    policy: IdentityPolicy | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class IdentityResult(Contract):
    requested_model: str = Field(min_length=1)
    reported_model: str | None = None
    resolved_model_identity: str | None = None
    identity_evidence: IdentityEvidence = Field(default_factory=IdentityEvidence)
    identity_policy: IdentityPolicy = IdentityPolicy.report_only
    identity_policy_result: IdentityVerdict = IdentityVerdict.not_evaluated
    policy_passed: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def evidence_consistency(self) -> IdentityResult:
        if self.identity_evidence.policy is not None and self.identity_evidence.policy != self.identity_policy:
            raise ValueError("identity evidence policy must match identity_policy")
        if self.identity_policy_result == IdentityVerdict.unreported and self.reported_model is not None:
            raise ValueError("unreported identity cannot have reported_model")
        if self.identity_policy_result in (
            IdentityVerdict.exact_match, IdentityVerdict.alias_match, IdentityVerdict.mismatch
        ) and not self.reported_model:
            raise ValueError("evaluated identity requires reported_model")
        return self


class ReasoningProfile(Contract):
    supported: bool = Field(default=False, strict=True)
    levels: list[str] = Field(default_factory=list, max_length=32)
    control: str | None = Field(default=None, max_length=4096)
    default_level: str | None = None

    @model_validator(mode="after")
    def validate_mapping(self) -> "ReasoningProfile":
        from .reasoning import LEGACY_CONTROLS, evaluate_control

        if any(not level.strip() or len(level) > 64 for level in self.levels):
            raise ValueError("reasoning.levels entries must be nonempty and at most 64 characters")
        if len(set(self.levels)) != len(self.levels):
            raise ValueError("reasoning.levels must be unique")
        if self.default_level is not None and (
            not self.supported or self.default_level not in self.levels
        ):
            raise ValueError("reasoning.default_level must belong to supported reasoning.levels")
        if self.control in LEGACY_CONTROLS:
            if self.default_level is not None:
                raise ValueError("replace legacy reasoning.control metadata with a CEL expression")
        elif self.control is not None:
            for level in self.levels or ["__validation__"]:
                evaluate_control(self.control, level)
        elif self.default_level is not None:
            raise ValueError("reasoning.default_level requires reasoning.control CEL")
        return self


class ParameterProfile(Contract):
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    seed: int | None = None

    @field_validator("temperature", "top_p")
    @classmethod
    def probability_range(cls, v: Any) -> Any:
        if v is not None and not 0 <= v <= 1:
            raise ValueError("must be between 0 and 1")
        return v


class ResourceLifecycle(str, Enum):
    draft = "draft"
    published = "published"
    deprecated = "deprecated"


class ModelProfile(Contract):
    id: str
    provider: str
    model: str | None = None
    enabled: bool = True
    capabilities: dict[str, Any]
    identity_policy: IdentityPolicy = IdentityPolicy.report_only
    identity_aliases: dict[str, str] = Field(default_factory=dict)
    identity_alias_version: str | None = None
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    supports_tools: bool = False
    tool_features: list[str] = Field(default_factory=list)
    context_window: int | None = Field(default=None, gt=0, strict=True)
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    reasoning: ReasoningProfile = Field(default_factory=ReasoningProfile)
    parameters: ParameterProfile = Field(default_factory=ParameterProfile)
    provenance: dict[str, Any] = Field(default_factory=dict)
    profile_hash: str | None = None
    generation: int = Field(default=1, ge=1, strict=True)
    lifecycle: ResourceLifecycle = ResourceLifecycle.draft
    published_at: str | None = None
    deprecated_at: str | None = None

    @model_validator(mode="after")
    def validate_identity_aliases(self) -> ModelProfile:
        if self.identity_aliases and not self.identity_alias_version:
            raise ValueError("identity_alias_version is required when identity_aliases are configured")
        if any(not alias.strip() or not canonical.strip()
               for alias, canonical in self.identity_aliases.items()):
            raise ValueError("identity_aliases must map non-empty model names")
        if self.lifecycle == ResourceLifecycle.draft and (
            self.published_at is not None or self.deprecated_at is not None
        ):
            raise ValueError("draft model profiles cannot have lifecycle timestamps")
        if self.lifecycle == ResourceLifecycle.published and (
            self.published_at is None or self.deprecated_at is not None
        ):
            raise ValueError("published model profiles require published_at and cannot be deprecated")
        if self.lifecycle == ResourceLifecycle.deprecated and self.deprecated_at is None:
            raise ValueError("deprecated model profiles require deprecated_at")
        return self

    @field_validator("input_modalities")
    @classmethod
    def text_required(cls, v: list[str]) -> list[str]:
        if "text" not in v or any(not item.strip() for item in v):
            raise ValueError("input_modalities must include text and contain nonempty strings")
        return list(dict.fromkeys(v))

    @field_validator("capabilities")
    @classmethod
    def require_modalities(cls, v: dict[str, Any]) -> dict[str, Any]:
        if "input_modalities" in v and not v["input_modalities"]:
            raise ValueError("input_modalities cannot be empty")
        result = dict(v)
        for key in ("structured_output", "native_search", "system_messages"):
            if key in result and type(result[key]) is not bool:
                raise ValueError(f"capabilities.{key} must be a boolean")
            result.setdefault(key, False)
        return result
