"""M3 review R04/R05/R07 的真实层：真实 Harbor + 真实 Docker 的中断/期限/所有权。

``MOTTE_HARBOR_RUNNER_PYTHON`` 指向固定 Harbor 环境时运行；未配置时跳过
（skip 不是通过证据）。这里用 ``tasks-slow/slow-sleep`` 任务把"运行中被取消"
与"总期限到点"变成确定性场景：

1. **运行中取消（R04/R05）**：真实 Job 启动后，等第一个 Trial 的容器出现，
   再 ``adapter.interrupt``——断言容器被定向停止、诱饵容器未被动过、已完成
   Trial 的证据保留、恢复只观察不重启；
2. **Job 期限（R07）**：``job_sec`` 到点触发 ``JOB_TIMEOUT``，Runner 进程与
   容器都被停止，部分证据与清理结果都如实记录。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter
from motte_benchmark.harbor.parser import PARSER_VERSION
from motte_contracts.external_job import ExternalJobHandle, ExternalJobSpec
from motte_sdk import terminalbench as tb
from motte_sdk.external_jobs import DurableExternalJobRunner, ExternalJobSupervisor
from motte_sdk.service import build_run_service
from motte_storage.artifacts import ArtifactStore
from motte_storage.external_jobs import MemoryExternalJobs

SLOW_TASKS_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks-slow"
)
TEST_IMAGE = "alpine:3.20"
RUNNER_PYTHON = os.environ.get("MOTTE_HARBOR_RUNNER_PYTHON")

pytestmark = pytest.mark.skipif(
    not RUNNER_PYTHON,
    reason="real Harbor runner required (MOTTE_HARBOR_RUNNER_PYTHON)",
)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True, text=True, check=False,
    ).returncode == 0


def _run_inputs(tmp_path: Path, *, job_sec: float | None, delete: bool = True) -> dict[str, Any]:
    service = build_run_service(tmp_path / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(SLOW_TASKS_ROOT), source_id="review-live-slow",
        dataset_revision="slow-1",
    )
    timeouts = {"job_sec": job_sec} if job_sec else None
    return tb.build_run_inputs(
        record=record, run_id="run-slow", job_id="job-slow",
        profile=tb.terminal_bench_profile(
            n_trials=1, timeouts=timeouts,
            environment={"type": "docker", "delete": delete},
        ),
    )


def _adapter() -> HarborJobAdapter:
    assert RUNNER_PYTHON
    repo_root = Path(__file__).resolve().parents[2]
    return HarborJobAdapter(
        argv=[str(Path(RUNNER_PYTHON).parent / "harbor-entry")],
        data_root=str(SLOW_TASKS_ROOT),
        # 用仓库当前源码跑桥接层：否则真实链路验证的是上一份部署快照
        # （install-harbor 是部署路径，不是测试路径）。
        extra_env={"PYTHONPATH": os.pathsep.join([
            str(repo_root / "packages/benchmark-runtime"),
            str(repo_root / "packages/contracts"),
        ])},
    )


def _runner(adapter: HarborJobAdapter, tmp_path: Path, *, poll: float = 1.0):
    return DurableExternalJobRunner(
        ExternalJobSupervisor(adapter, poll_interval_seconds=poll),
        MemoryExternalJobs(), artifacts=ArtifactStore(str(tmp_path / "artifacts")),
        work_root=str(tmp_path / "jobs"), parser_version=PARSER_VERSION,
    )


def _spec(inputs: dict[str, Any], tmp_path: Path) -> ExternalJobSpec:
    external = inputs["manifest"]["external_benchmark"]
    return ExternalJobSpec(
        run_id="run-slow", adapter_id=ADAPTER_ID, adapter_version="1",
        runner_version="harbor-0.23.0", execution_config_hash=inputs["manifest_hash"],
        dataset_revision="slow-1", selected_case_ids=inputs["case_ids"],
        profile=external["profile"], work_root=str(tmp_path / "jobs"),
        environment_digest=external["environment_digest"],
        limits=external["limits"], runner_config=external["runner_config"],
    )


@pytest.mark.skipif(not _docker_available(), reason="real Docker daemon required")
def test_real_in_flight_cancel_stops_containers_and_keeps_decoy(tmp_path: Path) -> None:
    """真实运行中取消：本 Job 容器被停止、诱饵保留、部分证据可采集（R04/R05）。"""
    import docker

    assert RUNNER_PYTHON
    inputs = _run_inputs(tmp_path, job_sec=None, delete=False)
    spec = _spec(inputs, tmp_path)
    adapter = _adapter()
    client = docker.from_env()
    decoy = client.containers.run(
        TEST_IMAGE, ["sleep", "600"], detach=True, name=f"motte-live-decoy-{os.getpid()}",
        labels={"motte.job": "unrelated-live-job"}, remove=False,
    )
    handle = adapter.prepare(spec)
    started = adapter.start(spec, handle)
    try:
        # 等真实容器出现（Harbor 用 compose 起 main 服务，标签由平台 overlay 注入）。
        deadline = time.monotonic() + 180
        owned: list[Any] = []
        while time.monotonic() < deadline:
            listed = adapter.ownership(started).list_owned()
            owned = [
                item for item in listed["containers"]
                if item["labels"].get("motte.job") == started.job_id
            ]
            if owned:
                break
            time.sleep(2.0)
        assert owned, "本 Job 的容器必须带 motte.job 标签（overlay 注入生效）"
        assert owned[0]["labels"].get("motte.owner"), owned[0]

        # 平台取消路径：中断进程组 + 定向停止本 Job 容器 + 尽力采集部分证据。
        supervisor = ExternalJobSupervisor(adapter, poll_interval_seconds=0.5)
        starts_before = len(adapter.start_calls)
        cancelled = supervisor.interrupt(spec, started)
        assert cancelled["job_status"] == "cancelled"
        assert len(cancelled["results"]) == 1, "计划单元必须有 disposition"
        cancelled_handle = ExternalJobHandle.model_validate(cancelled["handle"])
        assert cancelled_handle.status.value == "cancelled"
        assert cancelled_handle.owned_resources["container_state"] in ("clean", "residual")
        decoy.reload()
        assert decoy.status == "running", "诱饵容器不得被停止"

        # 恢复只观察、不重启：start 次数不变，结果与取消时一致。
        recovered = supervisor.recover(spec, cancelled_handle)
        assert len(adapter.start_calls) == starts_before, "恢复绝不能重启 Runner"
        assert len(recovered["results"]) == len(cancelled["results"])

        # 清理：删除本 Job 容器，诱饵仍在；daemon 可达时状态不超过可观察证据。
        cleanup = adapter.cleanup(cancelled_handle)
        assert cleanup["container_state"] in ("clean", "residual")
        assert cleanup["state"] in ("clean", "residual", "unknown")
        decoy.reload()
        assert decoy.status == "running"
        remaining = [
            item for item in cleanup["containers"]
            if item["labels"].get("motte.job") == started.job_id
        ]
        assert remaining == [], remaining
        assert adapter.poll(cancelled_handle).status.value != "active"
    finally:
        try:
            decoy.remove(force=True)
        except Exception:  # noqa: BLE001 - 清理失败不影响断言结论
            pass
        adapter.cleanup(started)


@pytest.mark.skipif(not _docker_available(), reason="real Docker daemon required")
def test_real_wrapper_crash_leaves_locatable_residue(tmp_path: Path) -> None:
    """真实硬崩溃（SIGKILL，无 handler）：容器与位置仍可定位、可清理（R04/R05）。"""
    import docker
    import signal as _signal

    inputs = _run_inputs(tmp_path, job_sec=None, delete=False)
    spec = _spec(inputs, tmp_path)
    adapter = _adapter()
    client = docker.from_env()
    decoy = client.containers.run(
        TEST_IMAGE, ["sleep", "600"], detach=True,
        name=f"motte-crash-decoy-{os.getpid()}",
        labels={"motte.job": "unrelated-crash-job"}, remove=False,
    )
    handle = adapter.prepare(spec)
    started = adapter.start(spec, handle)
    try:
        deadline = time.monotonic() + 180
        owned: list[Any] = []
        while time.monotonic() < deadline:
            owned = [
                item for item in adapter.ownership(started).list_owned()["containers"]
                if item["labels"].get("motte.job") == started.job_id
            ]
            if owned:
                break
            time.sleep(2.0)
        assert owned, "本 Job 的容器必须带 motte.job 标签"

        # 硬崩溃：SIGKILL 进程组，Runner 的 handler 没有机会执行（不依赖 finally）。
        pid = int(started.owned_resources["pids"][0])
        os.killpg(os.getpgid(pid), _signal.SIGKILL)
        poll_deadline = time.monotonic() + 30
        while time.monotonic() < poll_deadline:
            if adapter.poll(started).status.value != "active":
                break
            time.sleep(0.5)
        assert adapter.poll(started).status.value != "active"

        # 早写的定位文件仍在：位置可定位（R05 的核心要求）。
        work_dir = Path(started.work_dir)
        location = json.loads((work_dir / "harbor" / "job-location.json").read_text("utf-8"))
        assert location["job_dir"] and location["trial_names"]

        # 清理：核验所有权后删除本 Job 容器，诱饵保留；残留逐项可定位。
        cleanup = adapter.cleanup(started)
        assert cleanup["container_state"] in ("clean", "residual")
        assert cleanup["state"] in ("clean", "residual", "unknown")
        decoy.reload()
        assert decoy.status == "running", "诱饵容器不得被停止"
        remaining = [
            item for item in cleanup["containers"]
            if item["labels"].get("motte.job") == started.job_id
        ]
        assert remaining == [], remaining
        for item in cleanup["leftovers"]:
            assert isinstance(item, dict) and item.get("kind")
    finally:
        try:
            decoy.remove(force=True)
        except Exception:  # noqa: BLE001 - 清理失败不影响断言结论
            pass
        adapter.cleanup(started)


@pytest.mark.skipif(not _docker_available(), reason="real Docker daemon required")
def test_real_job_deadline_cancels_and_keeps_evidence(tmp_path: Path) -> None:
    """真实期限：``job_sec`` 到点 → JOB_TIMEOUT + 中断 + 部分证据 + 清理（R07）。"""
    inputs = _run_inputs(tmp_path, job_sec=25.0)
    assert inputs["manifest"]["external_benchmark"]["limits"]["max_wall_seconds"] == 25.0
    adapter = _adapter()
    runner = _runner(adapter, tmp_path)
    started = time.monotonic()
    outcome = runner({"id": "run-slow", "case_ids": inputs["case_ids"],
                      "manifest": inputs["manifest"]})
    elapsed = time.monotonic() - started

    assert outcome["error"] and outcome["error"]["code"] == "JOB_TIMEOUT", outcome["error"]
    assert outcome["job_status"] == "failed"
    # 任务本身 sleep 600 秒：期限必须远早于此结束。
    assert elapsed < 180, f"deadline not enforced (took {elapsed:.0f}s)"
    handle = ExternalJobHandle.model_validate(outcome["handle"])
    assert adapter.poll(handle).status.value != "active"
    # 计划单元仍然有 disposition（真实中断后可能尚未产出 reward）。
    assert len(outcome["results"]) == 1
    cleanup = adapter.cleanup(handle)
    assert cleanup["state"] in ("clean", "residual", "unknown")
    assert cleanup["container_state"] in ("clean", "residual", "unknown")
    for item in cleanup["leftovers"]:
        assert isinstance(item, dict) and item.get("kind")
    # 冻结任务副本与定位文件都在（R03/R05 的真实层证据）。
    work_dir = Path(handle.work_dir)
    assert (work_dir / "frozen-tasks" / "manifest.json").is_file()
    location = json.loads((work_dir / "harbor" / "job-location.json").read_text("utf-8"))
    assert location["job_name"] == "job-slow"
    assert location["trial_names"], "定位文件必须记录 Harbor 生成的 Trial 名"
