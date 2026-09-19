"""Strict, offline contracts and policy gates for managed dataset sources."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from .messages import Contract

MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
NonEmptyStr = Annotated[str, Field(min_length=1)]
SourceId = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
HttpsUrl = Annotated[str, Field(pattern=r"^https://\S+$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ReviewDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
PositiveInt = Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES, strict=True)]

SourceTier = Literal[
    "builtin-smoke",
    "managed-public",
    "generated-internal",
    "restricted-public",
    "advanced",
]
GovernanceStatus = Literal["approved", "approved-internal", "restricted", "pending"]
DistributionScope = Literal["public", "internal-only", "restricted", "blocked"]
ComparabilityStatus = Literal["established", "not-established", "not-applicable"]
ArtifactFormat = Literal["json", "jsonl", "csv", "parquet"]
SourceAction = Literal["fetch", "import", "publish", "generate"]

_ACTIONS: tuple[SourceAction, ...] = ("fetch", "import", "publish", "generate")
_EXTERNAL_ACTIONS: tuple[SourceAction, ...] = ("fetch", "import", "publish")
_UNKNOWN_LICENSE_VALUES = frozenset(
    {
        "",
        "not reviewed",
        "not verified",
        "not-reviewed",
        "not-verified",
        "pending",
        "unknown",
        "unreviewed",
        "unverified",
        "unset",
    }
)
_VERIFIED_LICENSE_IDS = frozenset({
    "0BSD",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSL-1.0",
    "CC-BY-4.0",
    "CC-BY-NC-4.0",
    "CC-BY-NC-SA-4.0",
    "CC-BY-SA-4.0",
    "CC0-1.0",
    "ISC",
    "MIT",
    "MPL-2.0",
    "ODC-By-1.0",
    "ODbL-1.0",
    "Unlicense",
})


class SourceContract(Contract):
    """Base contract for source declarations: immutable, strict, and closed to unknown keys."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceGovernance(SourceContract):
    status: GovernanceStatus
    distribution_scope: DistributionScope
    stable_eligible: bool = Field(strict=True)
    reviewer: NonEmptyStr | None
    reviewed_at: ReviewDate | None
    decision_notes: NonEmptyStr

    @field_validator("reviewed_at")
    @classmethod
    def review_date_is_real(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("reviewed_at must be a real ISO calendar date") from error
        return value

    @model_validator(mode="after")
    def status_matches_scope(self) -> Self:
        expected: dict[GovernanceStatus, DistributionScope] = {
            "approved": "public",
            "approved-internal": "internal-only",
            "restricted": "restricted",
            "pending": "blocked",
        }
        if self.distribution_scope != expected[self.status]:
            raise ValueError(
                f"governance status {self.status!r} requires "
                f"distribution_scope={expected[self.status]!r}"
            )
        if self.stable_eligible and self.status in {"pending", "restricted"}:
            raise ValueError("pending/restricted sources cannot be stable eligible")
        if (self.reviewer is None) != (self.reviewed_at is None):
            raise ValueError("reviewer and reviewed_at must be set together")
        return self


class SourceLinks(SourceContract):
    homepage: HttpsUrl | None
    repository: HttpsUrl | None
    dataset_card: HttpsUrl | None
    citation: HttpsUrl | None


class SourceRevision(SourceContract):
    kind: NonEmptyStr
    resolver: NonEmptyStr
    value: NonEmptyStr | None


class SourceArtifact(SourceContract):
    logical_name: NonEmptyStr
    url: HttpsUrl | None
    format: ArtifactFormat | None
    sha256: Sha256 | None
    bytes: PositiveInt | None
    max_bytes: PositiveInt | None
    required: bool = Field(strict=True)

    @model_validator(mode="after")
    def size_fits_limit(self) -> Self:
        if self.bytes is not None and self.max_bytes is not None and self.bytes > self.max_bytes:
            raise ValueError("artifact bytes cannot exceed max_bytes")
        return self


class SourceUpstream(SourceContract):
    revision: SourceRevision
    artifacts: list[SourceArtifact] = Field(min_length=1)

    @model_validator(mode="after")
    def artifact_names_are_unique(self) -> Self:
        names = [artifact.logical_name for artifact in self.artifacts]
        folded = [name.casefold() for name in names]
        if len(set(folded)) != len(folded):
            raise ValueError("artifact logical_name values must be case-insensitively unique")
        if not any(artifact.required for artifact in self.artifacts):
            raise ValueError("at least one upstream artifact must be required")
        return self


class SourceLicenseEvidence(SourceContract):
    declared_ids: list[NonEmptyStr]
    verified_spdx: NonEmptyStr | None
    evidence_urls: list[HttpsUrl]

    @field_validator("declared_ids", "evidence_urls")
    @classmethod
    def entries_are_unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("entries must be unique")
        return values


class SourceLicense(SourceContract):
    data: SourceLicenseEvidence
    code: SourceLicenseEvidence
    commercial_use: NonEmptyStr
    redistribution: NonEmptyStr
    attribution: NonEmptyStr
    share_alike: NonEmptyStr
    review_notes: NonEmptyStr


class SourceConverter(SourceContract):
    id: NonEmptyStr
    version: NonEmptyStr | None


class SourceScorer(SourceContract):
    id: NonEmptyStr
    version: NonEmptyStr | None


class SourceConversion(SourceContract):
    converter: SourceConverter
    splits: list[NonEmptyStr]
    default_split: NonEmptyStr | None
    prompt_version: NonEmptyStr | None
    scorer: SourceScorer
    profiles: list[NonEmptyStr]
    optional_dependency: NonEmptyStr | None

    @field_validator("splits", "profiles")
    @classmethod
    def entries_are_unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("entries must be unique")
        return values

    @model_validator(mode="after")
    def default_split_is_declared(self) -> Self:
        if self.default_split is not None and self.default_split not in self.splits:
            raise ValueError("default_split must be present in splits")
        return self


class SourceSafety(SourceContract):
    network_entrypoint: Literal["cli-only", "none"]
    allowed_protocols: list[Literal["https"]]
    trust_remote_code: Literal[False]
    online_rows_fallback: Literal[False]
    executable_upstream_code: Literal[False]
    archive_auto_extract: Literal[False]

    @model_validator(mode="after")
    def network_policy_is_consistent(self) -> Self:
        if len(set(self.allowed_protocols)) != len(self.allowed_protocols):
            raise ValueError("allowed_protocols entries must be unique")
        if self.network_entrypoint == "none" and self.allowed_protocols:
            raise ValueError("network_entrypoint=none cannot allow network protocols")
        if self.network_entrypoint == "cli-only" and self.allowed_protocols != ["https"]:
            raise ValueError("networked sources must allow only HTTPS")
        return self


class SourceComparability(SourceContract):
    status: ComparabilityStatus
    notes: NonEmptyStr


class SourceSpec(SourceContract):
    schema_version: Literal[1]
    id: SourceId
    label: NonEmptyStr
    description: NonEmptyStr
    tier: SourceTier
    governance: SourceGovernance
    links: SourceLinks
    upstream: SourceUpstream
    license: SourceLicense
    conversion: SourceConversion
    safety: SourceSafety
    official_comparability: SourceComparability
    blockers: list[NonEmptyStr]

    @field_validator("blockers")
    @classmethod
    def blockers_are_unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("blockers must be unique")
        return values

    @model_validator(mode="after")
    def internal_source_boundaries(self) -> Self:
        if self.governance.status == "approved-internal":
            if self.tier != "generated-internal":
                raise ValueError("approved-internal sources must use tier=generated-internal")
            if self.safety.network_entrypoint != "none":
                raise ValueError("approved-internal sources cannot have a network entrypoint")
        return self


class SourceOverrideEvidence(SourceContract):
    actor: NonEmptyStr
    purpose: NonEmptyStr
    ticket: NonEmptyStr
    source_id: SourceId
    actions: list[SourceAction] = Field(min_length=1)
    approved_by: NonEmptyStr
    issued_at: NonEmptyStr
    expires_at: NonEmptyStr

    @field_validator("actor", "purpose", "ticket", "approved_by", "issued_at", "expires_at")
    @classmethod
    def strip_nonempty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("override evidence fields must be non-empty")
        return stripped

    @model_validator(mode="after")
    def approval_is_bounded(self) -> Self:
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("override actions must be unique")
        issued = _parse_approval_time(self.issued_at)
        expires = _parse_approval_time(self.expires_at)
        if expires <= issued:
            raise ValueError("override expires_at must be after issued_at")
        return self


@dataclass(frozen=True)
class SourceActionDecision:
    source_id: str
    action: SourceAction
    allowed: bool
    code: str | None
    reasons: tuple[str, ...]


class SourceActionBlockedError(ValueError):
    """Raised when source policy or immutable evidence blocks an action."""

    def __init__(self, decision: SourceActionDecision) -> None:
        self.decision = decision
        message = "; ".join(decision.reasons) or "source action blocked"
        super().__init__(f"{decision.code}: {decision.source_id} {decision.action}: {message}")


def _parse_approval_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("override timestamps must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("override timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _strict_json_loads(payload: str | bytes) -> object:
    try:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as error:
        raise ValueError("source manifest must be UTF-8 JSON") from error
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError(f"invalid source manifest JSON: {error}") from error


def validate_source_spec(
    payload: SourceSpec | Mapping[str, object] | str | bytes,
) -> SourceSpec:
    """Validate an in-memory source declaration without filesystem or network access."""
    if isinstance(payload, SourceSpec):
        return payload
    if isinstance(payload, (str, bytes)):
        return SourceSpec.model_validate(_strict_json_loads(payload))
    return SourceSpec.model_validate(payload)


def load_source_spec(path: str | Path) -> SourceSpec:
    """Load and strictly validate one UTF-8 JSON source declaration."""
    return validate_source_spec(Path(path).read_bytes())


def load_source_registry(directory: str | Path) -> dict[str, SourceSpec]:
    """Load a source directory, rejecting empty registries, duplicate ids, and filename drift."""
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"source registry directory does not exist: {root}")
    paths = sorted(root.glob("*.json"))
    if not paths:
        raise ValueError(f"source registry directory is empty: {root}")
    registry: dict[str, SourceSpec] = {}
    for path in paths:
        spec = load_source_spec(path)
        if path.stem != spec.id:
            raise ValueError(f"source manifest filename must match id: {path.name} != {spec.id}.json")
        if spec.id in registry:
            raise ValueError(f"duplicate source id: {spec.id}")
        registry[spec.id] = spec
    return registry


def _override_result(
    value: SourceOverrideEvidence | Mapping[str, object] | None,
) -> tuple[SourceOverrideEvidence | None, str | None]:
    if value is None:
        return None, None
    try:
        payload = value.model_dump() if isinstance(value, SourceOverrideEvidence) else value
        return SourceOverrideEvidence.model_validate(payload), None
    except ValidationError:
        return None, "restricted source requires complete, trusted approval evidence"


def _review_blockers(spec: SourceSpec) -> list[str]:
    reasons: list[str] = []
    if spec.governance.reviewer is None or spec.governance.reviewed_at is None:
        reasons.append("governance reviewer and reviewed_at are required")
    return reasons


def _conversion_blockers(spec: SourceSpec) -> list[str]:
    conversion = spec.conversion
    reasons: list[str] = []
    if conversion.converter.version is None:
        reasons.append("converter version is missing")
    if conversion.scorer.version is None:
        reasons.append("scorer version is missing")
    if conversion.prompt_version is None:
        reasons.append("prompt version is missing")
    if not conversion.splits:
        reasons.append("conversion splits are missing")
    if conversion.default_split is None:
        reasons.append("default split is missing")
    if not conversion.profiles:
        reasons.append("conversion profiles are missing")
    return reasons


def _license_evidence_blockers(
    kind: str,
    evidence: SourceLicenseEvidence,
) -> list[str]:
    reasons: list[str] = []
    verified_spdx = (evidence.verified_spdx or "").strip()
    declared_spdx = {item.strip().casefold() for item in evidence.declared_ids}
    if (
        not verified_spdx
        or verified_spdx not in _VERIFIED_LICENSE_IDS
        or verified_spdx.casefold() not in declared_spdx
        or any(marker in verified_spdx.casefold() for marker in _UNKNOWN_LICENSE_VALUES if marker)
    ):
        reasons.append(
            f"verified {kind} SPDX license is missing or inconsistent with declared_ids"
        )
    if not evidence.evidence_urls:
        reasons.append(f"{kind} license evidence URL is missing")
    return reasons


def _external_evidence_blockers(spec: SourceSpec) -> list[str]:
    reasons = _review_blockers(spec)
    reasons.extend(_license_evidence_blockers("data", spec.license.data))
    reasons.extend(_license_evidence_blockers("code", spec.license.code))
    for field_name in ("commercial_use", "redistribution", "attribution", "share_alike"):
        conclusion = getattr(spec.license, field_name).strip().lower()
        if conclusion in _UNKNOWN_LICENSE_VALUES or any(
            marker in conclusion for marker in ("unknown", "pending", "unreviewed", "unset")
        ):
            reasons.append(f"license conclusion {field_name} is unknown")
    if spec.upstream.revision.value is None:
        reasons.append("immutable upstream revision is missing")
    for artifact in (item for item in spec.upstream.artifacts if item.required):
        prefix = f"artifact {artifact.logical_name!r}"
        if artifact.url is None:
            reasons.append(f"{prefix} URL is missing")
        if artifact.format is None:
            reasons.append(f"{prefix} format is missing or unsupported")
        if artifact.sha256 is None:
            reasons.append(f"{prefix} SHA-256 is missing")
        if artifact.bytes is None:
            reasons.append(f"{prefix} byte count is missing")
        if artifact.max_bytes is None:
            reasons.append(f"{prefix} max_bytes is missing")
    reasons.extend(_conversion_blockers(spec))
    if spec.blockers:
        reasons.append("source blockers are not cleared")
    return reasons


def _internal_generation_blockers(spec: SourceSpec) -> list[str]:
    reasons = _review_blockers(spec)
    if spec.upstream.revision.value is None:
        reasons.append("generator revision is missing")
    for artifact in (item for item in spec.upstream.artifacts if item.required):
        if artifact.format is None:
            reasons.append(f"artifact {artifact.logical_name!r} format is missing or unsupported")
    reasons.extend(_conversion_blockers(spec))
    if spec.blockers:
        reasons.append("source blockers are not cleared")
    return reasons


def _restricted_approval_reasons(
    spec: SourceSpec,
    action: SourceAction,
    override: SourceOverrideEvidence | Mapping[str, object] | None,
    *,
    override_verified: bool,
    now: datetime | None,
) -> list[str]:
    evidence, invalid_reason = _override_result(override)
    if not override_verified:
        return ["restricted source approval must be verified by a trusted verifier"]
    if evidence is None:
        return [invalid_reason or "restricted source requires trusted approval evidence"]
    reasons: list[str] = []
    if evidence.source_id != spec.id:
        reasons.append("approval source_id does not match the requested source")
    if action not in evidence.actions:
        reasons.append("approval does not authorize the requested action")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        reasons.append("approval verification time must include a timezone")
    else:
        current = current.astimezone(timezone.utc)
        issued = _parse_approval_time(evidence.issued_at)
        expires = _parse_approval_time(evidence.expires_at)
        if current < issued:
            reasons.append("approval is not active yet")
        if current >= expires:
            reasons.append("approval has expired")
    return reasons


def evaluate_source_action(
    spec: SourceSpec | None,
    action: SourceAction,
    *,
    source_id: str | None = None,
    override: SourceOverrideEvidence | Mapping[str, object] | None = None,
    override_verified: bool = False,
    local: bool = False,
    internal_scope: bool = False,
    now: datetime | None = None,
) -> SourceActionDecision:
    """Evaluate one source action without side effects; every unknown condition fails closed."""
    if action not in _ACTIONS:
        raise ValueError(f"unsupported source action: {action}")
    effective_id = spec.id if spec is not None else (source_id or "<unknown>")
    if spec is None:
        return SourceActionDecision(
            effective_id, action, False, "SOURCE_NOT_FOUND", ("source is not registered",)
        )

    status = spec.governance.status
    if status == "pending":
        return SourceActionDecision(
            effective_id,
            action,
            False,
            "SOURCE_LICENSE_BLOCKED",
            ("pending sources cannot perform actions",),
        )

    if status == "approved-internal":
        policy_reasons: list[str] = []
        if action != "generate":
            policy_reasons.append("approved-internal sources only allow generate")
        if not local:
            policy_reasons.append("approved-internal generation must be local")
        if not internal_scope:
            policy_reasons.append("approved-internal generation requires internal scope")
        if policy_reasons:
            return SourceActionDecision(
                effective_id, action, False, "SOURCE_LICENSE_BLOCKED", tuple(policy_reasons)
            )
        evidence_reasons = _internal_generation_blockers(spec)
        return SourceActionDecision(
            effective_id,
            action,
            not evidence_reasons,
            None if not evidence_reasons else "SOURCE_NOT_READY",
            tuple(evidence_reasons),
        )

    if action not in _EXTERNAL_ACTIONS:
        return SourceActionDecision(
            effective_id,
            action,
            False,
            "SOURCE_LICENSE_BLOCKED",
            ("public sources do not support local internal generation",),
        )

    if status == "restricted":
        approval_reasons = _restricted_approval_reasons(
            spec,
            action,
            override,
            override_verified=override_verified,
            now=now,
        )
        if approval_reasons:
            return SourceActionDecision(
                effective_id,
                action,
                False,
                "SOURCE_APPROVAL_REQUIRED",
                tuple(approval_reasons),
            )

    evidence_reasons = _external_evidence_blockers(spec)
    return SourceActionDecision(
        effective_id,
        action,
        not evidence_reasons,
        None if not evidence_reasons else "SOURCE_NOT_READY",
        tuple(evidence_reasons),
    )


def require_source_action(
    spec: SourceSpec | None,
    action: SourceAction,
    *,
    source_id: str | None = None,
    override: SourceOverrideEvidence | Mapping[str, object] | None = None,
    override_verified: bool = False,
    local: bool = False,
    internal_scope: bool = False,
    now: datetime | None = None,
) -> SourceSpec:
    """Return the source only when the requested action is allowed, otherwise raise."""
    decision = evaluate_source_action(
        spec,
        action,
        source_id=source_id,
        override=override,
        override_verified=override_verified,
        local=local,
        internal_scope=internal_scope,
        now=now,
    )
    if not decision.allowed:
        raise SourceActionBlockedError(decision)
    assert spec is not None
    return spec


def require_registry_action(
    registry: Mapping[str, SourceSpec],
    source_id: str,
    action: SourceAction,
    *,
    override: SourceOverrideEvidence | Mapping[str, object] | None = None,
    override_verified: bool = False,
    local: bool = False,
    internal_scope: bool = False,
    now: datetime | None = None,
) -> SourceSpec:
    """Resolve a registry id and apply the same fail-closed action gate."""
    return require_source_action(
        registry.get(source_id),
        action,
        source_id=source_id,
        override=override,
        override_verified=override_verified,
        local=local,
        internal_scope=internal_scope,
        now=now,
    )


assert_source_action_allowed = require_source_action

__all__ = [
    "ArtifactFormat",
    "ComparabilityStatus",
    "DistributionScope",
    "GovernanceStatus",
    "MAX_ARTIFACT_BYTES",
    "SourceAction",
    "SourceActionBlockedError",
    "SourceActionDecision",
    "SourceArtifact",
    "SourceComparability",
    "SourceConversion",
    "SourceConverter",
    "SourceGovernance",
    "SourceLicense",
    "SourceLicenseEvidence",
    "SourceLinks",
    "SourceOverrideEvidence",
    "SourceRevision",
    "SourceSafety",
    "SourceScorer",
    "SourceSpec",
    "SourceTier",
    "SourceUpstream",
    "assert_source_action_allowed",
    "evaluate_source_action",
    "load_source_registry",
    "load_source_spec",
    "require_registry_action",
    "require_source_action",
    "validate_source_spec",
]
