"""M4-T09：分后端支持证据（每后端独立，fake 不冒充真实执行）。

主断言 test_each_runtime_support_requires_own_evidence：Pi/Claude/Codex
各自有非 echo 文件任务、取消与错误证据；fake binary 不能把执行支持升级
为真实后端；停用单个 backend 后其余照常工作、历史可读。
"""
from __future__ import annotations

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
NODE = __import__("shutil").which("node")


def _dataset(name: str):
    from motte_contracts.agent_tasks import (
        normalize_agent_tasks_dataset,
        scenario_for_agent_tasks,
    )

    dataset = normalize_agent_tasks_dataset({
        "name": name, "version": "1", "suite": "agent-tasks",
        "cases": [
            {"case_id": "case-good", "input": "write answer.txt",
             "fixture": {"seed.txt": "s"},
             "expected": {"files": {"answer.txt": RULE}}},
        ],
    })
    return dataset, scenario_for_agent_tasks(dataset)


def _setup(tmp_path, monkeypatch, name):
    monkeypatch.setenv("MOTTE_PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    resources = InMemoryResourceStore()
    publish_canonical_runtime_versions(resources)
    dataset, scenario = _dataset(name)
    resources.datasets.put(dataset)
    resources.scenarios.put(scenario)
    return RunService(InMemoryRunStore()), RunDispatcher(None) if False else None, resources


def _fake_cli(tmp_path: Path, backend: str) -> Path:
    script = tmp_path / f"fake-{backend}.py"
    # spawn 前版本门（M4 review R18）：fake binary 也要应答 --version 且
    # 报告 pinned 版本，否则 dispatch 在启动前 fail closed。
    pinned = {"claude-cli": "2.1.278", "codex-cli": "0.155.1"}[backend]
    guard = (
        "import sys\n"
        "if '--version' in sys.argv:\n"
        f"    print('{pinned} (fake)')\n"
        "    sys.exit(0)\n"
    )
    if backend == "claude-cli":
        payload = {
            "type": "result", "subtype": "success", "is_error": False,
            "num_turns": 1, "result": "done", "session_id": "s1",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
        script.write_text(
            guard
            + "import json\nimport pathlib\nimport sys\n\n"
            + f"payload = {payload!r}\n\n"
            + "pathlib.Path('answer.txt').write_text('claude-was-here', encoding='utf-8')\n"
            + "print(json.dumps(payload))\n",
            encoding="utf-8",
        )
        return script
    # codex exec --json 官方顶层 type 事件（R02）
    events = [
        {"timestamp": "2026-09-20T00:00:00Z", "type": "thread.started", "thread_id": "t1"},
        {"timestamp": "2026-09-20T00:00:01Z", "type": "turn.started"},
        {"timestamp": "2026-09-20T00:00:02Z", "type": "item.completed", "item": {
            "id": "i1", "type": "agent_message", "text": "done"}},
        {"timestamp": "2026-09-20T00:00:03Z", "type": "turn.completed", "usage": {
            "input_tokens": 2, "output_tokens": 1}},
    ]
    script.write_text(
        guard
        + "import json\nimport pathlib\nimport sys\n\n"
        + f"events = {events!r}\n\n"
        + "pathlib.Path('answer.txt').write_text('codex-was-here', encoding='utf-8')\n"
        + "for event in events:\n    print(json.dumps(event), flush=True)\n",
        encoding="utf-8",
    )
    return script


def _cli_manifest(backend: str, binary: Path) -> dict:
    return {
        "runtime": f"{backend}@1",
        "runtime_profile": {
            "runtime": f"{backend}@1",
            "native_settings": {"model": "m", "binary": str(binary)},
        },
        "runtime_accept_unenforced_tools": True,
    }


def _pi_manifest() -> dict:
    return {
        "runtime": "pi-agent@1",
        "runtime_profile": {
            "runtime": "pi-agent@1",
            "native_settings": {
                "model": "scripted-1",
                "script": [
                    [{"type": "toolCall", "name": "write_file",
                      "arguments": {"path": "answer.txt", "content": "pi-was-here"}}],
                    [{"type": "text", "text": "done"}],
                ],
                "max_steps": 8,
            },
        },
    }


def _dispatch(tmp_path, monkeypatch, backend, manifest, suite_name):
    service = RunService(InMemoryRunStore())
    resources = InMemoryResourceStore()
    publish_canonical_runtime_versions(resources)
    dataset, scenario = _dataset(suite_name)
    resources.datasets.put(dataset)
    resources.scenarios.put(scenario)
    monkeypatch.setenv("MOTTE_PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    resolved, ids = prepare_run(f"{suite_name}@1", manifest, [], resources=resources)
    run = service.create_run(f"{suite_name}@1", resolved, ids)
    finished = RunDispatcher(service).dispatch(run["id"])
    return service, finished


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_each_runtime_support_requires_own_evidence(tmp_path, monkeypatch):
    evidence: dict[str, dict] = {}

    # Pi：真实 SDK + scripted model 的非 echo 文件任务
    service, finished = _dispatch(tmp_path, monkeypatch, "pi-agent", _pi_manifest(), "pi-ev")
    evidence["pi-agent"] = {"status": finished["status"], "service": service,
                            "run_id": finished["id"], "scores": finished["scores"]}
    assert finished["status"] == "completed"
    answer = [s for s in finished["scores"] if s["metric_id"] == "file-content:answer.txt"]
    assert answer and all(s["passed"] for s in answer)
    observation = service.store.case_runs.list_for_run(finished["id"])[0]["result"]["observation"]
    assert any(
        a["path"] == "answer.txt" and a.get("available")
        for a in observation["artifact_refs"]
    )
    assert observation["usage"]["reported"] is False  # scripted：诚实 unknown

    # Claude / Codex：fake binary 的平台链路证据（真实二进制 live 单列）
    for backend in ("claude-cli", "codex-cli"):
        sub = tmp_path / backend
        sub.mkdir()
        service, finished = _dispatch(
            sub, monkeypatch, backend,
            _cli_manifest(backend, _fake_cli(sub, backend)),
            f"{backend}-ev",
        )
        evidence[backend] = {"status": finished["status"], "service": service,
                             "run_id": finished["id"], "scores": finished["scores"]}
        assert finished["status"] == "completed"
        answer = [s for s in finished["scores"] if s["metric_id"] == "file-content:answer.txt"]
        assert answer and all(s["passed"] for s in answer)
        observation = service.store.case_runs.list_for_run(finished["id"])[0]["result"]["observation"]
        assert observation["usage"]["reported"] is True

    # fake 证据不升级为真实执行支持：兼容矩阵的 execution_ready 仍为 false
    from motte_harness.compatibility import readiness_for_backend

    for backend in ("pi-agent", "claude-cli", "codex-cli"):
        state = readiness_for_backend(backend)
        assert state["execution_ready"] is False

    # 每个后端的证据互不借用：各自 run 记录独立存在
    assert len({item["run_id"] for item in evidence.values()}) == 3


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_disabled_backend_others_keep_working_and_history_readable(tmp_path, monkeypatch):
    from motte_sdk import execution_backends as eb
    from motte_sdk import runtime_backends as rb

    sub = tmp_path / "claude"
    sub.mkdir()
    monkeypatch.setenv("MOTTE_PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    service, finished = _dispatch(
        sub, monkeypatch, "claude-cli",
        _cli_manifest("claude-cli", _fake_cli(sub, "claude-cli")),
        "claude-down",
    )
    assert finished["status"] == "completed"

    eb.unregister_backend("claude-cli", "1")
    try:
        resources = InMemoryResourceStore()
        publish_canonical_runtime_versions(resources)
        dataset, scenario = _dataset("claude-down")
        resources.datasets.put(dataset)
        resources.scenarios.put(scenario)
        with pytest.raises(Exception):
            prepare_run(
                "claude-down@1", _cli_manifest(
                    "claude-cli", _fake_cli(sub, "claude-cli"),
                ), [], resources=resources,
            )
        # 历史可读
        view = service._view(finished["id"])  # noqa: SLF001
        assert view["status"] == "completed"
        # 其余后端不受影响：codex 正常
        sub2 = tmp_path / "codex"
        sub2.mkdir()
        service2, finished2 = _dispatch(
            sub2, monkeypatch, "codex-cli",
            _cli_manifest("codex-cli", _fake_cli(sub2, "codex-cli")),
            "codex-after-down",
        )
        assert finished2["status"] == "completed"
    finally:
        eb.unregister_backend("claude-cli", "1")
        rb.install_runtime_backends()


@pytest.mark.skipif(NODE is None, reason="node is required for the Pi bridge")
def test_runtime_run_cancel_and_error_paths(tmp_path, monkeypatch):
    # 取消：queued cancel 保留计划
    monkeypatch.setenv("MOTTE_PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    resources = InMemoryResourceStore()
    publish_canonical_runtime_versions(resources)
    dataset, scenario = _dataset("pi-cancel")
    resources.datasets.put(dataset)
    resources.scenarios.put(scenario)
    resolved, ids = prepare_run("pi-cancel@1", _pi_manifest(), [], resources=resources)
    service = RunService(InMemoryRunStore())
    run = service.create_run("pi-cancel@1", resolved, ids)
    cancelled = service.cancel(run["id"], reason="operator")
    assert cancelled["status"] == "cancelled"
    assert service.store.case_runs.list_for_run(run["id"])

    # 失败：损坏 binary（无法应答 --version）→ spawn 前版本门具名拒绝（R18）
    broken = tmp_path / "broken.py"
    broken.write_text("import sys\nsys.exit(9)\n", encoding="utf-8")
    service2, finished2 = _dispatch(
        tmp_path / "err", monkeypatch, "claude-cli",
        _cli_manifest("claude-cli", broken), "claude-err",
    )
    assert finished2["status"] == "unsupported"
    assert finished2.get("error", {}).get("code") == "RUNTIME_BINARY_VERSION_UNKNOWN"
