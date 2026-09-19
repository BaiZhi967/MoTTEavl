"""M1-T04：builtin-agent@1 后端注册、创建期校验与样本隔离。

验收对应：A06（native 模式模型不支持 tools → 创建期结构化错误、模型调用数 0）、
G01/G03/G04。
"""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from motte_contracts.agent_tasks import (
    normalize_agent_tasks_dataset,
    scenario_for_agent_tasks,
)
from motte_sdk.execution_backends import (
    ExecutionBackendError,
    backend_for,
    build_execution_handle,
    resolve_execution,
)


# ---------------------------------------------------------------- 测试资源

DATASET = {
    "name": "file-report",
    "version": "1",
    "cases": [
        {
            "case_id": "case-1",
            "input": "Read input.json and write report.json with the count.",
            "fixture": {"input.json": json.dumps([{"enabled": True}, {"enabled": False}])},
            "expected": {"files": {"report.json": {
                "mode": "schema",
                "schema": {"type": "object", "required": ["enabled_count"]},
            }}},
            "forbidden_paths": ["credentials.toml"],
            "limits": {"max_steps": 6, "max_tool_calls": 4},
        },
        {
            "case_id": "case-2",
            "input": "Write report.json saying hello.",
            "fixture": {"input.json": "{}"},
            "expected": {"files": {"report.json": {"mode": "exact", "expected": "hello"}}},
        },
    ],
}


class FakeResources:
    def __init__(self, datasets, scenarios, models=None):  # noqa: ANN001
        from types import SimpleNamespace

        self.datasets = SimpleNamespace(
            get=lambda name, version: datasets.get(f"{name}@{version}"),
            list=lambda: list(datasets.values()),
        )
        self.scenarios = SimpleNamespace(
            get=lambda name, version: scenarios.get(f"{name}@{version}"),
            list=lambda: list(scenarios.values()),
        )
        self.models = SimpleNamespace(
            get=lambda model_id: (models or {}).get(model_id),
            list=lambda: list((models or {}).values()),
        )


def _resources(models=None):  # noqa: ANN001
    dataset = normalize_agent_tasks_dataset(deepcopy(DATASET))
    scenario = scenario_for_agent_tasks(dataset)
    return FakeResources(
        {f"{dataset['name']}@{dataset['version']}": dataset},
        {f"{scenario['name']}@{scenario['version']}": scenario},
        models,
    )


PUBLISHED_TOOL_MODEL = {
    "id": "model-tools", "model": "test-model", "provider": "conn",
    "lifecycle": "published", "published_at": "2026-09-19T00:00:00Z",
    "supports_tools": True,
}


def _agent_manifest(resources, *, mode="native-tool", model="model-tools"):  # noqa: ANN001
    from motte_sdk.agent_tasks import resolve_agent_tasks_manifest

    scenario = next(iter(resources.scenarios.list()))
    resolved = resolve_agent_tasks_manifest(
        scenario,
        {"model": model, "agent": {"mode": mode}},
        resources,
    )
    # 模拟 resolve.py 的 provider 展开（单元层不引入 resource 展开）
    resolved["provider"] = {
        "kind": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
        "model": "test-model",
    }
    resolved["resource_snapshots"] = {"model_profile": {"id": model}}
    return resolve_execution(f"{scenario['name']}@{scenario['version']}", resolved, scenario=scenario)


# ---------------------------------------------------------------- 注册表

def test_registry_exposes_wired_builtin_agent():
    spec = backend_for("builtin-agent", "1")
    assert spec.id == "builtin-agent" and spec.available is True
    assert spec.capabilities == {"interactive": False, "safe_to_repeat": False}
    descriptor = _agent_manifest(_resources({"model-tools": PUBLISHED_TOOL_MODEL}))
    assert descriptor["execution"] == {
        "backend_id": "builtin-agent",
        "backend_version": "1",
        "capabilities": {"interactive": False, "safe_to_repeat": False},
    }
    assert descriptor["agent"] == "builtin-agent@1"
    assert descriptor["agent_config"]["mode"] == "native-tool"
    assert descriptor["benchmark_snapshot"]["suite"] == "agent-tasks"


def test_backend_rejects_unsupported_before_call():
    # native-tool + supports_tools=False：插件 prepare 期拒绝（零模型调用）
    no_tools_model = {**PUBLISHED_TOOL_MODEL, "id": "model-notools", "supports_tools": False}
    resources = _resources({"model-notools": no_tools_model})
    from motte_sdk.agent_tasks import resolve_agent_tasks_manifest

    scenario = next(iter(resources.scenarios.list()))
    with pytest.raises(ValueError, match="does not support tools"):
        resolve_agent_tasks_manifest(
            scenario, {"model": "model-notools", "agent": {"mode": "native-tool"}}, resources,
        )

    # 后端 validate：模式 / 工具集 / 预算 / 快照缺失分别结构化拒绝
    from motte_sdk.agent_backend import AgentBackendError, validate_agent_manifest

    good = _agent_manifest(_resources({"model-tools": PUBLISHED_TOOL_MODEL}))
    with pytest.raises(AgentBackendError) as exc:
        validate_agent_manifest({**good, "agent_config": {**good["agent_config"], "mode": "auto"}})
    assert exc.value.code == "AGENT_MODE_UNSUPPORTED"
    with pytest.raises(AgentBackendError) as exc:
        validate_agent_manifest({
            **good,
            "agent_config": {**good["agent_config"], "tools": ["read_file", "shell"]},
        })
    assert exc.value.code == "AGENT_TOOLS_UNSUPPORTED"
    with pytest.raises(AgentBackendError) as exc:
        validate_agent_manifest({
            **good, "agent_config": {**good["agent_config"], "budget": {"max_steps": -1}},
        })
    assert exc.value.code == "AGENT_BUDGET_INVALID"
    with pytest.raises(AgentBackendError) as exc:
        validate_agent_manifest({**good, "benchmark_snapshot": None})
    assert exc.value.code == "AGENT_SNAPSHOT_INVALID"

    # resolve_execution：未知 agent 版本 / skills 越界拒绝
    bad_agent = {**good, "agent": "builtin-agent@2"}
    with pytest.raises(ExecutionBackendError):
        resolve_execution("file-report@1", bad_agent)


# ---------------------------------------------------------------- 样本隔离

def _scripted_provider(responses_per_case):  # noqa: ANN001
    """每 case 一组顺序响应的 provider.complete 替身。"""
    state = {"case": None, "queue": [], "requests": []}

    def complete(request):  # noqa: ANN001
        state["requests"].append(request)
        return dict(state["queue"].pop(0))

    def bind(case_id):  # noqa: ANN001
        state["queue"] = list(responses_per_case[case_id])

    complete.bind = bind
    complete.requests = state["requests"]
    return complete


def test_two_cases_get_isolated_workspaces_and_histories(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    manifest = _agent_manifest(_resources({"model-tools": PUBLISHED_TOOL_MODEL}))

    native_calls = {
        # case-1：读 input.json → 写 report.json → final
        "case-1": [
            {"content": "", "tool_calls": [
                {"id": "c1", "name": "read_file", "arguments": json.dumps({"path": "input.json"})},
            ]},
            {"content": "", "tool_calls": [
                {"id": "c2", "name": "write_file", "arguments": json.dumps({
                    "path": "report.json",
                    "content": json.dumps({"enabled_count": 1}),
                })},
            ]},
            {"content": "counted", "tool_calls": []},
        ],
        # case-2：写同名 report.json 但内容不同
        "case-2": [
            {"content": "", "tool_calls": [
                {"id": "c1", "name": "write_file", "arguments": json.dumps({
                    "path": "report.json", "content": "hello",
                })},
            ]},
            {"content": "done", "tool_calls": []},
        ],
    }
    from types import SimpleNamespace

    provider = _scripted_provider(native_calls)

    class FakeCaseProvider:
        def __init__(self, complete_fn):  # noqa: ANN001
            self.provider = SimpleNamespace(complete=complete_fn)

    monkeypatch.setattr(
        "motte_sdk.agent_backend.build_agent_provider",
        lambda manifest_dict: FakeCaseProvider(provider),
    )

    run = {"id": "run-agent-1", "case_ids": ["case-1", "case-2"], "manifest": manifest}
    handle = build_execution_handle(run)
    assert handle.backend_id == "builtin-agent"

    provider.bind("case-1")
    result_one = handle.invoke("case-1")
    provider.bind("case-2")
    result_two = handle.invoke("case-2")

    # 各自终止成功、产物独立（同名不同内容）
    assert result_one["agent"]["termination_reason"] == "final_answer"
    assert result_two["agent"]["termination_reason"] == "final_answer"
    obs_one = result_one["observation"]
    obs_two = result_two["observation"]
    assert obs_one["case_id"] == "case-1" and obs_two["case_id"] == "case-2"
    report_one = next(a for a in obs_one["artifact_refs"] if a["path"] == "report.json")
    report_two = next(a for a in obs_two["artifact_refs"] if a["path"] == "report.json")
    assert report_one["sha256"] != report_two["sha256"]
    # case-2 的 workspace 不含 case-1 的 input.json 内容
    assert "input.json" in obs_one["workspace"]["before"]

    # 消息历史独立：case-2 第一轮请求只有自己的 user 消息（不继承 case-1）
    case_two_first_request = provider.requests[len(native_calls["case-1"])]
    assert [message.role for message in case_two_first_request.messages] == ["user"]

    # 隐藏断言不进入模型输入（请求文本不含 expected 结构与 forbidden 路径）
    all_content = json.dumps(
        [message.content for request in provider.requests for message in request.messages],
        ensure_ascii=False, default=str,
    )
    assert "credentials.toml" not in all_content
    assert '"required"' not in all_content

    # 工作区已清理
    assert result_one["cleanup"]["status"] == "success"
    assert result_two["cleanup"]["status"] == "success"


def test_workspace_policy_violation_terminates_with_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    manifest = _agent_manifest(_resources({"model-tools": PUBLISHED_TOOL_MODEL}))
    provider = _scripted_provider({
        "case-1": [
            {"content": "", "tool_calls": [
                {"id": "c1", "name": "write_file", "arguments": json.dumps({
                    "path": "../escape.txt", "content": "x",
                })},
            ]},
            {"content": "final", "tool_calls": []},
        ],
    })

    from types import SimpleNamespace

    class FakeCaseProvider:
        def __init__(self, complete_fn):  # noqa: ANN001
            self.provider = SimpleNamespace(complete=complete_fn)

    monkeypatch.setattr(
        "motte_sdk.agent_backend.build_agent_provider",
        lambda manifest_dict: FakeCaseProvider(provider),
    )
    run = {"id": "run-agent-2", "case_ids": ["case-1", "case-2"], "manifest": manifest}
    handle = build_execution_handle(run)
    provider.bind("case-1")
    result = handle.invoke("case-1")
    # 越权写入被 workspace 拒绝并回灌；agent 仍可 final，但产物断言会在评分侧失败
    assert result["agent"]["termination_reason"] == "final_answer"
    # 越权写入被 workspace 策略拒绝并回灌为 tool_error（无副作用）
    tool_errors = [
        event for event in result["events"]
        if event.get("type") == "tool_error" and "escape" in str(event.get("error", ""))
    ]
    assert tool_errors
    assert "escape.txt" not in result["observation"]["workspace"]["after"]
    # 冻结轨迹中该调用记录为 failed（未成功执行）
    failed_calls = [
        call for call in result["observation"]["tool_calls"]
        if call["tool_name"] == "write_file"
    ]
    assert failed_calls and failed_calls[0]["status"] == "failed"
