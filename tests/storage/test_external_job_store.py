"""M2-T03：外部 Job 持久化、幂等导入与取消/恢复映射。

反例与期望：
- 相同 key+hash 重复采集 no-op；同键不同内容 conflict 且两份摘要都保留。
- 采集与 checkpoint 同事务：崩溃恢复不产生重复记录/评分/Artifact 关联。
- 重复分派（崩溃后）只恢复观察；已导入记录不重复。
- 取消后迟到结果只作审计证据，终态不复活、评分不新增。
"""
import json
import os
import time

import pytest

from motte_contracts.external_job import ExternalJobSpec, ExternalJobStatus
from motte_sdk.dispatcher import RunDispatcher
from motte_sdk.execution_backends import (
    ExecutionBackendSpec,
    ExecutionHandle,
    register_backend,
    unregister_backend,
    validate_external_job_manifest,
)
from motte_sdk.external_jobs import DurableExternalJobRunner, ExternalJobSupervisor
from motte_sdk.service import RunService
from motte_benchmark.fake_runner import self_argv
from motte_benchmark.process import ProcessJobAdapter
from motte_storage.artifacts import ArtifactStore
from motte_storage.external_jobs import MemoryExternalJobs, SQLiteExternalJobs
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore


EXTERNAL_CONFIG = {
    "adapter_id": "process-fake",
    "adapter_version": "1",
    "runner_version": "fake-runner-1",
    "dataset_revision": "rev-1",
    "environment_digest": "sha256:" + "2" * 64,
    "profile": {"benchmark_id": "fake-bench", "benchmark_version": "1"},
    "retry_policy": {"runner": 0, "provider_transport": 0, "operator": 0},
    "limits": {"poll_interval_seconds": 0.05},
    # 创建/分派层要求 Runner 可消费的冻结输入（review R01）；
    # process-fake 适配器不读取它，仅满足契约。
    "runner_config": {
        "cases": [
            {"case_id": f"s-a:{index}", "subject": "s"} for index in range(1, 5)
        ],
    },
}

RECORD_A = {"case_id": "s-a:1", "status": "succeeded", "output": {"prediction": "A"}}
RECORD_A_ALT = {"case_id": "s-a:1", "status": "succeeded", "output": {"prediction": "X"}}
RECORD_B = {"case_id": "s-a:2", "status": "failed", "error": {"code": "RUNNER_EXIT"}}


def _job(job_id="job-1", run_id="run-1", status="launching", token="launch-t1"):
    return {
        "job_id": job_id,
        "run_id": run_id,
        "status": status,
        "launch_token": token,
        "spec": {"run_id": run_id},
        "handle": {"job_id": job_id, "run_id": run_id, "launch_token": token,
                   "status": status, "work_dir": "/tmp/w"},
        "checkpoint": {},
    }


@pytest.fixture(params=["memory", "sqlite"])
def job_store(request, tmp_path):
    if request.param == "memory":
        return MemoryExternalJobs()
    return SQLiteExternalJobs(str(tmp_path / "runs.db"))


def test_idempotent_collect_crash_and_conflict(job_store):
    job = job_store.begin_job(_job())
    assert job["status"] == "launching"

    first = job_store.import_record(
        "job-1", "s-a:1", "parser-v1", "hash-a", RECORD_A,
    )
    assert first["status"] == "imported"
    stored = job_store.get_job("job-1")
    assert stored["checkpoint"]["records_consumed"] == 1

    # 崩溃后重复采集：同键同内容 no-op，无新增记录，checkpoint 不变。
    again = job_store.import_record(
        "job-1", "s-a:1", "parser-v1", "hash-a", RECORD_A,
    )
    assert again["status"] == "noop"
    assert len(job_store.list_records("job-1")) == 1
    assert job_store.get_job("job-1")["checkpoint"]["records_consumed"] == 1

    other = job_store.import_record(
        "job-1", "s-a:2", "parser-v1", "hash-b", RECORD_B,
    )
    assert other["status"] == "imported"
    assert job_store.get_job("job-1")["checkpoint"]["records_consumed"] == 2

    # 同键不同内容：conflict，禁止覆盖；两份摘要都保留。
    conflict = job_store.import_record(
        "job-1", "s-a:1", "parser-v1", "hash-a-alt", RECORD_A_ALT,
    )
    assert conflict["status"] == "conflict"
    records = {row["source_record_key"]: row for row in job_store.list_records("job-1")}
    assert records["s-a:1"]["content_hash"] == "hash-a"
    assert records["s-a:1"]["payload"] == RECORD_A
    conflicts = job_store.list_conflicts("job-1")
    assert len(conflicts) == 1
    assert conflicts[0]["existing_hash"] == "hash-a"
    assert conflicts[0]["incoming_hash"] == "hash-a-alt"
    assert conflicts[0]["incoming_payload"] == RECORD_A_ALT
    assert job_store.get_job("job-1")["checkpoint"]["records_consumed"] == 2


def test_parser_version_is_part_of_the_key(job_store):
    job_store.begin_job(_job())
    first = job_store.import_record("job-1", "s-a:1", "parser-v1", "hash-a", RECORD_A)
    upgraded = job_store.import_record("job-1", "s-a:1", "parser-v2", "hash-a", RECORD_A)
    assert first["status"] == "imported"
    assert upgraded["status"] == "imported"
    assert len(job_store.list_records("job-1")) == 2


def test_job_lifecycle_and_recoverable_filter(job_store):
    job_store.begin_job(_job(token="launch-t1"))
    job_store.update_job("job-1", {"status": "active"})
    assert [job["job_id"] for job in job_store.recoverable_for_run("run-1")] == ["job-1"]
    job_store.update_job("job-1", {"status": "settled"})
    assert job_store.recoverable_for_run("run-1") == []
    assert [job["job_id"] for job in job_store.jobs_for_run("run-1")] == ["job-1"]

    # 同 job_id 不同 token 的 begin 是启动身份冲突，不是幂等重放。
    with pytest.raises(Exception, match="launch"):
        job_store.begin_job(_job(token="launch-t2"))


def _spec(work_root, case_ids):
    return ExternalJobSpec(
        run_id="run-durable",
        adapter_id="process-fake",
        adapter_version="1",
        runner_version="fake-runner-1",
        execution_config_hash="sha256:" + "1" * 64,
        dataset_revision="rev-1",
        selected_case_ids=list(case_ids),
        profile={"benchmark_id": "fake-bench", "benchmark_version": "1"},
        work_root=str(work_root),
        environment_digest="sha256:" + "2" * 64,
        limits={"poll_interval_seconds": 0.05},
    )


def _durable_service(tmp_path, mode):
    store = SQLiteRunStore(tmp_path / "runs.db")
    service = RunService(store)
    jobs = SQLiteExternalJobs(str(tmp_path / "runs.db"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    adapter = ProcessJobAdapter(argv=self_argv(), extra_env={"MOTTE_FAKE_MODE": mode})
    supervisor = ExternalJobSupervisor(adapter, poll_interval_seconds=0.05)
    runner = DurableExternalJobRunner(
        supervisor, jobs, artifacts=artifacts, work_root=tmp_path / "jobs",
    )
    runner.bind_service(service)

    def build(run):
        def attach(bound_service, run_id):
            runner.bind_service(bound_service)

        return ExecutionHandle(
            backend_id="process-fake",
            backend_version="1",
            execution_mode="job",
            run_job=runner,
            attach=attach,
        )

    register_backend(ExecutionBackendSpec(
        id="process-fake", version="1",
        validate=validate_external_job_manifest,
        build=build,
        capabilities={"interactive": False, "safe_to_repeat": False},
        execution_mode="job",
    ))
    return service, runner, jobs, artifacts


def _job_run(service):
    return service.create_run(
        "fake-suite@1",
        {
            "execution": {"backend_id": "process-fake", "backend_version": "1"},
            "external_benchmark": dict(EXTERNAL_CONFIG),
        },
        case_ids=["s-a:1", "s-a:2", "s-a:3", "s-a:4"],
    )


def test_durable_dispatch_import_and_crash_recovery(tmp_path):
    service, runner, jobs, artifacts = _durable_service(tmp_path, "ok")
    try:
        run = _job_run(service)
        completed = RunDispatcher(service).dispatch(run["id"])

        assert completed["status"] == "completed"
        assert {row["case_id"]: row["outcome"] for row in completed["cases"]} == {
            f"s-a:{index}": "responded" for index in range(1, 5)
        }
        job_rows = jobs.jobs_for_run(run["id"])
        assert len(job_rows) == 1 and job_rows[0]["status"] == "settled"
        assert len(jobs.list_records(job_rows[0]["job_id"])) == 4
        # 冻结的 outcome 工件存在且可读回（内容寻址路径，review R04）。
        outcome_files = list((tmp_path / "artifacts").rglob("outcome-*.json"))
        assert len(outcome_files) == 1
        payload = json.loads(outcome_files[0].read_text(encoding="utf-8"))
        assert payload["job_status"] == "settled"

        # 崩溃后重复分派（采集前后）：已导入记录 no-op，无重复评分。
        replay = runner(service.store.runs.get(run["id"]))
        assert replay["import"]["imported"] == 0
        assert replay["import"]["conflicts"] == []
        assert len(jobs.list_records(job_rows[0]["job_id"])) == 4
        assert len(service.store.scoring_passes.list_for_run(run["id"])) == 1
    finally:
        unregister_backend("process-fake", "1")


def test_nonzero_exit_maps_partial_dispositions(tmp_path):
    service, runner, jobs, artifacts = _durable_service(tmp_path, "fail")
    try:
        run = _job_run(service)
        finished = RunDispatcher(service).dispatch(run["id"])

        assert finished["status"] == "failed"
        by_case = {row["case_id"]: row["outcome"] for row in finished["cases"]}
        assert by_case == {
            "s-a:1": "responded", "s-a:2": "responded", "s-a:3": "responded",
            "s-a:4": "not_attempted",  # 未尝试不消失
        }
        assert finished["error"]["code"] == "JOB_NONZERO_EXIT"
        job_rows = jobs.jobs_for_run(run["id"])
        assert job_rows[0]["status"] == "failed"
    finally:
        unregister_backend("process-fake", "1")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_cancel_interrupts_job_and_late_results_stay_audit_only(tmp_path):
    import threading

    service, runner, jobs, artifacts = _durable_service(tmp_path, "hang")
    try:
        run = _job_run(service)
        worker = threading.Thread(
            target=lambda: RunDispatcher(service).dispatch(run["id"]), daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + 10
        active = None
        while time.monotonic() < deadline:
            recoverable = jobs.recoverable_for_run(run["id"])
            if recoverable and recoverable[0].get("status") == "active":
                active = recoverable[0]
                break
            time.sleep(0.05)
        assert active is not None, "job should become active with a live pid"
        pid = active["handle"]["owned_resources"]["pids"][0]

        cancelled = service.cancel(run["id"], reason="operator")
        worker.join(timeout=15)
        assert cancelled["status"] == "cancelled"
        job_rows = jobs.jobs_for_run(run["id"])
        assert job_rows[0]["status"] == "cancelled"

        # 迟到结果：只作审计证据，终态不复活、评分不新增。
        late = [
            {"case_id": "s-a:1", "status": "succeeded", "output": {"prediction": "A"}},
        ]
        summary = runner.import_late_results(run["id"], late)
        assert summary["audit_only"] is True
        assert summary["imported"] == 1
        after = service.get_run(run["id"])
        assert after["status"] == "cancelled"
        assert service.store.scoring_passes.list_for_run(run["id"]) == []
        events = [event["type"] for event in service.events(run["id"])]
        assert "external_job_late_results" in events

        stopped = time.monotonic() + 5
        while _pid_alive(pid) and time.monotonic() < stopped:
            time.sleep(0.05)
        assert not _pid_alive(pid), "cancelled job process must be interrupted"
    finally:
        unregister_backend("process-fake", "1")
