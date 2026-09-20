"""M3-T09：Terminal-Bench 公共 API 链路（M3-G16、A15 的用户面）。

覆盖：任务准备（只读）→ 预检（拒绝原因可执行）→ 创建 Run（只入队）→
Task/Trial 下钻（Trial 切换不串证据、迟到/未知身份显式）→ 重评分不重跑。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_benchmark.registry import register_adapter, unregister_adapter
from motte_storage import run_store as run_store_module

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "runs.db"
    monkeypatch.setenv("MOTTE_DB_PATH", str(db_path))
    application = create_app()
    with TestClient(application) as test_client:
        test_client.app = application
        yield test_client


def _prepare(client: TestClient, *, revision: str = "fixtures-2026-09-20") -> dict:
    response = client.post(
        "/api/v1/benchmarks/terminal-bench/prepare",
        json={
            "task_root": str(FIXTURE_ROOT),
            "source_id": "motte-harbor-fixtures",
            "revision": revision,
            "license_id": "Apache-2.0",
            "license_evidence": "repository-owned deterministic calibration tasks",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _published_model(client: TestClient) -> str:
    """注册 provider + 已发布模型（生产创建路径的前置）。"""
    provider = client.post("/api/v1/providers", json={
        "name": "m3-provider", "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:9/v1",
    })
    assert provider.status_code in (200, 201), provider.text
    created = client.post("/api/v1/models", json={
        "id": "m3-harbor-model", "provider": "m3-provider", "capabilities": {},
    })
    assert created.status_code in (200, 201), created.text
    published = client.post("/api/v1/models/m3-harbor-model/publish")
    assert published.status_code in (200, 201), published.text
    return "m3-harbor-model"


def test_prepare_is_read_only_and_reports_identity(client: TestClient) -> None:
    """准备只读：任务身份稳定、带 tests/solution 事实与许可，可重复执行。"""
    first = _prepare(client)
    assert first["state"] == "ready"
    assert first["total"] == 2
    paths = sorted(task["normalized_relative_path"] for task in first["tasks"])
    assert paths == ["hello-fail", "hello-pass"]
    keys = {task["task_key"] for task in first["tasks"]}
    assert len(keys) == 2
    for task in first["tasks"]:
        assert task["has_tests"] is True and task["has_solution"] is True
        assert task["file_count"] >= 4

    # 同字节重复准备：revision 不可变，hash 不变。
    again = _prepare(client)
    assert again["manifest_hash"] == first["manifest_hash"]
    tasks = client.get("/api/v1/benchmarks/terminal-bench/tasks").json()
    assert tasks["total"] == 2

    # 未固定 revision 拒绝，且错误可操作。
    bad = client.post("/api/v1/benchmarks/terminal-bench/prepare", json={
        "task_root": str(FIXTURE_ROOT), "source_id": "s", "revision": "latest",
    })
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "TASK_REVISION_UNPINNED"


def test_preflight_reports_actionable_reasons_without_starting_anything(
    client: TestClient,
) -> None:
    """预检失败关闭：无 Docker 探测报告时拒绝，且不产生任何 Run。"""
    _prepare(client)
    model_id = _published_model(client)
    report = client.get(
        "/api/v1/benchmarks/terminal-bench/preflight",
        params={"model": model_id, "n_trials": 3},
    ).json()
    assert report["ok"] is False
    assert "DOCKER_UNAVAILABLE" in report["reasons"]
    assert report["messages"]["DOCKER_UNAVAILABLE"]
    assert report["platform_custom_profile"] is False
    assert report["tasks"]
    # 预检失败不留任何 Run。
    assert client.get("/api/v1/runs").json()["total"] == 0


def test_create_run_requires_adapter_and_passed_preflight(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adapter 未注册 / 预检不过时拒绝创建，且没有 Job 被启动。"""
    _prepare(client)
    model_id = _published_model(client)
    body = {"model": model_id, "n_trials": 2}

    missing = client.post("/api/v1/benchmarks/terminal-bench/runs", json=body)
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "RUNNER_NOT_CONNECTED"

    from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter

    register_adapter(ADAPTER_ID, lambda: HarborJobAdapter(argv=["/nonexistent/harbor-entry"]))
    try:
        blocked = client.post("/api/v1/benchmarks/terminal-bench/runs", json=body)
        assert blocked.status_code == 422
        assert blocked.json()["error"]["code"] == "PREFLIGHT_FAILED"
        assert "DOCKER_UNAVAILABLE" in blocked.json()["error"]["details"]["reasons"]
        assert client.get("/api/v1/runs").json()["total"] == 0

        # 提供 Runner 探测报告后预检通过，Run 只入队（不执行）。
        report_path = Path(client.app.state.__dict__.get("tmp", "/tmp")) / "preflight.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({
            "available": True, "server_version": "27.4.0", "platform": "linux/arm64",
        }), encoding="utf-8")
        monkeypatch.setenv("MOTTE_HARBOR_PREFLIGHT_REPORT", str(report_path))
        created = client.post("/api/v1/benchmarks/terminal-bench/runs", json=body)
        assert created.status_code == 202, created.text
        payload = created.json()
        assert payload["status"] == "queued"
        assert payload["trials"] == 4, "two tasks x two planned repeats"

        run = client.get(f"/api/v1/runs/{payload['id']}").json()
        assert run["status"] == "queued"
        manifest = run["manifest"]
        external = manifest["external_benchmark"]
        assert external["adapter_id"] == ADAPTER_ID
        plan = external["runner_config"]["plan"]
        assert len(plan["trials"]) == 4
        assert {trial["repeat_index"] for trial in plan["trials"]} == {0, 1}
        assert external["runner_config"]["harbor"]["job"]["n_attempts"] == 2
        assert external["runner_config"]["harbor"]["job"]["retry"]["max_retries"] == 0
    finally:
        unregister_adapter(ADAPTER_ID)


def test_task_and_trial_drilldown_keeps_errors(client: TestClient) -> None:
    """Task→Trial→证据下钻：错误、未尝试与未知身份都可见，不串证据。"""
    from motte_contracts.trial import trial_id_for

    _prepare(client)
    # 直接通过 SDK 门面写一个 Run + Trial（API 读取路径与执行解耦）。
    from motte_sdk.service import build_run_service
    from motte_sdk import terminalbench as tb

    service = build_run_service(None)
    record = tb.prepared_dataset(service.store)
    _published_model(client)  # 保持与生产创建路径一致的前置
    inputs = tb.build_run_inputs(
        record=record, run_id="run-manual", job_id="job-manual",
        profile=tb.terminal_bench_profile(n_trials=2),
    )
    run = service.create_run(
        tb.SCENARIO_VERSION, inputs["manifest"], inputs["case_ids"], run_id="run-manual",
    )
    run_id = run["id"]
    assert run_id == "run-manual", "计划身份与 Run 身份必须一致"
    trials = inputs["trials"]
    service.store.trials.create_plans([dict(trial) for trial in trials])
    first, second = trials[0], trials[1]
    service.store.trials.put_result(
        str(first["trial_id"]),
        {
            "trial_id": str(first["trial_id"]),
            "source_trial_id": "native-1",
            "disposition": "failed",
            "termination": {"reason": "completed", "timings": {"agent_execution_sec": 1.5}},
            "verifier_observation": {"status": "scored", "rewards": {"reward": 0.0},
                                     "evidence_refs": [], "error": None},
            "artifact_refs": [
                {"artifact_id": "harbor/trials/a/trial.log", "kind": "harbor-trial-log",
                 "sha256": "sha256:" + "a" * 64, "size_bytes": 10, "complete": True,
                 "truncated": False},
            ],
            "usage": {"cost_usd": None, "coverage": "unavailable"},
            "coverage": {"items": {"reward": "complete"}, "missing": ["usage"], "partial": []},
            "parser_version": "harbor-terminal-bench-parser@1",
        },
        source_hash="sha256:first", parser_version="harbor-terminal-bench-parser@1",
    )
    service.store.trials.put_result(
        str(second["trial_id"]),
        {
            "trial_id": str(second["trial_id"]),
            "source_trial_id": "native-2",
            "disposition": "indeterminate",
            "termination": {"reason": "verifier_error"},
            "verifier_observation": {"status": "verifier_error", "rewards": {},
                                     "evidence_refs": [], "error": {
                                         "code": "VerifierTimeoutError",
                                         "message": "verifier timed out"}},
            "artifact_refs": [],
            "usage": {"cost_usd": None, "coverage": "unavailable"},
            "coverage": {"items": {"reward": "unavailable"}, "missing": ["reward"], "partial": []},
            "parser_version": "harbor-terminal-bench-parser@1",
        },
        source_hash="sha256:second", parser_version="harbor-terminal-bench-parser@1",
    )

    tasks = client.get(f"/api/v1/runs/{run_id}/tasks").json()
    assert tasks["total"] == 2, "both tasks appear even with partial results"
    row = next(item for item in tasks["items"] if item["planned_trials"] == 2)
    assert row["valid_trials"] == 1
    assert row["invalid_trials"] == 1
    assert row["valid_trial_pass_rate"] == 0.0
    assert row["gate"]["passed"] is False, "coverage 1/2 must not pass a full-coverage gate"

    trial_list = client.get(
        f"/api/v1/runs/{run_id}/tasks/{row['task_key']}/trials",
    ).json()
    assert trial_list["total"] == 2

    # 第二个 Task 有计划但没有结果：计划 Trial 必须以 pending 出现，不消失。
    other = next(item for item in tasks["items"] if item["task_key"] != row["task_key"])
    other_trials = client.get(
        f"/api/v1/runs/{run_id}/tasks/{other['task_key']}/trials",
    ).json()["items"]
    assert [item["disposition"] for item in other_trials] == ["pending", "pending"]
    assert all(item["reward"] is None for item in other_trials)
    assert other["observed_trials"] == 0 and other["valid_trials"] == 0
    assert other["valid_trial_pass_rate"] is None
    assert other["task_pass_reason"] == "no_valid_trial"

    # 第一组 Trial 的详情：有效失败 vs Verifier 错误各自独立。
    first_detail = client.get(f"/api/v1/runs/{run_id}/trials/{trials[0]['trial_id']}").json()
    second_detail = client.get(f"/api/v1/runs/{run_id}/trials/{trials[1]['trial_id']}").json()
    assert first_detail["verifier_observation"]["rewards"] == {"reward": 0.0}
    assert first_detail["disposition"] == "failed"
    assert second_detail["verifier_observation"]["status"] == "verifier_error"
    assert second_detail["verifier_observation"]["rewards"] == {}
    assert second_detail["verifier_observation"]["error"]["code"] == "VerifierTimeoutError"
    assert second_detail["usage"]["cost_usd"] is None, "unknown cost stays null"
    # Trial 切换不串证据：两个 Trial 的 source hash 与引用各自独立。
    assert first_detail["source_hash"] != second_detail["source_hash"]
    assert first_detail["artifacts"] and not second_detail["artifacts"]

    # 迟到的/错误的 Trial 请求不会返回别的 Trial 数据。
    unknown = client.get(f"/api/v1/runs/{run_id}/trials/trial-does-not-exist")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "TRIAL_NOT_FOUND"

    # 跨 Run 归属校验：另一个 Run 的 trial_id 不能从这里读到。
    other_run = service.create_run(
        tb.SCENARIO_VERSION, inputs["manifest"], inputs["case_ids"], run_id="run-other",
    )
    cross = client.get(f"/api/v1/runs/{other_run['id']}/trials/{trials[0]['trial_id']}")
    assert cross.status_code == 404


def test_overview_reports_runner_connection_and_tasks(client: TestClient) -> None:
    """总览：runner 连接状态、任务数、许可与 run 列表。"""
    _prepare(client)
    overview = client.get("/api/v1/benchmarks/terminal-bench").json()
    assert overview["benchmark"] == "terminal-bench"
    assert overview["runner"]["harbor_version"] == "0.23.0"
    assert overview["runner"]["connected"] is False
    assert overview["items"][0]["tasks"] == 2
    assert overview["items"][0]["license_id"] == "Apache-2.0"


def test_planned_trial_ids_are_stable_across_requests(client: TestClient) -> None:
    """同一 Run 的计划 Trial 身份由冻结输入派生，不随请求变化。"""
    _prepare(client)
    from motte_sdk.service import build_run_service
    from motte_sdk import terminalbench as tb
    from motte_contracts.trial import trial_id_for

    service = build_run_service(None)
    record = tb.prepared_dataset(service.store)
    inputs = tb.build_run_inputs(
        record=record, run_id="run-stable", job_id="job-stable",
        profile=tb.terminal_bench_profile(n_trials=1),
    )
    task_key = inputs["case_ids"][0]
    plan = inputs["manifest"]["external_benchmark"]["runner_config"]["plan"]["trials"]
    expected = trial_id_for(
        run_id="run-stable", task_key=task_key, repeat_index=0,
        agent_config_hash=plan[0]["agent_config_hash"],
        environment_hash=plan[0]["environment_hash"],
    )
    assert plan[0]["trial_id"] == expected
    assert len({trial["trial_id"] for trial in plan}) == len(plan)
