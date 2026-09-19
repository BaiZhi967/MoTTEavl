"""M2-T02：外部 Job 受控句柄与进程生命周期。

反例与期望：
- 四个样本同属一个 Job：start 计数 1，不逐题启动。
- PID 被无关进程占用 / token 不可核验：interrupt 绝不信号无关进程，恢复只观察。
- 超时/中断只停本 Job 拥有的进程树；prepare 无任何进程执行。
- 启动意图（含 launch_token）先于 adapter.start 持久化。
- stdout/stderr 有界消费；结果文件越界/超大/半写一律拒绝，不伪造记录。

假 Runner 是 `motte_benchmark.fake_runner`（`-m` 调用），合成数据仅用于
生命周期验证，不是官方 C-Eval 内容。测试内不直接 spawn 进程：无关进程
用另一个 hang 模式 Job 扮演（不同 token / 不同 work root）。
"""
import json
import os
import time
from pathlib import Path

import pytest

from motte_benchmark.fake_runner import self_argv
from motte_benchmark.process import ProcessJobAdapter
from motte_benchmark.protocol import BenchmarkRuntimeError
from motte_contracts.external_job import ExternalJobHandle, ExternalJobSpec, ExternalJobStatus
from motte_sdk.external_jobs import ExternalJobSupervisor


def _spec(work_root, case_ids=("s-a:1", "s-a:2", "s-a:3", "s-a:4"), limits=None):
    return ExternalJobSpec(
        run_id="run-test",
        adapter_id="process-fake",
        adapter_version="1",
        runner_version="fake-runner-1",
        execution_config_hash="sha256:" + "1" * 64,
        dataset_revision="rev-1",
        selected_case_ids=list(case_ids),
        profile={"benchmark_id": "fake-bench", "benchmark_version": "1"},
        work_root=str(work_root),
        environment_digest="sha256:" + "2" * 64,
        limits=limits or {},
    )


def _adapter(*, argv=None, extra_env=None, mode="ok"):
    merged_env = {"MOTTE_FAKE_MODE": mode}
    merged_env.update(extra_env or {})
    if argv is None:
        argv = self_argv()
    return ProcessJobAdapter(argv=argv, extra_env=merged_env)


class _UnrelatedJob:
    """用独立 hang Job 扮演无关存活进程；退出时按自己的 token 清理。"""

    def __init__(self, tmp_path):
        self.adapter = _adapter(mode="hang")
        self.supervisor = ExternalJobSupervisor(self.adapter)
        self.handle = self.supervisor.launch(_spec(tmp_path / "unrelated-work"))

    @property
    def pid(self) -> int:
        return self.handle.owned_resources["pids"][0]

    def close(self) -> None:
        owned = self.adapter.poll(self.handle)
        if owned.status == ExternalJobStatus.active:
            self.adapter.interrupt(self.handle)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _single_job_dir(work_root):
    job_dirs = [item for item in work_root.iterdir() if item.is_dir()]
    assert len(job_dirs) == 1
    return job_dirs[0]


def test_launch_once_and_pid_reuse(tmp_path):
    adapter = _adapter(mode="ok")
    supervisor = ExternalJobSupervisor(adapter)

    outcome = supervisor.run(_spec(tmp_path / "work"))

    assert outcome["job_status"] == "settled"
    assert [item["source_case_id"] for item in outcome["results"]] == [
        "s-a:1", "s-a:2", "s-a:3", "s-a:4",
    ]
    # 四个样本同属一个 Job：只 start 一次。
    assert len(adapter.start_calls) == 1
    assert adapter.start_calls[0]["selected_case_ids"] == [
        "s-a:1", "s-a:2", "s-a:3", "s-a:4",
    ]

    # PID 复用防护：句柄指向一个不持有本 token 的存活进程时，interrupt 不信号。
    unrelated = _UnrelatedJob(tmp_path)
    try:
        foreign_handle = ExternalJobHandle(
            job_id="job-foreign",
            run_id="run-test",
            launch_token="launch-not-ours",
            owned_resources={"pids": [unrelated.pid]},
            launch_identity={"pid": unrelated.pid, "launch_token": "launch-not-ours"},
            work_dir=str(tmp_path / "work" / "job-foreign"),
            status=ExternalJobStatus.active,
        )
        report = adapter.cleanup(foreign_handle)
        assert _pid_alive(unrelated.pid)  # 无关进程未被误杀
        assert report["interrupted"] is False
        assert any(
            item.get("kind") == "identity_unverified"
            for item in report["leftovers"]
        )
    finally:
        unrelated.close()


def test_prepare_creates_workdir_without_process(tmp_path):
    adapter = _adapter()
    handle = adapter.prepare(_spec(tmp_path / "work"))

    assert handle.status == ExternalJobStatus.prepared
    assert os.path.isdir(handle.work_dir)
    assert str(handle.work_dir).startswith(str(tmp_path / "work"))
    assert adapter.start_calls == []
    assert adapter.spawned_processes() == []


def test_timeout_interrupts_only_owned_tree(tmp_path):
    adapter = _adapter(mode="spawn_child")
    supervisor = ExternalJobSupervisor(adapter)
    work_root = tmp_path / "work"
    unrelated = _UnrelatedJob(tmp_path)
    try:
        outcome = supervisor.run(_spec(
            work_root, limits={"max_wall_seconds": 1, "poll_interval_seconds": 0.1},
        ))
        assert outcome["job_status"] == "failed"
        assert outcome["error"]["code"] == "JOB_TIMEOUT"
        # Runner 派生的子进程也属于本 Job 的进程树，一并停止。
        child_pid = int(
            (_single_job_dir(work_root) / "child.pid").read_text(encoding="utf-8"),
        )
        deadline = time.monotonic() + 5
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_alive(child_pid)
        # 无关进程不受影响。
        assert _pid_alive(unrelated.pid)
    finally:
        unrelated.close()


def test_launch_intent_persisted_before_start(tmp_path):
    adapter = _adapter(mode="ok")
    order: list[str] = []

    class RecordingAdapter:
        def __init__(self) -> None:
            self._inner = adapter

        def prepare(self, spec):
            order.append("prepare")
            return self._inner.prepare(spec)

        def start(self, spec, handle):
            order.append("start")
            return self._inner.start(spec, handle)

        def poll(self, handle):
            return self._inner.poll(handle)

        def interrupt(self, handle):
            return self._inner.interrupt(handle)

        def collect(self, handle, cursor):
            return self._inner.collect(handle, cursor)

        def cleanup(self, handle):
            return self._inner.cleanup(handle)

    supervisor = ExternalJobSupervisor(
        RecordingAdapter(),
        intent_journal=lambda record: order.append("journal:" + record["event"]),
    )
    supervisor.run(_spec(tmp_path / "work"))

    assert order.index("journal:launch_intent") < order.index("start")
    assert order.index("journal:launch_started") > order.index("start")


def test_bounded_stdout_consumption(tmp_path):
    adapter = _adapter(mode="noise")
    supervisor = ExternalJobSupervisor(adapter)
    outcome = supervisor.run(_spec(
        tmp_path / "work", limits={"max_output_bytes": 8192, "poll_interval_seconds": 0.05},
    ))

    assert outcome["job_status"] == "settled"
    tail = adapter.output_tail()
    assert len(tail["stdout"]) <= 8192
    assert tail["stdout_truncated"] is True


def test_recover_with_verifiable_token_observes_without_restart(tmp_path):
    runner_argv = self_argv("--launch-token", "{launch_token}")
    adapter = _adapter(argv=runner_argv, mode="slow_ok")
    supervisor = ExternalJobSupervisor(adapter)
    spec = _spec(tmp_path / "work")
    launched = supervisor.launch(spec)

    # 崩溃后重启：新 supervisor 只凭持久句柄恢复观察，不再 start。
    adapter2 = _adapter(argv=runner_argv, mode="slow_ok")
    recovering = ExternalJobSupervisor(adapter2, poll_interval_seconds=0.05)
    outcome = recovering.recover(spec, launched)

    assert outcome["job_status"] == "settled"
    assert [item["source_case_id"] for item in outcome["results"]] == ["s-a:1"]
    assert adapter2.start_calls == []


def _dead_pid() -> int:
    """取一个确认不存在的 PID，模拟崩溃后已消失的 Job 进程。"""
    candidate = os.getpid() + 100_000
    while _pid_alive(candidate):
        candidate += 1
    return candidate


def test_recover_unverifiable_token_maps_indeterminate(tmp_path):
    adapter = _adapter()
    supervisor = ExternalJobSupervisor(adapter)
    dead_pid = _dead_pid()

    stale = ExternalJobHandle(
        job_id="job-stale",
        run_id="run-test",
        launch_token="launch-gone",
        owned_resources={"pids": [dead_pid]},
        launch_identity={"pid": dead_pid, "launch_token": "launch-gone"},
        work_dir=str(tmp_path / "work" / "job-stale"),
        status=ExternalJobStatus.active,
    )
    outcome = supervisor.recover(_spec(tmp_path / "work"), stale)

    assert outcome["job_status"] == "indeterminate"
    assert outcome["error"]["code"] == "JOB_OUTCOME_INDETERMINATE"
    assert adapter.start_calls == []


def test_collect_rejects_escape_oversize_and_halfwritten(tmp_path):
    adapter = _adapter()
    handle = adapter.prepare(_spec(tmp_path / "work"))
    work_dir = Path(handle.work_dir)

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"records": [
        {"case_id": "s-a:1", "status": "succeeded"},
    ]}), encoding="utf-8")
    os.symlink(outside, work_dir / "results.json")
    with pytest.raises(BenchmarkRuntimeError, match="results"):
        adapter.collect(handle, {})

    (work_dir / "results.json").unlink()
    (work_dir / "results.json").write_text(json.dumps({"records": [
        {"case_id": f"s-a:{index}", "status": "succeeded"} for index in range(64)
    ]}), encoding="utf-8")
    with pytest.raises(BenchmarkRuntimeError) as excinfo:
        adapter.collect(handle, {}, max_result_bytes=64)
    assert excinfo.value.code == "JOB_OUTPUT_INVALID"

    (work_dir / "results.json").unlink()
    (work_dir / "results.json").write_text(
        '{"records": [{"case_id": "s-a:1", "sta', encoding="utf-8",  # 半写文件
    )
    with pytest.raises(BenchmarkRuntimeError) as excinfo:
        adapter.collect(handle, {})
    assert excinfo.value.code == "JOB_OUTPUT_INVALID"


def test_nonzero_exit_preserves_partial_results(tmp_path):
    adapter = _adapter(mode="fail")
    supervisor = ExternalJobSupervisor(adapter)

    outcome = supervisor.run(_spec(tmp_path / "work"))

    assert outcome["job_status"] == "failed"
    assert len(outcome["results"]) == 3
    assert outcome["error"]["code"] == "JOB_NONZERO_EXIT"
    assert len(adapter.start_calls) == 1


def test_interrupt_cancels_owned_job(tmp_path):
    adapter = _adapter(mode="hang")
    supervisor = ExternalJobSupervisor(adapter)
    spec = _spec(tmp_path / "work")
    handle = supervisor.launch(spec)

    cancelled = adapter.interrupt(handle)
    assert cancelled.status == ExternalJobStatus.cancelled
    pid = cancelled.owned_resources["pids"][0]
    deadline = time.monotonic() + 5
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(pid)
