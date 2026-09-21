"""验收 F-08：场景 Run 的步骤下钻端点必须存在，且只投影已持久化的证据。

真实事故：Web 的 ScenarioPages 请求 /api/v1/runs/{run_id}/steps，服务端从未注册
该端点（404），页面永久显示「逐步证据 能力不可用（HTTP 404）」「Workflow 未知」。
M5 的"逐步可验证"因此在 UI 上完全不可见。

这里钉住三件事：有观察时按观察投影（步骤状态、断言、checkpoint 的冻结 hash）、
没有观察时回退到声明的步骤并标 unknown=True，以及路由本身已注册。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.api.app.scenario_steps import build_scenario_steps_view
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

WORKFLOW_SNAPSHOT = {
    "workflow_id": "order-cancel",
    "version": "1",
    "content_hash": "sha256:" + "a" * 64,
    "steps": [
        {"step_id": "request", "kind": "send_message", "message": "请取消订单 order-1",
         "timeout_sec": 30},
        {"step_id": "final-check", "kind": "checkpoint", "label": "final"},
    ],
    "fixture_refs": [],
}

OBSERVATION = {
    "schema": "workflow-observation@1",
    "workflow_ref": "order-cancel@1",
    "workflow_content_hash": "sha256:" + "a" * 64,
    "status": "completed",
    "reason": None,
    "steps": [
        {"seq": 1, "step_id": "request", "kind": "send_message", "status": "succeeded",
         "detail": None, "duration_ms": 12.5, "depth": 1,
         "assertions": [{"condition": "step_results.request.status == sent",
                         "satisfied": True, "actual": "sent"}]},
        {"seq": 2, "step_id": "final-check", "kind": "checkpoint", "status": "succeeded",
         "detail": None, "duration_ms": 0.5, "depth": 1, "assertions": []},
    ],
    "checkpoints": {"final-check": {"content_hash": "sha256:" + "b" * 64, "label": "final"}},
}


def run_view(**overrides):
    run = {
        "id": "run-1",
        "status": "completed",
        "case_ids": ["case-1"],
        "manifest": {
            "workflow_snapshot": WORKFLOW_SNAPSHOT,
            "fixture_snapshot": {
                "order-state@1": {
                    "fixture_id": "order-state", "version": 1, "kind": "json",
                    "isolation": "per_case",
                }
            },
        },
    }
    run.update(overrides)
    return run


def rows_with_observation(cleanup=None):
    return [{
        "run_id": "run-1", "case_id": "case-1", "outcome": "responded",
        "result": {
            "scenario": {"workflow_ref": "order-cancel@1", "status": "completed"},
            "observation": OBSERVATION,
            "cleanup": list(cleanup or []),
        },
    }]


def test_projection_uses_the_persisted_observation():
    view = build_scenario_steps_view(run_view(), rows_with_observation())

    assert view["unknown"] is False
    assert view["workflow"] == {
        "workflow_id": "order-cancel", "version": "1",
        "content_hash": "sha256:" + "a" * 64,
    }
    steps = {step["step_id"]: step for step in view["steps"]}
    assert set(steps) == {"request", "final-check"}
    assert steps["request"]["status"] == "succeeded"
    assert steps["request"]["assertions"][0]["satisfied"] is True
    # checkpoint 的冻结内容 hash 作为该步的证据暴露出来
    assert steps["final-check"]["checkpoint"] == {
        "label": "final", "frozen": True, "state_hash": "sha256:" + "b" * 64,
    }
    assert view["fixtures"] == [{
        "fixture_id": "order-state", "version": 1, "kind": "json",
        "owner": "per_case", "isolated": True, "snapshot": None, "cleanup": None,
    }]


def test_projection_reports_fixture_cleanup_when_recorded():
    view = build_scenario_steps_view(
        run_view(),
        rows_with_observation([{"fixture_id": "order-state", "status": "cleaned",
                                "residual": [], "error": None}]),
    )

    assert view["fixtures"][0]["cleanup"] == {
        "status": "cleaned", "residual": [], "error": None,
    }


def test_projection_without_evidence_falls_back_to_declared_steps_and_says_unknown():
    """没有观察时不发明状态：只给声明的步骤，并标 unknown=True。"""
    view = build_scenario_steps_view(run_view(status="queued"), [])

    assert view["unknown"] is True
    assert [step["step_id"] for step in view["steps"]] == ["request", "final-check"]
    assert all(step["status"] is None and step["unknown"] is True for step in view["steps"])


def test_route_is_registered_and_404s_for_an_unknown_run():
    client = TestClient(create_app(InMemoryRunStore(), resource_store=InMemoryResourceStore()))

    missing = client.get("/api/v1/runs/run-missing/steps")
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "RUN_NOT_FOUND"
