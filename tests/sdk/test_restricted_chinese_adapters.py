"""Restricted Chinese adapters remain strict, offline, and non-publishable."""

from __future__ import annotations

import ast
import inspect
from copy import deepcopy
from typing import Any

import pytest

from motte_contracts.direct_llm_v2 import (
    DirectLlmDatasetV2,
    case_ids_sha256,
    dataset_fingerprint_v2,
)
from motte_sdk.adapters import (
    convert_ceval_direct as public_convert_ceval_direct,
    convert_cmmlu_direct as public_convert_cmmlu_direct,
)
import motte_sdk.adapters.restricted_chinese as restricted_chinese
from motte_sdk.adapters.restricted_chinese import (
    CEVAL_CONVERTER_ID,
    CEVAL_DATASET_NAME,
    CEVAL_PROMPT_VERSION,
    CMMLU_CONVERTER_ID,
    CMMLU_DATASET_NAME,
    CMMLU_PROMPT_VERSION,
    RestrictedChineseConversionError,
    convert_ceval_direct,
    convert_cmmlu_direct,
)

REVISION = "a" * 40
PROFILE_COUNTS = {"smoke": 3, "regression": 7, "full": 12}


def _row(index: int, *, answer: str | None = None) -> dict[str, object]:
    return {
        "id": index,
        "question": f"第 {index} 题的问题是什么？",
        "A": f"选项 A-{index}",
        "B": f"选项 B-{index}",
        "C": f"选项 C-{index}",
        "D": f"选项 D-{index}",
        "answer": answer or "ABCD"[(index - 1) % 4],
    }


def _rows() -> dict[str, list[dict[str, object]]]:
    return {
        "computer_network": [_row(index) for index in range(1, 5)],
        "modern_chinese_history": [_row(index) for index in range(5, 9)],
        "professional_law": [_row(index) for index in range(9, 13)],
    }


def _artifacts() -> list[dict[str, object]]:
    return [
        {
            "logical_name": "test.zip",
            "url": f"https://raw.example.test/source/{REVISION}/test.zip",
            "sha256": "b" * 64,
            "bytes": 1234,
        }
    ]


def _license_evidence() -> dict[str, object]:
    return {
        "status": "restricted",
        "declared_ids": ["CC-BY-NC-SA-4.0"],
        "evidence_version": "license-review-v1",
        "evidence_urls": [f"https://raw.example.test/source/{REVISION}/LICENSE"],
        "citation": "Restricted Chinese benchmark citation text.",
        "attribution": "Original benchmark authors; non-commercial share-alike terms apply.",
    }


def _convert(converter=convert_ceval_direct, **kwargs: Any) -> dict[str, Any]:
    profile_counts = kwargs.pop("profile_counts", PROFILE_COUNTS)
    schema_version = kwargs.pop("schema_version", 1)
    return converter(
        _rows(),
        revision=REVISION,
        artifacts=_artifacts(),
        license_evidence=_license_evidence(),
        schema_version=schema_version,
        profile_counts=profile_counts,
        **kwargs,
    )


def _walk(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_package_exports_both_converters_without_wrappers():
    assert public_convert_ceval_direct is convert_ceval_direct
    assert public_convert_cmmlu_direct is convert_cmmlu_direct


def test_ceval_and_cmmlu_are_distinct_valid_v2_datasets():
    ceval = _convert(convert_ceval_direct)
    cmmlu = _convert(convert_cmmlu_direct)

    assert DirectLlmDatasetV2.model_validate(ceval).dataset_fingerprint == (
        dataset_fingerprint_v2(ceval)
    )
    assert DirectLlmDatasetV2.model_validate(cmmlu).dataset_fingerprint == (
        dataset_fingerprint_v2(cmmlu)
    )
    assert ceval["name"] == CEVAL_DATASET_NAME
    assert cmmlu["name"] == CMMLU_DATASET_NAME
    assert ceval["name"] != cmmlu["name"]
    assert ceval["eval"]["prompt_version"] == CEVAL_PROMPT_VERSION
    assert cmmlu["eval"]["prompt_version"] == CMMLU_PROMPT_VERSION
    assert ceval["provenance"]["converter"]["id"] == CEVAL_CONVERTER_ID
    assert cmmlu["provenance"]["converter"]["id"] == CMMLU_CONVERTER_ID
    assert ceval["provenance"]["source_id"] == "ceval"
    assert cmmlu["provenance"]["source_id"] == "cmmlu"
    assert ceval["dataset_fingerprint"] != cmmlu["dataset_fingerprint"]
    assert not set(case["case_id"] for case in ceval["cases"]).intersection(
        case["case_id"] for case in cmmlu["cases"]
    )


def test_prompt_scorer_metadata_and_offline_policy_are_explicit():
    dataset = _convert()
    scorer = dataset["eval"]["scorer"]
    assert scorer["id"] == "choice"
    assert scorer["version"] == "1"
    assert scorer["config"] == {
        "labels": ["A", "B", "C", "D"],
        "allow_bare_final_label": False,
    }
    assert dataset["eval"]["max_retries"] == 0
    assert dataset["eval"]["max_output_tokens"] == 16

    case = dataset["cases"][0]
    assert "只输出一个答案标记 [ANSWER:X]" in case["input"]
    assert "不要输出解释、推理过程或任何其他文本" in case["input"]
    assert case["metadata"] == {
        "source_line": 1,
        "source_id": "1",
        "language": "zh",
        "subject": "computer_network",
        "category": "computer_network",
        "difficulty": None,
        "split": "test",
        "tags": ["multiple-choice", "ceval", "restricted", "not-publishable"],
        "template_family": CEVAL_PROMPT_VERSION,
        "scorer": None,
    }

    signature = inspect.signature(convert_ceval_direct)
    assert "status" not in signature.parameters
    assert "publishable" not in signature.parameters
    assert signature.parameters["schema_version"].default is inspect.Parameter.empty

    module_tree = ast.parse(inspect.getsource(restricted_chinese))
    imported_modules = {
        node.module
        for node in ast.walk(module_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(module_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not imported_modules.intersection(
        {
            "motte_sdk.dataset_sources",
            "motte_sdk.managed_sources",
            "urllib.request",
            "requests",
            "httpx",
        }
    )
    forbidden_calls = {
        "fetch",
        "publish",
        "require_source_action",
        "open",
        "write",
        "replace",
        "mkdir",
        "unlink",
    }
    called_names = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(module_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    assert not called_names.intersection(forbidden_calls)


def test_profiles_are_fixed_hashed_stratified_and_nested():
    first = _convert()
    second = _convert()
    assert first == second

    profiles = {profile["name"]: profile for profile in first["profiles"]}
    assert {name: profile["count"] for name, profile in profiles.items()} == PROFILE_COUNTS
    assert profiles["smoke"]["case_ids"] == profiles["regression"]["case_ids"][:3]
    assert profiles["regression"]["case_ids"] == profiles["full"]["case_ids"][:7]
    assert set(profiles["full"]["case_ids"]) == {case["case_id"] for case in first["cases"]}
    for profile in profiles.values():
        assert profile["strategy"] == "nested-stratified-subject-fixed-ids"
        assert profile["dimensions"] == ["subject", "answer_label"]
        assert profile["case_ids_sha256"] == case_ids_sha256(profile["case_ids"])

    changed = _convert(profile_seed="different-fixed-seed")
    changed_profiles = {profile["name"]: profile for profile in changed["profiles"]}
    assert changed_profiles["full"]["case_ids"] != profiles["full"]["case_ids"]
    assert changed["profiles_sha256"] != first["profiles_sha256"]
    assert changed["dataset_fingerprint"] != first["dataset_fingerprint"]


def test_converter_config_records_subject_and_answer_distributions():
    dataset = _convert()
    config = dataset["provenance"]["converter"]["config"]
    expected_distribution = {"A": 1, "B": 1, "C": 1, "D": 1}
    assert config["subject_statistics"] == {
        subject: {"count": 4, "answer_label_distribution": expected_distribution}
        for subject in sorted(_rows())
    }
    assert config["profile_counts"] == PROFILE_COUNTS
    assert config["profile_statistics"]["full"]["answer_label_distribution"] == {
        "A": 3,
        "B": 3,
        "C": 3,
        "D": 3,
    }
    assert config["official_comparability"] is False


def test_license_evidence_is_extractable_and_always_restricted():
    evidence = _license_evidence()
    dataset = _convert()
    provenance = dataset["provenance"]
    config = provenance["converter"]["config"]

    assert config["license_evidence"] == evidence
    assert config["license_evidence"]["citation"] == evidence["citation"]
    assert config["license_evidence"]["attribution"] == evidence["attribution"]
    assert provenance["license"]["status"] == "restricted"
    assert provenance["license"]["redistribution"] == "restricted-declared"
    assert config["governance"] == {
        "status": "restricted",
        "distribution_scope": "restricted",
        "publishable": False,
        "stable_eligible": False,
        "data_redistribution_allowed": False,
    }
    assert "approved" not in _walk(dataset)
    assert all(
        node.get("publishable") is not True
        for node in _walk(dataset)
        if isinstance(node, dict) and "publishable" in node
    )


@pytest.mark.parametrize(
    ("evidence", "match"),
    [
        ({}, "schema mismatch"),
        ({**_license_evidence(), "status": "approved"}, "must be 'restricted'"),
        ({**_license_evidence(), "declared_ids": []}, "declared_ids must be non-empty"),
        ({**_license_evidence(), "evidence_version": ""}, "evidence_version"),
        ({**_license_evidence(), "evidence_urls": []}, "evidence_urls must be non-empty"),
        ({**_license_evidence(), "citation": ""}, "citation"),
        ({**_license_evidence(), "attribution": ""}, "attribution"),
        ({**_license_evidence(), "unknown": "x"}, "schema mismatch"),
    ],
)
def test_license_evidence_is_required_strict_and_nonempty(evidence, match):
    with pytest.raises(RestrictedChineseConversionError, match=match):
        convert_ceval_direct(
            _rows(),
            revision=REVISION,
            artifacts=_artifacts(),
            license_evidence=evidence,
            schema_version=1,
            profile_counts=PROFILE_COUNTS,
        )


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda row: row.update({"unknown": "x"}), "schema mismatch"),
        (lambda row: row.update({"answer": "E"}), "one of A-D"),
        (lambda row: row.update({"answer": "a"}), "one of A-D"),
        (lambda row: row.update({"question": "  "}), "question must be non-empty"),
        (lambda row: row.update({"A": ""}), "option A must be non-empty"),
        (lambda row: row.update({"id": ""}), "id must be non-empty"),
    ],
)
def test_rows_are_exact_and_reject_bad_values(mutation, match):
    rows = _rows()
    mutation(rows["computer_network"][0])
    with pytest.raises(RestrictedChineseConversionError, match=match):
        convert_ceval_direct(
            rows,
            revision=REVISION,
            artifacts=_artifacts(),
            license_evidence=_license_evidence(),
            schema_version=1,
            profile_counts=PROFILE_COUNTS,
        )


def test_source_ids_are_globally_unique_across_subjects():
    rows = _rows()
    rows["professional_law"][0]["id"] = rows["computer_network"][0]["id"]
    with pytest.raises(RestrictedChineseConversionError, match="duplicate source id"):
        convert_cmmlu_direct(
            rows,
            revision=REVISION,
            artifacts=_artifacts(),
            license_evidence=_license_evidence(),
            schema_version=1,
            profile_counts=PROFILE_COUNTS,
        )


@pytest.mark.parametrize(
    ("revision", "artifact_mutation", "match"),
    [
        ("main", None, "40-character lowercase"),
        (REVISION.upper(), None, "40-character lowercase"),
        (REVISION, lambda item: item.update({"url": "http://example.test/data"}), "HTTPS"),
        (
            REVISION,
            lambda item: item.update({"url": f"https://exa mple.test/{REVISION}/data"}),
            "whitespace or control",
        ),
        (
            REVISION,
            lambda item: item.update({"url": f"https://example.test/{REVISION}/bad\tpath"}),
            "whitespace or control",
        ),
        (
            REVISION,
            lambda item: item.update({"url": "https://example.test/no-pinned-commit/data"}),
            "must contain the pinned revision",
        ),
        (REVISION, lambda item: item.update({"sha256": "B" * 64}), "lowercase hex"),
        (REVISION, lambda item: item.update({"bytes": 0}), "positive integer"),
        (REVISION, lambda item: item.update({"extra": "x"}), "schema mismatch"),
    ],
)
def test_revision_and_artifact_pins_are_strict(revision, artifact_mutation, match):
    artifacts = _artifacts()
    if artifact_mutation is not None:
        artifact_mutation(artifacts[0])
    with pytest.raises(RestrictedChineseConversionError, match=match):
        convert_ceval_direct(
            _rows(),
            revision=revision,
            artifacts=artifacts,
            license_evidence=_license_evidence(),
            schema_version=1,
            profile_counts=PROFILE_COUNTS,
        )


def test_schema_version_and_profile_counts_fail_closed():
    with pytest.raises(TypeError, match="schema_version"):
        convert_ceval_direct(
            _rows(),
            revision=REVISION,
            artifacts=_artifacts(),
            license_evidence=_license_evidence(),
            profile_counts=PROFILE_COUNTS,
        )
    with pytest.raises(RestrictedChineseConversionError, match="unsupported row schema_version"):
        _convert(schema_version=2)
    with pytest.raises(RestrictedChineseConversionError, match="smoke <= regression <= full"):
        _convert(profile_counts={"smoke": 8, "regression": 4, "full": 12})
    with pytest.raises(RestrictedChineseConversionError, match="full profile"):
        _convert(profile_counts={"smoke": 3, "regression": 7, "full": 11})


def test_subject_mapping_order_does_not_change_identity():
    rows = _rows()
    reversed_rows = dict(reversed(list(rows.items())))
    first = convert_ceval_direct(
        rows,
        revision=REVISION,
        artifacts=_artifacts(),
        license_evidence=_license_evidence(),
        schema_version=1,
        profile_counts=PROFILE_COUNTS,
    )
    second = convert_ceval_direct(
        reversed_rows,
        revision=REVISION,
        artifacts=deepcopy(_artifacts()),
        license_evidence=deepcopy(_license_evidence()),
        schema_version=1,
        profile_counts=PROFILE_COUNTS,
    )
    assert first == second
