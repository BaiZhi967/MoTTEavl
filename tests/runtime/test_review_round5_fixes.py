"""M1 review 第五轮（3320c43）反例回归：2 项修复。

#1 超时结算失败后，迟到线程不得改写隔离中的调用记录。
#2 父目录校验后被替换为 symlink 的创建竞态，基于受信 fd 的相对创建。
全部离线：scripted provider、本地目录；不触发任何真实模型或网络。
"""
from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from motte_contracts.agent_tasks import normalize_agent_tasks_dataset, scenario_for_agent_tasks
from motte_storage.run_store import InMemoryRunStore
from motte_sdk.execution_backends import build_execution_handle
from motte_sdk.service import RunService

SECRET = "sk-SENTINEL9988776655443322"


# ---------------------------------------------------------------- 公共辅助

def _manifest(tmp_path, cases, mode="native-tool", budget=None):  # noqa: ANN001
    dataset = normalize_agent_tasks_dataset({
        "name": "r5-tasks", "version": "1", "cases": cases,
    })
    scenario = scenario_for_agent_tasks(dataset)
    from motte_sdk.agent_tasks import resolve_agent_tasks_manifest

    resources = SimpleNamespace(
        datasets=SimpleNamespace(
            get=lambda name, version: dataset
            if f"{name}@{version}" == "r5-tasks@1" else None,
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
    return resolve_execution("r5-tasks@1", resolved, scenario=scenario)


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_AGENT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))


# ---------------------------------------------------------------- #1 结算权威

def test_timeout_settle_failure_freezes_invocation(tmp_path, monkeypatch):
    """模型调用超时 + 首次结算注入临时存储错误（Run → needs_review）：
    迟到的模型返回不得再改写隔离中的 invocation。

    关键反例构造：settle 存储只坏第一次（后续成功），证明修复依赖的是
    "后台线程失去结算权限"，而不是"存储一直坏"。
    """
    service = RunService(InMemoryRunStore())
    cases = [{"case_id": "case-1", "input": "slow call", "fixture": {}, "expected": {}}]
    manifest = _manifest(
        tmp_path, cases, budget={"per_call_timeout_sec": 0.05, "max_steps": 2},
    )

    release = threading.Event()
    provider_returned = threading.Event()

    def blocking_complete(request):  # noqa: ANN001
        release.wait(5)
        provider_returned.set()
        return {"content": "late", "tool_calls": []}

    provider = SimpleNamespace(provider=SimpleNamespace(complete=blocking_complete))

    invocations_repo = service.store.invocations
    original_transition = invocations_repo.transition
    settled_attempts = {"n": 0}

    def flaky_transition(invocation_id, **kwargs):  # noqa: ANN001
        if kwargs.get("status") == "settled":
            settled_attempts["n"] += 1
            if settled_attempts["n"] == 1:  # 仅主流程的超时结算坏一次
                raise OSError("transient invocation store outage")
        return original_transition(invocation_id, **kwargs)

    monkeypatch.setattr(invocations_repo, "transition", flaky_transition)

    import motte_sdk.agent_backend as agent_backend
    from motte_sdk.dispatcher import RunDispatcher

    original_build = agent_backend.build_agent_provider
    agent_backend.build_agent_provider = lambda m: provider
    try:
        run = service.create_run("r5-tasks@1", manifest, ["case-1"])
        dispatcher = RunDispatcher(service)
        claimed = dispatcher.claim()
        handle = build_execution_handle(claimed)
        if handle.attach:
            handle.attach(service, run["id"])
        result = service.execute(run["id"], provider=handle.invoke)
    finally:
        agent_backend.build_agent_provider = original_build

    assert result["status"] == "needs_review", json.dumps(result.get("error"), ensure_ascii=False)
    assert settled_attempts["n"] == 1

    invocations_before = service.store.invocations.list_for_run(run["id"])
    assert any(item["status"] == "dispatching" for item in invocations_before)

    # 释放阻塞的模型调用：迟到返回只被丢弃，不得触发任何新的 settled 写入
    release.set()
    assert provider_returned.wait(2), "late provider return must run"
    time.sleep(0.1)
    invocations_after = service.store.invocations.list_for_run(run["id"])
    assert invocations_after == invocations_before, (
        "late thread must not mutate the quarantined invocation"
    )
    assert settled_attempts["n"] == 1  # 没有第二次结算尝试


# ---------------------------------------------------------------- #2 父目录替换竞态

def test_parent_swap_during_creation_cannot_redirect(tmp_path, monkeypatch):
    """父目录校验后、子目录创建前，把父目录换成指向 victim 的 symlink：
    创建基于受信 fd 执行，绝不落到外部目录。"""
    from motte_sandbox.workspace import CaseWorkspace, WorkspacePolicyError

    base = tmp_path / "ws"
    base.mkdir()
    victim = tmp_path / "victim-dir"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")

    original = CaseWorkspace._open_or_create_component
    swaps = {"n": 0}

    def racing_open(self, parent_fd, part, *, create):  # noqa: ANN001
        if part == "case-1" and swaps["n"] == 0:
            swaps["n"] += 1
            # 竞态注入点：run-1 已通过 fd 校验（fd 持有原 inode），在 case-1
            # 创建前把 run-1 的路径替换为指向 victim 的 symlink
            run1 = base / "run-1"
            os.rmdir(run1)  # 此刻 run-1 为空目录
            os.symlink(victim, run1)
        return original(self, parent_fd, part, create=create)

    monkeypatch.setattr(CaseWorkspace, "_open_or_create_component", racing_open)

    with pytest.raises(WorkspacePolicyError):
        CaseWorkspace(base / "run-1" / "case-1", anchor=base)

    # victim 内没有任何新建目录；越界创建被拒绝而不是被重定向
    assert swaps["n"] == 1
    assert sorted(item.name for item in victim.iterdir()) == ["keep.txt"]


def test_fd_chain_validation_still_supports_normal_paths(tmp_path):
    """fd 化改造后的回归：正常目录链创建、读写、清理不变。"""
    from motte_sandbox.workspace import CaseWorkspace

    base = tmp_path / "ws"
    workspace = CaseWorkspace(base / "run-1" / "case-1", anchor=base)
    workspace.write_text("deep/nested/out.txt", "v")
    assert workspace.read_text("deep/nested/out.txt") == "v"
    snapshot = workspace.snapshot()
    assert snapshot["complete"] is True and "deep/nested/out.txt" in snapshot["files"]
    assert workspace.cleanup() == {"status": "success", "residual": []}
    assert not (base / "run-1" / "case-1").exists()
