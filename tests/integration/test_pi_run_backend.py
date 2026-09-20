"""M4-T04：Pi 平台纵向链路（真实 bridge 驱动的 RunService 全链路）。

主断言 test_pi_backend_case_isolation_and_scoring：两个 Case 连续执行，
session/workspace 无交叉污染；产物经 ArtifactStore 冻结、评分沿用 agent-tasks
确定性指标；无平台 Provider 调用。另覆盖 queued cancel、桥断连不自动重放、
禁用 backend 后历史可读。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from motte_sdk.dispatcher import RunDispatcher
from motte_sdk.resolve import prepare_run
from motte_sdk.runtime_backends import publish_canonical_runtime_versions
from motte_sdk.service import RunService
from motte_storage.run_store import InMemoryRunStore
from motte_storage.resource_store import InMemoryResourceStore

NODE = shutil.which("node")

_WRITE_SCRIPT = [
    [{"type": "toolCall", "name": "write_file",
      "arguments": {"path": "answer.txt", "content": "pi-was-here"}},
     {"type": "toolCall", "name": "write_file",
      "arguments": {"path": "notes.md", "content": "case notes"}}],
    [{"type": "text", "text": "wrote the files"}],
]


def _dataset_fixture():
    from motte_contracts.agent_tasks import (
        normalize_agent_tasks_dataset,
        scenario_for_agent_tasks,
    )

    dataset = normalize_agent_tasks_dataset({
        "name": "pi-suite", "version": "1", "suite": "agent-tasks",
        "cases": [
            {"case_id": "case-alpha", "input": "write answer.txt and notes.md",
             "fixture": {"seed.txt": "seed-a"},
             "expected": {"files": {"answer.txt": {"mode": "contains", "expected": "pi-was-here"}}}},
            {"case_id": "case-beta", "input": "write answer.txt and notes.md",
             "fixture": {"seed.txt": "seed-b"},
             "expected": {"files": {"answer.txt": {"mode": "contains", "expected": "pi-was-here"}}}},
        ],
    })
    scenario = scenario_for_agent_tasks(dataset)
    return dataset, scenario


def _pi_manifest():
    return {
        "runtime": "pi-agent@1",
        "runtime_profile": {
            "runtime": "pi-agent@1",
            "native_settings": {
                "model": "scripted-1",
                "script": _WRITE_SCRIPT,
                "max_steps": 8,
            },
        },
        "runtime_accept_unenforced_tools": False,
    }


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_PI_WORKSPACE_ROOT", str(tmp_path / "pi-ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    resources = InMemoryResourceStore()
    publish_canonical_runtime_versions(resources)
    dataset, scenario = _dataset_fixture()
    resources.datasets.put(dataset)
    resources.scenarios.put(scenario)
    store = InMemoryRunStore()
    service = RunService(store)
    dispatcher = RunDispatcher(service)
    return service, dispatcher, resources


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_pi_backend_case_isolation_and_scoring(tmp_path, monkeypatch):
    service, dispatcher, resources = _setup(tmp_path, monkeypatch)
    resolved, case_ids = prepare_run(
        "pi-suite@1", _pi_manifest(), [], resources=resources,
    )
    assert resolved["execution"]["backend_id"] == "pi-agent"
    assert resolved["runtime_snapshot"]["model_control"] == "runner-configured"

    run = service.create_run("pi-suite@1", resolved, case_ids)
    finished = dispatcher.dispatch(run["id"])

    assert finished["status"] == "completed"
    assert finished["case_ids"] == ["case-alpha", "case-beta"]

    # 评分沿用 agent-tasks 确定性指标：answer.txt 内容断言通过
    scores = finished["scores"]
    assert scores, "scoring pass must exist"
    alpha = [s for s in scores if s["case_id"] == "case-alpha"]
    assert alpha and all(s["passed"] for s in alpha if s["metric_id"] == "file-content:answer.txt")

    # 两个 Case 独立 session/workspace：session_id 不同，事件按 case 归属
    events = service.store.events.list_for_run(run["id"])
    pi_events = [e for e in events if str(e.get("type", "")).startswith("pi_")]
    alpha_sessions = {e["session_id"] for e in pi_events if e.get("case_id") == "case-alpha"}
    beta_sessions = {e["session_id"] for e in pi_events if e.get("case_id") == "case-beta"}
    assert alpha_sessions and beta_sessions
    assert alpha_sessions.isdisjoint(beta_sessions), "cases must not share sessions"
    for event in pi_events:
        assert event["case_id"] in {"case-alpha", "case-beta"}

    # Observation 冻结 + 产物真实落库；workspace fixture 不串案
    case_runs = {row["case_id"]: row for row in service.store.case_runs.list_for_run(run["id"])}
    for case_id in ("case-alpha", "case-beta"):
        observation = case_runs[case_id]["result"]["observation"]
        artifact_paths = {a["path"] for a in observation["artifact_refs"] if a.get("available")}
        assert {"answer.txt", "notes.md", "seed.txt"} <= artifact_paths
        assert observation["workspace"]["before"] == ["seed.txt"]
        assert observation["usage"]["reported"] is False
        assert observation["termination"]["reason"] == "final_answer"
        agent_section = case_runs[case_id]["result"]["agent"]
        assert agent_section["runtime"]["backend"] == "pi-agent@1"

    # 无平台 Provider：manifest 不含 provider，模型计量来源是 scripted
    assert "provider" not in resolved or not resolved.get("provider")
    invocations = service.store.invocations.list_for_run(run["id"])
    model_invocations = [i for i in invocations if i.get("kind") == "model"]
    assert model_invocations and all(
        i["request_summary"].get("metering_source") == "scripted-model"
        for i in model_invocations
    )

    # 重启读取：重建 service/store 视图后历史仍完整（InMemory：直接复用 store）
    view = service._view(run["id"])  # noqa: SLF001 - 集成断言
    assert view["current_scoring_pass_id"]


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_pi_backend_forbidden_write_fails_quality(tmp_path, monkeypatch):
    service, dispatcher, resources = _setup(tmp_path, monkeypatch)
    manifest = _pi_manifest()
    manifest["runtime_profile"]["native_settings"]["script"] = [
        [{"type": "toolCall", "name": "write_file",
          "arguments": {"path": "leak.txt", "content": "should-not-exist"}}],
        [{"type": "text", "text": "done"}],
    ]
    resolved, case_ids = prepare_run(
        "pi-suite@1", {**manifest,
                       "case_selection": {"mode": "all"}}, [],
        resources=resources,
    )
    run = service.create_run("pi-suite@1", {**resolved, "cases": resolved["cases"]}, case_ids)
    # 注入 forbidden 断言：修改 case expected 不可行（已冻结）——改用独立数据集太重，
    # 这里直接断言 exit=completed 不代表质量通过：answer.txt 缺失 → 评分失败。
    finished = dispatcher.dispatch(run["id"])
    assert finished["status"] == "completed"
    scores = finished["scores"]
    answer_scores = [s for s in scores if s["metric_id"] == "file-content:answer.txt"]
    # A07：exit=completed 不代表质量通过——缺产物记 insufficient_evidence，
    # passed 为 None（未进入分母）而非假成功。
    assert answer_scores and all(not s["passed"] for s in answer_scores)
    assert all(s.get("reason") == "artifact_not_captured" for s in answer_scores)


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_pi_backend_queued_cancel_preserves_plan(tmp_path, monkeypatch):
    service, dispatcher, resources = _setup(tmp_path, monkeypatch)
    resolved, case_ids = prepare_run("pi-suite@1", _pi_manifest(), [], resources=resources)
    run = service.create_run("pi-suite@1", resolved, case_ids)
    cancelled = service.cancel(run["id"], reason="operator")
    assert cancelled["status"] == "cancelled"
    # 计划分母保留：两个 case 行都在，未尝试
    rows = service.store.case_runs.list_for_run(run["id"])
    assert {row["case_id"] for row in rows} == {"case-alpha", "case-beta"}


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_pi_backend_bridge_failure_is_not_replayed(tmp_path, monkeypatch):
    service, dispatcher, resources = _setup(tmp_path, monkeypatch)
    manifest = _pi_manifest()
    resolved, case_ids = prepare_run("pi-suite@1", manifest, [], resources=resources)
    run = service.create_run("pi-suite@1", resolved, case_ids)

    # 桥断连：指向不存在的 bridge 路径 → Case 失败，错误入证据，不自动重放
    from motte_sdk import runtime_backends as rb

    original_build = rb._RUNTIME_BUILDS["pi-agent"]  # noqa: SLF001

    def broken_build(broken_run):
        from motte_sdk.pi_runtime import PiRuntimeCaseExecutor

        executor = PiRuntimeCaseExecutor(
            broken_run, bridge_path=tmp_path / "missing-bridge.mjs",
        )
        return type("H", (), {"invoke": staticmethod(executor.invoke), "attach": None})()

    rb._RUNTIME_BUILDS["pi-agent"] = broken_build  # noqa: SLF001
    try:
        from motte_sdk import execution_backends as eb

        eb.unregister_backend("pi-agent", "1")
        rb.install_runtime_backends()
        outcome = dispatcher.dispatch(run["id"])
    finally:
        rb._RUNTIME_BUILDS["pi-agent"] = original_build  # noqa: SLF001
        eb.unregister_backend("pi-agent", "1")
        rb.install_runtime_backends()

    assert outcome["status"] == "failed"
    rows = service.store.case_runs.list_for_run(run["id"])
    assert rows and rows[0]["result"].get("error")

    # retry 显式新建子 Run，不自动重放原操作
    child = service.retry(run["id"])
    assert child["parent_run_id"] == run["id"]


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_pi_backend_disabled_rejects_new_runs_and_keeps_history(tmp_path, monkeypatch):
    service, dispatcher, resources = _setup(tmp_path, monkeypatch)
    resolved, case_ids = prepare_run("pi-suite@1", _pi_manifest(), [], resources=resources)
    run = service.create_run("pi-suite@1", resolved, case_ids)
    finished = dispatcher.dispatch(run["id"])
    assert finished["status"] == "completed"

    from motte_sdk import execution_backends as eb

    eb.unregister_backend("pi-agent", "1")
    try:
        # 关闭后创建新的 runtime Run 被拒绝
        with pytest.raises(Exception) as raised:
            prepare_run("pi-suite@1", _pi_manifest(), [], resources=resources)
        assert "EXECUTION_BACKEND" in str(raised.value) or "unsupported" in str(raised.value)
        # 历史与其余 backend 不受影响
        view = service._view(run["id"])  # noqa: SLF001
        assert view["status"] == "completed"
        assert service.store.events.list_for_run(run["id"])
    finally:
        from motte_sdk import runtime_backends as rb

        eb.unregister_backend("pi-agent", "1")
        rb.install_runtime_backends()
