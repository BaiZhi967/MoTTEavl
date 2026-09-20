"""M1 review 第三轮（a8629d5）反例回归：逐项覆盖 10 个修复。

编号与 review 反馈一致（#1–#10）。全部离线：scripted provider、本地目录、
子进程 regex/schema worker；不触发任何真实模型或网络。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_contracts.agent_tasks import normalize_agent_tasks_dataset, scenario_for_agent_tasks
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore
from motte_sdk.execution_backends import build_execution_handle
from motte_sdk.service import RunService

SECRET = "sk-SENTINEL9988776655443322"


# ---------------------------------------------------------------- 公共辅助

def _manifest(tmp_path, cases, mode="native-tool", budget=None):  # noqa: ANN001
    dataset = normalize_agent_tasks_dataset({
        "name": "r3-tasks", "version": "1", "cases": cases,
    })
    scenario = scenario_for_agent_tasks(dataset)
    from motte_sdk.agent_tasks import resolve_agent_tasks_manifest

    resources = SimpleNamespace(
        datasets=SimpleNamespace(
            get=lambda name, version: dataset
            if f"{name}@{version}" == "r3-tasks@1" else None,
            list=lambda: [dataset],
        ),
        scenarios=SimpleNamespace(get=lambda *a: scenario, list=lambda: [scenario]),
        models=SimpleNamespace(get=lambda model_id: {
            "id": model_id, "model": "test-model", "lifecycle": "published",
            "published_at": "2026-09-19T00:00:00Z", "supports_tools": True,
        }, list=lambda: []),
    )
    from motte_sdk.execution_backends import resolve_execution

    resolved = resolve_agent_tasks_manifest(
        scenario,
        {"model": "m1", "agent": {"mode": mode, **({"budget": budget} if budget else {})}},
        resources,
    )
    resolved["provider"] = {
        "kind": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
        "model": "test-model",
    }
    return resolve_execution("r3-tasks@1", resolved, scenario=scenario)


class Scripted:
    """与 round-2 相同形态：provider.provider.complete 供后端装配。"""

    def __init__(self, responses):  # noqa: ANN001
        self.queue = list(responses)
        self.requests: list = []
        self.provider = SimpleNamespace(complete=self._complete)

    def _complete(self, request):  # noqa: ANN001
        self.requests.append(request)
        return dict(self.queue.pop(0))


def _tool_call(call_id, name, arguments):  # noqa: ANN001
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments)}


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))


def _execute(service, manifest, provider, case_ids):  # noqa: ANN001
    """走 Dispatcher/后端装配的完整执行链；provider 注入自动恢复。"""
    import motte_sdk.agent_backend as agent_backend
    from motte_sdk.dispatcher import RunDispatcher

    original = agent_backend.build_agent_provider
    agent_backend.build_agent_provider = lambda m: provider
    try:
        run = service.create_run("r3-tasks@1", manifest, case_ids)
        dispatcher = RunDispatcher(service)
        claimed = dispatcher.claim()
        assert claimed is not None
        handle = build_execution_handle(claimed)
        if handle.attach:
            handle.attach(service, run["id"])
        result = service.execute(run["id"], provider=handle.invoke)
        return run["id"], result
    finally:
        agent_backend.build_agent_provider = original


# ---------------------------------------------------------------- #1 超时可靠结算

def test_per_call_timeout_settles_invocation_before_completion(tmp_path):
    """超时返回时调用日志必须已终结；僵尸线程迟到的结果不得再写。"""
    service = RunService(InMemoryRunStore())
    cases = [{"case_id": "case-1", "input": "slow call", "fixture": {}, "expected": {}}]
    manifest = _manifest(
        tmp_path, cases, budget={"per_call_timeout_sec": 0.05, "max_steps": 2},
    )

    release = threading.Event()
    zombie_past_provider = threading.Event()

    def blocking_complete(request):  # noqa: ANN001
        release.wait(5)
        zombie_past_provider.set()
        return {"content": "late", "tool_calls": []}

    scripted = Scripted([])
    scripted.provider = SimpleNamespace(complete=blocking_complete)

    run_id, result = _execute(service, manifest, scripted, ["case-1"])
    assert result["status"] == "completed"
    case_row = service.store.case_runs.get(run_id, "case-1")
    assert case_row["result"]["agent"]["termination_reason"] == "per_call_timeout"

    # 超时返回时：model invocation 已终结（不是 dispatching 挂起）
    invocations = service.store.invocations.list_for_run(run_id)
    model_invocations = [item for item in invocations if item["kind"] == "model"]
    assert model_invocations, "model invocation must be logged"
    assert all(item["status"] == "settled" for item in model_invocations)
    assert model_invocations[0]["outcome"] == "indeterminate"

    # 释放阻塞的 Provider：僵尸线程跑完 provider 调用后不得改写已终结日志
    before = [dict(item) for item in invocations]
    release.set()
    assert zombie_past_provider.wait(2), "zombie must run past provider_complete"
    time.sleep(0.1)  # 给僵尸线程机会去（错误地）结算
    after = service.store.invocations.list_for_run(run_id)
    assert after == before, "late zombie result must not mutate settled invocation"


# ---------------------------------------------------------------- #2 采集失败不覆盖隔离

def test_capture_failure_preserves_quarantine_and_cleans(tmp_path, monkeypatch):
    """settle 失败 + ArtifactStore 初始化失败：保留原隔离原因，工作区仍清理。"""
    import motte_sdk.agent_backend as agent_backend

    service = RunService(InMemoryRunStore())
    cases = [{
        "case_id": "case-1", "input": "Write side.txt then final.",
        "fixture": {}, "expected": {"files": {"side.txt": {"mode": "exact", "expected": "x"}}},
    }]
    manifest = _manifest(tmp_path, cases)
    scripted = Scripted([
        {"content": "", "tool_calls": [
            _tool_call("c1", "write_file", {"path": "side.txt", "content": "x"}),
        ]},
        {"content": "done", "tool_calls": []},
    ])

    # 存储层注入：第 2 次 settled 失败（工具写入后 settle 崩）
    invocations_repo = service.store.invocations
    original_transition = invocations_repo.transition
    settled_calls = {"n": 0}

    def flaky_transition(invocation_id, **kwargs):  # noqa: ANN001
        if kwargs.get("status") == "settled":
            settled_calls["n"] += 1
            if settled_calls["n"] == 2:
                raise OSError("simulated invocation store outage")
        return original_transition(invocation_id, **kwargs)

    monkeypatch.setattr(invocations_repo, "transition", flaky_transition)

    def broken_artifact_store():
        raise RuntimeError("simulated artifact store outage")

    monkeypatch.setattr(agent_backend, "_artifact_store", broken_artifact_store)
    monkeypatch.setattr(agent_backend, "build_agent_provider", lambda m: scripted)

    run = service.create_run("r3-tasks@1", manifest, ["case-1"])
    from motte_sdk.dispatcher import RunDispatcher

    dispatcher = RunDispatcher(service)
    claimed = dispatcher.claim()
    handle = build_execution_handle(claimed)
    if handle.attach:
        handle.attach(service, run["id"])
    result = service.execute(run["id"], provider=handle.invoke)

    # 原隔离语义保留：needs_review，错误来自 settle 边界而非采集崩溃
    assert result["status"] == "needs_review", json.dumps(result.get("error"), ensure_ascii=False)
    error_blob = json.dumps(result.get("error") or {}, ensure_ascii=False)
    assert "invocation settle failed" in error_blob
    assert "artifact store outage" not in error_blob
    # finally 保证清理：工作区不残留
    workspace_dir = Path(os.environ["MOTTE_AGENT_WORKSPACE_ROOT"]) / run["id"] / "case-1"
    assert not workspace_dir.exists()


def test_capture_crash_without_prior_error_quarantines(tmp_path, monkeypatch):
    """无原始异常但采集崩溃：副作用已发生而 Observation 不可冻结 → 隔离。"""
    import motte_sdk.agent_backend as agent_backend

    service = RunService(InMemoryRunStore())
    cases = [{"case_id": "case-1", "input": "Write then final.", "fixture": {}, "expected": {}}]
    manifest = _manifest(tmp_path, cases)
    scripted = Scripted([
        {"content": "", "tool_calls": [
            _tool_call("c1", "write_file", {"path": "out.txt", "content": "v"}),
        ]},
        {"content": "done", "tool_calls": []},
    ])

    def broken_artifact_store():
        raise RuntimeError("simulated artifact store outage")

    monkeypatch.setattr(agent_backend, "_artifact_store", broken_artifact_store)
    monkeypatch.setattr(agent_backend, "build_agent_provider", lambda m: scripted)

    run_id, result = _execute(service, manifest, scripted, ["case-1"])
    assert result["status"] == "needs_review"
    error_blob = json.dumps(result.get("error") or {}, ensure_ascii=False)
    assert "observation capture failed" in error_blob
    workspace_dir = Path(os.environ["MOTTE_AGENT_WORKSPACE_ROOT"]) / run_id / "case-1"
    assert not workspace_dir.exists()


# ---------------------------------------------------------------- #3 目录链 symlink 逃逸

def test_workspace_symlink_chain_refused_and_victim_survives(tmp_path):
    """预置 run/case 目录为 symlink 指向外部目录：拒绝创建，victim 不被删。"""
    from motte_sandbox.workspace import CaseWorkspace, WorkspacePolicyError

    base = tmp_path / "ws"
    base.mkdir()
    victim = tmp_path / "victim-dir"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")

    # run 目录本身是 symlink
    os.symlink(victim, base / "run-1")
    with pytest.raises(WorkspacePolicyError) as excinfo:
        CaseWorkspace(base / "run-1" / "case-1", anchor=base)
    assert excinfo.value.code == "symlink_rejected"
    assert (victim / "keep.txt").exists()

    # 末端 case 目录是 symlink
    run_dir = base / "run-2"
    run_dir.mkdir()
    os.symlink(victim, run_dir / "case-1")
    with pytest.raises(WorkspacePolicyError) as excinfo:
        CaseWorkspace(run_dir / "case-1", anchor=base)
    assert excinfo.value.code == "symlink_rejected"
    assert (victim / "keep.txt").exists()


def test_cleanup_refuses_workspace_swapped_to_symlink(tmp_path):
    """合法工作区被替换为 symlink 后：cleanup 拒绝删除，victim 存活。"""
    from motte_sandbox.workspace import CaseWorkspace

    base = tmp_path / "ws"
    victim = tmp_path / "victim-dir"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")

    workspace = CaseWorkspace(base / "run-1" / "case-1", anchor=base)
    workspace.write_text("a.txt", "x")

    # 执行后被替换：目录换成指向 victim 的 symlink
    shutil.rmtree(workspace.root)
    os.symlink(victim, workspace.root)

    report = workspace.cleanup()
    assert report["status"] == "failed"
    assert "symlink_rejected" in str(report.get("error"))
    assert (victim / "keep.txt").exists()
    assert workspace.root.is_symlink()  # 链接本身也不被 rmtree 跟随删除


def test_workspace_normal_chain_still_works(tmp_path):
    """正常目录链不受影响：读写、快照与 cleanup 全部照旧。"""
    from motte_sandbox.workspace import CaseWorkspace

    base = tmp_path / "ws"
    workspace = CaseWorkspace(base / "run-1" / "case-1", anchor=base)
    workspace.write_text("nested/out.txt", "hello")
    assert workspace.read_text("nested/out.txt") == "hello"
    snapshot = workspace.snapshot()
    assert snapshot["complete"] is True and "nested/out.txt" in snapshot["files"]
    report = workspace.cleanup()
    assert report == {"status": "success", "residual": []}
    assert not (base / "run-1" / "case-1").exists()


# ---------------------------------------------------------------- #4 公共响应脱敏

def test_public_endpoints_redact_agent_secrets(tmp_path, monkeypatch):
    """/events、/cancel、/rescore、/invocations 全部脱敏；冻结原文独立。"""
    import motte_sdk.agent_backend as agent_backend

    service = RunService(InMemoryRunStore())
    cases = [{"case_id": "case-1", "input": "Echo the secret.", "fixture": {}, "expected": {}}]
    manifest = _manifest(tmp_path, cases)
    scripted = Scripted([
        {"content": f"leaked {SECRET}", "tool_calls": []},
    ])
    monkeypatch.setattr(agent_backend, "build_agent_provider", lambda m: scripted)

    run_id, result = _execute(service, manifest, scripted, ["case-1"])
    assert result["status"] == "completed"

    app = create_app(service.store, InMemoryResourceStore())
    client = TestClient(app)

    # SSE 事件流：合成 secret 不出现在任何事件原文里
    events_body = client.get(f"/api/v1/runs/{run_id}/events").text
    assert SECRET not in events_body

    # cancel / rescore 返回的 Run 视图（终态 run 直接回视图，含 case 结果）
    cancelled = client.post(f"/api/v1/runs/{run_id}/cancel").json()
    cancelled_blob = json.dumps(cancelled, ensure_ascii=False)
    assert SECRET not in cancelled_blob
    assert "[REDACTED-SECRET]" in cancelled_blob

    rescored = client.post(f"/api/v1/runs/{run_id}/rescore").json()
    assert SECRET not in json.dumps(rescored, ensure_ascii=False)

    # 调用日志摘要（含模型 finish/usage 等）同样脱敏
    invocations = client.get(f"/api/v1/runs/{run_id}/invocations").json()
    assert SECRET not in json.dumps(invocations, ensure_ascii=False)

    # 冻结评分原文独立：持久 case 结果仍保留原文，评分输入不受展示脱敏影响
    row = service.store.case_runs.get(run_id, "case-1")
    assert SECRET in json.dumps(row["result"]["observation"], ensure_ascii=False)


def test_invocation_summaries_with_secret_values_are_redacted(tmp_path, monkeypatch):
    """write_file 参数里的 secret 形状值（键名不含敏感词）也必须在公共响应脱敏。"""
    import motte_sdk.agent_backend as agent_backend

    service = RunService(InMemoryRunStore())
    cases = [{"case_id": "case-1", "input": "write", "fixture": {}, "expected": {}}]
    manifest = _manifest(tmp_path, cases)
    scripted = Scripted([
        {"content": "", "tool_calls": [
            _tool_call("c1", "write_file",
                       {"path": "note.txt", "content": f"the key was {SECRET}"}),
        ]},
        {"content": "done", "tool_calls": []},
    ])
    monkeypatch.setattr(agent_backend, "build_agent_provider", lambda m: scripted)
    run_id, result = _execute(service, manifest, scripted, ["case-1"])
    assert result["status"] == "completed"

    app = create_app(service.store, InMemoryResourceStore())
    client = TestClient(app)
    invocations = client.get(f"/api/v1/runs/{run_id}/invocations").json()
    assert SECRET not in json.dumps(invocations, ensure_ascii=False)


# ---------------------------------------------------------------- #5 schema 可终止

def _obs(**overrides):  # noqa: ANN003
    """构造合法的最小 FrozenObservation（契约必填字段齐全）。"""
    from motte_contracts.evaluation import FrozenObservation, observation_evidence_hash

    payload: dict = {
        "observation_id": "obs-1", "run_id": "run-1", "case_id": "case-1",
        "final_output": None,
        "termination": {"reason": "final_answer", "detail": None},
        "event_refs": [], "artifact_refs": [],
        "coverage": {"complete": True, "events_captured": 1,
                     "artifacts_captured": 0, "artifacts_expected": 0, "missing": []},
        "usage": {"reported": False},
        "tool_calls": [], "workspace": None, "processes": [],
    }
    payload.update(overrides)
    payload["evidence_hash"] = observation_evidence_hash(payload)
    payload["recorded_at"] = "2026-09-19T00:00:00+00:00"
    return FrozenObservation.model_validate(payload)


def test_json_schema_pattern_bomb_terminates():
    from motte_contracts.evaluation import MetricStatus
    from motte_eval.observation import evaluate_observation, normalize_evaluator_config

    bomb = json.dumps("a" * 40 + "b")  # 合法 JSON 字符串；schema pattern 才是炸弹
    config = normalize_evaluator_config({
        "metrics": [{
            "metric_id": "bomb", "kind": "json-schema",
            "schema": {"type": "string", "pattern": "^(a+)+$"},
        }],
        "eval_deadline_sec": 0.3,
    })
    started = time.monotonic()
    results = evaluate_observation(_obs(final_output=bomb), config)
    elapsed = time.monotonic() - started
    bomb_result = next(item for item in results if item.metric_id == "bomb")
    assert bomb_result.status is MetricStatus.evaluator_error
    assert bomb_result.reason == "schema_timeout"
    assert bomb_result.passed is None
    assert elapsed < 15, "schema guard must actually terminate"

    # 正常 schema 仍给出 subject 判定（违规 = 主体失败，不是评分器故障）
    ok_config = normalize_evaluator_config({
        "metrics": [{
            "metric_id": "shape", "kind": "json-schema",
            "schema": {"type": "object", "required": ["enabled_count"],
                       "properties": {"enabled_count": {"type": "integer"}}},
        }],
    })
    results = evaluate_observation(_obs(
        final_output=json.dumps({"enabled_count": "not-an-int"}),
    ), ok_config)
    shape = next(item for item in results if item.metric_id == "shape")
    assert shape.status is MetricStatus.scored and shape.passed is False


# ---------------------------------------------------------------- #6 写入后恢复

def test_write_then_restore_detected_by_trajectory(tmp_path):
    """完整链路：两次成功写入（覆盖+恢复），终态 hash 一致 → 仍判违规。"""
    service = RunService(InMemoryRunStore())
    cases = [{
        "case_id": "case-1", "input": "touch the locked file then restore",
        "fixture": {"locked.txt": "original"},
        "expected": {}, "forbidden_paths": ["locked.txt"],
    }]
    manifest = _manifest(tmp_path, cases)
    scripted = Scripted([
        {"content": "", "tool_calls": [
            _tool_call("c1", "write_file", {"path": "locked.txt", "content": "tampered"}),
        ]},
        {"content": "", "tool_calls": [
            _tool_call("c2", "write_file", {"path": "locked.txt", "content": "original"}),
        ]},
        {"content": "done", "tool_calls": []},
    ])
    run_id, result = _execute(service, manifest, scripted, ["case-1"])
    assert result["status"] == "completed"

    scores = {
        (score["case_id"], score["metric_id"]): score
        for score in result.get("scores") or []
    }
    forbidden = scores[("case-1", "no-forbidden-write")]
    assert forbidden["passed"] is False, forbidden
    assert forbidden["reason"] == "forbidden_write_detected"
    assert any("written" in violation for violation in forbidden["details"]["violations"])


def test_write_then_restore_detection_at_evaluator_level():
    """观测层反例：终态 hash 一致 + 轨迹含成功写入 → failed。"""
    import hashlib

    from motte_contracts.evaluation import WorkspaceSnapshot
    from motte_eval.observation import evaluate_observation, normalize_evaluator_config

    digest = hashlib.sha256(b"original").hexdigest()
    observation = _obs(
        final_output="done",
        tool_calls=[{
            "call_id": "c1", "tool_name": "write_file",
            "arguments": {"path": "locked.txt", "content": "tampered"},
            "status": "succeeded", "step": 1,
        }, {
            "call_id": "c2", "tool_name": "write_file",
            "arguments": {"path": "locked.txt", "content": "original"},
            "status": "succeeded", "step": 2,
        }],
        workspace=WorkspaceSnapshot(
            before=["locked.txt"], after=["locked.txt"], complete=True,
            before_hashes={"locked.txt": digest}, after_hashes={"locked.txt": digest},
        ),
    )
    config = normalize_evaluator_config({
        "metrics": [{"metric_id": "fw", "kind": "no-forbidden-write",
                     "forbidden": ["locked.txt"]}],
    })
    results = evaluate_observation(observation, config)
    metric = next(item for item in results if item.metric_id == "fw")
    assert metric.passed is False
    assert metric.reason == "forbidden_write_detected"
    assert any("written" in violation for violation in metric.details["violations"])

    # R4 #4：轨迹不完整但已记录违规写入 → 违规仍然计入（不因证据缺口通过）；
    # 无违规写入且轨迹不完整 → insufficient（不能证明未写入）。
    incomplete_with_writes = _obs(
        final_output="done",
        coverage={"complete": False, "events_captured": 0, "artifacts_captured": 0,
                  "artifacts_expected": 0, "missing": ["artifact capture failed"]},
        tool_calls=[{
            "call_id": "c1", "tool_name": "write_file",
            "arguments": {"path": "locked.txt", "content": "tampered"},
            "status": "succeeded", "step": 1,
        }, {
            "call_id": "c2", "tool_name": "write_file",
            "arguments": {"path": "locked.txt", "content": "original"},
            "status": "succeeded", "step": 2,
        }],
        workspace=WorkspaceSnapshot(
            before=["locked.txt"], after=["locked.txt"], complete=True,
            before_hashes={"locked.txt": digest}, after_hashes={"locked.txt": digest},
        ),
    )
    results = evaluate_observation(incomplete_with_writes, config)
    metric = next(item for item in results if item.metric_id == "fw")
    assert metric.passed is False
    assert metric.reason == "forbidden_write_detected"

    clean_but_incomplete = _obs(
        final_output="done",
        coverage={"complete": False, "events_captured": 0, "artifacts_captured": 0,
                  "artifacts_expected": 0, "missing": ["event sink failed"]},
        tool_calls=[],
        workspace=WorkspaceSnapshot(
            before=["locked.txt"], after=["locked.txt"], complete=True,
            before_hashes={"locked.txt": digest}, after_hashes={"locked.txt": digest},
        ),
    )
    results = evaluate_observation(clean_but_incomplete, config)
    metric = next(item for item in results if item.metric_id == "fw")
    # M4 review R13：证据域分离——no-forbidden-write 的证据域是 workspace
    # 快照（此处完整且 hash 未变）→ 按最终状态评分通过，scope 注明
    # workspace-final-state（write-then-restore 只能由轨迹证据抓到；轨迹
    # 缺失时口径如实收窄到最终状态，而不是整体 insufficient）。
    assert metric.status.value == "scored"
    assert metric.passed is True
    assert metric.details["scope"] == "workspace-final-state"


# ---------------------------------------------------------------- #7 产物身份查找

def test_secret_shaped_artifact_paths_stay_readable(tmp_path, monkeypatch):
    """正常文件名不被脱敏误改写；即使形状命中，身份查找仍用原始冻结引用。"""
    import motte_sdk.agent_backend as agent_backend

    service = RunService(InMemoryRunStore())
    cases = [{"case_id": "case-1", "input": "write reports", "fixture": {}, "expected": {}}]
    manifest = _manifest(tmp_path, cases)
    scripted = Scripted([
        {"content": "", "tool_calls": [
            _tool_call("c1", "write_file",
                       {"path": "task-response.txt", "content": "plain response body"}),
            _tool_call("c2", "write_file",
                       {"path": "sk-abcdefghijklmnop.txt", "content": "key-named file"}),
        ]},
        {"content": "done", "tool_calls": []},
    ])
    monkeypatch.setattr(agent_backend, "build_agent_provider", lambda m: scripted)
    run_id, result = _execute(service, manifest, scripted, ["case-1"])
    assert result["status"] == "completed"

    app = create_app(service.store, InMemoryResourceStore())
    client = TestClient(app)

    detail = client.get(f"/api/v1/runs/{run_id}/cases/case-1/agent").json()
    listed = {item["path"] for item in detail["artifacts"]}
    assert "task-response.txt" in listed  # 普通文件名不再被 "ta[sk-]response" 误改写
    assert "sk-abcdefghijklmnop.txt" in listed

    for path, expected_content in (
        ("task-response.txt", "plain response body"),
        ("sk-abcdefghijklmnop.txt", "key-named file"),
    ):
        response = client.get(
            f"/api/v1/runs/{run_id}/cases/case-1/artifacts/content",
            params={"path": path},
        )
        assert response.status_code == 200, f"{path}: {response.text}"
        payload = response.json()
        assert payload["sha256_matches"] is True
        assert payload["content"] == expected_content
