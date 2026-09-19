"""Direct LLM SDK：内置样例、落库、创建期展开、评分（零网络零费用）。"""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

import motte_sdk.direct_llm as direct_llm_module

from motte_contracts.direct_llm import SCORER_VERSION, import_direct_llm_jsonl, scenario_for
from motte_sdk.direct_llm import (BUILTIN_DATASETS, BUILTIN_ENV, BuiltinUnavailable, builtin_catalog,
                                  builtin_dir, builtin_source, direct_llm_scores,
                                  import_direct_llm_split, persist_direct_llm_dataset,
                                  resolve_direct_llm_manifest)
from motte_sdk.resolve import ManifestResolutionError, prepare_run
from motte_storage.resource_store import (
    InMemoryResourceStore,
    ResourceConflictError,
    SQLiteResourceStore,
)


def raw_source(count=6):
    return ("\n".join(json.dumps({"input": f"SYNTHETIC q{i}", "expected": f"a{i}"})
                      for i in range(count)) + "\n").encode()


def seeded_resources(*, ceiling=8192, context_window=None, count=6, scorer=None):
    resources = InMemoryResourceStore()
    resources.providers.put({"name": "local", "kind": "openai_compatible",
                             "base_url": "https://local.test/v1", "model": "unused"})
    profile = {"id": "probe", "provider": "local", "model": "probe-1",
               "capabilities": {}, "max_output_tokens": ceiling}
    if context_window is not None:
        profile["context_window"] = context_window
    resources.models.put(profile)
    record = import_direct_llm_jsonl(raw_source(count), name="direct-llm-synthetic", version="1",
                                     license_id="synthetic-only", scorer=scorer, source="synthetic",
                                     synthetic=True)
    resources.datasets.put(record)
    scenario = scenario_for(record, version="1")
    resources.scenarios.put(scenario)
    return resources, scenario, record


# ------------------------------------------------------------------ 内置样例

def test_builtin_catalog_matches_the_shipped_files():
    items = {item["id"]: item for item in builtin_catalog()}
    assert sorted(items) == sorted(entry.id for entry in BUILTIN_DATASETS)
    assert items["direct-llm-exact-answer"]["scorer"] == "exact"
    assert items["direct-llm-classify"]["scorer"] == "contains"
    assert items["direct-llm-json-extract"]["scorer"] == "regex"
    assert [items[key]["cases"] for key in sorted(items)] == [8, 8, 7]
    assert all(item["importable"] is True for item in items.values())
    assert all(item["source"] == f"builtin:{item['id']}" for item in items.values())


def test_every_builtin_file_imports_as_a_frozen_dataset():
    for entry in BUILTIN_DATASETS:
        record = import_direct_llm_jsonl(
            builtin_source(entry.id), name=entry.id, version="1", license_id="internal-sample",
            scorer=entry.scorer, source=f"builtin:{entry.id}")
        assert record["cases"], entry.id
        assert record["cases_sha256"]
        assert record["provenance"]["source_line_count"] == len(record["cases"])
        # 内置 JSONL 里同一份源文本被多题复用，case_id 必须仍然唯一
        assert len({case["case_id"] for case in record["cases"]}) == len(record["cases"])


def test_builtin_unknown_id_and_missing_directory_are_structured(monkeypatch):
    with pytest.raises(BuiltinUnavailable, match="unknown builtin dataset"):
        builtin_source("nope")
    monkeypatch.setenv(BUILTIN_ENV, "/definitely/not/here")
    assert builtin_dir().as_posix() == "/definitely/not/here"
    with pytest.raises(BuiltinUnavailable, match=BUILTIN_ENV):
        builtin_source("direct-llm-classify")
    # 目录缺失时清单仍然可用，只是标记为不可导入
    assert [item["importable"] for item in builtin_catalog()] == [False, False, False]


# ------------------------------------------------------------------ 落库

def test_import_split_persists_dataset_and_scenario_with_auto_version():
    resources = InMemoryResourceStore()
    receipt = import_direct_llm_split(raw_source(), name="direct-llm-synthetic", version=None,
                                      license_id="synthetic-only", resources=resources,
                                      source="synthetic", synthetic=True)
    assert receipt["imported"] == "direct-llm-synthetic@1"
    assert receipt["scenario"] == "direct-llm-synthetic@1"
    assert receipt["suite"] == "direct-llm" and receipt["scorer"] == "exact"
    assert receipt["cases"] == 6 and receipt["source"] == "synthetic"
    assert receipt["dataset_fingerprint"].startswith("sha256:")
    assert resources.datasets.get("direct-llm-synthetic", "1")["dataset_fingerprint"] == \
        receipt["dataset_fingerprint"]
    assert resources.scenarios.get("direct-llm-synthetic", "1")["dataset"] == "direct-llm-synthetic@1"
    # 同内容重复导入复用版本（幂等），内容不同才取下一个空号
    assert import_direct_llm_split(raw_source(), name="direct-llm-synthetic", version=None,
                                   license_id="synthetic-only", resources=resources,
                                   source="synthetic", synthetic=True) == receipt
    second = import_direct_llm_split(raw_source(7), name="direct-llm-synthetic", version=None,
                                     license_id="synthetic-only", resources=resources,
                                     source="synthetic", synthetic=True)
    assert second["imported"] == "direct-llm-synthetic@2"


def test_auto_version_uses_complete_dataset_identity():
    resources = InMemoryResourceStore()
    receipts = [
        import_direct_llm_split(
            raw_source(), name="identity", version=None, license_id=license_id,
            scorer=scorer, source=source, resources=resources, synthetic=True,
        )
        for source, license_id, scorer in (
            ("source-a", "license-a", "exact"),
            ("source-b", "license-a", "exact"),
            ("source-b", "license-b", "exact"),
            ("source-b", "license-b", "contains"),
        )
    ]
    assert [receipt["imported"] for receipt in receipts] == [
        "identity@1", "identity@2", "identity@3", "identity@4"
    ]
    assert len({receipt["dataset_fingerprint"] for receipt in receipts}) == 4
    assert len({receipt["cases_sha256"] for receipt in receipts}) == 1
    assert import_direct_llm_split(
        raw_source(), name="identity", version=None, license_id="license-b",
        scorer="contains", source="source-b", resources=resources, synthetic=True,
    ) == receipts[-1]


def test_legacy_dataset_without_fingerprint_remains_idempotent():
    resources = InMemoryResourceStore()
    legacy = import_direct_llm_jsonl(
        raw_source(), name="legacy", version="1", license_id="synthetic-only",
        source="synthetic", synthetic=True,
    )
    resources.datasets.put(legacy)
    resources.scenarios.put(scenario_for(legacy, version="1"))

    automatic = persist_direct_llm_dataset(legacy, resources)
    explicit = persist_direct_llm_dataset(legacy, resources, version="1")
    assert automatic == explicit
    assert automatic["imported"] == "legacy@1"
    assert automatic["dataset_fingerprint"].startswith("sha256:")
    assert "dataset_fingerprint" not in resources.datasets.get("legacy", "1")
    assert len(resources.datasets.list()) == 1


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_concurrent_auto_versions_retry_without_partial_pairs(tmp_path, monkeypatch, backend):
    resources = InMemoryResourceStore() if backend == "memory" else SQLiteResourceStore(
        tmp_path / "concurrent-direct.db"
    )
    records = [
        import_direct_llm_jsonl(
            raw_source(count), name="concurrent", version="1", license_id="synthetic-only",
            source="synthetic", synthetic=True,
        )
        for count in range(1, 13)
    ]
    barrier = Barrier(len(records))
    original_next = direct_llm_module.next_dataset_version

    def synchronized_next(record, store):
        candidate = original_next(record, store)
        barrier.wait(timeout=10)
        return candidate

    conflicts = 0
    conflicts_lock = Lock()
    original_publish = resources.publish_dataset_scenario

    def tracking_publish(dataset, scenario):
        nonlocal conflicts
        try:
            return original_publish(dataset, scenario)
        except ResourceConflictError:
            with conflicts_lock:
                conflicts += 1
            raise

    monkeypatch.setattr(direct_llm_module, "next_dataset_version", synchronized_next)
    monkeypatch.setattr(resources, "publish_dataset_scenario", tracking_publish)
    with ThreadPoolExecutor(max_workers=len(records)) as pool:
        futures = [pool.submit(persist_direct_llm_dataset, record, resources) for record in records]
        receipts = [future.result(timeout=20) for future in futures]

    versions = {str(index) for index in range(1, len(records) + 1)}
    assert conflicts >= len(records) - 1
    assert {receipt["imported"].split("@", 1)[1] for receipt in receipts} == versions
    assert {item["version"] for item in resources.datasets.list()} == versions
    assert {item["version"] for item in resources.scenarios.list()} == versions


def test_persist_is_atomic_when_the_scenario_conflicts():
    resources = InMemoryResourceStore()
    resources.scenarios.put({"name": "atomic", "version": "1", "cases": ["conflict"]})
    record = import_direct_llm_jsonl(
        raw_source(), name="atomic", version="1", license_id="synthetic-only",
        source="synthetic", synthetic=True,
    )

    with pytest.raises(ResourceConflictError, match="different content"):
        persist_direct_llm_dataset(record, resources, version="1")
    assert resources.datasets.get("atomic", "1") is None
    assert resources.scenarios.get("atomic", "1") == {
        "name": "atomic", "version": "1", "cases": ["conflict"]
    }
    assert persist_direct_llm_dataset(record, resources)["imported"] == "atomic@2"
    assert resources.datasets.get("atomic", "2") is not None
    assert resources.scenarios.get("atomic", "2") is not None


def test_persist_rejects_reusing_a_version_with_different_content():
    resources = InMemoryResourceStore()
    record = import_direct_llm_jsonl(raw_source(), name="direct-llm-synthetic", version="1",
                                     license_id="synthetic-only", source="synthetic")
    persist_direct_llm_dataset(record, resources)
    other = import_direct_llm_jsonl(raw_source(7), name="direct-llm-synthetic", version="1",
                                    license_id="synthetic-only", source="synthetic")
    with pytest.raises(ResourceConflictError):
        persist_direct_llm_dataset(other, resources, version="1")
    # 显式换版本号则允许（同 name 可以有多个不可变版本）
    assert persist_direct_llm_dataset(other, resources, version="2")["imported"] == \
        "direct-llm-synthetic@2"


# ------------------------------------------------------------------ 创建期展开

def test_resolve_projects_cases_verbatim_and_snapshots_the_dataset():
    resources, scenario, record = seeded_resources()
    manifest = resolve_direct_llm_manifest(scenario, {"model": "probe"}, resources)
    assert list(manifest["cases"]) == [case["case_id"] for case in record["cases"]]
    assert manifest["cases"]["direct-llm-synthetic-0000"] == {"prompt": "SYNTHETIC q0"}
    assert manifest["case_selection"] == {"mode": "all", "count": 6, "seed": None}
    assert manifest["benchmark_snapshot"]["dataset"] == record
    assert manifest["benchmark_snapshot"]["scenario"] == scenario
    provenance = manifest["benchmark_provenance"]
    assert provenance["suite"] == "direct-llm" and provenance["dataset"] == "direct-llm-synthetic@1"
    assert provenance["scorer"] == "exact" and provenance["scorer_version"] == SCORER_VERSION
    assert provenance["prompt_version"] == "direct-llm-verbatim-v1"
    assert provenance["max_output_tokens"] == 1024 and provenance["max_retries"] == 0
    assert provenance["run_selection"] == {"mode": "all", "count": 6, "seed": None}
    assert provenance["cases_sha256"] == record["cases_sha256"]
    assert provenance["dataset_fingerprint"].startswith("sha256:")


def test_resolve_honours_subset_and_run_level_output_budget():
    resources, scenario, _ = seeded_resources()
    manifest = resolve_direct_llm_manifest(scenario, {
        "model": "probe",
        "parameters": {"temperature": 0.4, "max_output_tokens": 64},
        "case_selection": {"mode": "ids", "case_ids": ["direct-llm-synthetic-0002",
                                                       "direct-llm-synthetic-0000"]},
    }, resources)
    assert list(manifest["cases"]) == ["direct-llm-synthetic-0000", "direct-llm-synthetic-0002"]
    assert manifest["benchmark_provenance"]["run_selection"] == {
        "mode": "ids", "count": 2, "seed": None}
    assert manifest["benchmark_provenance"]["max_output_tokens"] == 64
    assert manifest["benchmark_provenance"]["selected_count"] == 6  # 数据集本身不变


def test_resolve_rejects_reserved_keys_mismatch_and_bad_budget():
    resources, scenario, _ = seeded_resources()
    for manifest, message in (
            ({"cases": {}}, "cannot be overridden"),
            ({"benchmark_snapshot": {}}, "cannot be overridden"),
            ({"tools": []}, "cannot be overridden"),
            ({"dataset": "other@1"}, "does not match scenario"),
            ({"parameters": {"max_output_tokens": 0}}, "positive integer"),
            ({"parameters": {"max_output_tokens": True}}, "positive integer"),
            ({"case_selection": {"mode": "ids", "case_ids": ["nope"]}}, "not in dataset"),
    ):
        with pytest.raises(ValueError, match=message):
            resolve_direct_llm_manifest(scenario, manifest, resources)


def test_resolve_rejects_unknown_dataset():
    resources, scenario, record = seeded_resources()
    # 已登记套件的数据集不可删除，因此用指向缺失数据集的场景来覆盖该分支
    with pytest.raises(ResourceConflictError):
        resources.datasets.delete(record["name"], "1")
    orphan = {**scenario, "dataset": "direct-llm-missing@1"}
    with pytest.raises(ValueError, match="dataset not found"):
        resolve_direct_llm_manifest(orphan, {}, resources)


# ------------------------------------------------------------------ prepare_run 接线

def test_prepare_run_expands_a_direct_llm_scenario_once():
    resources, scenario, record = seeded_resources(context_window=4096)
    manifest, case_ids = prepare_run("direct-llm-synthetic@1", {"model": "probe"}, [], resources)
    assert case_ids == [case["case_id"] for case in record["cases"]]
    provider = manifest["provider"]
    assert provider["model"] == "probe-1"
    assert provider["parameters"]["max_output_tokens"] == 1024
    assert provider["max_retries"] == 0
    assert manifest["cases"]["direct-llm-synthetic-0000"]["prompt"] == "SYNTHETIC q0"
    preflight = manifest["budget"]["context_preflight"]
    assert preflight["method"] == "utf8-bytes-plus-structure-v1"
    assert preflight["context_window"] == 4096
    assert preflight["output_tokens_reserved"] == 1024
    assert preflight["stats"]["case_count"] == len(record["cases"])
    assert "per_case" not in preflight
    assert manifest["resource_snapshots"]["model_profile"]["context_window"] == 4096


def test_prepare_run_keeps_an_explicit_output_budget_and_rejects_over_ceiling():
    resources, scenario, _ = seeded_resources(ceiling=512, context_window=4096)
    manifest, _ = prepare_run("direct-llm-synthetic@1",
                              {"model": "probe", "parameters": {"max_output_tokens": 128}},
                              [], resources)
    assert manifest["provider"]["parameters"]["max_output_tokens"] == 128
    assert manifest["budget"]["context_preflight"]["output_tokens_reserved"] == 128
    with pytest.raises(ManifestResolutionError) as error:
        prepare_run("direct-llm-synthetic@1",
                    {"model": "probe", "parameters": {"max_output_tokens": 4096}}, [], resources)
    assert error.value.code == "MODEL_CONFIG_INVALID"


def test_prepare_run_rejects_context_overflow_before_execution_or_provider_calls(monkeypatch):
    import motte_provider.config as provider_config
    import motte_sdk.resolve as resolve_module

    resources, _, _ = seeded_resources(context_window=1024)
    calls = {"execution": 0, "provider": 0}

    def unexpected_execution(*args, **kwargs):
        calls["execution"] += 1
        raise AssertionError("resolve_execution must not run after context preflight fails")

    def unexpected_provider(*args, **kwargs):
        calls["provider"] += 1
        raise AssertionError("provider construction must not run during creation preflight")

    monkeypatch.setattr(resolve_module, "resolve_execution", unexpected_execution)
    monkeypatch.setattr(provider_config, "build_case_provider", unexpected_provider)

    with pytest.raises(ManifestResolutionError) as error:
        prepare_run("direct-llm-synthetic@1", {"model": "probe"}, [], resources)

    assert error.value.code == "CONTEXT_WINDOW_EXCEEDED"
    assert "direct-llm-synthetic-0000" in str(error.value)
    assert calls == {"execution": 0, "provider": 0}


def test_prepare_run_requires_provider_and_rejects_two_selection_channels():
    resources, scenario, _ = seeded_resources()
    with pytest.raises(ManifestResolutionError) as error:
        prepare_run("direct-llm-synthetic@1", {}, [], resources)
    assert error.value.code == "RUN_CONFIG_INVALID"
    with pytest.raises(ManifestResolutionError) as error:
        prepare_run("direct-llm-synthetic@1", {"model": "probe"}, ["direct-llm-synthetic-0000"],
                    resources)
    assert error.value.code == "RUN_CONFIG_INVALID" and "case_selection" in str(error.value)
    with pytest.raises(ManifestResolutionError) as error:
        prepare_run("direct-llm-synthetic@1",
                    {"model": "probe", "benchmark_provenance": {}}, [], resources)
    assert error.value.code == "SNAPSHOT_RESERVED"


def test_case_selection_is_still_benchmark_only():
    resources = InMemoryResourceStore()
    with pytest.raises(ManifestResolutionError, match="only supported for benchmark"):
        prepare_run("direct-llm@1", {"case_selection": {"mode": "all"}}, [], resources)


# ------------------------------------------------------------------ 评分

def _run_with(manifest, case_ids):
    return {"id": "run-1", "case_ids": list(case_ids), "manifest": manifest,
            "status": "completed"}


def test_scores_cover_every_outcome_and_missing_results():
    resources, scenario, record = seeded_resources(count=5)
    manifest = resolve_direct_llm_manifest(scenario, {"model": "probe"}, resources)
    run = _run_with(manifest, manifest["cases"])
    results = [
        {"case_id": "direct-llm-synthetic-0000", "result": {"content": "a0"}},
        {"case_id": "direct-llm-synthetic-0001", "result": {"content": "nope"}},
        {"case_id": "direct-llm-synthetic-0002",
         "result": {"error": {"class": "rate_limit", "message": "busy"}}},
        # 0003 完全没有落库结果（例如运行中途取消）→ 未尝试
        {"case_id": "direct-llm-synthetic-0004", "outcome": "not_attempted", "result": None},
    ]
    scores = direct_llm_scores(run, results)
    assert [score["outcome"] for score in scores] == [
        "correct", "wrong_answer", "call_failed", "not_attempted", "not_attempted"]
    assert scores[0]["passed"] is True and scores[0]["judged"] is True
    assert scores[1]["passed"] is False
    assert scores[2]["error_class"] == "rate_limit" and scores[2]["responded"] is False
    assert scores[3]["attempted"] is False and scores[3]["scorer"] == "exact"
    assert scores[4] == {"case_id": "direct-llm-synthetic-0004", "outcome": "not_attempted",
                         "passed": False, "attempted": False, "responded": False, "judged": False,
                         "scorer": "exact", "scorer_version": SCORER_VERSION}


def test_scores_use_per_case_scorer_and_skip_cases_without_expectation():
    raw = ("\n".join([
        json.dumps({"input": "q0", "expected": "LABEL", "scorer": "contains"}),
        json.dumps({"input": "q1"}),
        json.dumps({"input": "q2", "expected": r"\d{3}", "scorer": "regex"}),
    ]) + "\n").encode()
    resources = InMemoryResourceStore()
    resources.providers.put({"name": "local", "kind": "openai_compatible",
                             "base_url": "https://local.test/v1", "model": "m"})
    record = import_direct_llm_jsonl(raw, name="direct-llm-mixed", version="1",
                                     license_id="synthetic-only", scorer="exact")
    resources.datasets.put(record)
    scenario = scenario_for(record, version="1")
    manifest = resolve_direct_llm_manifest(scenario, {}, resources)
    run = _run_with(manifest, manifest["cases"])
    scores = direct_llm_scores(run, [
        {"case_id": "direct-llm-mixed-0000", "result": {"content": "判定：LABEL"}},
        {"case_id": "direct-llm-mixed-0001", "result": {"content": "任意输出"}},
        {"case_id": "direct-llm-mixed-0002", "result": {"content": "code 404"}},
    ])
    assert [score["outcome"] for score in scores] == ["correct", "no_expectation", "correct"]
    assert [score["scorer"] for score in scores] == ["contains", "exact", "regex"]
    assert scores[1]["judged"] is False


def test_scores_reject_an_unsupported_snapshot_scorer_version():
    resources, scenario, _ = seeded_resources()
    manifest = resolve_direct_llm_manifest(scenario, {}, resources)
    manifest["benchmark_provenance"]["scorer_version"] = "direct-llm-answer-v0"
    with pytest.raises(ValueError, match="unsupported snapshot scorer version"):
        direct_llm_scores(_run_with(manifest, manifest["cases"]), [])
