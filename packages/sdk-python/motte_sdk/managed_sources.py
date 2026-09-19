"""Unified offline conversion and CLI-only publication orchestration.

This layer deliberately does not own source declarations, fetching, storage, or
benchmark adapters. It coordinates those existing boundaries and fails closed when
a converter or governance condition is unavailable.
"""
from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from motte_contracts.dataset_sources import (
    SourceActionBlockedError,
    SourceOverrideEvidence,
    SourceSpec,
    evaluate_source_action,
    load_source_registry,
    require_source_action,
)
from motte_contracts.direct_llm_v2 import (
    DirectLlmDatasetV2,
    converter_config_sha256,
    scenario_for_v2,
)
from motte_contracts.identity import canonical_sha256
from motte_sdk.dataset_sources import (
    ArtifactManifest,
    SafeFetcher,
    SourcePipelineError,
    artifact_cache_path,
    build_artifact_manifest,
    parse_artifact_bytes,
    read_verified_cache_bytes,
    source_cache_root,
)
from motte_sdk.direct_llm_v2 import normalize_direct_llm_v2_dataset, persist_direct_llm_v2_dataset
from motte_sdk.publication import publication_audit, retarget_publication_audit

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
CLI_ENTRYPOINT = "cli"


class ManagedSourceError(RuntimeError):
    """Structured conversion/preparation failure suitable for CLI/API mapping."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConverterCallable(Protocol):
    def __call__(
        self,
        *,
        artifacts: Mapping[str, Mapping[str, Any]],
        source: SourceSpec,
        revision: str,
        config: Mapping[str, Any],
    ) -> Any: ...


@dataclass(frozen=True)
class ConversionResult:
    """Optional converter envelope for explicit input and rejected-row accounting."""

    dataset: dict[str, Any]
    input_count: int | None = None
    rejected: int = 0


class ConverterRegistry:
    """Fixed converter-id/version registry with lazy built-in adapter resolution."""

    def __init__(self, converters: Mapping[tuple[str, str], ConverterCallable] | None = None) -> None:
        self._converters: dict[tuple[str, str], ConverterCallable] = dict(converters or {})

    def register(self, converter_id: str, version: str, converter: ConverterCallable) -> None:
        self._converters[(converter_id, version)] = converter

    def get(self, converter_id: str, version: str) -> ConverterCallable:
        try:
            return self._converters[(converter_id, version)]
        except KeyError:
            converter = _lazy_builtin_converter(converter_id, version)
            self._converters[(converter_id, version)] = converter
            return converter


@dataclass(frozen=True)
class _CachedArtifact:
    logical_name: str
    path: str
    raw: bytes
    parsed: Any
    sha256: str
    bytes: int
    format: str | None


_SOURCE_KIND_BY_TIER: dict[str, str] = {
    "builtin-smoke": "builtin-smoke",
    "managed-public": "managed-public",
    "generated-internal": "generated-internal",
    "restricted-public": "restricted-public",
    "advanced": "advanced",
}


_SOURCE_KIND_BY_TIER: dict[str, str] = {
    "builtin-smoke": "builtin-smoke",
    "managed-public": "managed-public",
    "generated-internal": "generated-internal",
    "restricted-public": "restricted-public",
    "advanced": "advanced",
}


_BUILTIN_CONVERTER_ALIASES: dict[str, frozenset[str]] = {
    "mmlu-pro-to-direct": frozenset({"mmlu-pro-5shot-cot-to-direct"}),
}


_BUILTIN_LICENSE_PLACEHOLDERS: dict[str, frozenset[str]] = {
    # These adapter values are intentionally non-authoritative. The managed
    # boundary records the explicit mapping before replacing them with SourceSpec evidence.
    "truthfulqa-binary-to-direct": frozenset({"unknown"}),
    "mmlu-pro-to-direct": frozenset({"unresolved-MIT-data-vs-Apache-2.0-code"}),
    "mmlu-pro-5shot-cot-to-direct": frozenset({"unresolved-MIT-data-vs-Apache-2.0-code"}),
    "mmlu-pro-zero-shot-to-direct": frozenset({"unresolved-MIT-data-vs-Apache-2.0-code"}),
}


_BUILTIN_ADAPTERS: dict[str, tuple[str, str]] = {
    "truthfulqa-binary-to-direct": (
        "motte_sdk.adapters.truthfulqa",
        "convert_truthfulqa",
    ),
    "mmlu-pro-to-direct": (
        "motte_sdk.adapters.mmlu_pro",
        "convert_mmlu_pro_5shot_cot",
    ),
    "mmlu-pro-5shot-cot-to-direct": (
        "motte_sdk.adapters.mmlu_pro",
        "convert_mmlu_pro_5shot_cot",
    ),
    "mmlu-pro-zero-shot-to-direct": (
        "motte_sdk.adapters.mmlu_pro",
        "convert_mmlu_pro_zero_shot",
    ),
}


def _lazy_builtin_converter(converter_id: str, version: str) -> ConverterCallable:
    target = _BUILTIN_ADAPTERS.get(converter_id)
    if target is None or version != "1":
        raise ManagedSourceError(
            "CONVERTER_UNAVAILABLE",
            f"converter is not registered: {converter_id}@{version}",
            details={"converter_id": converter_id, "version": version},
        )
    module_name, function_name = target
    try:
        module = importlib.import_module(module_name)
        function = getattr(module, function_name)
    except (ImportError, ModuleNotFoundError, AttributeError) as error:
        raise ManagedSourceError(
            "CONVERTER_UNAVAILABLE",
            f"converter adapter is unavailable: {converter_id}@{version}",
            details={
                "converter_id": converter_id,
                "version": version,
                "module": module_name,
                "function": function_name,
                "error": str(error),
            },
        ) from error
    if not callable(function):
        raise ManagedSourceError(
            "CONVERTER_UNAVAILABLE",
            f"converter adapter is not callable: {converter_id}@{version}",
            details={"module": module_name, "function": function_name},
        )
    return _builtin_wrapper(converter_id, function)


def _builtin_wrapper(converter_id: str, function: Callable[..., Any]) -> ConverterCallable:
    def invoke(
        *,
        artifacts: Mapping[str, Mapping[str, Any]],
        source: SourceSpec,
        revision: str,
        config: Mapping[str, Any],
    ) -> Any:
        common_orchestration = {
            "input_count", "converter_id", "converter_version", "max_bytes", "timeout"
        }
        truth_options = {"expected_rows", "name", "profile_targets", "seed", "version"}
        mmlu_options = {
            "demonstrations_per_category",
            "expected_categories",
            "expected_category_names",
            "expected_test_rows",
            "expected_validation_rows",
            "name",
            "profile_targets",
            "seed",
            "version",
        }
        adapter_options = (
            truth_options if converter_id == "truthfulqa-binary-to-direct" else mmlu_options
        )
        allowed = common_orchestration | adapter_options
        if converter_id != "truthfulqa-binary-to-direct":
            allowed |= {"runner_revision", "test_artifact", "validation_artifact"}
        unsupported = sorted(set(config) - allowed)
        if unsupported:
            raise ManagedSourceError(
                "SOURCE_CONFIG_INVALID",
                "converter config contains fields unsupported by the selected adapter",
                details={"converter_id": converter_id, "fields": unsupported},
            )
        options = {key: value for key, value in config.items() if key in adapter_options}
        if converter_id == "truthfulqa-binary-to-direct":
            item = next(iter(artifacts.values()))
            return function(
                item["raw"],
                revision=revision,
                artifact={
                    "logical_name": item["logical_name"],
                    "url": item["url"],
                    "sha256": item["sha256"],
                    "bytes": item["bytes"],
                },
                **options,
            )
        if converter_id in {
            "mmlu-pro-to-direct",
            "mmlu-pro-5shot-cot-to-direct",
            "mmlu-pro-zero-shot-to-direct",
        }:
            runner_revision = config.get("runner_revision")
            if not isinstance(runner_revision, str):
                raise ManagedSourceError(
                    "SOURCE_CONFIG_INVALID",
                    "MMLU-Pro conversion requires runner_revision",
                )

            def split_rows(split: str) -> Any:
                configured_name = config.get(f"{split}_artifact")
                if configured_name is not None:
                    item = artifacts.get(configured_name)
                    if item is None:
                        raise ManagedSourceError(
                            "SOURCE_CONFIG_INVALID",
                            f"configured {split}_artifact is not cached",
                            details={"logical_name": configured_name},
                        )
                    return item["parsed"]
                candidates = [
                    item for name, item in artifacts.items() if split in name.casefold()
                ]
                if len(candidates) == 1:
                    return candidates[0]["parsed"]
                if len(artifacts) == 1:
                    parsed = next(iter(artifacts.values()))["parsed"]
                    if isinstance(parsed, Mapping):
                        value = parsed.get(split, parsed.get(f"{split}_rows"))
                        if value is not None:
                            return value
                raise ManagedSourceError(
                    "CONVERTER_UNAVAILABLE",
                    f"cannot identify MMLU-Pro {split} artifact by logical name",
                )

            test_rows = split_rows("test")
            validation_rows = split_rows("validation")
            if (
                isinstance(test_rows, (str, bytes, bytearray))
                or not isinstance(test_rows, Sequence)
                or isinstance(validation_rows, (str, bytes, bytearray))
                or not isinstance(validation_rows, Sequence)
            ):
                raise ManagedSourceError(
                    "CONVERTER_FAILED",
                    "MMLU-Pro test and validation artifacts must contain row sequences",
                )
            pinned_artifacts = [
                {
                    "logical_name": item["logical_name"],
                    "url": item["url"],
                    "sha256": item["sha256"],
                    "bytes": item["bytes"],
                }
                for item in artifacts.values()
            ]
            pinned_provenance = {
                "dataset_revision": revision,
                "runner_revision": runner_revision,
                "artifacts": pinned_artifacts,
            }
            dataset = function(
                test_rows,
                validation_rows,
                pinned_provenance=pinned_provenance,
                **options,
            )
            return ConversionResult(dataset=dataset, input_count=len(test_rows))
        raise ManagedSourceError("CONVERTER_UNAVAILABLE", "unsupported built-in converter")

    return invoke


def _source_from_registry(
    source_id: str,
    registry: Mapping[str, SourceSpec] | None,
) -> SourceSpec:
    if registry is not None:
        sources = registry
    else:
        try:
            sources = load_source_registry(
                Path(__file__).resolve().parents[3] / "datasets" / "direct-llm" / "sources"
            )
        except (OSError, ValueError) as error:
            raise ManagedSourceError(
                "SOURCE_REGISTRY_INVALID",
                f"source registry could not be loaded: {error}",
            ) from error
    source = sources.get(source_id)
    if source is None:
        raise ManagedSourceError(
            "SOURCE_NOT_FOUND", f"source is not registered: {source_id}", details={"source_id": source_id}
        )
    return source


def _pipeline_error(error: SourcePipelineError) -> ManagedSourceError:
    return ManagedSourceError(error.code, error.message, details=error.details)


def _read_cached(
    source: SourceSpec,
    revision: str,
    manifest: ArtifactManifest,
    logical_name: str,
    *,
    cache_root: str | Path | None,
) -> _CachedArtifact:
    entry = next(item for item in manifest.artifacts if item.logical_name == logical_name)
    try:
        path = artifact_cache_path(source, revision, logical_name, cache_root=cache_root)
    except SourcePipelineError as error:
        raise _pipeline_error(error) from error
    try:
        raw = read_verified_cache_bytes(
            path,
            entry,
            cache_root=source_cache_root(cache_root),
        )
    except SourcePipelineError as error:
        raise _pipeline_error(error) from error
    artifact = next(item for item in source.upstream.artifacts if item.logical_name == logical_name)
    if not artifact.format:
        raise ManagedSourceError(
            "SOURCE_FORMAT_UNSUPPORTED",
            f"artifact {logical_name!r} has no registered format",
        )
    try:
        parsed = parse_artifact_bytes(raw, artifact.format)
    except SourcePipelineError as error:
        raise _pipeline_error(error) from error
    return _CachedArtifact(
        logical_name=logical_name,
        path=str(path),
        raw=raw,
        parsed=parsed,
        sha256=entry.sha256,
        bytes=entry.bytes,
        format=artifact.format,
    )


def _cached_artifacts(
    source: SourceSpec,
    revision: str,
    *,
    cache_root: str | Path | None,
) -> tuple[ArtifactManifest, dict[str, dict[str, Any]]]:
    try:
        manifest = build_artifact_manifest(source, revision)
    except SourcePipelineError as error:
        raise _pipeline_error(error) from error
    artifacts: dict[str, dict[str, Any]] = {}
    for entry in manifest.artifacts:
        cached = _read_cached(source, revision, manifest, entry.logical_name, cache_root=cache_root)
        source_artifact = next(
            item for item in source.upstream.artifacts if item.logical_name == entry.logical_name
        )
        artifacts[entry.logical_name] = {
            "logical_name": cached.logical_name,
            "path": cached.path,
            "raw": cached.raw,
            "parsed": cached.parsed,
            "sha256": cached.sha256,
            "bytes": cached.bytes,
            "format": cached.format,
            "url": source_artifact.url,
        }
    return manifest, artifacts


def _infer_input_count(artifacts: Mapping[str, Mapping[str, Any]]) -> int | None:
    counts = []
    for item in artifacts.values():
        parsed = item.get("parsed")
        if isinstance(parsed, list):
            counts.append(len(parsed))
        elif isinstance(parsed, Mapping):
            nested = [value for value in parsed.values() if isinstance(value, list)]
            if nested:
                counts.append(sum(len(value) for value in nested))
    return sum(counts) if counts else None


def _converter_result(value: Any) -> ConversionResult:
    if isinstance(value, ConversionResult):
        return value
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], dict):
        metadata = value[1]
        if isinstance(metadata, Mapping):
            return ConversionResult(
                dataset=value[0],
                input_count=metadata.get("input_count"),
                rejected=metadata.get("rejected", 0),
            )
    if not isinstance(value, dict):
        raise ManagedSourceError("CONVERTER_FAILED", "converter did not return a dataset object")
    return ConversionResult(dataset=value)


def _validate_count(result: ConversionResult, inferred_count: int | None, config: Mapping[str, Any]) -> int:
    expected = result.input_count
    if expected is None:
        expected = config.get("input_count", inferred_count)
    if expected is None:
        expected = len(result.dataset.get("cases", []))
    if type(expected) is not int or expected < 0:
        raise ManagedSourceError("CONVERSION_REJECTED", "converter input_count must be a non-negative integer")
    if type(result.rejected) is not int or result.rejected < 0:
        raise ManagedSourceError("CONVERSION_REJECTED", "converter rejected count must be non-negative")
    output_count = len(result.dataset.get("cases", [])) if isinstance(result.dataset, dict) else 0
    if result.rejected != 0 or output_count + result.rejected != expected:
        raise ManagedSourceError(
            "CONVERSION_REJECTED",
            "converter output does not account for every input row",
            details={
                "input_count": expected,
                "output_count": output_count,
                "rejected": result.rejected,
            },
        )
    return expected


def _converter_for(
    converter_registry: ConverterRegistry | Mapping[tuple[str, str], ConverterCallable] | ConverterCallable | None,
    converter_id: str,
    version: str,
) -> ConverterCallable:
    if converter_registry is None:
        return ConverterRegistry().get(converter_id, version)
    if isinstance(converter_registry, ConverterRegistry):
        return converter_registry.get(converter_id, version)
    if callable(converter_registry):
        return converter_registry
    try:
        return converter_registry[(converter_id, version)]
    except KeyError as error:
        raise ManagedSourceError(
            "CONVERTER_UNAVAILABLE",
            f"converter is not registered: {converter_id}@{version}",
            details={"converter_id": converter_id, "version": version},
        ) from error


def _source_license_error(message: str, *, details: Mapping[str, Any] | None = None) -> ManagedSourceError:
    return ManagedSourceError("SOURCE_LICENSE_INVALID", message, details=details)


def _reconcile_source_provenance(
    dataset: dict[str, Any],
    source: SourceSpec,
    converter_id: str,
    converter_version: str,
    resolved_revision: str,
    manifest: ArtifactManifest,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Replace adapter governance claims with the validated SourceSpec snapshot."""
    normalized = deepcopy(dataset)
    provenance = normalized.get("provenance")
    if not isinstance(provenance, dict):
        raise _source_license_error("converter provenance must be an object")
    adapter_license = provenance.get("license")
    if not isinstance(adapter_license, Mapping):
        raise _source_license_error("converter provenance license must be an object")
    adapter_id = adapter_license.get("id")
    if not isinstance(adapter_id, str) or not adapter_id.strip():
        raise _source_license_error("converter provenance license id must be non-empty")

    declared_ids = list(source.license.data.declared_ids)
    if not declared_ids:
        raise _source_license_error(
            "source data license declared_ids are required",
            details={"source_id": source.id},
        )
    declared_by_folded = {value.casefold(): value for value in declared_ids}
    adapter_declared_id = declared_by_folded.get(adapter_id.casefold())
    mapped_placeholder = adapter_declared_id is None
    if mapped_placeholder and adapter_id not in _BUILTIN_LICENSE_PLACEHOLDERS.get(converter_id, frozenset()):
        raise _source_license_error(
            "adapter license id is not declared by the source and has no explicit mapping",
            details={
                "source_id": source.id,
                "converter_id": converter_id,
                "adapter_license_id": adapter_id,
                "declared_ids": declared_ids,
            },
        )

    verified_spdx = source.license.data.verified_spdx
    if verified_spdx is not None and verified_spdx.casefold() not in declared_by_folded:
        raise _source_license_error(
            "source verified SPDX license is not one of its declared data licenses",
            details={"source_id": source.id, "verified_spdx": verified_spdx, "declared_ids": declared_ids},
        )
    if source.governance.status == "approved" and verified_spdx is None:
        raise _source_license_error(
            "approved source requires a verified data SPDX license",
            details={"source_id": source.id, "declared_ids": declared_ids},
        )

    authoritative_id = verified_spdx or adapter_declared_id or declared_ids[0]
    authoritative_license = {
        "id": authoritative_id,
        "status": source.governance.status,
        "evidence_urls": list(source.license.data.evidence_urls),
        "commercial_use": source.license.commercial_use,
        "redistribution": source.license.redistribution,
        "reviewed_at": source.governance.reviewed_at,
    }
    source_kind = _SOURCE_KIND_BY_TIER.get(source.tier)
    if source_kind is None:
        raise _source_license_error(
            "source tier has no provenance source_kind mapping",
            details={"source_id": source.id, "tier": source.tier},
        )
    authoritative_artifacts: list[dict[str, Any]] = []
    for entry in manifest.artifacts:
        item = artifacts.get(entry.logical_name)
        if not isinstance(item, Mapping):
            raise _source_license_error(
                "verified artifact metadata is missing",
                details={"source_id": source.id, "logical_name": entry.logical_name},
            )
        actual = {
            "logical_name": item.get("logical_name"),
            "url": item.get("url"),
            "sha256": item.get("sha256"),
            "bytes": item.get("bytes"),
        }
        expected = {
            "logical_name": entry.logical_name,
            "url": entry.url,
            "sha256": entry.sha256,
            "bytes": entry.bytes,
        }
        if actual != expected:
            raise _source_license_error(
                "verified artifact metadata does not match the manifest",
                details={"source_id": source.id, "logical_name": entry.logical_name},
            )
        authoritative_artifacts.append(expected)
    artifact_manifest_sha256 = canonical_sha256(authoritative_artifacts)

    provenance["source_id"] = source.id
    provenance["source_kind"] = source_kind
    provenance["homepage"] = source.links.homepage
    provenance["upstream_revision"] = resolved_revision
    provenance["artifacts"] = authoritative_artifacts
    provenance["artifact_manifest_sha256"] = artifact_manifest_sha256
    provenance["license"] = authoritative_license
    provenance["synthetic"] = source.tier == "generated-internal"

    converter = provenance.get("converter")
    if not isinstance(converter, dict):
        raise _source_license_error("converter provenance converter must be an object")
    adapter_converter_id = converter.get("id")
    adapter_converter_version = converter.get("version")
    if not isinstance(adapter_converter_id, str) or not isinstance(adapter_converter_version, str):
        raise _source_license_error(
            "adapter converter identity must be non-empty strings",
            details={"source_id": source.id},
        )
    allowed_adapter_ids = {converter_id} | set(_BUILTIN_CONVERTER_ALIASES.get(converter_id, frozenset()))
    if adapter_converter_id not in allowed_adapter_ids or adapter_converter_version != converter_version:
        raise _source_license_error(
            "adapter converter identity is not pinned or explicitly mapped",
            details={
                "source_id": source.id,
                "expected": {"id": converter_id, "version": converter_version},
                "actual": {"id": adapter_converter_id, "version": adapter_converter_version},
            },
        )
    converter["id"] = converter_id
    converter["version"] = converter_version
    converter_config = converter.get("config")
    if not isinstance(converter_config, dict):
        raise _source_license_error("converter provenance config must be an object")
    converter_config = deepcopy(converter_config)
    if "governance_status" in converter_config:
        converter_config["governance_status"] = source.governance.status
    # Preserve the adapter claim and the explicit mapping in the immutable config hash.
    converter_config["license_adapter_id"] = adapter_id
    converter_config["license_authority_id"] = authoritative_id
    converter_config["adapter_converter_id"] = adapter_converter_id
    converter_config["adapter_converter_version"] = adapter_converter_version
    if adapter_converter_id != converter_id:
        converter_config["converter_mapping"] = "builtin-canonical-to-adapter"
    if mapped_placeholder:
        converter_config["license_mapping"] = "builtin-placeholder-to-source-declared-license"
    converter["config"] = converter_config
    converter["config_sha256"] = converter_config_sha256(converter_config)
    provenance["converter"] = converter
    normalized["provenance"] = provenance
    for key in ("cases_sha256", "profiles_sha256", "dataset_fingerprint"):
        normalized.pop(key, None)
    return normalized


def validate_registered_dataset_provenance(
    dataset: Mapping[str, Any], source: SourceSpec,
) -> None:
    """Require published provenance to remain bound to the current SourceSpec."""
    provenance = dataset.get("provenance")
    if not isinstance(provenance, Mapping):
        raise _source_license_error("dataset provenance must be an object")
    source_kind = _SOURCE_KIND_BY_TIER.get(source.tier)
    if source_kind is None:
        raise _source_license_error(
            "source tier has no provenance source_kind mapping",
            details={"source_id": source.id, "tier": source.tier},
        )

    expected: dict[str, Any] = {
        "source_id": source.id,
        "source_kind": source_kind,
        "homepage": source.links.homepage,
        "converter_id": source.conversion.converter.id,
        "converter_version": source.conversion.converter.version,
        "synthetic": source.tier == "generated-internal",
    }
    if source.tier == "generated-internal":
        revision = source.upstream.revision.value
        if revision is None:
            raise _source_license_error(
                "generated-internal source revision is missing",
                details={"source_id": source.id},
            )
        expected["upstream_revision"] = revision
        expected_names = [item.logical_name for item in source.upstream.artifacts]
        actual_artifacts = provenance.get("artifacts")
        actual_names = (
            [item.get("logical_name") for item in actual_artifacts]
            if isinstance(actual_artifacts, list)
            else None
        )
        expected["artifact_names"] = expected_names
        expected["artifact_manifest_sha256"] = provenance.get("artifact_manifest_sha256")
        actual = {
            "source_id": provenance.get("source_id"),
            "source_kind": provenance.get("source_kind"),
            "homepage": provenance.get("homepage"),
            "upstream_revision": provenance.get("upstream_revision"),
            "artifact_names": actual_names,
            "artifact_manifest_sha256": provenance.get("artifact_manifest_sha256"),
            "converter_id": (
                provenance.get("converter", {}).get("id")
                if isinstance(provenance.get("converter"), Mapping)
                else None
            ),
            "converter_version": (
                provenance.get("converter", {}).get("version")
                if isinstance(provenance.get("converter"), Mapping)
                else None
            ),
            "synthetic": provenance.get("synthetic"),
        }
    else:
        try:
            from motte_sdk.dataset_sources import build_artifact_manifest, resolve_revision

            revision = resolve_revision(source)
            manifest = build_artifact_manifest(source, revision)
        except SourcePipelineError as error:
            raise _source_license_error(
                f"current source evidence is not runnable: {error}",
                details={"source_id": source.id, "code": error.code},
            ) from error
        expected_artifacts = [
            {
                "logical_name": item.logical_name,
                "url": item.url,
                "sha256": item.sha256,
                "bytes": item.bytes,
            }
            for item in manifest.artifacts
        ]
        expected.update({
            "upstream_revision": revision,
            "artifacts": expected_artifacts,
            "artifact_manifest_sha256": canonical_sha256(expected_artifacts),
        })
        converter = provenance.get("converter")
        actual = {
            "source_id": provenance.get("source_id"),
            "source_kind": provenance.get("source_kind"),
            "homepage": provenance.get("homepage"),
            "upstream_revision": provenance.get("upstream_revision"),
            "artifacts": provenance.get("artifacts"),
            "artifact_manifest_sha256": provenance.get("artifact_manifest_sha256"),
            "converter_id": converter.get("id") if isinstance(converter, Mapping) else None,
            "converter_version": converter.get("version") if isinstance(converter, Mapping) else None,
            "synthetic": provenance.get("synthetic"),
        }
    mismatches = [key for key, value in expected.items() if actual.get(key) != value]
    if mismatches:
        raise _source_license_error(
            "published dataset provenance does not match the current SourceSpec",
            details={"source_id": source.id, "mismatches": mismatches},
        )


def _governance_receipt(
    source: SourceSpec,
    revision: str,
    manifest: ArtifactManifest,
    artifacts: Mapping[str, Mapping[str, Any]],
    converter_id: str,
    converter_version: str,
    config: Mapping[str, Any],
    dataset: dict[str, Any],
    input_count: int,
    allow_experimental: bool,
) -> dict[str, Any]:
    profiles = dataset["profiles"]
    publish_decision = evaluate_source_action(source, "publish")
    return {
        "source": source.id,
        "source_id": source.id,
        "revision": revision,
        "artifact_manifest_sha256": manifest.sha256,
        "artifact_paths": {name: item["path"] for name, item in artifacts.items()},
        "converter": {
            "id": converter_id,
            "version": converter_version,
            "config_sha256": converter_config_sha256(dict(config)),
        },
        "converter_config_sha256": converter_config_sha256(dict(config)),
        "input": {"artifacts": len(artifacts), "rows": input_count},
        "output": {
            "cases": len(dataset["cases"]),
            "profiles": len(profiles),
            "cases_sha256": dataset["cases_sha256"],
        },
        "rejected": 0,
        "profile_hashes": {profile["name"]: profile["case_ids_sha256"] for profile in profiles},
        "dataset_fingerprint": dataset["dataset_fingerprint"],
        "governance": {
            "status": source.governance.status,
            "allow_experimental": allow_experimental,
            "publishable": publish_decision.allowed,
        },
    }


def convert_cached_source(
    source_id: str,
    revision: str,
    config: Mapping[str, Any],
    allow_experimental: bool,
    *,
    registry: Mapping[str, SourceSpec] | None = None,
    cache_root: str | Path | None = None,
    converter_registry: ConverterRegistry | Mapping[tuple[str, str], ConverterCallable] | ConverterCallable | None = None,
) -> dict[str, Any]:
    """Convert verified local cache bytes without network or publication side effects."""
    if type(allow_experimental) is not bool:
        raise ManagedSourceError("SOURCE_CONFIG_INVALID", "allow_experimental must be a boolean")
    if not isinstance(config, Mapping):
        raise ManagedSourceError("SOURCE_CONFIG_INVALID", "converter config must be an object")
    source = _source_from_registry(source_id, registry)
    publish_decision = evaluate_source_action(source, "publish")
    if not publish_decision.allowed and not allow_experimental:
        raise ManagedSourceError(
            "SOURCE_LICENSE_BLOCKED",
            "source is not publish-ready; local experimental conversion requires allow_experimental=true",
            details={
                "source_id": source.id,
                "status": source.governance.status,
                "action": "convert",
                "publish_decision_code": publish_decision.code,
                "publish_decision_reasons": list(publish_decision.reasons),
            },
        )
    try:
        from motte_sdk.dataset_sources import resolve_revision

        resolved_revision = resolve_revision(source, revision)
    except SourcePipelineError as error:
        raise _pipeline_error(error) from error
    converter_id = source.conversion.converter.id
    converter_version = source.conversion.converter.version
    configured_converter_id = config.get("converter_id")
    configured_converter_version = config.get("converter_version")
    if configured_converter_id is not None and configured_converter_id != converter_id:
        raise ManagedSourceError(
            "SOURCE_CONFIG_INVALID",
            "converter_id does not match the source manifest",
            details={"expected": converter_id, "actual": configured_converter_id},
        )
    if configured_converter_version is not None and configured_converter_version != converter_version:
        raise ManagedSourceError(
            "SOURCE_CONFIG_INVALID",
            "converter_version does not match the source manifest",
            details={"expected": converter_version, "actual": configured_converter_version},
        )
    if converter_version is None:
        raise ManagedSourceError(
            "CONVERTER_UNAVAILABLE",
            f"source converter version is not pinned: {converter_id}",
            details={"converter_id": converter_id},
        )
    manifest, artifacts = _cached_artifacts(source, resolved_revision, cache_root=cache_root)
    converter = _converter_for(converter_registry, converter_id, converter_version)
    try:
        converted = converter(
            artifacts=artifacts,
            source=source,
            revision=resolved_revision,
            config=dict(config),
        )
    except ManagedSourceError:
        raise
    except Exception as error:
        raise ManagedSourceError(
            "CONVERTER_FAILED",
            f"converter failed: {error}",
            details={"converter_id": converter_id, "version": converter_version},
        ) from error
    result = _converter_result(converted)
    input_count = _validate_count(result, _infer_input_count(artifacts), config)
    try:
        adapter_payload = deepcopy(result.dataset)
        adapter_payload.pop("dataset_fingerprint", None)
        adapter_dataset = normalize_direct_llm_v2_dataset(adapter_payload)
        DirectLlmDatasetV2.model_validate(adapter_dataset)
    except Exception as error:
        raise ManagedSourceError(
            "DATASET_INVALID", f"converter returned an invalid Direct LLM v2 dataset: {error}",
            details={"converter_id": converter_id, "version": converter_version},
        ) from error
    try:
        dataset = _reconcile_source_provenance(
            adapter_dataset,
            source,
            converter_id,
            converter_version,
            resolved_revision,
            manifest,
            artifacts,
        )
        dataset = normalize_direct_llm_v2_dataset(dataset)
        DirectLlmDatasetV2.model_validate(dataset)
    except ManagedSourceError:
        raise
    except Exception as error:
        raise ManagedSourceError(
            "DATASET_INVALID", f"converter returned an invalid Direct LLM v2 dataset: {error}",
            details={"converter_id": converter_id, "version": converter_version},
        ) from error
    return _governance_receipt(
        source,
        resolved_revision,
        manifest,
        artifacts,
        converter_id,
        converter_version,
        config,
        dataset,
        input_count,
        allow_experimental,
    ) | {"dataset": dataset}


def _clock_text(clock: Callable[[], Any] | None) -> str:
    value = clock() if clock is not None else datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ManagedSourceError("SOURCE_CONFIG_INVALID", "clock must return a non-empty timestamp")


def _require_action(
    source: SourceSpec,
    action: str,
    override: Any,
    *,
    override_verified: bool = False,
) -> None:
    try:
        require_source_action(
            source,
            action,
            override=override,
            override_verified=override_verified,
        )
    except SourceActionBlockedError as error:
        decision = error.decision
        raise ManagedSourceError(
            decision.code or "SOURCE_ACTION_BLOCKED",
            "; ".join(decision.reasons),
            details={"source_id": source.id, "action": action},
        ) from error


def _publish_converted_receipt(
    receipt: dict[str, Any],
    config: Mapping[str, Any],
    *,
    resources: Any,
    actor: str,
    entrypoint: str,
    clock: Callable[[], Any] | None,
) -> dict[str, Any]:
    if resources is None:
        raise ManagedSourceError("PUBLICATION_FAILED", "resources are required for publication")
    if not receipt.get("governance", {}).get("publishable", False):
        raise ManagedSourceError(
            "SOURCE_LICENSE_BLOCKED",
            "source conversion is not publishable under current governance",
            details={"source_id": receipt.get("source_id"), "governance": receipt.get("governance")},
        )
    working = deepcopy(receipt)
    dataset = working.pop("dataset")
    explicit_version = config.get("version") if "version" in config else None
    if explicit_version is not None and (
        not isinstance(explicit_version, str) or not explicit_version.strip()
    ):
        raise ManagedSourceError("SOURCE_CONFIG_INVALID", "version must be a non-empty string")
    initial_version = explicit_version or dataset.get("version", "1")
    if not isinstance(initial_version, str) or not initial_version.strip():
        raise ManagedSourceError("SOURCE_CONFIG_INVALID", "dataset version must be non-empty")
    dataset["version"] = initial_version
    try:
        dataset = normalize_direct_llm_v2_dataset(dataset)
        initial_scenario = scenario_for_v2(dataset, version=initial_version)
        initial_audit = publication_audit(
            dataset,
            initial_scenario,
            working,
            actor=actor,
            entrypoint=entrypoint,
            published_at=_clock_text(clock),
        )
        published = persist_direct_llm_v2_dataset(
            dataset,
            resources,
            version=explicit_version,
            publication=initial_audit,
        )
        dataset_name, _, target_version = published["imported"].rpartition("@")
        scenario_name, _, scenario_version = published["scenario"].rpartition("@")
        dataset = resources.datasets.get(dataset_name, target_version)
        scenario = resources.scenarios.get(scenario_name, scenario_version)
        if dataset is None or scenario is None:
            raise ValueError("published dataset/scenario bundle is unavailable")
        expected_audit = retarget_publication_audit(initial_audit, dataset, scenario)
        audit = resources.publications.get(expected_audit["id"])
        if audit is None:
            raise ValueError("published audit is unavailable")
    except ManagedSourceError:
        raise
    except Exception as error:
        raise ManagedSourceError("PUBLICATION_FAILED", f"atomic publication failed: {error}") from error
    working["publication_audit"] = audit
    working["publication_id"] = audit["id"]
    working["publishable"] = True
    working["dataset"] = dataset
    return working


def prepare_source(
    source_id: str,
    revision: str,
    config: Mapping[str, Any],
    *,
    resources: Any,
    actor: str,
    entrypoint: str = CLI_ENTRYPOINT,
    registry: Mapping[str, SourceSpec] | None = None,
    cache_root: str | Path | None = None,
    transport: Any = None,
    fetcher: SafeFetcher | None = None,
    converter_registry: ConverterRegistry | Mapping[tuple[str, str], ConverterCallable] | ConverterCallable | None = None,
    clock: Callable[[], Any] | None = None,
    override: SourceOverrideEvidence | Mapping[str, Any] | None = None,
    override_verified: bool = False,
) -> dict[str, Any]:
    """CLI-only fetch, convert, audit, and atomic v2 publication."""
    if entrypoint != CLI_ENTRYPOINT:
        raise ManagedSourceError("SOURCE_NETWORK_DISABLED", "prepare_source requires the CLI entrypoint")
    if override_verified:
        raise ManagedSourceError(
            "SOURCE_OVERRIDE_UNVERIFIED",
            "the CLI cannot self-report trusted override verification",
        )
    if not isinstance(config, Mapping):
        raise ManagedSourceError("SOURCE_CONFIG_INVALID", "prepare config must be an object")
    source = _source_from_registry(source_id, registry)
    if source.governance.status == "restricted" and not (override_verified and override is not None):
        raise ManagedSourceError(
            "SOURCE_OVERRIDE_UNVERIFIED",
            "restricted source requires verified override evidence",
            details={"source_id": source.id},
        )
    for action in ("fetch", "import", "publish"):
        _require_action(source, action, override, override_verified=override_verified)
    if resources is None:
        raise ManagedSourceError("PUBLICATION_FAILED", "resources are required for publication")
    effective_fetcher = fetcher or SafeFetcher(cache_root=cache_root, transport=transport)
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    configured_max_bytes = config.get("max_bytes")
    try:
        from motte_sdk.dataset_sources import resolve_revision

        resolved_revision = resolve_revision(source, revision)
    except SourcePipelineError as error:
        raise _pipeline_error(error) from error
    try:
        for artifact in source.upstream.artifacts:
            if not artifact.required:
                continue
            artifact_max_bytes = (
                configured_max_bytes if configured_max_bytes is not None else artifact.max_bytes
            )
            if artifact_max_bytes is None:
                raise SourcePipelineError(
                    "SOURCE_NOT_READY",
                    f"artifact {artifact.logical_name!r} has no max_bytes",
                )
            effective_fetcher.fetch(
                source,
                resolved_revision,
                artifact.logical_name,
                entrypoint=CLI_ENTRYPOINT,
                timeout=timeout,
                max_bytes=artifact_max_bytes,
                override=override,
            )
    except SourcePipelineError as error:
        raise _pipeline_error(error) from error
    receipt = convert_cached_source(
        source_id,
        resolved_revision,
        config,
        allow_experimental=False,
        registry=registry,
        cache_root=cache_root,
        converter_registry=converter_registry,
    )
    return _publish_converted_receipt(
        receipt,
        config,
        resources=resources,
        actor=actor,
        entrypoint=entrypoint,
        clock=clock,
    )


def import_cached_source(
    source_id: str,
    revision: str,
    config: Mapping[str, Any],
    *,
    resources: Any,
    actor: str,
    entrypoint: str = CLI_ENTRYPOINT,
    registry: Mapping[str, SourceSpec] | None = None,
    cache_root: str | Path | None = None,
    converter_registry: ConverterRegistry | Mapping[tuple[str, str], ConverterCallable] | ConverterCallable | None = None,
    clock: Callable[[], Any] | None = None,
    override: SourceOverrideEvidence | Mapping[str, Any] | None = None,
    override_verified: bool = False,
) -> dict[str, Any]:
    """CLI-only import of an already cached artifact; never fetches network data."""
    if entrypoint != CLI_ENTRYPOINT:
        raise ManagedSourceError("SOURCE_NETWORK_DISABLED", "import_cached_source requires the CLI entrypoint")
    if override_verified:
        raise ManagedSourceError(
            "SOURCE_OVERRIDE_UNVERIFIED",
            "the CLI cannot self-report trusted override verification",
        )
    if not isinstance(config, Mapping):
        raise ManagedSourceError("SOURCE_CONFIG_INVALID", "import config must be an object")
    source = _source_from_registry(source_id, registry)
    if source.governance.status == "restricted":
        raise ManagedSourceError(
            "SOURCE_OVERRIDE_UNVERIFIED",
            "restricted source requires verified override evidence",
            details={"source_id": source.id},
        )
    for action in ("import", "publish"):
        _require_action(source, action, override, override_verified=override_verified)
    receipt = convert_cached_source(
        source_id,
        revision,
        config,
        allow_experimental=False,
        registry=registry,
        cache_root=cache_root,
        converter_registry=converter_registry,
    )
    return _publish_converted_receipt(
        receipt,
        config,
        resources=resources,
        actor=actor,
        entrypoint=entrypoint,
        clock=clock,
    )


__all__ = [
    "CLI_ENTRYPOINT",
    "ConversionResult",
    "ConverterCallable",
    "ConverterRegistry",
    "ManagedSourceError",
    "convert_cached_source",
    "import_cached_source",
    "prepare_source",
]
