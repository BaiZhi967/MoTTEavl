"""Direct LLM v2 dataset publication, selected-only snapshots, and scoring adapters.

This module performs no network I/O. Creation reads an immutable dataset resource
once; retry and rescore consume only the persisted selected-case snapshot.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from motte_contracts.direct_llm_v2 import (
    CONTRACT_VERSION,
    DATASET_VERSION,
    EVAL_KEY,
    PLUGIN_VERSION,
    SUITE,
    CaseMetadataV2,
    ConverterProvenanceV2,
    DatasetProfileV2,
    DirectLlmCaseV2,
    DirectLlmDatasetV2,
    ProvenanceArtifactV2,
    ProvenanceV2,
    ScorerSpec,
    case_ids_sha256,
    converter_config_sha256,
    dataset_fingerprint_v2,
    scenario_for_v2,
    scorer_config_sha256,
    validate_scenario,
)
from motte_contracts.identity import canonical_sha256
from motte_contracts.selection import CASE_SELECTION_KEY, RUN_SELECTION_KEY, select_cases
from motte_sdk.datasets import next_dataset_version

SNAPSHOT_KEY = "benchmark_snapshot"
PROVENANCE_KEY = "benchmark_provenance"
PROFILE_KEY = "profile"
RESERVED_KEYS = frozenset({SNAPSHOT_KEY, PROVENANCE_KEY, "benchmark_cases", "tools", "cases"})
MAX_AUTO_VERSION_ATTEMPTS = 256
MIXED_SCORER_ID = "direct-llm-deterministic"
MIXED_SCORER_VERSION = "2"
SNAPSHOT_CONTENT_HASH_KEY = "selected_content_sha256"


class DirectLlmV2SnapshotIntegrityError(ValueError):
    """A persisted v2 snapshot is missing or no longer matches its content identity."""

    code = "DIRECT_LLM_V2_SNAPSHOT_INTEGRITY"

    def __init__(self, reason: str) -> None:
        super().__init__(f"direct-llm v2 snapshot integrity check failed: {reason}")
        self.evidence = {"code": self.code, "reason": reason}


def _snapshot_content_sha256(snapshot: dict[str, Any]) -> str:
    """Bind selected case content to its selection and dataset/scorer identity."""
    payload = {
        key: deepcopy(value)
        for key, value in snapshot.items()
        if key != SNAPSHOT_CONTENT_HASH_KEY
    }
    return canonical_sha256(payload)


def _checked_derived_hash(
    record: dict[str, Any], key: str, expected: str, label: str,
) -> dict[str, Any]:
    supplied = record.get(key)
    if supplied is not None and supplied != expected:
        raise ValueError(f"{label} mismatch")
    record[key] = expected
    return record


def _normalize_scorer(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("v2 scorer must be a full scorer spec object")
    normalized = deepcopy(value)
    config = normalized.get("config")
    if not isinstance(config, dict):
        raise ValueError("v2 scorer config must be an object")
    scorer_id = normalized.get("id")
    version = normalized.get("version")
    if not isinstance(scorer_id, str) or not isinstance(version, str):
        raise ValueError("v2 scorer id and version must be strings")
    from motte_eval.scorer_registry import get_scorer

    normalized_config = get_scorer(scorer_id, version).normalize_config(config)
    normalized["config"] = normalized_config
    _checked_derived_hash(
        normalized, "config_sha256", scorer_config_sha256(normalized_config),
        "scorer config_sha256",
    )
    return ScorerSpec.model_validate(normalized).model_dump(mode="json")


def _normalize_case(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("v2 cases must be objects")
    normalized = deepcopy(value)
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict) and metadata.get("scorer") is not None:
        normalized["metadata"] = {
            **metadata,
            "scorer": _normalize_scorer(metadata["scorer"]),
        }
    if normalized.get("scorer") is not None:
        normalized["scorer"] = _normalize_scorer(normalized["scorer"])
    return DirectLlmCaseV2.model_validate(normalized).model_dump(mode="json")


def _normalize_provenance(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("v2 provenance must be an object")
    normalized = deepcopy(value)
    artifacts = normalized.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("v2 provenance artifacts must be a list")
    normalized_artifacts = [
        ProvenanceArtifactV2.model_validate(item).model_dump(mode="json") for item in artifacts
    ]
    normalized["artifacts"] = normalized_artifacts
    _checked_derived_hash(
        normalized,
        "artifact_manifest_sha256",
        canonical_sha256(normalized_artifacts),
        "artifact_manifest_sha256",
    )
    converter = normalized.get("converter")
    if not isinstance(converter, dict):
        raise ValueError("v2 provenance converter must be an object")
    normalized_converter = deepcopy(converter)
    config = normalized_converter.get("config")
    if not isinstance(config, dict):
        raise ValueError("v2 converter config must be an object")
    _checked_derived_hash(
        normalized_converter,
        "config_sha256",
        converter_config_sha256(config),
        "converter config_sha256",
    )
    normalized["converter"] = ConverterProvenanceV2.model_validate(
        normalized_converter
    ).model_dump(mode="json")
    return ProvenanceV2.model_validate(normalized).model_dump(mode="json")


def _normalize_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("v2 profiles must be objects")
    normalized = deepcopy(value)
    case_ids = normalized.get("case_ids")
    if not isinstance(case_ids, list):
        raise ValueError("v2 profile case_ids must be a list")
    normalized.setdefault("count", len(case_ids))
    _checked_derived_hash(
        normalized, "case_ids_sha256", case_ids_sha256(case_ids),
        "profile case_ids_sha256",
    )
    return DatasetProfileV2.model_validate(normalized).model_dump(mode="json")


def normalize_direct_llm_v2_dataset(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize aliases and fill derived hashes, rejecting any conflicting supplied hash."""
    if not isinstance(record, dict):
        raise ValueError("v2 dataset must be an object")
    normalized = deepcopy(record)
    eval_spec = normalized.get(EVAL_KEY)
    if not isinstance(eval_spec, dict):
        raise ValueError("v2 dataset eval must be an object")
    normalized_eval = deepcopy(eval_spec)
    normalized_eval["scorer"] = _normalize_scorer(normalized_eval.get("scorer"))
    normalized[EVAL_KEY] = normalized_eval
    cases = normalized.get("cases")
    profiles = normalized.get("profiles")
    if not isinstance(cases, list) or not isinstance(profiles, list):
        raise ValueError("v2 dataset cases and profiles must be lists")
    normalized_cases = [_normalize_case(case) for case in cases]
    from motte_eval.direct_llm_v2 import validate_expected

    for case in normalized_cases:
        expected = case.get("expected")
        if expected is None:
            continue
        scorer = (case["metadata"].get("scorer") or normalized_eval["scorer"])
        validate_expected(expected, scorer)
    normalized_profiles = [_normalize_profile(profile) for profile in profiles]
    normalized["cases"] = normalized_cases
    normalized["profiles"] = normalized_profiles
    normalized["provenance"] = _normalize_provenance(normalized.get("provenance"))
    _checked_derived_hash(
        normalized, "cases_sha256", canonical_sha256(normalized_cases), "cases_sha256",
    )
    _checked_derived_hash(
        normalized, "profiles_sha256", canonical_sha256(normalized_profiles), "profiles_sha256",
    )
    supplied_fingerprint = normalized.pop("dataset_fingerprint", None)
    fingerprint = dataset_fingerprint_v2(normalized)
    if supplied_fingerprint is not None and supplied_fingerprint != fingerprint:
        raise ValueError("dataset_fingerprint mismatch")
    normalized["dataset_fingerprint"] = fingerprint
    return DirectLlmDatasetV2.model_validate(normalized).model_dump(mode="json")


def _publication_for_target(
    publication: dict[str, Any] | None,
    dataset: dict[str, Any],
    scenario: dict[str, Any],
    *,
    allow_retarget: bool,
) -> dict[str, Any] | None:
    if publication is None:
        return None
    from motte_sdk.publication import retarget_publication_audit, validate_publication_audit

    validated = validate_publication_audit(publication)
    target = (
        f"{dataset['name']}@{dataset['version']}",
        f"{scenario['name']}@{scenario['version']}",
        dataset["dataset_fingerprint"],
    )
    current = (
        validated["dataset"], validated["scenario"], validated["dataset_fingerprint"],
    )
    if current == target:
        return validated
    if not allow_retarget:
        raise ValueError("publication audit does not match the explicit publication target")
    return retarget_publication_audit(validated, dataset, scenario)


def persist_direct_llm_v2_dataset(
    record: dict[str, Any], resources: Any, *, version: str | None = None,
    publication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically publish a normalized v2 dataset/scenario with auto-version retries."""
    from motte_storage.resource_store import ResourceConflictError

    candidate = normalize_direct_llm_v2_dataset(record)
    license_status = candidate["provenance"]["license"]["status"]
    if license_status not in {"approved", "approved-internal", "approved-test-only"}:
        raise ValueError(
            "SOURCE_LICENSE_BLOCKED: only approved datasets may be published; "
            "pending/restricted datasets must use the managed source publication pipeline"
        )
    fingerprint = candidate["dataset_fingerprint"]
    auto_version = version is None
    if publication is not None:
        original_scenario = scenario_for_v2(candidate, version=candidate["version"])
        _publication_for_target(
            publication, candidate, original_scenario, allow_retarget=False,
        )
    retry_version: str | None = None
    for _ in range(MAX_AUTO_VERSION_ATTEMPTS):
        target_version = version or retry_version or next_dataset_version(candidate, resources)
        existing = resources.datasets.get(candidate["name"], target_version)
        published = (
            existing
            if existing is not None and existing.get("dataset_fingerprint") == fingerprint
            else {**candidate, "version": target_version}
        )
        published = normalize_direct_llm_v2_dataset(published)
        scenario = scenario_for_v2(published, version=target_version)
        audit = _publication_for_target(
            publication, published, scenario, allow_retarget=auto_version,
        )
        if audit is not None:
            existing_audit = resources.publications.get(audit["id"])
            if existing_audit is not None:
                audit = existing_audit
        try:
            resources.publish_dataset_scenario(published, scenario, publication=audit)
        except ResourceConflictError:
            if not auto_version:
                raise
            occupied = [
                int(item["version"])
                for repository in (resources.datasets, resources.scenarios)
                for item in repository.list()
                if item.get("name") == candidate["name"]
                and isinstance(item.get("version"), str)
                and item["version"].isdigit()
            ]
            retry_version = str(max(occupied, default=0) + 1)
            continue
        return {
            "imported": f"{published['name']}@{target_version}",
            "scenario": f"{scenario['name']}@{scenario['version']}",
            "suite": SUITE,
            "plugin_version": PLUGIN_VERSION,
            "cases": len(published["cases"]),
            "source": published["provenance"]["source_id"],
            "cases_sha256": published["cases_sha256"],
            "dataset_fingerprint": fingerprint,
        }
    raise ResourceConflictError("could not allocate an atomic direct-llm v2 dataset/scenario version")


def import_direct_llm_v2_dataset(
    record: dict[str, Any], *, resources: Any, version: str | None = None,
    publication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return persist_direct_llm_v2_dataset(
        record, resources, version=version, publication=publication,
    )


def _profile_selection(dataset: dict[str, Any], profile_name: Any) -> dict[str, Any]:
    if not isinstance(profile_name, str) or not profile_name.strip():
        raise ValueError("profile must be a non-empty string")
    profile = next(
        (item for item in dataset["profiles"] if item["name"] == profile_name), None,
    )
    if profile is None:
        raise ValueError(f"unknown direct-llm v2 profile: {profile_name}")
    return {
        "mode": "profile",
        "profile": profile_name,
        "count": profile["count"],
        "case_ids": list(profile["case_ids"]),
        "seed": profile.get("seed"),
        "case_ids_sha256": profile["case_ids_sha256"],
    }


def _run_selection(dataset: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    explicit_profile = manifest.get(PROFILE_KEY)
    requested = manifest.get(CASE_SELECTION_KEY)
    if explicit_profile is not None and requested is not None:
        raise ValueError("profile and case_selection are mutually exclusive")
    if explicit_profile is not None:
        return _profile_selection(dataset, explicit_profile)
    if isinstance(requested, dict) and requested.get("mode") == "profile":
        unknown = set(requested) - {"mode", "profile"}
        if unknown:
            raise ValueError("profile selection cannot include ids, random count, or seed")
        return _profile_selection(dataset, requested.get("profile"))
    selected = select_cases(dataset, requested)
    selected["case_ids_sha256"] = case_ids_sha256(selected["case_ids"])
    return selected


def _compact_selection(selection: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in selection.items() if key != "case_ids"}


def _require_runnable_publication(
    scenario: dict[str, Any], dataset: dict[str, Any], resources: Any,
) -> None:
    allowed = {"approved", "approved-internal", "approved-test-only"}
    status = dataset["provenance"]["license"]["status"]
    if status not in allowed:
        raise ValueError(
            "SOURCE_LICENSE_BLOCKED: direct-llm v2 runs require an approved license status"
        )
    provenance = dataset["provenance"]
    source_id = provenance.get("source_id")
    from motte_contracts.dataset_sources import SourceActionBlockedError, require_source_action
    from motte_sdk.dataset_sources import SourcePipelineError, load_registry

    try:
        registered_source = load_registry().get(source_id) if isinstance(source_id, str) else None
    except SourcePipelineError as error:
        raise ValueError(f"SOURCE_NOT_READY: source registry is unavailable: {error}") from error
    test_only_fixture = (
        status == "approved-test-only"
        and provenance.get("synthetic") is True
        and provenance.get("source_kind") == "synthetic-test"
    )
    if registered_source is None and not test_only_fixture:
        raise ValueError(f"SOURCE_NOT_FOUND: current SourceSpec is unavailable: {source_id!r}")
    if registered_source is not None:
        try:
            if registered_source.governance.status == "approved-internal":
                require_source_action(
                    registered_source, "generate", local=True, internal_scope=True,
                )
            else:
                require_source_action(registered_source, "publish")
        except SourceActionBlockedError as error:
            reasons = "; ".join(error.decision.reasons)
            raise ValueError(
                f"{error.decision.code or 'SOURCE_NOT_READY'}: "
                f"current SourceSpec blocks new runs: {reasons}"
            ) from error
        from motte_sdk.managed_sources import (
            ManagedSourceError,
            validate_registered_dataset_provenance,
        )

        try:
            validate_registered_dataset_provenance(dataset, registered_source)
        except ManagedSourceError as error:
            raise ValueError(f"{error.code}: {error}") from error
    publications = getattr(resources, "publications", None)
    if publications is None or not callable(getattr(publications, "list", None)):
        raise ValueError("PUBLICATION_REQUIRED: matching publication audit is unavailable")
    from motte_sdk.publication import validate_publication_audit

    dataset_ref = scenario["dataset"]
    scenario_ref = f"{scenario['name']}@{scenario['version']}"
    fingerprint = dataset["dataset_fingerprint"]
    for publication in publications.list():
        try:
            validated = validate_publication_audit(publication)
        except ValueError:
            continue
        if (
            validated["dataset"] == dataset_ref
            and validated["scenario"] == scenario_ref
            and validated["dataset_fingerprint"] == fingerprint
        ):
            return
    raise ValueError("PUBLICATION_REQUIRED: no matching publication audit for direct-llm v2 run")


def require_runnable_direct_llm_v2_snapshot(
    run: dict[str, Any], resources: Any,
) -> None:
    """Recheck current governance without rebuilding a persisted selected snapshot."""
    manifest = run.get("manifest")
    snapshot = manifest.get(SNAPSHOT_KEY) if isinstance(manifest, dict) else None
    provenance = manifest.get(PROVENANCE_KEY) if isinstance(manifest, dict) else None
    snapshot_dataset = snapshot.get("dataset") if isinstance(snapshot, dict) else None
    snapshot_scenario = snapshot.get("scenario") if isinstance(snapshot, dict) else None
    if not all(isinstance(value, dict) for value in (
        snapshot, provenance, snapshot_dataset, snapshot_scenario,
    )):
        raise ValueError("SNAPSHOT_INVALID: direct-llm v2 run identity is missing")
    scenario_ref = run.get("scenario_version")
    if not isinstance(scenario_ref, str) or "@" not in scenario_ref:
        raise ValueError("SNAPSHOT_INVALID: run scenario identity is missing")
    scenario_name, scenario_version = scenario_ref.rsplit("@", 1)
    scenario = resources.scenarios.get(scenario_name, scenario_version)
    if scenario is None:
        raise ValueError(f"SCENARIO_NOT_FOUND: {scenario_ref}")
    validate_scenario(scenario)
    expected_scenario = {
        "name": scenario_name,
        "version": scenario_version,
        "plugin_version": PLUGIN_VERSION,
    }
    if snapshot_scenario != expected_scenario:
        raise ValueError("SNAPSHOT_INVALID: scenario identity does not match the parent run")
    dataset_ref = scenario["dataset"]
    if snapshot_dataset.get("ref") != dataset_ref or provenance.get("dataset") != dataset_ref:
        raise ValueError("SNAPSHOT_INVALID: dataset reference does not match the scenario")
    dataset_name, separator, dataset_version = dataset_ref.rpartition("@")
    if not separator:
        raise ValueError("SNAPSHOT_INVALID: scenario dataset reference is invalid")
    stored = resources.datasets.get(dataset_name, dataset_version)
    if stored is None:
        raise ValueError(f"DATASET_NOT_FOUND: {dataset_ref}")
    dataset = normalize_direct_llm_v2_dataset(stored)
    fingerprint = dataset["dataset_fingerprint"]
    if (
        snapshot_dataset.get("fingerprint") != fingerprint
        or provenance.get("dataset_fingerprint") != fingerprint
        or snapshot_dataset.get("cases_sha256") != dataset["cases_sha256"]
        or provenance.get("cases_sha256") != dataset["cases_sha256"]
    ):
        raise ValueError("SNAPSHOT_DATASET_MISMATCH: stored dataset identity has changed")
    _require_runnable_publication(scenario, dataset, resources)


def resolve_direct_llm_v2_manifest(
    scenario: dict[str, Any], manifest: dict[str, Any], resources: Any,
) -> dict[str, Any]:
    """Resolve a v2 scenario into a selected-only, replay-safe run snapshot."""
    validate_scenario(scenario)
    name, separator, version = str(scenario["dataset"]).rpartition("@")
    if not separator:
        raise ValueError("scenario dataset must be a name@version reference")
    stored = resources.datasets.get(name, version)
    if stored is None:
        raise ValueError(f"dataset not found: {scenario['dataset']}")
    dataset = normalize_direct_llm_v2_dataset(stored)
    _require_runnable_publication(scenario, dataset, resources)
    if RESERVED_KEYS.intersection(manifest):
        raise ValueError("direct-llm v2 cases, tools and snapshots cannot be overridden")
    if manifest.get("dataset", scenario["dataset"]) != scenario["dataset"]:
        raise ValueError("manifest dataset does not match scenario")
    preset_budget = dataset[EVAL_KEY]["max_output_tokens"]
    budget = (manifest.get("parameters") or {}).get("max_output_tokens")
    if budget is not None and (type(budget) is not int or budget <= 0):
        raise ValueError("parameters.max_output_tokens must be a positive integer")

    selection = _run_selection(dataset, manifest)
    by_id = {case["case_id"]: case for case in dataset["cases"]}
    selected_cases = [deepcopy(by_id[case_id]) for case_id in selection["case_ids"]]
    compact_selection = _compact_selection(selection)
    resolved = deepcopy(manifest)
    resolved.pop(PROFILE_KEY, None)
    resolved["cases"] = {
        case["case_id"]: {"case_id": case["case_id"], "prompt": case["input"]}
        for case in selected_cases
    }
    resolved[CASE_SELECTION_KEY] = compact_selection
    snapshot = {
        "schema_version": 2,
        "scenario": {
            "name": scenario["name"],
            "version": scenario["version"],
            "plugin_version": PLUGIN_VERSION,
        },
        "dataset": {
            "ref": scenario["dataset"],
            "name": dataset["name"],
            "version": dataset["version"],
            "contract_version": CONTRACT_VERSION,
            "fingerprint": dataset["dataset_fingerprint"],
            "cases_sha256": dataset["cases_sha256"],
            "total_cases": len(dataset["cases"]),
            "eval": deepcopy(dataset[EVAL_KEY]),
            "provenance": deepcopy(dataset["provenance"]),
        },
        "selection": deepcopy(selection),
        "selected_cases": selected_cases,
    }
    snapshot[SNAPSHOT_CONTENT_HASH_KEY] = _snapshot_content_sha256(snapshot)
    resolved[SNAPSHOT_KEY] = snapshot
    eval_spec = dataset[EVAL_KEY]
    resolved[PROVENANCE_KEY] = {
        "suite": SUITE,
        "plugin_version": PLUGIN_VERSION,
        "id": eval_spec["id"],
        "version": DATASET_VERSION,
        "dataset": scenario["dataset"],
        "dataset_fingerprint": dataset["dataset_fingerprint"],
        "cases_sha256": dataset["cases_sha256"],
        "selected_count": len(dataset["cases"]),
        "prompt_version": eval_spec["prompt_version"],
        "scorer": MIXED_SCORER_ID,
        "scorer_version": MIXED_SCORER_VERSION,
        "max_output_tokens": budget if budget is not None else preset_budget,
        "max_retries": eval_spec["max_retries"],
        RUN_SELECTION_KEY: compact_selection,
    }
    return resolved


def selected_cases_from_snapshot(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return selected cases only after verifying the complete persisted v2 identity."""
    manifest = run.get("manifest")
    if not isinstance(manifest, dict):
        raise DirectLlmV2SnapshotIntegrityError("run has no manifest")
    snapshot = manifest.get(SNAPSHOT_KEY)
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 2:
        raise DirectLlmV2SnapshotIntegrityError(
            "run has no direct-llm v2 selected-case snapshot"
        )
    supplied_hash = snapshot.get(SNAPSHOT_CONTENT_HASH_KEY)
    if not isinstance(supplied_hash, str) or not supplied_hash:
        raise DirectLlmV2SnapshotIntegrityError(
            f"snapshot is missing {SNAPSHOT_CONTENT_HASH_KEY}"
        )
    try:
        expected_hash = _snapshot_content_sha256(snapshot)
    except (TypeError, ValueError) as error:
        raise DirectLlmV2SnapshotIntegrityError(
            "snapshot content is not canonical JSON"
        ) from error
    if supplied_hash != expected_hash:
        raise DirectLlmV2SnapshotIntegrityError("snapshot content hash mismatch")

    selected = snapshot.get("selected_cases")
    selection = snapshot.get("selection")
    dataset = snapshot.get("dataset")
    scenario = snapshot.get("scenario")
    if not all((
        isinstance(selected, list),
        isinstance(selection, dict),
        isinstance(dataset, dict),
        isinstance(scenario, dict),
    )):
        raise DirectLlmV2SnapshotIntegrityError("snapshot identity fields are invalid")
    try:
        cases = [
            DirectLlmCaseV2.model_validate(item).model_dump(mode="json")
            for item in selected
        ]
        ScorerSpec.model_validate((dataset.get("eval") or {}).get("scorer"))
    except (TypeError, ValueError) as error:
        raise DirectLlmV2SnapshotIntegrityError(
            "selected cases or dataset scorer are invalid"
        ) from error
    ids = [case["case_id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise DirectLlmV2SnapshotIntegrityError(
            "duplicate case_id in selected-case snapshot"
        )
    if ids != selection.get("case_ids") or case_ids_sha256(ids) != selection.get(
        "case_ids_sha256"
    ):
        raise DirectLlmV2SnapshotIntegrityError(
            "selected-case snapshot does not match selection identity"
        )
    run_ids = run.get("case_ids")
    if not isinstance(run_ids, list) or run_ids != ids:
        raise DirectLlmV2SnapshotIntegrityError(
            "run case_ids do not match selected-case snapshot order"
        )
    provider_cases = manifest.get("cases")
    expected_provider_cases = {
        case["case_id"]: {"case_id": case["case_id"], "prompt": case["input"]}
        for case in cases
    }
    if provider_cases != expected_provider_cases:
        raise DirectLlmV2SnapshotIntegrityError(
            "provider case projection does not match selected-case snapshot"
        )
    return cases


def _effective_scorer(case: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    metadata = CaseMetadataV2.model_validate(case["metadata"])
    if metadata.scorer is not None:
        return metadata.scorer.model_dump(mode="json")
    dataset = snapshot["dataset"]
    return ScorerSpec.model_validate(dataset["eval"]["scorer"]).model_dump(mode="json")


def direct_llm_v2_scores(
    run: dict[str, Any], results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Score only from the selected snapshot; resources and upstream sources are never read."""
    manifest = run.get("manifest") or {}
    snapshot = manifest.get(SNAPSHOT_KEY)
    if not isinstance(snapshot, dict):
        raise ValueError("run has no direct-llm v2 snapshot")
    cases = selected_cases_from_snapshot(run)
    selected_ids = {case["case_id"] for case in cases}
    rows: dict[str, dict[str, Any]] = {}
    for row in results:
        case_id = row.get("case_id") if isinstance(row, dict) else None
        if not isinstance(case_id, str) or case_id not in selected_ids:
            raise ValueError(f"result case_id is not selected: {case_id!r}")
        if case_id in rows:
            raise ValueError(f"duplicate result case_id: {case_id}")
        rows[case_id] = row
    scores: list[dict[str, Any]] = []
    for case in cases:
        case_id = case["case_id"]
        spec = _effective_scorer(case, snapshot)
        row = rows.get(case_id)
        if row is None or row.get("outcome") == "not_attempted":
            score: dict[str, Any] = {
                "outcome": "not_attempted",
                "passed": False,
                "attempted": False,
                "responded": False,
                "judged": False,
                "scorer": spec["id"],
                "scorer_version": spec["version"],
                "details": {"config_sha256": spec["config_sha256"]},
            }
        else:
            from motte_eval.direct_llm_v2 import score_answer_case

            evaluator_spec = deepcopy(spec)
            score = score_answer_case(row.get("result"), case.get("expected"), evaluator_spec)
            details = dict(score.get("details") or {})
            reported_hash = details.get("config_sha256", details.get("config_hash"))
            if reported_hash is not None and reported_hash != spec["config_sha256"]:
                raise ValueError("evaluator scorer config hash does not match snapshot")
            details.pop("config_hash", None)
            details["config_sha256"] = spec["config_sha256"]
            score = {
                **score,
                "scorer": spec["id"],
                "scorer_version": spec["version"],
                "details": details,
            }
        scores.append({"case_id": case_id, **score})
    return scores


def aggregate_direct_llm_v2(
    run: dict[str, Any], scores: list[dict[str, Any]],
) -> dict[str, Any]:
    cases = selected_cases_from_snapshot(run)
    selected_ids = [case["case_id"] for case in cases]
    from motte_eval.direct_llm_v2 import aggregate_scores

    return {
        **aggregate_scores(scores, selected_ids),
        "denominator": "judged_cases",
        "scorer_version": MIXED_SCORER_VERSION,
    }


# Short aliases for callers already importing the versioned module.
normalize_dataset = normalize_direct_llm_v2_dataset
persist_dataset = persist_direct_llm_v2_dataset
resolve_manifest = resolve_direct_llm_v2_manifest
score_run = direct_llm_v2_scores
