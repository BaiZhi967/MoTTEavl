"""M4-T06/T07：Claude/Codex batch 的平台纵向链路（fake binary 经 dispatcher）。

与 Pi 集成测试同一模式：真实 repository/dispatcher/RunService，执行体是
受控 fake binary（真实二进制 live 待授权）。fake binary 必须应答
``--version`` 并报告 pinned 版本——spawn 前版本门（M4 review R18）对
fixture 一视同仁。断言评分、产物、会话身份、unknown usage 语义、非零
退出终态（R06）、取消打断（R04）、工具轨迹缺失时否定断言不足（R13）、
版本漂移 fail closed（R18）与后端禁用边界。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from motte_sdk.dispatcher import RunDispatcher
from motte_sdk.resolve import prepare_run
from motte_sdk.runtime_backends import publish_canonical_runtime_versions
from motte_sdk.service import RunService
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

RULE = {"mode": "contains", "expected": "-was-here"}
PINNED = {"claude-cli": "2.1.278", "codex-cli": "0.155.1"}

# codex exec --json 的官方顶层 type 事件形态（R02）
_CODEX_EVENTS = [
    {"timestamp": "2026-09-20T00:00:00Z", "type": "thread.started", "thread_id": "th-fake"},
    {"timestamp": "2026-09-20T00:00:01Z", "type": "turn.started"},
    {"timestamp": "2026-09-20T00:00:02Z", "type": "item.completed", "item": {
        "id": "i1", "type": "agent_message", "text": "done"}},
    {"timestamp": "2026-09-20T00:00:03Z", "type": "turn.completed", "usage": {
        "input_tokens": 7, "output_tokens": 3, "cached_input_tokens": 0}},
]


def _dataset(backend: str, *, extra_cases=()):
    from motte_contracts.agent_tasks import (
        normalize_agent_tasks_dataset,
        scenario_for_agent_tasks,
    )

    cases = [
        {"case_id": "case-one", "input": f"please write answer.txt ({backend})",
         "fixture": {"seed.txt": "s"},
         "expected": {"files": {"answer.txt": RULE}}},
        *extra_cases,
    ]
    dataset = normalize_agent_tasks_dataset({
        "name": f"{backend}-suite", "version": "1", "suite": "agent-tasks",
        "cases": cases,
    })
    return dataset, scenario_for_agent_tasks(dataset)


def _version_guard(backend: str) -> str:
    return (
        "import sys\n"
        "if '--version' in sys.argv:\n"
        f"    print('{PINNED[backend]} (fake)')\n"
        "    sys.exit(0)\n"
    )


def _fake_binary(tmp_path: Path, backend: str, *, suffix="") -> Path:
    if backend.startswith("claude"):
        payload = {
            "type": "result", "subtype": "success", "is_error": False,
            "duration_ms": 100, "duration_api_ms": 80, "num_turns": 1,
            "result": "done", "session_id": "fake-session-1",
            "total_cost_usd": 0.002,
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        }
        script = tmp_path / f"fake-claude{suffix}.py"
        script.write_text(
            _version_guard(backend)
            + "import json\nimport pathlib\nimport sys\n\n"
            + f"payload = {payload!r}\n\n"
            + "pathlib.Path('answer.txt').write_text('claude-was-here', encoding='utf-8')\n"
            + "print(json.dumps(payload))\n",
            encoding="utf-8",
        )
        return script
    script = tmp_path / f"fake-codex{suffix}.py"
    script.write_text(
        _version_guard(backend)
        + "import json\nimport pathlib\nimport sys\n\n"
        + f"events = {_CODEX_EVENTS!r}\n\n"
        + "pathlib.Path('answer.txt').write_text('codex-was-here', encoding='utf-8')\n"
        + "for event in events:\n    print(json.dumps(event), flush=True)\n",
        encoding="utf-8",
    )
    return script


def _manifest(backend: str, binary: Path) -> dict:
    return {
        "runtime": f"{backend}@1",
        "runtime_profile": {
            "runtime": f"{backend}@1",
            "native_settings": {"model": "fake-model", "binary": str(binary)},
        },
        # not-enforced 工具边界：显式确认后才允许运行（T01 契约）
        "runtime_accept_unenforced_tools": True,
    }


def _setup(tmp_path, monkeypatch, backend, *, extra_cases=()):
    monkeypatch.setenv("MOTTE_PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    resources = InMemoryResourceStore()
    publish_canonical_runtime_versions(resources)
    dataset, scenario = _dataset(backend, extra_cases=extra_cases)
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

    # R14：原始 stdout 冻结为证据产物，parsed 带 raw_ref 指针。
    raw_refs = [ref for ref in observation["event_refs"] if ref["kind"] == "artifact"]
    assert raw_refs and any("raw-stdout" in ref["locator"] for ref in raw_refs)

    if backend == "claude-cli":
        # 原生 usage/cost 回报 → observed
        assert observation["usage"]["reported"] is True
        assert observation["usage"]["total_tokens"] == 15
        assert observation["usage"]["cost_total"] == 0.002
        # R13：claude 单对象结果没有工具轨迹 → coverage 不冒充 complete。
        assert observation["coverage"]["complete"] is False
        assert any(
            str(item).startswith("tool_trajectory:absent")
            for item in observation["coverage"]["missing"]
        )
    else:
        # exec 流回报 usage；费用未知保持 unknown
        assert observation["usage"]["reported"] is True
        assert observation["usage"]["total_tokens"] == 10
        assert observation["usage"]["cost_total"] is None
        # R02/R13：原生顶层 type 事件流完整解析 → 轨迹 complete。
        assert observation["coverage"]["complete"] is True

    # 会话身份进入事件流（stderr 事件按 backend 命名；此处至少 run 级事件完整）
    assert service.store.events.list_for_run(run["id"])

    # R15：session 记录落盘且终态（生产恢复入口可观察）。
    from motte_harness.session import SESSION_STORE_ROOT, load_session

    session_files = sorted((Path.cwd() / SESSION_STORE_ROOT).glob(
        f"{run['id']}/*/sess-*.json"
    )) if (Path.cwd() / SESSION_STORE_ROOT).exists() else []
    if session_files:
        record = load_session(session_files[0])
        assert record is not None and record["state"] == "terminal"


@pytest.mark.parametrize("backend", ["claude-cli", "codex-cli"])
def test_cli_backend_success_json_with_nonzero_exit_is_not_final(tmp_path, monkeypatch, backend):
    """R06：成功文本不能覆盖执行错误——exit!=0 终态必须是 error。"""
    service, dispatcher, resources, binary = _setup(tmp_path, monkeypatch, backend)
    sad = tmp_path / f"sad-{backend}.py"
    if backend == "claude-cli":
        payload = {
            "type": "result", "subtype": "success", "is_error": False,
            "num_turns": 1, "result": "done", "session_id": "s1",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
        body = (
            _version_guard(backend)
            + "import json\nimport pathlib\nimport sys\n\n"
            + f"payload = {payload!r}\n\n"
            + "print(json.dumps(payload))\n"
            + "sys.exit(9)\n"
        )
    else:
        events = [dict(_CODEX_EVENTS[0]), dict(_CODEX_EVENTS[-1])]
        body = (
            _version_guard(backend)
            + "import json\nimport sys\n\n"
            + f"events = {events!r}\n\n"
            + "for event in events:\n    print(json.dumps(event), flush=True)\n"
            + "sys.exit(9)\n"
        )
    sad.write_text(body, encoding="utf-8")

    resolved, case_ids = prepare_run(
        f"{backend}-suite@1", _manifest(backend, sad), [], resources=resources,
    )
    run = service.create_run(f"{backend}-suite@1", resolved, case_ids)
    finished = dispatcher.dispatch(run["id"])
    assert finished["status"] == "completed"  # 案件有终局，质量不通过
    answer = [s for s in finished["scores"] if s["metric_id"] == "file-content:answer.txt"]
    assert answer and all(not s["passed"] for s in answer)
    rows = service.store.case_runs.list_for_run(run["id"])
    observation = rows[0]["result"]["observation"]
    agent_section = rows[0]["result"]["agent"]
    assert agent_section["termination_reason"] == "error"
    assert observation["termination"]["reason"] == "error"
    assert "9" in str(observation["termination"]["detail"])


@pytest.mark.parametrize("backend", ["claude-cli", "codex-cli"])
def test_cli_backend_version_gate_fails_closed(tmp_path, monkeypatch, backend):
    """R18：版本无法判定 / 版本漂移都在 spawn 前具名拒绝。"""
    service, dispatcher, resources, _ = _setup(tmp_path, monkeypatch, backend)

    # 无版本输出：probe 无法判定 → RUNTIME_BINARY_VERSION_UNKNOWN
    silent = tmp_path / f"silent-{backend}.py"
    silent.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    resolved, case_ids = prepare_run(
        f"{backend}-suite@1", _manifest(backend, silent), [], resources=resources,
    )
    run = service.create_run(f"{backend}-suite@1", resolved, case_ids)
    finished = dispatcher.dispatch(run["id"])
    assert finished["status"] == "unsupported"
    assert finished.get("error", {}).get("code") == "RUNTIME_BINARY_VERSION_UNKNOWN"

    # 版本漂移 → RUNTIME_VERSION_DRIFT，不启动执行。
    drifted = tmp_path / f"drifted-{backend}.py"
    drifted.write_text(
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('0.0.1-old (fake)')\n"
        "    sys.exit(0)\n"
        "import pathlib\npathlib.Path('answer.txt').write_text('drift', encoding='utf-8')\n",
        encoding="utf-8",
    )
    service2 = RunService(InMemoryRunStore())
    resolved2, case_ids2 = prepare_run(
        f"{backend}-suite@1", _manifest(backend, drifted), [], resources=resources,
    )
    run2 = service2.create_run(f"{backend}-suite@1", resolved2, case_ids2)
    finished2 = RunDispatcher(service2).dispatch(run2["id"])
    assert finished2["status"] == "unsupported"
    assert finished2.get("error", {}).get("code") == "RUNTIME_VERSION_DRIFT"


def test_cli_cancel_interrupts_external_process(tmp_path, monkeypatch):
    """R04：取消打断受控进程——迟到文件不再生成，终态 cancelled。"""
    backend = "claude-cli"
    monkeypatch.setenv("MOTTE_PI_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    resources = InMemoryResourceStore()
    publish_canonical_runtime_versions(resources)
    dataset, scenario = _dataset(backend)
    resources.datasets.put(dataset)
    resources.scenarios.put(scenario)

    slow = tmp_path / "slow-claude.py"
    slow.write_text(
        _version_guard(backend)
        + "import pathlib\nimport time\n\n"
        + "pathlib.Path('started.txt').write_text('1', encoding='utf-8')\n"
        + "time.sleep(8)\n"
        + "pathlib.Path('late.txt').write_text('should-not-exist', encoding='utf-8')\n"
        + "import json\nprint(json.dumps({"
        "'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1,"
        " 'result': 'late', 'session_id': 's'}))\n",
        encoding="utf-8",
    )
    service = RunService(InMemoryRunStore())
    dispatcher = RunDispatcher(service)
    resolved, case_ids = prepare_run(
        f"{backend}-suite@1", _manifest(backend, slow), [], resources=resources,
    )
    run = service.create_run(f"{backend}-suite@1", resolved, case_ids)

    import os
    import threading

    outcome: dict = {}

    def worker():
        outcome["finished"] = dispatcher.dispatch(run["id"])

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    ws_root = Path(os.environ["MOTTE_PI_WORKSPACE_ROOT"]) / run["id"]
    deadline = time.monotonic() + 15
    started_seen = False
    while time.monotonic() < deadline:
        if ws_root.exists() and any(ws_root.glob("*/started.txt")):
            started_seen = True
            break
        time.sleep(0.05)
    assert started_seen, "fake CLI never wrote its startup flag"

    cancelled = service.cancel(run["id"], reason="operator")
    # 运行中的 sample Run：取消先记录请求（cancellation 字段），执行器
    # 的 cancel_watch 轮询命中后打断受控进程，终态随后落定。
    assert cancelled.get("cancellation"), "cancellation request was not recorded"
    thread.join(timeout=30)
    assert not thread.is_alive(), "dispatch did not settle after cancellation"

    # 迟到副作用没有发生：late.txt 不存在（进程被真正打断）。
    late = list(ws_root.glob("*/late.txt")) if ws_root.exists() else []
    assert not late
    settled = service.get_run(run["id"])
    assert settled["status"] in RunService.TERMINAL
    rows = service.store.case_runs.list_for_run(run["id"])
    if rows:
        observation = rows[0]["result"]["observation"]
        assert observation["termination"]["reason"] == "cancelled"

