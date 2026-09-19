"""Strict offline adapters for isolated MMLU-Pro Direct LLM protocols."""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
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
    "question_id",
    "question",
    "options",
    "answer",
    "answer_index",
    "cot_content",
    "category",
    "src",
)
DEFAULT_EXPECTED_TEST_ROWS = 12_032
DEFAULT_EXPECTED_VALIDATION_ROWS = 70
DEFAULT_EXPECTED_CATEGORIES = 14
DEFAULT_DEMONSTRATIONS_PER_CATEGORY = 5
DEFAULT_CATEGORIES = (
    "biology",
    "business",
    "chemistry",
    "computer science",
    "economics",
    "engineering",
    "health",
    "history",
    "law",
    "math",
    "other",
    "philosophy",
    "physics",
    "psychology",
)
DEFAULT_PROFILE_TARGETS = {"smoke": 56, "regression": 560, "full": 12_032}
FIVE_SHOT_PROTOCOL_ID = "mmlu-pro-5shot-cot-direct-v1"
ZERO_SHOT_PROTOCOL_ID = "mmlu-pro-zero-shot-direct-v1"
FIVE_SHOT_CONVERTER_ID = "mmlu-pro-5shot-cot-to-direct"
ZERO_SHOT_CONVERTER_ID = "mmlu-pro-zero-shot-to-direct"
CONVERTER_VERSION = "1"
SOURCE_ID = "mmlu-pro"
SOURCE_HOMEPAGE = "https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro"
SOURCE_REPOSITORY = "https://github.com/TIGER-AI-Lab/MMLU-Pro"
DEFAULT_PROFILE_SEED = "mmlu-pro-profile-v1"
FIVE_SHOT_DEFAULT_SEED = DEFAULT_PROFILE_SEED
ZERO_SHOT_DEFAULT_SEED = DEFAULT_PROFILE_SEED

_LABELS = tuple("ABCDEFGHIJ")
_REVISION = re.compile(r"[0-9a-f]{40}")
_ARTIFACT_HASH = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[0-9A-Za-z._-]+")


class MmluProConversionError(ValueError):
    """Pinned MMLU-Pro inputs cannot produce a valid frozen dataset."""


def _stable_hex(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _positive_integer(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise MmluProConversionError(f"{label} must be a positive integer")
    return value


def _strict_artifact(artifact: Mapping[str, Any], index: int) -> dict[str, Any]:
    required = {"logical_name", "url", "sha256", "bytes"}
    actual = set(artifact)
    if actual != required:
        raise MmluProConversionError(
            f"pinned artifact {index} schema mismatch "
            f"(missing={sorted(required - actual)}, extra={sorted(actual - required)})"
        )
    logical_name = artifact["logical_name"]
    url = artifact["url"]
    sha256 = artifact["sha256"]
    size = artifact["bytes"]
    if not isinstance(logical_name, str) or not logical_name.strip():
        raise MmluProConversionError(f"pinned artifact {index} logical_name must be non-empty")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise MmluProConversionError(f"pinned artifact {index} url must be HTTPS")
    if not isinstance(sha256, str) or _ARTIFACT_HASH.fullmatch(sha256) is None:
        raise MmluProConversionError(
            f"pinned artifact {index} sha256 must be 64 lowercase hex characters"
        )
    if type(size) is not int or size <= 0:
        raise MmluProConversionError(f"pinned artifact {index} bytes must be positive")
    return {"logical_name": logical_name, "url": url, "sha256": sha256, "bytes": size}


def _strict_pinned_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MmluProConversionError("pinned_provenance must be an object")
    required = {"dataset_revision", "runner_revision", "artifacts"}
    actual = set(value)
    if actual != required:
        raise MmluProConversionError(
            "pinned_provenance schema mismatch "
            f"(missing={sorted(required - actual)}, extra={sorted(actual - required)})"
        )
    dataset_revision = value["dataset_revision"]
    runner_revision = value["runner_revision"]
    for label, revision in (
        ("dataset_revision", dataset_revision),
        ("runner_revision", runner_revision),
    ):
        if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
            raise MmluProConversionError(f"{label} must be a pinned 40-character commit SHA")
    raw_artifacts = value["artifacts"]
    if isinstance(raw_artifacts, (str, bytes, bytearray, Mapping)):
        raise MmluProConversionError("pinned artifacts must be a non-empty list")
    try:
        artifacts = [
            _strict_artifact(artifact, index)
            for index, artifact in enumerate(raw_artifacts, start=1)
        ]
    except TypeError as error:
        raise MmluProConversionError("pinned artifacts must be a non-empty list") from error
    if not artifacts:
        raise MmluProConversionError("pinned artifacts must be a non-empty list")
    names = [artifact["logical_name"] for artifact in artifacts]
    if len(set(names)) != len(names):
        raise MmluProConversionError("pinned artifact logical_name values must be unique")
    artifacts.sort(key=lambda artifact: artifact["logical_name"])
    dataset_artifacts = [
        artifact for artifact in artifacts if dataset_revision in artifact["url"]
    ]
    runner_artifacts = [
        artifact for artifact in artifacts if runner_revision in artifact["url"]
    ]
    if any(
        dataset_revision not in artifact["url"] and runner_revision not in artifact["url"]
        for artifact in artifacts
    ):
        raise MmluProConversionError(
            "every pinned artifact URL must contain the dataset or runner revision"
        )
    dataset_names = {artifact["logical_name"].casefold() for artifact in dataset_artifacts}
    if not any("test" in name for name in dataset_names):
        raise MmluProConversionError("pinned dataset artifacts must include the test split")
    if not any("validation" in name for name in dataset_names):
        raise MmluProConversionError("pinned dataset artifacts must include the validation split")
    if not runner_artifacts:
        raise MmluProConversionError("pinned artifacts must include runner revision evidence")
    return {
        "dataset_revision": dataset_revision,
        "runner_revision": runner_revision,
        "artifacts": artifacts,
    }


def _strict_rows(
    source: Iterable[Mapping[str, Any]], *, split: str, expected_rows: int
) -> list[dict[str, Any]]:
    if isinstance(source, (str, bytes, bytearray, Mapping)):
        raise MmluProConversionError(f"{split} rows must be an iterable of row objects")
    try:
        raw_rows = list(source)
    except TypeError as error:
        raise MmluProConversionError(
            f"{split} rows must be an iterable of row objects"
        ) from error
    if len(raw_rows) != expected_rows:
        raise MmluProConversionError(
            f"MMLU-Pro {split} row count mismatch: expected {expected_rows}, got {len(raw_rows)}"
        )

    required = set(REQUIRED_ROW_FIELDS)
    seen_ids: set[int] = set()
    rows: list[dict[str, Any]] = []
    for source_line, raw_row in enumerate(raw_rows, start=1):
        if not isinstance(raw_row, Mapping):
            raise MmluProConversionError(f"{split} row {source_line} must be an object")
        actual = set(raw_row)
        if actual != required:
            raise MmluProConversionError(
                f"{split} row {source_line} schema mismatch "
                f"(missing={sorted(required - actual)}, extra={sorted(actual - required)})"
            )

        question_id = raw_row["question_id"]
        if type(question_id) is not int or question_id < 0:
            raise MmluProConversionError(
                f"{split} row {source_line} question_id must be a non-negative integer"
            )
        if question_id in seen_ids:
            raise MmluProConversionError(
                f"{split} row {source_line} duplicates question_id {question_id}"
            )
        seen_ids.add(question_id)

        question = raw_row["question"]
        category = raw_row["category"]
        src = raw_row["src"]
        for field, value in (("question", question), ("category", category), ("src", src)):
            if not isinstance(value, str) or not value.strip():
                raise MmluProConversionError(
                    f"{split} row {source_line} {field} must be non-empty text"
                )

        raw_options = raw_row["options"]
        if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= len(_LABELS):
            raise MmluProConversionError(
                f"{split} row {source_line} options must contain 2-10 entries"
            )
        if any(not isinstance(option, str) or not option.strip() for option in raw_options):
            raise MmluProConversionError(
                f"{split} row {source_line} options must contain non-empty text"
            )
        options = [option for option in raw_options if option != "N/A"]
        if not 2 <= len(options) <= len(_LABELS):
            raise MmluProConversionError(
                f"{split} row {source_line} must retain 2-10 options after N/A filtering"
            )

        answer = raw_row["answer"]
        answer_index = raw_row["answer_index"]
        if not isinstance(answer, str) or answer not in _LABELS:
            raise MmluProConversionError(
                f"{split} row {source_line} answer must be one of A-J"
            )
        if type(answer_index) is not int or not 0 <= answer_index < len(options):
            raise MmluProConversionError(
                f"{split} row {source_line} answer_index is outside options"
            )
        expected_answer = _LABELS[answer_index]
        if answer != expected_answer:
            raise MmluProConversionError(
                f"{split} row {source_line} answer {answer!r} does not match "
                f"answer_index {answer_index} ({expected_answer!r})"
            )

        cot_content = raw_row["cot_content"]
        if not isinstance(cot_content, str):
            raise MmluProConversionError(
                f"{split} row {source_line} cot_content must be text"
            )
        if split == "validation" and not cot_content.strip():
            raise MmluProConversionError(
                f"validation row {source_line} cot_content must be non-empty"
            )

        rows.append(
            {
                "question_id": question_id,
                "question": question,
                "options": list(options),
                "answer": answer,
                "answer_index": answer_index,
                "cot_content": cot_content,
                "category": category,
                "src": src,
            }
        )
    return rows


def _validated_splits(
    test_rows: Iterable[Mapping[str, Any]],
    validation_rows: Iterable[Mapping[str, Any]],
    *,
    expected_test_rows: int,
    expected_validation_rows: int,
    expected_categories: int,
    expected_category_names: Iterable[str] | None,
    demonstrations_per_category: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected_test_rows = _positive_integer(expected_test_rows, "expected_test_rows")
    expected_validation_rows = _positive_integer(
        expected_validation_rows, "expected_validation_rows"
    )
    expected_categories = _positive_integer(expected_categories, "expected_categories")
    demonstrations_per_category = _positive_integer(
        demonstrations_per_category, "demonstrations_per_category"
    )
    if expected_validation_rows != expected_categories * demonstrations_per_category:
        raise MmluProConversionError(
            "expected_validation_rows must equal expected_categories * "
            "demonstrations_per_category"
        )

    test = _strict_rows(test_rows, split="test", expected_rows=expected_test_rows)
    validation = _strict_rows(
        validation_rows, split="validation", expected_rows=expected_validation_rows
    )
    test_ids = {row["question_id"] for row in test}
    validation_ids = {row["question_id"] for row in validation}
    overlap = sorted(test_ids.intersection(validation_ids))
    if overlap:
        raise MmluProConversionError(
            f"MMLU-Pro question_id values must be disjoint across splits: {overlap[0]}"
        )

    test_categories = {row["category"] for row in test}
    validation_categories = {row["category"] for row in validation}
    if len(test_categories) != expected_categories:
        raise MmluProConversionError(
            f"MMLU-Pro test category count mismatch: expected {expected_categories}, "
            f"got {len(test_categories)}"
        )
    if expected_category_names is None:
        required_categories = (
            set(DEFAULT_CATEGORIES)
            if expected_categories == DEFAULT_EXPECTED_CATEGORIES
            else test_categories
        )
    else:
        if isinstance(expected_category_names, (str, bytes, bytearray, Mapping)):
            raise MmluProConversionError("expected_category_names must be an iterable of names")
        try:
            category_names = list(expected_category_names)
        except TypeError as error:
            raise MmluProConversionError(
                "expected_category_names must be an iterable of names"
            ) from error
        if any(not isinstance(category, str) or not category.strip() for category in category_names):
            raise MmluProConversionError(
                "expected_category_names must contain non-empty strings"
            )
        if len(category_names) != expected_categories or len(set(category_names)) != len(category_names):
            raise MmluProConversionError(
                "expected_category_names must contain the expected number of unique names"
            )
        required_categories = set(category_names)
    if test_categories != required_categories:
        raise MmluProConversionError(
            "MMLU-Pro test categories do not match the expected category names"
        )
    if validation_categories != test_categories:
        raise MmluProConversionError(
            "MMLU-Pro validation categories must exactly match test categories"
        )
    validation_counts = Counter(row["category"] for row in validation)
    wrong_counts = {
        category: count
        for category, count in validation_counts.items()
        if count != demonstrations_per_category
    }
    if wrong_counts:
        raise MmluProConversionError(
            "MMLU-Pro validation must contain exactly "
            f"{demonstrations_per_category} demonstrations per category: {wrong_counts}"
        )
    return test, validation


def _source_summary(
    test_rows: list[dict[str, Any]], validation_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "test_rows_sha256": canonical_sha256(test_rows),
        "validation_rows_sha256": canonical_sha256(validation_rows),
        "test_category_counts": dict(sorted(Counter(row["category"] for row in test_rows).items())),
        "validation_category_counts": dict(
            sorted(Counter(row["category"] for row in validation_rows).items())
        ),
    }


def _format_question(row: Mapping[str, Any]) -> str:
    options = "\n".join(
        f"{_LABELS[index]}. {option}" for index, option in enumerate(row["options"])
    )
    return f"Question:\n{row['question']}\nOptions:\n{options}"


def _five_shot_prompt(
    row: Mapping[str, Any], demonstrations: list[dict[str, Any]]
) -> str:
    sections = [
        f"Protocol: {FIVE_SHOT_PROTOCOL_ID}",
        f"Category: {row['category']}",
        f"Study the {len(demonstrations)} same-category demonstrations, reason step by step, "
        "and answer the test question. Replace X with the selected option label in the "
        "required final marker.",
    ]
    for index, demonstration in enumerate(demonstrations, start=1):
        sections.append(
            f"Demonstration {index}:\n{_format_question(demonstration)}\n"
            f"Reasoning:\n{demonstration['cot_content']}\n"
            f"Final answer:\n[ANSWER:{demonstration['answer']}]"
        )
    sections.append(
        f"Test question:\n{_format_question(row)}\n"
        "Reason step by step. Your response must end with exactly one final marker on its own "
        "line, in this form:\n[ANSWER:X]"
    )
    return "\n\n".join(sections)


def _zero_shot_prompt(row: Mapping[str, Any]) -> str:
    return (
        f"Protocol: {ZERO_SHOT_PROTOCOL_ID}\n\n"
        f"{_format_question(row)}\n\n"
        "Choose the best option. Replace X with its label. Your response must contain no other "
        "text and its final line must be:\n[ANSWER:X]"
    )


def _profile_targets(
    profile_targets: Mapping[str, int] | None, case_count: int
) -> dict[str, int]:
    targets = dict(DEFAULT_PROFILE_TARGETS if profile_targets is None else profile_targets)
    if set(targets) != set(DEFAULT_PROFILE_TARGETS):
        raise MmluProConversionError(
            f"profile targets must be named {list(DEFAULT_PROFILE_TARGETS)!r}"
        )
    normalized: dict[str, int] = {}
    for name in DEFAULT_PROFILE_TARGETS:
        target = _positive_integer(targets[name], f"profile target {name!r}")
        if target > case_count:
            raise MmluProConversionError(
                f"profile target {name!r} exceeds test case count {case_count}"
            )
        normalized[name] = target
    if normalized["full"] != case_count:
        raise MmluProConversionError("full profile must contain every test case")
    return normalized


def _stratified_profile(
    cases: list[dict[str, Any]], *, name: str, target: int, seed: str
) -> tuple[list[str], dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        groups[case["metadata"]["category"]].append(case)
    categories = sorted(groups)
    for category in categories:
        groups[category].sort(
            key=lambda case: (
                _stable_hex(seed, "candidate", case["case_id"]),
                case["case_id"],
            )
        )

    base, remainder = divmod(target, len(categories))
    remainder_order = sorted(
        categories, key=lambda category: (_stable_hex(seed, "quota", category), category)
    )
    requested = {category: base for category in categories}
    for category in remainder_order[:remainder]:
        requested[category] += 1

    selected_cases: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    shortfall = 0
    for category in categories:
        chosen = groups[category][:requested[category]]
        selected_cases.extend(chosen)
        selected_ids.update(case["case_id"] for case in chosen)
        shortfall += max(0, requested[category] - len(chosen))

    fill_count = target - len(selected_cases)
    if fill_count:
        remaining = [case for case in cases if case["case_id"] not in selected_ids]
        remaining.sort(
            key=lambda case: (
                _stable_hex(seed, "fill", case["case_id"]),
                case["case_id"],
            )
        )
        selected_cases.extend(remaining[:fill_count])
    if len(selected_cases) != target:
        raise MmluProConversionError(f"profile {name!r} could not select {target} cases")
    selected_cases.sort(
        key=lambda case: (
            _stable_hex(seed, "output", case["case_id"]),
            case["case_id"],
        )
    )
    selected = [case["case_id"] for case in selected_cases]
    selected_counts = Counter(case["metadata"]["category"] for case in selected_cases)
    statistics = {
        "target": target,
        "selected": len(selected),
        "requested_per_category": requested,
        "selected_per_category": dict(sorted(selected_counts.items())),
        "shortfall_before_fill": shortfall,
        "filled_from_other_categories": fill_count,
    }
    return selected, statistics


def _profiles(
    cases: list[dict[str, Any]], *, seed: str, targets: Mapping[str, int]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    statistics: dict[str, Any] = {}
    for name in DEFAULT_PROFILE_TARGETS:
        target = targets[name]
        if name == "full":
            selected = sorted(case["case_id"] for case in cases)
            selected_counts = Counter(case["metadata"]["category"] for case in cases)
            profile_stats = {
                "target": target,
                "selected": len(selected),
                "selected_per_category": dict(sorted(selected_counts.items())),
                "shortfall_before_fill": 0,
                "filled_from_other_categories": 0,
            }
            strategy = "all-stable-case-id-order"
            profile_seed = None
        else:
            selected, profile_stats = _stratified_profile(
                cases, name=name, target=target, seed=seed
            )
            strategy = "stratified-category-fixed-ids"
            profile_seed = seed
        profiles.append(
            {
                "name": name,
                "strategy": strategy,
                "count": len(selected),
                "case_ids": selected,
                "case_ids_sha256": case_ids_sha256(selected),
                "dimensions": ["category", "subject"],
                "seed": profile_seed,
            }
        )
        statistics[name] = profile_stats
    return profiles, statistics


def _scorer() -> dict[str, Any]:
    config = {"labels": list(_LABELS), "allow_bare_final_label": False}
    return {
        "id": "choice",
        "version": "1",
        "config": config,
        "config_sha256": scorer_config_sha256(config),
    }


def _cases(
    test_rows: list[dict[str, Any]],
    *,
    protocol_id: str,
    prompt_builder: Callable[[Mapping[str, Any]], str],
) -> list[dict[str, Any]]:
    cases = []
    for source_line, row in enumerate(test_rows, start=1):
        cases.append(
            {
                "case_id": f"mmlu-pro-test-{row['question_id']}",
                "input": prompt_builder(row),
                "expected": row["answer"],
                "metadata": {
                    "source_line": source_line,
                    "source_id": str(row["question_id"]),
                    "language": "en",
                    "subject": row["src"],
                    "category": row["category"],
                    "difficulty": None,
                    "split": "test",
                    "tags": ["multiple-choice", "mmlu-pro", protocol_id],
                    "template_family": protocol_id,
                },
            }
        )
    return cases


def _dataset(
    *,
    cases: list[dict[str, Any]],
    pinned: dict[str, Any],
    protocol_id: str,
    converter_id: str,
    official_comparability: bool | dict[str, str],
    source_summary: Mapping[str, Any],
    seed: str,
    targets: Mapping[str, int],
    expected_test_rows: int,
    expected_validation_rows: int,
    expected_categories: int,
    expected_category_names: list[str],
    demonstrations_per_category: int,
    max_output_tokens: int,
    name: str,
    version: str,
) -> dict[str, Any]:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise MmluProConversionError("name must be a Direct LLM compatible identifier")
    if not isinstance(version, str) or not version.strip():
        raise MmluProConversionError("version must be non-empty")
    if not isinstance(seed, str) or not seed:
        raise MmluProConversionError("seed must be non-empty")

    cases = [
        DirectLlmCaseV2.model_validate(case).model_dump(mode="json")
        for case in cases
    ]
    targets = _profile_targets(targets, len(cases))
    profiles, profile_statistics = _profiles(cases, seed=seed, targets=targets)
    converter_config = {
        "protocol_id": protocol_id,
        "prompt_version": protocol_id,
        "answer_extractor": {
            "id": "choice-final-marker",
            "version": "1",
            "marker": "[ANSWER:{value}]",
            "require_final_line": True,
        },
        "option_preprocessing": {"drop_exact": ["N/A"], "preserve_order": True},
        "validation_demo_order": "pinned-source-row-order",
        "dataset_revision": pinned["dataset_revision"],
        "runner_revision": pinned["runner_revision"],
        "expected_test_rows": expected_test_rows,
        "expected_validation_rows": expected_validation_rows,
        "expected_categories": expected_categories,
        "expected_category_names": expected_category_names,
        "demonstrations_per_category": demonstrations_per_category,
        "profile_targets": dict(targets),
        "profile_statistics": profile_statistics,
        "source_summary": dict(source_summary),
        "official_comparability": official_comparability,
    }
    artifacts = pinned["artifacts"]
    provenance = {
        "source_id": SOURCE_ID,
        "source_kind": "managed-public",
        "homepage": SOURCE_HOMEPAGE,
        "upstream_revision": pinned["dataset_revision"],
        "artifacts": artifacts,
        "artifact_manifest_sha256": canonical_sha256(artifacts),
        "license": {
            "id": "unresolved-MIT-data-vs-Apache-2.0-code",
            "status": "pending",
            "evidence_urls": [SOURCE_HOMEPAGE, SOURCE_REPOSITORY],
            "commercial_use": "unknown",
            "redistribution": "unknown",
            "reviewed_at": None,
        },
        "converter": {
            "id": converter_id,
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
            "prompt_version": protocol_id,
            "max_output_tokens": max_output_tokens,
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


def convert_mmlu_pro_5shot_cot(
    test_rows: Iterable[Mapping[str, Any]],
    validation_rows: Iterable[Mapping[str, Any]],
    *,
    pinned_provenance: Mapping[str, Any],
    expected_test_rows: int = DEFAULT_EXPECTED_TEST_ROWS,
    expected_validation_rows: int = DEFAULT_EXPECTED_VALIDATION_ROWS,
    expected_categories: int = DEFAULT_EXPECTED_CATEGORIES,
    expected_category_names: Iterable[str] | None = None,
    demonstrations_per_category: int = DEFAULT_DEMONSTRATIONS_PER_CATEGORY,
    profile_targets: Mapping[str, int] | None = None,
    seed: str = FIVE_SHOT_DEFAULT_SEED,
    name: str = "mmlu-pro-5shot-cot-direct",
    version: str = "1",
) -> dict[str, Any]:
    """Build the parity-pending category-specific five-shot CoT protocol."""
    pinned = _strict_pinned_provenance(pinned_provenance)
    test, validation = _validated_splits(
        test_rows,
        validation_rows,
        expected_test_rows=expected_test_rows,
        expected_validation_rows=expected_validation_rows,
        expected_categories=expected_categories,
        expected_category_names=expected_category_names,
        demonstrations_per_category=demonstrations_per_category,
    )
    demonstrations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in validation:
        demonstrations[row["category"]].append(row)
    cases = _cases(
        test,
        protocol_id=FIVE_SHOT_PROTOCOL_ID,
        prompt_builder=lambda row: _five_shot_prompt(row, demonstrations[row["category"]]),
    )
    targets = DEFAULT_PROFILE_TARGETS if profile_targets is None else profile_targets
    return _dataset(
        cases=cases,
        pinned=pinned,
        protocol_id=FIVE_SHOT_PROTOCOL_ID,
        converter_id=FIVE_SHOT_CONVERTER_ID,
        official_comparability={"status": "not-established", "parity": "pending"},
        source_summary=_source_summary(test, validation),
        seed=seed,
        targets=targets,
        expected_test_rows=expected_test_rows,
        expected_validation_rows=expected_validation_rows,
        expected_categories=expected_categories,
        expected_category_names=sorted({row["category"] for row in test}),
        demonstrations_per_category=demonstrations_per_category,
        max_output_tokens=1024,
        name=name,
        version=version,
    )


def convert_mmlu_pro_zero_shot(
    test_rows: Iterable[Mapping[str, Any]],
    validation_rows: Iterable[Mapping[str, Any]],
    *,
    pinned_provenance: Mapping[str, Any],
    expected_test_rows: int = DEFAULT_EXPECTED_TEST_ROWS,
    expected_validation_rows: int = DEFAULT_EXPECTED_VALIDATION_ROWS,
    expected_categories: int = DEFAULT_EXPECTED_CATEGORIES,
    expected_category_names: Iterable[str] | None = None,
    demonstrations_per_category: int = DEFAULT_DEMONSTRATIONS_PER_CATEGORY,
    profile_targets: Mapping[str, int] | None = None,
    seed: str = ZERO_SHOT_DEFAULT_SEED,
    name: str = "mmlu-pro-zero-shot-direct",
    version: str = "1",
) -> dict[str, Any]:
    """Build the custom non-comparable zero-shot final-marker protocol."""
    pinned = _strict_pinned_provenance(pinned_provenance)
    test, _validation = _validated_splits(
        test_rows,
        validation_rows,
        expected_test_rows=expected_test_rows,
        expected_validation_rows=expected_validation_rows,
        expected_categories=expected_categories,
        expected_category_names=expected_category_names,
        demonstrations_per_category=demonstrations_per_category,
    )
    cases = _cases(
        test,
        protocol_id=ZERO_SHOT_PROTOCOL_ID,
        prompt_builder=_zero_shot_prompt,
    )
    targets = DEFAULT_PROFILE_TARGETS if profile_targets is None else profile_targets
    return _dataset(
        cases=cases,
        pinned=pinned,
        protocol_id=ZERO_SHOT_PROTOCOL_ID,
        converter_id=ZERO_SHOT_CONVERTER_ID,
        official_comparability=False,
        source_summary=_source_summary(test, _validation),
        seed=seed,
        targets=targets,
        expected_test_rows=expected_test_rows,
        expected_validation_rows=expected_validation_rows,
        expected_categories=expected_categories,
        expected_category_names=sorted({row["category"] for row in test}),
        demonstrations_per_category=demonstrations_per_category,
        max_output_tokens=16,
        name=name,
        version=version,
    )


convert_mmlu_pro_5shot = convert_mmlu_pro_5shot_cot
convert_mmlu_pro_zero_shot_direct = convert_mmlu_pro_zero_shot


__all__ = [
    "DEFAULT_CATEGORIES",
    "DEFAULT_DEMONSTRATIONS_PER_CATEGORY",
    "DEFAULT_EXPECTED_CATEGORIES",
    "DEFAULT_EXPECTED_TEST_ROWS",
    "DEFAULT_EXPECTED_VALIDATION_ROWS",
    "DEFAULT_PROFILE_SEED",
    "DEFAULT_PROFILE_TARGETS",
    "FIVE_SHOT_PROTOCOL_ID",
    "MmluProConversionError",
    "REQUIRED_ROW_FIELDS",
    "ZERO_SHOT_PROTOCOL_ID",
    "convert_mmlu_pro_5shot",
    "convert_mmlu_pro_5shot_cot",
    "convert_mmlu_pro_zero_shot",
    "convert_mmlu_pro_zero_shot_direct",
]
