"""Offline Direct LLM v2 first-batch end-to-end smoke coverage."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from motte_sdk.adapters.mmlu_pro import convert_mmlu_pro_zero_shot
from motte_sdk.adapters.truthfulqa import convert_truthfulqa
from motte_sdk.core_zh import build_dataset as build_core_zh_dataset
from motte_contracts.direct_llm_v2 import scenario_for_v2
from motte_sdk.direct_llm_v2 import (
    DirectLlmV2SnapshotIntegrityError,
    normalize_direct_llm_v2_dataset,
    persist_direct_llm_v2_dataset,
)
from motte_sdk.publication import publication_audit
from motte_sdk.replay_run import ReplayProvider
from motte_sdk.resolve import prepare_run
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

PROFILE_TARGETS = {"smoke": 1, "regression": 1, "full": 2}
RUNTIME_PROFILE = "runtime-smoke"


class CountingReplayProvider:
    """Deterministic provider fixture with no transport or scorer substitution."""

    def __init__(self, outputs: dict[str, dict[str, Any]]) -> None:
        self._replay = ReplayProvider({
            case_id: {"output": output} for case_id, output in outputs.items()
        })
        self.calls: list[str] = []

    def invoke(self, case_id: str) -> dict[str, Any]:
        self.calls.append(case_id)
        return self._replay.invoke(case_id)


def _approve_test_copy(record: dict[str, Any]) -> dict[str, Any]:
    approved = deepcopy(record)
    approved["provenance"]["license"]["status"] = "approved-test-only"
    approved["provenance"]["source_id"] = (
        f"runtime-test:{approved['provenance']['source_id']}"
    )
    approved["provenance"]["source_kind"] = "synthetic-test"
    approved["provenance"]["synthetic"] = True
    approved.pop("dataset_fingerprint", None)
    return approved


def _truthfulqa_fixture() -> dict[str, Any]:
    rows = [
        {
            "Question": "Can a person breathe normally underwater without equipment?",
            "Best Answer": "No, people need breathing equipment underwater.",
            "Best Incorrect Answer": "Yes, anyone can breathe underwater unaided.",
            "Category": "Science",
            "Type": "Adversarial",
        },
        {
            "Question": "Does drinking clean water usually relieve thirst?",
            "Best Answer": "Yes, clean water usually relieves thirst.",
            "Best Incorrect Answer": "No, water always increases thirst.",
            "Category": "Health",
            "Type": "Non-adversarial",
        },
    ]
    return convert_truthfulqa(
        rows,
        revision="1" * 40,
        artifact={
            "logical_name": "TruthfulQA.csv",
            "url": "https://example.test/TruthfulQA.csv",
            "sha256": "a" * 64,
            "bytes": 1,
        },
        expected_rows=2,
        profile_targets=PROFILE_TARGETS,
        name="runtime-truthfulqa",
    )


def _mmlu_row(question_id: int, answer_index: int, *, validation: bool = False) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "question": f"Fixture question {question_id}?",
        "options": ["first", "second", "third"],
        "answer": "ABC"[answer_index],
        "answer_index": answer_index,
        "cot_content": "Pinned demonstration reasoning." if validation else "",
        "category": "fixture-category",
        "src": "fixture-source",
    }


def _mmlu_pro_fixture() -> dict[str, Any]:
    dataset_revision = "2" * 40
    runner_revision = "3" * 40
    pinned = {
        "dataset_revision": dataset_revision,
        "runner_revision": runner_revision,
        "artifacts": [
            {
                "logical_name": "test.json",
                "url": f"https://example.test/{dataset_revision}/test.json",
                "sha256": "b" * 64,
                "bytes": 1,
            },
            {
                "logical_name": "validation.json",
                "url": f"https://example.test/{dataset_revision}/validation.json",
                "sha256": "c" * 64,
                "bytes": 1,
            },
            {
                "logical_name": "evaluate.py",
                "url": f"https://example.test/{runner_revision}/evaluate.py",
                "sha256": "d" * 64,
                "bytes": 1,
            },
        ],
    }
    return convert_mmlu_pro_zero_shot(
        [_mmlu_row(1, 0), _mmlu_row(2, 1)],
        [_mmlu_row(100, 2, validation=True)],
        pinned_provenance=pinned,
        expected_test_rows=2,
        expected_validation_rows=1,
        expected_categories=1,
        expected_category_names=["fixture-category"],
        demonstrations_per_category=1,
        profile_targets=PROFILE_TARGETS,
        name="runtime-mmlu-pro-zero",
    )


def _effective_scorer(dataset: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    return case["metadata"].get("scorer") or dataset["eval"]["scorer"]


def _core_zh_fixture() -> tuple[dict[str, Any], list[str]]:
    record = build_core_zh_dataset()
    first_by_scorer: dict[str, dict[str, Any]] = {}
    for case in record["cases"]:
        scorer_id = _effective_scorer(record, case)["id"]
        first_by_scorer.setdefault(scorer_id, case)
    required = {"exact", "numeric", "json_equal", "choice"}
    assert required.issubset(first_by_scorer)

    selected = [
        first_by_scorer["exact"]["case_id"],
        first_by_scorer["numeric"]["case_id"],
        first_by_scorer["json_equal"]["case_id"],
        first_by_scorer["choice"]["case_id"],
    ]
    selected.append(next(
        case["case_id"] for case in record["cases"] if case["case_id"] not in selected
    ))
    record["profiles"].append({
        "name": RUNTIME_PROFILE,
        "strategy": "fixed-runtime-scorer-coverage",
        "count": len(selected),
        "case_ids": selected,
        "dimensions": ["scorer"],
        "seed": None,
    })
    record.pop("profiles_sha256", None)
    return record, selected


def _correct_output(dataset: dict[str, Any], case: dict[str, Any]) -> str:
    scorer_id = _effective_scorer(dataset, case)["id"]
    expected = case["expected"]
    if scorer_id in {"choice", "numeric"}:
        return f"[ANSWER:{expected}]"
    return expected


def _seed_catalog() -> tuple[
    InMemoryResourceStore,
    dict[str, tuple[dict[str, Any], dict[str, Any], str]],
    list[str],
]:
    resources = InMemoryResourceStore()
    resources.providers.put({
        "name": "offline", "kind": "openai_compatible",
        "base_url": "https://offline.invalid/v1",
    })
    resources.models.put({
        "id": "offline-model", "provider": "offline", "model": "fixture-model",
        "capabilities": {}, "max_output_tokens": 2048,
    })

    core_record, core_ids = _core_zh_fixture()
    sources = {
        "truthfulqa": (_truthfulqa_fixture(), "smoke"),
        "mmlu-pro-zero": (_mmlu_pro_fixture(), "smoke"),
        "core-zh": (core_record, RUNTIME_PROFILE),
    }
    catalog: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {}
    for key, (source, profile) in sources.items():
        approved_source = _approve_test_copy(source)
        normalized = normalize_direct_llm_v2_dataset(approved_source)
        scenario = scenario_for_v2(normalized, version="1")
        publication = publication_audit(
            normalized,
            scenario,
            {"dataset_fingerprint": normalized["dataset_fingerprint"], "source": "runtime-test"},
            actor="pytest",
            entrypoint="runtime-test",
            published_at="2026-09-19T00:00:00Z",
        )
        receipt = persist_direct_llm_v2_dataset(
            approved_source,
            resources,
            version="1",
            publication=publication,
        )
        dataset_name, _, dataset_version = receipt["imported"].rpartition("@")
        scenario_name, _, scenario_version = receipt["scenario"].rpartition("@")
        dataset = resources.datasets.get(dataset_name, dataset_version)
        scenario = resources.scenarios.get(scenario_name, scenario_version)
        assert dataset is not None and scenario is not None
        catalog[key] = (dataset, scenario, profile)
    return resources, catalog, core_ids


def _prepare_profile(
    resources: InMemoryResourceStore,
    scenario: dict[str, Any],
    profile: str,
) -> tuple[dict[str, Any], list[str]]:
    scenario_ref = f"{scenario['name']}@{scenario['version']}"
    manifest, case_ids = prepare_run(
        scenario_ref,
        {
            "model": "offline-model",
            "case_selection": {"mode": "profile", "profile": profile},
        },
        [],
        resources,
    )
    assert list(manifest["cases"]) == case_ids
    assert all(set(case) == {"case_id", "prompt"} for case in manifest["cases"].values())
    return manifest, case_ids


def test_first_batch_v2_fake_provider_end_to_end_uses_frozen_snapshots():
    resources, catalog, core_ids = _seed_catalog()
    service = RunService(InMemoryRunStore())
    seen_scorers: set[tuple[str, str]] = set()

    for key in ("truthfulqa", "mmlu-pro-zero"):
        dataset, scenario, profile = catalog[key]
        manifest, case_ids = _prepare_profile(resources, scenario, profile)
        by_id = {case["case_id"]: case for case in dataset["cases"]}
        fake = CountingReplayProvider({
            case_id: {"content": _correct_output(dataset, by_id[case_id])}
            for case_id in case_ids
        })
        run = service.create_run(
            f"{scenario['name']}@{scenario['version']}", manifest, case_ids
        )
        result = service.execute(run["id"], provider=fake.invoke)
        assert result["status"] == "completed"
        assert fake.calls == case_ids
        assert [score["outcome"] for score in result["scores"]] == ["correct"]
        seen_scorers.update(
            (score["scorer"], score["scorer_version"]) for score in result["scores"]
        )

    dataset, scenario, profile = catalog["core-zh"]
    manifest, case_ids = _prepare_profile(resources, scenario, profile)
    assert case_ids == core_ids
    assert manifest["benchmark_snapshot"]["selection"]["count"] == 5
    assert len(manifest["benchmark_snapshot"]["selected_cases"]) == 5
    assert manifest["benchmark_snapshot"]["dataset"]["total_cases"] == 1000
    unselected = next(case["case_id"] for case in dataset["cases"] if case["case_id"] not in case_ids)
    assert unselected not in str(manifest["benchmark_snapshot"])

    by_id = {case["case_id"]: case for case in dataset["cases"]}
    outputs = {
        case_ids[0]: {"content": _correct_output(dataset, by_id[case_ids[0]])},
        case_ids[1]: {"content": "numeric output without the required final marker"},
        case_ids[2]: {"content": _correct_output(dataset, by_id[case_ids[2]])},
        case_ids[3]: {"error": {"class": "auth", "message": "offline synthetic stop"}},
        case_ids[4]: {"content": _correct_output(dataset, by_id[case_ids[4]])},
    }
    fake = CountingReplayProvider(outputs)
    run = service.create_run(
        f"{scenario['name']}@{scenario['version']}", manifest, case_ids
    )
    result = service.execute(run["id"], provider=fake.invoke)
    assert result["status"] == "failed"
    assert fake.calls == case_ids[:4]
    assert [score["outcome"] for score in result["scores"]] == [
        "correct", "invalid_format", "correct", "call_failed", "not_attempted",
    ]
    seen_scorers.update(
        (score["scorer"], score["scorer_version"]) for score in result["scores"]
    )
    assert seen_scorers == {
        ("choice", "1"), ("numeric", "1"), ("json_equal", "1"), ("exact", "1"),
    }

    aggregate = result["scoring_pass"]["summary"]["aggregate"]
    assert aggregate["selected"] == 5
    assert aggregate["judged"] == 3 and aggregate["attempted"] == 4
    assert aggregate["accuracy"] == pytest.approx(2 / 3)
    assert aggregate["coverage"] == pytest.approx(3 / 5)
    assert aggregate["completion"] == aggregate["attempt_rate"] == pytest.approx(4 / 5)
    assert aggregate["format_failure_rate"] == pytest.approx(1 / 3)

    snapshot = deepcopy(result["manifest"]["benchmark_snapshot"])
    call_count = len(fake.calls)
    rescored = service.rescore(result["id"])
    assert rescored["scores"] == result["scores"]
    assert rescored["manifest"]["benchmark_snapshot"] == snapshot
    assert len(fake.calls) == call_count

    child = service.retry(result["id"])
    assert child["parent_run_id"] == result["id"]
    assert child["case_ids"] == result["case_ids"]
    assert child["manifest"]["benchmark_snapshot"] == snapshot
    assert len(fake.calls) == call_count

    tampered = service.store.runs.get(result["id"])
    assert tampered is not None
    tampered["manifest"]["benchmark_snapshot"]["selected_cases"][0]["expected"] = "tampered"
    service.store.runs.save(tampered)
    current_pass = service.store.scoring_passes.current(result["id"])
    with pytest.raises(
        DirectLlmV2SnapshotIntegrityError, match="snapshot content hash mismatch"
    ):
        service.rescore(result["id"])
    assert service.store.scoring_passes.current(result["id"]) == current_pass
    with pytest.raises(DirectLlmV2SnapshotIntegrityError):
        service.retry(result["id"])

    fresh_manifest, fresh_ids = _prepare_profile(resources, scenario, profile)
    fresh = service.create_run(
        f"{scenario['name']}@{scenario['version']}", fresh_manifest, fresh_ids
    )
    tampered_execution = service.store.runs.get(fresh["id"])
    assert tampered_execution is not None
    tampered_execution["manifest"]["benchmark_snapshot"]["selected_cases"][0][
        "input"
    ] = "tampered prompt"
    service.store.runs.save(tampered_execution)
    never_called = CountingReplayProvider({})
    with pytest.raises(DirectLlmV2SnapshotIntegrityError):
        service.execute(fresh["id"], provider=never_called.invoke)
    assert never_called.calls == []
