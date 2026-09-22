"""M3 review R03：不可变任务副本与 Runner 边界复验（M3-T01/T03/T06、G01/G06）。

复现反例：``prepare → build_run_inputs`` 之后修改 ``instruction.md``，
``verify_task_manifest`` 已返回不一致，但执行侧只检查目录存在，Harbor 照旧
执行改动后的任务包。本文件验证：

1. ``prepare`` 把每个选中任务的每个文件按冻结清单逐字节复验后复制到
   ``frozen-tasks/<task_key>/``，并把 ``MOTTE_TASK_ROOT`` 指向这份副本；
2. 排队后修改内容 / 路径替换 / symlink / 增删文件都在 ``start`` 之前拒绝
   （``HARBOR_TASK_CONTENT_DRIFT``），源目录缺失 → ``HARBOR_TASK_SOURCE_MISSING``；
3. Runner 侧（``entry.verify_frozen_tasks``）在构造 JobConfig 前复验副本，
   并写出 ``harbor/frozen-tasks.json`` 供平台审计。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from motte_benchmark.harbor import entry as harbor_entry
from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter
from motte_benchmark.harbor.frozen import (
    FROZEN_DIR_NAME,
    frozen_dir_name,
    task_files_hash,
)
from motte_benchmark.harbor.tasks import prepare_task_manifest
from motte_contracts.external_job import ExternalJobSpec
from motte_sdk import terminalbench as tb

TASKS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"


def _write_task(root: Path, name: str) -> Path:
    task_dir = root / name
    (task_dir / "environment").mkdir(parents=True, exist_ok=True)
    (task_dir / "tests").mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(
        f'schema_version = "1.4"\n\n[metadata]\nname = "{name}"\n', encoding="utf-8",
    )
    (task_dir / "instruction.md").write_text("original instruction\n", encoding="utf-8")
    (task_dir / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")
    (task_dir / "tests" / "test.sh").write_text("#!/bin/bash\necho 1\n", encoding="utf-8")
    (task_dir / "solution").mkdir(parents=True, exist_ok=True)
    (task_dir / "solution" / "solve.sh").write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    return task_dir


def _inputs(tmp_path: Path, *, task_root: Path = TASKS_ROOT, n_trials: int = 1):
    from motte_sdk.service import build_run_service

    service = build_run_service(tmp_path / "runs.db")
    record = tb.prepare_terminal_bench_dataset(
        service.store, task_root=str(task_root), source_id="review-r03",
        dataset_revision="r03-1",
    )
    return tb.build_run_inputs(
        record=record, run_id="run-r03", job_id="job-r03",
        profile=tb.terminal_bench_profile(n_trials=n_trials),
    )


def _spec(inputs: dict[str, Any], tmp_path: Path, *, limits: dict[str, Any] | None = None):
    external = inputs["manifest"]["external_benchmark"]
    return ExternalJobSpec(
        run_id="run-r03", adapter_id=ADAPTER_ID, adapter_version="1",
        runner_version="harbor-0.23.0", execution_config_hash=inputs["manifest_hash"],
        dataset_revision="r03-1", selected_case_ids=inputs["case_ids"],
        profile=external["profile"], work_root=str(tmp_path / "jobs"),
        environment_digest=external["environment_digest"],
        limits=limits or {"poll_interval_seconds": 0.01},
        runner_config=external["runner_config"],
    )


def _adapter(tmp_path: Path, *, data_root: Path = TASKS_ROOT) -> HarborJobAdapter:
    return HarborJobAdapter(
        argv=["/bin/true"], data_root=str(data_root), default_limits={"max_wall_seconds": 60.0},
    )


def test_prepare_materializes_verified_frozen_copy(tmp_path: Path) -> None:
    """复现反例的修复面：执行只读冻结副本，且副本逐字节等于冻结清单。"""
    inputs = _inputs(tmp_path)
    plan = inputs["manifest"]["external_benchmark"]["runner_config"]["plan"]
    adapter = _adapter(tmp_path)
    handle = adapter.prepare(_spec(inputs, tmp_path))

    frozen_root = Path(handle.work_dir) / FROZEN_DIR_NAME
    manifest = json.loads((frozen_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "motte-frozen-tasks@1"
    assert manifest["task_files_hash"] == task_files_hash(plan["task_files"])
    assert manifest["file_count"] == sum(len(item["files"]) for item in manifest["tasks"].values())

    for task_key, entry in manifest["tasks"].items():
        # 目录名由 task_key 确定性派生且 compose/文件系统安全（含冒号的 key
        # 会破坏 docker 的 ``<host>:<container>`` 挂载规格）。
        assert entry["directory"] == frozen_dir_name(task_key)
        assert ":" not in entry["directory"]
        task_dir = frozen_root / entry["directory"]
        assert task_dir.is_dir(), task_key
        for relative, digest in entry["files"].items():
            data = (task_dir / relative).read_bytes()
            assert "sha256:" + __import__("hashlib").sha256(data).hexdigest() == digest
            assert data == (TASKS_ROOT / entry["relative_path"] / relative).read_bytes()
    # Runner 的任务根指向副本，不再指向可被改动的源目录。
    assert handle.owned_resources["frozen_task_root"] == str(frozen_root)
    assert handle.owned_resources["frozen_tasks_hash"] == task_files_hash(plan["task_files"])
    assert adapter._process._extra_env["MOTTE_TASK_ROOT"] == str(frozen_root)


def test_modified_task_after_queueing_is_refused_before_start(tmp_path: Path) -> None:
    """复现反例：prepare 后改 instruction.md，prepare 必须拒绝（零执行）。"""
    task_root = tmp_path / "tasks"
    _write_task(task_root, "hello-pass")
    inputs = _inputs(tmp_path, task_root=task_root)
    # 排队后修改内容（review 的复现步骤）。
    (task_root / "hello-pass" / "instruction.md").write_text("tampered\n", encoding="utf-8")

    adapter = _adapter(tmp_path, data_root=task_root)
    with pytest.raises(Exception) as drift:
        adapter.prepare(_spec(inputs, tmp_path))
    assert getattr(drift.value, "code", None) == "HARBOR_TASK_CONTENT_DRIFT", drift.value
    assert "instruction.md" in str(drift.value)
    assert adapter.start_calls == [], "漂移必须在 start 之前拒绝"


def test_added_removed_and_replaced_paths_are_refused(tmp_path: Path) -> None:
    """文件增删、路径替换（symlink）、源目录缺失都不能沿用旧 Run。"""
    task_root = tmp_path / "tasks"
    _write_task(task_root, "hello-pass")
    inputs = _inputs(tmp_path, task_root=task_root)
    plan_files = inputs["manifest"]["external_benchmark"]["runner_config"]["plan"]["task_files"]

    # 1. 新增文件：清单里没有 → 漂移。
    (task_root / "hello-pass" / "tests" / "extra.sh").write_text("echo x\n", encoding="utf-8")
    adapter = _adapter(tmp_path, data_root=task_root)
    with pytest.raises(Exception) as extra:
        adapter.prepare(_spec(inputs, tmp_path))
    assert getattr(extra.value, "code", None) == "HARBOR_TASK_CONTENT_DRIFT"
    (task_root / "hello-pass" / "tests" / "extra.sh").unlink()

    # 2. 路径替换：把 tests/test.sh 换成指向别处的 symlink。
    target = task_root / "hello-pass" / "tests" / "test.sh"
    target.unlink()
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/bash\necho pwned\n", encoding="utf-8")
    try:
        target.symlink_to(outside)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires Developer Mode or symlink privilege")
        raise
    with pytest.raises(Exception) as swapped:
        adapter.prepare(_spec(inputs, tmp_path))
    assert getattr(swapped.value, "code", None) == "HARBOR_TASK_CONTENT_DRIFT"
    assert "test.sh" in str(swapped.value)
    target.unlink()
    target.write_text("#!/bin/bash\necho 1\n", encoding="utf-8")

    # 3. 源目录整体缺失：不能"重新计算身份后沿用旧 Run"。
    shutil.rmtree(task_root)
    with pytest.raises(Exception) as missing:
        adapter.prepare(_spec(inputs, tmp_path))
    assert getattr(missing.value, "code", None) == "HARBOR_TASK_SOURCE_MISSING"

    # 4. 冻结清单缺失（旧 manifest 没有 task_files）→ 明确拒绝。
    broken = json.loads(json.dumps(inputs))
    del broken["manifest"]["external_benchmark"]["runner_config"]["plan"]["task_files"]
    with pytest.raises(Exception) as no_hashes:
        _adapter(tmp_path).prepare(_spec(broken, tmp_path))
    assert getattr(no_hashes.value, "code", None) == "HARBOR_TASK_FILES_MISSING"
    assert plan_files  # 清单本身非空（前置断言，避免误判）


def test_runner_boundary_reverifies_and_records(tmp_path: Path) -> None:
    """Runner 侧复验：副本漂移 → 抛错且不启动；正常时写核验记录。"""
    inputs = _inputs(tmp_path)
    plan = inputs["manifest"]["external_benchmark"]["runner_config"]["plan"]
    adapter = _adapter(tmp_path)
    handle = adapter.prepare(_spec(inputs, tmp_path))
    work_dir = Path(handle.work_dir)
    frozen_root = work_dir / FROZEN_DIR_NAME

    record = harbor_entry.verify_frozen_tasks(work_dir, frozen_root, plan)
    assert record["ok"] is True and record["differences"] == []
    assert record["files_verified"] > 0
    assert record["task_files_hash"] == task_files_hash(plan["task_files"])
    written = json.loads((work_dir / "harbor/frozen-tasks.json").read_text(encoding="utf-8"))
    assert written["verified_at"] and written["files_verified"] == record["files_verified"]

    # 副本被改动（模拟磁盘层替换）→ Runner 拒绝，差异逐项写出。
    frozen_manifest = json.loads((frozen_root / "manifest.json").read_text(encoding="utf-8"))
    first_entry = frozen_manifest["tasks"][sorted(frozen_manifest["tasks"])[0]]
    (frozen_root / first_entry["directory"] / "instruction.md").write_text(
        "tampered\n", encoding="utf-8",
    )
    with pytest.raises(harbor_entry.RunnerBridgeError) as drift:
        harbor_entry.verify_frozen_tasks(work_dir, frozen_root, plan)
    assert "drifted" in str(drift.value)
    failed = json.loads((work_dir / "harbor/frozen-tasks.json").read_text(encoding="utf-8"))
    assert failed["ok"] is False
    assert any(item["code"] == "TASK_FILE_DRIFT" for item in failed["differences"])


def test_frozen_dir_name_is_compose_safe_and_matched_by_the_parser(tmp_path: Path) -> None:
    """副本目录名必须能被 docker compose 用，且 Parser 能据此归属（真实运行约束）。

    真实 Harbor 会把任务目录 basename 带进 Trial 目录名与 compose 的
    ``<host>:<container>`` 挂载规格：``sha256:...`` 里的冒号会让
    ``docker compose up`` 直接失败（invalid volume specification）。因此执行
    目录名要安全化，Parser 用冻结清单里的目录名做第一优先匹配。
    """
    from motte_benchmark.harbor.parser import parse_harbor_files

    assert frozen_dir_name("sha256:" + "a" * 64) == "sha256-" + "a" * 64
    assert ":" not in frozen_dir_name("sha256:" + "a" * 64)

    inputs = _inputs(tmp_path)
    plan = inputs["manifest"]["external_benchmark"]["runner_config"]["plan"]
    handle = _adapter(tmp_path).prepare(_spec(inputs, tmp_path))
    work_dir = Path(handle.work_dir)
    frozen_root = work_dir / FROZEN_DIR_NAME
    manifest = json.loads((frozen_root / "manifest.json").read_text(encoding="utf-8"))

    # 模拟真实 Harbor 的产物：task_id.path 指向副本目录，Trial 名以目录名前缀开头。
    task_key, entry = sorted(manifest["tasks"].items())[0]
    directory = entry["directory"]
    trial_dir = work_dir / "jobs" / "job-r03" / f"{directory[:32]}__Abc1234"
    (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)
    (trial_dir / "result.json").write_text(json.dumps({
        "id": "live-shaped-trial",
        "trial_name": trial_dir.name,
        "task_id": {"path": f"/data/{FROZEN_DIR_NAME}/{directory}"},
        "exception_info": None,
        "verifier_result": {"rewards": {"reward": 1.0}},
    }), encoding="utf-8")
    (trial_dir / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")
    (work_dir / "harbor" / "job-location.json").write_text(json.dumps({
        "job_dir": str(work_dir / "jobs" / "job-r03"),
        "jobs_dir": str(work_dir / "jobs"), "job_name": "job-r03",
        "started": True, "trials": [trial_dir.name], "trial_names": [trial_dir.name],
        "compose_projects": [],
    }), encoding="utf-8")

    files = _adapter(tmp_path).read_output_files(handle)
    assert "frozen-tasks/manifest.json" in files, "冻结清单必须进入证据（Parser 依赖它）"
    encoded = {rel: text.encode("utf-8") for rel, text in files.items()}
    parsed = parse_harbor_files(encoded, plan["trials"])
    matched = [
        result for result in parsed["results"]
        if result["termination"].get("task_matching", {}).get("method")
    ]
    assert len(matched) == 1, parsed["results"]
    assert matched[0]["task_key"] == task_key
    assert matched[0]["termination"]["task_matching"]["method"] == "frozen_dir"
    assert matched[0]["disposition"] == "succeeded"


def test_runner_rejects_plan_that_disagrees_with_frozen_manifest(tmp_path: Path) -> None:
    """Runner 比对 ``canonical_hash(plan["task_files"])``：计划与副本不一致即拒绝。"""
    inputs = _inputs(tmp_path)
    plan = json.loads(json.dumps(
        inputs["manifest"]["external_benchmark"]["runner_config"]["plan"],
    ))
    handle = _adapter(tmp_path).prepare(_spec(inputs, tmp_path))
    work_dir = Path(handle.work_dir)
    # 计划被替换（另一份 task_files）→ 与冻结副本清单不一致。
    plan["task_files"] = {
        key: {rel: "sha256:" + "0" * 64 for rel in files}
        for key, files in plan["task_files"].items()
    }
    with pytest.raises(harbor_entry.RunnerBridgeError) as mismatch:
        harbor_entry.verify_frozen_tasks(work_dir, work_dir / FROZEN_DIR_NAME, plan)
    assert "frozen task set does not match" in str(mismatch.value)


def test_runner_resolves_planned_tasks_to_the_frozen_copy(tmp_path: Path) -> None:
    """Runner 只把计划内的任务解析到 frozen-tasks/<task_key>，未知路径拒绝。"""
    inputs = _inputs(tmp_path)
    plan = inputs["manifest"]["external_benchmark"]["runner_config"]["plan"]
    config = inputs["manifest"]["external_benchmark"]["runner_config"]["harbor"]
    handle = _adapter(tmp_path).prepare(_spec(inputs, tmp_path))
    frozen_root = Path(handle.work_dir) / FROZEN_DIR_NAME

    resolved = harbor_entry.resolved_task_paths(config, frozen_root, plan)
    assert all(Path(entry["path"]).is_dir() for entry in resolved)
    keys = {str(entry["task_key"]) for entry in plan["tasks"]}
    assert {Path(entry["path"]).name for entry in resolved} == {
        frozen_dir_name(key) for key in keys
    }

    unplanned = json.loads(json.dumps(config))
    unplanned["job"]["tasks"][0]["path"] = "tasks/not-selected"
    with pytest.raises(harbor_entry.RunnerBridgeError) as unplanned_error:
        harbor_entry.resolved_task_paths(unplanned, frozen_root, plan)
    assert "not part of the frozen plan" in str(unplanned_error.value)


def test_compose_policy_and_frozen_copy_share_the_same_bytes(tmp_path: Path) -> None:
    """冻结副本里的 compose 与源目录一致（R02 检查的字节就是执行的字节）。"""
    task_root = tmp_path / "tasks"
    task = _write_task(task_root, "hello-pass")
    compose = "services:\n  main:\n    privileged: true\n"
    (task / "environment" / "docker-compose.yaml").write_text(compose, encoding="utf-8")
    inputs = _inputs(tmp_path, task_root=task_root)
    # prepare 只物化字节：策略拒绝发生在预检（R02），这里验证副本内容一致。
    handle = _adapter(tmp_path, data_root=task_root).prepare(_spec(inputs, tmp_path))
    frozen_root = Path(handle.work_dir) / FROZEN_DIR_NAME
    manifest = json.loads((frozen_root / "manifest.json").read_text(encoding="utf-8"))
    for _task_key, entry in manifest["tasks"].items():
        copied = (
            frozen_root / entry["directory"] / "environment" / "docker-compose.yaml"
        ).read_text(encoding="utf-8")
        assert copied == compose
