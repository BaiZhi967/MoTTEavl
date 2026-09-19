"""Typed Direct LLM dataset contract v2.

This module only defines immutable record shapes, canonical identities, and
versioned scenario validation. Scorer execution and SDK resolution belong to
later layers; v1 remains in :mod:`motte_contracts.direct_llm` unchanged.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from .identity import canonical_sha256, dataset_fingerprint as identity_dataset_fingerprint
from .messages import Contract

SUITE: Literal["direct-llm"] = "direct-llm"
DATASET_ID: Literal["direct-llm-prompts"] = "direct-llm-prompts"
CONTRACT_VERSION: Literal[2] = 2
DATASET_VERSION: Literal[2] = 2
PLUGIN_VERSION: Literal["2"] = "2"
EVAL_KEY = "eval"
SELECTION_ALL = "all-rows-in-file-order"
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_CONTENT_HASH = re.compile(r"[0-9a-f]{64}")


def _canonical_hash(value: Any, label: str) -> str:
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must contain canonical JSON values") from error


def scorer_config_sha256(config: dict[str, Any]) -> str:
    return _canonical_hash(config, "scorer config")


def converter_config_sha256(config: dict[str, Any]) -> str:
    return _canonical_hash(config, "converter config")


def case_ids_sha256(case_ids: list[str]) -> str:
    return _canonical_hash(case_ids, "profile case_ids")


def _nonempty(value: str, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} must be non-empty")
    return value


class ScorerSpec(Contract):
    id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    config: dict[str, Any]
    config_sha256: str

    @field_validator("id", "version")
    @classmethod
    def validate_identity(cls, value: str, info: Any) -> str:
        return _nonempty(value, f"scorer {info.field_name}")

    @model_validator(mode="after")
    def validate_config_hash(self) -> ScorerSpec:
        expected = scorer_config_sha256(self.config)
        if self.config_sha256 != expected:
            raise ValueError("scorer config_sha256 mismatch")
        return self


class CaseMetadataV2(Contract):
    source_line: int = Field(gt=0, strict=True)
    source_id: str = Field(min_length=1, max_length=512)
    language: str = Field(min_length=1, max_length=64)
    subject: str | None = Field(default=None, max_length=256)
    category: str | None = Field(default=None, max_length=256)
    difficulty: str | None = Field(default=None, max_length=128)
    split: str = Field(min_length=1, max_length=128)
    tags: list[str] = Field(default_factory=list, max_length=128)
    template_family: str | None = Field(default=None, max_length=256)
    scorer: ScorerSpec | None = None

    @field_validator("source_id", "language", "split")
    @classmethod
    def validate_required_text(cls, value: str, info: Any) -> str:
        return _nonempty(value, f"metadata {info.field_name}")

    @field_validator("subject", "category", "difficulty", "template_family")
    @classmethod
    def validate_optional_text(cls, value: str | None, info: Any) -> str | None:
        if value is not None:
            return _nonempty(value, f"metadata {info.field_name}")
        return None

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("metadata tags must contain non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("metadata tags must be distinct")
        return value


class DirectLlmCaseV2(Contract):
    case_id: str = Field(min_length=1, max_length=512)
    input: str = Field(min_length=1)
    expected: str | None = Field(default=None, min_length=1)
    metadata: CaseMetadataV2

    @model_validator(mode="before")
    @classmethod
    def normalize_input_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        has_input = "input" in normalized
        has_prompt = "prompt" in normalized
        if has_input == has_prompt:
            raise ValueError("exactly one of input or prompt is required")
        if has_prompt:
            normalized["input"] = normalized.pop("prompt")
        if "scorer" in normalized:
            metadata = normalized.get("metadata")
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be an object when scorer is provided")
            if "scorer" in metadata:
                raise ValueError("scorer must be provided either at case level or metadata, not both")
            normalized["metadata"] = {**metadata, "scorer": normalized.pop("scorer")}
        return normalized

    @field_validator("case_id", "input")
    @classmethod
    def validate_required_text(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("expected")
    @classmethod
    def validate_expected(cls, value: str | None) -> str | None:
        if value is not None:
            return _nonempty(value, "expected")
        return None


class ProvenanceArtifactV2(Contract):
    logical_name: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=1, max_length=4096)
    sha256: str
    bytes: int = Field(gt=0, strict=True)

    @field_validator("logical_name", "url")
    @classmethod
    def validate_required_text(cls, value: str, info: Any) -> str:
        return _nonempty(value, f"artifact {info.field_name}")

    @field_validator("sha256")
    @classmethod
    def validate_content_hash(cls, value: str) -> str:
        if not _CONTENT_HASH.fullmatch(value):
            raise ValueError("artifact sha256 must be 64 lowercase hex characters")
        return value


class LicenseProvenanceV2(Contract):
    id: str = Field(min_length=1, max_length=256)
    status: Literal[
        "approved", "approved-internal", "approved-test-only", "restricted", "pending"
    ]
    evidence_urls: list[str] = Field(default_factory=list, max_length=128)
    commercial_use: str = Field(min_length=1, max_length=128)
    redistribution: str = Field(min_length=1, max_length=128)
    reviewed_at: str | None = None

    @field_validator("id", "status", "commercial_use", "redistribution")
    @classmethod
    def validate_required_text(cls, value: str, info: Any) -> str:
        return _nonempty(value, f"license {info.field_name}")

    @field_validator("evidence_urls")
    @classmethod
    def validate_evidence_urls(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("license evidence_urls must contain non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("license evidence_urls must be distinct")
        return value

    @field_validator("reviewed_at")
    @classmethod
    def validate_reviewed_at(cls, value: str | None) -> str | None:
        if value is not None:
            return _nonempty(value, "license reviewed_at")
        return None


class ConverterProvenanceV2(Contract):
    id: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    config: dict[str, Any]
    config_sha256: str

    @field_validator("id", "version")
    @classmethod
    def validate_identity(cls, value: str, info: Any) -> str:
        return _nonempty(value, f"converter {info.field_name}")

    @model_validator(mode="after")
    def validate_config_hash(self) -> ConverterProvenanceV2:
        expected = converter_config_sha256(self.config)
        if self.config_sha256 != expected:
            raise ValueError("converter config_sha256 mismatch")
        return self


class ProvenanceV2(Contract):
    source_id: str = Field(min_length=1, max_length=256)
    source_kind: str = Field(min_length=1, max_length=128)
    homepage: str | None = Field(default=None, max_length=4096)
    upstream_revision: str = Field(min_length=1, max_length=512)
    artifacts: list[ProvenanceArtifactV2] = Field(min_length=1)
    artifact_manifest_sha256: str
    license: LicenseProvenanceV2
    converter: ConverterProvenanceV2
    synthetic: bool = Field(strict=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_identity_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for alias, canonical in (("source", "source_id"), ("revision", "upstream_revision")):
            if alias in normalized:
                if canonical in normalized:
                    raise ValueError(f"use only {canonical}, not both {alias} and {canonical}")
                normalized[canonical] = normalized.pop(alias)
        return normalized

    @field_validator("source_id", "source_kind", "upstream_revision")
    @classmethod
    def validate_required_text(cls, value: str, info: Any) -> str:
        return _nonempty(value, f"provenance {info.field_name}")

    @field_validator("homepage")
    @classmethod
    def validate_homepage(cls, value: str | None) -> str | None:
        if value is not None:
            return _nonempty(value, "provenance homepage")
        return None

    @model_validator(mode="after")
    def validate_artifact_manifest(self) -> ProvenanceV2:
        names = [artifact.logical_name for artifact in self.artifacts]
        if len(set(names)) != len(names):
            raise ValueError("provenance artifact logical_name values must be distinct")
        payload = [artifact.model_dump(mode="json") for artifact in self.artifacts]
        expected = _canonical_hash(payload, "artifact manifest")
        if self.artifact_manifest_sha256 != expected:
            raise ValueError("artifact_manifest_sha256 mismatch")
        return self


class DatasetProfileV2(Contract):
    name: str = Field(min_length=1, max_length=128)
    strategy: str = Field(min_length=1, max_length=128)
    count: int = Field(gt=0, strict=True)
    case_ids: list[str] = Field(min_length=1)
    case_ids_sha256: str
    dimensions: list[str] = Field(default_factory=list, max_length=32)
    seed: str | None = Field(default=None, max_length=256)

    @field_validator("name", "strategy")
    @classmethod
    def validate_required_text(cls, value: str, info: Any) -> str:
        return _nonempty(value, f"profile {info.field_name}")

    @field_validator("case_ids")
    @classmethod
    def validate_case_ids(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("profile case_ids must contain non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("profile case_ids must be distinct")
        return value

    @field_validator("dimensions")
    @classmethod
    def validate_dimensions(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("profile dimensions must contain non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("profile dimensions must be distinct")
        return value

    @field_validator("seed")
    @classmethod
    def validate_seed(cls, value: str | None) -> str | None:
        if value is not None:
            return _nonempty(value, "profile seed")
        return None

    @model_validator(mode="after")
    def validate_fixed_ids(self) -> DatasetProfileV2:
        if self.count != len(self.case_ids):
            raise ValueError("profile count must match case_ids")
        if self.case_ids_sha256 != case_ids_sha256(self.case_ids):
            raise ValueError("profile case_ids_sha256 mismatch")
        return self


class EvalSpecV2(Contract):
    suite: Literal["direct-llm"]
    id: Literal["direct-llm-prompts"]
    version: Literal[2]
    selected_count: int = Field(gt=0, strict=True)
    selection: Literal["all-rows-in-file-order"]
    scorer: ScorerSpec
    prompt_version: str = Field(min_length=1, max_length=256)
    max_output_tokens: int = Field(gt=0, strict=True)
    max_retries: int = Field(ge=0, strict=True)

    @field_validator("prompt_version")
    @classmethod
    def validate_prompt_version(cls, value: str) -> str:
        return _nonempty(value, "prompt_version")


class _DatasetPayloadV2(Contract):
    name: str = Field(min_length=1, max_length=64, pattern=r"[0-9A-Za-z._-]+")
    version: str = Field(min_length=1, max_length=64)
    contract_version: Literal[2]
    eval: EvalSpecV2
    provenance: ProvenanceV2
    cases: list[DirectLlmCaseV2] = Field(min_length=1)
    cases_sha256: str
    profiles: list[DatasetProfileV2] = Field(min_length=1)
    profiles_sha256: str

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _nonempty(value, "dataset version")

    @model_validator(mode="after")
    def validate_dataset_content(self) -> _DatasetPayloadV2:
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("duplicate case_id in direct-llm v2 dataset")
        if self.eval.selected_count != len(self.cases):
            raise ValueError("eval selected_count must match cases")
        case_payload = [case.model_dump(mode="json") for case in self.cases]
        if self.cases_sha256 != _canonical_hash(case_payload, "dataset cases"):
            raise ValueError("cases_sha256 mismatch")
        profile_names = [profile.name for profile in self.profiles]
        if len(set(profile_names)) != len(profile_names):
            raise ValueError("dataset profile names must be distinct")
        known_ids = set(case_ids)
        for profile in self.profiles:
            unknown = [case_id for case_id in profile.case_ids if case_id not in known_ids]
            if unknown:
                raise ValueError(f"profile {profile.name!r} references unknown case_id: {unknown[0]!r}")
        profile_payload = [profile.model_dump(mode="json") for profile in self.profiles]
        if self.profiles_sha256 != _canonical_hash(profile_payload, "dataset profiles"):
            raise ValueError("profiles_sha256 mismatch")
        return self


class DirectLlmDatasetV2(_DatasetPayloadV2):
    dataset_fingerprint: str

    @model_validator(mode="after")
    def validate_fingerprint(self) -> DirectLlmDatasetV2:
        if not _HASH.fullmatch(self.dataset_fingerprint):
            raise ValueError("dataset_fingerprint must be a canonical sha256 identity")
        payload = self.model_dump(mode="json", exclude={"dataset_fingerprint"})
        if self.dataset_fingerprint != identity_dataset_fingerprint(payload):
            raise ValueError("dataset_fingerprint mismatch")
        return self


class DirectLlmScenarioV2(Contract):
    name: str = Field(min_length=1, max_length=64, pattern=r"[0-9A-Za-z._-]+")
    version: str = Field(min_length=1, max_length=64)
    mode: Literal["direct-llm"]
    plugin_version: Literal["2"]
    eval: EvalSpecV2
    dataset: str = Field(min_length=3, max_length=256)

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _nonempty(value, "scenario version")

    @field_validator("dataset")
    @classmethod
    def validate_dataset_reference(cls, value: str) -> str:
        name, separator, version = value.rpartition("@")
        if not separator or not name or not version:
            raise ValueError("scenario dataset must be a name@version reference")
        return value


def _eval_of(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    eval_spec = record.get(EVAL_KEY)
    if not isinstance(eval_spec, dict):
        return None
    if (eval_spec.get("suite") != SUITE or eval_spec.get("id") != DATASET_ID
            or eval_spec.get("version") != DATASET_VERSION):
        return None
    return eval_spec


def is_dataset(record: Any) -> bool:
    return _eval_of(record) is not None and not isinstance(record.get("dataset"), str)


def is_scenario(record: Any) -> bool:
    return _eval_of(record) is not None and isinstance(record.get("dataset"), str)


def dataset_fingerprint_v2(record: dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "dataset_fingerprint"}
    normalized = _DatasetPayloadV2.model_validate(payload).model_dump(mode="json")
    return identity_dataset_fingerprint(normalized)


def validate_dataset(record: dict[str, Any]) -> None:
    try:
        DirectLlmDatasetV2.model_validate(record)
    except ValidationError as error:
        raise ValueError(f"invalid direct-llm v2 dataset: {error}") from error


def scenario_for_v2(dataset: dict[str, Any], *, version: str) -> dict[str, Any]:
    validated = DirectLlmDatasetV2.model_validate(dataset)
    scenario = DirectLlmScenarioV2(
        name=validated.name,
        version=version,
        mode="direct-llm",
        plugin_version=PLUGIN_VERSION,
        eval=validated.eval,
        dataset=f"{validated.name}@{validated.version}",
    )
    return scenario.model_dump(mode="json")


def validate_scenario(record: dict[str, Any]) -> None:
    try:
        DirectLlmScenarioV2.model_validate(record)
    except ValidationError as error:
        raise ValueError(f"invalid direct-llm v2 scenario: {error}") from error


# Concise aliases for callers that already carry the module version in their import path.
CaseV2 = DirectLlmCaseV2
ProfileV2 = DatasetProfileV2
ArtifactV2 = ProvenanceArtifactV2
DatasetV2 = DirectLlmDatasetV2
ScenarioV2 = DirectLlmScenarioV2
