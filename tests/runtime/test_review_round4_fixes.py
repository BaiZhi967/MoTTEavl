"""M1 review 第四轮（cc56c91）反例回归：逐项覆盖 6 个修复。

编号与 review 反馈一致（#1–#6）。全部离线：scripted provider、本地目录、
子进程 schema worker；不触发任何真实模型或网络。
"""
from __future__ import annotations

import json
import os
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
        "name": "r4-tasks", "version": "1", "cases": cases,
    })
    scenario = scenario_for_agent_tasks(dataset)
    from motte_sdk.agent_tasks import resolve_agent_tasks_manifest

    resources = SimpleNamespace(
        datasets=SimpleNamespace(
            get=lambda name, version: dataset
            if f"{name}@{version}" == "r4-tasks@1" else None,
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
    return resolve_execution("r4-tasks@1", resolved, scenario=scenario)


class Scripted:
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
    import motte_sdk.agent_backend as agent_backend
    from motte_sdk.dispatcher import RunDispatcher

    original = agent_backend.build_agent_provider
    agent_backend.build_agent_provider = lambda m: provider
    try:
        run = service.create_run("r4-tasks@1", manifest, case_ids)
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


# ---------------------------------------------------------------- #1 结算异常出线程

def test_settle_failure_in_worker_thread_quarantines(tmp_path, monkeypatch):
    """启用调用超时后，Provider 正常返回但 settled 落盘失败：
    异常必须从后台线程带回主线程——中止执行并 needs_review，不得照常成功。"""
    service = RunService(InMemoryRunStore())
    cases = [{"case_id": "case-1", "input": "quick call", "fixture": {}, "expected": {}}]
    manifest = _manifest(
        tmp_path, cases, budget={"per_call_timeout_sec": 30, "max_steps": 2},
    )
    scripted = Scripted([{"content": "ok", "tool_calls": []}])

    # 存储层注入：第一次 settled 转换（model invocation 的成功结算）失败
    invocations_repo = service.store.invocations
    original_transition = invocations_repo.transition

    def flaky_transition(invocation_id, **kwargs):  # noqa: ANN001
        if kwargs.get("status") == "settled":
            raise OSError("simulated invocation store outage")
        return original_transition(invocation_id, **kwargs)

    monkeypatch.setattr(invocations_repo, "transition", flaky_transition)

    run_id, result = _execute(service, manifest, scripted, ["case-1"])
    assert result["status"] == "needs_review", json.dumps(result.get("error"), ensure_ascii=False)
    error_blob = json.dumps(result.get("error") or {}, ensure_ascii=False)
    assert "invocation settle failed" in error_blob
    # Run 不是 completed：执行被中止，无第二次模型调用
    assert len(scripted.requests) == 1
    # invocation 保留 dispatching 证据（settle 确实失败过）
    invocations = service.store.invocations.list_for_run(run_id)
    assert any(item["status"] == "dispatching" for item in invocations)


# ---------------------------------------------------------------- #2 中文相邻密钥

def test_redaction_boundaries_cover_cjk_and_filenames():
    from motte_trace.redaction import redact_text

    # 中文相邻文本中的密钥必须命中（\b 会因中文也算单词字符而漏掉）
    assert SECRET not in redact_text(f"密钥是{SECRET}，不要外传")
    assert "[REDACTED-SECRET]" in redact_text(f"密钥是{SECRET}，不要外传")
    # 普通文件名 / 内嵌形状不误伤
    assert redact_text("task-response.txt") == "task-response.txt"
    assert redact_text("mask-abcdefghijklmnop") == "mask-abcdefghijklmnop"
    # 独立 token 形态照旧命中
    assert "[REDACTED-SECRET]" in redact_text(f"leaked {SECRET}")
    assert SECRET not in redact_text(f"leaked {SECRET}")


def test_cjk_adjacent_secret_redacted_through_api(tmp_path, monkeypatch):
    import motte_sdk.agent_backend as agent_backend

    service = RunService(InMemoryRunStore())
    cases = [{"case_id": "case-1", "input": "Echo the secret.", "fixture": {}, "expected": {}}]
    manifest = _manifest(tmp_path, cases)
    scripted = Scripted([{"content": f"密钥是{SECRET}", "tool_calls": []}])
    monkeypatch.setattr(agent_backend, "build_agent_provider", lambda m: scripted)
    run_id, result = _execute(service, manifest, scripted, ["case-1"])
    assert result["status"] == "completed"

    app = create_app(service.store, InMemoryResourceStore())
    client = TestClient(app)
    view = client.get(f"/api/v1/runs/{run_id}").json()
    blob = json.dumps(view, ensure_ascii=False)
    assert SECRET not in blob
    assert "[REDACTED-SECRET]" in blob


# ---------------------------------------------------------------- #3 路径别名绕过

def test_path_alias_write_still_forbidden(tmp_path):
    """./locked.txt 覆盖再恢复：按 workspace 同源规范化匹配，仍判违规。"""
    service = RunService(InMemoryRunStore())
    cases = [{
        "case_id": "case-1", "input": "touch locked.txt via alias then restore",
        "fixture": {"locked.txt": "original"},
        "expected": {}, "forbidden_paths": ["locked.txt"],
    }]
    manifest = _manifest(tmp_path, cases)
    scripted = Scripted([
        {"content": "", "tool_calls": [
            _tool_call("c1", "write_file", {"path": "./locked.txt", "content": "tampered"}),
        ]},
        {"content": "", "tool_calls": [
            _tool_call("c2", "write_file", {"path": "./locked.txt", "content": "original"}),
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
    assert any(violation.startswith("locked.txt ")
               for violation in forbidden["details"]["violations"])


# ---------------------------------------------------------------- #4 证据缺口不吞违规

def test_recorded_violation_survives_capture_gap(tmp_path, monkeypatch):
    """覆盖并恢复禁止文件后 Artifact 写入失败（coverage 不完整）：
    已记录的违规写入仍然判违规，不得因其它证据缺失而通过。"""
    from motte_storage.artifacts import ArtifactStore

    def broken_put_bytes(self, artifact_id, data, **kwargs):  # noqa: ANN001
        raise OSError("simulated artifact outage")

    monkeypatch.setattr(ArtifactStore, "put_bytes", broken_put_bytes)

    service = RunService(InMemoryRunStore())
    cases = [{
        "case_id": "case-1", "input": "touch locked.txt then restore",
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

    # Observation 确实不完整（采集失败被如实记录）
    row = service.store.case_runs.get(run_id, "case-1")
    assert row["result"]["observation"]["coverage"]["complete"] is False

    scores = {
        (score["case_id"], score["metric_id"]): score
        for score in result.get("scores") or []
    }
    forbidden = scores[("case-1", "no-forbidden-write")]
    assert forbidden["passed"] is False, forbidden
    assert forbidden["reason"] == "forbidden_write_detected"


# ---------------------------------------------------------------- #5 批量校验期限

def test_tool_args_schema_batch_respects_eval_deadline():
    """多次 args_schema 校验不得重复领用同一份剩余期限：
    全局 1s 耗尽后停止并报 eval_deadline_exceeded，而不是 3s+ 仍通过。"""
    from motte_contracts.evaluation import (
        FrozenObservation,
        MetricStatus,
        ToolCallRecord,
        observation_evidence_hash,
    )
    from motte_eval.observation import evaluate_observation, normalize_evaluator_config

    calls = [
        ToolCallRecord(call_id=f"c-{index}", tool_name="write_file",
                       arguments={"path": f"out-{index}.txt", "content": "x"},
                       status="succeeded", step=1)
        for index in range(25)
    ]
    payload: dict = {
        "observation_id": "obs-1", "run_id": "run-1", "case_id": "case-1",
        "final_output": "done", "termination": {"reason": "final_answer", "detail": None},
        "event_refs": [], "artifact_refs": [],
        "coverage": {"complete": True, "events_captured": len(calls),
                     "artifacts_captured": 0, "artifacts_expected": 0, "missing": []},
        "usage": {"reported": False}, "tool_calls": calls, "workspace": None,
        "processes": [],
    }
    payload["evidence_hash"] = observation_evidence_hash(payload)
    payload["recorded_at"] = "2026-09-19T00:00:00+00:00"
    observation = FrozenObservation.model_validate(payload)
    config = normalize_evaluator_config({
        "metrics": [{
            "metric_id": "args", "kind": "tool-call", "tool": "write_file",
            "args_schema": {"type": "object",
                            "properties": {"path": {"type": "string"},
                                           "content": {"type": "string"}}},
        }],
        "eval_deadline_sec": 1.0,
    })
    started = time.monotonic()
    results = evaluate_observation(observation, config)
    elapsed = time.monotonic() - started
    metric = next(item for item in results if item.metric_id == "args")
    assert metric.status is MetricStatus.evaluator_error
    assert metric.reason in {"eval_deadline_exceeded", "schema_timeout"}
    assert metric.passed is None
    assert elapsed < 3.0, "batch validation must stop at the global deadline"


# ---------------------------------------------------------------- #6 创建副作用

def test_workspace_creation_has_no_side_effect_in_victim(tmp_path):
    """预置 ws/run-1 → victim 时：拒绝创建，且不在 victim 里创建任何目录。"""
    from motte_sandbox.workspace import CaseWorkspace, WorkspacePolicyError

    base = tmp_path / "ws"
    base.mkdir()
    victim = tmp_path / "victim-dir"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")

    os.symlink(victim, base / "run-1")
    with pytest.raises(WorkspacePolicyError):
        CaseWorkspace(base / "run-1" / "case-1", anchor=base)

    # 拒绝的同时不留下越界副作用：victim 内容不变
    assert sorted(item.name for item in victim.iterdir()) == ["keep.txt"]
    assert not (base / "run-1" / "case-1").exists() or (base / "run-1").is_symlink()

    # 中间目录为 symlink 的深层路径同样无副作用
    run2 = base / "run-2"
    run2.mkdir()
    os.symlink(victim, run2 / "case-x")
    with pytest.raises(WorkspacePolicyError):
        CaseWorkspace(run2 / "case-x" / "deep" / "leaf", anchor=base)
    assert sorted(item.name for item in victim.iterdir()) == ["keep.txt"]


def test_workspace_nested_chain_created_level_by_level(tmp_path):
    """缺失的多级目录逐级安全创建，正常路径不受影响。"""
    from motte_sandbox.workspace import CaseWorkspace

    base = tmp_path / "ws"
    workspace = CaseWorkspace(base / "run-1" / "case-1", anchor=base)
    workspace.write_text("nested/out.txt", "v")
    assert workspace.read_text("nested/out.txt") == "v"
    assert workspace.cleanup() == {"status": "success", "residual": []}
