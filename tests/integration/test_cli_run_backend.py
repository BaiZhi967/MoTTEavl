"""M4-T06/T07：Claude/Codex batch 的平台纵向链路（fake binary 经 dispatcher）。

与 Pi 集成测试同一模式：真实 repository/dispatcher/RunService，执行体是
受控 fake binary（真实二进制 live 待授权）。断言评分、产物、会话身份、
unknown usage 语义与后端禁用边界。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from motte_sdk.dispatcher import RunDispatcher
from motte_sdk.resolve import prepare_run
from motte_sdk.runtime_backends import publish_canonical_runtime_versions
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

RULE = {"mode": "contains", "expected": "-was-here"}


def _dataset(backend: str):
    from motte_contracts.agent_tasks import (
        normalize_agent_tasks_dataset,
        scenario_for_agent_tasks,
    )

    dataset = normalize_agent_tasks_dataset({
        "name": f"{backend}-suite", "version": "1", "suite": "agent-tasks",
        "cases": [
            {"case_id": "case-one", "input": f"please write answer.txt ({backend})",
             "fixture": {"seed.txt": "s"},
             "expected": {"files": {"answer.txt": RULE}}},
        ],
    })
    return dataset, scenario_for_agent_tasks(dataset)


def _fake_binary(tmp_path: Path, backend: str) -> Path:
    if backend.startswith("claude"):
        payload = {
            "type": "result", "subtype": "success", "is_error": False,
            "duration_ms": 100, "duration_api_ms": 80, "num_turns": 1,
            "result": "done", "session_id": "fake-session-1",
            "total_cost_usd": 0.002,
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        }
        script = tmp_path / "fake-claude.py"
        script.write_text(
            "import json\nimport pathlib\nimport sys\n\n"
            f"payload = {payload!r}\n\n"
            "pathlib.Path('answer.txt').write_text('claude-was-here', encoding='utf-8')\n"
            "print(json.dumps(payload))\n",
            encoding="utf-8",
        )
        return script
    events = [
        {"id": "e1", "msg": {"type": "thread.started", "thread_id": "th-fake"}},
        {"id": "e2", "msg": {"type": "turn.started"}},
        {"id": "e3", "msg": {"type": "item.completed", "item": {
            "id": "i1", "type": "agent_message", "text": "done"}}},
        {"id": "e4", "msg": {"type": "turn.completed", "usage": {
            "input_tokens": 7, "output_tokens": 3}}},
        {"id": "e5", "msg": {"type": "thread.completed"}},
    ]
    script = tmp_path / "fake-codex.py"
    script.write_text(
        "import json\nimport pathlib\nimport sys\n\n"
        f"events = {events!r}\n\n"
        "pathlib.Path('answer.txt').write_text('codex-was-here', encoding='utf-8')\n"
        "for event in events:\n    print(json.dumps(event), flush=True)\n",
        encoding="utf-8",
    )
    return script


def _manifest(backend: str, binary: Path) -> dict:
    native = {"model": "fake-model"}
    if backend == "claude":
        native["binary"] = str(binary)
    else:
        native["binary"] = str(binary)
    return {
        "runtime": f"{backend}@1",
        "runtime_profile": {
            "runtime": f"{backend}@1",
            "native_settings": native,
        },
        # not-enforced 工具边界：显式确认后才允许运行（T01 契约）
        "runtime_accept_unenforced_tools": True,
    }


def _setup(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("MOTTE_PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    resources = InMemoryResourceStore()
    publish_canonical_runtime_versions(resources)
    dataset, scenario = _dataset(backend)
    resources.datasets.put(dataset)
    resources.scenarios.put(scenario)
    binary = _fake_binary(tmp_path, backend)
    service = RunService(InMemoryRunStore())
    dispatcher = RunDispatcher(service)
    return service, dispatcher, resources, binary


@pytest.mark.parametrize("backend", ["claude-cli", "codex-cli"])
def test_cli_backend_end_to_end_scoring_and_evidence(tmp_path, monkeypatch, backend):
    service, dispatcher, resources, binary = _setup(tmp_path, monkeypatch, backend)
    resolved, case_ids = prepare_run(
        f"{backend}-suite@1", _manifest(backend, binary), [], resources=resources,
    )
    assert resolved["execution"]["backend_id"] == backend

    run = service.create_run(f"{backend}-suite@1", resolved, case_ids)
    finished = dispatcher.dispatch(run["id"])

    assert finished["status"] == "completed"
    answer = [s for s in finished["scores"] if s["metric_id"] == "file-content:answer.txt"]
    assert answer and all(s["passed"] for s in answer)

    rows = service.store.case_runs.list_for_run(run["id"])
    result = rows[0]["result"]
    observation = result["observation"]
    artifact_paths = {a["path"] for a in observation["artifact_refs"] if a.get("available")}
    assert {"answer.txt", "seed.txt"} <= artifact_paths
    agent_section = result["agent"]
    assert agent_section["runtime"]["backend"] == f"{backend}@1"
    assert agent_section["termination_reason"] == "final_answer"

    if backend == "claude-cli":
        # 原生 usage/cost 回报 → observed
        assert observation["usage"]["reported"] is True
        assert observation["usage"]["total_tokens"] == 15
        assert observation["usage"]["cost_total"] == 0.002
    else:
        # exec 流回报 usage；费用未知保持 unknown
        assert observation["usage"]["reported"] is True
        assert observation["usage"]["total_tokens"] == 10
        assert observation["usage"]["cost_total"] is None

    # 会话身份进入事件流（stderr 事件按 backend 命名；此处至少 run 级事件完整）
    assert service.store.events.list_for_run(run["id"])


@pytest.mark.parametrize("backend", ["claude-cli", "codex-cli"])
def test_cli_backend_failing_binary_keeps_error_evidence(tmp_path, monkeypatch, backend):
    service, dispatcher, resources, _ = _setup(tmp_path, monkeypatch, backend)
    broken = tmp_path / f"broken-{backend}.py"
    broken.write_text(
        "import sys\n\nprint('boom', file=sys.stderr)\nsys.exit(3)\n",
        encoding="utf-8",
    )
    resolved, case_ids = prepare_run(
        f"{backend}-suite@1", _manifest(backend, broken), [], resources=resources,
    )
    run = service.create_run(f"{backend}-suite@1", resolved, case_ids)
    finished = dispatcher.dispatch(run["id"])
    assert finished["status"] == "completed"  # 案件完成但质量未过
    answer = [s for s in finished["scores"] if s["metric_id"] == "file-content:answer.txt"]
    assert answer and all(not s["passed"] for s in answer)
    rows = service.store.case_runs.list_for_run(run["id"])
    observation = rows[0]["result"]["observation"]
    # 非零退出/无输出：证据不足，不是假成功
    assert observation["termination"]["reason"] == "invalid_state"
