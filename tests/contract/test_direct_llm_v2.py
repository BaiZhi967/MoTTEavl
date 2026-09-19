"""Pure Direct LLM v2 contract and suite-dispatch tests; no resolver or scorer execution."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from motte_contracts import suites
from motte_contracts.direct_llm import import_direct_llm_jsonl, scenario_for as scenario_for_v1
from motte_contracts.direct_llm_v2 import (
    CONTRACT_VERSION,
    DATASET_ID,
    DATASET_VERSION,
    PLUGIN_VERSION,
    SUITE,
    CaseMetadataV2,
    DirectLlmCaseV2,
    DirectLlmDatasetV2,
    ProvenanceV2,
    ScorerSpec,
    case_ids_sha256,
    converter_config_sha256,
    dataset_fingerprint_v2,
    scenario_for_v2,
    scorer_config_sha256,
    validate_dataset,
    validate_scenario,
)
from motte_contracts.identity import canonical_sha256, dataset_fingerprint
from motte_storage.resource_store import InMemoryResourceStore


def _scorer(scorer_id="exact", config=None):
    config = {} if config is None else config
    return {
        "id": scorer_id,
        "version": "1",
        "config": config,
        "config_sha256": scorer_config_sha256(config),
    }


def _case(case_id, source_line, *, prompt=False, scorer=None):
    record = {
        "case_id": case_id,
        "expected": f"answer-{source_line}",
        "metadata": {
            "source_line": source_line,
            "source_id": f"upstream-{source_line}",
            "language": "en",
            "subject": "physics",
            "category": "science",
            "difficulty": None,
            "split": "test",
            "tags": ["synthetic", "deterministic"],
            "template_family": "contract-v2-test",
        },
    }
    record["prompt" if prompt else "input"] = f"Question {source_line}"
    if scorer is not None:
        record["scorer"] = scorer
    return DirectLlmCaseV2.model_validate(record).model_dump(mode="json")


def _dataset():
    default_scorer = _scorer()
    cases = [
        _case("case-1", 1),
        _case("case-2", 2, prompt=True, scorer=_scorer("contains", {"case_sensitive": True})),
    ]
    artifacts = [
        {
            "logical_name": "test-jsonl",
            "url": "https://example.test/dataset/test.jsonl",
            "sha256": "a" * 64,
            "bytes": 2048,
        }
    ]
    converter_config = {"split": "test", "prompt_version": "contract-v2-test"}
    provenance = {
        "source_id": "contract-fixture",
        "source_kind": "synthetic-test",
        "homepage": "https://example.test/dataset",
        "upstream_revision": "commit-123",
        "artifacts": artifacts,
        "artifact_manifest_sha256": canonical_sha256(artifacts),
        "license": {
            "id": "project-owned-test",
            "status": "approved-test-only",
            "evidence_urls": ["https://example.test/license"],
            "commercial_use": "test-only",
            "redistribution": "test-only",
            "reviewed_at": None,
        },
        "converter": {
            "id": "contract-fixture-to-direct",
            "version": "1",
            "config": converter_config,
            "config_sha256": converter_config_sha256(converter_config),
        },
        "synthetic": True,
    }
    profile_ids = [case["case_id"] for case in cases]
    profiles = [
        {
            "name": "full",
            "strategy": "all-fixed-ids",
            "count": len(profile_ids),
            "case_ids": profile_ids,
            "case_ids_sha256": case_ids_sha256(profile_ids),
            "dimensions": ["category"],
            "seed": None,
        }
    ]
    record = {
        "name": "contract-v2-fixture",
        "version": "1",
        "contract_version": CONTRACT_VERSION,
        "eval": {
            "suite": SUITE,
            "id": DATASET_ID,
            "version": DATASET_VERSION,
            "selected_count": len(cases),
            "selection": "all-rows-in-file-order",
            "scorer": default_scorer,
            "prompt_version": "contract-v2-test",
            "max_output_tokens": 256,
            "max_retries": 0,
        },
        "provenance": provenance,
        "cases": cases,
        "cases_sha256": canonical_sha256(cases),
        "profiles": profiles,
        "profiles_sha256": canonical_sha256(profiles),
    }
    record["dataset_fingerprint"] = dataset_fingerprint_v2(record)
    return record


def test_v2_dataset_roundtrip_and_canonical_identity():
    record = _dataset()
    validate_dataset(record)
    parsed = DirectLlmDatasetV2.model_validate(record)
    assert parsed.contract_version == parsed.eval.version == 2
    assert parsed.cases[1].input == "Question 2"
    assert parsed.cases[1].metadata.scorer.id == "contains"
    assert parsed.profiles[0].case_ids == ["case-1", "case-2"]
    normalized = parsed.model_dump(mode="json")
    assert normalized == record
    assert parsed.dataset_fingerprint == dataset_fingerprint(normalized)
    assert DirectLlmDatasetV2.model_validate_json(parsed.model_dump_json()) == parsed


def test_dataset_fingerprint_ignores_storage_address_but_covers_semantics():
    record = _dataset()
    renamed = deepcopy(record)
    renamed["name"] = "same-content-other-name"
    renamed["version"] = "99"
    assert dataset_fingerprint_v2(renamed) == record["dataset_fingerprint"]
    validate_dataset(renamed)

    changed = deepcopy(record)
    changed["provenance"]["license"]["status"] = "restricted"
    assert dataset_fingerprint_v2(changed) != record["dataset_fingerprint"]
    with pytest.raises(ValueError, match="dataset_fingerprint mismatch"):
        validate_dataset(changed)
    with pytest.raises(ValueError, match="Extra inputs"):
        validate_dataset({**record, "untracked": True})


def test_case_input_alias_and_typed_metadata_are_strict():
    scorer = _scorer()
    prompt = _case("alias", 3, prompt=True, scorer=scorer)
    assert prompt["input"] == "Question 3" and "prompt" not in prompt
    assert prompt["metadata"]["scorer"] == scorer and "scorer" not in prompt

    both = deepcopy(prompt)
    both["prompt"] = "duplicate"
    with pytest.raises(ValidationError, match="exactly one"):
        DirectLlmCaseV2.model_validate(both)
    unknown = deepcopy(prompt)
    unknown["metadata"]["rationale"] = "must not enter the frozen contract"
    with pytest.raises(ValidationError, match="Extra inputs"):
        DirectLlmCaseV2.model_validate(unknown)
    extra = {**prompt, "arbitrary": True}
    with pytest.raises(ValidationError, match="Extra inputs"):
        DirectLlmCaseV2.model_validate(extra)
    bad_expected = {**prompt, "expected": {"choice": "A"}}
    with pytest.raises(ValidationError):
        DirectLlmCaseV2.model_validate(bad_expected)


def test_scorer_spec_and_provenance_validate_canonical_hashes_and_required_fields():
    config = {"labels": ["A", "B"], "marker": "[ANSWER:{value}]"}
    spec = ScorerSpec(
        id="choice", version="1", config=config, config_sha256=scorer_config_sha256(config)
    )
    assert spec.config_sha256 == canonical_sha256(config)
    with pytest.raises(ValidationError, match="config_sha256 mismatch"):
        ScorerSpec(id="choice", version="1", config=config, config_sha256="sha256:" + "0" * 64)

    provenance = deepcopy(_dataset()["provenance"])
    assert ProvenanceV2.model_validate(provenance).source_id == "contract-fixture"
    alias = deepcopy(provenance)
    alias["source"] = alias.pop("source_id")
    alias["revision"] = alias.pop("upstream_revision")
    normalized = ProvenanceV2.model_validate(alias)
    assert (
        normalized.source_id == "contract-fixture" and normalized.upstream_revision == "commit-123"
    )
    unknown_status = deepcopy(provenance)
    unknown_status["license"]["status"] = "unknown"
    with pytest.raises(ValidationError, match="license.status"):
        ProvenanceV2.model_validate(unknown_status)
    missing = deepcopy(provenance)
    del missing["converter"]
    with pytest.raises(ValidationError, match="converter"):
        ProvenanceV2.model_validate(missing)
    tampered = deepcopy(provenance)
    tampered["artifacts"][0]["bytes"] += 1
    with pytest.raises(ValidationError, match="artifact_manifest_sha256 mismatch"):
        ProvenanceV2.model_validate(tampered)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda record: record["cases"][0].update(case_id="case-2"), "duplicate case_id"),
        (
            lambda record: record["profiles"][0]["case_ids"].append("missing"),
            "profile count must match case_ids",
        ),
        (lambda record: record["eval"].update(selected_count=1), "selected_count"),
        (lambda record: record.update(cases_sha256="sha256:" + "0" * 64), "cases_sha256"),
        (lambda record: record.update(profiles_sha256="sha256:" + "0" * 64), "profiles_sha256"),
        (
            lambda record: record.update(dataset_fingerprint="sha256:" + "0" * 64),
            "dataset_fingerprint mismatch",
        ),
    ],
)
def test_dataset_validation_rejects_tampered_ids_counts_and_hashes(mutation, message):
    record = _dataset()
    mutation(record)
    with pytest.raises(ValueError, match=message):
        validate_dataset(record)


def test_profile_references_only_known_fixed_case_ids():
    record = _dataset()
    profile = record["profiles"][0]
    profile["case_ids"] = ["case-1", "missing"]
    profile["case_ids_sha256"] = case_ids_sha256(profile["case_ids"])
    record["profiles_sha256"] = canonical_sha256(record["profiles"])
    with pytest.raises(ValueError, match="unknown case_id"):
        validate_dataset(record)


def test_scenario_v2_requires_explicit_plugin_version_and_dispatches():
    record = _dataset()
    scenario = scenario_for_v2(record, version="7")
    assert scenario == {
        "name": "contract-v2-fixture",
        "version": "7",
        "mode": "direct-llm",
        "plugin_version": PLUGIN_VERSION,
        "eval": record["eval"],
        "dataset": "contract-v2-fixture@1",
    }
    validate_scenario(scenario)
    assert suites.suite_of(record) == suites.suite_of(scenario) == SUITE
    suites.validate_dataset(record)
    suites.validate_scenario(scenario)
    assert suites.validated_dataset_identity(record) == (SUITE, "2")
    assert suites.validated_scenario_identity(scenario) == (SUITE, "2")
    for broken in (
        {key: value for key, value in scenario.items() if key != "plugin_version"},
        {**scenario, "plugin_version": "1"},
        {**scenario, "dataset": "missing-version"},
    ):
        with pytest.raises(ValueError, match="direct-llm v2 scenario"):
            validate_scenario(broken)


def test_v2_top_eval_and_plugin_markers_cannot_conflict_or_downgrade():
    record = _dataset()
    for marker in (1, 3, "2", True):
        changed = deepcopy(record)
        changed["contract_version"] = marker
        with pytest.raises(ValueError, match="direct-llm v2 dataset"):
            validate_dataset(changed)
        with pytest.raises(ValueError):
            suites.validate_dataset(changed)

    scenario = scenario_for_v2(record, version="1")
    changed = deepcopy(scenario)
    changed["eval"]["version"] = 1
    assert suites.suite_of(changed) == SUITE
    with pytest.raises(ValueError, match="plugin_version"):
        suites.validate_scenario(changed)
    for marker in ("1", "3", 2, True):
        changed = {**scenario, "plugin_version": marker}
        with pytest.raises(ValueError):
            suites.validate_scenario(changed)


def test_suite_dispatch_keeps_v1_and_gsm8k_and_fails_closed_for_unknown_direct_version():
    v1 = import_direct_llm_jsonl(
        b'{"input":"v1","expected":"ok"}\n',
        name="v1-fixture",
        version="1",
        license_id="test",
        source="test",
        synthetic=True,
    )
    v1_scenario = scenario_for_v1(v1, version="1")
    suites.validate_dataset(v1)
    suites.validate_scenario(v1_scenario)
    assert "plugin_version" not in v1_scenario

    from motte_contracts.gsm8k import import_official_jsonl

    gsm8k = import_official_jsonl(
        b'{"question":"1+1?","answer":"work\\n#### 2"}\n',
        name="gsm-fixture",
        version="1",
        revision="synthetic",
        license_id="test",
        scope="full",
        source="test",
        synthetic=True,
    )
    assert suites.suite_of(gsm8k) == "gsm8k"
    suites.validate_dataset(gsm8k)

    unknown = deepcopy(_dataset())
    unknown["eval"]["version"] = 3
    assert suites.suite_of(unknown) == SUITE and suites.is_managed(unknown)
    with pytest.raises(ValueError, match="unsupported direct-llm dataset contract version: 3"):
        suites.validate_dataset(unknown)
    double_unknown = deepcopy(_dataset())
    double_unknown["eval"].update(id="unknown-direct-prompts", version=3)
    assert suites.suite_of(double_unknown) == SUITE and suites.is_managed(double_unknown)
    with pytest.raises(ValueError, match="unsupported direct-llm dataset contract version: 3"):
        suites.validate_dataset(double_unknown)
    with pytest.raises(ValueError, match="unsupported direct-llm dataset contract version: 3"):
        InMemoryResourceStore().datasets.put(double_unknown)

    unknown_scenario = scenario_for_v2(_dataset(), version="1")
    unknown_scenario["eval"]["version"] = 3
    assert suites.suite_of(unknown_scenario) == SUITE
    with pytest.raises(ValueError, match="unsupported direct-llm scenario contract version: 3"):
        suites.validate_scenario(unknown_scenario)

    malformed_v2 = deepcopy(_dataset())
    malformed_v2["eval"]["id"] = "unknown-direct-prompts"
    assert suites.suite_of(malformed_v2) == SUITE
    with pytest.raises(ValueError, match="invalid direct-llm v2 dataset"):
        suites.validate_dataset(malformed_v2)

    incomplete_legacy_marker = {"name": "ordinary", "version": "1", "eval": {"suite": SUITE}}
    assert suites.suite_of(incomplete_legacy_marker) is None
