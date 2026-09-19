"""MMLU-Pro protocol adapters: strict, deterministic, and offline."""
from __future__ import annotations

from copy import deepcopy

import pytest

from motte_contracts.direct_llm_v2 import DirectLlmDatasetV2, scorer_config_sha256
from motte_sdk.adapters import (
    convert_mmlu_pro_5shot as public_convert_5shot,
    convert_mmlu_pro_5shot_cot as public_convert_5shot_cot,
    convert_mmlu_pro_zero_shot as public_convert_zero_shot,
    convert_mmlu_pro_zero_shot_direct as public_convert_zero_shot_direct,
)
from motte_sdk.adapters.mmlu_pro import (
    FIVE_SHOT_PROTOCOL_ID,
    ZERO_SHOT_PROTOCOL_ID,
    MmluProConversionError,
    convert_mmlu_pro_5shot,
    convert_mmlu_pro_5shot_cot,
    convert_mmlu_pro_zero_shot,
    convert_mmlu_pro_zero_shot_direct,
)

CATEGORIES = ("mathematics", "history")
PROFILE_TARGETS = {"smoke": 4, "regression": 6, "full": 8}


def _row(question_id: int, category: str, *, validation: bool = False) -> dict[str, object]:
    answer_index = question_id % 3
    return {
        "question_id": question_id,
        "question": f"Solve $x_{question_id}^2$ for café 漢字 in {category}.",
        "options": [
            f"first-{question_id}",
            f"second $\\frac{{1}}{{{question_id + 1}}}$",
            f"third-λ-{question_id}",
        ],
        "answer": "ABC"[answer_index],
        "answer_index": answer_index,
        "cot_content": (
            f"VALIDATION_COT_{category}_{question_id}"
            if validation else f"SECRET_TEST_COT_{question_id}"
        ),
        "category": category,
        "src": f"source-{category}",
    }


def _rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    test = [_row(index, CATEGORIES[index % 2]) for index in range(8)]
    validation = [
        _row(100 + category_index * 10 + demo_index, category, validation=True)
        for category_index, category in enumerate(CATEGORIES)
        for demo_index in range(2)
    ]
    return test, validation


def _pinned() -> dict[str, object]:
    return {
        "dataset_revision": "a" * 40,
        "runner_revision": "b" * 40,
        "artifacts": [
            {
                "logical_name": "test.parquet",
                "url": f"https://example.test/{'a' * 40}/test.parquet",
                "sha256": "c" * 64,
                "bytes": 123,
            },
            {
                "logical_name": "validation.parquet",
                "url": f"https://example.test/{'a' * 40}/validation.parquet",
                "sha256": "d" * 64,
                "bytes": 45,
            },
            {
                "logical_name": "official-evaluate.py",
                "url": f"https://example.test/{'b' * 40}/evaluate.py",
                "sha256": "e" * 64,
                "bytes": 67,
            },
        ],
    }


def _convert(converter, **kwargs):
    test, validation = _rows()
    return converter(
        test,
        validation,
        pinned_provenance=_pinned(),
        expected_test_rows=8,
        expected_validation_rows=4,
        expected_categories=2,
        demonstrations_per_category=2,
        profile_targets=PROFILE_TARGETS,
        **kwargs,
    )


def _keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def test_adapter_package_exports_all_mmlu_pro_converters_without_wrappers():
    assert public_convert_5shot is convert_mmlu_pro_5shot
    assert public_convert_5shot_cot is convert_mmlu_pro_5shot_cot
    assert public_convert_zero_shot is convert_mmlu_pro_zero_shot
    assert public_convert_zero_shot_direct is convert_mmlu_pro_zero_shot_direct


def test_both_protocols_emit_contract_valid_datasets_and_pinned_hashes():
    five_shot = _convert(convert_mmlu_pro_5shot_cot)
    zero_shot = _convert(convert_mmlu_pro_zero_shot)

    for dataset in (five_shot, zero_shot):
        validated = DirectLlmDatasetV2.model_validate(dataset)
        scorer = dataset["eval"]["scorer"]
        assert validated.dataset_fingerprint == dataset["dataset_fingerprint"]
        assert scorer["id"] == "choice" and scorer["version"] == "1"
        assert scorer["config_sha256"] == scorer_config_sha256(scorer["config"])
        assert not scorer["config_sha256"].endswith(dataset["cases_sha256"])
        assert all(
            len(artifact["sha256"]) == 64 and not artifact["sha256"].startswith("sha256:")
            for artifact in dataset["provenance"]["artifacts"]
        )
        assert dataset["provenance"]["upstream_revision"] == "a" * 40
        assert dataset["provenance"]["converter"]["config"]["runner_revision"] == "b" * 40
        assert dataset["eval"]["max_retries"] == 0
        assert "retry_wrong" not in set(_keys(dataset))
        assert "ground_truth_aware_retry" not in set(_keys(dataset))


def test_strict_schema_rejects_drift_duplicate_ids_and_bad_options():
    test, validation = _rows()
    test[0]["new_upstream_field"] = "drift"
    with pytest.raises(MmluProConversionError, match="schema mismatch"):
        convert_mmlu_pro_zero_shot(
            test,
            validation,
            pinned_provenance=_pinned(),
            expected_test_rows=8,
            expected_validation_rows=4,
            expected_categories=2,
            demonstrations_per_category=2,
            profile_targets=PROFILE_TARGETS,
        )

    test, validation = _rows()
    test[1]["question_id"] = test[0]["question_id"]
    with pytest.raises(MmluProConversionError, match="duplicates question_id"):
        _convert_rows(test, validation)

    test, validation = _rows()
    test[0]["options"] = ["only one"]
    with pytest.raises(MmluProConversionError, match="2-10"):
        _convert_rows(test, validation)

    test, validation = _rows()
    validation[0]["question_id"] = test[0]["question_id"]
    with pytest.raises(MmluProConversionError, match="disjoint across splits"):
        _convert_rows(test, validation)


def _convert_rows(test, validation):
    return convert_mmlu_pro_zero_shot(
        test,
        validation,
        pinned_provenance=_pinned(),
        expected_test_rows=8,
        expected_validation_rows=4,
        expected_categories=2,
        demonstrations_per_category=2,
        profile_targets=PROFILE_TARGETS,
    )


def test_answer_index_and_label_must_agree():
    test, validation = _rows()
    test[0]["answer"] = "J"
    with pytest.raises(MmluProConversionError, match="does not match answer_index"):
        _convert_rows(test, validation)

    test, validation = _rows()
    validation[0]["answer_index"] = True
    with pytest.raises(MmluProConversionError, match="outside options"):
        _convert_rows(test, validation)


def test_five_shot_uses_only_same_category_demos_and_never_test_cot():
    dataset = _convert(convert_mmlu_pro_5shot_cot)
    case = next(case for case in dataset["cases"] if case["metadata"]["category"] == "mathematics")
    prompt = case["input"]

    assert prompt.count("Demonstration ") == 2
    assert prompt.index("VALIDATION_COT_mathematics_100") < \
        prompt.index("VALIDATION_COT_mathematics_101")
    assert "VALIDATION_COT_mathematics" in prompt
    assert "VALIDATION_COT_history" not in prompt
    assert "SECRET_TEST_COT" not in prompt
    test_section = prompt.split("Test question:", 1)[1]
    assert f"[ANSWER:{case['expected']}]" not in test_section
    assert case["metadata"] == {
        "source_line": case["metadata"]["source_line"],
        "source_id": case["case_id"].removeprefix("mmlu-pro-test-"),
        "language": "en",
        "subject": "source-mathematics",
        "category": "mathematics",
        "difficulty": None,
        "split": "test",
        "tags": ["multiple-choice", "mmlu-pro", FIVE_SHOT_PROTOCOL_ID],
        "template_family": FIVE_SHOT_PROTOCOL_ID,
        "scorer": None,
    }


def test_zero_shot_has_no_demo_or_cot_and_preserves_unicode_latex_and_option_order():
    test, validation = _rows()
    test[0]["options"].append("N/A")
    dataset = _convert_rows(test, validation)
    prompt = dataset["cases"][0]["input"]

    assert "Demonstration" not in prompt
    assert "Reasoning:" not in prompt
    assert "VALIDATION_COT" not in prompt and "SECRET_TEST_COT" not in prompt
    assert "N/A" not in prompt
    assert prompt.endswith("[ANSWER:X]")
    assert "café 漢字" in prompt and "$x_0^2$" in prompt and "$\\frac{1}{1}$" in prompt
    assert prompt.index("A. first-0") < prompt.index("B. second") < prompt.index("C. third-λ-0")


def test_profiles_are_fixed_balanced_and_stably_fingerprinted():
    first = _convert(convert_mmlu_pro_zero_shot)
    second = _convert(convert_mmlu_pro_zero_shot)

    assert first["dataset_fingerprint"] == second["dataset_fingerprint"]
    assert first["cases_sha256"] == second["cases_sha256"]
    assert [profile["name"] for profile in first["profiles"]] == [
        "smoke", "regression", "full"
    ]
    assert [profile["count"] for profile in first["profiles"]] == [4, 6, 8]
    smoke_ids, regression_ids, full_ids = (
        set(profile["case_ids"]) for profile in first["profiles"]
    )
    assert smoke_ids < regression_ids < full_ids
    assert len(first["profiles"][2]["case_ids"]) == len(set(first["profiles"][2]["case_ids"]))
    stats = first["provenance"]["converter"]["config"]["profile_statistics"]
    assert stats["smoke"]["selected_per_category"] == {"history": 2, "mathematics": 2}
    assert stats["smoke"]["shortfall_before_fill"] == 0


def test_profiles_fill_category_shortfalls_deterministically_and_record_statistics():
    test, validation = _rows()
    for row in test[:-1]:
        row["category"] = "history"
        row["src"] = "source-history"
    test[-1]["category"] = "mathematics"
    test[-1]["src"] = "source-mathematics"

    first = _convert_rows(test, validation)
    second = _convert_rows(deepcopy(test), deepcopy(validation))
    stats = first["provenance"]["converter"]["config"]["profile_statistics"]["smoke"]

    assert first["profiles"][0]["case_ids"] == second["profiles"][0]["case_ids"]
    assert stats["selected_per_category"] == {"history": 3, "mathematics": 1}
    assert stats["shortfall_before_fill"] == 1
    assert stats["filled_from_other_categories"] == 1


def test_zero_shot_fingerprint_binds_validation_rows_without_putting_them_in_prompts():
    test, validation = _rows()
    first = _convert_rows(test, validation)
    validation[0]["cot_content"] += "-changed"
    changed = _convert_rows(test, validation)
    first_summary = first["provenance"]["converter"]["config"]["source_summary"]
    changed_summary = changed["provenance"]["converter"]["config"]["source_summary"]

    assert first["cases_sha256"] == changed["cases_sha256"]
    assert first_summary["validation_rows_sha256"] != changed_summary["validation_rows_sha256"]
    assert first["dataset_fingerprint"] != changed["dataset_fingerprint"]
    assert "VALIDATION_COT" not in changed["cases"][0]["input"]


def test_protocols_have_distinct_names_prompts_converters_and_fingerprints():
    five_shot = _convert(convert_mmlu_pro_5shot_cot)
    zero_shot = _convert(convert_mmlu_pro_zero_shot)
    five_config = five_shot["provenance"]["converter"]["config"]
    zero_config = zero_shot["provenance"]["converter"]["config"]

    assert five_shot["name"] != zero_shot["name"]
    assert five_shot["eval"]["prompt_version"] == FIVE_SHOT_PROTOCOL_ID
    assert zero_shot["eval"]["prompt_version"] == ZERO_SHOT_PROTOCOL_ID
    assert five_shot["provenance"]["converter"]["id"] != \
        zero_shot["provenance"]["converter"]["id"]
    assert five_config["official_comparability"] == {
        "status": "not-established", "parity": "pending"
    }
    assert zero_config["official_comparability"] is False
    assert five_shot["dataset_fingerprint"] != zero_shot["dataset_fingerprint"]
    assert five_shot["cases_sha256"] != zero_shot["cases_sha256"]
    assert [profile["case_ids"] for profile in five_shot["profiles"]] == [
        profile["case_ids"] for profile in zero_shot["profiles"]
    ]


def test_pinned_artifact_order_is_canonical():
    test, validation = _rows()
    normal = _convert_rows(test, validation)
    reversed_pin = _pinned()
    reversed_pin["artifacts"].reverse()
    reordered = convert_mmlu_pro_zero_shot(
        test,
        validation,
        pinned_provenance=reversed_pin,
        expected_test_rows=8,
        expected_validation_rows=4,
        expected_categories=2,
        demonstrations_per_category=2,
        profile_targets=PROFILE_TARGETS,
    )

    assert normal["provenance"]["artifacts"] == reordered["provenance"]["artifacts"]
    assert normal["dataset_fingerprint"] == reordered["dataset_fingerprint"]


def test_pinned_provenance_and_forbidden_retry_input_are_strict():
    test, validation = _rows()
    bad_pin = deepcopy(_pinned())
    bad_pin["dataset_revision"] = "main"
    with pytest.raises(MmluProConversionError, match="pinned 40-character"):
        convert_mmlu_pro_5shot_cot(
            test,
            validation,
            pinned_provenance=bad_pin,
            expected_test_rows=8,
            expected_validation_rows=4,
            expected_categories=2,
            demonstrations_per_category=2,
            profile_targets=PROFILE_TARGETS,
        )

    missing_runner = deepcopy(_pinned())
    missing_runner["artifacts"] = missing_runner["artifacts"][:2]
    with pytest.raises(MmluProConversionError, match="runner revision evidence"):
        convert_mmlu_pro_zero_shot(
            test,
            validation,
            pinned_provenance=missing_runner,
            expected_test_rows=8,
            expected_validation_rows=4,
            expected_categories=2,
            demonstrations_per_category=2,
            profile_targets=PROFILE_TARGETS,
        )

    bad_hash = deepcopy(_pinned())
    bad_hash["artifacts"][0]["sha256"] = "sha256:" + "c" * 64
    with pytest.raises(MmluProConversionError, match="64 lowercase hex"):
        convert_mmlu_pro_zero_shot(
            test,
            validation,
            pinned_provenance=bad_hash,
            expected_test_rows=8,
            expected_validation_rows=4,
            expected_categories=2,
            demonstrations_per_category=2,
            profile_targets=PROFILE_TARGETS,
        )

    test[0]["retry_wrong"] = True
    with pytest.raises(MmluProConversionError, match="schema mismatch"):
        _convert_rows(test, validation)


def test_full_12032_synthetic_capacity_and_default_profiles():
    categories = [f"category-{index:02d}" for index in range(14)]
    test = [_row(index, categories[index % 14]) for index in range(12_032)]
    validation = [
        _row(20_000 + category_index * 10 + demo_index, category, validation=True)
        for category_index, category in enumerate(categories)
        for demo_index in range(5)
    ]

    dataset = convert_mmlu_pro_zero_shot(
        test,
        validation,
        pinned_provenance=_pinned(),
        expected_category_names=categories,
    )

    assert len(dataset["cases"]) == 12_032
    assert [profile["name"] for profile in dataset["profiles"]] == [
        "smoke", "regression", "full"
    ]
    assert [profile["count"] for profile in dataset["profiles"]] == [56, 560, 12_032]
    stats = dataset["provenance"]["converter"]["config"]["profile_statistics"]
    assert set(stats["smoke"]["selected_per_category"].values()) == {4}
    assert set(stats["regression"]["selected_per_category"].values()) == {40}
    smoke_ids = set(dataset["profiles"][0]["case_ids"])
    smoke_categories = {
        case["metadata"]["category"] for case in dataset["cases"]
        if case["case_id"] in smoke_ids
    }
    assert smoke_categories == set(categories)
    DirectLlmDatasetV2.model_validate(dataset)
