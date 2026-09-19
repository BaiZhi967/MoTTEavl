"""M2 review（2026-09-20）16 项修复的验收测试。

逐项对应 docs/verification/M2-review-2026-09-20.md 的“修复验收”：
R01 Runner 可消费配置/受控加载、R03 冻结映射、R04 导入中断恢复、
R05 跨进程取消、R06 输出边界 symlink、R07/R08 指标与口径、
R09 证据链、R10 完成标记、R11 零结果、R12 固定 pass、R13 持久化、
R14 入队预检。贯穿性端到端样例见 test_m2_review_e2e_sample.py。
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from motte_benchmark.fake_runner import self_argv
from motte_benchmark.opencompass.adapter import CevalJobAdapter
from motte_benchmark.opencompass.parser import (
    CevalParserError,
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
from motte_sdk.execution_backends import external_job_spec_from_run
from motte_sdk.external_jobs import DurableExternalJobRunner, ExternalJobSupervisor
from motte_sdk.service import RunService
from motte_storage.artifacts import ArtifactStore
from motte_storage.external_jobs import SQLiteExternalJobs
from motte_storage.run_store import SQLiteRunStore
from motte_contracts.external_job import ExternalJobHandle, ExternalJobSpec


# ------------------------------------------------------------------ 共享夹具


def _row(row_id, subject, answer, question="Q?"):
    row = {
        "id": row_id, "subject": subject, "question": question,
        "A": "1", "B": "2", "C": "3", "D": "4",
    }
    if answer is not None:
        row["answer"] = answer
    return row


def _dataset(rows, revision="rev-review-1", split="val", benchmark_id="ceval"):
    content = "\n".join(
        json.dumps({**row, "split": split}, ensure_ascii=False) for row in rows
    ).encode("utf-8")
    return prepare_external_dataset(
        files={"data.jsonl": content},
        dataset_revision=revision,
        benchmark_id=benchmark_id,
        default_split=split,
    )


def _published_model(model="gpt-review"):
    return {
        "id": "m-review",
        "provider": "openai",
        "model": model,
        "parameters": {"temperature": 0.0},
        "lifecycle": "published",
        "context_window": 8192,
    }


def _inputs(dataset, **kwargs):
    return prepare_external_run_inputs(
        dataset,
        benchmark_id="ceval",
        model_id="m-review",
        model_record=_published_model(),
        **kwargs,
    )


def _spec_from_inputs(inputs, work_root):
    run = {
        "id": "run-review",
        "case_ids": inputs["case_ids"],
        "manifest": inputs["manifest"],
    }
    return external_job_spec_from_run(run, work_root=str(work_root))


LOGIC_ROWS = [
    _row("logic-1", "logic", "B"),
    _row("logic-2", "logic", "B"),
    _row("logic-3", "logic", "B"),
    _row("logic-4", "logic", "B"),
]


# ------------------------------------------------------------------ R01


def test_r01_runner_config_is_consumable_and_pinned():
    dataset = _dataset(LOGIC_ROWS)
    inputs = _inputs(
        dataset, case_ids=["logic-2", "logic-1"], scope="custom-subset",
        credentials={"api_key": {"ref": "env:MOTTE_TEST_KEY"}},
    )
    external = inputs["manifest"]["external_benchmark"]
    runner_config = external["runner_config"]
    # 逐题 prompt 按冻结选择顺序携带题干与选项（不依赖 sample_id 约定）。
    assert [case["case_id"] for case in runner_config["cases"]] == ["logic-2", "logic-1"]
    prompt = runner_config["cases"][0]["prompt"]
    assert "题目：Q?" in prompt and "A. 1" in prompt and "Answer:" in prompt
    # 模型快照 + allowlist + 凭据引用（不是原始值）。
    assert runner_config["model"]["model"] == "gpt-review"
    assert runner_config["model"]["parameters"] == {"temperature": 0.0}
    assert runner_config["credentials"] == {"api_key": "env:MOTTE_TEST_KEY"}
    assert runner_config["config_hash"].startswith("sha256:")
    # 平台侧 gold 冻结在 case_expectations（R03 权威来源）。
    assert inputs["manifest"]["case_expectations"] == {"logic-2": "B", "logic-1": "B"}


def test_r01_adapter_prepare_writes_runner_config_into_work_dir(tmp_path):
    dataset = _dataset(LOGIC_ROWS)
    inputs = _inputs(dataset)
    spec = _spec_from_inputs(inputs, tmp_path)
    adapter = CevalJobAdapter(argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "opencompass_ok"})
    handle = adapter.prepare(spec)
    written = json.loads(
        (Path(handle.work_dir) / "runner-config.json").read_text(encoding="utf-8"),
    )
    assert [case["case_id"] for case in written["cases"]] == [
        "logic-1", "logic-2", "logic-3", "logic-4",
    ]
    profile = json.loads(
        (Path(handle.work_dir) / "job-profile.json").read_text(encoding="utf-8"),
    )
    assert profile["benchmark_id"] == "ceval"


def test_r01_adapters_load_from_shared_controlled_config(tmp_path, monkeypatch):
    from motte_benchmark import registry
    from motte_benchmark.runner_config import (
        DEFAULT_WRAPPER,
        ensure_builtin_adapters,
        load_runner_adapter_config,
    )

    config_path = tmp_path / "adapters.json"
    config_path.write_text(json.dumps({"adapters": [
        {"benchmark": "ceval", "module": "motte_benchmark.fake_runner"},
        {"benchmark": "cmmlu", "module": "motte_benchmark.fake_runner"},
    ]}), encoding="utf-8")
    monkeypatch.setenv("MOTTE_RUNNER_CONFIG", str(config_path))
    try:
        registered = ensure_builtin_adapters(force=True)
        assert "ceval-opencompass" in registered and "cmmlu-opencompass" in registered
    finally:
        registry.unregister_adapter("ceval-opencompass")
        registry.unregister_adapter("cmmlu-opencompass")

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"adapters": [
        {"benchmark": "not-a-benchmark", "argv": ["x"]},
    ]}), encoding="utf-8")
    from motte_benchmark.protocol import BenchmarkRuntimeError

    with pytest.raises(BenchmarkRuntimeError, match="unknown benchmark"):
        load_runner_adapter_config(bad)

    # 无配置且默认 wrapper 不存在：不注册，RUNNER_NOT_CONNECTED 如实呈现。
    monkeypatch.setenv("MOTTE_RUNNER_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setattr("motte_benchmark.runner_config.DEFAULT_WRAPPER", str(tmp_path / "nope"))
    assert "ceval-opencompass" not in ensure_builtin_adapters(force=True)


def test_r01_default_wrapper_registers_when_deployed(tmp_path, monkeypatch):
    from motte_benchmark import registry
    from motte_benchmark import runner_config as rc

    wrapper = tmp_path / "opencompass-entry"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("MOTTE_RUNNER_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setattr(rc, "DEFAULT_WRAPPER", str(wrapper))
    try:
        registered = rc.ensure_builtin_adapters(force=True)
        assert {"ceval-opencompass", "cmmlu-opencompass"} <= set(registered)
    finally:
        registry.unregister_adapter("ceval-opencompass")
        registry.unregister_adapter("cmmlu-opencompass")


# ------------------------------------------------------------------ R13


def test_r13_prepare_state_survives_restart_and_second_instance(tmp_path):
    from apps.api.app.main import create_app
    from fastapi.testclient import TestClient
    from motte_storage.resource_store import InMemoryResourceStore

    db = str(tmp_path / "runs.db")
    resources = InMemoryResourceStore()
    resources.models.put(_published_model())
    body = {
        "files": {
            "data.jsonl": "\n".join(
                json.dumps({**row, "split": "val"}, ensure_ascii=False)
                for row in LOGIC_ROWS
            ),
        },
        "dataset_revision": "rev-restart-1",
    }
    app_a = TestClient(create_app(store=SQLiteRunStore(db), resource_store=resources))
    assert app_a.post("/api/v1/benchmarks/external/ceval/prepare", json=body).status_code == 200

    # 第二个 API 实例（同一存储）：catalog 仍 prepared、题目清单可查。
    app_b = TestClient(create_app(store=SQLiteRunStore(db), resource_store=resources))
    status = app_b.get("/api/v1/benchmarks/external/catalog").json()["benchmarks"][0]
    assert status["benchmark_id"] == "ceval"
    assert status["dataset"]["rows"] == 4
    cases = app_b.get("/api/v1/benchmarks/external/ceval/cases").json()
    assert cases["total"] == 4
    assert {case["case_id"] for case in cases["cases"]} <= {
        "logic-1", "logic-2", "logic-3", "logic-4",
    }


def test_r13_catalog_backed_by_dataset_store(tmp_path):
    from motte_storage.benchmark_datasets import SQLiteBenchmarkDatasets

    store = SQLiteBenchmarkDatasets(str(tmp_path / "runs.db"))
    catalog = BenchmarkCatalog(store)
    catalog.register("ceval")
    dataset = _dataset(LOGIC_ROWS, revision="rev-cat-1")
    catalog.update_dataset("ceval", dataset)

    fresh = BenchmarkCatalog(SQLiteBenchmarkDatasets(str(tmp_path / "runs.db")))
    fresh.register("ceval")
    restored = fresh.dataset("ceval")
    assert restored is not None and restored.row_count == 4
    assert restored.rows[0]["question"] == "Q?"
    assert fresh.status("ceval")["dataset"]["rows"] == 4


# ------------------------------------------------------------------ R14


def test_r14_creation_preflight_rejects_before_any_job(tmp_path):
    from apps.api.app.main import create_app
    from fastapi.testclient import TestClient
    from motte_storage.resource_store import InMemoryResourceStore

    db = str(tmp_path / "runs.db")
    resources = InMemoryResourceStore()
    resources.models.put({
        "id": "m-draft", "provider": "openai", "model": "gpt-draft",
        "lifecycle": "draft",
    })
    resources.models.put(_published_model())
    client = TestClient(create_app(store=SQLiteRunStore(db), resource_store=resources))
    body = {
        "files": {
            "data.jsonl": "\n".join(
                json.dumps({**row, "split": "val"}, ensure_ascii=False)
                for row in LOGIC_ROWS[:2]
            ),
        },
        "dataset_revision": "rev-preflight-1",
    }
    assert client.post("/api/v1/benchmarks/external/ceval/prepare", json=body).status_code == 200

    from motte_benchmark import registry
    from motte_benchmark.fake_runner import self_argv as _argv

    registry.register_adapter("ceval-opencompass", lambda: CevalJobAdapter(
        argv=_argv(), extra_env={"MOTTE_FAKE_MODE": "opencompass_ok"},
    ))
    try:
        cases = [
            ({"model": "m-draft"}, "MODEL_NOT_PUBLISHED"),
            ({"model": "m-review", "split": "test"}, "SPLIT_NOT_IN_DATASET"),
            ({"model": "m-review", "few_shot": 5}, "FEWSHOT_EXAMPLES_INSUFFICIENT"),
            ({"model": "m-review", "scope": "half"}, "SCOPE_INVALID"),
            (
                {"model": "m-review", "scope": "full", "case_ids": ["logic-1"]},
                "SCOPE_FULL_MISMATCH",
            ),
            ({"model": "m-review", "case_ids": ["logic-404"]}, "UNKNOWN_CASE_ID"),
        ]
        for payload, expected in cases:
            response = client.post("/api/v1/benchmarks/external/ceval/runs", json=payload)
            assert response.status_code == 422, payload
            error = response.json()["error"]
            assert error["code"] == "RUN_REQUEST_INVALID", payload
            assert any(expected in reason for reason in error["details"]["reasons"]), (
                payload, error["details"]["reasons"],
            )
        jobs = SQLiteExternalJobs(db)
        assert jobs.jobs_for_run("") == []
        # GET 预检与创建同一校验/同一输入：draft 模型同样给出原因。
        preflight = client.get(
            "/api/v1/benchmarks/external/ceval/preflight",
            params={"model": "m-draft"},
        ).json()
        assert preflight["ok"] is False
        assert any("MODEL_NOT_PUBLISHED" in reason for reason in preflight["reasons"])
    finally:
        registry.unregister_adapter("ceval-opencompass")


def test_r14_validate_request_checks_context_budget():
    dataset = _dataset(LOGIC_ROWS)
    tiny = dict(_published_model())
    tiny["context_window"] = 3
    reasons = validate_external_run_request(
        dataset, benchmark_id="ceval", model_record=tiny, scope="custom-subset",
    )
    assert any(reason.startswith("CONTEXT_WINDOW_INSUFFICIENT") for reason in reasons)


# ------------------------------------------------------------------ R03


def _adapter_with_tree(tmp_path, cases, details_by_position, dataset="ceval"):
    """构造受控工作目录 + 冻结配置 + OpenCompass 输出树，然后 collect。"""
    adapter = CevalJobAdapter(argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "hang"})
    spec = ExternalJobSpec(
        run_id="run-map", adapter_id="ceval-opencompass", adapter_version="1",
        runner_version="r", execution_config_hash="h", dataset_revision="rev",
        selected_case_ids=[case["case_id"] for case in cases],
        profile={"benchmark_id": "ceval", "benchmark_version": "1"},
        work_root=str(tmp_path), environment_digest="d",
        runner_config={"cases": cases},
    )
    handle = adapter.prepare(spec)
    results_dir = Path(handle.work_dir) / "outputs" / "results" / "mock-model"
    results_dir.mkdir(parents=True)
    (results_dir / f"{dataset}-logic.json").write_text(
        json.dumps({"accuracy": 50.0, "details": details_by_position}, ensure_ascii=False),
        encoding="utf-8",
    )
    return adapter, handle


def test_r03_runner_row_index_maps_to_frozen_case_ids(tmp_path):
    # 任意形状的 Case ID（不是 subject-idx），选择顺序即映射顺序。
    cases = [
        {"case_id": "q-nine", "subject": "logic"},
        {"case_id": "q-one", "subject": "logic"},
    ]
    details = {
        "0": {"origin_prediction": "答案为 B", "predictions": "B", "references": "B"},
        "1": {"origin_prediction": "答案为 A", "predictions": "A", "references": "A"},
    }
    adapter, handle = _adapter_with_tree(tmp_path, cases, details)
    results, cursor = adapter.collect(handle, {})
    by_key = {result.stable_case_key: result for result in results}
    # 行 0 → q-nine（答对），行 1 → q-one（答错）；绝不生成 logic-0/logic-1。
    assert set(by_key) == {"q-nine", "q-one"}
    assert by_key["q-nine"].output["prediction"] == "B"
    assert by_key["q-one"].output["prediction"] == "A"
    assert cursor["records_consumed"] == 2


def test_r03_extra_and_unknown_rows_are_quarantined(tmp_path):
    cases = [{"case_id": "q-1", "subject": "logic"}]
    details = {
        "0": {"origin_prediction": "答案为 B", "predictions": "B", "references": "B"},
        "1": {"origin_prediction": "答案为 A", "predictions": "A", "references": "A"},
        "2": {"origin_prediction": "答案为 C", "predictions": "C", "references": "C"},
    }
    adapter, handle = _adapter_with_tree(tmp_path, cases, details)
    results, _ = adapter.collect(handle, {})
    mapped = [result for result in results if not result.error]
    quarantined = [result for result in results if result.error]
    assert [result.source_case_id for result in mapped] == ["q-1"]
    assert len(quarantined) == 2
    assert all(
        result.error["code"] == "EXTERNAL_CASE_MAPPING_UNKNOWN" for result in quarantined
    )
    assert all(result.stable_case_key.startswith("unmapped:") for result in quarantined)


def test_r03_missing_frozen_config_refuses_collect(tmp_path):
    adapter = CevalJobAdapter(argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "hang"})
    spec = ExternalJobSpec(
        run_id="run-map", adapter_id="ceval-opencompass", adapter_version="1",
        runner_version="r", execution_config_hash="h", dataset_revision="rev",
        selected_case_ids=["q-1"],
        profile={"benchmark_id": "ceval", "benchmark_version": "1"},
        work_root=str(tmp_path), environment_digest="d",
    )
    handle = adapter.prepare(spec)
    results_dir = Path(handle.work_dir) / "outputs" / "results" / "mock-model"
    results_dir.mkdir(parents=True)
    (results_dir / "ceval-logic.json").write_text(
        json.dumps({"accuracy": 50.0, "details": {
            "0": {"origin_prediction": "B", "predictions": "B", "references": "B"},
        }}), encoding="utf-8",
    )
    (Path(handle.work_dir) / "runner-config.json").unlink()
    with pytest.raises(ValueError, match="frozen case mapping"):
        adapter.collect(handle, {})


# ------------------------------------------------------------------ R04


class _FlakyImports:
    """在指定次数的 import 后抛错，模拟导入中途崩溃（review R04）。"""

    def __init__(self, store, fail_on):
        self._store = store
        self._fail_on = fail_on
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self._store, name)

    def import_record(self, *args, **kwargs):
        self.calls += 1
        if self.calls == self._fail_on:
            raise OSError("simulated crash during import")
        return self._store.import_record(*args, **kwargs)


def _review_service(tmp_path, mode, monkeypatch):
    """走真实分派路径：registry adapter + ARTIFACT_ROOT/MOTTE_JOB_WORK_ROOT 指向 tmp。"""
    from motte_benchmark import registry

    store = SQLiteRunStore(tmp_path / "runs.db")
    service = RunService(store)
    jobs = SQLiteExternalJobs(str(tmp_path / "runs.db"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MOTTE_JOB_WORK_ROOT", str(tmp_path / "jobs"))
    registry.register_adapter("ceval-opencompass", lambda: CevalJobAdapter(
        argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": mode},
    ), replace=True)
    return service, jobs, artifacts


@pytest.mark.parametrize("fail_on", [1, 2])
def test_r04_import_crash_recovers_without_second_start(tmp_path, fail_on, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MOTTE_JOB_WORK_ROOT", str(tmp_path / "jobs"))
    from motte_benchmark import registry
    from motte_sdk.dispatcher import RunDispatcher

    registry.register_adapter("ceval-opencompass", lambda: CevalJobAdapter(
        argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "opencompass_ok"},
    ), replace=True)
    try:
        dataset = _dataset(LOGIC_ROWS)
        inputs = _inputs(dataset)
        store = SQLiteRunStore(tmp_path / "runs.db")
        real_jobs = SQLiteExternalJobs(str(tmp_path / "runs.db"))
        store.external_jobs = _FlakyImports(real_jobs, fail_on)
        service = RunService(store)
        run = service.create_run(
            inputs["scenario_version"], inputs["manifest"], inputs["case_ids"],
        )

        # 第一次分派在导入中途崩溃：Run 失败，Job 未写终态（导入在终态之前）。
        first = RunDispatcher(service).dispatch(run["id"])
        assert first["status"] in {"failed", "quarantined"}
        job_rows = real_jobs.jobs_for_run(run["id"])
        assert len(job_rows) == 1
        outcome_before = sorted(
            path.name for path in (tmp_path / "artifacts").rglob("outcome-*.json")
        )

        # 恢复（重派）：重新采集 + 幂等补齐；Job 仍只有一个（不二次启动）。
        recovery_adapter = CevalJobAdapter(
            argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "opencompass_ok"},
        )
        supervisor = ExternalJobSupervisor(recovery_adapter, poll_interval_seconds=0.05)
        runner = DurableExternalJobRunner(
            supervisor, real_jobs, artifacts=ArtifactStore(tmp_path / "artifacts"),
            work_root=tmp_path / "jobs", parser_version="ceval-opencompass-parser@1",
        )
        runner.bind_service(service)
        recovered = runner(service.store.runs.get(run["id"]))
        assert recovered["job_status"] in {"settled", "failed"}
        # opencompass_ok 产出 2 行（logic-1/logic-2）：崩溃后恢复完整补齐。
        records = real_jobs.list_records(job_rows[0]["job_id"])
        assert sorted(record["source_record_key"] for record in records) == [
            "logic-1", "logic-2",
        ]
        assert recovered["import"]["conflicts"] == []
        assert len(real_jobs.jobs_for_run(run["id"])) == 1
        # 原始完整证据未被截断版本覆盖（内容寻址工件仍在）。
        outcome_after = sorted(
            path.name for path in (tmp_path / "artifacts").rglob("outcome-*.json")
        )
        assert set(outcome_before) <= set(outcome_after)
    finally:
        registry.unregister_adapter("ceval-opencompass")


def test_r04_checkpoint_records_import_completion(tmp_path, monkeypatch):
    dataset = _dataset(LOGIC_ROWS)
    service, jobs, artifacts = _review_service(tmp_path, "opencompass_ok", monkeypatch)
    inputs = _inputs(dataset)
    run = service.create_run(
        inputs["scenario_version"], inputs["manifest"], inputs["case_ids"],
    )
    from motte_sdk.dispatcher import RunDispatcher

    RunDispatcher(service).dispatch(run["id"])
    job = jobs.jobs_for_run(run["id"])[0]
    assert job["checkpoint"]["import_completed"] is True
    assert job["checkpoint"]["cursor"]["ceval_native"]
    assert job["checkpoint"]["evidence"]["outcome_artifact"]
    assert job["checkpoint"]["evidence"]["raw_bundle_artifact"]


# ------------------------------------------------------------------ R05


def test_r05_cross_instance_cancel_interrupts_hanging_job(tmp_path, monkeypatch):
    dataset = _dataset(LOGIC_ROWS)
    service, jobs, artifacts = _review_service(tmp_path, "hang", monkeypatch)
    inputs = _inputs(dataset)
    run = service.create_run(
        inputs["scenario_version"], inputs["manifest"], inputs["case_ids"],
    )

    # 第二个 RunService 实例（同一存储）：API/Worker 分进程形态。
    other = RunService(SQLiteRunStore(str(tmp_path / "runs.db")))

    from motte_sdk.dispatcher import RunDispatcher

    worker_result: dict = {}

    def _work():
        worker_result["finished"] = RunDispatcher(service).dispatch(run["id"])

    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    pid = None
    while time.monotonic() < deadline:
        recoverable = jobs.recoverable_for_run(run["id"])
        if recoverable and recoverable[0].get("handle", {}).get("owned_resources", {}).get("pids"):
            pid = recoverable[0]["handle"]["owned_resources"]["pids"][0]
            break
        time.sleep(0.05)
    assert pid is not None, "job should become active with a live pid"

    cancelled_view = other.cancel(run["id"], reason="operator-from-another-process")
    # 另一实例只落持久取消请求（返回仍是运行中）；Worker 的观察循环消费它。
    assert cancelled_view["status"] in {"running", "cancelled"}
    worker.join(timeout=20)
    assert worker_result.get("finished", {}).get("status") == "cancelled"
    final = other.get_run(run["id"])
    assert final["status"] == "cancelled"
    assert jobs.jobs_for_run(run["id"])[0]["status"] == "cancelled"

    stopped = time.monotonic() + 5
    while time.monotonic() < stopped:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("cancelled job process was not interrupted in bounded time")


def test_r05_default_max_wall_bounds_hanging_jobs():
    from motte_benchmark.protocol import DEFAULT_JOB_LIMITS

    # 公开创建路径不声明 max_wall_seconds 时也有有限上界（review R05）。
    assert isinstance(DEFAULT_JOB_LIMITS["max_wall_seconds"], (int, float))
    assert DEFAULT_JOB_LIMITS["max_wall_seconds"] > 0


# ------------------------------------------------------------------ R06


def test_r06_outputs_root_symlink_cannot_become_trusted_root(tmp_path):
    work = tmp_path / "job"
    work.mkdir()
    outside = tmp_path / "outside"
    (outside / "results" / "mock-model").mkdir(parents=True)
    (outside / "results" / "mock-model" / "ceval-logic.json").write_text(
        json.dumps({"accuracy": 100.0, "details": {
            "0": {"origin_prediction": "B", "predictions": "B", "references": "B"},
        }}), encoding="utf-8",
    )
    # outputs 本身是指向根外目录的 symlink：不得被重新认定为安全根。
    (work / "outputs").symlink_to(outside)
    with pytest.raises(CevalParserError, match="path-escape"):
        parse_opencompass_results(
            work / "outputs", dataset="ceval", trusted_root=work,
        )


def test_r06_parent_chain_symlink_inside_boundary_rejected(tmp_path):
    work = tmp_path / "job"
    (work / "outputs" / "results").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "ceval-law.json").write_text(
        json.dumps({"accuracy": 100.0}), encoding="utf-8",
    )
    (work / "outputs" / "results" / "mock-model").mkdir()
    (work / "outputs" / "results" / "mock-model" / "ceval-law.json").symlink_to(
        target / "ceval-law.json",
    )
    (work / "outputs" / "results" / "mock-model" / "ceval-logic.json").write_text(
        json.dumps({"accuracy": 100.0, "details": {
            "0": {"origin_prediction": "B", "predictions": "B", "references": "B"},
        }}), encoding="utf-8",
    )
    with pytest.raises(CevalParserError, match="path-escape"):
        parse_opencompass_results(
            work / "outputs", dataset="ceval", trusted_root=work,
        )


def test_r06_relative_work_root_stays_inside_boundary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    work = Path("job")
    (work / "outputs" / "results" / "mock-model").mkdir(parents=True)
    (work / "outputs" / "results" / "mock-model" / "ceval-logic.json").write_text(
        json.dumps({"accuracy": 100.0, "details": {
            "0": {"origin_prediction": "答案为 B", "predictions": "B", "references": "B"},
        }}), encoding="utf-8",
    )
    parsed = parse_opencompass_results(
        Path("job") / "outputs", dataset="ceval", trusted_root=Path("job"),
    )
    assert parsed["samples"][0]["prediction"] == "B"


# ------------------------------------------------------------------ R07


def test_r07_gate_resolves_metric_identity_not_always_accuracy():
    from motte_eval.gates import evaluate_gate

    candidate = {
        "metric_value": 1.0,  # accuracy 1.0（若按旧实现，任何指标都会拿它比较）
        "metric_values": {"accuracy": 1.0, "cost.total_usd": None},
        "coverage": 1.0,
        "cost_passable": False,
    }
    # 费用指标 + 费用未知：自动不足证据，不需要调用方记得设 require_cost_known。
    cost_gate = evaluate_gate(
        {"metric": "cost.total_usd", "op": "lte", "threshold": 2, "required_coverage": 1.0},
        candidate,
    )
    assert cost_gate["passed"] is False
    reason = next(rule for rule in cost_gate["rules"] if rule["id"] == "metric_threshold")
    assert "cost unknown" in reason["reason"]

    # 未知指标：明确失败，不退回 accuracy。
    unknown = evaluate_gate(
        {"metric": "not_a_metric", "op": "lte", "threshold": 2, "required_coverage": 1.0},
        candidate,
    )
    assert unknown["passed"] is False
    reason = next(rule for rule in unknown["rules"] if rule["id"] == "metric_threshold")
    assert "unsupported metric" in reason["reason"]

    # accuracy 语义不受影响（方向 gte/lte 仍由 policy 决定）。
    acc = evaluate_gate(
        {"metric": "accuracy", "op": "gte", "threshold": 0.5, "required_coverage": 1.0},
        candidate,
    )
    assert acc["passed"] is True
    assert acc["metric_id"] == "accuracy"


# ------------------------------------------------------------------ R08


def _manifest_for_compare(**profile_overrides):
    base_profile = {
        "benchmark_id": "ceval", "benchmark_version": "1",
        "split": "val", "few_shot": {"count": 0, "source_split": "dev"},
        "prompt_template_version": "p1", "answer_extractor": "first-option",
        "extractor_version": "1",
        "aggregation": "subject-macro-and-sample-weighted",
        "aggregation_version": "1", "seed": 7,
        "runner_version": "opencompass-0.4.2", "environment_digest": "d1",
    }
    profile = {**base_profile, **profile_overrides}
    return {
        "model": "m1",
        "case_ids": ["c1", "c2"],
        "case_expectations": {"c1": "A", "c2": "B"},
        "external_benchmark": {
            "adapter_id": "ceval-opencompass", "dataset_revision": "rev-1",
            "profile": profile,
        },
        "evaluation": {"scorer_id": "external-mcq", "scorer_version": "external-mcq@1"},
    }


def _compare(base, cand):
    from motte_contracts.comparison import ComparisonPolicy, RunReportRef
    from motte_eval.comparison import compare_run_reports

    return compare_run_reports(
        RunReportRef(run_id="b", scoring_pass_id="p1", report_schema="report-v1", evidence_hash="h"),
        RunReportRef(run_id="c", scoring_pass_id="p2", report_schema="report-v1", evidence_hash="h"),
        baseline_manifest=base, candidate_manifest=cand,
        policy=ComparisonPolicy(allowed_factors=("model",)),
    )


def test_r08_each_forbidden_change_blocks_eligibility():
    base = _manifest_for_compare()
    # 模型是允许变量：单独换模型仍可比。
    model_changed = dict(base)
    model_changed["model"] = "m2"
    assert _compare(base, model_changed).eligible is True

    forbidden = [
        ("few_shot", {"count": 5, "source_split": "dev"}),
        ("split", "test"),
        ("aggregation", "micro-only"),
        ("aggregation_version", "2"),
        ("runner_version", "opencompass-0.5.0"),
        ("environment_digest", "d2"),
        ("seed", 8),
        ("benchmark_version", "2"),
    ]
    for field, value in forbidden:
        candidate = _manifest_for_compare(**{field: value})
        result = _compare(base, candidate)
        assert result.eligible is False, field
        assert any(field in reason for reason in result.reasons), field


def test_r08_missing_identity_on_one_side_blocks():
    base = _manifest_for_compare()
    one_sided = _manifest_for_compare()
    del one_sided["external_benchmark"]["profile"]["runner_version"]
    result = _compare(base, one_sided)
    assert result.eligible is False
    assert any("IDENTITY_MISSING:runner_version" in reason for reason in result.reasons)


def test_r08_frozen_expectation_change_blocks():
    base = _manifest_for_compare()
    candidate = _manifest_for_compare()
    candidate["case_expectations"] = {"c1": "A", "c2": "C"}
    result = _compare(base, candidate)
    assert result.eligible is False
    assert any("CASE_SET_CHANGED" in reason for reason in result.reasons)
    assert result.case_diff["changed"] == ["c2"]


# ------------------------------------------------------------------ R09


def test_r09_evidence_chain_survives_work_dir_cleanup(tmp_path, monkeypatch):
    dataset = _dataset(LOGIC_ROWS)
    service, jobs, artifacts = _review_service(tmp_path, "opencompass_ok", monkeypatch)
    inputs = _inputs(dataset)
    run = service.create_run(
        inputs["scenario_version"], inputs["manifest"], inputs["case_ids"],
    )
    from motte_sdk.dispatcher import RunDispatcher

    finished = RunDispatcher(service).dispatch(run["id"])
    job = jobs.jobs_for_run(run["id"])[0]
    evidence = job["checkpoint"]["evidence"]

    # 原始输出 bundle 落受控不可变工件，包含可重新解析的内容。
    bundle = json.loads(artifacts.read_bytes(evidence["raw_bundle_artifact"]))
    files = bundle["files"]
    assert any("ceval-logic.json" in rel for rel in files)
    content_entry = next(
        entry for rel, entry in files.items() if "ceval-logic.json" in rel
    )
    assert content_entry["sha256"] and content_entry["content"]

    # 清掉可变工作目录后，仍可从冻结证据重新解析出同样本。
    import shutil

    work_dir = Path(job["handle"]["work_dir"])
    shutil.rmtree(work_dir)
    rebuilt = tmp_path / "reparse"
    for rel, entry in files.items():
        target = rebuilt / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry["content"], encoding="utf-8")
    parsed = parse_opencompass_results(
        rebuilt / "outputs", dataset="ceval", trusted_root=rebuilt,
    )
    assert [sample["prediction"] for sample in parsed["samples"]] == ["B", "A"]

    # native/diagnostic 进入版本化指标事实：run 事件 + scoring pass summary。
    events = [event["type"] for event in service.events(run["id"])]
    assert "external_job_metrics" in events
    summary = finished["scoring_pass"]["summary"]
    assert summary["external_job_metrics"]["native"]["ceval_logic/accuracy"] == 0.5


# ------------------------------------------------------------------ R10


def _run_and_forget(tmp_path, mode):
    adapter = ProcessJobAdapter(argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": mode})
    spec = ExternalJobSpec(
        run_id="run-r10", adapter_id="x", adapter_version="1",
        runner_version="r", execution_config_hash="h", dataset_revision="rev",
        selected_case_ids=["s-a:1"],
        profile={"benchmark_id": "b", "benchmark_version": "1"},
        work_root=str(tmp_path), environment_digest="d",
        runner_config={"cases": [{"case_id": "s-a:1", "subject": "s"}]},
    )
    handle = adapter.prepare(spec)
    from motte_contracts.external_job import ExternalJobStatus, new_launch_token

    launching = handle.model_copy(update={
        "launch_token": new_launch_token(), "status": ExternalJobStatus.launching,
    })
    started = adapter.start(spec, launching)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        started = adapter.poll(started)
        if str(started.status) != "ExternalJobStatus.active":
            break
        time.sleep(0.05)
    return started


def test_r10_partial_output_without_marker_stays_indeterminate(tmp_path):
    handle = _run_and_forget(tmp_path, "partial_quiet")
    assert (Path(handle.work_dir) / "results.json").exists()
    # 进程不可核验（新 adapter 实例）+ 无完成标记：不确定，不升级成功。
    fresh = ProcessJobAdapter(argv=self_argv())
    polled = fresh.poll(handle)
    assert str(polled.status) == "ExternalJobStatus.indeterminate"


def test_r10_marker_carries_trusted_exit_code(tmp_path):
    handle = _run_and_forget(tmp_path, "ok")
    fresh = ProcessJobAdapter(argv=self_argv())
    polled = fresh.poll(handle)
    assert str(polled.status) == "ExternalJobStatus.settled"
    assert polled.owned_resources["exit_code"] == 0
    assert polled.launch_identity["observed_via"] == "completion-marker"


# ------------------------------------------------------------------ R11


def test_r11_zero_results_cannot_finalize_completed(tmp_path):
    from motte_sdk.dispatcher import RunDispatcher
    from motte_sdk.execution_backends import (
        ExecutionBackendSpec,
        ExecutionHandle,
        register_backend,
        unregister_backend,
        validate_external_job_manifest,
    )

    def build(run):
        def run_job(dispatched):
            return {"job_status": "settled", "results": [], "error": None,
                    "handle": {"job_id": "job-r11"}}

        return ExecutionHandle(
            backend_id="fake-empty", backend_version="1", execution_mode="job",
            run_job=run_job,
        )

    register_backend(ExecutionBackendSpec(
        id="fake-empty", version="1", validate=validate_external_job_manifest,
        build=build, capabilities={"interactive": False, "safe_to_repeat": False},
        execution_mode="job",
    ))
    try:
        service = RunService(SQLiteRunStore(tmp_path / "runs.db"))
        dataset = _dataset(LOGIC_ROWS)
        inputs = _inputs(dataset)
        manifest = {**inputs["manifest"], "execution": {
            "backend_id": "fake-empty", "backend_version": "1",
        }}
        run = service.create_run(inputs["scenario_version"], manifest, inputs["case_ids"])
        finished = RunDispatcher(service).dispatch(run["id"])
        assert finished["status"] == "failed"
        assert finished["error"]["code"] == "EXTERNAL_EMPTY_RESULTS"
        by_case = {row["case_id"]: row["outcome"] for row in finished["cases"]}
        assert set(by_case.values()) == {"not_attempted"}
    finally:
        unregister_backend("fake-empty", "1")


# ------------------------------------------------------------------ R12


def test_r12_gate_pins_scoring_pass_and_does_not_drift_after_rescore(tmp_path, monkeypatch):
    dataset = _dataset(LOGIC_ROWS[:2])
    service, jobs, artifacts = _review_service(tmp_path, "opencompass_ok", monkeypatch)
    inputs = _inputs(dataset)
    run = service.create_run(
        inputs["scenario_version"], inputs["manifest"], inputs["case_ids"],
    )
    from motte_sdk.dispatcher import RunDispatcher

    finished = RunDispatcher(service).dispatch(run["id"])
    assert finished["status"] == "completed"
    comparisons = ComparisonService(service.store, baselines=service.store.baselines)

    first_pass = finished["current_scoring_pass_id"]
    gate_before = comparisons.evaluate_gate(
        run["id"],
        policy={"metric": "accuracy", "op": "gte", "threshold": 0.5,
                "required_coverage": 1.0},
        scoring_pass_id=first_pass,
    )
    assert gate_before["passed"] is True
    assert gate_before["report_refs"]["candidate"]["scoring_pass_id"] == first_pass

    # rescore 产生新 pass（相同输入 → 相同结论）；再以人工固定 pass 模拟
    # 修正后的评分变化：旧 pass 引用与结论不漂移，新 pass 反映新事实。
    rescored = service.rescore(run["id"])
    second_pass = rescored["current_scoring_pass_id"]
    assert second_pass != first_pass
    corrected = service._append_scoring_pass(
        run["id"],
        [{"case_id": "logic-1", "passed": False}, {"case_id": "logic-2", "passed": False}],
        source="manual-fixation",
        skip_aggregate=True,
    )
    third_pass = corrected["id"]
    gate_old = comparisons.evaluate_gate(
        run["id"],
        policy={"metric": "accuracy", "op": "gte", "threshold": 0.5,
                "required_coverage": 1.0},
        scoring_pass_id=first_pass,
    )
    assert gate_old["passed"] is True and gate_old["conclusion_hash"] == gate_before["conclusion_hash"]
    gate_second = comparisons.evaluate_gate(
        run["id"],
        policy={"metric": "accuracy", "op": "gte", "threshold": 0.5,
                "required_coverage": 1.0},
        scoring_pass_id=second_pass,
    )
    assert gate_second["passed"] is True
    gate_new = comparisons.evaluate_gate(
        run["id"],
        policy={"metric": "accuracy", "op": "gte", "threshold": 0.5,
                "required_coverage": 1.0},
        scoring_pass_id=third_pass,
    )
    assert gate_new["passed"] is False


def test_r12_unfixed_report_cannot_gate(tmp_path):
    dataset = _dataset(LOGIC_ROWS)
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"))
    inputs = _inputs(dataset)
    run = service.create_run(inputs["scenario_version"], inputs["manifest"], inputs["case_ids"])
    comparisons = ComparisonService(service.store)
    # queued + 无 ScoringPass：一条原始正确预测不能冒充已固定报告。
    service.store.case_runs.upsert({
        "run_id": run["id"], "case_id": "logic-1",
        "outcome": "responded", "result": {"prediction": "B", "gold": "B"},
    })
    with pytest.raises(ComparisonError) as raised:
        comparisons.evaluate_gate(
            run["id"], policy={"metric": "accuracy", "op": "gte", "threshold": 0.5},
        )
    assert raised.value.code in {"NO_SCORING_EVIDENCE", "RUN_NOT_TERMINAL"}


def test_r12_baseline_snapshot_is_pinned(tmp_path, monkeypatch):
    dataset = _dataset(LOGIC_ROWS[:2])
    service, jobs, artifacts = _review_service(tmp_path, "opencompass_ok", monkeypatch)
    inputs = _inputs(dataset)
    run = service.create_run(
        inputs["scenario_version"], inputs["manifest"], inputs["case_ids"],
    )
    from motte_sdk.dispatcher import RunDispatcher

    finished = RunDispatcher(service).dispatch(run["id"])
    comparisons = ComparisonService(service.store, baselines=service.store.baselines)
    snapshot = service.store.baselines.put({
        "id": "baseline-1",
        "run_id": run["id"],
        "scoring_pass_id": finished["current_scoring_pass_id"],
        "metrics": {"comparable": True, "reasons": [], "accuracy": 0.5},
    })
    gate = comparisons.evaluate_gate(
        run["id"],
        policy={"metric": "accuracy", "op": "gte", "threshold": 0.5,
                "require_comparable": True},
        baseline_snapshot_id=snapshot["id"],
    )
    assert gate["passed"] is True
    assert gate["report_refs"]["baseline"]["baseline_snapshot_id"] == "baseline-1"
    with pytest.raises(ComparisonError, match="baseline snapshot nope not found"):
        comparisons.evaluate_gate(
            run["id"], policy={"metric": "accuracy", "op": "gte", "threshold": 0.5},
            baseline_snapshot_id="nope",
        )
