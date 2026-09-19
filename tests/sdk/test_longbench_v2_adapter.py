"""LongBench v2 full-context conversion and fail-closed aggregation."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy

import pytest

from motte_contracts.direct_llm_v2 import (
    DirectLlmDatasetV2,
    case_ids_sha256,
    converter_config_sha256,
    dataset_fingerprint_v2,
    scenario_for_v2,
)
from motte_contracts.identity import canonical_sha256
from motte_eval.scorer_registry import resolve_scorer
from motte_sdk.adapters import (
    aggregate_longbench_v2 as public_aggregate,
    convert_longbench_v2_direct as public_convert,
)
from motte_sdk.adapters.longbench_v2 import (
    CONTEXT_ESTIMATOR,
    DEFAULT_EXPECTED_DOMAINS,
    DEFAULT_EXPECTED_ROWS,
    DEFAULT_PROFILE_TARGETS,
    PROTOCOL_ID,
    REQUIRED_ROW_FIELDS,
    LongBenchV2ConversionError,
    aggregate_longbench_v2,
    convert_longbench_v2_direct,
)
from motte_sdk.resolve import ManifestResolutionError, prepare_run
from motte_storage.resource_store import InMemoryResourceStore

REVISION = "a" * 40
DOMAINS = (
    "Single-Document QA",
    "Multi-Document QA",
    "Long In-context Learning",
    "Long-dialogue History",
    "Code Repository Understanding",
    "Long Structured Data",
)
DIFFICULTIES = ("easy", "hard")
LENGTHS = ("short", "medium", "long")


def _row(index: int, *, long_context: bool = False) -> dict[str, str]:
    domain = DOMAINS[index % len(DOMAINS)]
    context = f"CONTEXT-BEGIN-{index}\nSource 文档 context for case {index}.\nCONTEXT-END-{index}"
    if long_context:
        context = f"CONTEXT-BEGIN-{index}\n{'x' * 5000}\nCONTEXT-END-{index}"
    return {
        "_id": f"lbv2-{index:04d}",
        "domain": domain,
        "sub_domain": f"{domain} / subtype {index % 3}",
        "difficulty": DIFFICULTIES[index % len(DIFFICULTIES)],
        "length": LENGTHS[index % len(LENGTHS)],
        "context": context,
        "question": f"Which option answers case {index}?",
        "choice_A": f"answer A for {index}",
        "choice_B": f"answer B for {index}",
        "choice_C": f"answer C for {index}",
        "choice_D": f"answer D for {index}",
        "answer": "ABCD"[index % 4],
    }


@pytest.fixture(scope="module")
def rows() -> list[dict[str, str]]:
    return [_row(index, long_context=index == 0) for index in range(DEFAULT_EXPECTED_ROWS)]


@pytest.fixture(scope="module")
def artifacts() -> list[dict[str, object]]:
    return [
        {
            "logical_name": "test.json",
            "url": f"https://huggingface.co/datasets/THUDM/LongBench-v2/resolve/{REVISION}/data/test.json",
            "sha256": "b" * 64,
            "bytes": 123456,
        }
    ]


@pytest.fixture(scope="module")
def dataset(rows, artifacts) -> dict:
    return convert_longbench_v2_direct(rows, revision=REVISION, artifacts=artifacts)


def _rehash(dataset: dict) -> dict:
    dataset["cases_sha256"] = canonical_sha256(dataset["cases"])
    for profile in dataset["profiles"]:
        profile["count"] = len(profile["case_ids"])
        profile["case_ids_sha256"] = case_ids_sha256(profile["case_ids"])
    dataset["profiles_sha256"] = canonical_sha256(dataset["profiles"])
    config = dataset["provenance"]["converter"]["config"]
    dataset["provenance"]["converter"]["config_sha256"] = converter_config_sha256(config)
    dataset.pop("dataset_fingerprint", None)
    dataset["dataset_fingerprint"] = dataset_fingerprint_v2(dataset)
    return DirectLlmDatasetV2.model_validate(dataset).model_dump(mode="json")


def test_public_exports_are_direct_functions():
    assert public_convert is convert_longbench_v2_direct
    assert public_aggregate is aggregate_longbench_v2


def test_full_conversion_validates_503_rows_six_domains_and_distribution(dataset, rows):
    validated = DirectLlmDatasetV2.model_validate(dataset)
    config = dataset["provenance"]["converter"]["config"]
    distribution = config["source_distribution"]

    assert REQUIRED_ROW_FIELDS == (
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
    assert validated.eval.selected_count == DEFAULT_EXPECTED_ROWS == 503
    assert len(dataset["cases"]) == 503
    assert len({case["case_id"] for case in dataset["cases"]}) == 503
    assert distribution["rows"] == {"test": 503}
    assert distribution["domain"] == dict(Counter(row["domain"] for row in rows))
    assert len(distribution["domain"]) == DEFAULT_EXPECTED_DOMAINS == 6
    assert distribution["sub_domain"] == dict(
        sorted(Counter(row["sub_domain"] for row in rows).items())
    )
    assert distribution["length"] == dict(sorted(Counter(row["length"] for row in rows).items()))
    assert distribution["difficulty"] == dict(
        sorted(Counter(row["difficulty"] for row in rows).items())
    )
    assert any(len(row["context"].encode("utf-8")) > len(row["context"]) for row in rows)
    context_bytes = sorted(len(row["context"].encode("utf-8")) for row in rows)
    assert distribution["context_utf8_bytes"] == {
        "max": context_bytes[-1],
        "p50": context_bytes[(50 * len(context_bytes) + 99) // 100 - 1],
        "p95": context_bytes[(95 * len(context_bytes) + 99) // 100 - 1],
    }
    assert dataset["eval"]["max_retries"] == 0
    assert dataset["eval"]["prompt_version"] == PROTOCOL_ID


def test_metadata_prompt_and_choice_scorer_preserve_source_content(dataset, rows):
    source = rows[0]
    case = dataset["cases"][0]
    metadata = case["metadata"]

    assert metadata == {
        "source_line": 1,
        "source_id": source["_id"],
        "language": "en",
        "subject": source["domain"],
        "category": source["sub_domain"],
        "difficulty": source["difficulty"],
        "split": "test",
        "tags": [
            "longbench-v2",
            "multiple-choice",
            "full-context",
            f"length:{source['length']}",
        ],
        "template_family": PROTOCOL_ID,
        "scorer": dataset["eval"]["scorer"],
    }
    assert case["expected"] == source["answer"]
    assert case["input"].count(source["context"]) == 1
    assert case["input"].count(source["question"]) == 1
    assert "CONTEXT-BEGIN-0" in case["input"] and "CONTEXT-END-0" in case["input"]
    assert case["input"].index(source["context"]) < case["input"].index(source["question"])
    assert source["context"] not in json.dumps(metadata, ensure_ascii=False)
    assert source["question"] not in json.dumps(metadata, ensure_ascii=False)
    for label in "ABCD":
        assert f"{label}. {source[f'choice_{label}']}" in case["input"]
    scorer = resolve_scorer(metadata["scorer"])
    assert scorer.implementation.scorer_id == "choice"
    assert dict(scorer.config) == {
        "labels": ["A", "B", "C", "D"],
        "allow_bare_final_label": False,
    }


def test_provenance_is_double_pinned_pending_and_never_claims_approval(dataset):
    provenance = dataset["provenance"]
    config = provenance["converter"]["config"]
    serialized = json.dumps(provenance, ensure_ascii=False).casefold()

    assert provenance["upstream_revision"] == REVISION
    assert REVISION in provenance["artifacts"][0]["url"]
    assert provenance["artifacts"][0]["sha256"] == "b" * 64
    assert config["immutable_pins"] == {
        "revision": REVISION,
        "artifact_manifest_sha256": provenance["artifact_manifest_sha256"],
    }
    assert provenance["license"]["status"] == "pending"
    assert provenance["license"]["commercial_use"] == "unknown"
    assert provenance["license"]["redistribution"] == "unknown"
    assert provenance["license"]["reviewed_at"] is None
    assert "approved" not in serialized
    assert config["official_comparability"]["status"] == "pending"
    assert config["context_preflight"] == {
        "owner": "sdk",
        "method": CONTEXT_ESTIMATOR,
        "conservative_upper_bound": True,
        "official_tokenizer_parity": False,
    }


def test_protocol_is_full_direct_without_alternate_context_paths(dataset):
    config = dataset["provenance"]["converter"]["config"]
    protocol = config["protocol"]
    serialized = json.dumps(config, sort_keys=True).casefold()

    assert protocol == {
        "id": PROTOCOL_ID,
        "mode": "full-direct",
        "context_policy": "full-verbatim-v1",
        "choice_order": "source-order",
        "answer_marker": "[ANSWER:{value}]",
        "require_final_marker": True,
    }
    for forbidden in ("truncate", "head-tail", "no-context", "retrieval-augmented", '"rag"'):
        assert forbidden not in serialized


def test_profiles_are_fixed_nested_and_cover_the_exact_full_set(dataset):
    profiles = {profile["name"]: profile for profile in dataset["profiles"]}
    assert list(profiles) == ["smoke", "regression", "full"]
    assert {name: profile["count"] for name, profile in profiles.items()} == (
        DEFAULT_PROFILE_TARGETS
    )
    smoke = set(profiles["smoke"]["case_ids"])
    regression = set(profiles["regression"]["case_ids"])
    full = set(profiles["full"]["case_ids"])
    assert smoke < regression < full
    assert full == {case["case_id"] for case in dataset["cases"]}
    assert (
        len({case["metadata"]["subject"] for case in dataset["cases"] if case["case_id"] in smoke})
        == 6
    )
    repeated = convert_longbench_v2_direct(
        [_row(index, long_context=index == 0) for index in range(503)],
        revision=REVISION,
        artifacts=dataset["provenance"]["artifacts"],
    )
    assert repeated["profiles"] == dataset["profiles"]
    assert repeated["dataset_fingerprint"] == dataset["dataset_fingerprint"]


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("answer", "E", "answer must be exactly"),
        ("context", "", "context must be non-empty"),
        ("question", "", "question must be non-empty"),
        ("choice_C", "   ", "choice_C must be non-empty"),
        ("difficulty", "", "difficulty must be non-empty"),
        ("length", "", "length must be non-empty"),
    ],
)
def test_rejects_invalid_required_row_values(rows, artifacts, field, value, match):
    changed = deepcopy(rows)
    changed[0][field] = value
    with pytest.raises(LongBenchV2ConversionError, match=match):
        convert_longbench_v2_direct(changed, revision=REVISION, artifacts=artifacts)


def test_rejects_schema_drift_duplicate_ids_wrong_counts_and_domains(rows, artifacts):
    missing = deepcopy(rows)
    missing[0].pop("context")
    with pytest.raises(LongBenchV2ConversionError, match="schema mismatch"):
        convert_longbench_v2_direct(missing, revision=REVISION, artifacts=artifacts)

    extra = deepcopy(rows)
    extra[0]["unexpected"] = "not in the source schema"
    with pytest.raises(LongBenchV2ConversionError, match="schema mismatch"):
        convert_longbench_v2_direct(extra, revision=REVISION, artifacts=artifacts)

    duplicate = deepcopy(rows)
    duplicate[1]["_id"] = duplicate[0]["_id"]
    with pytest.raises(LongBenchV2ConversionError, match="duplicates _id"):
        convert_longbench_v2_direct(duplicate, revision=REVISION, artifacts=artifacts)

    with pytest.raises(LongBenchV2ConversionError, match="row count mismatch"):
        convert_longbench_v2_direct(rows[:-1], revision=REVISION, artifacts=artifacts)
    with pytest.raises(LongBenchV2ConversionError, match="expected_rows is fixed at 503"):
        convert_longbench_v2_direct(rows[:12], REVISION, artifacts, expected_rows=12)
    with pytest.raises(LongBenchV2ConversionError, match="expected_domains is fixed at 6"):
        convert_longbench_v2_direct(rows, REVISION, artifacts, expected_domains=5)

    one_domain = deepcopy(rows)
    for row in one_domain:
        row["domain"] = "only-one"
    with pytest.raises(LongBenchV2ConversionError, match="domain count mismatch"):
        convert_longbench_v2_direct(one_domain, revision=REVISION, artifacts=artifacts)


def test_rejects_unpinned_revision_and_artifact_evidence(rows, artifacts):
    with pytest.raises(LongBenchV2ConversionError, match="40-character"):
        convert_longbench_v2_direct(rows, revision="main", artifacts=artifacts)

    changed = deepcopy(artifacts)
    changed[0]["url"] = "https://example.test/data/test.json"
    with pytest.raises(LongBenchV2ConversionError, match="pinned revision"):
        convert_longbench_v2_direct(rows, revision=REVISION, artifacts=changed)

    changed = deepcopy(artifacts)
    changed[0]["sha256"] = "unpinned"
    with pytest.raises(LongBenchV2ConversionError, match="64 lowercase"):
        convert_longbench_v2_direct(rows, revision=REVISION, artifacts=changed)


def test_aggregate_is_stable_for_identical_predictions_and_reports_all_dimensions(dataset):
    predictions = {case["case_id"]: case["expected"] for case in dataset["cases"]}
    reversed_predictions = dict(reversed(list(predictions.items())))

    first = aggregate_longbench_v2(dataset, predictions)
    second = aggregate_longbench_v2(dataset, reversed_predictions)

    assert first == second
    assert first["overall"] == {
        "selected": 503,
        "judged": 503,
        "correct": 503,
        "not_attempted": 0,
        "accuracy": 1.0,
        "coverage": 1.0,
        "outcomes": {
            "correct": 503,
            "wrong_answer": 0,
            "invalid_format": 0,
            "call_failed": 0,
            "no_expectation": 0,
            "not_attempted": 0,
        },
    }
    assert first["overall_publishable"] is True
    assert first["official_comparability"]["status"] == "pending"
    assert len(first["case_outcomes"]) == 503
    assert len(first["by_domain"]) == 6
    assert set(first["by_difficulty"]) == set(DIFFICULTIES)
    assert set(first["by_length"]) == set(LENGTHS)
    for dimension in ("by_domain", "by_difficulty", "by_length"):
        assert sum(group["selected"] for group in first[dimension].values()) == 503
        assert sum(group["correct"] for group in first[dimension].values()) == 503


def test_aggregate_materializes_missing_as_not_attempted_and_keeps_selected_denominator(dataset):
    predictions = {case["case_id"]: case["expected"] for case in dataset["cases"][:-3]}
    aggregate = aggregate_longbench_v2(dataset, predictions)
    overall = aggregate["overall"]

    assert overall["selected"] == 503
    assert overall["judged"] == overall["correct"] == 500
    assert overall["not_attempted"] == 3
    assert overall["accuracy"] == 500 / 503
    assert overall["coverage"] == 500 / 503
    assert overall["outcomes"] == {
        "correct": 500,
        "wrong_answer": 0,
        "invalid_format": 0,
        "call_failed": 0,
        "no_expectation": 0,
        "not_attempted": 3,
    }
    assert aggregate["overall_publishable"] is False
    assert [row["outcome"] for row in aggregate["case_outcomes"][-3:]] == [
        "not_attempted",
        "not_attempted",
        "not_attempted",
    ]

    explicit = {
        **predictions,
        **{case["case_id"]: None for case in dataset["cases"][-3:]},
    }
    assert aggregate_longbench_v2(dataset, explicit) == aggregate


def test_aggregate_accepts_standard_scores_and_rejects_duplicates_or_unknown_ids(dataset):
    first_case = dataset["cases"][0]
    second_case = dataset["cases"][1]
    scores = [
        {
            "case_id": first_case["case_id"],
            "outcome": "correct",
            "judged": True,
            "passed": True,
        },
        {
            "case_id": second_case["case_id"],
            "outcome": "call_failed",
            "judged": False,
            "passed": False,
        },
    ]
    aggregate = aggregate_longbench_v2(dataset, scores)
    assert aggregate["overall"]["selected"] == 503
    assert aggregate["overall"]["judged"] == 1
    assert aggregate["overall"]["correct"] == 1
    assert aggregate["overall"]["not_attempted"] == 501
    assert aggregate["overall"]["outcomes"] == {
        "correct": 1,
        "wrong_answer": 0,
        "invalid_format": 0,
        "call_failed": 1,
        "no_expectation": 0,
        "not_attempted": 501,
    }
    assert sum(aggregate["overall"]["outcomes"].values()) == 503
    assert aggregate["overall_publishable"] is False

    with pytest.raises(LongBenchV2ConversionError, match="duplicate score case_id"):
        aggregate_longbench_v2(dataset, [scores[0], scores[0]])
    with pytest.raises(LongBenchV2ConversionError, match="not in the full profile"):
        aggregate_longbench_v2(dataset, [{"case_id": "unknown", "prediction": "A"}])
    inconsistent = {**scores[0], "judged": False}
    with pytest.raises(LongBenchV2ConversionError, match="judged flag conflicts"):
        aggregate_longbench_v2(dataset, [inconsistent])
    with pytest.raises(LongBenchV2ConversionError, match="unknown outcome"):
        aggregate_longbench_v2(
            dataset, [{"case_id": first_case["case_id"], "outcome": {"bad": True}}]
        )


def test_aggregate_rejects_altered_protocol_profiles_pins_and_governance(dataset):
    altered_protocol = deepcopy(dataset)
    altered_protocol["provenance"]["converter"]["config"]["protocol"]["mode"] = "rag"
    with pytest.raises(LongBenchV2ConversionError, match="canonical"):
        aggregate_longbench_v2(_rehash(altered_protocol), {})

    altered_profile = deepcopy(dataset)
    altered_profile["profiles"][0]["case_ids"] = altered_profile["profiles"][0]["case_ids"][:1]
    with pytest.raises(LongBenchV2ConversionError, match="profile 'smoke'"):
        aggregate_longbench_v2(_rehash(altered_profile), {})

    altered_pin = deepcopy(dataset)
    altered_pin["provenance"]["converter"]["config"]["immutable_pins"]["revision"] = "b" * 40
    with pytest.raises(LongBenchV2ConversionError, match="immutable pins"):
        aggregate_longbench_v2(_rehash(altered_pin), {})

    altered_eval = deepcopy(dataset)
    altered_eval["eval"]["max_retries"] = 1
    with pytest.raises(LongBenchV2ConversionError, match="eval config"):
        aggregate_longbench_v2(_rehash(altered_eval), {})

    altered_license = deepcopy(dataset)
    altered_license["provenance"]["license"]["status"] = "approved"
    with pytest.raises(LongBenchV2ConversionError, match="remain pending"):
        aggregate_longbench_v2(_rehash(altered_license), {})


def test_aggregate_rejects_non_503_fixture(dataset):
    reduced = deepcopy(dataset)
    removed_id = reduced["cases"].pop()["case_id"]
    reduced["eval"]["selected_count"] = 502
    for profile in reduced["profiles"]:
        profile["case_ids"] = [case_id for case_id in profile["case_ids"] if case_id != removed_id]
    reduced = _rehash(reduced)
    with pytest.raises(LongBenchV2ConversionError, match="frozen protocol"):
        aggregate_longbench_v2(reduced, {})


def test_sdk_context_preflight_rejects_full_prompt_before_execution_or_provider_calls(
    dataset, monkeypatch
):
    import motte_provider.config as provider_config
    import motte_sdk.resolve as resolve_module

    resources = InMemoryResourceStore()
    resources.providers.put(
        {
            "name": "local",
            "kind": "openai_compatible",
            "base_url": "https://local.test/v1",
            "model": "unused",
        }
    )
    resources.models.put(
        {
            "id": "probe",
            "provider": "local",
            "model": "probe-1",
            "capabilities": {},
            "max_output_tokens": 64,
            "context_window": 1024,
        }
    )
    from motte_sdk.direct_llm_v2 import persist_direct_llm_v2_dataset
    from motte_sdk.publication import publication_audit

    published_dataset = deepcopy(dataset)
    published_dataset["provenance"].update({
        "source_id": "longbench-context-test-fixture",
        "source_kind": "synthetic-test",
        "synthetic": True,
    })
    published_dataset["provenance"]["license"]["status"] = "approved-test-only"
    published_dataset = _rehash(published_dataset)
    scenario = scenario_for_v2(published_dataset, version="1")
    audit = publication_audit(
        published_dataset, scenario, {"fixture": "longbench-context-preflight"},
        actor="pytest", entrypoint="sdk-test", published_at="2026-09-19T00:00:00Z",
    )
    persist_direct_llm_v2_dataset(
        published_dataset, resources, version="1", publication=audit,
    )
    calls = {"execution": 0, "provider": 0}

    def unexpected_execution(*args, **kwargs):
        calls["execution"] += 1
        raise AssertionError("execution resolution must follow context preflight")

    def unexpected_provider(*args, **kwargs):
        calls["provider"] += 1
        raise AssertionError("provider construction must follow context preflight")

    monkeypatch.setattr(resolve_module, "resolve_execution", unexpected_execution)
    monkeypatch.setattr(provider_config, "build_case_provider", unexpected_provider)

    with pytest.raises(ManifestResolutionError) as error:
        prepare_run(
            "longbench-v2-direct@1",
            {"model": "probe", "profile": "full"},
            [],
            resources,
        )

    assert error.value.code == "CONTEXT_WINDOW_EXCEEDED"
    assert "longbench-v2-lbv2-0000" in str(error.value)
    assert CONTEXT_ESTIMATOR in str(error.value)
    assert calls == {"execution": 0, "provider": 0}
    assert "CONTEXT-BEGIN-0" in dataset["cases"][0]["input"]
    assert "CONTEXT-END-0" in dataset["cases"][0]["input"]
