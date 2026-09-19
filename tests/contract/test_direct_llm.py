"""Synthetic data only. Direct LLM 契约：整份校验、评分器版本、套件判定。"""

import hashlib
import json
from copy import deepcopy

import pytest

from motte_contracts import suites
from motte_contracts.direct_llm import (
    CONTRACT_VERSION,
    DATASET_ID,
    DATASET_VERSION,
    DEFAULT_SCORER,
    EVAL_KEY,
    PLUGIN_VERSION,
    SCORER_VERSION,
    SUITE,
    effective_scorers,
    import_direct_llm_jsonl,
    is_dataset,
    is_scenario,
    normalize_scorer,
    scenario_for,
    validate_dataset,
    validate_scenario,
)
from motte_contracts.selection import digest, run_selected_count, select_cases
from motte_storage.resource_store import (
    InMemoryResourceStore,
    ResourceConflictError,
    SQLiteResourceStore,
)


def raw_source(rows=None):
    rows = (
        rows
        if rows is not None
        else [{"input": f"SYNTHETIC question {i}", "expected": f"answer-{i}"} for i in range(6)]
    )
    return ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode()


def imported(raw=None, *, name="direct-llm-synthetic", scorer=None, version="1"):
    return import_direct_llm_jsonl(
        raw if raw is not None else raw_source(),
        name=name,
        version=version,
        license_id="synthetic-only",
        scorer=scorer,
        source="synthetic",
        synthetic=True,
    )


def test_import_freezes_the_whole_file_and_provenance():
    raw = raw_source()
    record = imported(raw)
    assert record == imported(raw)
    assert record["provenance"] == {
        "source": "synthetic",
        "license": "synthetic-only",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_line_count": 6,
        "synthetic": True,
    }
    assert record["cases_sha256"] == digest(record["cases"])
    assert record[EVAL_KEY] == {
        "suite": SUITE,
        "id": DATASET_ID,
        "version": DATASET_VERSION,
        "selected_count": 6,
        "selection": "all-rows-in-file-order",
        "scorer": DEFAULT_SCORER,
        "prompt_version": "direct-llm-verbatim-v1",
        "scorer_version": SCORER_VERSION,
        "max_output_tokens": 1024,
        "max_retries": 0,
    }
    assert [case["case_id"] for case in record["cases"]] == [
        f"direct-llm-synthetic-{i:04d}" for i in range(6)
    ]
    assert record["cases"][0] == {
        "case_id": "direct-llm-synthetic-0000",
        "input": "SYNTHETIC question 0",
        "expected": "answer-0",
        "metadata": {"source_line": 1},
    }
    assert is_dataset(record) is True and is_scenario(record) is False


def test_import_accepts_prompt_alias_custom_ids_and_scorer_overrides():
    raw = raw_source(
        [
            {"prompt": "用 prompt 别名提问", "expected": "ok"},
            {"input": "自定义 id", "case_id": "custom-7", "expected": "ok"},
            {"input": "单题覆盖评分器", "expected": "LABEL", "scorer": "contains"},
            {"input": "没有期望答案的题"},
        ]
    )
    record = imported(raw)
    assert [case["case_id"] for case in record["cases"]] == [
        "direct-llm-synthetic-0000",
        "custom-7",
        "direct-llm-synthetic-0002",
        "direct-llm-synthetic-0003",
    ]
    assert record["cases"][0]["input"] == "用 prompt 别名提问"
    assert record["cases"][2]["metadata"] == {"source_line": 3, "scorer": "contains"}
    assert "expected" not in record["cases"][3]
    assert effective_scorers(record) == {
        "direct-llm-synthetic-0000": "exact",
        "custom-7": "exact",
        "direct-llm-synthetic-0002": "contains",
        "direct-llm-synthetic-0003": "exact",
    }


def test_import_skips_blank_lines_but_keeps_physical_line_numbers():
    raw = b'{"input": "first", "expected": "a"}\n\n{"input": "second", "expected": "b"}\n'
    record = imported(raw)
    assert [case["metadata"]["source_line"] for case in record["cases"]] == [1, 3]
    assert record["provenance"]["source_line_count"] == 2


@pytest.mark.parametrize(
    "rows,message",
    [
        (
            [
                {"input": "a", "expected": "b"},
                {"input": "a", "expected": "b"},
                {"input": "a", "expected": "b"},
                {"input": "a", "expected": "b"},
                {"input": "a", "case_id": "direct-llm-synthetic-0000", "expected": "b"},
            ],
            "duplicate case_id",
        ),
        ([{"input": "", "expected": "b"}], "input/prompt"),
        ([{"expected": "b"}], "input/prompt"),
        ([{"input": "a", "prompt": "b"}], "input/prompt"),
        ([{"input": "a", "expected": "b", "unknown": 1}], "unknown keys"),
        ([{"input": "a", "expected": ""}], "expected"),
        ([{"input": "a", "expected": 3}], "expected"),
        ([{"input": "a", "expected": "b", "scorer": "fuzzy"}], "scorer"),
        ([{"input": "a", "expected": "[unclosed", "scorer": "regex"}], "invalid regex"),
        ([{"input": "a", "case_id": "  ", "expected": "b"}], "case_id"),
    ],
)
def test_import_rejects_bad_rows_as_a_whole(rows, message):
    with pytest.raises(ValueError, match=message):
        imported(raw_source(rows))


def test_import_dataset_level_regex_scorer_is_validated_too():
    assert imported(raw_source([{"input": "a", "expected": "\\d+"}]), scorer="regex")
    with pytest.raises(ValueError, match="invalid regex"):
        imported(raw_source([{"input": "a", "expected": "[unclosed"}]), scorer="regex")


@pytest.mark.parametrize(
    "raw,message",
    [
        (b"", "at least one case"),
        (b"\n\n", "at least one case"),
        (b"not json\n", "invalid JSON at source line 1"),
        (b'"scalar"\n', "expected a JSON object"),
        (b'{"input": "a"}\n{"input": "b"}\n'.replace(b"}", b"}", 1) + b"oops\n", "invalid JSON"),
    ],
)
def test_import_rejects_bad_files(raw, message):
    with pytest.raises(ValueError, match=message):
        imported(raw)


def test_import_requires_identity_fields_and_safe_name():
    with pytest.raises(ValueError, match="required"):
        import_direct_llm_jsonl(raw_source(), name="", version="1", license_id="X")
    with pytest.raises(ValueError, match="required"):
        import_direct_llm_jsonl(raw_source(), name="n", version="1", license_id="")
    with pytest.raises(ValueError, match="name"):
        import_direct_llm_jsonl(raw_source(), name="bad name", version="1", license_id="X")
    with pytest.raises(ValueError, match="UTF-8"):
        import_direct_llm_jsonl(b"\xff\xfe", name="n", version="1", license_id="X")


def test_validate_dataset_detects_tampering():
    record = imported()
    truncated = deepcopy(record)
    truncated["cases"] = truncated["cases"][:3]
    truncated["cases_sha256"] = digest(truncated["cases"])
    with pytest.raises(ValueError, match="selected count"):
        validate_dataset(truncated)
    unhashed = deepcopy(record)
    unhashed["cases"][0]["input"] = "tampered"
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_dataset(unhashed)
    unknown_eval = deepcopy(record)
    unknown_eval[EVAL_KEY] = {**record[EVAL_KEY], "id": "other-prompts"}
    with pytest.raises(ValueError, match="unsupported direct-llm dataset"):
        validate_dataset(unknown_eval)
    bad_source = deepcopy(record)
    bad_source["provenance"]["source_sha256"] = "zz"
    with pytest.raises(ValueError, match="provenance"):
        validate_dataset(bad_source)
    few_lines = deepcopy(record)
    few_lines["provenance"]["source_line_count"] = 2
    with pytest.raises(ValueError, match="provenance"):
        validate_dataset(few_lines)
    forged_fingerprint = deepcopy(record)
    forged_fingerprint["dataset_fingerprint"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        validate_dataset(forged_fingerprint)


def test_scenario_for_and_validate_scenario():
    record = imported()
    scenario = scenario_for(record, version="1")
    assert scenario["name"] == record["name"] and scenario["version"] == "1"
    assert scenario["mode"] == "direct-llm" and scenario["dataset"] == f"{record['name']}@1"
    assert scenario[EVAL_KEY] == record[EVAL_KEY]
    validate_scenario(scenario)
    assert is_scenario(scenario) is True and is_dataset(scenario) is False
    for broken in (
        {**scenario, "mode": "replay"},
        {**scenario, "dataset": "no-at-sign"},
        {**scenario, "version": ""},
        {**scenario, EVAL_KEY: {**scenario[EVAL_KEY], "selected_count": 0}},
    ):
        with pytest.raises(ValueError, match="invalid direct-llm scenario"):
            validate_scenario(broken)


def test_v1_explicit_version_markers_must_be_consistent_and_typed():
    record = imported()
    validate_dataset({**record, "contract_version": CONTRACT_VERSION})
    for marker in (2, 99, "1", True):
        with pytest.raises(ValueError, match="contract_version"):
            validate_dataset({**record, "contract_version": marker})
    with pytest.raises(ValueError, match="cannot declare scenario plugin_version"):
        validate_dataset({**record, "plugin_version": PLUGIN_VERSION})

    scenario = scenario_for(record, version="1")
    validate_scenario(
        {
            **scenario,
            "contract_version": CONTRACT_VERSION,
            "plugin_version": PLUGIN_VERSION,
        }
    )
    for marker in ("2", "99", 1, True):
        with pytest.raises(ValueError, match="plugin_version"):
            validate_scenario({**scenario, "plugin_version": marker})
    with pytest.raises(ValueError, match="contract_version"):
        validate_scenario({**scenario, "contract_version": 2})

    store = InMemoryResourceStore()
    with pytest.raises(ValueError, match="contract_version"):
        store.datasets.put({**record, "contract_version": 2})
    with pytest.raises(ValueError, match="plugin_version"):
        store.scenarios.put({**scenario, "plugin_version": "2"})


def test_bool_and_unknown_eval_versions_never_validate_as_v1():
    record = imported()
    for version in (True, "1", 3):
        changed = deepcopy(record)
        changed[EVAL_KEY]["version"] = version
        assert suites.suite_of(changed) == SUITE
        with pytest.raises(ValueError, match="unsupported direct-llm dataset contract version"):
            suites.validate_dataset(changed)
        with pytest.raises(ValueError, match="unsupported direct-llm dataset"):
            validate_dataset(changed)


def test_normalize_scorer_is_strict():
    assert normalize_scorer(None) == DEFAULT_SCORER
    assert normalize_scorer("") == DEFAULT_SCORER
    assert normalize_scorer("regex") == "regex"
    with pytest.raises(ValueError, match="unsupported scorer"):
        normalize_scorer("semantic")


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_managed_versions_are_immutable_while_generic_records_still_upsert(tmp_path, backend):
    store = (
        InMemoryResourceStore()
        if backend == "memory"
        else SQLiteResourceStore(tmp_path / "resources.db")
    )
    record = imported()
    assert store.datasets.put(record) == store.datasets.put(record)
    changed = deepcopy(record)
    changed["cases"][0]["input"] = "different"
    changed["cases_sha256"] = digest(changed["cases"])
    with pytest.raises(ResourceConflictError):
        store.datasets.put(changed)
    with pytest.raises(ResourceConflictError):
        store.datasets.put({"name": record["name"], "version": "1"})
    with pytest.raises(ResourceConflictError):
        store.datasets.delete(record["name"], "1")
    scenario = scenario_for(record, version="1")
    store.scenarios.put(scenario)
    with pytest.raises(ResourceConflictError):
        store.scenarios.put({**scenario, "dataset": "other@1"})
    # 所有版本化资源都 insert-only；不能用未知 suite 绕过不可变语义。
    generic = {"name": "generic", "version": "1", "anything": 1}
    assert store.datasets.put(generic) == store.datasets.put(generic)
    with pytest.raises(ResourceConflictError):
        store.datasets.put({"name": "generic", "version": "1", "anything": 2})
    other = {
        "name": "other",
        "version": "1",
        EVAL_KEY: {**record[EVAL_KEY], "suite": "other-suite"},
    }
    store.datasets.put(other)
    with pytest.raises(ResourceConflictError):
        store.datasets.delete("other", "1")


def test_suite_dispatch_recognizes_both_suites():
    from motte_contracts.gsm8k import import_official_jsonl

    direct = imported()
    gsm8k = import_official_jsonl(
        b'{"question": "q", "answer": "r\\n#### 1"}\n',
        name="gsm8k-test",
        version="1",
        revision="synthetic",
        license_id="synthetic-only",
        synthetic=True,
        scope="full",
    )
    assert suites.suite_of(direct) == SUITE
    assert suites.suite_of(gsm8k) == "gsm8k"
    assert suites.suite_of({"name": "x", "version": "1"}) is None
    incomplete = {"name": "x", "version": "1", EVAL_KEY: {"suite": SUITE}}
    assert suites.is_managed(incomplete) is False
    explicit = {**incomplete, "contract_version": 2}
    assert suites.is_managed(explicit) is True
    with pytest.raises(ValueError, match="unsupported direct-llm dataset contract version"):
        suites.validate_dataset(explicit)
    direct_scenario = scenario_for(direct, version="1")
    suites.validate_dataset(direct)
    suites.validate_scenario(direct_scenario)
    assert suites.validated_dataset_identity(direct) == (SUITE, "1")
    assert suites.validated_scenario_identity(direct_scenario) == (SUITE, "1")
    with pytest.raises(ValueError, match="unsupported dataset"):
        suites.validate_dataset({"name": "x", "version": "1"})
    with pytest.raises(ValueError, match="unsupported scenario"):
        suites.validate_scenario({"name": "x", "version": "1"})


def test_suite_of_run_uses_the_snapshot_and_defaults_to_gsm8k():
    assert suites.suite_of_run({"manifest": {}}) is None
    assert (
        suites.suite_of_run({"manifest": {"benchmark_provenance": {"selected_count": 3}}})
        == "gsm8k"
    )
    assert suites.suite_of_run({"manifest": {"benchmark_provenance": {"suite": SUITE}}}) == SUITE
    with pytest.raises(ValueError, match="unsupported explicit eval suite"):
        suites.suite_of_run({"manifest": {"benchmark_provenance": {"suite": "unknown"}}})


def test_gsm8k_provenance_carries_an_explicit_suite_marker():
    """两个套件的运行都带 benchmark_provenance，`suite` 是唯一判别字段（旧运行缺省按 gsm8k）。"""
    from motte_contracts.gsm8k import import_official_jsonl
    from motte_contracts.gsm8k import scenario_for as gsm8k_scenario_for
    from motte_sdk.benchmark import resolve_benchmark_manifest

    raw = b'{"question": "q", "answer": "r\\n#### 1"}\n'
    dataset = import_official_jsonl(
        raw,
        name="gsm8k-test",
        version="1",
        revision="synthetic",
        license_id="synthetic-only",
        synthetic=True,
        scope="full",
    )
    store = InMemoryResourceStore()
    store.datasets.put(dataset)
    scenario = gsm8k_scenario_for(dataset, name="gsm8k-test-full", version="1")
    manifest = resolve_benchmark_manifest(scenario, {}, store)
    assert manifest["benchmark_provenance"]["suite"] == "gsm8k"
    assert suites.suite_of_run({"manifest": manifest}) == "gsm8k"


def test_run_level_selection_is_shared_with_gsm8k():
    record = imported(raw_source([{"input": f"q{i}", "expected": f"a{i}"} for i in range(10)]))
    every = select_cases(record)
    assert every["mode"] == "all" and every["count"] == 10
    picked = select_cases(
        record,
        {"mode": "ids", "case_ids": ["direct-llm-synthetic-0003", "direct-llm-synthetic-0001"]},
    )
    assert picked["case_ids"] == ["direct-llm-synthetic-0001", "direct-llm-synthetic-0003"]
    random = select_cases(record, {"mode": "random", "count": 4, "seed": "deadbeef"})
    assert random == select_cases(record, {"mode": "random", "count": 4, "seed": "deadbeef"})
    assert run_selected_count({"selected_count": 10, "run_selection": {"count": 4}}) == 4
    assert run_selected_count({"selected_count": 10}) == 10
