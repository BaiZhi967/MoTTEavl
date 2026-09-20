"""M2 第三轮 review（2026-09-20）11 项修复的验收测试。

逐项对应 docs/verification/M2-review-round3-2026-09-20.md 的"修复验收"：
R3-01 可执行配置、R3-02 真实题目/few-shot 导出、R3-03 argv 身份、
R3-04 fd 锚定读取、R3-05 证据白名单、R3-06 冻结映射、R3-07 工件恢复、
R3-08 超预算拒绝、R3-09 实验指针、R3-10 逐题预算、R3-11 revision 事务
不可变。真实固定 Runner + 本地确定性端点仍按 not_run 记录。
"""
import ast
import builtins
import json
import os
import threading
import time
from pathlib import Path

import pytest

from motte_benchmark.fake_runner import self_argv
from motte_benchmark.opencompass.adapter import CevalJobAdapter
from motte_benchmark.opencompass.parser import PARSER_VERSION
from motte_benchmark.opencompass.entry import (
    export_subject_files,
    main as entry_main,
    render_opencompass_config_source,
)
from motte_sdk.benchmark_catalog import (
    BenchmarkCatalog,
    prepare_external_dataset,
    prepare_external_run_inputs,
    validate_external_run_request,
)
from motte_sdk.external_jobs import DurableExternalJobRunner, ExternalJobSupervisor
from motte_sdk.service import RunService
from motte_storage.artifacts import ArtifactStore
from motte_storage.benchmark_datasets import (
    MemoryBenchmarkDatasets,
    RevisionConflictError,
    SQLiteBenchmarkDatasets,
)
from motte_storage.external_jobs import SQLiteExternalJobs
from motte_storage.run_store import SQLiteRunStore
from motte_contracts.external_job import ExternalJobSpec


def _row(row_id, subject, answer, question="Q?", split="val"):
    row = {
        "id": row_id, "subject": subject, "question": question,
        "A": "1", "B": "2", "C": "3", "D": "4", "split": split,
    }
    if answer is not None:
        row["answer"] = answer
    return row


def _dataset(rows, revision="rev-r3-1"):
    content = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8")
    return prepare_external_dataset(files={"data.jsonl": content}, dataset_revision=revision)


def _published_model(**overrides):
    return {
        "id": "m-r3", "provider": "openai", "model": "gpt-r3",
        "parameters": {}, "lifecycle": "published", "context_window": 8192,
        **overrides,
    }


def _inputs(dataset, **kwargs):
    return prepare_external_run_inputs(
        dataset, benchmark_id="ceval", model_id="m-r3",
        model_record=_published_model(), **kwargs,
    )


LOGIC_ROWS = [_row(f"logic-{i}", "logic", "B") for i in range(1, 5)]


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


# ------------------------------------------------------------------ R3-01


def _assert_names_resolve(source: str) -> None:
    """静态作用域检查（等价于 review 的 NameError 复现，无动态执行）。

    收集模块内 import/赋值/类与函数定义/内置名，再遍历全部名字读取：
    未解析名 → 与真实执行同类的 ``NameError: name ... is not defined``。
    """
    tree = ast.parse(source)
    defined = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            assert node.id in defined, (
                f"generated config would raise NameError: {node.id!r} is not defined"
            )


def test_r3_01_generated_config_resolves_and_uses_pinned_shapes(tmp_path, monkeypatch):
    dataset = _dataset(LOGIC_ROWS[:2])
    inputs = _inputs(dataset, credentials={"api_key": {"ref": "env:MOTTE_R3_KEY"}})
    config = inputs["manifest"]["external_benchmark"]["runner_config"]
    work = tmp_path / "job"
    work.mkdir()
    data_files = export_subject_files(work, config)
    source = render_opencompass_config_source(config, data_files)

    # review 的 NameError 反例（引用未定义的 CevalDataset）必须不复现。
    _assert_names_resolve(source)
    assert "CevalDataset" not in source and "CmmlUDataset" not in source
    # 0.4.2 契约：数据集/模型项 type=<导入的类对象>；端点参数 openai_api_base。
    assert "type=LocalMCQDataset," in source
    assert "type=RuntimeOpenAI," in source
    assert "base_url_env=None" in source  # retain upstream endpoint when no ref
    # 凭据引用经 os.environ 解析；明文不落盘。
    monkeypatch.setenv("MOTTE_R3_KEY", "synthetic-key")
    assert "os.environ" in source and "MOTTE_R3_KEY" in source
    assert "synthetic-key" not in source
    # 本地数据集的 load 样板与导出文件契约一致。
    assert "path=" in source and "infer_cfg=" in source
    assert "data_file=" not in source and "few_shot_file=" not in source
    rows = [json.loads(line) for line in data_files["logic"].read_text().splitlines()]
    assert [row["id"] for row in rows] == ["logic-1", "logic-2"]


# ------------------------------------------------------------------ R3-02


def test_r3_02_production_config_carries_structured_rows_and_few_shot():
    dev = [_row("logic-d1", "logic", "A", question="示例题干XYZ", split="dev")]
    dataset = _dataset(LOGIC_ROWS[:2] + dev)
    inputs = _inputs(dataset, few_shot=1, case_ids=["logic-1", "logic-2"])
    config = inputs["manifest"]["external_benchmark"]["runner_config"]
    case = config["cases"][0]
    # 生产 cases 带结构化题目/选项与渲染 prompt（含 few-shot）。
    assert case["question"] == "Q?"
    assert case["options"] == {"A": "1", "B": "2", "C": "3", "D": "4"}
    assert "示例题干XYZ" in case["prompt"] and "Answer:" in case["prompt"]
    # few-shot 示例内容随配置冻结（含示例 gold——示例带答案是合法的）。
    assert config["few_shot_examples"][0]["question"] == "示例题干XYZ"
    assert config["few_shot_examples"][0]["answer"] == "A"
    # 目标样本 gold 不进 prompt（隔离约束）。
    assert "answer" not in case
    target_section = case["prompt"].rsplit("Answer:", 1)[-1]
    assert target_section == ""


def test_r3_02_bridge_exports_real_production_rows(tmp_path):
    dataset = _dataset(LOGIC_ROWS[:2])
    inputs = _inputs(dataset)
    config = inputs["manifest"]["external_benchmark"]["runner_config"]
    work = tmp_path / "job"
    work.mkdir()
    export_subject_files(work, config)
    exported = [
        json.loads(line)
        for line in (work / "data" / "logic.jsonl").read_text().splitlines()
    ]
    # 非空题目与选项（不再是全空兜底），顺序与冻结选样一致。
    assert [row["id"] for row in exported] == ["logic-1", "logic-2"]
    assert all(row["question"] == "Q?" for row in exported)
    assert all(row["A"] == "1" and row["D"] == "4" for row in exported)
    assert all("answer" not in row for row in exported)


def test_r3_02_bridge_exports_few_shot_examples_file(tmp_path):
    dev = [_row("logic-d1", "logic", "A", question="示例题干XYZ", split="dev")]
    dataset = _dataset(LOGIC_ROWS[:1] + dev)
    inputs = _inputs(dataset, few_shot=1)
    config = inputs["manifest"]["external_benchmark"]["runner_config"]
    work = tmp_path / "job"
    work.mkdir()
    data_files = export_subject_files(work, config)
    examples = json.loads((work / "data" / "few_shot.json").read_text())
    assert examples == [{
        "question": "示例题干XYZ",
        "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
        "answer": "A",
    }]
    assert "__few_shot__" in data_files


# ------------------------------------------------------------------ R3-03


def _sleep_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "runner-python"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'mkdir -p "$MOTTE_WORK_DIR/outputs/20260920_090000/results/mock-model"\n'
        'printf \'{"accuracy": 100.0, "details": {"0": {"origin_prediction": "B", "predictions": "B", "references": "B"}}}\' > "$MOTTE_WORK_DIR/outputs/20260920_090000/results/mock-model/ceval-logic.json"\n'
        "sleep 600\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def test_r3_03_entry_keeps_token_in_argv_for_cross_instance_claim(tmp_path, monkeypatch):
    stub = _sleep_stub(tmp_path)
    monkeypatch.setenv("MOTTE_RUNNER_PYTHON", str(stub))
    # 与 wrapper 相同的 argv 形态：entry 模块 + --launch-token 占位符。
    adapter = CevalJobAdapter(argv=[
        str(stub), "-m", "motte_benchmark.opencompass.entry",
        "--launch-token", "{launch_token}",
    ])
    spec = ExternalJobSpec(
        run_id="run-r3-03", adapter_id="ceval-opencompass", adapter_version="1",
        runner_version="r", execution_config_hash="h", dataset_revision="rev",
        selected_case_ids=["logic-1"],
        profile={"benchmark_id": "ceval", "benchmark_version": "1"},
        work_root=str(tmp_path), environment_digest="d",
        runner_config={"cases": [
            {"case_id": "logic-1", "subject": "logic", "question": "Q?",
             "options": {"A": "1", "B": "2", "C": "3", "D": "4"}, "prompt": "p"},
        ]},
    )
    handle = adapter.prepare(spec)
    from motte_contracts.external_job import ExternalJobStatus, new_launch_token

    launching = handle.model_copy(update={
        "launch_token": new_launch_token(), "status": ExternalJobStatus.launching,
    })
    started = adapter.start(spec, launching)
    time.sleep(1.0)  # entry 完成配置生成并进入 sleep（上游"推理中"）

    # 另一实例（Worker 重启后）：argv 中的 token 足以认领活进程。
    fresh_adapter = CevalJobAdapter(argv=[str(stub)])
    polled = fresh_adapter.poll(started)
    assert str(polled.status) == "ExternalJobStatus.active"

    fresh_adapter.interrupt(started)
    pid = started.owned_resources["pids"][0]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("claimed process was not interrupted")


def test_r3_03_wrapper_passes_token_into_entry_argv(tmp_path):
    import subprocess
    import sys as _sys

    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "runner" / "opencompass-entry"
    # pinned 解释器 stub：entry 模块调用记录 argv 后交真实解释器执行；
    # 其余（上游 opencompass CLI）按成功退出。
    entry_python = tmp_path / "python"
    entry_python.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-m" ] && [ "$2" = "motte_benchmark.opencompass.entry" ]; then\n'
        '  printf "%s\\n" "$@" > "$MOTTE_WORK_DIR/entry-argv.txt"\n'
        f'  exec "{_sys.executable}" "$@"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    entry_python.chmod(0o755)
    work = tmp_path / "job"
    work.mkdir()
    (work / "runner-config.json").write_text(json.dumps({
        "benchmark_id": "ceval",
        "model": {"model": "gpt-r3", "parameters": {}},
        "credentials": {},
        "few_shot": {"count": 0},
        "cases": [R3_CASES[0]],
    }), encoding="utf-8")
    env = {
        **os.environ,
        "MOTTE_WORK_DIR": str(work),
        "MOTTE_RUNNER_PYTHON": str(entry_python),
        "MOTTE_LAUNCH_TOKEN": "launch-r3",
        "MOTTE_JOB_ID": "job-r3",
        "MOTTE_RUN_ID": "run-r3",
    }
    subprocess.run(  # noqa: S603 - 受控测试脚本
        ["bash", str(wrapper)], env=env, check=True, timeout=60,
        capture_output=True, text=True,
    )
    argv = (work / "entry-argv.txt").read_text().split()
    identity_args = argv[argv.index("--launch-token"):]
    assert identity_args == [
        "--launch-token", "launch-r3", "--job-id", "job-r3", "--run-id", "run-r3",
    ]
    # 真实 entry 执行：身份文件落工作目录（审计）。
    identity = json.loads((work / "launch-identity.json").read_text())
    assert identity["launch_token"] == "launch-r3"
    assert identity["job_id"] == "job-r3"


# ------------------------------------------------------------------ R3-04


R3_CASES = [{"case_id": "c1", "subject": "logic", "question": "Q?",
             "options": {"A": "1", "B": "2", "C": "3", "D": "4"}, "prompt": "p"}]


def _adapter_with_workspace(tmp_path, cases):
    adapter = CevalJobAdapter(argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "hang"})
    spec = ExternalJobSpec(
        run_id="run-r3", adapter_id="ceval-opencompass", adapter_version="1",
        runner_version="r", execution_config_hash="h", dataset_revision="rev",
        selected_case_ids=[case["case_id"] for case in cases],
        profile={"benchmark_id": "ceval", "benchmark_version": "1"},
        work_root=str(tmp_path), environment_digest="d",
        runner_config={"cases": cases},
    )
    return adapter, adapter.prepare(spec)


def test_r3_04_root_outputs_symlink_cannot_become_trusted(tmp_path):
    import shutil

    outside = tmp_path / "outside"
    (outside / "results" / "mock-model").mkdir(parents=True)
    (outside / "results" / "mock-model" / "ceval-logic.json").write_text(
        json.dumps({"accuracy": 1.0, "details": {
            "0": {"origin_prediction": "OUTSIDE_REVIEW_SENTINEL"},
        }}), encoding="utf-8",
    )
    adapter, handle = _adapter_with_workspace(tmp_path, R3_CASES)
    outputs = Path(handle.work_dir) / "outputs"
    outputs.mkdir()
    shutil.rmtree(outputs)
    outputs.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        adapter.read_output_files(handle)


def test_r3_04_symlink_component_rejected_by_fd_walk(tmp_path):
    adapter, handle = _adapter_with_workspace(tmp_path, R3_CASES)
    results = Path(handle.work_dir) / "outputs" / "results"
    results.mkdir(parents=True)
    target_dir = tmp_path / "elsewhere"
    target_dir.mkdir()
    (target_dir / "ceval-logic.json").write_text(
        json.dumps({"accuracy": 1.0}), encoding="utf-8",
    )
    (results / "mock-model").mkdir()
    (results / "mock-model" / "ceval-logic.json").symlink_to(target_dir / "ceval-logic.json")
    with pytest.raises(ValueError, match="symlink"):
        adapter.read_output_files(handle)


def test_r3_04_fd_walk_survives_parent_replacement_race(tmp_path):
    """父目录在清单之后、读取之前被替换：fd 链打开拒绝，不读外部内容。"""
    import shutil

    adapter, handle = _adapter_with_workspace(tmp_path, R3_CASES)
    results = Path(handle.work_dir) / "outputs" / "results" / "mock-model"
    results.mkdir(parents=True)
    (results / "ceval-logic.json").write_text(
        json.dumps({"accuracy": 0.0, "details": {
            "0": {"origin_prediction": "B", "predictions": "B", "references": "B"},
        }}), encoding="utf-8",
    )
    files = adapter.read_output_files(handle)
    assert any("ceval-logic.json" in rel for rel in files)
    # 清单成功后再替换父目录为指向外部的 symlink：fd 锚定拒绝读取。
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ceval-logic.json").write_text("OUTSIDE_REVIEW_SENTINEL", encoding="utf-8")
    shutil.rmtree(Path(handle.work_dir) / "outputs" / "results")
    (Path(handle.work_dir) / "outputs" / "results").symlink_to(outside)
    with pytest.raises(ValueError):
        adapter.read_output_files(handle)


# ------------------------------------------------------------------ R3-05


def test_r3_05_resolved_secret_configs_are_not_archived(tmp_path, monkeypatch):
    service, jobs, artifacts = _review_service(tmp_path, "opencompass_ts_ok", monkeypatch)
    dataset = _dataset(LOGIC_ROWS)
    inputs = _inputs(dataset)
    run = service.create_run(inputs["scenario_version"], inputs["manifest"], inputs["case_ids"])
    from motte_sdk.dispatcher import RunDispatcher

    finished = RunDispatcher(service).dispatch(run["id"])
    assert finished["status"] == "completed"
    job = jobs.jobs_for_run(run["id"])[0]
    work_dir = Path(job["handle"]["work_dir"])
    # 上游会把解析后的配置（含运行时解析的密钥）dump 到实验目录 configs/。
    injected = work_dir / "outputs" / "20260920_090000" / "configs" / "resolved.py"
    injected.parent.mkdir(parents=True, exist_ok=True)
    injected.write_text(
        "models = [dict(key='SYNTHETIC_REVIEW_API_KEY')]", encoding="utf-8",
    )
    # 已冻结的 bundle（密钥出现之前）不含标记；configs/ 从未进入证据。
    bundle = json.loads(
        artifacts.read_bytes(job["checkpoint"]["evidence"]["raw_bundle_artifact"]),
    )
    blob = json.dumps(bundle)
    assert "SYNTHETIC_REVIEW_API_KEY" not in blob
    assert "configs/" not in blob
    # 白名单在读取层生效：再次读取也不收集 configs/（不是事后过滤）。
    from types import SimpleNamespace

    adapter = CevalJobAdapter(argv=self_argv())
    files = adapter.read_output_files(SimpleNamespace(work_dir=str(work_dir)))
    assert not any("configs/" in rel for rel in files)
    assert any("results/mock-model/ceval-logic.json" in rel for rel in files)


# ------------------------------------------------------------------ R3-06


def test_r3_06_mapping_bound_to_frozen_config_not_work_dir(tmp_path):
    def case(case_id):
        return {"case_id": case_id, "subject": "logic", "question": "Q?",
                "options": {"A": "1", "B": "2", "C": "3", "D": "4"}, "prompt": "p"}

    adapter, handle = _adapter_with_workspace(tmp_path, [case("c1"), case("c2")])
    results_dir = Path(handle.work_dir) / "outputs" / "results" / "mock-model"
    results_dir.mkdir(parents=True)
    (results_dir / "ceval-logic.json").write_text(json.dumps({
        "accuracy": 50.0,
        "details": {
            "0": {"origin_prediction": "答案为 B", "predictions": "B", "references": "B"},
            "1": {"origin_prediction": "答案为 A", "predictions": "A", "references": "A"},
        },
    }), encoding="utf-8")
    frozen = adapter.read_output_files(handle)
    assert "runner-config.json" in frozen
    # 冻结后反转工作目录里的 cases 顺序：同一冻结输出的归属不变。
    config_path = Path(handle.work_dir) / "runner-config.json"
    tampered = json.loads(config_path.read_text())
    tampered["cases"].reverse()
    config_path.write_text(json.dumps(tampered), encoding="utf-8")
    rows, _ = adapter.collect_from_files(handle, {}, frozen)
    assert [(r.stable_case_key, r.output["prediction"]) for r in rows] == [
        ("c1", "B"), ("c2", "A"),
    ]


# ------------------------------------------------------------------ R3-07


def test_r3_07_import_crash_recovers_from_frozen_artifact_after_cleanup(tmp_path, monkeypatch):
    service, jobs, artifacts = _review_service(tmp_path, "opencompass_ok", monkeypatch)
    dataset = _dataset(LOGIC_ROWS)
    inputs = _inputs(dataset)

    class FlakyImports:
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

    flaky = FlakyImports(jobs, fail_on=2)
    service.store.external_jobs = flaky
    run = service.create_run(inputs["scenario_version"], inputs["manifest"], inputs["case_ids"])
    from motte_sdk.dispatcher import RunDispatcher

    first = RunDispatcher(service).dispatch(run["id"])
    assert first["status"] in {"failed", "quarantined"}  # 导入中断：Run 失败
    job = jobs.jobs_for_run(run["id"])[0]
    assert len(jobs.list_records(job["job_id"])) == 1  # 第 2 条导入中断
    # 冻结工件引用在导入前已落 checkpoint（R3-07 的前置持久化）。
    assert job["checkpoint"]["evidence"]["raw_bundle_artifact"]
    assert job["checkpoint"]["import_completed"] is False

    # 清理工作目录：恢复只能依赖已冻结工件。
    import shutil

    shutil.rmtree(Path(job["handle"]["work_dir"]))
    recovery = DurableExternalJobRunner(
        ExternalJobSupervisor(CevalJobAdapter(
            argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "opencompass_ok"},
        ), poll_interval_seconds=0.05),
        jobs, artifacts=artifacts, work_root=tmp_path / "jobs",
        parser_version=PARSER_VERSION,
    )
    recovery.bind_service(service)
    outcome = recovery(service.store.runs.get(run["id"]))
    assert outcome["recovered_from"] == "frozen-artifact"
    assert outcome["job_status"] == "settled"
    assert sorted(
        record["source_record_key"] for record in jobs.list_records(job["job_id"])
    ) == ["logic-1", "logic-2"]
    # 没有第二次启动：Job 仍只有一个。
    assert len(jobs.jobs_for_run(run["id"])) == 1


# ------------------------------------------------------------------ R3-08


def test_r3_08_over_budget_evidence_refuses_finalization(tmp_path, monkeypatch):
    # 挂起 runner 先建 Run；再用预置的大结果文件走一次新 runner 的采集。
    service, jobs, artifacts = _review_service(tmp_path, "hang", monkeypatch)
    dataset = _dataset(LOGIC_ROWS)
    inputs = _inputs(dataset, case_ids=["logic-1"])
    run = service.create_run(inputs["scenario_version"], inputs["manifest"], inputs["case_ids"])

    # 直接构造受控工作目录（prepare 由 recovery runner 的 launch 完成；
    # 这里手工落 runner-config + 9 MiB 结果文件 + 完成标记）。
    work = tmp_path / "jobs" / "job-r3-08"
    (work / "outputs" / "results" / "mock-model").mkdir(parents=True)
    (work / "runner-config.json").write_text(json.dumps({
        "cases": [inputs["manifest"]["external_benchmark"]["runner_config"]["cases"][0]],
    }), encoding="utf-8")
    big = {"accuracy": 100.0, "details": {
        "0": {"origin_prediction": "答案为 B", "predictions": "B", "references": "B",
              "padding": "x" * (9 * 1024 * 1024)},
    }}
    (work / "outputs" / "results" / "mock-model" / "ceval-logic.json").write_text(
        json.dumps(big, ensure_ascii=False), encoding="utf-8",
    )
    (work / ".motte-job-complete").write_text(
        json.dumps({"exit_code": 0, "completed": True}), encoding="utf-8",
    )

    adapter = CevalJobAdapter(argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": "hang"})
    supervisor = ExternalJobSupervisor(adapter, poll_interval_seconds=0.05)
    runner = DurableExternalJobRunner(
        supervisor, jobs, artifacts=artifacts, work_root=tmp_path / "jobs",
        parser_version=PARSER_VERSION,
    )
    runner.bind_service(service)
    handle = {
        "job_id": "job-r3-08", "run_id": run["id"],
        "launch_token": "launch-r3-08", "status": "active",
        "owned_resources": {"kind": "process", "pids": [], "exit_code": None},
        "launch_identity": {}, "work_dir": str(work),
        "collection_cursor": {},
    }
    jobs.begin_job({
        "job_id": "job-r3-08", "run_id": run["id"], "status": "active",
        "launch_token": "launch-r3-08",
        "spec": {"run_id": run["id"]}, "handle": handle,
        "checkpoint": {"import_completed": False},
    })
    outcome = runner(service.store.runs.get(run["id"]))
    assert outcome["job_status"] == "failed"
    assert outcome["error"]["code"] == "EVIDENCE_INCOMPLETE"
    assert outcome["import"]["imported"] == 0
    job = jobs.get_job("job-r3-08")
    assert job["status"] == "failed"
    assert job["checkpoint"]["evidence"]["complete"] is False
    assert jobs.list_records("job-r3-08") == []


# ------------------------------------------------------------------ R3-09


def test_r3_09_adapter_consumes_experiment_pointer_from_frozen_files(tmp_path):
    doc_a = {"accuracy": 0.0, "details": {}}
    doc_b = {"accuracy": 100.0, "details": {
        "0": {"origin_prediction": "B", "predictions": "B", "references": "B"},
    }}
    adapter, handle = _adapter_with_workspace(tmp_path, R3_CASES)
    outputs = Path(handle.work_dir) / "outputs"
    for name, doc in (("exp-a", doc_a), ("exp-b", doc_b)):
        target = outputs / name / "results" / "mock-model"
        target.mkdir(parents=True)
        (target / "ceval-logic.json").write_text(json.dumps(doc), encoding="utf-8")
    (outputs / "experiment.json").write_text(
        json.dumps({"experiment": "exp-b"}), encoding="utf-8",
    )
    frozen = adapter.read_output_files(handle)
    rows, _ = adapter.collect_from_files(handle, {}, frozen)
    # 指针选择 exp-b；exp-a 的空结果不混入。
    assert len(rows) == 1 and rows[0].stable_case_key == "c1"
    assert rows[0].output["prediction"] == "B"

    # 无效指针（指向不存在的实验）→ 明确拒绝。
    (outputs / "experiment.json").write_text(
        json.dumps({"experiment": "exp-missing"}), encoding="utf-8",
    )
    frozen = adapter.read_output_files(handle)
    with pytest.raises(Exception, match="exp-missing|declared experiment"):  # noqa: B017, PT011
        adapter.collect_from_files(handle, {}, frozen)

    # 无指针的多候选 → 拒绝合并。
    (outputs / "experiment.json").unlink()
    frozen = adapter.read_output_files(handle)
    with pytest.raises(Exception, match="ambiguous"):  # noqa: B017, PT011
        adapter.collect_from_files(handle, {}, frozen)


# ------------------------------------------------------------------ R3-10


def test_r3_10_budget_uses_longest_row_with_few_shot():
    short = _row("logic-1", "logic", "B", question="1")
    long_row = _row("logic-2", "logic", "B", question="L" * 500)
    dev = [_row("logic-d", "logic", "A", question="D" * 300, split="dev")]
    dataset = _dataset([short, long_row] + dev)
    model = _published_model(
        context_window=700, parameters={"max_output_tokens": 64},
    )
    reasons = validate_external_run_request(
        dataset, benchmark_id="ceval", model_record=model,
        scope="custom-subset", few_shot=1,
    )
    # 最长题目(500) + 示例(300) + 模板 + 输出 64 > 700：必须拦截。
    assert any(
        reason.startswith("CONTEXT_WINDOW_INSUFFICIENT") for reason in reasons
    )
    # 题目重排不改变结论。
    reordered = _dataset([long_row, short] + dev, revision="rev-r3-10b")
    reasons_reordered = validate_external_run_request(
        reordered, benchmark_id="ceval", model_record=model,
        scope="custom-subset", few_shot=1,
    )
    assert bool(reasons) == bool(reasons_reordered)


# ------------------------------------------------------------------ R3-11


def test_r3_11_revision_immutability_is_transactional(tmp_path):
    import concurrent.futures

    store = SQLiteBenchmarkDatasets(str(tmp_path / "runs.db"))
    barrier = threading.Barrier(2)

    def submit(question: str) -> str:
        catalog = BenchmarkCatalog(store)
        catalog.register("ceval")
        dataset = _dataset(
            [_row("logic-1", "logic", "B", question=question)],
            revision="rev-r3-race",
        )
        barrier.wait(timeout=10)  # 两实例都在 put 前汇合（原竞争窗口）
        try:
            catalog.update_dataset("ceval", dataset)
            return "accepted"
        except ValueError:
            return "conflict"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, q) for q in ("What is 1+1?", "What is 2+2?")]
        results = [future.result(timeout=30) for future in futures]
    assert sorted(results) == ["accepted", "conflict"], results
    # 既有记录未被后写者覆盖。
    stored = store.get("ceval", "rev-r3-race")
    questions = [row["question"] for row in stored["rows"]]
    assert questions in (["What is 1+1?"], ["What is 2+2?"])


def test_r3_11_memory_store_matches_sqlite_semantics():
    store = MemoryBenchmarkDatasets()
    payload = {
        "benchmark_id": "ceval", "dataset_revision": "r", "state": "ready",
        "files": [{"logical_name": "a", "sha256": "h1", "size_bytes": 1}],
    }
    assert store.put_immutable(dict(payload))["status"] == "created"
    assert store.put_immutable(dict(payload))["status"] == "identical"
    with pytest.raises(RevisionConflictError):
        store.put_immutable({
            **payload,
            "files": [{"logical_name": "a", "sha256": "h2", "size_bytes": 1}],
        })
    # 冲突不覆盖：内容仍是 h1。
    assert store.get("ceval", "r")["files"][0]["sha256"] == "h1"
