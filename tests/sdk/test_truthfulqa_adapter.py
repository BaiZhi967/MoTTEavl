"""TruthfulQA Binary Direct adapter: deterministic, offline, review-stage only."""
from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter

import pytest

from motte_contracts.direct_llm_v2 import DirectLlmDatasetV2, dataset_fingerprint_v2
from motte_eval.scorer_registry import resolve_scorer
from motte_sdk.adapters.truthfulqa import (
    conversion_receipt,
    DEFAULT_PROFILE_TARGETS,
    REQUIRED_COLUMNS,
    TruthfulQaConversionError,
    convert_truthfulqa,
)

REVISION = "a" * 40


def _row(index: int, *, category: str | None = None, kind: str = "general") -> dict[str, str]:
    return {
        "Question": f"Which statement is true? {index}",
        "Best Answer": f"The measured result is {index}.",
        "Best Incorrect Answer": f"The measured result is not {index}.",
        "Category": category or f"category-{index % 3}",
        "Type": kind if index % 2 else "knowledge",
    }


def _artifact_for(raw: bytes) -> dict[str, object]:
    return {
        "logical_name": "TruthfulQA.csv",
        "url": f"https://raw.example.test/{REVISION}/TruthfulQA.csv",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def _csv(rows: list[dict[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(REQUIRED_COLUMNS), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _convert(rows: list[dict[str, str]], **kwargs):
    expected_rows = kwargs.pop("expected_rows", len(rows))
    profile_targets = kwargs.pop(
        "profile_targets", {"smoke": 5, "regression": 8, "full": len(rows)}
    )
    return convert_truthfulqa(
        rows,
        revision=REVISION,
        artifact={
            "logical_name": "TruthfulQA.csv",
            "url": "https://example.test/TruthfulQA.csv",
            "sha256": "b" * 64,
            "bytes": 100,
        },
        expected_rows=expected_rows,
        profile_targets=profile_targets,
        **kwargs,
    )


def test_csv_bytes_bind_to_pinned_artifact_and_strict_schema():
    raw = _csv([_row(1)])
    dataset = convert_truthfulqa(
        raw,
        revision=REVISION,
        artifact=_artifact_for(raw),
        expected_rows=1,
        profile_targets={"smoke": 1, "regression": 1, "full": 1},
    )
    assert dataset["provenance"]["upstream_revision"] == REVISION
    assert dataset["provenance"]["artifacts"][0]["bytes"] == len(raw)
    parsed = DirectLlmDatasetV2.model_validate(dataset)
    assert parsed.dataset_fingerprint == dataset_fingerprint_v2(dataset)
    receipt = conversion_receipt(dataset)
    assert receipt.official_comparability is False
    assert receipt.status == "experimental"
    assert receipt.governance_status == "pending"
    assert resolve_scorer(dataset["eval"]["scorer"]).scorer_id == "choice"

    broken_header = raw.replace(b"Best Incorrect Answer", b"Incorrect Answer")
    with pytest.raises(TruthfulQaConversionError, match="schema mismatch"):
        convert_truthfulqa(
            broken_header,
            revision=REVISION,
            artifact=_artifact_for(broken_header),
            expected_rows=1,
            profile_targets={"smoke": 1, "regression": 1, "full": 1},
        )
    with pytest.raises(TruthfulQaConversionError, match="artifact sha256"):
        convert_truthfulqa(
            raw,
            revision=REVISION,
            artifact={**_artifact_for(raw), "sha256": "c" * 64},
            expected_rows=1,
            profile_targets={"smoke": 1, "regression": 1, "full": 1},
        )


def test_unicode_normalization_duplicate_answers_and_duplicate_question_are_rejected():
    row = _row(1)
    row["Question"] = "Ａ  question\u00a0with  spaces"
    dataset = _convert([row])
    assert "Ａ" not in dataset["cases"][0]["input"]
    assert "question with spaces" in dataset["cases"][0]["input"]

    same_after_nfkc = _row(2)
    same_after_nfkc["Best Incorrect Answer"] = "Ａ"
    same_after_nfkc["Best Answer"] = "A"
    with pytest.raises(TruthfulQaConversionError, match="identical normalized"):
        _convert([same_after_nfkc])

    duplicate = dict(row)
    with pytest.raises(TruthfulQaConversionError, match="duplicates"):
        _convert([row, duplicate])


def test_shuffle_is_stable_and_seed_changes_mapping_and_fingerprint():
    rows = [_row(index) for index in range(12)]
    first = _convert(rows, seed="seed-a")
    second = _convert(rows, seed="seed-a")
    changed = _convert(rows, seed="seed-b")
    assert first == second
    assert first["dataset_fingerprint"] == dataset_fingerprint_v2(first)
    assert first["dataset_fingerprint"] != changed["dataset_fingerprint"]
    mappings = [
        tuple(tag for tag in case["metadata"]["tags"] if tag.startswith("shuffle:"))
        for case in first["cases"]
    ]
    changed_mappings = [
        tuple(tag for tag in case["metadata"]["tags"] if tag.startswith("shuffle:"))
        for case in changed["cases"]
    ]
    assert mappings != changed_mappings
    assert all(case["input"].endswith("nothing else.") for case in first["cases"])
    assert all("Best Answer" not in case["input"] for case in first["cases"])
    assert all("Best Incorrect Answer" not in case["input"] for case in first["cases"])


def test_profiles_are_deterministic_stratified_and_fixture_fallback_is_explicit():
    rows = [_row(index, category=f"cat-{index % 4}", kind=f"type-{index % 3}") for index in range(12)]
    dataset = _convert(rows)
    profiles = {profile["name"]: profile for profile in dataset["profiles"]}
    assert set(profiles) == set(DEFAULT_PROFILE_TARGETS)
    assert profiles["smoke"]["count"] == 5
    assert profiles["regression"]["count"] == 8
    assert profiles["full"]["count"] == 12
    case_by_id = {case["case_id"]: case for case in dataset["cases"]}
    for name in ("smoke", "regression"):
        categories = Counter(
            case_by_id[case_id]["metadata"]["category"]
            for case_id in profiles[name]["case_ids"]
        )
        assert sorted(categories.values()) == ([1, 1, 1, 2] if name == "smoke" else [2, 2, 2, 2])
    assert set(profiles["regression"]["case_ids"]).issubset(
        set(profiles["full"]["case_ids"])
    )
    assert profiles["smoke"]["strategy"].startswith("stratified")


def test_817_synthetic_rows_are_full_and_approximately_balanced():
    rows = [_row(index, category=f"cat-{index % 7}") for index in range(817)]
    dataset = convert_truthfulqa(
        rows,
        revision=REVISION,
        artifact={
            "logical_name": "TruthfulQA.csv",
            "url": "https://example.test/TruthfulQA.csv",
            "sha256": "d" * 64,
            "bytes": 817,
        },
    )
    assert len(dataset["cases"]) == 817
    assert [profile["count"] for profile in dataset["profiles"]] == [50, 300, 817]
    labels = [case["expected"] for case in dataset["cases"]]
    assert 0.4 < labels.count("A") / len(labels) < 0.6
    assert len({case["case_id"] for case in dataset["cases"]}) == 817
    assert all(case["metadata"]["source_line"] == index + 2 for index, case in enumerate(dataset["cases"]))


def test_pinned_revision_and_row_count_are_required():
    rows = [_row(1)]
    with pytest.raises(TruthfulQaConversionError, match="pinned"):
        convert_truthfulqa(
            rows,
            revision="main",
            artifact={"logical_name": "x", "url": "https://x", "sha256": "e" * 64, "bytes": 1},
            expected_rows=1,
            profile_targets={"smoke": 1, "regression": 1, "full": 1},
        )
    with pytest.raises(TruthfulQaConversionError, match="row count"):
        _convert(rows, expected_rows=817)
