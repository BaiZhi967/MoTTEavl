"""M3 review R05：Job 定位文件必须早写、硬终止可恢复（T06/T07、A07/A09）。

复现（review 原文）：在 work/jobs 中放置已经完成的 Trial 文件，但没有
``job-location.json``（对应 Job 未返回前 Runner 被结束）。``read_output_files``
只输出配置、计划和 ``job_dir_unavailable``，不采集已有 Trial。原因是定位文件
要等 ``Job.run()`` 返回或捕获普通 Exception 才写，TERM/KILL 不保证走异常分支。

覆盖：
1. 真实 SIGTERM（``entry.py`` 的 handler，硬终止、不依赖 ``finally``）之后
   定位文件与完成标记仍然落盘，已完成 Trial 的证据可采集、可解析；
2. 只有"启动中"定位文件时 ``read_output_files`` 采集已完成 Trial，未产出的
   计划单元标 ``not_attempted``（不重启任务、不丢已完成证据）。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter
from motte_contracts.external_job import ExternalJobSpec
from motte_sdk import terminalbench as tb
from motte_sdk.service import build_run_service

TASKS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"
BENCHMARK_RUNTIME = Path(__file__).resolve().parents[2] / "packages" / "benchmark-runtime"


def _inputs(tmp_path: Path) -> dict[str, Any]:
    service = build_run_service(tmp_path / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(TASKS_ROOT), source_id="review-r05",
        dataset_revision="r05-1",
    )
    return tb.build_run_inputs(
        record=record, run_id="run-r05", job_id="job-r05",
        profile=tb.terminal_bench_profile(n_trials=1),
    )


def _spec(inputs: dict[str, Any], tmp_path: Path) -> ExternalJobSpec:
    external = inputs["manifest"]["external_benchmark"]
    return ExternalJobSpec(
        run_id="run-r05", adapter_id=ADAPTER_ID, adapter_version="1",
        runner_version="harbor-0.23.0", execution_config_hash=inputs["manifest_hash"],
        dataset_revision="r05-1", selected_case_ids=inputs["case_ids"],
        profile=external["profile"], work_root=str(tmp_path / "jobs"),
        environment_digest=external["environment_digest"],
        limits=external["limits"], runner_config=external["runner_config"],
    )


def _stage_partial_trial(work_dir: Path, job_name: str, *, task_path: str,
                         reward: float = 1.0) -> str:
    """在 Job 目录里放一个已完成 Trial（其余计划单元尚未产出）。"""
    jobs_dir = work_dir / "jobs"
    trial_name = "hello-pass__Killed1"
    trial_dir = jobs_dir / job_name / trial_name
    (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)
    (trial_dir / "result.json").write_text(json.dumps({
        "id": "partial-trial-1",
        "trial_name": trial_name,
        "task_id": {"path": task_path},
        "started_at": "2026-09-20T00:00:00+00:00",
        "finished_at": "2026-09-20T00:00:03+00:00",
        "exception_info": None,
        "verifier_result": {"rewards": {"reward": reward}},
        "agent_result": {"exit_code": 0},
    }), encoding="utf-8")
    (trial_dir / "verifier" / "reward.txt").write_text(f"{reward}\n", encoding="utf-8")
    return trial_name


def test_real_sigterm_keeps_location_and_partial_trial(tmp_path: Path) -> None:
    """真实 SIGTERM：定位文件 + 完成标记（143）落盘，已完成 Trial 不丢。"""
    inputs = _inputs(tmp_path)
    adapter = HarborJobAdapter(argv=["/bin/true"], data_root=str(TASKS_ROOT))
    handle = adapter.prepare(_spec(inputs, tmp_path))
    work_dir = Path(handle.work_dir)
    job_name = "job-r05"
    trial_name = _stage_partial_trial(work_dir, job_name, task_path="/data/tasks/hello-pass")

    program = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(BENCHMARK_RUNTIME)!r})\n"
        "from pathlib import Path\n"
        "from motte_benchmark.harbor import entry\n"
        f"work = Path({str(work_dir)!r})\n"
        "jobs_dir = work / 'jobs'\n"
        f"entry.record_job_location(work, jobs_dir, {job_name!r}, reused=False, started=True,\n"
        f"                          trial_names=['{trial_name}', 'hello-fail__Killed2'])\n"
        f"entry._install_signal_handlers(work, jobs_dir, {job_name!r})\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        # 硬终止：SIGTERM 到进程组（与 Runner 被取消时一致）。
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        assert proc.wait(timeout=20) == 143, "handler 必须以 143 退出"
    finally:
        if proc.poll() is None:  # pragma: no cover - 兜底
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    location = json.loads((work_dir / "harbor" / "job-location.json").read_text(encoding="utf-8"))
    assert location["started"] is True
    assert trial_name in location["trials"]
    assert location["compose_projects"], "定位文件必须记录 compose project 名（R04）"
    marker = json.loads((work_dir / ".motte-job-complete").read_text(encoding="utf-8"))
    assert marker == {
        "exit_code": 143, "completed": True,
        "detail": "runner received signal 15; partial trials kept",
    }
    signalled = json.loads((work_dir / "harbor" / "signal.json").read_text(encoding="utf-8"))
    assert signalled["signal"] == 15 and signalled["exit_code"] == 143
    assert trial_name in signalled["trials"]

    # 采集：只有"启动中"的定位文件也把已完成 Trial 冻结进来，其余 not_attempted。
    files = adapter.read_output_files(handle)
    index = json.loads(files["harbor/evidence-index.json"])
    assert index["job_location"]["started"] is True
    assert index["job_location"]["trials"] == [trial_name]
    assert index["files"]["harbor/job-location.json"]["scope"] == "outer"

    results, cursor = adapter.collect_from_files(handle, {}, files)
    payloads = [item.output for item in results]
    scored = [
        item for item in payloads if item["verifier_observation"]["status"] == "scored"
    ]
    assert len(scored) == 1, payloads
    assert scored[0]["verifier_observation"]["rewards"] == {"reward": 1.0}
    assert scored[0]["disposition"] == "succeeded"
    assert sum(1 for item in payloads if item["disposition"] == "not_attempted") == 1
    assert cursor["trial_count"] == 2


def test_started_location_without_any_trial_keeps_every_unit_not_attempted(
    tmp_path: Path,
) -> None:
    """启动中且尚无 Trial：全部计划单元 not_attempted（有 disposition，不空结果）。"""
    inputs = _inputs(tmp_path)
    adapter = HarborJobAdapter(argv=["/bin/true"], data_root=str(TASKS_ROOT))
    handle = adapter.prepare(_spec(inputs, tmp_path))
    work_dir = Path(handle.work_dir)
    # 定位文件指向的 Job 目录确实存在（Runner 在建 Job 时就创建它），只是还没有 Trial。
    (work_dir / "jobs" / "job-r05").mkdir(parents=True, exist_ok=True)
    (work_dir / "harbor" / "job-location.json").write_text(json.dumps({
        "job_dir": str(work_dir / "jobs" / "job-r05"),
        "jobs_dir": str(work_dir / "jobs"),
        "job_name": "job-r05",
        "started": True,
        "trials": [],
        "trial_names": ["hello-pass__Pending"],
        "compose_projects": ["hello-pass__pending__env"],
    }), encoding="utf-8")

    files = adapter.read_output_files(handle)
    results, cursor = adapter.collect_from_files(handle, {}, files)
    assert [item.output["disposition"] for item in results] == [
        "not_attempted", "not_attempted",
    ]
    assert cursor["trial_count"] == 2
    # 采集里明确标注这是"启动中"的部分证据，而不是一次完成的运行。
    index = json.loads(files["harbor/evidence-index.json"])
    assert index["job_location"]["started"] is True
    assert index["job_location"]["trial_names"] == ["hello-pass__Pending"]


def test_location_file_is_written_before_the_job_runs(tmp_path: Path) -> None:
    """顺序断言：定位文件在 Job.run 之前就存在（review R05 的核心要求）。"""
    import asyncio

    from motte_benchmark.harbor import entry as harbor_entry

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    jobs_dir = work_dir / "jobs"
    job_name = "job-order"

    async def _run() -> None:
        location = harbor_entry.record_job_location(
            work_dir, jobs_dir, job_name, reused=False, started=True,
            trial_names=["hello-pass__Abc1234"],
        )
        # 记录之后立刻可见（不是等 Job.run 返回才写）。
        assert Path(location["job_dir"]).parent == jobs_dir
        assert json.loads(
            (work_dir / "harbor" / "job-location.json").read_text(encoding="utf-8"),
        )["started"] is True

    asyncio.run(_run())
    assert (work_dir / "harbor" / "job-location.json").is_file()
