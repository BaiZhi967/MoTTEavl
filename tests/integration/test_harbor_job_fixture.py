"""M3-T06/T07：Harbor Job 采集、导入与恢复（M3-A07/A09/A10 的平台侧）。

两条路径，边界明确：

1. **离线（默认，本文件）**：用真实 Harbor 产物 fixture（由
   ``scripts/runner/capture-harbor-fixtures`` 从真实 Docker 运行脱敏而来）
   驱动适配器 → 共享 supervisor（真实冻结/checkpoint/幂等导入）→ 纯 Parser。
   验证"一 Job 多 Task/Trial、全部计划单元有 disposition、恢复只观察不重启、
   证据 hash 冲突阻断最终化"。
2. **真实 Runner（可选）**：``MOTTE_HARBOR_RUNNER_PYTHON`` 指向固定 Harbor
   环境时用真实 Harbor + 真实 Docker 跑仓库内确定性校准任务；未配置时跳过，
   最终记录见 ``docs/verification/M3.md``（skip 不作为通过证据）。

进程所有权（PID 归属、token 核验、进程组中断、残留清单）由 M2 的
``ProcessJobAdapter`` 与本阶段的真实校准运行覆盖，这里不重复实现。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter
from motte_benchmark.harbor.parser import PARSER_VERSION
from motte_contracts.external_job import (
    ExternalJobHandle,
    ExternalJobSpec,
    ExternalJobStatus,
)
from motte_sdk.external_jobs import DurableExternalJobRunner, ExternalJobSupervisor
from motte_sdk import terminalbench as tb
from motte_sdk.service import build_run_service
from motte_storage.artifacts import ArtifactStore
from motte_storage.external_jobs import MemoryExternalJobs

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "samples"
TASKS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"
#: 失败模式任务集（agent/verifier 超时），与 errors-timeout 样本一一对应。
TASKS_ERRORS_ROOT = TASKS_ROOT.parent / "tasks-errors"
SAMPLE = "pass-fail-2x2"


class FixtureHarborAdapter(HarborJobAdapter):
    """把脱敏的真实产物铺进工作目录，替代子进程 Runner。

    生命周期方法与真实适配器完全一致（``read_output_files`` /
    ``collect_from_files`` / 资源账本都走同一份实现），只有
    "跑 Job"这一步被替换成"铺 fixture"，因此被验证的是平台侧的采集、
    冻结、解析与导入语义。
    """

    def __init__(self, sample_dir: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)  # 真实适配器：prepare/读取/解析都走同一实现
        self.sample_dir = sample_dir
        self.start_count = 0
        self.staged = False

    def prepare(self, spec: ExternalJobSpec) -> ExternalJobHandle:
        handle = super().prepare(spec)
        self._stage(Path(handle.work_dir))
        return handle

    def _stage(self, work_dir: Path) -> None:
        job_dir = work_dir / "fixture-job"
        job_dir.mkdir(parents=True, exist_ok=True)
        for name in ("config.json", "lock.json"):
            source = self.sample_dir / name
            if source.is_file():
                shutil.copy2(source, job_dir / name)
        (job_dir / "result.json").write_text(
            json.dumps({"id": "job-fixture", "n_total_trials": 4}), encoding="utf-8",
        )
        for path in sorted((self.sample_dir / "trials").rglob("*")):
            if path.is_file():
                target = job_dir / path.relative_to(self.sample_dir / "trials")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        harness = work_dir / "harbor"
        harness.mkdir(parents=True, exist_ok=True)
        (harness / "job-location.json").write_text(
            json.dumps({
                "job_dir": str(job_dir), "jobs_dir": str(work_dir),
                "job_name": "fixture-job", "trials": [], "reused_existing_job_dir": False,
            }),
            encoding="utf-8",
        )
        self.staged = True

    def start(self, spec: ExternalJobSpec, handle: ExternalJobHandle) -> ExternalJobHandle:
        """不启动子进程：直接记账（owner token 与资源账本与真实适配器同构）。"""
        self.start_count += 1
        owned = {
            "kind": "fixture-runner",
            "pids": [],
            "exit_code": 0,
            "owner_token": handle.launch_token,
            "job_id": handle.job_id,
            "run_id": handle.run_id,
            "container_label": f"motte.job={handle.job_id}",
            "resources": [{
                "kind": "job_dir", "path": str(handle.work_dir),
                "owner_token": handle.launch_token,
            }],
        }
        return handle.model_copy(update={
            "status": ExternalJobStatus.active, "owned_resources": owned,
        })

    def poll(self, handle: ExternalJobHandle) -> ExternalJobHandle:
        return handle.model_copy(update={"status": ExternalJobStatus.settled})


class StartCountingAdapter(FixtureHarborAdapter):
    """在 ``start_count`` 之外记录 token，用于断言"恢复不重启"。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tokens: list[str] = []

    def start(self, spec: ExternalJobSpec, handle: ExternalJobHandle) -> ExternalJobHandle:
        self.tokens.append(handle.launch_token)
        return super().start(spec, handle)


def _build_store(tmp_path: Path):
    service = build_run_service(tmp_path / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(TASKS_ROOT), source_id="motte-harbor-fixtures",
        dataset_revision="fixtures-2026-09-20", license_id="Apache-2.0",
    )
    return service, record


def _spec(record: dict[str, Any], inputs: dict[str, Any], work_root: Path) -> ExternalJobSpec:
    external = inputs["manifest"]["external_benchmark"]
    return ExternalJobSpec(
        run_id="run-one", adapter_id=ADAPTER_ID, adapter_version="1",
        runner_version="harbor-0.23.0",
        execution_config_hash=inputs["manifest_hash"],
        dataset_revision="fixtures-2026-09-20",
        selected_case_ids=[task["task_key"] for task in record["manifest"]["tasks"]],
        profile=external["profile"], work_root=str(work_root),
        environment_digest=external["environment_digest"],
        limits={"poll_interval_seconds": 0.01},
        runner_config=external["runner_config"],
    )


def _run(service, record, tmp_path: Path, *, adapter_class=StartCountingAdapter):
    inputs = tb.build_run_inputs(
        record=record, run_id="run-one", job_id="job-one",
        profile=tb.terminal_bench_profile(n_trials=2),
    )
    work_root = tmp_path / "jobs"
    adapter = adapter_class(SAMPLES / SAMPLE, argv=["/bin/true"])
    job_store = MemoryExternalJobs()
    runner = DurableExternalJobRunner(
        ExternalJobSupervisor(adapter, poll_interval_seconds=0.01, sleep=lambda _s: None),
        job_store, artifacts=ArtifactStore(str(tmp_path / "artifacts")),
        work_root=str(work_root), parser_version=PARSER_VERSION,
    )
    run = {"id": "run-one", "case_ids": inputs["case_ids"],
           "manifest": inputs["manifest"]}
    outcome = runner(run)
    return inputs, adapter, job_store, runner, run, outcome


def test_harbor_one_job_many_tasks_trials(tmp_path: Path) -> None:
    """一 Job 两 Task 各两 Trial：一次启动、全部计划单元保留且 disposition 明确。"""
    service, record = _build_store(tmp_path)
    inputs, adapter, _job_store, _runner, _run_doc, outcome = _run(service, record, tmp_path)

    assert adapter.start_count == 1, "一个 Run 只允许启动一次 Runner"
    assert outcome["job_status"] == "settled"
    assert len(outcome["results"]) == 4, "两 Task × 两 Trial 全部保留"

    payloads = [row["output"] for row in outcome["results"]]
    assert sorted(item["disposition"] for item in payloads) == [
        "failed", "failed", "succeeded", "succeeded",
    ]
    # 每个计划的 Trial 都在结果里有对应身份（repeat_index 全覆盖）。
    by_task: dict[str, set[int]] = {}
    for item in payloads:
        by_task.setdefault(item["task_key"], set()).add(item["repeat_index"])
    assert len(by_task) == 2
    assert all(repeats == {0, 1} for repeats in by_task.values())

    # 冻结证据包含完整原始字节与 hash，且先冻结后解析。
    jobs = _job_store.jobs_for_run("run-one")
    assert jobs and jobs[0]["status"] == "settled"
    evidence = jobs[0]["checkpoint"]["evidence"]
    assert evidence["frozen_before_parse"] is True
    assert evidence["raw_bundle_artifact"]
    assert evidence["complete"] is True
    # 原始 bundle 可读回，且与解析结果一致（不依赖可清理的工作目录）。
    raw = json.loads(_runner.artifacts.read_bytes(evidence["raw_bundle_artifact"]))
    assert "harbor/trials/hello-pass__5RmqnD2/result.json" in raw["files"]


def test_trial_results_import_into_the_trial_store(tmp_path: Path) -> None:
    """采集结果写入 Trial 存储：计划 4 个、结果与 disposition 一一对应。"""
    service, record = _build_store(tmp_path)
    inputs, _adapter, _job_store, _runner, run, outcome = _run(service, record, tmp_path)

    service_rows = {
        f"{row['stable_case_key']}": {"result": row["output"]} for row in outcome["results"]
    }
    imported = service._import_trials(run, service_rows)
    assert imported["skipped"] is False
    assert imported["created"] == 4, "计划来自冻结 manifest，先落计划再落结果"
    assert imported["stored"] == 4
    assert not imported["conflicts"]

    rows = service.store.trials.list_for_run("run-one")
    assert len(rows) == 4
    assert {row["status"] for row in rows} == {"succeeded", "failed"}
    # 重复导入是幂等的：结果与 hash 都不变。
    again = service._import_trials(run, service_rows)
    assert again["created"] == 0 and again["stored"] == 0 and again["identical"] == 4


def test_recovery_observes_only_and_never_restarts(tmp_path: Path) -> None:
    """导入中断后恢复：从已冻结工件补齐，不重新启动 Runner（M3-A09）。"""
    service, record = _build_store(tmp_path)
    inputs, adapter, job_store, runner, run, first = _run(service, record, tmp_path)
    assert adapter.start_count == 1

    # 模拟"证据已冻结但导入未完成"的崩溃：清掉导入完成标记。
    jobs = job_store.jobs_for_run("run-one")
    job_id = jobs[-1]["job_id"]
    checkpoint = dict(jobs[-1]["checkpoint"])
    checkpoint["import_completed"] = False
    job_store.update_job(job_id, {"checkpoint": checkpoint})

    recovered = runner(run)
    assert adapter.start_count == 1, "恢复只观察/补齐，绝不第二次启动"
    assert len(recovered["results"]) == len(first["results"])
    assert {row["output"]["trial_id"] for row in recovered["results"]} == {
        row["output"]["trial_id"] for row in first["results"]
    }


def test_conflicting_trial_evidence_blocks_without_overwriting(tmp_path: Path) -> None:
    """同一 Trial 的原始证据变化：hash 冲突显式失败，旧证据不被覆盖（M3-A10）。"""
    service, record = _build_store(tmp_path)
    from motte_contracts.trial import canonical_hash

    inputs = tb.build_run_inputs(
        record=record, run_id="run-one", job_id="job-one",
        profile=tb.terminal_bench_profile(n_trials=2),
    )
    plan = inputs["manifest"]["external_benchmark"]["runner_config"]["plan"]["trials"]
    created = service.store.trials.create_plans([dict(item) for item in plan])
    assert [item["status"] for item in created] == ["created"] * 4
    trial_id = str(plan[0]["trial_id"])
    original = {"trial_id": trial_id, "disposition": "failed",
                "verifier_observation": {"status": "scored", "rewards": {"reward": 0.0}}}
    assert service.store.trials.put_result(
        trial_id, original, source_hash="sha256:raw-1", parser_version=PARSER_VERSION,
    )["status"] == "stored"

    tampered = {"trial_id": trial_id, "disposition": "succeeded",
                "verifier_observation": {"status": "scored", "rewards": {"reward": 1.0}}}
    conflict = service.store.trials.put_result(
        trial_id, tampered, source_hash="sha256:raw-2", parser_version=PARSER_VERSION,
    )
    assert conflict["status"] == "conflict"
    stored = service.store.trials.get(trial_id)
    assert stored["result"]["disposition"] == "failed", "旧证据保持原样"
    assert stored["source_hash"] == "sha256:raw-1"
    assert canonical_hash(stored["result"]) == canonical_hash(original)


def test_adapter_records_owner_tokens_and_residual_state(tmp_path: Path) -> None:
    """资源账本：容器标签/owner token 可核验，清理结果分 clean/residual。"""
    service, record = _build_store(tmp_path)
    inputs = tb.build_run_inputs(
        record=record, run_id="run-one", job_id="job-one",
        profile=tb.terminal_bench_profile(n_trials=1),
    )
    spec = _spec(record, inputs, tmp_path / "jobs")
    adapter = FixtureHarborAdapter(SAMPLES / SAMPLE, argv=["unused-fixture-runner"])
    handle = adapter.prepare(spec)
    started = adapter.start(spec, handle)
    owned = started.owned_resources
    assert owned["owner_token"] == handle.launch_token
    assert owned["container_label"] == "motte.job=" + handle.job_id
    assert any(item["kind"] == "job_dir" for item in owned["resources"])
    outcome = adapter.cleanup(started)
    assert outcome["owner_token"] == handle.launch_token
    assert outcome["job_id"] == handle.job_id
    assert outcome["known_resources"] == owned["resources"]
    assert outcome["job_dir_present"] is True
    # fixture Runner 没有真实子进程，所以"清理"只能如实报告"无法核验身份"，
    # 绝不宣称 clean（M3-T07：不确定的残留必须可定位，不能假装干净）。
    assert outcome["state"] == "residual"
    assert any(item.get("kind") == "identity_unverified" for item in outcome["leftovers"])


def test_verifier_visibility_and_probe_boundaries_are_recorded(tmp_path: Path) -> None:
    """采集边界如实记录：Verifier 可见性、不可观测的用量与缺失证据。"""
    service, record = _build_store(tmp_path)
    _inputs, _adapter, _job_store, _runner, _run_doc, outcome = _run(
        service, record, tmp_path,
    )
    payloads = [row["output"] for row in outcome["results"]]
    modes = {item["termination"].get("verifier_environment_mode") for item in payloads}
    assert modes == {"shared"}, "Verifier 与 Agent 共享环境的边界必须记录"
    unknown = [item for item in payloads if item["usage"]["cost_usd"] is None]
    assert len(unknown) == 4, "oracle 运行没有 provider 成本：保持 null，不填 0"
    assert all(item["usage"]["coverage"] == "unavailable" for item in unknown)


def test_error_sample_distinguishes_agent_and_verifier_failure(tmp_path: Path) -> None:
    """Agent 超时与 Verifier 超时在平台侧仍然是两类结果（M3-A04/A06）。"""
    service = build_run_service(tmp_path / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(TASKS_ERRORS_ROOT), source_id="motte-harbor-fixtures",
        dataset_revision="fixtures-errors-2026-09-20",
    )
    inputs = tb.build_run_inputs(
        record=record, run_id="run-err", job_id="job-err",
        profile=tb.terminal_bench_profile(n_trials=1),
    )
    work_root = tmp_path / "jobs-err"
    adapter = StartCountingAdapter(SAMPLES / "errors-timeout", argv=["unused-fixture-runner"])
    runner = DurableExternalJobRunner(
        ExternalJobSupervisor(adapter, poll_interval_seconds=0.01, sleep=lambda _s: None),
        MemoryExternalJobs(), artifacts=ArtifactStore(str(tmp_path / "artifacts-err")),
        work_root=str(work_root), parser_version=PARSER_VERSION,
    )
    outcome = runner({"id": "run-err", "case_ids": inputs["case_ids"],
                      "manifest": inputs["manifest"]})
    payloads = [row["output"] for row in outcome["results"]]
    statuses = sorted(item["verifier_observation"]["status"] for item in payloads)
    # errors-timeout 两个任务各 1 个 Trial：agent 超时但 Verifier 仍给出 reward=0；
    # verifier 超时没有 reward，因此是缺证据/Verifier 错误，不是失败。
    assert "scored" in statuses
    assert "verifier_error" in statuses
    agent_timeout = next(
        item for item in payloads
        if item["termination"].get("exception_type") == "AgentTimeoutError"
    )
    assert agent_timeout["verifier_observation"]["rewards"] == {"reward": 0.0}
    assert agent_timeout["disposition"] == "failed"
    assert agent_timeout["termination"]["failure_phase"] == "agent"

    verifier_timeout = next(
        item for item in payloads
        if item["termination"].get("exception_type") == "VerifierTimeoutError"
    )
    assert verifier_timeout["verifier_observation"]["rewards"] == {}
    assert verifier_timeout["disposition"] == "indeterminate"
    assert verifier_timeout["termination"]["failure_phase"] == "verifier"


@pytest.mark.skipif(
    not __import__("os").environ.get("MOTTE_HARBOR_RUNNER_PYTHON"),
    reason="real Harbor runner required (MOTTE_HARBOR_RUNNER_PYTHON)",
)
def test_real_harbor_docker_calibration(tmp_path: Path) -> None:  # pragma: no cover - live
    """真实 Harbor + 真实 Docker + oracle Agent：确定性通过/失败校准。

    独立命令：``scripts/runner/verify-harbor-local /path/to/runner/bin/python``。
    skip 不是通过证据；实际结果记录在 ``docs/verification/M3.md``。
    """
    import os
    import subprocess

    runner_python = os.environ["MOTTE_HARBOR_RUNNER_PYTHON"]
    assert Path(runner_python).is_file(), runner_python
    service = build_run_service(tmp_path / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(TASKS_ROOT), source_id="motte-harbor-fixtures",
        dataset_revision="fixtures-2026-09-20", license_id="Apache-2.0",
    )
    inputs = tb.build_run_inputs(
        record=record, run_id="run-live", job_id="job-live",
        profile=tb.terminal_bench_profile(n_trials=1),
    )
    adapter = HarborJobAdapter(
        argv=[str(Path(runner_python).parent / "harbor-entry")],
        data_root=str(TASKS_ROOT),
    )
    supervisor = ExternalJobSupervisor(adapter, poll_interval_seconds=1.0)
    runner = DurableExternalJobRunner(
        supervisor, MemoryExternalJobs(),
        artifacts=ArtifactStore(str(tmp_path / "artifacts")),
        work_root=str(tmp_path / "jobs"), parser_version=PARSER_VERSION,
    )
    outcome = runner({"id": "run-live", "case_ids": inputs["case_ids"],
                      "manifest": inputs["manifest"]})
    payloads = [row["output"] for row in outcome["results"]]
    rewards = {
        item["task_key"]: item["verifier_observation"]["rewards"].get("reward")
        for item in payloads
    }
    assert sorted(rewards.values()) == [0.0, 1.0], rewards
    assert len(payloads) == 2
    # 真实运行后按句柄清理：只能操作本 Job 拥有的资源，并如实报告残留。
    handle = ExternalJobHandle.model_validate(outcome["handle"])
    cleanup = adapter.cleanup(handle)
    assert cleanup["owner_token"] == handle.launch_token
    assert cleanup["state"] in ("clean", "residual")
    # 残留逐项可定位：每一项都必须带具体种类与说明，而不是一个布尔。
    for item in cleanup["leftovers"]:
        assert isinstance(item, dict) and item.get("kind")
    # 真实运行结束后不得再有本 Job 的容器存活（标签归本 Job）。
    alive = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=motte.job={handle.job_id}"],
        capture_output=True, text=True, check=False,
    )
    assert alive.stdout.strip() == "", alive.stdout
