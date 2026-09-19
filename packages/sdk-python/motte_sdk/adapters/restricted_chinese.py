"""Strict offline adapters for restricted C-Eval and CMMLU source rows.

The adapters only transform caller-supplied, already-local rows. They do not fetch,
publish, persist, or relax the source governance gate.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any
from urllib.parse import urlsplit

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

ROW_SCHEMA_VERSION = 1
REQUIRED_ROW_FIELDS = frozenset({"id", "question", "A", "B", "C", "D", "answer"})
LICENSE_EVIDENCE_FIELDS = frozenset(
    {
        "status",
        "declared_ids",
        "evidence_version",
        "evidence_urls",
        "citation",
        "attribution",
    }
)
ARTIFACT_FIELDS = frozenset({"logical_name", "url", "sha256", "bytes"})
DEFAULT_PROFILE_COUNTS = {"smoke": 50, "regression": 500}
PROFILE_NAMES = ("smoke", "regression", "full")
ANSWER_LABELS = tuple("ABCD")
CONVERTER_VERSION = "1"

CEVAL_DATASET_NAME = "ceval-zh-direct"
CEVAL_PROMPT_VERSION = "ceval-zh-direct-v1"
CEVAL_CONVERTER_ID = "ceval-to-direct"
CEVAL_PROFILE_SEED = "ceval-zh-profile-v1"

CMMLU_DATASET_NAME = "cmmlu-zh-direct"
CMMLU_PROMPT_VERSION = "cmmlu-zh-direct-v1"
CMMLU_CONVERTER_ID = "cmmlu-to-direct"
CMMLU_PROFILE_SEED = "cmmlu-zh-profile-v1"

_REVISION = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class RestrictedChineseConversionError(ValueError):
    """Pinned restricted rows cannot produce a valid Direct LLM v2 dataset."""


@dataclass(frozen=True)
class _SourceDefinition:
    source_id: str
    dataset_name: str
    prompt_version: str
    converter_id: str
    profile_seed: str
    homepage: str


_CEVAL = _SourceDefinition(
    source_id="ceval",
    dataset_name=CEVAL_DATASET_NAME,
    prompt_version=CEVAL_PROMPT_VERSION,
    converter_id=CEVAL_CONVERTER_ID,
    profile_seed=CEVAL_PROFILE_SEED,
    homepage="https://github.com/SJTU-LIT/ceval",
)
_CMMLU = _SourceDefinition(
    source_id="cmmlu",
    dataset_name=CMMLU_DATASET_NAME,
    prompt_version=CMMLU_PROMPT_VERSION,
    converter_id=CMMLU_CONVERTER_ID,
    profile_seed=CMMLU_PROFILE_SEED,
    homepage="https://github.com/haonan-li/CMMLU",
)


def _schema_error(label: str, actual: set[Any], expected: frozenset[str]) -> None:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected, key=repr)
    raise RestrictedChineseConversionError(
        f"{label} schema mismatch (missing={missing}, extra={extra})"
    )


def _text(value: Any, label: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RestrictedChineseConversionError(f"{label} must be non-empty text")
    normalized = value.strip()
    if max_length is not None and len(normalized) > max_length:
        raise RestrictedChineseConversionError(
            f"{label} must contain at most {max_length} characters"
        )
    return normalized


def _https_url(value: Any, label: str) -> str:
    url = _text(value, label, max_length=4096)
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url
    ):
        raise RestrictedChineseConversionError(
            f"{label} must not contain whitespace or control characters"
        )
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as error:
        raise RestrictedChineseConversionError(f"{label} must be a valid HTTPS URL") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RestrictedChineseConversionError(
            f"{label} must be an absolute credential-free HTTPS URL without query or fragment"
        )
    return url


def _strict_artifacts(
    artifacts: Iterable[Mapping[str, Any]], revision: str
) -> list[dict[str, Any]]:
    if isinstance(artifacts, (str, bytes, bytearray, Mapping)):
        raise RestrictedChineseConversionError("artifacts must be a non-empty iterable of objects")
    try:
        supplied = list(artifacts)
    except TypeError as error:
        raise RestrictedChineseConversionError(
            "artifacts must be a non-empty iterable of objects"
        ) from error
    if not supplied:
        raise RestrictedChineseConversionError("artifacts must be non-empty")

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(supplied, start=1):
        if not isinstance(raw, Mapping):
            raise RestrictedChineseConversionError(f"artifact {index} must be an object")
        actual = set(raw)
        if actual != ARTIFACT_FIELDS:
            _schema_error(f"artifact {index}", actual, ARTIFACT_FIELDS)
        logical_name = _text(raw["logical_name"], f"artifact {index} logical_name", max_length=256)
        url = _https_url(raw["url"], f"artifact {index} url")
        if revision not in url:
            raise RestrictedChineseConversionError(
                f"artifact {index} URL must contain the pinned revision"
            )
        sha256 = raw["sha256"]
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise RestrictedChineseConversionError(
                f"artifact {index} sha256 must be 64 lowercase hex characters"
            )
        size = raw["bytes"]
        if type(size) is not int or size <= 0:
            raise RestrictedChineseConversionError(
                f"artifact {index} bytes must be a positive integer"
            )
        normalized.append(
            {"logical_name": logical_name, "url": url, "sha256": sha256, "bytes": size}
        )

    names = [artifact["logical_name"] for artifact in normalized]
    if len(set(names)) != len(names):
        raise RestrictedChineseConversionError("artifact logical_name values must be unique")
    normalized.sort(key=lambda artifact: artifact["logical_name"])
    return normalized


def _strict_string_list(value: Any, label: str, *, urls: bool = False) -> list[str]:
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise RestrictedChineseConversionError(f"{label} must be a non-empty list")
    try:
        supplied = list(value)
    except TypeError as error:
        raise RestrictedChineseConversionError(f"{label} must be a non-empty list") from error
    if not supplied:
        raise RestrictedChineseConversionError(f"{label} must be non-empty")
    normalized = [
        _https_url(item, f"{label} entry")
        if urls
        else _text(item, f"{label} entry", max_length=128)
        for item in supplied
    ]
    if len(set(normalized)) != len(normalized):
        raise RestrictedChineseConversionError(f"{label} entries must be unique")
    return sorted(normalized)


def _strict_license_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RestrictedChineseConversionError("license_evidence must be an object")
    actual = set(value)
    if actual != LICENSE_EVIDENCE_FIELDS:
        _schema_error("license_evidence", actual, LICENSE_EVIDENCE_FIELDS)
    if value["status"] != "restricted":
        raise RestrictedChineseConversionError("license_evidence status must be 'restricted'")
    declared_ids = _strict_string_list(value["declared_ids"], "license declared_ids")
    evidence_urls = _strict_string_list(value["evidence_urls"], "license evidence_urls", urls=True)
    return {
        "status": "restricted",
        "declared_ids": declared_ids,
        "evidence_version": _text(
            value["evidence_version"], "license evidence_version", max_length=128
        ),
        "evidence_urls": evidence_urls,
        "citation": _text(value["citation"], "license citation", max_length=4096),
        "attribution": _text(value["attribution"], "license attribution", max_length=4096),
    }


def _source_row_id(value: Any, subject: str, source_line: int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise RestrictedChineseConversionError(
            f"subject {subject!r} row {source_line} id must be non-empty text or an integer"
        )
    normalized = str(value).strip()
    if not normalized:
        raise RestrictedChineseConversionError(
            f"subject {subject!r} row {source_line} id must be non-empty"
        )
    if len(normalized) > 256:
        raise RestrictedChineseConversionError(
            f"subject {subject!r} row {source_line} id is too long"
        )
    return normalized


def _strict_rows_by_subject(
    rows_by_subject: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    schema_version: int,
) -> list[dict[str, Any]]:
    if type(schema_version) is not int or schema_version != ROW_SCHEMA_VERSION:
        raise RestrictedChineseConversionError(
            f"unsupported row schema_version: {schema_version!r}"
        )
    if not isinstance(rows_by_subject, Mapping) or not rows_by_subject:
        raise RestrictedChineseConversionError(
            "rows_by_subject must be a non-empty subject-to-rows mapping"
        )

    normalized_subjects: dict[str, Iterable[Mapping[str, Any]]] = {}
    for raw_subject, subject_rows in rows_by_subject.items():
        subject = _text(raw_subject, "subject", max_length=256)
        if subject in normalized_subjects:
            raise RestrictedChineseConversionError(
                f"subject names must remain unique after trimming: {subject!r}"
            )
        normalized_subjects[subject] = subject_rows

    seen_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for subject in sorted(normalized_subjects):
        raw_subject_rows = normalized_subjects[subject]
        if isinstance(raw_subject_rows, (str, bytes, bytearray, Mapping)):
            raise RestrictedChineseConversionError(
                f"subject {subject!r} rows must be a non-empty iterable of objects"
            )
        try:
            supplied = list(raw_subject_rows)
        except TypeError as error:
            raise RestrictedChineseConversionError(
                f"subject {subject!r} rows must be a non-empty iterable of objects"
            ) from error
        if not supplied:
            raise RestrictedChineseConversionError(f"subject {subject!r} rows must be non-empty")
        for source_line, raw_row in enumerate(supplied, start=1):
            if not isinstance(raw_row, Mapping):
                raise RestrictedChineseConversionError(
                    f"subject {subject!r} row {source_line} must be an object"
                )
            actual = set(raw_row)
            if actual != REQUIRED_ROW_FIELDS:
                _schema_error(f"subject {subject!r} row {source_line}", actual, REQUIRED_ROW_FIELDS)
            source_id = _source_row_id(raw_row["id"], subject, source_line)
            if source_id in seen_ids:
                raise RestrictedChineseConversionError(
                    f"duplicate source id across subjects: {source_id!r}"
                )
            seen_ids.add(source_id)
            answer = raw_row["answer"]
            if not isinstance(answer, str) or answer not in ANSWER_LABELS:
                raise RestrictedChineseConversionError(
                    f"subject {subject!r} row {source_line} answer must be one of A-D"
                )
            row = {
                "id": source_id,
                "subject": subject,
                "source_line": source_line,
                "question": _text(
                    raw_row["question"],
                    f"subject {subject!r} row {source_line} question",
                ),
                "answer": answer,
            }
            for label in ANSWER_LABELS:
                row[label] = _text(
                    raw_row[label],
                    f"subject {subject!r} row {source_line} option {label}",
                )
            rows.append(row)
    return rows


def _stable_hex(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _prompt(row: Mapping[str, Any], definition: _SourceDefinition) -> str:
    return (
        f"协议：{definition.prompt_version}\n"
        f"科目：{row['subject']}\n"
        f"题目：{row['question']}\n"
        f"A. {row['A']}\n"
        f"B. {row['B']}\n"
        f"C. {row['C']}\n"
        f"D. {row['D']}\n\n"
        "请选择唯一正确选项。只输出一个答案标记 [ANSWER:X]，其中 X 只能是 A、B、C 或 D；"
        "不要输出解释、推理过程或任何其他文本。"
    )


def _cases(
    rows: list[dict[str, Any]],
    *,
    definition: _SourceDefinition,
    split: str,
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for row in rows:
        identity = _stable_hex(definition.source_id, row["subject"], row["id"])
        case = {
            "case_id": f"{definition.source_id}-{identity}",
            "input": _prompt(row, definition),
            "expected": row["answer"],
            "metadata": {
                "source_line": row["source_line"],
                "source_id": row["id"],
                "language": "zh",
                "subject": row["subject"],
                "category": row["subject"],
                "difficulty": None,
                "split": split,
                "tags": [
                    "multiple-choice",
                    definition.source_id,
                    "restricted",
                    "not-publishable",
                ],
                "template_family": definition.prompt_version,
            },
        }
        cases.append(DirectLlmCaseV2.model_validate(case).model_dump(mode="json"))
    return cases


def _normalized_profile_counts(
    profile_counts: Mapping[str, int] | None, case_count: int
) -> dict[str, int]:
    if profile_counts is None:
        counts = {
            "smoke": min(DEFAULT_PROFILE_COUNTS["smoke"], case_count),
            "regression": min(DEFAULT_PROFILE_COUNTS["regression"], case_count),
            "full": case_count,
        }
    else:
        if not isinstance(profile_counts, Mapping) or set(profile_counts) != set(PROFILE_NAMES):
            raise RestrictedChineseConversionError(
                f"profile_counts must contain exactly {list(PROFILE_NAMES)!r}"
            )
        counts = dict(profile_counts)
    for name in PROFILE_NAMES:
        count = counts[name]
        if type(count) is not int or count <= 0:
            raise RestrictedChineseConversionError(
                f"profile count {name!r} must be a positive integer"
            )
        if count > case_count:
            raise RestrictedChineseConversionError(
                f"profile count {name!r} exceeds case count {case_count}"
            )
    if counts["full"] != case_count:
        raise RestrictedChineseConversionError("full profile must contain every case")
    if not counts["smoke"] <= counts["regression"] <= counts["full"]:
        raise RestrictedChineseConversionError(
            "profile counts must satisfy smoke <= regression <= full"
        )
    return counts


def _stratified_order(cases: list[dict[str, Any]], seed: str) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        subject = case["metadata"]["subject"]
        answer = case["expected"]
        assert isinstance(subject, str) and isinstance(answer, str)
        groups[(subject, answer)].append(case)
    ranked: list[tuple[Fraction, str, str, dict[str, Any]]] = []
    for (subject, answer), stratum_cases in groups.items():
        stratum_cases.sort(
            key=lambda case: (_stable_hex(seed, "candidate", case["case_id"]), case["case_id"])
        )
        size = len(stratum_cases)
        for index, case in enumerate(stratum_cases):
            ranked.append(
                (
                    Fraction(index, size),
                    _stable_hex(seed, "stratum", subject, answer),
                    _stable_hex(seed, "tie", case["case_id"]),
                    case,
                )
            )
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked]


def _selection_statistics(selected: list[dict[str, Any]]) -> dict[str, Any]:
    subject_counts = Counter(case["metadata"]["subject"] for case in selected)
    answer_counts = Counter(case["expected"] for case in selected)
    return {
        "count": len(selected),
        "subject_counts": dict(sorted(subject_counts.items())),
        "answer_label_distribution": {
            label: answer_counts.get(label, 0) for label in ANSWER_LABELS
        },
    }


def _profiles(
    cases: list[dict[str, Any]],
    *,
    seed: str,
    counts: Mapping[str, int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ordered = _stratified_order(cases, seed)
    profiles: list[dict[str, Any]] = []
    statistics: dict[str, Any] = {}
    for name in PROFILE_NAMES:
        selected_cases = ordered[: counts[name]]
        selected = [case["case_id"] for case in selected_cases]
        profiles.append(
            {
                "name": name,
                "strategy": "nested-stratified-subject-fixed-ids",
                "count": len(selected),
                "case_ids": selected,
                "case_ids_sha256": case_ids_sha256(selected),
                "dimensions": ["subject", "answer_label"],
                "seed": seed,
            }
        )
        statistics[name] = _selection_statistics(selected_cases)
    return profiles, statistics


def _subject_statistics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["subject"]].append(row)
    return {
        subject: {
            "count": len(subject_rows),
            "answer_label_distribution": {
                label: sum(row["answer"] == label for row in subject_rows)
                for label in ANSWER_LABELS
            },
        }
        for subject, subject_rows in sorted(grouped.items())
    }


def _scorer() -> dict[str, Any]:
    config = {"labels": list(ANSWER_LABELS), "allow_bare_final_label": False}
    return {
        "id": "choice",
        "version": "1",
        "config": config,
        "config_sha256": scorer_config_sha256(config),
    }


def _convert(
    rows_by_subject: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    definition: _SourceDefinition,
    revision: str,
    artifacts: Iterable[Mapping[str, Any]],
    license_evidence: Mapping[str, Any],
    split: str,
    schema_version: int,
    profile_counts: Mapping[str, int] | None,
    profile_seed: str,
    version: str,
) -> dict[str, Any]:
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise RestrictedChineseConversionError(
            "revision must be a pinned 40-character lowercase commit SHA"
        )
    split = _text(split, "split", max_length=128)
    profile_seed = _text(profile_seed, "profile_seed", max_length=256)
    version = _text(version, "version", max_length=64)
    artifact_records = _strict_artifacts(artifacts, revision)
    license_snapshot = _strict_license_evidence(license_evidence)
    rows = _strict_rows_by_subject(rows_by_subject, schema_version=schema_version)
    cases = _cases(rows, definition=definition, split=split)
    counts = _normalized_profile_counts(profile_counts, len(cases))
    profiles, profile_statistics = _profiles(cases, seed=profile_seed, counts=counts)

    converter_config = {
        "row_schema_version": schema_version,
        "required_row_fields": sorted(REQUIRED_ROW_FIELDS),
        "prompt_version": definition.prompt_version,
        "split": split,
        "profile_counts": dict(counts),
        "profile_seed": profile_seed,
        "profile_strategy": "nested-stratified-subject-fixed-ids",
        "profile_statistics": profile_statistics,
        "subject_statistics": _subject_statistics(rows),
        "license_evidence": license_snapshot,
        "governance": {
            "status": "restricted",
            "distribution_scope": "restricted",
            "publishable": False,
            "stable_eligible": False,
            "data_redistribution_allowed": False,
        },
        "official_comparability": False,
    }
    declared_license = ",".join(license_snapshot["declared_ids"])
    if len(declared_license) > 256:
        raise RestrictedChineseConversionError(
            "combined license declared_ids exceed provenance id length"
        )
    provenance = {
        "source_id": definition.source_id,
        "source_kind": "restricted-public",
        "homepage": definition.homepage,
        "upstream_revision": revision,
        "artifacts": artifact_records,
        "artifact_manifest_sha256": canonical_sha256(artifact_records),
        "license": {
            "id": declared_license,
            "status": "restricted",
            "evidence_urls": license_snapshot["evidence_urls"],
            "commercial_use": "noncommercial-only-declared",
            "redistribution": "restricted-declared",
            "reviewed_at": None,
        },
        "converter": {
            "id": definition.converter_id,
            "version": CONVERTER_VERSION,
            "config": converter_config,
            "config_sha256": converter_config_sha256(converter_config),
        },
        "synthetic": False,
    }
    record: dict[str, Any] = {
        "name": definition.dataset_name,
        "version": version,
        "contract_version": CONTRACT_VERSION,
        "eval": {
            "suite": SUITE,
            "id": DATASET_ID,
            "version": DATASET_VERSION,
            "selected_count": len(cases),
            "selection": SELECTION_ALL,
            "scorer": _scorer(),
            "prompt_version": definition.prompt_version,
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


def convert_ceval_direct(
    rows_by_subject: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    revision: str,
    artifacts: Iterable[Mapping[str, Any]],
    license_evidence: Mapping[str, Any],
    split: str = "test",
    schema_version: int,
    profile_counts: Mapping[str, int] | None = None,
    profile_seed: str = CEVAL_PROFILE_SEED,
    version: str = "1",
) -> dict[str, Any]:
    """Convert local C-Eval rows while retaining restricted governance semantics."""
    return _convert(
        rows_by_subject,
        definition=_CEVAL,
        revision=revision,
        artifacts=artifacts,
        license_evidence=license_evidence,
        split=split,
        schema_version=schema_version,
        profile_counts=profile_counts,
        profile_seed=profile_seed,
        version=version,
    )


def convert_cmmlu_direct(
    rows_by_subject: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    revision: str,
    artifacts: Iterable[Mapping[str, Any]],
    license_evidence: Mapping[str, Any],
    split: str = "test",
    schema_version: int,
    profile_counts: Mapping[str, int] | None = None,
    profile_seed: str = CMMLU_PROFILE_SEED,
    version: str = "1",
) -> dict[str, Any]:
    """Convert local CMMLU rows while retaining restricted governance semantics."""
    return _convert(
        rows_by_subject,
        definition=_CMMLU,
        revision=revision,
        artifacts=artifacts,
        license_evidence=license_evidence,
        split=split,
        schema_version=schema_version,
        profile_counts=profile_counts,
        profile_seed=profile_seed,
        version=version,
    )


__all__ = [
    "ANSWER_LABELS",
    "CEVAL_CONVERTER_ID",
    "CEVAL_DATASET_NAME",
    "CEVAL_PROMPT_VERSION",
    "CMMLU_CONVERTER_ID",
    "CMMLU_DATASET_NAME",
    "CMMLU_PROMPT_VERSION",
    "DEFAULT_PROFILE_COUNTS",
    "LICENSE_EVIDENCE_FIELDS",
    "REQUIRED_ROW_FIELDS",
    "ROW_SCHEMA_VERSION",
    "RestrictedChineseConversionError",
    "convert_ceval_direct",
    "convert_cmmlu_direct",
]
