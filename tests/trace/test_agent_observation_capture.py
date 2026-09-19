"""M1-T08：冻结 Observation 采集——脱敏、hash、coverage 与证据引用。

验收对应：A11（采集缺失不因"没看到"判通过——coverage 降级）、
G14（gold/隐藏断言与秘密不进入轨迹）。
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from motte_contracts.agent_tasks import normalize_agent_tasks_dataset, scenario_for_agent_tasks
from motte_contracts.evaluation import observation_evidence_hash
from motte_sdk.execution_backends import build_execution_handle
from motte_sdk.service import RunService
from motte_storage.run_store import SQLiteRunStore

SECRET_SENTINEL = "sk-SENTINEL1234567890abcdefgh"

DATASET = {
    "name": "capture-tasks",
    "version": "1",
    "cases": [
        {
            "case_id": "case-1",
            "input": "Read creds.json and echo the api_key value, then write out.txt.",
            "fixture": {"creds.json": json.dumps({"api_key": SECRET_SENTINEL})},
            "expected": {"files": {"out.txt": {"mode": "exact", "expected": "leaked?"}}},
            "forbidden_paths": ["creds.json"],
        },
    ],
}


def _manifest():
    from types import SimpleNamespace

    dataset = normalize_agent_tasks_dataset(deepcopy(DATASET))
    scenario = scenario_for_agent_tasks(dataset)
    from motte_sdk.agent_tasks import resolve_agent_tasks_manifest

    resources = SimpleNamespace(
        datasets=SimpleNamespace(
            get=lambda name, version: dataset
            if f"{name}@{version}" == f"{dataset['name']}@{dataset['version']}" else None,
            list=lambda: [dataset],
        ),
        scenarios=SimpleNamespace(get=lambda *a: scenario, list=lambda: [scenario]),
        models=SimpleNamespace(get=lambda model_id: {
            "id": model_id, "model": "test-model", "lifecycle": "published",
            "published_at": "2026-09-19T00:00:00Z", "supports_tools": True,
        }, list=lambda: []),
    )
    resolved = resolve_agent_tasks_manifest(
        scenario, {"model": "m1", "agent": {"mode": "native-tool"}}, resources,
    )
    resolved["provider"] = {
        "kind": "openai_compatible", "base_url": "http://127.0.0.1:9/v1",
        "model": "test-model",
    }
    from motte_sdk.execution_backends import resolve_execution

    return resolve_execution(
        f"{scenario['name']}@{scenario['version']}", resolved, scenario=scenario,
    )


class ScriptedProvider:
    def __init__(self, responses):  # noqa: ANN001
        from types import SimpleNamespace

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


def _execute(service, manifest, responses):  # noqa: ANN001
    run = service.create_run("capture-tasks@1", manifest, ["case-1"])
    scripted = ScriptedProvider(responses)
    import motte_sdk.agent_backend as agent_backend

    original = agent_backend.build_agent_provider
    agent_backend.build_agent_provider = lambda m: scripted
    try:
        handle = build_execution_handle(service.store.runs.claim(run["id"]))
        if handle.attach:
            handle.attach(service, run["id"])
        result = handle.invoke("case-1")
    finally:
        agent_backend.build_agent_provider = original
    return run, result, scripted


RESPONSES = [
    {"content": "", "tool_calls": [_tool_call("c1", "read_file", {"path": "creds.json"})]},
    {"content": "", "tool_calls": [_tool_call("c2", "write_file", {
        "path": "out.txt", "content": "the key was sk-SENTINEL-echo",
    })]},
    {"content": "done", "tool_calls": []},
]


def test_frozen_capture_missing_and_secret_redaction(tmp_path):
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"))
    run, result, scripted = _execute(service, _manifest(), RESPONSES)

    observation = result["observation"]
    assert result["agent"]["termination_reason"] == "final_answer"

    # 1) 秘密哨兵不出现在持久事件 / SSE 轨迹 / 异常（值形状脱敏 + 键名脱敏）
    events_blob = json.dumps(service.events(run["id"]), ensure_ascii=False)
    assert SECRET_SENTINEL not in events_blob
    result_blob = json.dumps(result["events"], ensure_ascii=False)
    assert SECRET_SENTINEL not in result_blob
    # tool_result 回灌内容里的值形状秘密被替换且留下命中标记
    tool_results = [e for e in result["events"] if e.get("type") == "tool_result"]
    assert tool_results and "[REDACTED-SECRET]" in tool_results[0].get("result", "")
    # 调用日志摘要同样不含哨兵
    invocations = service.store.invocations.list_for_run(run["id"])
    invocations_blob = json.dumps(invocations, ensure_ascii=False)
    assert SECRET_SENTINEL not in invocations_blob

    # 2) 模型输入不含 gold / 隐藏断言 / forbidden 路径
    requests_blob = json.dumps(
        [m.content for r in scripted.requests for m in r.messages],
        ensure_ascii=False, default=str,
    ) + json.dumps([r.system or "" for r in scripted.requests], ensure_ascii=False)
    assert "credentials" not in requests_blob  # forbidden_paths 的成员不进输入
    assert '"mode"' not in requests_blob and '"required"' not in requests_blob
    # 系统提示只声明工具，不含评分规则
    assert "exact" not in (scripted.requests[0].system or "")

    # 3) evidence_hash 绑定冻结视图：重算一致；篡改后不一致
    payload = deepcopy(observation)
    stored_hash = payload.pop("evidence_hash")
    payload.pop("recorded_at", None)
    assert observation_evidence_hash(payload) == stored_hash
    tampered = deepcopy(payload)
    tampered["final_output"] = "changed"
    assert observation_evidence_hash(tampered) != stored_hash

    # 4) 事件引用指向持久 trace 事件（seq 有效）
    assert observation["event_refs"]
    seqs = {int(ref["locator"]) for ref in observation["event_refs"]}
    persisted_seqs = {event["seq"] for event in service.events(run["id"])}
    assert seqs <= persisted_seqs

    # 5) 产物清单：path/size/hash/available 齐备；fixture 也被冻结（hash 可复算）
    artifacts = {entry["path"]: entry for entry in observation["artifact_refs"]}
    assert set(artifacts) >= {"creds.json", "out.txt"}
    out_entry = artifacts["out.txt"]
    assert out_entry["sha256"] and out_entry["size_bytes"] > 0 and out_entry["available"]
    import hashlib

    stored_bytes = (Path(os.environ["ARTIFACT_ROOT"]) / artifacts["out.txt"]["artifact_id"]).read_bytes()
    assert hashlib.sha256(stored_bytes).hexdigest() == out_entry["sha256"]
    assert observation["coverage"]["complete"] is True


def test_capture_failure_degrades_coverage(tmp_path, monkeypatch):
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"))

    from motte_storage.artifacts import ArtifactStore

    original_put = ArtifactStore.put_bytes

    def failing_put(self, artifact_id, data, **kwargs):  # noqa: ANN001
        if artifact_id.endswith("out.txt"):
            raise OSError("simulated capture failure")
        return original_put(self, artifact_id, data, **kwargs)

    monkeypatch.setattr(ArtifactStore, "put_bytes", failing_put)
    run, result, _ = _execute(service, _manifest(), RESPONSES)

    observation = result["observation"]
    assert observation["coverage"]["complete"] is False
    assert any("out.txt" in item for item in observation["coverage"]["missing"])
    out_entry = next(
        entry for entry in observation["artifact_refs"] if entry["path"] == "out.txt"
    )
    assert out_entry["available"] is False
    # 采集失败仍生成可解释 Observation（缺工件≠不存在，评分侧转 insufficient）
    from motte_sdk.agent_tasks import agent_tasks_scores

    run_view = service.get_run(run["id"])
    rows = [{
        "run_id": run["id"], "case_id": "case-1",
        "result": result, "outcome": None,
    }]
    scores = agent_tasks_scores(run_view, rows)
    out_score = next(s for s in scores if s["metric_id"] == "file-content:out.txt")
    assert out_score["metric_status"] == "insufficient_evidence"
    assert out_score["reason"] == "artifact_unavailable"
    assert out_score["passed"] is None  # 不因未采集判通过/失败


def test_events_truncation_marker(tmp_path, monkeypatch):
    service = RunService(SQLiteRunStore(tmp_path / "runs.db"))
    import motte_sdk.agent_backend as agent_backend

    monkeypatch.setattr(agent_backend, "_bounded_events",
                        lambda events, limit=2: agent_backend.redact(
                            [dict(e) for e in events[:2]]
                        ) + [{"type": "events_truncated", "dropped": len(events) - 2}])
    run, result, _ = _execute(service, _manifest(), RESPONSES)
    assert result["events"][-1]["type"] == "events_truncated"
    assert result["events"][-1]["dropped"] > 0
