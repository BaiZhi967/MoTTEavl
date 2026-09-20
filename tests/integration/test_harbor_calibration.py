"""M3-T10：从公共创建入口到报告的完整校准链路（M3-A01/A05/A15 的端到端）。

与 ``test_harbor_job_fixture.py`` 的分工：

- 那个文件验证"采集/冻结/解析/导入"的平台语义（离线 fixture，默认运行）；
- 本文件验证**生产链路**：API 准备 → 预检 → 创建 Run → Dispatcher →
  真实固定 Harbor Runner（真实 Docker + 确定性 oracle Agent）→ 冻结产物 →
  纯 Parser → ScoringPass → 报告接口。

真实层需要 ``MOTTE_HARBOR_RUNNER_PYTHON`` 指向固定 Harbor 环境；未提供时该
用例跳过（skip 不作为通过证据，记录见 ``docs/verification/M3.md``）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter
from motte_benchmark.harbor.parser import PARSER_VERSION, parse_harbor_files
from motte_benchmark.registry import register_adapter, unregister_adapter
from motte_sdk import terminalbench as tb
from motte_sdk.service import build_run_service

TASKS_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"
SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "samples"


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    unregister_adapter(ADAPTER_ID)


def test_known_pass_fail_and_cleanup_calibration(tmp_path: Path) -> None:
    """校准任务集本身自洽：解析出的 reward 与 Verifier 期望一致（离线）。"""
    files = {
        "harbor/" + path.relative_to(SAMPLES / "pass-fail-2x2").as_posix(): path.read_bytes()
        for path in (SAMPLES / "pass-fail-2x2").rglob("*")
        if path.is_file() and path.name != "SOURCES.json"
    }
    plan = json.loads(files["harbor/plan.json"])
    plans = [
        {
            "trial_id": f"trial-{task['normalized_relative_path'].rsplit('/', 1)[-1]}-{repeat}",
            "run_id": "run-cal", "task_key": task["task_key"], "repeat_index": repeat,
            "agent_config_hash": "sha256:a", "environment_hash": "sha256:e",
        }
        for task in plan["tasks"] for repeat in range(2)
    ]
    parsed = parse_harbor_files(files, plans)
    per_task: dict[str, list[float | None]] = {}
    for result in parsed["results"]:
        per_task.setdefault(result["task_key"], []).append(
            result["verifier_observation"]["rewards"].get("reward"),
        )
    rewards = sorted(
        next(value for value in values if value is not None)
        for values in per_task.values()
    )
    # 已知通过任务 reward=1、已知失败任务 reward=0，两者都不是"缺失"。
    assert rewards == [0.0, 1.0], per_task
    assert parsed["trial_count"] == 4


@pytest.mark.skipif(
    not os.environ.get("MOTTE_HARBOR_RUNNER_PYTHON"),
    reason="real Harbor runner required (MOTTE_HARBOR_RUNNER_PYTHON)",
)
def test_production_chain_api_to_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """生产链路：API 创建 → Dispatcher → 真实 Harbor/Docker → 评分 → 报告。

    独立命令：``scripts/runner/verify-harbor-local /path/to/runner/bin/python``。
    """
    runner_python = Path(os.environ["MOTTE_HARBOR_RUNNER_PYTHON"])
    assert runner_python.is_file(), runner_python
    db_path = tmp_path / "runs.db"
    monkeypatch.setenv("MOTTE_DB_PATH", str(db_path))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MOTTE_JOB_WORK_ROOT", str(tmp_path / "jobs"))
    report_path = tmp_path / "preflight.json"
    report_path.write_text(json.dumps({
        "available": True, "server_version": os.environ.get("MOTTE_DOCKER_VERSION", "27.4.0"),
        "platform": "linux/arm64",
    }), encoding="utf-8")
    monkeypatch.setenv("MOTTE_HARBOR_PREFLIGHT_REPORT", str(report_path))

    # 真实适配器：固定 Runner 的 wrapper + 受控任务根目录。
    register_adapter(
        ADAPTER_ID,
        lambda: HarborJobAdapter(
            argv=[str(runner_python.parent / "harbor-entry")], data_root=str(TASKS_ROOT),
        ),
    )

    application = create_app()
    with TestClient(application) as client:
        prepared = client.post("/api/v1/benchmarks/terminal-bench/prepare", json={
            "task_root": str(TASKS_ROOT), "source_id": "motte-harbor-fixtures",
            "revision": "fixtures-2026-09-20", "license_id": "Apache-2.0",
        })
        assert prepared.status_code == 201, prepared.text

        provider = client.post("/api/v1/providers", json={
            "name": "m3-live-provider", "kind": "openai_compatible",
            "base_url": "http://127.0.0.1:9/v1",
        })
        assert provider.status_code in (200, 201), provider.text
        model = client.post("/api/v1/models", json={
            "id": "m3-live-model", "provider": "m3-live-provider", "capabilities": {},
        })
        assert model.status_code in (200, 201), model.text
        assert client.post("/api/v1/models/m3-live-model/publish").status_code in (200, 201)

        preflight = client.get(
            "/api/v1/benchmarks/terminal-bench/preflight",
            params={"model": "m3-live-model", "n_trials": 1},
        ).json()
        assert preflight["ok"] is True, preflight
        assert preflight["platform_custom_profile"] is False

        created = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
            "model": "m3-live-model", "n_trials": 1,
        })
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]

        # Worker 侧：同一 Dispatcher 分派，执行由真实 Runner 承担。
        service = build_run_service(db_path)
        from motte_sdk.dispatcher import RunDispatcher

        dispatcher = RunDispatcher(service)
        claimed = dispatcher.claim(run_id=run_id)
        assert claimed is not None, "run 必须可被本 Worker 认领"
        dispatcher.execute_claimed(claimed)

        run = client.get(f"/api/v1/runs/{run_id}").json()
        assert run["status"] == "completed", json.dumps(run.get("error"))[:400]

        tasks = client.get(f"/api/v1/runs/{run_id}/tasks").json()
        assert tasks["total"] == 2
        aggregate = tasks["items"][0]["aggregate"]
        assert aggregate["valid_trial_pass_rate"] == 0.5, aggregate
        assert aggregate["valid_trial_coverage"] == 1.0, aggregate
        assert aggregate["gate"]["passed"] is True, aggregate["gate"]
        assert aggregate["cost"]["known_cost_usd"] is None, "无 provider 成本：保持 null"

        # Trial 详情：两次不同结论各自带证据，且不串。
        first = tasks["items"][0]
        trials = client.get(f"/api/v1/runs/{run_id}/tasks/{first['task_key']}/trials").json()
        assert trials["total"] == 1
        detail = client.get(f"/api/v1/runs/{run_id}/trials/{trials['items'][0]['trial_id']}").json()
        assert detail["verifier_observation"]["status"] == "scored"
        assert detail["termination"]["verifier_environment_mode"] == "shared"

        report = client.get(f"/api/v1/runs/{run_id}/report").json()
        assert report["status"] == "completed"
        assert report["summary"]["aggregate"]["valid_trial_coverage"] == 1.0
        assert report["scoring_pass_id"]

        # 重评分：只读冻结证据，不重新执行任务。
        rescored = client.post(f"/api/v1/runs/{run_id}/rescore")
        assert rescored.status_code in (200, 202), rescored.text
        passes = client.get(f"/api/v1/runs/{run_id}/scoring-passes").json()
        assert passes["total"] >= 2, "重评分产生新的 ScoringPass，旧 pass 保留"
        after = client.get(f"/api/v1/runs/{run_id}/tasks").json()
        assert after["items"][0]["aggregate"]["valid_trial_coverage"] == 1.0

    # 清理：本 Job 不得留下存活容器。
    import subprocess

    alive = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=motte.job"],
        capture_output=True, text=True, check=False,
    )
    assert alive.stdout.strip() == "", alive.stdout
    assert PARSER_VERSION.startswith("harbor-terminal-bench-parser")
