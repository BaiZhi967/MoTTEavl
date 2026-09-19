"""M1-T09：Agent Run 的 API/CLI 一致链路（全部离线，scripted provider）。

验收对应：G15（API/CLI/Worker 对同一 Run 状态与分数一致）、
创建期结构化拒绝、GET 零副作用、多指标报告、artifact 归属读取。
"""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

CASES = [
    {
        "case_id": "file-report-001",
        "input": "Read input.json and write report.json with the enabled count.",
        "fixture": {"input.json": json.dumps([{"enabled": True}, {"enabled": False}])},
        "expected": {"files": {"report.json": {
            "mode": "schema",
            "schema": {"type": "object", "required": ["enabled_count"]},
        }}},
        "forbidden_paths": ["credentials.toml"],
        "limits": {"max_steps": 6, "max_tool_calls": 4},
    },
    {
        "case_id": "file-report-002",
        "input": "Write hello.txt containing hi.",
        "fixture": {},
        "expected": {"files": {"hello.txt": {"mode": "exact", "expected": "hi"}}},
    },
]

SCRIPTED_RESPONSES = {
    "file-report-001": [
        {"content": "", "tool_calls": [
            {"id": "c1", "name": "read_file", "arguments": json.dumps({"path": "input.json"})},
        ]},
        {"content": "", "tool_calls": [
            {"id": "c2", "name": "write_file", "arguments": json.dumps({
                "path": "report.json",
                "content": json.dumps({"enabled_count": 1}),
            })},
        ]},
        {"content": "counted 1", "tool_calls": []},
    ],
    "file-report-002": [
        {"content": "", "tool_calls": [
            {"id": "c1", "name": "write_file",
             "arguments": json.dumps({"path": "hello.txt", "content": "hi"})},
        ]},
        {"content": "done", "tool_calls": []},
    ],
}


class ScriptedProvider:
    """队列空时按"当前 case"绑定脚本响应；跨 case 状态隔离。"""

    def __init__(self):
        self.queue: list[dict] = []
        self.requests: list = []
        self.current_case = {"id": None}
        self.provider = SimpleNamespace(complete=self._complete)

    def _complete(self, request):  # noqa: ANN001
        self.requests.append(request)
        if not self.queue:
            self.queue = list(SCRIPTED_RESPONSES[self.current_case["id"]])
        return dict(self.queue.pop(0))


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    store = InMemoryRunStore()
    resources = InMemoryResourceStore()
    application = create_app(store, resources)
    client = TestClient(application)
    provider = ScriptedProvider()
    import motte_sdk.agent_backend as agent_backend

    monkeypatch.setattr(agent_backend, "build_agent_provider", lambda manifest: provider)

    # provider connection + model + publish
    created = client.post("/api/v1/providers", json={
        "name": "local", "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:9/v1",
    })
    assert created.status_code in (200, 201), created.text
    model = client.post("/api/v1/models", json={
        "id": "agent-model", "provider": "local", "capabilities": {},
        "supports_tools": True,
    })
    assert model.status_code in (200, 201), model.text
    published = client.post("/api/v1/models/agent-model/publish")
    assert published.status_code == 200, published.text

    imported = client.post("/api/v1/agent-tasks/import", json={
        "name": "file-report", "content": json.dumps(CASES),
    })
    assert imported.status_code == 201, imported.text
    return client, application, provider


def test_agent_api_cli_web_same_pass(app_env):
    client, application, provider = app_env

    # dry-run 预检
    dry = client.post("/api/v1/agent-tasks/runs/dry-run", json={
        "scenario": "file-report@1", "model": "agent-model", "mode": "native-tool",
    })
    assert dry.status_code == 200, dry.text
    assert dry.json()["backend"] == "builtin-agent"
    assert dry.json()["selected_cases"] == 2
    assert dry.json()["prompt_version"] == "builtin-react-native@1"

    # 创建 Run
    created = client.post("/api/v1/agent-tasks/runs", json={
        "scenario": "file-report@1", "model": "agent-model", "mode": "native-tool",
    })
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    assert created.json()["manifest"]["execution"]["backend_id"] == "builtin-agent"

    # Worker 执行：handle 已在 fixture patch 下构建（scripted provider 按当前 case 绑定）
    service = application.state.run_service
    from motte_sdk.dispatcher import RunDispatcher
    from motte_sdk.execution_backends import build_execution_handle

    dispatcher = RunDispatcher(service)
    claimed = dispatcher.claim()
    assert claimed is not None
    handle = build_execution_handle(claimed)
    if handle.attach:
        handle.attach(service, run_id)

    def tracked_invoke(case_id):  # noqa: ANN001
        provider.current_case["id"] = case_id
        return handle.invoke(case_id)

    result = service.execute(run_id, provider=tracked_invoke)
    assert result["status"] == "completed", json.dumps(
        result.get("error"), ensure_ascii=False
    )

    # 多指标分数：每 case 两个 file-content + 一个 no-forbidden-write（case-1）
    view = client.get(f"/api/v1/runs/{run_id}").json()
    metric_ids = sorted({score["metric_id"] for score in view["scores"]})
    assert metric_ids == [
        "file-content:hello.txt", "file-content:report.json", "no-forbidden-write",
    ]
    by_metric = {}
    for score in view["scores"]:
        by_metric[(score["case_id"], score["metric_id"])] = score
    assert by_metric[("file-report-001", "file-content:report.json")]["passed"] is True
    assert by_metric[("file-report-002", "file-content:hello.txt")]["passed"] is True
    assert by_metric[("file-report-001", "no-forbidden-write")]["passed"] is True

    # 同一事实源的 report 与 scoring pass：API 与 CLI 入口读到相同内容（CLI 见独立用例）
    report = client.get(f"/api/v1/runs/{run_id}/report").json()
    assert report["summary"]["passed"] >= 3
    passes = client.get(f"/api/v1/runs/{run_id}/scoring-passes").json()
    assert passes["total"] == 1
    assert passes["items"][0]["summary"]["multi_metric"] is True

    # GET 零副作用：反复读 report/run 不产生模型调用或新评分
    calls_before = len(provider.requests)
    for _ in range(3):
        client.get(f"/api/v1/runs/{run_id}/report")
        client.get(f"/api/v1/runs/{run_id}")
    assert len(provider.requests) == calls_before
    assert client.get(f"/api/v1/runs/{run_id}/scoring-passes").json()["total"] == 1

    # rescore：新 pass，旧 pass 内容不变；零模型调用
    first_pass = deepcopy(passes["items"][0])
    rescored = client.post(f"/api/v1/runs/{run_id}/rescore")
    assert rescored.status_code == 200
    assert len(provider.requests) == calls_before
    passes_after = client.get(f"/api/v1/runs/{run_id}/scoring-passes").json()
    assert passes_after["total"] == 2
    assert passes_after["items"][0] == first_pass
    # 指定历史 pass 的报告仍可读
    historical = client.get(
        f"/api/v1/runs/{run_id}/report?scoring_pass_id={first_pass['id']}"
    )
    assert historical.status_code == 200
    assert historical.json()["scoring_pass_id"] == first_pass["id"]

    # 样本下钻 + artifact 归属读取
    detail = client.get(f"/api/v1/runs/{run_id}/cases/file-report-001/agent")
    assert detail.status_code == 200
    assert detail.json()["agent"]["termination_reason"] == "final_answer"
    artifact = client.get(
        f"/api/v1/runs/{run_id}/cases/file-report-001/artifacts/content?path=report.json"
    )
    assert artifact.status_code == 200
    assert artifact.json()["sha256_matches"] is True
    assert json.loads(artifact.json()["content"])["enabled_count"] == 1
    # 不在 run 里的 case / 未捕获的 path → 404
    assert client.get(
        f"/api/v1/runs/{run_id}/cases/unknown/agent"
    ).status_code == 404
    assert client.get(
        f"/api/v1/runs/{run_id}/cases/file-report-001/artifacts/content?path=missing.txt"
    ).status_code == 404

    # 调用日志下钻
    invocations = client.get(f"/api/v1/runs/{run_id}/invocations").json()
    assert invocations["total"] > 0
    settled = [item for item in invocations["items"] if item["status"] == "settled"]
    assert settled and all("outcome" in item for item in settled)

    # retry 子 Run：completed 不允许整段重试；失败的 Run 显式 retry 生成子 Run
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code in (409, 422)

    failed = client.post("/api/v1/agent-tasks/runs", json={
        "scenario": "file-report@1", "model": "agent-model", "mode": "native-tool",
        "case_selection": {"mode": "ids", "case_ids": ["file-report-001"]},
    })
    failed_id = failed.json()["id"]

    class ExplodingProvider:
        def __init__(self):
            self.provider = SimpleNamespace(complete=self._complete)

        def _complete(self, request):  # noqa: ANN001
            from motte_provider.errors import ProviderError

            raise ProviderError("hard failure")

    import motte_sdk.agent_backend as agent_backend

    original_build = agent_backend.build_agent_provider
    agent_backend.build_agent_provider = lambda manifest: ExplodingProvider()
    try:
        from motte_sdk.dispatcher import RunDispatcher

        dispatcher = RunDispatcher(service)
        dispatcher.execute_claimed(service.store.runs.claim(failed_id))
    finally:
        agent_backend.build_agent_provider = original_build
    assert client.get(f"/api/v1/runs/{failed_id}").json()["status"] == "failed"
    retried = client.post(f"/api/v1/runs/{failed_id}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["parent_run_id"] == failed_id
    assert retried.json()["id"] != failed_id


def test_native_tool_unsupported_model_rejected_at_creation(app_env):
    client, application, provider = app_env
    # 换一个不支持 tools 的已发布模型
    created = client.post("/api/v1/models", json={
        "id": "plain-model", "provider": "local", "capabilities": {},
        "supports_tools": False,
    })
    assert created.status_code in (200, 201)
    assert client.post("/api/v1/models/plain-model/publish").status_code == 200

    rejected = client.post("/api/v1/agent-tasks/runs", json={
        "scenario": "file-report@1", "model": "plain-model", "mode": "native-tool",
    })
    assert rejected.status_code == 422
    assert "does not support tools" in rejected.json()["error"]["message"]
    assert len(provider.requests) == 0  # 模型调用数 0

    # legacy-json 模式对同一模型可用（显式模式，无静默降级）
    legacy = client.post("/api/v1/agent-tasks/runs", json={
        "scenario": "file-report@1", "model": "plain-model", "mode": "legacy-json",
    })
    assert legacy.status_code == 202


def test_form_inputs_survive_invalid_submission(app_env):
    client, application, provider = app_env
    # 预检失败返回结构化错误（前端据此保留表单输入并显示禁用原因）
    dry = client.post("/api/v1/agent-tasks/runs/dry-run", json={
        "scenario": "missing@9", "model": "agent-model", "mode": "native-tool",
    })
    assert dry.status_code == 422
    assert dry.json()["error"]["code"] == "SCENARIO_NOT_FOUND"

    bad_budget = client.post("/api/v1/agent-tasks/runs/dry-run", json={
        "scenario": "file-report@1", "model": "agent-model", "mode": "native-tool",
        "budget": {"max_steps": 0},
    })
    assert bad_budget.status_code == 422


def test_cli_import_and_run_match_api_view(tmp_path, monkeypatch):
    """CLI 导入 + 创建的 Run 与 API 读取一致（同一 SQLite）。"""
    import os

    db_path = str(tmp_path / "cli.db")
    cases_file = tmp_path / "cases.json"
    cases_file.write_text(json.dumps([
        {
            "case_id": "cli-case-1", "input": "Write a.txt with content A.",
            "fixture": {}, "expected": {"files": {"a.txt": {"mode": "exact", "expected": "A"}}},
        },
    ]), encoding="utf-8")
    monkeypatch.setenv("MOTTE_DB_PATH", db_path)

    import motte_cli.main as cli_main

    imported = cli_main.main([
        "agent-tasks", "import", "--file", str(cases_file), "--name", "cli-tasks",
    ])
    assert imported == 0
    listed = cli_main.main(["agent-tasks", "list"])
    assert listed == 0

    # 准备已发布模型（直接写资源库，CLI 侧无模型子命令依赖 API）
    from motte_storage.factory import create_resource_store
    from motte_storage.run_store import SQLiteRunStore
    from motte_sdk.service import RunService

    resources = create_resource_store()
    resources.models.put({
        "id": "cli-model", "provider": "conn", "model": "cli-model",
        "lifecycle": "published", "published_at": "2026-09-19T00:00:00Z",
        "supports_tools": True, "enabled": True, "capabilities": {},
    })
    resources.providers.put({
        "name": "conn", "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:9/v1", "model": "cli-model",
    })

    run_code = cli_main.main([
        "agent-tasks", "run", "--scenario", "cli-tasks@1", "--model", "cli-model",
        "--mode", "native-tool",
    ])
    assert run_code == 0

    # API 读取同一 Run：状态 queued、backend builtin-agent
    service = RunService(SQLiteRunStore(db_path))
    runs = [run for run in service.store.runs.list() if run["scenario_version"] == "cli-tasks@1"]
    assert runs and runs[0]["status"] == "queued"
    assert runs[0]["manifest"]["execution"]["backend_id"] == "builtin-agent"
