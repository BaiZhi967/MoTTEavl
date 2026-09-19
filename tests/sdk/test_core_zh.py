"""Deterministic, offline tests for the project-owned Chinese benchmark generator."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from motte_contracts.direct_llm_v2 import DirectLlmDatasetV2, scorer_config_sha256
from motte_eval.scorer_registry import resolve_scorer
from motte_sdk.core_zh import (
    CANONICAL_SEED,
    CATEGORY_QUOTAS,
    CONVERTER_VERSION,
    GENERATOR_VERSION,
    PROMPT_VERSION,
    SCORER_PROFILE_VERSION,
    TOTAL_CASES,
    CoreZhFact,
    answer_distribution,
    build_dataset,
    build_fixture,
    build_quality_report,
    build_review_sample,
    generate_facts,
    primary_expected,
    recompute_expected,
    sample_random_facts,
    scan_sensitive_values,
)

EXPECTED_CANONICAL_FINGERPRINT = (
    "sha256:e36471050b087dbb1599aad0bda8a8d0dcab86ecc75113bd8040c34f5a6beecf"
)
SOURCE_SPEC = (
    Path(__file__).resolve().parents[2]
    / "datasets"
    / "direct-llm"
    / "sources"
    / "motte-core-zh.json"
)


@pytest.fixture(scope="module")
def dataset() -> dict:
    return build_dataset()


def test_canonical_dataset_has_exact_quotas_and_valid_v2_shape(dataset):
    validated = DirectLlmDatasetV2.model_validate(dataset)
    counts = Counter(case["metadata"]["category"] for case in dataset["cases"])

    assert len(dataset["cases"]) == TOTAL_CASES == 1000
    assert (
        dict(counts)
        == CATEGORY_QUOTAS
        == {
            "arithmetic": 160,
            "datetime": 90,
            "unit_conversion": 80,
            "set_logic": 70,
            "ticket_intent": 200,
            "json_extraction": 200,
            "format_instruction": 120,
            "noise_boundary": 80,
        }
    )
    assert validated.dataset_fingerprint == EXPECTED_CANONICAL_FINGERPRINT
    assert dataset["eval"]["prompt_version"] == PROMPT_VERSION
    assert dataset["provenance"]["upstream_revision"] == GENERATOR_VERSION
    assert dataset["provenance"]["converter"]["version"] == CONVERTER_VERSION
    assert dataset["provenance"]["converter"]["config"]["scorer_profile_version"] == (
        SCORER_PROFILE_VERSION
    )
    assert dataset["provenance"]["license"] == {
        "id": "project-owned-internal",
        "status": "pending",
        "evidence_urls": [],
        "commercial_use": "unknown",
        "redistribution": "unknown",
        "reviewed_at": None,
    }


def test_canonical_seed_versions_and_pending_source_governance_are_literal():
    assert CANONICAL_SEED == 20260919
    assert GENERATOR_VERSION == "motte-core-zh-generator-v1"
    assert CONVERTER_VERSION == "motte-core-zh-converter-v1"
    assert PROMPT_VERSION == "motte-core-zh-prompt-v1"
    assert SCORER_PROFILE_VERSION == "motte-core-zh-scorers-v1"

    source = json.loads(SOURCE_SPEC.read_text(encoding="utf-8"))
    assert source["upstream"]["revision"]["value"] == GENERATOR_VERSION
    assert source["conversion"]["converter"]["version"] == CONVERTER_VERSION
    assert source["conversion"]["prompt_version"] == PROMPT_VERSION
    assert source["conversion"]["scorer"]["version"] == SCORER_PROFILE_VERSION
    assert source["governance"] == {
        "status": "pending",
        "distribution_scope": "blocked",
        "stable_eligible": False,
        "reviewer": None,
        "reviewed_at": None,
        "decision_notes": (
            "Ownership, privacy, and stratified human review are pending; generation and "
            "publication remain blocked until attributable approval evidence exists."
        ),
    }
    assert source["license"]["commercial_use"] == "unknown"
    assert source["license"]["redistribution"] == "blocked-pending-review"
    assert source["blockers"] == [
        "The stratified 200-case dual human review is pending.",
        "The three-reference-model calibration is pending.",
        "Ownership and privacy approval is pending; generation and publication remain blocked.",
    ]


def test_every_case_has_stable_identity_typed_metadata_and_scorer_spec(dataset):
    ids = []
    source_ids = []
    scorer_ids = set()
    required_metadata = {
        "source_line",
        "source_id",
        "language",
        "subject",
        "category",
        "difficulty",
        "split",
        "tags",
        "template_family",
        "scorer",
    }
    for line_no, case in enumerate(dataset["cases"], 1):
        metadata = case["metadata"]
        scorer = metadata["scorer"]
        ids.append(case["case_id"])
        source_ids.append(metadata["source_id"])
        scorer_ids.add(scorer["id"])
        assert set(metadata) == required_metadata
        assert metadata["source_line"] == line_no
        assert metadata["language"] == "zh-CN"
        assert metadata["split"] == "generated"
        assert metadata["source_id"].startswith(f"{GENERATOR_VERSION}:")
        assert scorer["version"] == "1"
        assert scorer["config_sha256"] == scorer_config_sha256(scorer["config"])
        resolved = resolve_scorer(scorer)
        assert dict(resolved.config) == scorer["config"]
        assert resolved.config_sha256 == scorer["config_sha256"]
        assert case["input"].strip() and case["expected"].strip()
    assert len(ids) == len(set(ids)) == TOTAL_CASES
    assert len(source_ids) == len(set(source_ids)) == TOTAL_CASES
    assert scorer_ids == {"numeric", "choice", "json_equal", "exact"}


def test_generation_is_deterministic_and_different_seed_changes_identity(dataset):
    repeated = build_dataset(seed=CANONICAL_SEED)
    changed = build_dataset(seed=CANONICAL_SEED + 1)

    assert repeated == dataset
    assert repeated["dataset_fingerprint"] == EXPECTED_CANONICAL_FINGERPRINT
    assert changed["dataset_fingerprint"] != repeated["dataset_fingerprint"]
    assert changed["cases_sha256"] != repeated["cases_sha256"]
    assert [case["case_id"] for case in changed["cases"]] == [
        case["case_id"] for case in repeated["cases"]
    ]
    assert any(
        left["input"] != right["input"]
        for left, right in zip(changed["cases"], repeated["cases"], strict=True)
    )


def test_profiles_are_exact_stratified_and_smoke_covers_every_template_family(dataset):
    profiles = {profile["name"]: profile for profile in dataset["profiles"]}
    by_id = {case["case_id"]: case for case in dataset["cases"]}

    assert {name: profile["count"] for name, profile in profiles.items()} == {
        "smoke": 80,
        "regression": 500,
        "full": 1000,
    }
    smoke_categories = Counter(
        by_id[case_id]["metadata"]["category"] for case_id in profiles["smoke"]["case_ids"]
    )
    regression_categories = Counter(
        by_id[case_id]["metadata"]["category"] for case_id in profiles["regression"]["case_ids"]
    )
    all_families = {case["metadata"]["template_family"] for case in dataset["cases"]}
    smoke_families = Counter(
        by_id[case_id]["metadata"]["template_family"] for case_id in profiles["smoke"]["case_ids"]
    )
    assert dict(smoke_categories) == {category: 10 for category in CATEGORY_QUOTAS}
    assert dict(regression_categories) == {
        category: quota // 2 for category, quota in CATEGORY_QUOTAS.items()
    }
    assert set(smoke_families) == all_families
    assert min(smoke_families.values()) >= 2
    assert profiles["full"]["case_ids"] == [case["case_id"] for case in dataset["cases"]]


def test_all_1000_stored_golds_are_recomputed_by_independent_oracle(dataset):
    facts = generate_facts()
    assert len(facts) == len(dataset["cases"]) == 1000
    for fact, case in zip(facts, dataset["cases"], strict=True):
        assert recompute_expected(fact) == case["expected"]
        assert primary_expected(fact) == case["expected"]


def test_10000_random_facts_have_primary_and_independent_oracle_agreement():
    facts = sample_random_facts(10_000, seed=771923)
    assert len(facts) == 10_000
    assert Counter(fact.category for fact in facts).keys() == CATEGORY_QUOTAS.keys()
    assert all(primary_expected(fact) == recompute_expected(fact) for fact in facts)


def test_no_duplicate_prompt_real_pii_or_secret_and_choice_distribution_is_balanced(dataset):
    prompts = [case["input"] for case in dataset["cases"]]
    assert len(prompts) == len(set(prompts))
    assert scan_sensitive_values(dataset["cases"]) == []
    assert answer_distribution(dataset["cases"])["ticket_intent:choice"] == {
        "A": 50,
        "B": 50,
        "C": 50,
        "D": 50,
    }
    facts = generate_facts()
    assert Counter(
        fact.payload["priority"]
        for fact in facts
        if fact.category == "json_extraction" and "priority" in fact.payload
    ) == {False: 25, True: 25}
    assert Counter(
        fact.payload["active"]
        for fact in facts
        if fact.category == "json_extraction" and "active" in fact.payload
    ) == {False: 25, True: 25}


def test_sensitive_value_scan_rejects_non_reserved_values_and_accepts_reserved_values():
    unsafe = [
        {
            "case_id": "unsafe",
            "input": (
                "alice@real-company.cn 8.8.8.8 2001:4860:4860::8888 "
                "13812345678 11010519491231002X sk-abcdefghijklmnop"
            ),
            "expected": "x",
        }
    ]
    kinds = {issue["kind"] for issue in scan_sensitive_values(unsafe)}
    assert kinds == {
        "non-reserved-email",
        "non-documentation-ip",
        "phone-like-value",
        "national-id-like-value",
        "secret-like-value",
    }
    safe = [
        {
            "case_id": "safe",
            "input": (
                "test-user@example.invalid doc@example.com qa@subdomain.example.org "
                "runner@service.test local@service.example 192.0.2.10 2001:db8::10"
            ),
            "expected": "x",
        }
    ]
    assert scan_sensitive_values(safe) == []


@pytest.mark.parametrize(
    "fact,expected",
    [
        (
            CoreZhFact(
                "arithmetic",
                0,
                "boundary",
                "medium",
                {
                    "operation": "discount",
                    "list_price": 100,
                    "discount_percent": 15,
                },
            ),
            "85",
        ),
        (
            CoreZhFact(
                "datetime",
                0,
                "boundary",
                "hard",
                {
                    "operation": "day_difference",
                    "start": "2024-02-28",
                    "end": "2024-03-01",
                },
            ),
            "2",
        ),
        (
            CoreZhFact(
                "datetime",
                0,
                "boundary",
                "hard",
                {
                    "operation": "timezone",
                    "local": "2026-12-31T23:30",
                    "source_offset": 8,
                    "target_offset": 9,
                    "options": [
                        "2027-01-01 00:30",
                        "2026-12-31 22:30",
                        "2027-01-01 01:30",
                        "2026-12-31 23:30",
                    ],
                },
            ),
            "A",
        ),
        (
            CoreZhFact(
                "unit_conversion",
                0,
                "boundary",
                "medium",
                {
                    "operation": "scale",
                    "amount": 125,
                    "numerator": 1,
                    "denominator": 100,
                    "source_unit": "厘米",
                    "target_unit": "米",
                },
            ),
            "1.25",
        ),
        (
            CoreZhFact(
                "set_logic",
                0,
                "boundary",
                "easy",
                {
                    "operation": "sort_unique",
                    "values": [3, 1, 3, 2, 1],
                },
            ),
            "1,2,3",
        ),
        (
            CoreZhFact(
                "ticket_intent",
                0,
                "boundary",
                "medium",
                {
                    "operation": "ticket_intent",
                    "issue": "account_locked",
                    "style": 0,
                    "ticket_id": "TEST-BOUNDARY",
                },
            ),
            "C",
        ),
        (
            CoreZhFact(
                "json_extraction",
                0,
                "boundary",
                "medium",
                {
                    "operation": "missing",
                    "record_id": "TEST-MISSING",
                    "amount": 9,
                    "owner": None,
                },
            ),
            '{"amount":9,"owner":null,"record_id":"TEST-MISSING"}',
        ),
        (
            CoreZhFact(
                "format_instruction",
                0,
                "boundary",
                "easy",
                {
                    "operation": "status_line",
                    "record_id": "TEST-FMT",
                    "status": "OK",
                },
            ),
            "[RESULT|id=TEST-FMT|status=OK]",
        ),
        (
            CoreZhFact(
                "noise_boundary",
                0,
                "boundary",
                "hard",
                {
                    "operation": "corrected_order",
                    "order_id": "TEST-ORDER",
                    "original_quantity": 3,
                    "corrected_quantity": 4,
                },
            ),
            '{"order_id":"TEST-ORDER","quantity":4}',
        ),
        (
            CoreZhFact(
                "noise_boundary",
                0,
                "boundary",
                "hard",
                {
                    "operation": "inclusive_boundary",
                    "value": 10,
                    "threshold": 10,
                },
            ),
            "A",
        ),
    ],
)
def test_independent_oracle_boundary_examples(fact, expected):
    assert primary_expected(fact) == expected
    assert recompute_expected(fact) == expected


def test_local_generation_manifest_is_real_deterministic_evidence_not_remote_claim(dataset):
    artifacts = dataset["provenance"]["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["logical_name"] == "generation-recipe"
    assert artifacts[0]["url"].startswith("urn:motteavl:generator:motte-core-zh:")
    assert len(artifacts[0]["sha256"]) == 64
    assert artifacts[0]["bytes"] > 0
    assert dataset["provenance"]["homepage"] is None
    assert dataset["provenance"]["synthetic"] is True


def test_small_fixture_has_at_most_two_cases_per_template_family():
    one_each = build_fixture(per_template_family=1)
    two_each = build_fixture(per_template_family=2)
    one_counts = Counter(case["metadata"]["template_family"] for case in one_each)
    two_counts = Counter(case["metadata"]["template_family"] for case in two_each)
    assert one_counts and set(one_counts.values()) == {1}
    assert two_counts and max(two_counts.values()) <= 2
    with pytest.raises(ValueError, match="must be 1 or 2"):
        build_fixture(per_template_family=3)


def test_review_sample_supports_advertised_minimum_and_full_boundaries(dataset):
    minimum = build_review_sample(dataset=dataset, count=8)
    full = build_review_sample(dataset=dataset, count=1000)
    assert Counter(item["metadata"]["category"] for item in minimum["items"]) == {
        category: 1 for category in CATEGORY_QUOTAS
    }
    assert Counter(item["metadata"]["category"] for item in full["items"]) == CATEGORY_QUOTAS
    assert {item["case_id"] for item in full["items"]} == {
        case["case_id"] for case in dataset["cases"]
    }
    for count in (7, 1001):
        with pytest.raises(ValueError, match="review count must be between"):
            build_review_sample(dataset=dataset, count=count)


def test_review_sample_and_quality_report_leave_human_gates_pending(dataset):
    sample = build_review_sample(dataset=dataset)
    sample_categories = Counter(item["metadata"]["category"] for item in sample["items"])
    assert sample["status"] == "pending-human"
    assert sample["selection"]["count"] == len(sample["items"]) == 200
    assert dict(sample_categories) == {
        category: quota // 5 for category, quota in CATEGORY_QUOTAS.items()
    }
    assert all(
        item["review"]
        == {
            "status": "pending-human",
            "required_reviewers": 2,
            "completed_reviewers": 0,
            "decision": None,
            "notes": None,
        }
        for item in sample["items"]
    )

    report = build_quality_report(oracle_sample_count=256)
    assert report["automated"]["status"] == "passed"
    assert report["automated"]["checks"]["random_oracle_sample_count"] == 256
    assert report["automated"]["checks"]["smoke_covers_all_template_families"] is True
    assert report["automated"]["checks"]["json_booleans_balanced"] is True
    assert report["human_review"]["status"] == "pending-human"
    assert report["human_review"]["completed_cases"] == 0
    assert report["model_calibration"]["status"] == "pending-human"
    assert report["model_calibration"]["completed_reference_models"] == 0
    assert report["release"]["status"] == "blocked"
    assert report["release"]["stable_eligible"] is False
