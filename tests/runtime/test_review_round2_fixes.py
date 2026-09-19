"""M1 review 第二轮（fcf4508）反例回归：逐项覆盖 18 个修复。

编号与 review 反馈一致（#1–#18）。全部离线：scripted provider、本地目录与
假 schema URL；不触发任何真实模型或网络。
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_contracts.agent_tasks import normalize_agent_tasks_dataset, scenario_for_agent_tasks
from motte_contracts.evaluation import (
    FrozenObservation,
    WorkspaceSnapshot,
    observation_evidence_hash,
)
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore
from motte_sdk.execution_backends import build_execution_handle
from motte_sdk.service import RunService

SECRET = "sk-SENTINEL9988776655443322"


# ---------------------------------------------------------------- 公共辅助

def _manifest(tmp_path, cases, mode="native-tool", budget=None):  # noqa: ANN001
    dataset = normalize_agent_tasks_dataset({
        "name": "r2-tasks", "version": "1", "cases": cases,
    })
    scenario = scenario_for_agent_tasks(dataset)
    from motte_sdk.agent_tasks import resolve_agent_tasks_manifest

    resources = SimpleNamespace(
        datasets=SimpleNamespace(
            get=lambda name, version: dataset
            if f"{name}@{version}" == "r2-tasks@1" else None,
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
        scenario, {"model": "m1", "agent": {"mode": mode, **({"budget": budget} if budget else {})}},
        resources,
    )
    resolved["provider"] = {
        "kind": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
        "model": "test-model",
    }
    return resolve_execution("r2-tasks@1", resolved, scenario=scenario)


class Scripted:
    def __init__(self, responses):  # noqa: ANN001
        self.queue = list(responses)
        self.requests: list = []
        self.provider = SimpleNamespace(complete=self._complete)

    def _complete(self, request):  # noqa: ANN001
        self.requests.append(request)
        return dict(self.queue.pop(0))

    @property
    def max_output_tokens_seen(self):  # noqa: ANN201
        return [request.max_output_tokens for request in self.requests]


def _tool_call(call_id, name, arguments):  # noqa: ANN001
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments)}


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))


def _execute(service, manifest, provider, case_ids):  # noqa: ANN001
    run = service.create_run("r2-tasks@1", manifest, case_ids)
    import motte_sdk.agent_backend as agent_backend

    original = agent_backend.build_agent_provider
    agent_backend.build_agent_provider = lambda m: provider
    try:
        from motte_sdk.dispatcher import RunDispatcher

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


# ---------------------------------------------------------------- #1 路径逃逸

def test_case_id_cannot_escape_workspace_root(tmp_path):
    """case_id 为绝对路径/穿越串时：创建期拒绝，cleanup 永不触碰外部目录。"""
    victim = tmp_path / "victim-dir"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")

    cases = [{
        "case_id": str(victim),  # 绝对路径作为 case_id
        "input": "x", "fixture": {},
    }]
    with pytest.raises(ValueError, match="case_id"):
        normalize_agent_tasks_dataset({"name": "evil", "version": "1", "cases": cases})
    for bad in ("../escape", "a/b", "..", ".hidden", "x" * 200, ""):
        if bad == "":
            continue
        with pytest.raises(ValueError):
            normalize_agent_tasks_dataset({
                "name": "evil", "version": "1",
                "cases": [{"case_id": bad, "input": "x"}],
            })

    # 纵深防御：workspace root 构造同样拒绝（绕过契约直调后端的路径）
    from motte_sdk.agent_backend import AgentBackendError, _workspace_root

    for bad_id in (str(victim), "../escape", "a/b"):
        with pytest.raises(AgentBackendError, match="CASE_ID_INVALID|path component"):
            _workspace_root("run-1", bad_id)
    assert (victim / "keep.txt").exists()


# ---------------------------------------------------------------- #2 证据边界失败

def test_invocation_settle_failure_quarantines(tmp_path, monkeypatch):
    service = RunService(InMemoryRunStore())
    cases = [{
        "case_id": "case-1", "input": "Write side.txt then final.",
        "fixture": {}, "expected": {"files": {"side.txt": {"mode": "exact", "expected": "x"}}},
    }]
    manifest = _manifest(tmp_path, cases)

    scripted = Scripted([
        {"content": "", "tool_calls": [_tool_call("c1", "write_file", {"path": "side.txt", "content": "x"})]},
        {"content": "done", "tool_calls": []},
    ])

    # 注入：在存储层让 settle 转换失败（副作用已发生；真实包装路径生效）
    import motte_sdk.agent_backend as agent_backend

    original_build = agent_backend.build_agent_provider
    agent_backend.build_agent_provider = lambda m: scripted
    invocations_repo = service.store.invocations
    original_transition = invocations_repo.transition
    settled_calls = {"n": 0}

    def flaky_transition(invocation_id, **kwargs):  # noqa: ANN001
        if kwargs.get("status") == "settled":
            settled_calls["n"] += 1
            if settled_calls["n"] == 2:  # 第一次 model settle 成功；工具 settle 失败
                raise OSError("simulated invocation store outage")
        return original_transition(invocation_id, **kwargs)

    monkeypatch.setattr(invocations_repo, "transition", flaky_transition)
    try:
        run = service.create_run("r2-tasks@1", manifest, ["case-1"])
        from motte_sdk.dispatcher import RunDispatcher

        dispatcher = RunDispatcher(service)
        claimed = dispatcher.claim()
        handle = build_execution_handle(claimed)
        if handle.attach:
            handle.attach(service, run["id"])
        result = service.execute(run["id"], provider=handle.invoke)
        # 不确定状态：needs_review，绝不当作普通工具错误继续
        assert result["status"] == "needs_review", json.dumps(result.get("error"), ensure_ascii=False)
        assert result["error"]["code"] == "CALL_OUTCOME_INDETERMINATE"
        # 留下 dispatching 记录（证据可见，不静默）
        invocations = service.store.invocations.list_for_run(run["id"])
        assert any(item["status"] == "dispatching" for item in invocations)
        # 只发生了一次模型调用（异常中止后续）
        assert len(scripted.requests) == 1
    finally:
        agent_backend.build_agent_provider = original_build


# ---------------------------------------------------------------- #3 legacy 取消后不执行工具

def test_legacy_cancel_blocks_tool_after_model_response(tmp_path, monkeypatch):
    service = RunService(InMemoryRunStore())
    cases = [{
        "case_id": "case-1", "input": "Write side.txt.",
        "fixture": {}, "expected": {},
    }]
    manifest = _manifest(tmp_path, cases, mode="legacy-json")
    writes: list[str] = []

    scripted = Scripted([
        {"content": json.dumps({"action": "tool", "tool": "write_file",
                                "input": {"path": "side.txt", "content": "x"}})},
        {"content": json.dumps({"action": "final", "answer": "done"})},
    ])

    def workspace_tools(ws):  # noqa: ANN001
        def write_file(arguments):  # noqa: ANN001
            writes.append(arguments["path"])
            return "ok"
        return {"write_file": write_file}

    import motte_sdk.agent_backend as agent_backend

    monkeypatch.setattr(agent_backend, "_workspace_tools", workspace_tools)
    # R3 #10：monkeypatch 自动恢复，不残留假 provider 影响后续测试
    monkeypatch.setattr(agent_backend, "build_agent_provider", lambda m: scripted)
    run = service.create_run("r2-tasks@1", manifest, ["case-1"])
    from motte_sdk.dispatcher import RunDispatcher

    dispatcher = RunDispatcher(service)

    # 首个模型响应返回后立即取消（模拟"模型调用期间取消"）
    real_complete = scripted._complete

    def complete_then_cancel(request):  # noqa: ANN001
        envelope = real_complete(request)
        service.cancel(run["id"], reason="operator")
        return envelope

    scripted.provider = SimpleNamespace(complete=complete_then_cancel)
    claimed = dispatcher.claim()
    handle = build_execution_handle(claimed)
    if handle.attach:
        handle.attach(service, run["id"])
    result = service.execute(run["id"], provider=handle.invoke)
    assert result["status"] == "cancelled"
    assert writes == []  # 取消后无任何工具写入
    assert len(scripted.requests) == 1  # 也无第二次模型调用


# ---------------------------------------------------------------- #4 预算接线

def test_output_token_budget_reaches_request(tmp_path):
    from motte_agent.budget import ExecutionBudget
    from motte_agent.builtin_react import BuiltinReActRuntime

    scripted = Scripted([{"content": "final"}])
    budget = ExecutionBudget.from_config({"max_steps": 2, "max_output_tokens": 1})
    runtime = BuiltinReActRuntime(
        scripted.provider.complete, mode="native-tool", model="m", budget=budget,
        declared_tools=(),
    )
    outcome = runtime.run_agent("x")
    assert scripted.max_output_tokens_seen == [1]
    assert outcome["budget"]["max_output_tokens"] == {
        "limit": 1, "enforcement": "enforced",
    }


def test_per_call_timeout_stops_loop(tmp_path):
    import time

    from motte_agent.budget import ExecutionBudget
    from motte_agent.builtin_react import BuiltinReActRuntime

    def slow_complete(request):  # noqa: ANN001
        time.sleep(0.3)
        return {"content": "late"}

    budget = ExecutionBudget.from_config({"max_steps": 4, "per_call_timeout_sec": 0.02})
    runtime = BuiltinReActRuntime(
        slow_complete, mode="native-tool", model="m", budget=budget, declared_tools=(),
    )
    started = time.monotonic()
    outcome = runtime.run_agent("x")
    elapsed = time.monotonic() - started
    assert outcome["termination_reason"] == "per_call_timeout"
    assert elapsed < 0.25  # 期限到期即返回，不等完整 0.3s 调用
    assert outcome["budget"]["per_call_timeout_sec"]["enforcement"] == "enforced"


# ---------------------------------------------------------------- #5 公共 API 脱敏

def test_public_api_redacts_final_output(tmp_path):
    service = RunService(InMemoryRunStore())
    cases = [{
        "case_id": "case-1", "input": "Echo the secret.",
        "fixture": {}, "expected": {},
    }]
    manifest = _manifest(tmp_path, cases)
    scripted = Scripted([
        {"content": f"leaked {SECRET}", "tool_calls": []},
    ])
    app = create_app(service.store, InMemoryResourceStore())
    client = TestClient(app)
    import motte_sdk.agent_backend as agent_backend

    original = agent_backend.build_agent_provider
    agent_backend.build_agent_provider = lambda m: scripted
    try:
        run_id, result = _execute(service, manifest, scripted, ["case-1"])
        assert result["status"] == "completed"

        case_detail = client.get(f"/api/v1/runs/{run_id}/cases/case-1/agent").json()
        run_view = client.get(f"/api/v1/runs/{run_id}").json()
        report = client.get(f"/api/v1/runs/{run_id}/report").json()
        for blob, label in (
            (json.dumps(case_detail), "case detail"),
            (json.dumps(run_view), "run view"),
            (json.dumps(report), "report"),
        ):
            assert SECRET not in blob, label
            assert "[REDACTED-SECRET]" in blob, label
        # 冻结评分输入未被改动：持久 case 结果仍含原文（评分一致性不受影响）
        stored = service.store.case_runs.get(run_id, "case-1")
        assert SECRET in json.dumps(stored["result"]["observation"]["final_output"])
    finally:
        agent_backend.build_agent_provider = original


# ---------------------------------------------------------------- #6 覆盖/删除禁写

def test_forbidden_write_detects_overwrite_end_to_end(tmp_path):
    service = RunService(InMemoryRunStore())
    cases = [{
        "case_id": "case-1", "input": "Overwrite the forbidden creds file.",
        "fixture": {"credentials.toml": "original"},
        "expected": {},
        "forbidden_paths": ["credentials.toml"],
    }]
    manifest = _manifest(tmp_path, cases)
    scripted = Scripted([
        {"content": "", "tool_calls": [
            _tool_call("c1", "write_file", {"path": "credentials.toml", "content": "leaked"}),
        ]},
        {"content": "done", "tool_calls": []},
    ])
    run_id, result = _execute(service, manifest, scripted, ["case-1"])
    assert result["status"] == "completed"
    view = service.get_run(run_id)
    forbidden = next(
        score for score in view["scores"] if score["metric_id"] == "no-forbidden-write"
    )
    assert forbidden["passed"] is False
    assert any("modified" in item for item in forbidden["details"]["violations"])


# ---------------------------------------------------------------- #7 regex 全局期限

def test_regex_metric_cannot_outrun_global_deadline():
    from motte_contracts.evaluation import observation_evidence_hash
    from motte_eval.observation import evaluate_observation, normalize_evaluator_config

    payload = {
        "observation_id": "obs-1", "run_id": "run-1", "case_id": "case-1",
        "final_output": "a" * 60 + "b", "termination": {"reason": "final_answer"},
        "coverage": {"complete": True},
    }
    payload["evidence_hash"] = observation_evidence_hash(payload)
    observation = FrozenObservation.model_validate(payload)

    # 指标级 timeout_sec 超过配置上限 → 拒绝
    with pytest.raises(ValueError, match="timeout_sec"):
        normalize_evaluator_config({"metrics": [
            {"metric_id": "x", "kind": "regex", "pattern": "a", "timeout_sec": 86400},
        ]})

    # 全局期限 0.1s：灾难正则在剩余期限内被截断，不等待指标自身超时
    import time

    config = normalize_evaluator_config({
        "metrics": [{"metric_id": "bomb", "kind": "regex", "pattern": "(a+)+$"}],
        "eval_deadline_sec": 0.1,
    })
    started = time.monotonic()
    results = evaluate_observation(observation, config, artifact_reader=lambda _id: None)
    elapsed = time.monotonic() - started
    bomb = next(item for item in results if item.metric_id == "bomb")
    assert bomb.status.value == "evaluator_error"
    assert bomb.reason in ("regex_timeout", "eval_deadline_exceeded")
    assert elapsed < 5, "global deadline must bound the metric"


# ---------------------------------------------------------------- #8 schema 远程 $ref

def test_json_schema_ref_blocked_without_network():
    from motte_contracts.evaluation import observation_evidence_hash
    from motte_eval.observation import evaluate_observation

    payload = {
        "observation_id": "obs-1", "run_id": "run-1", "case_id": "case-1",
        "final_output": '{"a": 1}', "termination": {"reason": "final_answer"},
        "coverage": {"complete": True},
    }
    payload["evidence_hash"] = observation_evidence_hash(payload)
    observation = FrozenObservation.model_validate(payload)
    results = evaluate_observation(observation, {"metrics": [
        {"metric_id": "remote", "kind": "json-schema",
         "schema": {"$ref": "http://127.0.0.1:1/never-visited.json"}},
    ]}, artifact_reader=lambda _id: None)
    remote = next(item for item in results if item.metric_id == "remote")
    assert remote.status.value == "evaluator_error"
    assert remote.reason == "schema_unresolvable"
    assert "not allowed" in str(remote.details.get("error")) or remote.details.get("error")


# ---------------------------------------------------------------- #9 异常路径采集与清理

def test_provider_exception_still_captures_and_cleans(tmp_path):
    service = RunService(InMemoryRunStore())
    cases = [{
        "case_id": "case-1", "input": "Write side.txt then fail.",
        "fixture": {}, "expected": {"files": {"side.txt": {"mode": "exact", "expected": "x"}}},
    }]
    manifest = _manifest(tmp_path, cases)

    class HalfFail(Scripted):
        def _complete(self, request):  # noqa: ANN001
            self.requests.append(request)
            if len(self.requests) == 1:
                return {"content": "", "tool_calls": [
                    _tool_call("c1", "write_file", {"path": "side.txt", "content": "x"}),
                ]}
            raise RuntimeError("provider exploded on second call")

    provider = HalfFail([])
    run_id, result = _execute(service, manifest, provider, ["case-1"])
    assert result["status"] == "failed"
    row = service.store.case_runs.get(run_id, "case-1")
    # 异常路径仍有冻结产物与 observation，workspace 已清理
    observation = row["result"].get("observation") or {}
    assert observation.get("artifact_refs"), "capture must run on the exception path"
    paths = {entry["path"] for entry in observation["artifact_refs"]}
    assert "side.txt" in paths
    assert row["result"].get("cleanup") == {"status": "success", "residual": []}
    ws_root = Path(os.environ["MOTTE_AGENT_WORKSPACE_ROOT"]) / run_id
    assert not ws_root.exists() or not any(ws_root.rglob("side.txt"))


# ---------------------------------------------------------------- #10 hash/归属校验

def test_scoring_rejects_tampered_observation(tmp_path):
    service = RunService(InMemoryRunStore())
    cases = [{
        "case_id": "case-1", "input": "Write side.txt.",
        "fixture": {}, "expected": {"files": {"side.txt": {"mode": "exact", "expected": "x"}}},
    }]
    manifest = _manifest(tmp_path, cases)
    scripted = Scripted([
        {"content": "", "tool_calls": [_tool_call("c1", "write_file", {"path": "side.txt", "content": "x"})]},
        {"content": "done", "tool_calls": []},
    ])
    run_id, result = _execute(service, manifest, scripted, ["case-1"])
    assert result["status"] == "completed"

    from motte_sdk.agent_tasks import agent_tasks_scores

    rows = service.store.case_runs.list_for_run(run_id)
    run_view = service.get_run(run_id)

    # 篡改 workspace（保留旧 hash）→ 拒评
    tampered = deepcopy(rows)
    tampered[0]["result"]["observation"]["workspace"]["after"].append("smuggled.txt")
    for score in agent_tasks_scores(run_view, tampered):
        if score["metric_id"] == "file-content:side.txt":
            assert score["metric_status"] == "insufficient_evidence"
            assert score["reason"] == "observation_hash_mismatch"

    # 篡改归属（换 run/case id）→ 拒评
    hijacked = deepcopy(rows)
    hijacked[0]["result"]["observation"]["run_id"] = "run-other"
    payload = hijacked[0]["result"]["observation"]
    payload["evidence_hash"] = observation_evidence_hash({
        key: value for key, value in payload.items()
        if key not in {"evidence_hash", "recorded_at"}
    })
    for score in agent_tasks_scores(run_view, hijacked):
        if score["metric_id"] == "file-content:side.txt":
            assert score["reason"] == "observation_run_mismatch"


# ---------------------------------------------------------------- #11 file-exists 实读

def test_file_exists_checks_real_readability():
    from motte_contracts.evaluation import observation_evidence_hash
    from motte_eval.observation import evaluate_observation

    payload = {
        "observation_id": "obs-1", "run_id": "run-1", "case_id": "case-1",
        "final_output": None, "termination": {"reason": "final_answer"},
        "coverage": {"complete": True},
        "artifact_refs": [{
            "artifact_id": "agent/run/case/gone.txt", "path": "gone.txt",
            "available": True, "sha256": "0" * 64,
        }],
    }
    payload["evidence_hash"] = observation_evidence_hash(payload)
    observation = FrozenObservation.model_validate(payload)
    results = evaluate_observation(observation, {"metrics": [
        {"metric_id": "exists", "kind": "file-exists", "path": "gone.txt"},
    ]}, artifact_reader=lambda _id: None)  # Artifact 已丢失
    exists = next(item for item in results if item.metric_id == "exists")
    assert exists.status.value == "insufficient_evidence"
    assert exists.reason == "artifact_unavailable"
    assert exists.passed is None


# ---------------------------------------------------------------- #12 JSON 截断

def test_json_schema_rejects_oversized_input():
    from motte_contracts.evaluation import observation_evidence_hash
    from motte_eval.observation import evaluate_observation

    payload = {
        "observation_id": "obs-1", "run_id": "run-1", "case_id": "case-1",
        "final_output": "{}INVALID", "termination": {"reason": "final_answer"},
        "coverage": {"complete": True},
    }
    payload["evidence_hash"] = observation_evidence_hash(payload)
    observation = FrozenObservation.model_validate(payload)
    results = evaluate_observation(observation, {
        "metrics": [{"metric_id": "j", "kind": "json-schema", "schema": {"type": "object"}}],
        "max_input_bytes": 2,
    }, artifact_reader=lambda _id: None)
    metric = next(item for item in results if item.metric_id == "j")
    assert metric.status.value == "evaluator_error"
    assert metric.reason == "input_too_large"


# ---------------------------------------------------------------- #13 legacy call_id 状态

def test_legacy_tool_failures_recorded_as_failed(tmp_path):
    service = RunService(InMemoryRunStore())
    cases = [{
        "case_id": "case-1", "input": "Read missing then write side.txt.",
        "fixture": {}, "expected": {},
    }]
    manifest = _manifest(tmp_path, cases, mode="legacy-json")
    scripted = Scripted([
        {"content": json.dumps({"action": "tool", "tool": "read_file", "input": {"path": "missing.txt"}})},
        {"content": json.dumps({"action": "tool", "tool": "write_file",
                                "input": {"path": "side.txt", "content": "x"}})},
        {"content": json.dumps({"action": "final", "answer": "done"})},
    ])
    run_id, result = _execute(service, manifest, scripted, ["case-1"])
    assert result["status"] == "completed"
    row = service.store.case_runs.get(run_id, "case-1")
    calls = row["result"]["observation"]["tool_calls"]
    assert len(calls) == 2
    read_call = next(c for c in calls if c["tool_name"] == "read_file")
    write_call = next(c for c in calls if c["tool_name"] == "write_file")
    assert read_call["status"] == "failed"
    assert write_call["status"] == "succeeded"
    assert read_call["call_id"] != write_call["call_id"]


# ---------------------------------------------------------------- #14 预算双计

def test_failed_tool_counts_once(tmp_path):
    from motte_agent.budget import ExecutionBudget
    from motte_agent.builtin_react import BuiltinReActRuntime

    attempts = []

    def flaky(arguments):  # noqa: ANN001
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("first fails")
        return "ok"

    scripted = Scripted([
        {"content": json.dumps({"action": "tool", "tool": "flaky", "input": {}})},
        {"content": json.dumps({"action": "tool", "tool": "flaky", "input": {}})},
        {"content": json.dumps({"action": "final", "answer": "ok"})},
    ])
    budget = ExecutionBudget(max_steps=4, max_tool_calls=2)
    runtime = BuiltinReActRuntime(
        scripted.provider.complete, {"flaky": flaky}, budget=budget,
    )
    outcome = runtime.run_agent("retry within budget")
    assert outcome["termination_reason"] == "final_answer"
    assert outcome["tool_calls"] == 2  # 每次尝试恰计一次（失败不双计）


# ---------------------------------------------------------------- #15 步数单一来源

def test_legacy_budget_steps_not_capped_at_default(tmp_path):
    from motte_agent.budget import ExecutionBudget
    from motte_agent.builtin_react import BuiltinReActRuntime

    responses = [
        {"content": json.dumps({"action": "tool", "tool": "noop", "input": {}})}
        for _ in range(11)
    ] + [{"content": json.dumps({"action": "final", "answer": "ok"})}]
    scripted = Scripted(responses)
    budget = ExecutionBudget(max_steps=12)
    runtime = BuiltinReActRuntime(scripted.provider.complete, {"noop": lambda a: "ok"}, budget=budget)
    outcome = runtime.run_agent("long run")
    assert outcome["termination_reason"] == "final_answer"
    assert outcome["steps"] == 12  # 超过旧默认 8 仍可继续


# ---------------------------------------------------------------- #16/#17/#18 Web

def test_web_fixes_covered_by_component_tests():
    """#16/#17/#18 的行为断言在 apps/web/tests/AgentPages.test.tsx（见该文件）；
    这里固定 Python 侧对应的 API 稳定结构契约（#17）。"""
    service = RunService(InMemoryRunStore())
    cases = [{"case_id": "case-1", "input": "x", "fixture": {}, "expected": {}}]
    workspace_root = Path(os.environ["MOTTE_AGENT_WORKSPACE_ROOT"])
    manifest = _manifest(workspace_root.parent, cases)
    app = create_app(service.store, InMemoryResourceStore())
    client = TestClient(app)
    run = service.create_run("r2-tasks@1", manifest, ["case-1"])
    detail = client.get(f"/api/v1/runs/{run['id']}/cases/case-1/agent")
    assert detail.status_code == 200
    body = detail.json()
    assert body["pending"] is True
    for key in ("events", "capture_errors", "artifacts"):
        assert isinstance(body[key], list)
    assert body["observation"] is None and body["agent"] is None
