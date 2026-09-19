"""M2 第二轮 review（2026-09-20）12 项修复的验收测试。

逐项对应 docs/verification/M2-review-round2-2026-09-20.md 的"修复验收"：
R2-01 桥接（离线部分）、R2-02 时间戳目录、R2-03 稀疏映射、R2-04 快照
可比性、R2-05 内容身份与 revision 重写、R2-06 先冻结后解析、R2-07 恢复
不替换证据、R2-09 默认超时、R2-10 token 预算。R2-11 见 web 测试；
R2-12 由 make openapi-check 门禁。真实固定环境端到端仍按 not_run 记录。
"""
import json
import os
import time
from pathlib import Path

import pytest

from motte_benchmark.fake_runner import self_argv
from motte_benchmark.opencompass.adapter import CevalJobAdapter
from motte_benchmark.opencompass.entry import (
    export_subject_files,
    main as entry_main,
    render_opencompass_config_source,
)
from motte_benchmark.opencompass.parser import (
    CevalParserError,
    parse_opencompass_files,
    parse_opencompass_results,
)
from motte_benchmark.process import ProcessJobAdapter
from motte_sdk.benchmark_catalog import (
    BenchmarkCatalog,
    prepare_external_dataset,
    prepare_external_run_inputs,
    validate_external_run_request,
)
from motte_sdk.comparisons import ComparisonError, ComparisonService
from motte_sdk.external_jobs import DurableExternalJobRunner, ExternalJobSupervisor
from motte_sdk.service import RunService
from motte_storage.artifacts import ArtifactStore
from motte_storage.benchmark_datasets import SQLiteBenchmarkDatasets
from motte_storage.external_jobs import SQLiteExternalJobs
from motte_storage.run_store import SQLiteRunStore
from motte_contracts.external_job import ExternalJobSpec


def _row(row_id, subject, answer, question="Q?"):
    row = {
        "id": row_id, "subject": subject, "question": question,
        "A": "1", "B": "2", "C": "3", "D": "4",
    }
    if answer is not None:
        row["answer"] = answer
    return row


def _dataset(rows, revision="rev-r2-1", split="val"):
    content = "\n".join(
        json.dumps({**row, "split": split}, ensure_ascii=False) for row in rows
    ).encode("utf-8")
    return prepare_external_dataset(
        files={"data.jsonl": content}, dataset_revision=revision, default_split=split,
    )


def _published_model(**overrides):
    return {
        "id": "m-r2", "provider": "openai", "model": "gpt-r2",
        "parameters": {}, "lifecycle": "published", "context_window": 8192,
        **overrides,
    }


def _inputs(dataset, **kwargs):
    return prepare_external_run_inputs(
        dataset, benchmark_id="ceval", model_id="m-r2",
        model_record=_published_model(), **kwargs,
    )


LOGIC_ROWS = [_row(f"logic-{i}", "logic", "B") for i in range(1, 5)]


def _adapter_with_tree(tmp_path, cases, details, dataset="ceval", rel=None):
    adapter = CevalJobAdapter(argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "hang"})
    spec = ExternalJobSpec(
        run_id="run-r2", adapter_id="ceval-opencompass", adapter_version="1",
        runner_version="r", execution_config_hash="h", dataset_revision="rev",
        selected_case_ids=[case["case_id"] for case in cases],
        profile={"benchmark_id": "ceval", "benchmark_version": "1"},
        work_root=str(tmp_path), environment_digest="d",
        runner_config={"cases": cases},
    )
    handle = adapter.prepare(spec)
    target = Path(handle.work_dir) / (rel or "outputs/results/mock-model")
    target.mkdir(parents=True)
    (target / f"{dataset}-logic.json").write_text(
        json.dumps({"accuracy": 50.0, "details": details}, ensure_ascii=False),
        encoding="utf-8",
    )
    return adapter, handle


def _review_service(tmp_path, mode, monkeypatch):
    from motte_benchmark import registry

    store = SQLiteRunStore(tmp_path / "runs.db")
    service = RunService(store)
    jobs = SQLiteExternalJobs(str(tmp_path / "runs.db"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MOTTE_JOB_WORK_ROOT", str(tmp_path / "jobs"))
    monkeypatch.setitem(
        registry._FACTORIES, "ceval-opencompass",
        registry._FACTORIES.get("ceval-opencompass"),
    )
    registry.register_adapter("ceval-opencompass", lambda: CevalJobAdapter(
        argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": mode},
    ), replace=True)
    return service, jobs, artifacts


# ------------------------------------------------------------------ R2-01


def test_r2_01_bridge_renders_opencompass_config_with_credential_refs(tmp_path):
    work = tmp_path / "job"
    work.mkdir()
    # 使用生产 runner-config 的 case 形态（question/options/prompt；
    # review R3-02：桥接消费结构化字段，目标 gold 不随 case 导出）。
    platform = {
        "benchmark_id": "ceval",
        "model": {"model": "gpt-r2", "parameters": {"temperature": 0.0}},
        "credentials": {"api_key": {"ref": "env:MOTTE_TEST_KEY"}},
        "few_shot": {"count": 0, "source_split": "dev"},
        "cases": [
            {
                "case_id": "logic-1", "subject": "logic", "question": "Q?",
                "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                "prompt": "…Q?…Answer:",
            },
        ],
    }
    data_files = export_subject_files(work, platform)
    assert (work / "data" / "logic.jsonl").exists()
    source = render_opencompass_config_source(platform, data_files)
    assert "models = [" in source and "datasets = [" in source
    # 凭据引用经 os.environ 解析，明文不落盘。
    assert "os.environ" in source and "MOTTE_TEST_KEY" in source
    assert "sk-plain" not in source
    assert "opencompass-cli" not in source
    exported = [json.loads(line) for line in data_files["logic"].read_text().splitlines()]
    assert exported[0]["id"] == "logic-1"
    assert exported[0]["question"] == "Q?" and exported[0]["B"] == "2"
    assert "answer" not in exported[0]  # 目标 gold 不进 Runner 数据


def test_r2_01_entry_invokes_pinned_cli_positionally_without_identity_flags(tmp_path, monkeypatch):
    work = tmp_path / "job"
    work.mkdir()
    (work / "runner-config.json").write_text(json.dumps({
        "benchmark_id": "ceval",
        "model": {"model": "gpt-r2", "parameters": {}},
        "credentials": {},
        "few_shot": {"count": 0},
        "cases": [
            {
                "case_id": "logic-1", "subject": "logic", "question": "Q?",
                "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                "prompt": "…Q?…Answer:",
            },
        ],
    }), encoding="utf-8")
    # 假 pinned 解释器：记录 argv，模拟 0.4.2 CLI 的时间戳目录行为。
    stub = tmp_path / "stub-python"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$@" > "$MOTTE_WORK_DIR/cli-argv.txt"\n'
        'mkdir -p "$MOTTE_WORK_DIR/outputs/20260920_090000/results/mock-model"\n'
        'printf \'{"accuracy": 100.0, "details": {"0": '
        '{"origin_prediction": "B", "predictions": "B", "references": "B"}}}\' '
        '> "$MOTTE_WORK_DIR/outputs/20260920_090000/results/mock-model/ceval-logic.json"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("MOTTE_WORK_DIR", str(work))
    monkeypatch.setenv("MOTTE_RUNNER_PYTHON", str(stub))
    exit_code = entry_main([])
    assert exit_code == 0
    argv = (work / "cli-argv.txt").read_text().split("\n")[0].split()
    # 固定版 CLI 调用形态：-m opencompass.cli.main <config(位置参数)>
    # --work-dir <outputs>；无 --config/--launch-token 等 0.4.2 不支持的
    # 身份参数（review R2-01）。
    assert argv[0:2] == ["-m", "opencompass.cli.main"]
    assert argv[2].endswith("opencompass-config.py")
    assert argv[3:] == ["--work-dir", str(work / "outputs")]
    assert "--launch-token" not in argv and "--config" not in argv
    # 实验目录指针 + 完成标记（R2-02/R2-10 契约）。
    pointer = json.loads((work / "outputs" / "experiment.json").read_text())
    assert pointer["experiment"] == "20260920_090000"
    marker = json.loads((work / ".motte-job-complete").read_text())
    assert marker == {"exit_code": 0, "completed": True}
    # 指针下可直接解析（真实固定版产物形态）。
    parsed = parse_opencompass_results(work / "outputs", dataset="ceval")
    assert parsed["samples"][0]["prediction"] == "B"


# ------------------------------------------------------------------ R2-02


def test_r2_02_timestamp_layout_pointer_ambiguity_and_legacy(tmp_path):
    doc = {"accuracy": 100.0, "details": {
        "0": {"origin_prediction": "B", "predictions": "B", "references": "B"},
    }}

    def mk(layout: dict, pointer=None):
        work = tmp_path / f"job-{len(list(tmp_path.iterdir()))}"
        out = work / "outputs"
        for rel, payload in layout.items():
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload), encoding="utf-8")
        if pointer is not None:
            (out / "experiment.json").write_text(json.dumps(pointer), encoding="utf-8")
        return work

    work = mk({"20260920_090000/results/mock-model/ceval-logic.json": doc})
    parsed = parse_opencompass_results(work / "outputs", dataset="ceval")
    assert parsed["experiment"] == "20260920_090000"

    work = mk(
        {"20260920_a/results/mock-model/ceval-logic.json": doc,
         "20260920_b/results/mock-model/ceval-logic.json": doc},
        pointer={"experiment": "20260920_b"},
    )
    assert parse_opencompass_results(work / "outputs", dataset="ceval")["experiment"] == "20260920_b"

    work = mk({"20260920_a/results/mock-model/ceval-logic.json": doc,
               "20260920_b/results/mock-model/ceval-logic.json": doc})
    with pytest.raises(CevalParserError, match="ambiguous-experiment"):
        parse_opencompass_results(work / "outputs", dataset="ceval")

    work = mk({"results/mock-model/ceval-logic.json": doc})
    assert parse_opencompass_results(work / "outputs", dataset="ceval")["experiment"] is None

    work = mk({"results/mock-model/ceval-logic.json": doc,
               "20260920_a/results/mock-model/ceval-law.json": doc})
    with pytest.raises(CevalParserError, match="ambiguous-experiment"):
        parse_opencompass_results(work / "outputs", dataset="ceval")


def test_r2_02_ts_layout_runs_through_full_dispatch_chain(tmp_path, monkeypatch):
    service, jobs, artifacts = _review_service(tmp_path, "opencompass_ts_ok", monkeypatch)
    dataset = _dataset(LOGIC_ROWS)
    inputs = _inputs(dataset)
    run = service.create_run(inputs["scenario_version"], inputs["manifest"], inputs["case_ids"])
    from motte_sdk.dispatcher import RunDispatcher

    finished = RunDispatcher(service).dispatch(run["id"])
    assert finished["status"] == "completed"
    job = jobs.jobs_for_run(run["id"])[0]
    assert job["checkpoint"]["evidence"]["frozen_before_parse"] is True
    bundle = json.loads(
        artifacts.read_bytes(job["checkpoint"]["evidence"]["raw_bundle_artifact"]),
    )
    assert any(
        "20260920_090000/results/mock-model/ceval-logic.json" in rel
        for rel in bundle["files"]
    )


# ------------------------------------------------------------------ R2-03


def test_r2_03_sparse_rows_map_by_original_row_index(tmp_path):
    cases = [
        {"case_id": "c1", "subject": "logic"},
        {"case_id": "c2", "subject": "logic"},
        {"case_id": "c3", "subject": "logic"},
    ]
    details = {
        str(i): {"origin_prediction": x, "predictions": x, "references": x}
        for i, x in [(0, "A"), (2, "C")]
    }
    adapter, handle = _adapter_with_tree(tmp_path, cases, details)
    rows, _ = adapter.collect(handle, {})
    assert [(r.stable_case_key, r.evidence_coverage["sample_id"]) for r in rows] == [
        ("c1", "logic-0"), ("c3", "logic-2"),
    ]

    # 游标只决定导入哪些记录，不改变身份：consumed=1 后行 2 仍归 c3。
    adapter2, handle2 = _adapter_with_tree(tmp_path / "again", cases, details)
    rows2, _ = adapter2.collect(handle2, {"records_consumed": 1})
    assert [(r.stable_case_key, r.evidence_coverage["sample_id"]) for r in rows2] == [
        ("c3", "logic-2"),
    ]


def test_r2_03_missing_first_row_keeps_not_attempted_and_scores_by_frozen_gold(tmp_path):
    # 缺首行（只有行 1）：c1 保持 not_attempted，行 1 归 c2。
    cases = [
        {"case_id": "c1", "subject": "logic"},
        {"case_id": "c2", "subject": "logic"},
    ]
    details = {"1": {"origin_prediction": "B", "predictions": "B", "references": "B"}}
    adapter, handle = _adapter_with_tree(tmp_path, cases, details)
    rows, _ = adapter.collect(handle, {})
    assert [(r.stable_case_key, r.evidence_coverage["sample_id"]) for r in rows] == [
        ("c2", "logic-1"),
    ]


def test_r2_03_multi_subject_and_out_of_range_index(tmp_path):
    cases = [
        {"case_id": "l-0", "subject": "logic"},
        {"case_id": "l-1", "subject": "logic"},
        {"case_id": "law-0", "subject": "law"},
    ]
    spec = ExternalJobSpec(
        run_id="run-r2", adapter_id="ceval-opencompass", adapter_version="1",
        runner_version="r", execution_config_hash="h", dataset_revision="rev",
        selected_case_ids=["l-0", "l-1", "law-0"],
        profile={"benchmark_id": "ceval", "benchmark_version": "1"},
        work_root=str(tmp_path), environment_digest="d",
        runner_config={"cases": cases},
    )
    adapter = CevalJobAdapter(argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "hang"})
    handle = adapter.prepare(spec)
    base = Path(handle.work_dir) / "outputs" / "results" / "mock-model"
    base.mkdir(parents=True)
    (base / "ceval-logic.json").write_text(json.dumps({
        "accuracy": 0.0,
        "details": {
            "1": {"origin_prediction": "A", "predictions": "A", "references": "A"},
            "7": {"origin_prediction": "B", "predictions": "B", "references": "B"},
        },
    }), encoding="utf-8")
    (base / "ceval-law.json").write_text(json.dumps({
        "accuracy": 0.0,
        "details": {"0": {"origin_prediction": "C", "predictions": "C", "references": "C"}},
    }), encoding="utf-8")
    rows, _ = adapter.collect(handle, {})
    mapped = {r.stable_case_key: r for r in rows if not r.error}
    unmapped = [r for r in rows if r.error]
    assert set(mapped) == {"l-1", "law-0"}  # 行 1→l-1；越界行 7 隔离
    assert len(unmapped) == 1 and unmapped[0].error["code"] == "EXTERNAL_CASE_MAPPING_UNKNOWN"
    assert unmapped[0].stable_case_key == "unmapped:logic-7"


# ------------------------------------------------------------------ R2-04


def test_r2_04_baseline_snapshot_uses_same_comparability_algorithm(tmp_path, monkeypatch):
    service, jobs, artifacts = _review_service(tmp_path, "opencompass_ok", monkeypatch)
    dataset_a = _dataset(LOGIC_ROWS[:2], revision="rev-A")
    inputs_a = _inputs(dataset_a)
    run_a = service.create_run(inputs_a["scenario_version"], inputs_a["manifest"], inputs_a["case_ids"])
    dataset_b = _dataset(LOGIC_ROWS[:2], revision="rev-B")
    inputs_b = _inputs(dataset_b)
    run_b = service.create_run(inputs_b["scenario_version"], inputs_b["manifest"], inputs_b["case_ids"])
    from motte_sdk.dispatcher import RunDispatcher

    finished_a = RunDispatcher(service).dispatch(run_a["id"])
    finished_b = RunDispatcher(service).dispatch(run_b["id"])
    comparisons = ComparisonService(service.store, baselines=service.store.baselines)

    # 普通 compare：不同 revision 不可比。
    report = comparisons.compare(run_a["id"], run_b["id"], allowed_factors=["model"])
    assert report.eligible is False

    # 快照自报 comparable=True 也不再被信任：Gate 走同一比较算法。
    snapshot = service.store.baselines.put({
        "id": "snap-r2-04",
        "run_id": run_a["id"],
        "scoring_pass_id": finished_a["current_scoring_pass_id"],
        "metrics": {"comparable": True, "reasons": [], "accuracy": 0.5},
    })
    gate = comparisons.evaluate_gate(
        run_b["id"],
        policy={"metric": "accuracy", "op": "gte", "threshold": 0.5,
                "require_comparable": True},
        baseline_snapshot_id=snapshot["id"],
    )
    assert gate["passed"] is False
    comparable_rule = next(r for r in gate["rules"] if r["id"] == "comparable")
    assert comparable_rule["passed"] is False
    assert "DATASET_REVISION_CHANGED" in comparable_rule["reason"]

    # 缺固定报告来源的快照：不能默认可比（绕过存储校验构造畸形快照）。
    class _MalformedBaselines:
        def get(self, snapshot_id):
            return {"id": snapshot_id, "run_id": "", "scoring_pass_id": "",
                    "metrics": {"comparable": True}}

    with pytest.raises(ComparisonError, match="BASELINE_INCOMPLETE"):
        ComparisonService(service.store, baselines=_MalformedBaselines()).evaluate_gate(
            finished_b["id"],
            policy={"metric": "accuracy", "op": "gte", "threshold": 0.0},
            baseline_snapshot_id="snap-incomplete",
        )


# ------------------------------------------------------------------ R2-05


def _manifest_for_compare(**overrides):
    manifest = {
        "model": "m-r2",
        "case_ids": ["logic-1"],
        "case_expectations": {"logic-1": "B"},
        "case_content_hashes": {"logic-1": "hash-v1"},
        "few_shot_hashes": [],
        "external_benchmark": {
            "adapter_id": "ceval-opencompass", "dataset_revision": "rev-same",
            "profile": {
                "benchmark_id": "ceval", "benchmark_version": "1",
                "split": "val", "few_shot": {"count": 0, "source_split": "dev"},
                "prompt_template_version": "p1", "answer_extractor": "first-option",
                "extractor_version": "1",
                "aggregation": "subject-macro-and-sample-weighted",
                "aggregation_version": "1", "seed": 7,
                "runner_version": "opencompass-0.4.2", "environment_digest": "d1",
            },
        },
        "evaluation": {"scorer_id": "external-mcq", "scorer_version": "external-mcq@1"},
    }
    manifest.update(overrides)
    return manifest


def _compare(base, cand):
    from motte_contracts.comparison import ComparisonPolicy, RunReportRef
    from motte_eval.comparison import compare_run_reports

    return compare_run_reports(
        RunReportRef(run_id="b", scoring_pass_id="p1", report_schema="r", evidence_hash="h"),
        RunReportRef(run_id="c", scoring_pass_id="p2", report_schema="r", evidence_hash="h"),
        baseline_manifest=base, candidate_manifest=cand,
        policy=ComparisonPolicy(allowed_factors=("model",)),
    )


def test_r2_05_same_revision_content_change_blocks_comparison():
    base = _manifest_for_compare()
    changed_question = _manifest_for_compare()
    changed_question["case_content_hashes"] = {"logic-1": "hash-v2"}
    result = _compare(base, changed_question)
    assert result.eligible is False
    assert any("CASE_CONTENT_CHANGED" in reason for reason in result.reasons)
    assert result.case_diff["changed"] == ["logic-1"]

    few_shot_changed = _manifest_for_compare(few_shot_hashes=["dev-hash-2"])
    result = _compare(base, few_shot_changed)
    assert result.eligible is False
    assert any("FEWSHOT_CONTENT_CHANGED" in reason for reason in result.reasons)

    one_sided = _manifest_for_compare()
    del one_sided["case_content_hashes"]
    result = _compare(base, one_sided)
    assert result.eligible is False
    assert any("IDENTITY_MISSING:case_content_hashes" in reason for reason in result.reasons)

    # 冻结内容相同（只换模型字段）仍可比：任务内容身份与合法变量分开。
    same_content = _manifest_for_compare()
    same_content["model"] = "m-other"
    assert _compare(base, same_content).eligible is True


def test_r2_05_run_inputs_freeze_content_hashes():
    dataset = _dataset(LOGIC_ROWS[:2])
    inputs = _inputs(dataset, case_ids=["logic-2", "logic-1"])
    manifest = inputs["manifest"]
    hashes = manifest["case_content_hashes"]
    assert set(hashes) == {"logic-1", "logic-2"}
    assert all(value for value in hashes.values())
    by_id = {entry.case_id: entry.row_sha256 for entry in dataset.manifest}
    assert hashes == {"logic-1": by_id["logic-1"], "logic-2": by_id["logic-2"]}


def test_r2_05_same_revision_rewrite_is_rejected(tmp_path):
    store = SQLiteBenchmarkDatasets(str(tmp_path / "runs.db"))
    catalog = BenchmarkCatalog(store)
    catalog.register("ceval")
    catalog.update_dataset("ceval", _dataset(LOGIC_ROWS, revision="rev-dup"))
    # 同内容幂等；不同内容拒绝。
    catalog.update_dataset("ceval", _dataset(LOGIC_ROWS, revision="rev-dup"))
    changed_rows = [_row("logic-1", "logic", "B", question="What is 100+100?")]
    with pytest.raises(ValueError, match="DATASET_REVISION_REUSED"):
        catalog.update_dataset("ceval", _dataset(changed_rows, revision="rev-dup"))
    # 换 revision 可写入。
    catalog.update_dataset("ceval", _dataset(changed_rows, revision="rev-dup-2"))


def test_r2_05_api_prepare_rejects_revision_reuse(tmp_path):
    from fastapi.testclient import TestClient
    from motte_storage.resource_store import InMemoryResourceStore

    from apps.api.app.main import create_app

    db = str(tmp_path / "runs.db")
    resources = InMemoryResourceStore()
    resources.models.put(_published_model())
    client = TestClient(create_app(store=SQLiteRunStore(db), resource_store=resources))

    def body(question):
        row = _row("logic-1", "logic", "B", question=question)
        row["split"] = "val"
        return {
            "files": {"data.jsonl": json.dumps(row, ensure_ascii=False)},
            "dataset_revision": "rev-api-dup",
        }

    assert client.post("/api/v1/benchmarks/external/ceval/prepare", json=body("What is 1+1?")).status_code == 200
    second = client.post("/api/v1/benchmarks/external/ceval/prepare", json=body("What is 100+100?"))
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "DATASET_REVISION_REUSED"
    # 同内容重放仍是 200（幂等）。
    assert client.post("/api/v1/benchmarks/external/ceval/prepare", json=body("What is 1+1?")).status_code == 200


# ------------------------------------------------------------------ R2-06


class _TamperingLegacyAdapter(ProcessJobAdapter):
    """旧协议 adapter：collect 读完后改写输出（R2-06 故障注入）。"""

    def collect(self, handle, cursor):
        results, collected = super().collect(handle, cursor)
        target = Path(handle.work_dir) / "results.json"
        if target.exists():
            target.write_text(json.dumps({"records": [
                {"case_id": "s-a:1", "status": "succeeded", "output": {"prediction": "D"}},
                {"case_id": "s-a:2", "status": "succeeded", "output": {"prediction": "D"}},
            ]}), encoding="utf-8")
        return results, collected


def test_r2_06_legacy_tamper_between_parse_and_freeze_fails_closed(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    adapter = _TamperingLegacyAdapter(argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "ok"})
    supervisor = ExternalJobSupervisor(adapter, poll_interval_seconds=0.05)
    jobs = SQLiteExternalJobs(str(tmp_path / "runs.db"))
    runner = DurableExternalJobRunner(
        supervisor, jobs, artifacts=artifacts, work_root=tmp_path / "jobs",
    )
    spec = ExternalJobSpec(
        run_id="run-r2-06", adapter_id="x", adapter_version="1",
        runner_version="r", execution_config_hash="h", dataset_revision="rev",
        selected_case_ids=["s-a:1", "s-a:2"],
        profile={"benchmark_id": "b", "benchmark_version": "1"},
        work_root=str(tmp_path), environment_digest="d",
        runner_config={"cases": [{"case_id": "s-a:1", "subject": "s"}]},
    )
    supervisor.intent_journal = runner._journal
    outcome = runner._settle(spec, supervisor.run(spec))
    # 解析后证据被改写：拒绝最终化，不产生"分数与证据不一致"的完成态。
    assert outcome["job_status"] == "failed"
    assert outcome["error"]["code"] == "EVIDENCE_INCONSISTENT"
    assert outcome["import"]["imported"] == 0


def test_r2_06_official_scores_bound_to_frozen_content_hash(tmp_path, monkeypatch):
    service, jobs, artifacts = _review_service(tmp_path, "opencompass_ok", monkeypatch)
    dataset = _dataset(LOGIC_ROWS)
    inputs = _inputs(dataset)
    run = service.create_run(inputs["scenario_version"], inputs["manifest"], inputs["case_ids"])
    from motte_sdk.dispatcher import RunDispatcher

    finished = RunDispatcher(service).dispatch(run["id"])
    assert finished["status"] == "completed"
    job = jobs.jobs_for_run(run["id"])[0]
    evidence = job["checkpoint"]["evidence"]
    assert evidence["frozen_before_parse"] is True and evidence["complete"] is True
    bundle_hash = evidence["raw_bundle_hash"]
    # 采集后改写工作目录：不影响已绑定的证据 hash / 已导入分数。
    work_dir = Path(job["handle"]["work_dir"])
    target = next(work_dir.rglob("ceval-logic.json"))
    tampered = json.loads(target.read_text())
    tampered["details"]["0"]["origin_prediction"] = "答案为 D"
    target.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    refreshed = jobs.get_job(job["job_id"])
    assert refreshed["checkpoint"]["evidence"]["raw_bundle_hash"] == bundle_hash
    bundle = json.loads(artifacts.read_bytes(evidence["raw_bundle_artifact"]))
    original = next(
        entry["content"] for rel, entry in bundle["files"].items()
        if "ceval-logic.json" in rel
    )
    assert "答案为 B" in original  # 冻结的是解析前字节，不是被改写后的


# ------------------------------------------------------------------ R2-07


def test_r2_07_recovery_after_work_dir_cleanup_keeps_evidence_refs(tmp_path, monkeypatch):
    import shutil

    service, jobs, artifacts = _review_service(tmp_path, "opencompass_ok", monkeypatch)
    dataset = _dataset(LOGIC_ROWS)
    inputs = _inputs(dataset)
    run = service.create_run(inputs["scenario_version"], inputs["manifest"], inputs["case_ids"])
    from motte_sdk.dispatcher import RunDispatcher

    RunDispatcher(service).dispatch(run["id"])
    job = jobs.jobs_for_run(run["id"])[0]
    evidence_before = job["checkpoint"]["evidence"]
    records_before = sorted(
        record["source_record_key"] for record in jobs.list_records(job["job_id"])
    )

    shutil.rmtree(Path(job["handle"]["work_dir"]))  # 工作目录被清理
    recovery_adapter = CevalJobAdapter(
        argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "opencompass_ok"},
    )
    supervisor = ExternalJobSupervisor(recovery_adapter, poll_interval_seconds=0.05)
    runner = DurableExternalJobRunner(
        supervisor, jobs, artifacts=artifacts, work_root=tmp_path / "jobs",
        parser_version="ceval-opencompass-parser@1",
    )
    runner.bind_service(service)
    replay = runner(service.store.runs.get(run["id"]))
    # 复用原 Artifact/指标/错误事实：证据引用与 hash 不变。
    assert replay["import"]["imported"] == 0
    assert replay["import"]["evidence"] == evidence_before
    assert jobs.get_job(job["job_id"])["checkpoint"]["evidence"] == evidence_before
    assert sorted(
        record["source_record_key"] for record in jobs.list_records(job["job_id"])
    ) == records_before
    # bundle 仍可从 Artifact 读回（不依赖工作目录）。
    bundle = json.loads(artifacts.read_bytes(evidence_before["raw_bundle_artifact"]))
    assert bundle["files"]


# ------------------------------------------------------------------ R2-09


def test_r2_09_default_max_wall_applies_to_opencompass_adapter(tmp_path):
    class Clock:
        def __init__(self):
            self.now = 1000.0

        def advance(self, seconds):
            self.now += seconds

        def __call__(self):
            return self.now

    clock = Clock()
    adapter = CevalJobAdapter(
        argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "hang"},
        default_limits={"poll_interval_seconds": 0.01, "max_wall_seconds": 3600.0},
    )
    spec = ExternalJobSpec(
        run_id="run-r2-09", adapter_id="ceval-opencompass", adapter_version="1",
        runner_version="r", execution_config_hash="h", dataset_revision="rev",
        selected_case_ids=["logic-1"],
        profile={"benchmark_id": "ceval", "benchmark_version": "1"},
        work_root=str(tmp_path), environment_digest="d",
        # 公开输入只带 poll_interval（review 复现条件）：默认超时仍须生效。
        limits={"poll_interval_seconds": 0.01},
        runner_config={"cases": [{"case_id": "logic-1", "subject": "logic"}]},
    )
    # 可控时钟 + 真 adapter：挂起进程在默认 3600s 预算内被 JOB_TIMEOUT 中断。
    real_sleep = time.sleep
    supervisor = ExternalJobSupervisor(
        adapter, poll_interval_seconds=0.01,
        sleep=lambda seconds: (real_sleep(min(seconds, 0.02)), clock.advance(1800))[1],
        monotonic=clock,
    )
    outcome = supervisor.run(spec)
    assert outcome["job_status"] == "failed"
    assert outcome["error"]["code"] == "JOB_TIMEOUT"
    assert outcome["error"]["details"]["max_wall_seconds"] == 3600.0
    pid = outcome["handle"]["owned_resources"]["pids"][0]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        real_sleep(0.05)
    else:
        pytest.fail("timed-out job process was not interrupted")


# ------------------------------------------------------------------ R2-10


def test_r2_10_preflight_binds_effective_output_budget():
    dataset = _dataset(LOGIC_ROWS)
    tiny = _published_model(
        context_window=32, max_output_tokens=8,
        parameters={"max_output_tokens": 8},
    )
    reasons = validate_external_run_request(
        dataset, benchmark_id="ceval", model_record=tiny, scope="custom-subset",
    )
    # Profile 默认 1024 超过模型声明上限 8（或参数 8 超窗口 32）都足以拒绝。
    assert any(
        reason.startswith("MODEL_LIMIT_EXCEEDED") for reason in reasons
    ) or any(
        reason.startswith("CONTEXT_WINDOW_INSUFFICIENT") for reason in reasons
    )

    ceiling_only = _published_model(max_output_tokens=8)
    reasons = validate_external_run_request(
        dataset, benchmark_id="ceval", model_record=ceiling_only, scope="custom-subset",
    )
    assert any(
        reason.startswith("MODEL_LIMIT_EXCEEDED:max_output_tokens 1024") for reason in reasons
    )

    fit = _published_model(parameters={"max_output_tokens": 128})
    reasons = validate_external_run_request(
        dataset, benchmark_id="ceval", model_record=fit, scope="custom-subset",
    )
    assert reasons == []


def test_r2_10_few_shot_template_overhead_counted():
    rows = LOGIC_ROWS + [
        _row("logic-d1", "logic", "A", question="很长的一段few-shot示例题干" * 10),
    ]
    dataset = prepare_external_dataset(
        files={"data.jsonl": "\n".join(
            json.dumps({**row, "split": split}, ensure_ascii=False)
            for row, split in [(r, "val") for r in LOGIC_ROWS] + [(rows[-1], "dev")]
        ).encode("utf-8")},
        dataset_revision="rev-r2-fs",
    )
    small_window = _published_model(
        context_window=256, parameters={"max_output_tokens": 128},
    )
    reasons = validate_external_run_request(
        dataset, benchmark_id="ceval", model_record=small_window,
        scope="custom-subset", few_shot=1,
    )
    assert any(
        reason.startswith("CONTEXT_WINDOW_INSUFFICIENT") for reason in reasons
    )
