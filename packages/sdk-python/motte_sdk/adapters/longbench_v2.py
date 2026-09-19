"""Strict offline LongBench v2 conversion and full-set aggregation."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping
from math import ceil
from typing import Any

from motte_contracts.direct_llm_v2 import (
    CONTRACT_VERSION,
    DATASET_ID,
    DATASET_VERSION,
    SELECTION_ALL,
    SUITE,
    DirectLlmCaseV2,
    DirectLlmDatasetV2,
    case_ids_sha256,
    converter_config_sha256,
    dataset_fingerprint_v2,
    scorer_config_sha256,
)
from motte_contracts.identity import canonical_sha256

REQUIRED_ROW_FIELDS = (
    "_id",
    "domain",
    "sub_domain",
    "difficulty",
    "length",
    "context",
    "question",
    "choice_A",
    "choice_B",
    "choice_C",
    "choice_D",
    "answer",
)
DEFAULT_EXPECTED_ROWS = 503
DEFAULT_EXPECTED_DOMAINS = 6
DEFAULT_PROFILE_TARGETS = {"smoke": 50, "regression": 200, "full": 503}
DEFAULT_PROFILE_SEED = "longbench-v2-profiles-v1"
PROTOCOL_ID = "longbench-v2-full-direct-v1"
PROMPT_VERSION = PROTOCOL_ID
CONVERTER_ID = "longbench-v2-to-direct"
CONVERTER_VERSION = "1"
SOURCE_ID = "longbench-v2"
SOURCE_HOMEPAGE = "https://huggingface.co/datasets/THUDM/LongBench-v2"
SOURCE_REPOSITORY = "https://github.com/THUDM/LongBench"
CONTEXT_ESTIMATOR = "utf8-bytes-plus-structure-v1"

_LABELS = ("A", "B", "C", "D")
_REVISION = re.compile(r"[0-9a-f]{40}")
_ARTIFACT_HASH = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[0-9A-Za-z._-]+")
_SCORE_OUTCOME_ORDER = (
    "correct",
    "wrong_answer",
    "invalid_format",
    "call_failed",
    "no_expectation",
    "not_attempted",
)
_SCORE_OUTCOMES = frozenset(_SCORE_OUTCOME_ORDER)
_JUDGED_OUTCOMES = frozenset({"correct", "wrong_answer", "invalid_format"})


class LongBenchV2ConversionError(ValueError):
    """Pinned LongBench v2 inputs cannot produce a valid frozen dataset."""


def _stable_hex(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _positive_integer(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise LongBenchV2ConversionError(f"{label} must be a positive integer")
    return value


def _required_text(value: Any, *, field: str, source_line: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LongBenchV2ConversionError(f"row {source_line} {field} must be non-empty text")
    return value


def _strict_revision(revision: Any) -> str:
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise LongBenchV2ConversionError(
            "revision must be a pinned 40-character lowercase commit SHA"
        )
    return revision


def _strict_artifact(artifact: Mapping[str, Any], *, index: int, revision: str) -> dict[str, Any]:
    required = {"logical_name", "url", "sha256", "bytes"}
    actual = set(artifact)
    if actual != required:
        raise LongBenchV2ConversionError(
            f"artifact {index} schema mismatch "
            f"(missing={sorted(required - actual)}, extra={sorted(actual - required)})"
        )
    logical_name = artifact["logical_name"]
    url = artifact["url"]
    sha256 = artifact["sha256"]
    size = artifact["bytes"]
    if not isinstance(logical_name, str) or not logical_name.strip():
        raise LongBenchV2ConversionError(f"artifact {index} logical_name must be non-empty")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise LongBenchV2ConversionError(f"artifact {index} url must be HTTPS")
    if revision not in url:
        raise LongBenchV2ConversionError(f"artifact {index} url must contain the pinned revision")
    if not isinstance(sha256, str) or _ARTIFACT_HASH.fullmatch(sha256) is None:
        raise LongBenchV2ConversionError(
            f"artifact {index} sha256 must be 64 lowercase hex characters"
        )
    if type(size) is not int or size <= 0:
        raise LongBenchV2ConversionError(f"artifact {index} bytes must be positive")
    return {
        "logical_name": logical_name,
        "url": url,
        "sha256": sha256,
        "bytes": size,
    }


def _strict_artifacts(
    artifacts: Iterable[Mapping[str, Any]], *, revision: str
) -> list[dict[str, Any]]:
    if isinstance(artifacts, (str, bytes, bytearray, Mapping)):
        raise LongBenchV2ConversionError("artifacts must be a non-empty iterable")
    try:
        pinned = [
            _strict_artifact(artifact, index=index, revision=revision)
            for index, artifact in enumerate(artifacts, start=1)
        ]
    except TypeError as error:
        raise LongBenchV2ConversionError("artifacts must be a non-empty iterable") from error
    if not pinned:
        raise LongBenchV2ConversionError("artifacts must be a non-empty iterable")
    names = [artifact["logical_name"] for artifact in pinned]
    if len(names) != len(set(names)):
        raise LongBenchV2ConversionError("artifact logical_name values must be unique")
    return sorted(pinned, key=lambda artifact: artifact["logical_name"])


def _strict_rows(
    rows: Iterable[Mapping[str, Any]], *, expected_rows: int, expected_domains: int
) -> list[dict[str, str]]:
    expected_rows = _positive_integer(expected_rows, "expected_rows")
    expected_domains = _positive_integer(expected_domains, "expected_domains")
    if expected_domains > expected_rows:
        raise LongBenchV2ConversionError("expected_domains cannot exceed expected_rows")
    if isinstance(rows, (str, bytes, bytearray, Mapping)):
        raise LongBenchV2ConversionError("rows must be an iterable of row objects")
    try:
        raw_rows = list(rows)
    except TypeError as error:
        raise LongBenchV2ConversionError("rows must be an iterable of row objects") from error
    if len(raw_rows) != expected_rows:
        raise LongBenchV2ConversionError(
            f"LongBench v2 row count mismatch: expected {expected_rows}, got {len(raw_rows)}"
        )

    required = set(REQUIRED_ROW_FIELDS)
    seen_ids: set[str] = set()
    validated: list[dict[str, str]] = []
    for source_line, raw_row in enumerate(raw_rows, start=1):
        if not isinstance(raw_row, Mapping):
            raise LongBenchV2ConversionError(f"row {source_line} must be an object")
        actual = set(raw_row)
        if actual != required:
            raise LongBenchV2ConversionError(
                f"row {source_line} schema mismatch "
                f"(missing={sorted(required - actual)}, extra={sorted(actual - required)})"
            )
        normalized = {
            field: _required_text(raw_row[field], field=field, source_line=source_line)
            for field in REQUIRED_ROW_FIELDS
        }
        row_id = normalized["_id"]
        if row_id in seen_ids:
            raise LongBenchV2ConversionError(f"row {source_line} duplicates _id {row_id!r}")
        seen_ids.add(row_id)
        if normalized["answer"] not in _LABELS:
            raise LongBenchV2ConversionError(
                f"row {source_line} answer must be exactly one of A, B, C, D"
            )
        validated.append(normalized)

    domain_count = len({row["domain"] for row in validated})
    if domain_count != expected_domains:
        raise LongBenchV2ConversionError(
            f"LongBench v2 domain count mismatch: expected {expected_domains}, got {domain_count}"
        )
    return validated


def _scorer() -> dict[str, Any]:
    config = {"labels": list(_LABELS), "allow_bare_final_label": False}
    return {
        "id": "choice",
        "version": "1",
        "config": config,
        "config_sha256": scorer_config_sha256(config),
    }


def _prompt(row: Mapping[str, str]) -> str:
    choices = "\n".join(f"{label}. {row[f'choice_{label}']}" for label in _LABELS)
    return (
        f"Protocol: {PROTOCOL_ID}\n\n"
        "Use the complete context below to answer the question.\n\n"
        f"Context:\n{row['context']}\n\n"
        f"Question:\n{row['question']}\n\n"
        f"Choices:\n{choices}\n\n"
        "Return exactly one final marker on its own line, replacing X with A, B, C, or D:\n"
        "[ANSWER:X]"
    )


def _cases(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    scorer = _scorer()
    cases: list[dict[str, Any]] = []
    for source_line, row in enumerate(rows, start=1):
        case = {
            "case_id": f"longbench-v2-{row['_id']}",
            "input": _prompt(row),
            "expected": row["answer"],
            "metadata": {
                "source_line": source_line,
                "source_id": row["_id"],
                "language": "en",
                "subject": row["domain"],
                "category": row["sub_domain"],
                "difficulty": row["difficulty"],
                "split": "test",
                "tags": [
                    "longbench-v2",
                    "multiple-choice",
                    "full-context",
                    f"length:{row['length']}",
                ],
                "template_family": PROTOCOL_ID,
                "scorer": scorer,
            },
        }
        cases.append(DirectLlmCaseV2.model_validate(case).model_dump(mode="json"))
    return cases


def _profile_order(cases: list[dict[str, Any]], *, seed: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        groups[case["metadata"]["subject"]].append(case)
    for domain, domain_cases in groups.items():
        domain_cases.sort(
            key=lambda case: (
                _stable_hex(seed, "case", domain, case["case_id"]),
                case["case_id"],
            )
        )
    domains = sorted(groups, key=lambda domain: (_stable_hex(seed, "domain", domain), domain))
    queues = {domain: deque(groups[domain]) for domain in domains}
    ordered: list[dict[str, Any]] = []
    while any(queues.values()):
        for domain in domains:
            if queues[domain]:
                ordered.append(queues[domain].popleft())
    return ordered


def _profile_distribution(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "domain": dict(sorted(Counter(case["metadata"]["subject"] for case in cases).items())),
        "sub_domain": dict(sorted(Counter(case["metadata"]["category"] for case in cases).items())),
        "difficulty": dict(
            sorted(Counter(case["metadata"]["difficulty"] for case in cases).items())
        ),
        "length": dict(
            sorted(
                Counter(
                    next(
                        tag.removeprefix("length:")
                        for tag in case["metadata"]["tags"]
                        if tag.startswith("length:")
                    )
                    for case in cases
                ).items()
            )
        ),
    }


def _profiles(
    cases: list[dict[str, Any]], *, seed: str, targets: Mapping[str, int]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(seed, str) or not seed:
        raise LongBenchV2ConversionError("profile_seed must be non-empty")
    ordered = _profile_order(cases, seed=seed)
    profiles: list[dict[str, Any]] = []
    statistics: dict[str, Any] = {}
    for name in DEFAULT_PROFILE_TARGETS:
        selected_cases = ordered[: targets[name]]
        selected = [case["case_id"] for case in selected_cases]
        profiles.append(
            {
                "name": name,
                "strategy": "stable-domain-round-robin-prefix-v1",
                "count": len(selected),
                "case_ids": selected,
                "case_ids_sha256": case_ids_sha256(selected),
                "dimensions": ["domain", "sub_domain", "difficulty", "length"],
                "seed": seed,
            }
        )
        statistics[name] = {
            "count": len(selected),
            **_profile_distribution(selected_cases),
        }
    return profiles, statistics


def _nearest_rank(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, ceil(percentile * len(ordered)) - 1)]


def _source_distribution(rows: list[dict[str, str]]) -> dict[str, Any]:
    context_bytes = [len(row["context"].encode("utf-8")) for row in rows]
    return {
        "rows": {"test": len(rows)},
        "domain": dict(sorted(Counter(row["domain"] for row in rows).items())),
        "sub_domain": dict(sorted(Counter(row["sub_domain"] for row in rows).items())),
        "length": dict(sorted(Counter(row["length"] for row in rows).items())),
        "difficulty": dict(sorted(Counter(row["difficulty"] for row in rows).items())),
        "context_utf8_bytes": {
            "max": max(context_bytes),
            "p50": _nearest_rank(context_bytes, 0.50),
            "p95": _nearest_rank(context_bytes, 0.95),
        },
    }


def convert_longbench_v2_direct(
    rows: Iterable[Mapping[str, Any]],
    revision: str,
    artifacts: Iterable[Mapping[str, Any]],
    *,
    expected_rows: int = DEFAULT_EXPECTED_ROWS,
    expected_domains: int = DEFAULT_EXPECTED_DOMAINS,
    name: str = "longbench-v2-direct",
    version: str = "1",
) -> dict[str, Any]:
    """Convert the immutable official 503-row snapshot without network I/O."""
    if expected_rows != DEFAULT_EXPECTED_ROWS:
        raise LongBenchV2ConversionError("expected_rows is fixed at 503")
    if expected_domains != DEFAULT_EXPECTED_DOMAINS:
        raise LongBenchV2ConversionError("expected_domains is fixed at 6")
    revision = _strict_revision(revision)
    pinned_artifacts = _strict_artifacts(artifacts, revision=revision)
    source_rows = _strict_rows(rows, expected_rows=expected_rows, expected_domains=expected_domains)
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise LongBenchV2ConversionError("name must be a Direct LLM compatible identifier")
    if not isinstance(version, str) or not version.strip():
        raise LongBenchV2ConversionError("version must be non-empty")

    cases = _cases(source_rows)
    targets = dict(DEFAULT_PROFILE_TARGETS)
    profiles, profile_statistics = _profiles(cases, seed=DEFAULT_PROFILE_SEED, targets=targets)
    distribution = _source_distribution(source_rows)
    artifact_manifest_sha256 = canonical_sha256(pinned_artifacts)
    converter_config = {
        "protocol": {
            "id": PROTOCOL_ID,
            "mode": "full-direct",
            "context_policy": "full-verbatim-v1",
            "choice_order": "source-order",
            "answer_marker": "[ANSWER:{value}]",
            "require_final_marker": True,
        },
        "immutable_pins": {
            "revision": revision,
            "artifact_manifest_sha256": artifact_manifest_sha256,
        },
        "expected_rows": expected_rows,
        "expected_domains": expected_domains,
        "source_rows_sha256": canonical_sha256(source_rows),
        "source_distribution": distribution,
        "profile_targets": dict(targets),
        "profile_seed": DEFAULT_PROFILE_SEED,
        "profile_statistics": profile_statistics,
        "context_preflight": {
            "owner": "sdk",
            "method": CONTEXT_ESTIMATOR,
            "conservative_upper_bound": True,
            "official_tokenizer_parity": False,
        },
        "official_comparability": {
            "status": "pending",
            "reason": "official extraction, tokenizer eligibility, and runner parity are not established",
        },
    }
    provenance = {
        "source_id": SOURCE_ID,
        "source_kind": "managed-public",
        "homepage": SOURCE_HOMEPAGE,
        "upstream_revision": revision,
        "artifacts": pinned_artifacts,
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "license": {
            "id": "Apache-2.0-data-claim-with-constituent-rights-unresolved",
            "status": "pending",
            "evidence_urls": [SOURCE_HOMEPAGE, SOURCE_REPOSITORY],
            "commercial_use": "unknown",
            "redistribution": "unknown",
            "reviewed_at": None,
        },
        "converter": {
            "id": CONVERTER_ID,
            "version": CONVERTER_VERSION,
            "config": converter_config,
            "config_sha256": converter_config_sha256(converter_config),
        },
        "synthetic": False,
    }
    scorer = _scorer()
    record: dict[str, Any] = {
        "name": name,
        "version": version,
        "contract_version": CONTRACT_VERSION,
        "eval": {
            "suite": SUITE,
            "id": DATASET_ID,
            "version": DATASET_VERSION,
            "selected_count": len(cases),
            "selection": SELECTION_ALL,
            "scorer": scorer,
            "prompt_version": PROMPT_VERSION,
            "max_output_tokens": 16,
            "max_retries": 0,
        },
        "provenance": provenance,
        "cases": cases,
        "cases_sha256": canonical_sha256(cases),
        "profiles": profiles,
        "profiles_sha256": canonical_sha256(profiles),
    }
    record["dataset_fingerprint"] = dataset_fingerprint_v2(record)
    return DirectLlmDatasetV2.model_validate(record).model_dump(mode="json")


def _length_of(case: Mapping[str, Any]) -> str:
    values = [
        tag.removeprefix("length:") for tag in case["metadata"]["tags"] if tag.startswith("length:")
    ]
    if len(values) != 1 or not values[0]:
        raise LongBenchV2ConversionError(
            f"case {case['case_id']!r} must contain exactly one length tag"
        )
    return values[0]


def _normalized_score_rows(
    scores: Iterable[Mapping[str, Any]] | Mapping[str, Any],
    *,
    cases_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    if isinstance(scores, Mapping):
        raw_scores: list[Mapping[str, Any]] = [
            {"case_id": case_id, "prediction": prediction} for case_id, prediction in scores.items()
        ]
    elif isinstance(scores, (str, bytes, bytearray)):
        raise LongBenchV2ConversionError("scores must be score objects or a prediction map")
    else:
        try:
            raw_scores = list(scores)
        except TypeError as error:
            raise LongBenchV2ConversionError(
                "scores must be score objects or a prediction map"
            ) from error

    outcomes: dict[str, str] = {}
    for index, row in enumerate(raw_scores, start=1):
        if not isinstance(row, Mapping):
            raise LongBenchV2ConversionError(f"score {index} must be an object")
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or case_id not in cases_by_id:
            raise LongBenchV2ConversionError(
                f"score {index} case_id is not in the full profile: {case_id!r}"
            )
        if case_id in outcomes:
            raise LongBenchV2ConversionError(f"duplicate score case_id: {case_id}")
        has_outcome = "outcome" in row
        has_prediction = "prediction" in row
        if has_outcome == has_prediction:
            raise LongBenchV2ConversionError(
                f"score {index} must contain exactly one of outcome or prediction"
            )
        if has_outcome:
            outcome = row["outcome"]
            if not isinstance(outcome, str) or outcome not in _SCORE_OUTCOMES:
                raise LongBenchV2ConversionError(f"score {index} has unknown outcome: {outcome!r}")
            expected_judged = outcome in _JUDGED_OUTCOMES
            if "judged" in row and row["judged"] is not expected_judged:
                raise LongBenchV2ConversionError(
                    f"score {index} judged flag conflicts with outcome"
                )
            if "passed" in row and row["passed"] is not (outcome == "correct"):
                raise LongBenchV2ConversionError(
                    f"score {index} passed flag conflicts with outcome"
                )
        else:
            prediction = row["prediction"]
            if prediction is None:
                outcome = "not_attempted"
            elif not isinstance(prediction, str):
                raise LongBenchV2ConversionError(f"score {index} prediction must be text or null")
            elif prediction not in _LABELS:
                outcome = "invalid_format"
            elif prediction == cases_by_id[case_id]["expected"]:
                outcome = "correct"
            else:
                outcome = "wrong_answer"
        outcomes[case_id] = outcome
    return outcomes


def _metrics(case_ids: Iterable[str], outcomes: Mapping[str, str]) -> dict[str, Any]:
    selected_ids = list(case_ids)
    selected = len(selected_ids)
    counts = Counter(outcomes[case_id] for case_id in selected_ids)
    judged = sum(counts[outcome] for outcome in _JUDGED_OUTCOMES)
    correct = counts["correct"]
    return {
        "selected": selected,
        "judged": judged,
        "correct": correct,
        "not_attempted": counts["not_attempted"],
        "accuracy": correct / selected if selected else None,
        "coverage": judged / selected if selected else None,
        "outcomes": {outcome: counts[outcome] for outcome in _SCORE_OUTCOME_ORDER},
    }


def _aggregation_protocol(
    dataset: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, dict[str, Any]]]:
    provenance = dataset["provenance"]
    converter = provenance["converter"]
    config = converter["config"]
    expected_protocol = {
        "id": PROTOCOL_ID,
        "mode": "full-direct",
        "context_policy": "full-verbatim-v1",
        "choice_order": "source-order",
        "answer_marker": "[ANSWER:{value}]",
        "require_final_marker": True,
    }
    if (
        provenance["source_id"] != SOURCE_ID
        or converter["id"] != CONVERTER_ID
        or converter["version"] != CONVERTER_VERSION
        or config.get("protocol") != expected_protocol
    ):
        raise LongBenchV2ConversionError(
            "dataset is not the canonical LongBench v2 full-direct protocol"
        )
    if provenance["license"]["status"] != "pending":
        raise LongBenchV2ConversionError("LongBench v2 license snapshot must remain pending")
    if config.get("immutable_pins") != {
        "revision": provenance["upstream_revision"],
        "artifact_manifest_sha256": provenance["artifact_manifest_sha256"],
    }:
        raise LongBenchV2ConversionError("LongBench v2 immutable pins do not match provenance")
    if (
        config.get("expected_rows") != DEFAULT_EXPECTED_ROWS
        or config.get("expected_domains") != DEFAULT_EXPECTED_DOMAINS
        or config.get("profile_targets") != DEFAULT_PROFILE_TARGETS
        or config.get("profile_seed") != DEFAULT_PROFILE_SEED
        or config.get("official_comparability", {}).get("status") != "pending"
        or config.get("context_preflight")
        != {
            "owner": "sdk",
            "method": CONTEXT_ESTIMATOR,
            "conservative_upper_bound": True,
            "official_tokenizer_parity": False,
        }
    ):
        raise LongBenchV2ConversionError(
            "LongBench v2 converter config does not match the frozen protocol"
        )
    if dataset["eval"] != {
        "suite": SUITE,
        "id": DATASET_ID,
        "version": DATASET_VERSION,
        "selected_count": DEFAULT_EXPECTED_ROWS,
        "selection": SELECTION_ALL,
        "scorer": _scorer(),
        "prompt_version": PROMPT_VERSION,
        "max_output_tokens": 16,
        "max_retries": 0,
    }:
        raise LongBenchV2ConversionError(
            "LongBench v2 eval config does not match the frozen protocol"
        )

    cases = dataset["cases"]
    if len(cases) != DEFAULT_EXPECTED_ROWS:
        raise LongBenchV2ConversionError(
            "LongBench v2 aggregation requires exactly 503 dataset cases"
        )
    expected_scorer = _scorer()
    source_ids: set[str] = set()
    domain_counts: Counter[str] = Counter()
    sub_domain_counts: Counter[str] = Counter()
    difficulty_counts: Counter[str] = Counter()
    length_counts: Counter[str] = Counter()
    for case in cases:
        metadata = case["metadata"]
        if (
            metadata["language"] != "en"
            or metadata["split"] != "test"
            or metadata["template_family"] != PROTOCOL_ID
            or metadata["scorer"] != expected_scorer
            or not isinstance(metadata["subject"], str)
            or not isinstance(metadata["category"], str)
            or not isinstance(metadata["difficulty"], str)
        ):
            raise LongBenchV2ConversionError(
                f"case {case['case_id']!r} metadata does not match the frozen protocol"
            )
        if metadata["source_id"] in source_ids:
            raise LongBenchV2ConversionError("LongBench v2 source_id values must be unique")
        source_ids.add(metadata["source_id"])
        domain_counts[metadata["subject"]] += 1
        sub_domain_counts[metadata["category"]] += 1
        difficulty_counts[metadata["difficulty"]] += 1
        length_counts[_length_of(case)] += 1
    if len(domain_counts) != DEFAULT_EXPECTED_DOMAINS:
        raise LongBenchV2ConversionError("LongBench v2 aggregation requires exactly 6 domains")
    distribution = config.get("source_distribution")
    expected_distribution = {
        "rows": {"test": DEFAULT_EXPECTED_ROWS},
        "domain": dict(sorted(domain_counts.items())),
        "sub_domain": dict(sorted(sub_domain_counts.items())),
        "length": dict(sorted(length_counts.items())),
        "difficulty": dict(sorted(difficulty_counts.items())),
    }
    if not isinstance(distribution, dict) or any(
        distribution.get(key) != value for key, value in expected_distribution.items()
    ):
        raise LongBenchV2ConversionError(
            "LongBench v2 source distributions do not match case metadata"
        )
    context_bytes = distribution.get("context_utf8_bytes")
    if (
        not isinstance(context_bytes, dict)
        or set(context_bytes) != {"max", "p50", "p95"}
        or any(type(value) is not int or value <= 0 for value in context_bytes.values())
        or not context_bytes["max"] >= context_bytes["p95"] >= context_bytes["p50"]
    ):
        raise LongBenchV2ConversionError("LongBench v2 context byte distribution is invalid")

    profiles = {profile["name"]: profile for profile in dataset["profiles"]}
    if set(profiles) != set(DEFAULT_PROFILE_TARGETS):
        raise LongBenchV2ConversionError(
            "LongBench v2 requires only smoke, regression, and full profiles"
        )
    case_ids = [case["case_id"] for case in cases]
    expected_strategy = "stable-domain-round-robin-prefix-v1"
    for name, count in DEFAULT_PROFILE_TARGETS.items():
        profile = profiles[name]
        if (
            profile["count"] != count
            or len(profile["case_ids"]) != count
            or profile["strategy"] != expected_strategy
            or profile["seed"] != DEFAULT_PROFILE_SEED
        ):
            raise LongBenchV2ConversionError(
                f"LongBench v2 profile {name!r} does not match the frozen protocol"
            )
    smoke = set(profiles["smoke"]["case_ids"])
    regression = set(profiles["regression"]["case_ids"])
    full = set(profiles["full"]["case_ids"])
    if not smoke < regression < full or full != set(case_ids):
        raise LongBenchV2ConversionError(
            "LongBench v2 profiles must be fixed nested subsets of all 503 cases"
        )
    return converter, case_ids, profiles


def aggregate_longbench_v2(
    dataset: Mapping[str, Any],
    scores: Iterable[Mapping[str, Any]] | Mapping[str, Any],
) -> dict[str, Any]:
    """Aggregate only the complete 503-case protocol without hiding missing attempts."""
    try:
        validated = DirectLlmDatasetV2.model_validate(dataset).model_dump(mode="json")
    except Exception as error:
        raise LongBenchV2ConversionError("dataset must be a valid Direct LLM v2 dataset") from error
    converter, case_ids, _profiles_by_name = _aggregation_protocol(validated)
    cases_by_id = {case["case_id"]: case for case in validated["cases"]}
    if len(cases_by_id) != DEFAULT_EXPECTED_ROWS:
        raise LongBenchV2ConversionError("LongBench v2 case_id values must be unique")
    provided = _normalized_score_rows(scores, cases_by_id=cases_by_id)
    outcomes = {case_id: provided.get(case_id, "not_attempted") for case_id in case_ids}

    groups: dict[str, dict[str, list[str]]] = {
        "difficulty": defaultdict(list),
        "length": defaultdict(list),
        "domain": defaultdict(list),
    }
    case_outcomes: list[dict[str, str]] = []
    for case_id in case_ids:
        case = cases_by_id[case_id]
        groups["difficulty"][case["metadata"]["difficulty"]].append(case_id)
        groups["length"][_length_of(case)].append(case_id)
        groups["domain"][case["metadata"]["subject"]].append(case_id)
        case_outcomes.append({"case_id": case_id, "outcome": outcomes[case_id]})

    overall = _metrics(case_ids, outcomes)
    by_dimension = {
        dimension: {
            value: _metrics(group_ids, outcomes) for value, group_ids in sorted(values.items())
        }
        for dimension, values in groups.items()
    }
    return {
        "schema_version": 1,
        "dataset_fingerprint": validated["dataset_fingerprint"],
        "protocol_id": PROTOCOL_ID,
        "selected_profile": "full",
        "denominator": "selected_cases",
        "overall": overall,
        "by_difficulty": by_dimension["difficulty"],
        "by_length": by_dimension["length"],
        "by_domain": by_dimension["domain"],
        "case_outcomes": case_outcomes,
        "overall_publishable": (
            overall["selected"] == DEFAULT_EXPECTED_ROWS
            and overall["judged"] == DEFAULT_EXPECTED_ROWS
            and overall["not_attempted"] == 0
        ),
        "official_comparability": converter["config"]["official_comparability"],
    }


__all__ = [
    "CONTEXT_ESTIMATOR",
    "DEFAULT_EXPECTED_DOMAINS",
    "DEFAULT_EXPECTED_ROWS",
    "DEFAULT_PROFILE_SEED",
    "DEFAULT_PROFILE_TARGETS",
    "LongBenchV2ConversionError",
    "PROTOCOL_ID",
    "REQUIRED_ROW_FIELDS",
    "aggregate_longbench_v2",
    "convert_longbench_v2_direct",
]
