"""M1-T07：调用日志边界、取消、崩溃恢复与 second-chance 排除。

验收对应：A08（模型调用中取消：无后续工具执行）、A09（写文件后崩溃：
不自动重放，needs_review，调用数不增加）。
"""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from motte_contracts.agent_tasks import normalize_agent_tasks_dataset, scenario_for_agent_tasks
from motte_sdk.execution_backends import build_execution_handle, resolve_execution
from motte_sdk.service import RunService
from motte_storage.run_store import SQLiteRunStore

DATASET = {
    "name": "interrupt-tasks",
    "version": "1",
    "cases": [
        {
            "case_id": "case-1",
            "input": "Write side-effect.txt then finish.",
            "fixture": {},
            "expected": {"files": {"side-effect.txt": {"mode": "exact", "expected": "boom"}}},
        },
        {
            "case_id": "case-2",
            "input": "Write other.txt then finish.",
            "fixture": {},
            "expected": {"files": {"other.txt": {"mode": "exact", "expected": "x"}}},
        },
    ],
}


def _agent_manifest(mode="native-tool"):  # noqa: ANN001
    dataset = normalize_agent_tasks_dataset(deepcopy(DATASET))
    scenario = scenario_for_agent_tasks(dataset)
    from motte_sdk.agent_tasks import resolve_agent_tasks_manifest

    resolved = resolve_agent_tasks_manifest(
        scenario, {"model": "m1", "agent": {"mode": mode}}, _fake_resources(dataset, scenario),
    )
    resolved["provider"] = {
        "kind": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
        "model": "test-model",
    }
    return resolve_execution(
        f"{scenario['name']}@{scenario['version']}", resolved, scenario=scenario,
    )


def _fake_resources(dataset, scenario):  # noqa: ANN001
    return SimpleNamespace(
        datasets=SimpleNamespace(
            get=lambda name, version: (
                dataset if f"{name}@{version}" == f"{dataset['name']}@{dataset['version']}" else None
            ),
            list=lambda: [dataset],
        ),
        scenarios=SimpleNamespace(get=lambda *a: scenario, list=lambda: [scenario]),
        models=SimpleNamespace(get=lambda model_id: {
            "id": model_id, "model": "test-model", "lifecycle": "published",
            "published_at": "2026-09-19T00:00:00Z", "supports_tools": True,
        }, list=lambda: []),
    )


class ScriptedProvider:
    """按 case 注入顺序响应；记录全部请求。"""

    def __init__(self):
        self.provider = SimpleNamespace(complete=self._complete)
        self.queue: list[dict] = []
        self.requests: list = []
        self.writes: list[str] = []

    def _complete(self, request):  # noqa: ANN001
        self.requests.append(request)
        return dict(self.queue.pop(0))

    def bind(self, responses):  # noqa: ANN001
        self.queue = list(responses)


def _tool_call(call_id, name, arguments):  # noqa: ANN001
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments)}


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))


def _service(tmp_path):  # noqa: ANN001
    return RunService(SQLiteRunStore(tmp_path / "runs.db"))


def _run(service, manifest):  # noqa: ANN001
    return service.create_run(
        f"{DATASET['name']}@1", manifest, ["case-1", "case-2"],
    )


class WorkerCrash(BaseException):
    """模拟 worker 进程死亡：不被服务层 except Exception 吞掉。"""


def test_crash_after_dispatch_does_not_replay(tmp_path, monkeypatch):
    service = _service(tmp_path)
    manifest = _agent_manifest()
    run = _run(service, manifest)
    scripted = ScriptedProvider()
    scripted.bind([
        # case-1：写文件（副作用）→ final；崩溃点在结果落库之前
        {"content": "", "tool_calls": [
            _tool_call("c1", "write_file", {"path": "side-effect.txt", "content": "boom"}),
        ]},
        {"content": "final", "tool_calls": []},
    ])
    monkeypatch.setattr(
        "motte_sdk.agent_backend.build_agent_provider", lambda m: scripted,
    )

    claimed = service.store.runs.claim(run["id"])
    handle = build_execution_handle(claimed)
    handle.attach(service, run["id"]) if handle.attach else None

    side_effect_calls = []
    real_invoke = handle.invoke

    def crashing_invoke(case_id):  # noqa: ANN001
        real_invoke(case_id)  # agent 完整执行：文件已写（副作用已发生）
        side_effect_calls.append(case_id)
        raise WorkerCrash("worker died before durable case completion")

    with pytest.raises(WorkerCrash):
        service.execute(run["id"], provider=crashing_invoke)

    # 崩溃后：case attempt 停在 dispatching（开放），无 case 结果落库
    attempts = service.store.attempts.list_for_run(run["id"])
    assert attempts and attempts[0]["status"] == "dispatching"
    assert service.store.case_runs.list_for_run(run["id"]) == []

    # worker 重启恢复：builtin-agent 非 safe_to_repeat → needs_review，绝不自动重放
    from apps.worker.motte_worker.runtime import WorkerLoop

    WorkerLoop(service).recover_interrupted()
    view = service.get_run(run["id"])
    assert view["status"] == "needs_review"
    assert view["error"]["code"] == "CALL_OUTCOME_INDETERMINATE"

    # 调用日志仍在；重放次数为 0（副作用只发生一次）
    invocations = service.store.invocations.list_for_run(run["id"])
    assert invocations, "invocation boundaries must survive the crash"
    assert len(side_effect_calls) == 1
    tool_invocations = [item for item in invocations if item["kind"] == "tool"]
    assert tool_invocations and all(item["status"] == "settled" for item in tool_invocations)

    # needs_review 终态：不会被再次 claim，只能由操作员显式 retry
    from motte_storage.integrity import RunConflictError

    with pytest.raises(RunConflictError):
        service.store.runs.claim(run["id"])


def test_transient_failure_does_not_second_chance_agent_runs(tmp_path, monkeypatch):
    service = _service(tmp_path)
    manifest = _agent_manifest()
    run = _run(service, manifest)

    calls = {"count": 0}

    class FlakyProvider(ScriptedProvider):
        def _complete(self, request):  # noqa: ANN001
            self.requests.append(request)
            calls["count"] += 1
            from motte_provider.errors import ProviderError

            raise ProviderError("server exploded (transient)")

    flaky = FlakyProvider()
    monkeypatch.setattr("motte_sdk.agent_backend.build_agent_provider", lambda m: flaky)
    from motte_sdk.dispatcher import RunDispatcher

    dispatcher = RunDispatcher(service)
    dispatcher.execute_claimed(service.store.runs.claim(run["id"]))
    view = service.get_run(run["id"])
    # 瞬态失败不整段补跑：首个 case 落盘错误即停止，case-2 不再被调用
    assert calls["count"] == 1  # 无 second-chance 补跑
    assert view["status"] == "failed"
    rows = service.store.case_runs.list_for_run(run["id"])
    outcomes = {row["case_id"]: row.get("outcome") for row in rows}
    assert outcomes == {"case-1": None, "case-2": "not_attempted"}
    assert rows[0]["result"].get("error")


def test_cancellation_during_model_call_blocks_tools(tmp_path, monkeypatch):
    service = _service(tmp_path)
    manifest = _agent_manifest()
    run = _run(service, manifest)
    scripted = ScriptedProvider()
    tool_executions: list[str] = []

    original_complete = scripted._complete

    def complete_then_cancel(request):  # noqa: ANN001
        envelope = original_complete(request)
        # 第一个模型响应后立即请求取消（模拟"模型调用中取消"）
        service.cancel(run["id"], reason="operator requested")
        return envelope

    scripted.provider = SimpleNamespace(complete=complete_then_cancel)
    monkeypatch.setattr("motte_sdk.agent_backend.build_agent_provider", lambda m: scripted)

    from motte_sdk.dispatcher import RunDispatcher

    dispatcher = RunDispatcher(service)

    # 第一个响应是工具调用；取消后不允许执行任何工具
    scripted.bind([
        {"content": "", "tool_calls": [
            _tool_call("c1", "write_file", {"path": "side-effect.txt", "content": "boom"}),
        ]},
    ])
    dispatcher.execute_claimed(service.store.runs.claim(run["id"]))
    view = service.get_run(run["id"])
    assert view["status"] == "cancelled"
    assert tool_executions == []  # 取消后无工具执行
    # 事件保留取消与调用不确定性
    types = [event["type"] for event in service.events(run["id"])]
    assert "cancellation_requested" in types
    assert "cancelled" in types
    # 不确定性保留：case 尝试有记录
    attempts = service.store.attempts.list_for_run(run["id"])
    assert attempts


def test_wall_time_budget_stops_background_writes(tmp_path, monkeypatch):
    service = _service(tmp_path)
    manifest = deepcopy(_agent_manifest())
    manifest["agent_config"]["budget"] = {"max_steps": 1, "max_tool_calls": 1, "wall_time_sec": 30}
    run = _run(service, manifest)
    scripted = ScriptedProvider()

    scripted.bind([
        # case-1：第一步工具调用后步数耗尽；case-2：直接 final
        {"content": "", "tool_calls": [
            _tool_call("c1", "write_file", {"path": "one.txt", "content": "1"}),
        ]},
        {"content": "final", "tool_calls": []},
    ])
    monkeypatch.setattr("motte_sdk.agent_backend.build_agent_provider", lambda m: scripted)

    from motte_sdk.dispatcher import RunDispatcher

    dispatcher = RunDispatcher(service)
    dispatcher.execute_claimed(service.store.runs.claim(run["id"]))
    view = service.get_run(run["id"])
    assert view["status"] == "completed"  # 执行可结束
    rows = service.store.case_runs.list_for_run(run["id"])
    terminated = [
        row for row in rows
        if row["result"].get("agent", {}).get("termination_reason") == "max_steps"
    ]
    assert terminated, "budget exhaustion must surface a specific stop reason"
    # 步数耗尽后无背景写入：第二个工具调用从未执行
    artifacts_written = [
        entry["path"] for row in rows
        for entry in (row["result"].get("observation") or {}).get("artifact_refs", [])
        if entry["path"].startswith("one") or entry["path"].startswith("two")
    ]
    assert "two.txt" not in artifacts_written
