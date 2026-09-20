"""M3 review R07：Job 总期限由 Supervisor 真实强制（超时 → 中断 + 部分证据 + 清理）。

复现/静态（review 原文）：不同 ``job_sec``、``environment_build_sec``、
``agent_setup_sec`` 都只生成固定倍率 1.0，秒数留在旁路记录；SDK 外部 limits
只有 poll interval，Supervisor 实际读取的 ``max_wall_seconds`` 没有设置——
UI/API/CLI 接受的 ``job_timeout_sec`` 完全不能限制 Job 总运行时间。

这里用一个**可控阻塞 Runner**（``tests/fixtures/.../blocking_runner.py``）跑
真实子进程：它在 Job 目录里先落一个已完成 Trial，然后阻塞 600 秒。断言：

- ``max_wall_seconds`` 到点触发 ``JOB_TIMEOUT``；
- 进程真的被中断（不是"等到自然结束"），墙钟时间远小于阻塞时长；
- 已完成 Trial 的部分证据被采集（分数保留）；
- 清理可执行且状态不超出可观察证据。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter
from motte_benchmark.harbor.parser import PARSER_VERSION
from motte_contracts.external_job import ExternalJobHandle, ExternalJobSpec
from motte_sdk import terminalbench as tb
from motte_sdk.external_jobs import DurableExternalJobRunner, ExternalJobSupervisor
from motte_sdk.service import build_run_service
from motte_storage.artifacts import ArtifactStore
from motte_storage.external_jobs import MemoryExternalJobs

TASKS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"
BLOCKING_RUNNER = (
    Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "blocking_runner.py"
)
#: 阻塞 Runner 声称会睡 600 秒；期限必须远早于此。
DEADLINE_SECONDS = 2.0


def _spec(
    tmp_path: Path, *, limits: dict[str, object] | None = None,
    timeouts: dict[str, object] | None = None,
) -> tuple[ExternalJobSpec, dict]:
    service = build_run_service(tmp_path / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(TASKS_ROOT), source_id="review-r07",
        dataset_revision="r07-1",
    )
    inputs = tb.build_run_inputs(
        record=record, run_id="run-r07", job_id="job-r07",
        profile=tb.terminal_bench_profile(n_trials=1, timeouts=timeouts),
    )
    external = inputs["manifest"]["external_benchmark"]
    spec = ExternalJobSpec(
        run_id="run-r07", adapter_id=ADAPTER_ID, adapter_version="1",
        runner_version="harbor-0.23.0", execution_config_hash=inputs["manifest_hash"],
        dataset_revision="r07-1", selected_case_ids=inputs["case_ids"],
        profile=external["profile"], work_root=str(tmp_path / "jobs"),
        environment_digest=external["environment_digest"],
        limits=limits if limits is not None else external["limits"],
        runner_config=external["runner_config"],
    )
    return spec, inputs


def test_job_deadline_interrupts_and_keeps_partial_evidence(tmp_path: Path) -> None:
    """期限触发 → 中断 → 已完成 Trial 的证据与分数保留 → 清理可执行。"""
    import sys

    # 期限来自操作员声明的 job_sec（真实公共路径：profile → limits → Supervisor）。
    spec, inputs = _spec(tmp_path, timeouts={"job_sec": DEADLINE_SECONDS})
    assert spec.limits["max_wall_seconds"] == DEADLINE_SECONDS
    adapter = HarborJobAdapter(
        argv=[sys.executable, str(BLOCKING_RUNNER)], data_root=str(TASKS_ROOT),
    )
    supervisor = ExternalJobSupervisor(adapter, poll_interval_seconds=0.05)
    runner = DurableExternalJobRunner(
        supervisor, MemoryExternalJobs(),
        artifacts=ArtifactStore(str(tmp_path / "artifacts")),
        work_root=str(tmp_path / "jobs"), parser_version=PARSER_VERSION,
    )
    started = time.monotonic()
    outcome = runner({"id": "run-r07", "case_ids": inputs["case_ids"],
                      "manifest": inputs["manifest"]})
    elapsed = time.monotonic() - started

    # 期限真的生效：远早于阻塞 Runner 的 600 秒，且错误码是 JOB_TIMEOUT。
    assert outcome["error"] and outcome["error"]["code"] == "JOB_TIMEOUT", outcome["error"]
    assert outcome["error"]["details"]["max_wall_seconds"] == DEADLINE_SECONDS
    assert outcome["job_status"] == "failed"
    assert elapsed < 60, f"deadline was not enforced (took {elapsed:.1f}s)"

    # 部分证据保留：阻塞 Runner 预置的 Trial 仍然出现在结果里（有分数）。
    payloads = [row["output"] for row in outcome["results"]]
    scored = [
        item for item in payloads
        if item["verifier_observation"]["status"] == "scored"
    ]
    assert scored, payloads
    assert scored[0]["verifier_observation"]["rewards"] == {"reward": 1.0}
    assert scored[0]["disposition"] == "succeeded"
    # 计划里没跑完的单元仍然有 disposition，不消失也不冒充通过。
    assert len(payloads) == 2
    assert any(item["disposition"] == "not_attempted" for item in payloads)

    # 中断后进程真的不在了（轮询到非 active），清理只操作本 Job 资源。
    handle = ExternalJobHandle.model_validate(outcome["handle"])
    assert adapter.poll(handle).status.value != "active"
    cleanup = adapter.cleanup(handle)
    assert cleanup["owner_token"] == handle.launch_token
    assert cleanup["state"] in ("clean", "residual", "unknown")
    for item in cleanup["leftovers"]:
        assert isinstance(item, dict) and item.get("kind")


def test_deadline_is_required_before_start(tmp_path: Path) -> None:
    """没有可执行期限的 Job 在创建时拒绝，不无限运行。"""
    spec, _inputs = _spec(tmp_path, limits={"poll_interval_seconds": 0.05})
    from motte_benchmark.protocol import BenchmarkRuntimeError

    adapter = HarborJobAdapter(argv=["/bin/true"], data_root=str(TASKS_ROOT))
    adapter._process.default_limits["max_wall_seconds"] = None
    with pytest.raises(BenchmarkRuntimeError) as missing:
        adapter.prepare(spec)
    assert missing.value.code == "HARBOR_JOB_DEADLINE_MISSING"

    adapter._process.default_limits["max_wall_seconds"] = -1.0
    with pytest.raises(BenchmarkRuntimeError):
        adapter.prepare(spec)


def test_requested_job_seconds_reach_the_supervisor_limits(tmp_path: Path) -> None:
    """``job_sec`` 必须变成 Supervisor 读取的 ``max_wall_seconds``（R07 接入点）。"""
    service = build_run_service(tmp_path / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(TASKS_ROOT), source_id="review-r07",
        dataset_revision="r07-1",
    )
    inputs = tb.build_run_inputs(
        record=record, run_id="run-r07", job_id="job-r07",
        profile=tb.terminal_bench_profile(
            n_trials=1, timeouts={"job_sec": 900.0, "agent_sec": 60.0,
                                  "agent_setup_sec": 30.0, "verifier_sec": 45.0},
        ),
    )
    external = inputs["manifest"]["external_benchmark"]
    assert external["limits"]["max_wall_seconds"] == 900.0
    assert external["limits"]["poll_interval_seconds"] > 0
    native = external["runner_config"]["harbor"]
    assert native["job"]["agents"][0]["override_timeout_sec"] == 60.0
    assert native["job"]["agents"][0]["override_setup_timeout_sec"] == 30.0
    assert native["job"]["verifier"]["override_timeout_sec"] == 45.0
    assert native["effective_timeouts"]["job_sec"]["effective_sec"] == 900.0
    assert native["effective_timeouts"]["agent_sec"]["effective_sec"] == 60.0
    # 冻结配置本身可序列化（进入 manifest 与证据）。
    json.dumps(native, ensure_ascii=False, sort_keys=True)


def test_environment_build_seconds_are_refused_at_creation(tmp_path: Path) -> None:
    """无法按秒数执行的参数在创建时拒绝（review R07 的"明确标未执行"）。"""
    from motte_benchmark.harbor.config import HarborConfigError

    service = build_run_service(tmp_path / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(TASKS_ROOT), source_id="review-r07",
        dataset_revision="r07-1",
    )
    with pytest.raises(HarborConfigError) as unsupported:
        tb.build_run_inputs(
            record=record, run_id="run-r07", job_id="job-r07",
            profile=tb.terminal_bench_profile(
                n_trials=1, timeouts={"environment_build_sec": 300.0},
            ),
        )
    assert unsupported.value.code == "HARBOR_TIMEOUT_UNSUPPORTED"
