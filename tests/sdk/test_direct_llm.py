"""Direct LLM SDK：内置样例、落库、创建期展开、评分（零网络零费用）。"""
import json

import pytest

from motte_contracts.direct_llm import SCORER_VERSION, import_direct_llm_jsonl, scenario_for
from motte_sdk.direct_llm import (BUILTIN_DATASETS, BUILTIN_ENV, BuiltinUnavailable, builtin_catalog,
                                  builtin_dir, builtin_source, direct_llm_scores,
                                  import_direct_llm_split, persist_direct_llm_dataset,
                                  resolve_direct_llm_manifest)
from motte_sdk.resolve import ManifestResolutionError, prepare_run
from motte_storage.resource_store import InMemoryResourceStore, ResourceConflictError


def raw_source(count=6):
    return ("\n".join(json.dumps({"input": f"SYNTHETIC q{i}", "expected": f"a{i}"})
                      for i in range(count)) + "\n").encode()


def seeded_resources(*, ceiling=8192, count=6, scorer=None):
    resources = InMemoryResourceStore()
    resources.providers.put({"name": "local", "kind": "openai_compatible",
                             "base_url": "https://local.test/v1", "model": "unused"})
    resources.models.put({"id": "probe", "provider": "local", "model": "probe-1",
                          "capabilities": {}, "max_output_tokens": ceiling})
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
    assert resources.scenarios.get("direct-llm-synthetic", "1")["dataset"] == "direct-llm-synthetic@1"
    # 同内容重复导入复用版本（幂等），内容不同才取下一个空号
    assert import_direct_llm_split(raw_source(), name="direct-llm-synthetic", version=None,
                                   license_id="synthetic-only", resources=resources,
                                   source="synthetic", synthetic=True) == receipt
    second = import_direct_llm_split(raw_source(7), name="direct-llm-synthetic", version=None,
                                     license_id="synthetic-only", resources=resources,
                                     source="synthetic", synthetic=True)
    assert second["imported"] == "direct-llm-synthetic@2"


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
    resources, scenario, record = seeded_resources()
    manifest, case_ids = prepare_run("direct-llm-synthetic@1", {"model": "probe"}, [], resources)
    assert case_ids == [case["case_id"] for case in record["cases"]]
    provider = manifest["provider"]
    assert provider["model"] == "probe-1"
    assert provider["parameters"]["max_output_tokens"] == 1024
    assert provider["max_retries"] == 0
    assert manifest["cases"]["direct-llm-synthetic-0000"]["prompt"] == "SYNTHETIC q0"


def test_prepare_run_keeps_an_explicit_output_budget_and_rejects_over_ceiling():
    resources, scenario, _ = seeded_resources(ceiling=512)
    manifest, _ = prepare_run("direct-llm-synthetic@1",
                              {"model": "probe", "parameters": {"max_output_tokens": 128}},
                              [], resources)
    assert manifest["provider"]["parameters"]["max_output_tokens"] == 128
    with pytest.raises(ManifestResolutionError) as error:
        prepare_run("direct-llm-synthetic@1",
                    {"model": "probe", "parameters": {"max_output_tokens": 4096}}, [], resources)
    assert error.value.code == "MODEL_CONFIG_INVALID"


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
