"""Direct LLM v2 SDK: normalized publication, selected snapshots, and offline replay."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

import motte_sdk.direct_llm_v2 as direct_v2_module
from motte_contracts.direct_llm import import_direct_llm_jsonl, scenario_for as scenario_for_v1
from motte_contracts.direct_llm_v2 import scenario_for_v2, scorer_config_sha256
from motte_contracts.identity import canonical_sha256
from motte_sdk.benchmark_plugins import (
    aggregate_with_plugin,
    plugin_for_scenario,
    prepare_with_plugin,
    score_with_plugin,
    suite_for_run,
)
from motte_sdk.direct_llm_v2 import (
    SNAPSHOT_CONTENT_HASH_KEY,
    DirectLlmV2SnapshotIntegrityError,
    aggregate_direct_llm_v2,
    direct_llm_v2_scores,
    import_direct_llm_v2_dataset,
    normalize_direct_llm_v2_dataset,
    persist_direct_llm_v2_dataset,
    resolve_direct_llm_v2_manifest,
    selected_cases_from_snapshot,
)
from motte_sdk.publication import publication_audit
from motte_sdk.resolve import prepare_run
from motte_storage.resource_store import InMemoryResourceStore, ResourceConflictError


def _dataset_input(name="direct-v2", count=6, *, tail_marker=""):
    ids = [f"shared-{index:05d}" for index in range(count)]
    cases = []
    for index, case_id in enumerate(ids):
        prompt = f"Question {index}"
        if index == count - 1 and tail_marker:
            prompt += f" {tail_marker}"
        cases.append(
            {
                "case_id": case_id,
                "input": prompt,
                "expected": str(index),
                "metadata": {
                    "source_line": index + 1,
                    "source_id": f"source-{index}",
                    "language": "en",
                    "subject": "synthetic",
                    "category": "test",
                    "difficulty": None,
                    "split": "test",
                    "tags": ["sdk-v2"],
                    "template_family": "sdk-v2-template",
                },
            }
        )
    smoke_ids = ids[: min(50, count)]
    return {
        "name": name,
        "version": "1",
        "contract_version": 2,
        "eval": {
            "suite": "direct-llm",
            "id": "direct-llm-prompts",
            "version": 2,
            "selected_count": count,
            "selection": "all-rows-in-file-order",
            "scorer": {"id": "numeric", "version": "1", "config": {}},
            "prompt_version": "sdk-v2-template",
            "max_output_tokens": 128,
            "max_retries": 0,
        },
        "provenance": {
            "source_id": f"source:{name}",
            "source_kind": "synthetic-test",
            "homepage": None,
            "upstream_revision": "fixture-v1",
            "artifacts": [
                {
                    "logical_name": "fixture-jsonl",
                    "url": f"https://example.test/{name}.jsonl",
                    "sha256": "a" * 64,
                    "bytes": max(1, count * 100),
                }
            ],
            "license": {
                "id": "test-only",
                "status": "approved-test-only",
                "evidence_urls": [],
                "commercial_use": "test-only",
                "redistribution": "test-only",
                "reviewed_at": None,
            },
            "converter": {
                "id": "sdk-v2-fixture",
                "version": "1",
                "config": {"prompt_version": "sdk-v2-template"},
            },
            "synthetic": True,
        },
        "cases": cases,
        "profiles": [
            {
                "name": "smoke",
                "strategy": "first-fixed-ids",
                "case_ids": smoke_ids,
                "dimensions": ["category"],
                "seed": None,
            },
            {
                "name": "full",
                "strategy": "all-fixed-ids",
                "case_ids": ids,
                "dimensions": [],
                "seed": None,
            },
        ],
    }


def _stored(count=6, *, name="direct-v2"):
    resources = InMemoryResourceStore()
    source = _dataset_input(name, count)
    normalized = normalize_direct_llm_v2_dataset(source)
    scenario = scenario_for_v2(normalized, version="1")
    publication = publication_audit(
        normalized,
        scenario,
        {"dataset_fingerprint": normalized["dataset_fingerprint"], "source": "sdk-test"},
        actor="pytest",
        entrypoint="sdk-test",
        published_at="2026-09-19T00:00:00Z",
    )
    persist_direct_llm_v2_dataset(
        source,
        resources,
        version="1",
        publication=publication,
    )
    return resources, resources.scenarios.get(name, "1"), resources.datasets.get(name, "1")


def _published_source_dataset(source_id: str, *, source_kind: str = "managed-public"):
    resources = InMemoryResourceStore()
    source = _dataset_input(name=f"governed-{source_id}")
    source["provenance"]["source_id"] = source_id
    source["provenance"]["source_kind"] = source_kind
    source["provenance"]["synthetic"] = source_kind == "synthetic-test"
    source["provenance"]["license"]["status"] = "approved"
    dataset = normalize_direct_llm_v2_dataset(source)
    scenario = scenario_for_v2(dataset, version="1")
    audit = publication_audit(
        dataset, scenario, {"fixture": "governance", "dataset_fingerprint": dataset["dataset_fingerprint"]},
        actor="pytest", entrypoint="sdk-test", published_at="2026-09-19T00:00:00Z",
    )
    persist_direct_llm_v2_dataset(source, resources, version="1", publication=audit)
    return resources, resources.scenarios.get(dataset["name"], "1")


def _run(manifest):
    return {
        "id": "run-v2",
        "scenario_version": "direct-v2@1",
        "status": "completed",
        "manifest": manifest,
        "case_ids": list(manifest["cases"]),
    }


def test_normalize_v2_dataset_fills_all_hashes_and_is_stable():
    source = _dataset_input()
    first = normalize_direct_llm_v2_dataset(source)
    second = normalize_direct_llm_v2_dataset(deepcopy(first))
    assert first == second
    assert first["dataset_fingerprint"].startswith("sha256:")
    assert first["cases_sha256"].startswith("sha256:")
    assert first["profiles_sha256"].startswith("sha256:")
    normalized_config = first["eval"]["scorer"]["config"]
    assert normalized_config["mode"] == "exact" and normalized_config["allow_sign"] is True
    assert first["eval"]["scorer"]["config_sha256"] == scorer_config_sha256(normalized_config)
    assert first["provenance"]["converter"]["config_sha256"].startswith("sha256:")
    assert first["provenance"]["artifact_manifest_sha256"].startswith("sha256:")
    assert all(profile["case_ids_sha256"].startswith("sha256:") for profile in first["profiles"])
    changed = normalize_direct_llm_v2_dataset(_dataset_input(tail_marker="changed"))
    assert changed["dataset_fingerprint"] != first["dataset_fingerprint"]
    forged = deepcopy(source)
    forged["cases_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="cases_sha256 mismatch"):
        normalize_direct_llm_v2_dataset(forged)
    invalid_expected = deepcopy(source)
    invalid_expected["cases"][0]["expected"] = "not-a-number"
    with pytest.raises(ValueError, match="invalid numeric expected"):
        normalize_direct_llm_v2_dataset(invalid_expected)


def test_persist_rejects_pending_and_restricted_provenance_without_side_effects():
    for status in ("pending", "restricted"):
        resources = InMemoryResourceStore()
        source = _dataset_input(name=f"blocked-{status}")
        source["provenance"]["license"]["status"] = status
        with pytest.raises(ValueError, match="SOURCE_LICENSE_BLOCKED"):
            persist_direct_llm_v2_dataset(source, resources)
        assert resources.datasets.list() == []
        assert resources.scenarios.list() == []
        assert resources.publications.list() == []


def test_run_gate_rejects_unknown_approved_source_even_with_valid_audit(monkeypatch):
    import motte_sdk.dataset_sources as source_registry

    resources, scenario = _published_source_dataset("unknown-public-source")
    monkeypatch.setattr(source_registry, "load_registry", lambda: {})
    with pytest.raises(ValueError, match="SOURCE_NOT_FOUND"):
        resolve_direct_llm_v2_manifest(scenario, {"profile": "smoke"}, resources)


@pytest.mark.parametrize(
    ("source_id", "error_code"),
    [("ifeval", "SOURCE_LICENSE_BLOCKED"), ("ceval", "SOURCE_APPROVAL_REQUIRED")],
)
def test_run_gate_uses_current_registered_source_status(source_id, error_code, monkeypatch):
    import motte_sdk.dataset_sources as source_registry

    current = source_registry.load_registry()[source_id]
    resources, scenario = _published_source_dataset(source_id)
    monkeypatch.setattr(source_registry, "load_registry", lambda: {source_id: current})
    with pytest.raises(ValueError, match=error_code):
        resolve_direct_llm_v2_manifest(scenario, {"profile": "smoke"}, resources)


def test_approved_internal_published_fixture_runs_until_current_blocker(monkeypatch):
    import motte_sdk.dataset_sources as source_registry
    from motte_sdk.core_zh import build_dataset

    current = source_registry.load_registry()["motte-core-zh"]
    approved_governance = current.governance.model_copy(update={
        "status": "approved-internal",
        "distribution_scope": "internal-only",
        "reviewer": "pytest",
        "reviewed_at": "2026-09-19",
        "decision_notes": "approved internal fixture",
    })
    approved_source = current.model_copy(update={"governance": approved_governance, "blockers": []})
    dataset = deepcopy(build_dataset())
    dataset["provenance"]["license"]["status"] = "approved-internal"
    converter = dataset["provenance"]["converter"]
    converter["config"]["governance_status"] = "approved-internal"
    converter.pop("config_sha256", None)
    dataset.pop("dataset_fingerprint", None)
    dataset = normalize_direct_llm_v2_dataset(dataset)
    scenario = scenario_for_v2(dataset, version="1")
    audit = publication_audit(
        dataset, scenario, {"fixture": "approved-internal"},
        actor="pytest", entrypoint="sdk-test", published_at="2026-09-19T00:00:00Z",
    )
    resources = InMemoryResourceStore()
    persist_direct_llm_v2_dataset(dataset, resources, version="1", publication=audit)
    monkeypatch.setattr(source_registry, "load_registry", lambda: {current.id: approved_source})

    resolved = resolve_direct_llm_v2_manifest(scenario, {"profile": "smoke"}, resources)
    assert resolved["benchmark_provenance"]["plugin_version"] == "2"

    blocked_source = approved_source.model_copy(update={"blockers": ["new review required"]})
    monkeypatch.setattr(source_registry, "load_registry", lambda: {current.id: blocked_source})
    with pytest.raises(ValueError, match="SOURCE_NOT_READY"):
        resolve_direct_llm_v2_manifest(scenario, {"profile": "smoke"}, resources)


def test_import_atomically_publishes_dataset_scenario_and_optional_audit():
    resources = InMemoryResourceStore()
    source = _dataset_input()
    normalized = normalize_direct_llm_v2_dataset(source)
    target_scenario = scenario_for_v2(normalized, version="1")
    publication = publication_audit(
        normalized,
        target_scenario,
        {"dataset_fingerprint": normalized["dataset_fingerprint"], "source": "pytest"},
        actor="pytest",
        entrypoint="sdk-test",
        published_at="2026-09-19T00:00:00Z",
    )
    receipt = import_direct_llm_v2_dataset(
        source,
        resources=resources,
        publication=publication,
    )
    assert receipt["imported"] == receipt["scenario"] == "direct-v2@1"
    assert receipt["plugin_version"] == "2" and receipt["cases"] == 6
    stored = resources.datasets.get("direct-v2", "1")
    scenario = resources.scenarios.get("direct-v2", "1")
    audit = resources.publications.get(publication["id"])
    assert stored["dataset_fingerprint"] == receipt["dataset_fingerprint"]
    assert scenario["plugin_version"] == "2" and scenario["dataset"] == "direct-v2@1"
    assert audit["dataset"] == audit["scenario"] == "direct-v2@1"
    assert audit["dataset_fingerprint"] == receipt["dataset_fingerprint"]
    assert (
        import_direct_llm_v2_dataset(
            _dataset_input(),
            resources=resources,
            publication=publication,
        )
        == receipt
    )


def test_persist_rejects_noncanonical_publication_instead_of_repairing_it():
    resources = InMemoryResourceStore()
    normalized = normalize_direct_llm_v2_dataset(_dataset_input())
    scenario = scenario_for_v2(normalized, version="1")
    audit = publication_audit(
        normalized, scenario, {"fixture": "invalid-audit"},
        actor="pytest", entrypoint="sdk-test", published_at="2026-09-19T00:00:00Z",
    )
    with pytest.raises(ValueError, match="publication audit"):
        persist_direct_llm_v2_dataset(
            normalized, resources, publication={**audit, "id": "publication-" + "0" * 64},
        )
    assert resources.datasets.list() == []
    assert resources.scenarios.list() == []
    assert resources.publications.list() == []


def test_auto_version_conflict_retries_without_an_orphan(monkeypatch):
    resources = InMemoryResourceStore()
    first = persist_direct_llm_v2_dataset(_dataset_input(), resources)
    assert first["imported"] == "direct-v2@1"
    changed = _dataset_input(tail_marker="new-version")
    original_next = direct_v2_module.next_dataset_version
    calls = {"count": 0}

    def stale_once(record, store):
        calls["count"] += 1
        return "1" if calls["count"] == 1 else original_next(record, store)

    monkeypatch.setattr(direct_v2_module, "next_dataset_version", stale_once)
    normalized = normalize_direct_llm_v2_dataset(changed)
    initial_scenario = scenario_for_v2(normalized, version="1")
    initial_audit = publication_audit(
        normalized, initial_scenario, {"fixture": "auto-version"},
        actor="pytest", entrypoint="sdk-test", published_at="2026-09-19T00:00:00Z",
    )
    second = persist_direct_llm_v2_dataset(changed, resources, publication=initial_audit)
    assert second["imported"] == second["scenario"] == "direct-v2@2"
    assert resources.datasets.get("direct-v2", "2") is not None
    assert resources.scenarios.get("direct-v2", "2") is not None
    assert resources.publications.get(initial_audit["id"]) is None
    [retargeted] = resources.publications.list()
    assert retargeted["id"] != initial_audit["id"]
    assert retargeted["dataset"] == retargeted["scenario"] == "direct-v2@2"
    assert retargeted["receipt_sha256"] == initial_audit["receipt_sha256"]
    assert len(resources.datasets.list()) == len(resources.scenarios.list()) == 2


def test_explicit_version_conflict_rolls_back_the_pair():
    resources = InMemoryResourceStore()
    resources.scenarios.put({"name": "direct-v2", "version": "7", "ordinary": True})
    with pytest.raises(ResourceConflictError):
        persist_direct_llm_v2_dataset(_dataset_input(), resources, version="7")
    assert resources.datasets.get("direct-v2", "7") is None


def test_atomic_bundle_rejects_wrong_dataset_binding_and_eval_contract():
    resources = InMemoryResourceStore()
    dataset = normalize_direct_llm_v2_dataset(_dataset_input())
    scenario = scenario_for_v2(dataset, version="1")

    wrong_binding = {**scenario, "dataset": "other@1"}
    with pytest.raises(ValueError, match="dataset reference"):
        resources.publish_dataset_scenario(dataset, wrong_binding)

    changed_source = _dataset_input()
    changed_source["eval"]["max_output_tokens"] = 64
    changed_dataset = normalize_direct_llm_v2_dataset(changed_source)
    mismatched_eval = scenario_for_v2(changed_dataset, version="1")
    with pytest.raises(ValueError, match="eval contracts"):
        resources.publish_dataset_scenario(dataset, mismatched_eval)

    wrong_plugin = {**scenario, "plugin_version": "1"}
    with pytest.raises(ValueError, match="invalid direct-llm v2 scenario"):
        resources.publish_dataset_scenario(dataset, wrong_plugin)

    assert resources.datasets.list() == []
    assert resources.scenarios.list() == []


def test_resolve_all_ids_random_and_profile_are_deterministic_and_exclusive():
    resources, scenario, dataset = _stored(8)
    all_manifest = resolve_direct_llm_v2_manifest(scenario, {}, resources)
    assert list(all_manifest["cases"]) == [case["case_id"] for case in dataset["cases"]]

    ids_manifest = resolve_direct_llm_v2_manifest(
        scenario,
        {
            "case_selection": {"mode": "ids", "case_ids": ["shared-00005", "shared-00001"]},
        },
        resources,
    )
    assert list(ids_manifest["cases"]) == ["shared-00001", "shared-00005"]

    random_request = {"case_selection": {"mode": "random", "count": 3, "seed": "deadbeef"}}
    first_random = resolve_direct_llm_v2_manifest(scenario, random_request, resources)
    second_random = resolve_direct_llm_v2_manifest(scenario, random_request, resources)
    assert list(first_random["cases"]) == list(second_random["cases"])
    assert (
        first_random["benchmark_snapshot"]["selection"]
        == second_random["benchmark_snapshot"]["selection"]
    )

    profile_manifest = resolve_direct_llm_v2_manifest(scenario, {"profile": "smoke"}, resources)
    assert list(profile_manifest["cases"]) == dataset["profiles"][0]["case_ids"]
    mode_profile = resolve_direct_llm_v2_manifest(
        scenario,
        {
            "case_selection": {"mode": "profile", "profile": "smoke"},
        },
        resources,
    )
    assert (
        mode_profile["benchmark_snapshot"]["selection"]
        == profile_manifest["benchmark_snapshot"]["selection"]
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        resolve_direct_llm_v2_manifest(
            scenario,
            {
                "profile": "smoke",
                "case_selection": {"mode": "all"},
            },
            resources,
        )
    with pytest.raises(ValueError, match="unknown direct-llm v2 profile"):
        resolve_direct_llm_v2_manifest(scenario, {"profile": "missing"}, resources)


def test_snapshot_is_selected_only_and_provider_projection_has_no_answers_or_provenance():
    resources, scenario, dataset = _stored(8)
    manifest = resolve_direct_llm_v2_manifest(
        scenario,
        {
            "case_selection": {"mode": "ids", "case_ids": ["shared-00000", "shared-00003"]},
        },
        resources,
    )
    snapshot = manifest["benchmark_snapshot"]
    assert set(snapshot) == {
        "schema_version",
        "scenario",
        "dataset",
        "selection",
        "selected_cases",
        SNAPSHOT_CONTENT_HASH_KEY,
    }
    assert snapshot[SNAPSHOT_CONTENT_HASH_KEY] == canonical_sha256({
        key: value for key, value in snapshot.items() if key != SNAPSHOT_CONTENT_HASH_KEY
    })
    assert set(snapshot["dataset"]) == {
        "ref",
        "name",
        "version",
        "contract_version",
        "fingerprint",
        "cases_sha256",
        "total_cases",
        "eval",
        "provenance",
    }
    assert "profiles" not in snapshot["dataset"] and "cases" not in snapshot["dataset"]
    assert [case["case_id"] for case in snapshot["selected_cases"]] == [
        "shared-00000",
        "shared-00003",
    ]
    assert snapshot["dataset"]["total_cases"] == len(dataset["cases"])
    assert all(set(projected) == {"case_id", "prompt"} for projected in manifest["cases"].values())
    serialized_provider_cases = json.dumps(manifest["cases"], sort_keys=True)
    assert "expected" not in serialized_provider_cases
    assert "scorer" not in serialized_provider_cases
    assert "provenance" not in serialized_provider_cases


class _ReadOnlyDatasetRepository:
    def __init__(self, datasets):
        self._datasets = datasets

    def get(self, name, version):
        return self._datasets[(name, version)]


class _ReadOnlyPublicationRepository:
    def __init__(self, publications):
        self._publications = publications

    def list(self):
        return deepcopy(self._publications)


class _ReadOnlyResources:
    def __init__(self, datasets, publications):
        self.datasets = _ReadOnlyDatasetRepository(datasets)
        self.publications = _ReadOnlyPublicationRepository(publications)


def test_two_12k_datasets_with_same_50_selected_cases_never_snapshot_unselected_rows():
    left = normalize_direct_llm_v2_dataset(
        _dataset_input("large-left", 12_000, tail_marker="UNSELECTED-LEFT")
    )
    right = normalize_direct_llm_v2_dataset(
        _dataset_input("large-right", 12_000, tail_marker="UNSELECTED-RIGHT")
    )
    publications = [
        publication_audit(
            dataset,
            scenario_for_v2(dataset, version="1"),
            {"dataset_fingerprint": dataset["dataset_fingerprint"], "source": "sdk-test"},
            actor="pytest",
            entrypoint="sdk-test",
            published_at="2026-09-19T00:00:00Z",
        )
        for dataset in (left, right)
    ]
    resources = _ReadOnlyResources(
        {
            ("large-left", "1"): left,
            ("large-right", "1"): right,
        },
        publications,
    )
    left_manifest = resolve_direct_llm_v2_manifest(
        scenario_for_v2(left, version="1"),
        {"profile": "smoke"},
        resources,
    )
    right_manifest = resolve_direct_llm_v2_manifest(
        scenario_for_v2(right, version="1"),
        {"profile": "smoke"},
        resources,
    )
    assert len(left_manifest["benchmark_snapshot"]["selected_cases"]) == 50
    assert len(right_manifest["benchmark_snapshot"]["selected_cases"]) == 50
    assert (
        left_manifest["benchmark_snapshot"]["selected_cases"]
        == right_manifest["benchmark_snapshot"]["selected_cases"]
    )
    assert left["dataset_fingerprint"] != right["dataset_fingerprint"]
    left_payload = json.dumps(left_manifest, ensure_ascii=False)
    right_payload = json.dumps(right_manifest, ensure_ascii=False)
    assert "UNSELECTED-LEFT" not in left_payload
    assert "UNSELECTED-RIGHT" not in right_payload
    assert "shared-11999" not in left_payload and "shared-11999" not in right_payload


def test_score_adapter_passes_the_full_normalized_scorer_contract_to_registry():
    resources, scenario, _ = _stored(3)
    manifest = resolve_direct_llm_v2_manifest(scenario, {"profile": "full"}, resources)
    run = _run(manifest)
    results = [
        {"case_id": case_id, "result": {"content": f"[ANSWER:{index}]"}}
        for index, case_id in enumerate(run["case_ids"])
    ]
    scores = direct_llm_v2_scores(run, results)
    assert [score["outcome"] for score in scores] == ["correct"] * 3
    assert all(score["scorer"] == "numeric" and score["scorer_version"] == "1" for score in scores)
    expected_hash = manifest["benchmark_snapshot"]["dataset"]["eval"]["scorer"]["config_sha256"]
    assert all(score["details"]["config_sha256"] == expected_hash for score in scores)


def test_retry_and_rescore_use_only_the_persisted_snapshot(monkeypatch):
    resources, scenario, _ = _stored(4)
    manifest = resolve_direct_llm_v2_manifest(scenario, {"profile": "full"}, resources)
    run = _run(manifest)
    results = [
        {"case_id": case_id, "result": {"content": f"answer-{index}"}}
        for index, case_id in enumerate(run["case_ids"])
    ]
    calls = []

    def fake_score(result, expected, spec):
        calls.append((result, expected, deepcopy(spec)))
        assert isinstance(spec["version"], str)
        assert set(spec) == {"id", "version", "config", "config_sha256"}
        return {
            "outcome": "correct",
            "passed": True,
            "attempted": True,
            "responded": True,
            "judged": True,
            "scorer": spec["id"],
            "scorer_version": spec["version"],
            "details": {"config_sha256": scorer_config_sha256(spec["config"])},
        }

    monkeypatch.setattr("motte_eval.direct_llm_v2.score_answer_case", fake_score)
    original = direct_llm_v2_scores(run, results)
    retry = direct_llm_v2_scores(deepcopy(run), results)
    rescore = score_with_plugin(run, results)
    assert original == retry == rescore
    assert len(calls) == 12
    assert selected_cases_from_snapshot(run) == manifest["benchmark_snapshot"]["selected_cases"]
    summary = aggregate_with_plugin(run, original)
    assert summary["selected"] == summary["judged"] == summary["correct"] == 4
    assert summary["accuracy"] == summary["coverage"] == 1.0

    with pytest.raises(ValueError, match="duplicate result case_id"):
        direct_llm_v2_scores(run, [results[0], results[0]])
    with pytest.raises(ValueError, match="result case_id is not selected"):
        direct_llm_v2_scores(run, [{"case_id": "foreign", "result": {"content": "x"}}])

    tampered = deepcopy(run)
    tampered["manifest"]["benchmark_snapshot"]["selection"]["case_ids"].reverse()
    with pytest.raises(DirectLlmV2SnapshotIntegrityError, match="content hash mismatch"):
        direct_llm_v2_scores(tampered, results)


def test_snapshot_content_identity_rejects_case_selection_scorer_and_projection_tampering():
    resources, scenario, _ = _stored(3)
    manifest = resolve_direct_llm_v2_manifest(scenario, {"profile": "full"}, resources)
    run = _run(manifest)
    snapshot = manifest["benchmark_snapshot"]

    mutations = []

    changed_expected = deepcopy(run)
    changed_expected["manifest"]["benchmark_snapshot"]["selected_cases"][0]["expected"] = "99"
    mutations.append(changed_expected)

    changed_prompt = deepcopy(run)
    changed_prompt["manifest"]["benchmark_snapshot"]["selected_cases"][0]["input"] = "changed"
    mutations.append(changed_prompt)

    changed_scorer = deepcopy(run)
    changed_scorer["manifest"]["benchmark_snapshot"]["selected_cases"][0]["metadata"]["scorer"] = (
        deepcopy(snapshot["dataset"]["eval"]["scorer"])
    )
    mutations.append(changed_scorer)

    changed_selection = deepcopy(run)
    changed_selection["manifest"]["benchmark_snapshot"]["selection"]["seed"] = "deadbeef"
    mutations.append(changed_selection)

    changed_dataset_scorer = deepcopy(run)
    changed_dataset_scorer["manifest"]["benchmark_snapshot"]["dataset"]["eval"]["scorer"]["config"][
        "allow_sign"
    ] = False
    mutations.append(changed_dataset_scorer)

    for tampered in mutations:
        with pytest.raises(DirectLlmV2SnapshotIntegrityError) as caught:
            selected_cases_from_snapshot(tampered)
        assert caught.value.code == "DIRECT_LLM_V2_SNAPSHOT_INTEGRITY"
        assert caught.value.evidence["reason"] == "snapshot content hash mismatch"

    changed_projection = deepcopy(run)
    first_id = changed_projection["case_ids"][0]
    changed_projection["manifest"]["cases"][first_id]["prompt"] = "changed"
    with pytest.raises(DirectLlmV2SnapshotIntegrityError, match="provider case projection"):
        selected_cases_from_snapshot(changed_projection)

    legacy_v2 = deepcopy(run)
    legacy_v2["manifest"]["benchmark_snapshot"].pop(SNAPSHOT_CONTENT_HASH_KEY)
    with pytest.raises(DirectLlmV2SnapshotIntegrityError, match="snapshot is missing"):
        selected_cases_from_snapshot(legacy_v2)


def test_v1_dispatch_is_unchanged_and_unknown_explicit_plugin_versions_fail_closed():
    raw = b'{"input":"legacy","expected":"ok"}\n'
    v1 = import_direct_llm_jsonl(
        raw,
        name="legacy-direct",
        version="1",
        license_id="test",
        source="test",
        synthetic=True,
    )
    v1_scenario = scenario_for_v1(v1, version="1")
    assert plugin_for_scenario(v1_scenario) == ("direct-llm", "1")

    resources, v2_scenario, _ = _stored()
    assert plugin_for_scenario(v2_scenario) == ("direct-llm", "2")
    prepared = prepare_with_plugin(
        "direct-llm", v2_scenario, {"profile": "smoke"}, resources, contract_version="2"
    )
    assert prepared["benchmark_provenance"]["plugin_version"] == "2"
    assert prepared["evaluation"]["adapter_version"] == "2"

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
            "model": "probe-v2",
            "capabilities": {},
            "max_output_tokens": 1024,
        }
    )
    resolved, case_ids = prepare_run(
        "direct-v2@1", {"model": "probe", "profile": "smoke"}, [], resources
    )
    assert case_ids == list(resolved["cases"])
    assert resolved["evaluation"]["adapter_version"] == "2"
    assert "profile" not in resolved

    missing_plugin = {key: value for key, value in v2_scenario.items() if key != "plugin_version"}
    with pytest.raises(ValueError, match="direct-llm v2 scenario"):
        plugin_for_scenario(missing_plugin)
    with pytest.raises(ValueError, match="plugin_version"):
        plugin_for_scenario({**v1_scenario, "plugin_version": "99"})
    with pytest.raises(ValueError, match="unsupported benchmark plugin"):
        suite_for_run(
            {
                "manifest": {
                    "benchmark_provenance": {
                        "suite": "direct-llm",
                        "plugin_version": "99",
                    }
                }
            }
        )


def test_explicit_v2_marker_on_v1_never_dispatches_the_v1_plugin(monkeypatch):
    import motte_sdk.benchmark_plugins as plugins

    v1 = import_direct_llm_jsonl(
        b'{"input":"legacy","expected":"ok"}\n',
        name="legacy-marker",
        version="1",
        license_id="test",
        source="test",
        synthetic=True,
    )
    scenario = scenario_for_v1(v1, version="1")
    dispatched = []

    def unexpected_dispatch(suite_id, contract_version="1"):
        dispatched.append((suite_id, contract_version))
        raise AssertionError("conflicting markers must fail before plugin lookup")

    monkeypatch.setattr(plugins, "benchmark_plugin", unexpected_dispatch)
    with pytest.raises(ValueError, match="contract_version"):
        plugins.plugin_for_scenario({**scenario, "contract_version": 2})
    with pytest.raises(ValueError, match="plugin_version"):
        plugins.plugin_for_scenario({**scenario, "plugin_version": "2"})

    double_unknown = deepcopy(scenario)
    double_unknown["eval"].update(id="unknown-direct-prompts", version=3)
    double_unknown["suite"] = "direct-llm"
    with pytest.raises(ValueError, match="unsupported direct-llm scenario contract version"):
        plugins.plugin_for_scenario(double_unknown)
    with pytest.raises(ValueError, match="invalid managed benchmark scenario"):
        plugins.plugin_for_scenario({"suite": "direct-llm"})
    for marker in ("", 0, False, None):
        with pytest.raises(ValueError, match="explicit plugin_version"):
            plugins.plugin_for_scenario({"suite": "custom-test", "plugin_version": marker})
    assert dispatched == []


def test_aggregate_v2_uses_judged_for_accuracy_and_selected_for_coverage():
    resources, scenario, _ = _stored(3)
    run = _run(resolve_direct_llm_v2_manifest(scenario, {}, resources))
    scores = [
        {
            "case_id": "shared-00000",
            "outcome": "correct",
            "judged": True,
            "attempted": True,
            "responded": True,
        },
        {
            "case_id": "shared-00001",
            "outcome": "call_failed",
            "judged": True,
            "attempted": True,
            "responded": False,
        },
        {
            "case_id": "shared-00002",
            "outcome": "no_expectation",
            "judged": False,
            "attempted": True,
            "responded": True,
        },
    ]
    summary = aggregate_direct_llm_v2(run, scores)
    assert summary["accuracy"] == 1.0
    assert summary["coverage"] == pytest.approx(1 / 3)
    assert summary["completion"] == 1.0
    assert summary["attempt_rate"] == 1.0
