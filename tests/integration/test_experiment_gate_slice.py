"""M6-T11：Experiment → Dispatcher → Report → Compare → Baseline → Gate 跨层切片。

固定集成矩阵的公共链路部分（全部离线、scripted provider、零费用）：

- API preview/create/allocate → 真实 RunDispatcher/Worker 执行 → ScoringPass
  → ReportSnapshot → compare（三级结论）→ baseline（固定 pass）→ 版本化
  gate（六类决策/退出码）→ 导出 JSON/JUnit；
- 同一报告重复 compare/gate/export：零模型调用、hash 不变（A18）；
- baseline current pass 漂移不改既有结论（A09）；
- 候选缺样本：缺失留在分母，完整覆盖 Gate 不通过（A03）；
- 取消实验只影响归属 Run（A20 公共入口部分）。

本文件只通过公共 HTTP 入口与既有 Dispatcher 驱动，不触库、无 SQL。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

#: m6-accept-* 受控测试数据（离线合成；live 验收另建同名前缀数据集）。
CASES = [
    {"case_id": "m6-accept-001", "input": "Reply with the single word: alpha",
     "expected": "alpha", "scorer": "exact"},
    {"case_id": "m6-accept-002", "input": "Reply with the single word: beta",
     "expected": "beta", "scorer": "exact"},
    {"case_id": "m6-accept-003", "input": "Reply with the single word: gamma",
     "expected": "gamma", "scorer": "exact"},
    {"case_id": "m6-accept-004", "input": "Describe anything.",
     "scorer": "exact"},  # 无期望 → no_expectation（A04 覆盖在 snapshot 层）
]

#: 逐题脚本响应（direct-llm provider 形状：content/usage/cost）。
SCRIPTED_ANSWERS = {
    "m6-accept-001": "alpha",
    "m6-accept-002": "beta",
    "m6-accept-003": "WRONG",  # 答错：关键样本规则必须失败
    "m6-accept-004": "something",
}


class ScriptedProvider:
    """direct-llm 的逐题 provider：按 case 返回脚本答案；可注入调用失败。"""

    def __init__(self):
        self.calls: list[str] = []
        self.fail_on: set[str] = set()

    def __call__(self, case_id: str) -> dict:
        self.calls.append(case_id)
        if case_id in self.fail_on:
            return {"error": {"class": "timeout", "message": "m6 scripted failure"}}
        return {
            "content": SCRIPTED_ANSWERS[case_id],
            "usage": {"total_tokens": 7},
            "cost": {"total": 0.001, "price_table_version": "v1"},
        }


def _import_dataset(client: TestClient) -> None:
    imported = client.post("/api/v1/benchmarks/direct-llm/import", json={
        "name": "m6-accept-dataset",
        "content": "\n".join(json.dumps(case) for case in CASES),
    })
    assert imported.status_code in (200, 201), imported.text


def _publish_model(client: TestClient) -> None:
    created = client.post("/api/v1/providers", json={
        "name": "m6-accept-provider", "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:9/v1",
    })
    assert created.status_code in (200, 201), created.text
    model = client.post("/api/v1/models", json={
        "id": "m6-accept-model", "provider": "m6-accept-provider",
        "capabilities": {}, "supports_tools": False,
    })
    assert model.status_code in (200, 201), model.text
    published = client.post("/api/v1/models/m6-accept-model/publish")
    assert published.status_code == 200, published.text


@pytest.fixture()
def slice_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    store = InMemoryRunStore()
    resources = InMemoryResourceStore()
    application = create_app(store, resources)
    client = TestClient(application)
    provider = ScriptedProvider()
    _publish_model(client)
    _import_dataset(client)
    return client, application, provider


def _drain_runs(application, count, provider):
    """真实 Dispatcher claim + RunService 执行（direct-llm provider 形状）。"""
    service = application.state.run_service
    from motte_sdk.dispatcher import RunDispatcher

    dispatcher = RunDispatcher(service)
    executed = []
    for _ in range(count):
        claimed = dispatcher.claim()
        if claimed is None:
            break
        executed.append(service.execute(claimed["id"], provider=provider))
    return executed


def _run_experiment(slice_env, *, experiment_id="m6-accept-exp"):
    """preview → create → allocate → dispatcher 执行。"""
    client, application, provider = slice_env
    spec = {
        "experiment_id": experiment_id,
        "version": "1",
        "task_ref": {"suite": "direct-llm",
                     "scenario_version": "m6-accept-dataset@1"},
        "factors": {"model_profile": ["m6-accept-model"]},
        "repeats": 1,
        "budget_policy": {"max_total_calls": 12},
        "created_by": "m6-integration",
        "reason": "m6 slice",
    }
    preview = client.post("/api/v1/experiments/preview", json=spec)
    assert preview.status_code == 200, preview.text
    assert preview.json()["cell_count"] == 1
    assert preview.json()["violations"] == []
    created = client.post("/api/v1/experiments", json=spec)
    assert created.status_code == 202, created.text
    outcome = created.json()
    assert outcome["failed"] == []
    cells = outcome["cells"]
    assert cells and cells[0]["allocation_status"] == "allocated"
    executed = _drain_runs(application, len(cells), provider=provider)
    return spec, outcome, executed


def test_comparison_http_uses_fixed_pass_and_detects_case_and_scorer_changes(slice_env):
    """T04: real create_app routes, with immutable pass references and no model calls."""
    client, application, provider = slice_env
    store = application.state.run_service.store
    for run_id, cases in [("m8-base", ["a", "b"]),
                          ("m8-same", ["a", "b"]),
                          ("m8-different", ["a", "c"])]:
        store.runs.create({
            "id": run_id, "schema_version": 2, "revision": 1,
            "scenario_version": "m8@1", "status": "completed",
            "manifest": {"evaluation": {"scorer_id": "exact", "scorer_version": "1"},
                         "model": run_id},
            "requested_manifest": {}, "case_ids": cases,
            "created_at": "2026-09-23T00:00:00Z",
            "updated_at": "2026-09-23T00:00:00Z",
        })
        store.scoring_passes.append({
            "id": f"old-{run_id}", "run_id": run_id,
            "scorer_id": "exact", "scorer_version": "1",
            "created_at": "2026-09-23T00:00:00Z", "source": "initial",
            "source_run_revision": 1, "summary": {},
        }, [{"case_id": case_id, "passed": True, "metric_id": "accuracy",
             "evaluator_id": "exact", "evaluator_version": "1",
             "metric_status": "scored", "value": 1.0, "denominator": True,
             "details": {}} for case_id in cases])
    first = client.get("/api/v1/comparisons", params={
        "baseline": "m8-base", "candidate": "m8-same", "factors": "model",
        "baseline_pass": "old-m8-base", "candidate_pass": "old-m8-same",
    })
    assert first.status_code == 200, first.text
    assert first.json()["metric_eligibility"]["quality"] is True
    assert first.json()["refs"]["baseline"]["scoring_pass_id"] == "old-m8-base"
    assert first.json()["refs"]["candidate"]["scoring_pass_id"] == "old-m8-same"
    statistics = client.get("/api/v1/comparisons/statistics", params={
        "baseline": "m8-base", "candidate": "m8-same", "factors": "model",
        "baseline_pass": "old-m8-base", "candidate_pass": "old-m8-same",
    })
    assert statistics.status_code == 200, statistics.text
    assert statistics.json()["applicable"] is True
    assert statistics.json()["n_pairs"] == 2
    assert statistics.json()["statistics"]["interval"]["seed"] == 20260921
    different = client.get("/api/v1/comparisons", params={
        "baseline": "m8-base", "candidate": "m8-different", "factors": "model",
        "baseline_pass": "old-m8-base", "candidate_pass": "old-m8-different",
    })
    assert different.status_code == 200, different.text
    assert different.json()["metric_eligibility"]["quality"] is False
    assert different.json()["case_diff"] == {"added": ["c"], "removed": ["b"], "changed": []}
    store.scoring_passes.append({
        "id": "new-m8-base", "run_id": "m8-base",
        "scorer_id": "other", "scorer_version": "2",
        "created_at": "2026-09-23T01:00:00Z", "source": "rescore",
        "source_run_revision": 1, "summary": {},
    }, [{"case_id": case_id, "passed": False, "metric_id": "accuracy",
         "evaluator_id": "other", "evaluator_version": "2",
         "metric_status": "scored", "value": 0.0, "denominator": True,
         "details": {}} for case_id in ["a", "b"]])
    fixed = client.get("/api/v1/runs/m8-base/report-snapshot",
                       params={"scoring_pass_id": "old-m8-base"})
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["metric_values"]["accuracy"] == 1.0
    assert client.get("/api/v1/comparisons", params={
        "baseline": "m8-base", "candidate": "m8-same", "factors": "model",
        "baseline_pass": "old-m8-base", "candidate_pass": "old-m8-same",
    }).json()["metric_eligibility"]["quality"] is True
    changed = client.get("/api/v1/comparisons", params={
        "baseline": "m8-base", "candidate": "m8-same", "factors": "model",
        "candidate_pass": "old-m8-same",
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["metric_eligibility"]["quality"] is False
    fixed_statistics = client.get("/api/v1/comparisons/statistics", params={
        "baseline": "m8-base", "candidate": "m8-same", "factors": "model",
        "baseline_pass": "old-m8-base", "candidate_pass": "old-m8-same",
    })
    assert fixed_statistics.json() == statistics.json()
    assert provider.calls == []


def test_terminal_trial_statistics_http_uses_fixed_pass_and_k(slice_env):
    client, application, provider = slice_env
    store = application.state.run_service.store
    for run_id in ("tb-base", "tb-candidate"):
        plan = [
            {"trial_id": f"{run_id}-{task}-{repeat}", "task_key": task,
             "repeat_index": repeat, "run_id": run_id,
             "agent_config_hash": "agent-a", "environment_hash": "env-a"}
            for task in ("a", "b") for repeat in range(2)
        ]
        store.runs.create({
            "id": run_id, "schema_version": 2, "revision": 1,
            "scenario_version": "terminal-bench@1", "status": "completed",
            "manifest": {"benchmark_provenance": {"suite": "terminal-bench-harbor"},
                         "task_manifest": {"trials": plan},
                         "evaluation": {"scorer_id": "harbor", "scorer_version": "1"}},
            "requested_manifest": {}, "case_ids": ["a", "b"],
            "created_at": "2026-09-23T00:00:00Z", "updated_at": "2026-09-23T00:00:00Z",
        })
        store.scoring_passes.append({
            "id": f"pass-{run_id}", "run_id": run_id,
            "scorer_id": "harbor", "scorer_version": "1",
            "created_at": "2026-09-23T00:00:00Z", "source": "initial",
            "source_run_revision": 1, "summary": {},
        }, [{
            "case_id": task, "trial_id": f"{run_id}-{task}-{repeat}",
            "metric_id": "reward", "evaluator_id": "harbor",
            "evaluator_version": "1", "metric_status": "scored",
            "unit": "trial", "value": 1.0 if repeat == 0 else 0.0,
            "passed": repeat == 0, "denominator": True,
            "details": {"repeat_index": repeat},
        } for task in ("a", "b") for repeat in range(2)])
    params = {"baseline": "tb-base", "candidate": "tb-candidate", "factors": "model",
              "baseline_pass": "pass-tb-base", "candidate_pass": "pass-tb-candidate",
              "k": 2}
    response = client.get("/api/v1/comparisons/statistics", params=params)
    assert response.status_code == 200, response.text
    stats = response.json()
    assert stats["trial_aggregation"]["baseline"]["per_task"]["a"]["pass_at_k"]["value"] == 1.0
    assert stats["refs"]["baseline"]["scoring_pass_id"] == "pass-tb-base"
    assert stats["n_pairs"] == 2
    invalid = client.get("/api/v1/comparisons/statistics", params={**params, "k": 0})
    assert invalid.status_code == 422
    assert provider.calls == []


def test_experiment_to_gate_full_slice(slice_env):
    """端到端：Run → ScoringPass → ReportSnapshot → Compare → Baseline → Gate → Export。"""
    client, application, provider = slice_env
    spec, outcome, executed = _run_experiment(slice_env)
    assert len(executed) == 1
    run = executed[0]
    assert run["status"] == "completed", json.dumps(run.get("error"))
    run_id = run["id"]

    passes = client.get(f"/api/v1/runs/{run_id}/scoring-passes").json()
    assert passes["total"] >= 1
    pass_id = passes["items"][0]["id"]

    snapshot = client.get(
        f"/api/v1/runs/{run_id}/report-snapshot", params={"scoring_pass_id": pass_id},
    )
    assert snapshot.status_code == 200, snapshot.text
    snap = snapshot.json()
    assert snap["counts"]["selected"] == 4
    # 003 答错仍 judged；004 无期望 → no_expectation（不进质量分母但保持可见）。
    assert snap["counts"]["judged"] == 3
    assert snap["counts"]["no_expectation"] == 1
    assert snap["counts"]["not_attempted"] == 0
    assert snap["counts"]["scored"] == 2
    assert snap["metric_values"]["judged_accuracy"] is not None

    baseline = client.post("/api/v1/baselines", json={
        "baseline_id": "m6-accept-baseline",
        "entries": [{"cell_key": None, "run_id": run_id,
                     "scoring_pass_id": pass_id}],
        "policy": {"allowed_factors": ["model"]},
        "created_by": "m6-integration", "reason": "m6 baseline",
    })
    assert baseline.status_code == 201, baseline.text
    selected = client.post("/api/v1/baselines/default", json={
        "scope": "m6-accept", "baseline_id": "m6-accept-baseline",
        "updated_by": "m6-integration", "reason": "first",
    })
    assert selected.status_code == 200, selected.text

    policy = client.post("/api/v1/gate-policies", json={
        "policy_id": "m6-accept-gate-policy", "version": "1",
        "rules": [
            {"rule_id": "acc", "kind": "metric_threshold", "metric_id": "accuracy",
             "operator": "gte", "threshold": 0.5},
            {"rule_id": "cov", "kind": "coverage", "min_coverage": 1.0},
            {"rule_id": "key", "kind": "critical_case",
             "critical_case_ids": ["m6-accept-003"]},
        ],
        "created_by": "m6-integration", "reason": "m6 policy",
    })
    assert policy.status_code == 201, policy.text

    gate = client.post("/api/v1/gates/versioned", json={
        "policy_id": "m6-accept-gate-policy", "policy_version": "1",
        "run_id": run_id, "scoring_pass_id": pass_id,
    })
    assert gate.status_code == 200, gate.text
    result = gate.json()
    # judged 3/4（004 无期望不进质量分母）→ 完整覆盖规则 insufficient，
    # 优先级高于质量失败（协议 §6）；关键样本失败同时保留在结果里。
    assert result["decision"] == "insufficient_evidence"
    assert result["exit_code"] == 5
    rule_map = {rule["rule_id"]: rule for rule in result["rule_results"]}
    assert rule_map["acc"]["status"] == "pass"
    assert rule_map["cov"]["status"] == "insufficient"
    assert rule_map["key"]["status"] == "fail"

    # 去掉覆盖规则后：A17——completed 不等于通过，quality_fail（退出码 1）。
    quality_policy = client.post("/api/v1/gate-policies", json={
        "policy_id": "m6-accept-quality-policy", "version": "1",
        "rules": [
            {"rule_id": "acc", "kind": "metric_threshold", "metric_id": "accuracy",
             "operator": "gte", "threshold": 0.5},
            {"rule_id": "key", "kind": "critical_case",
             "critical_case_ids": ["m6-accept-003"]},
        ],
        "created_by": "m6-integration", "reason": "m6 quality policy",
    })
    assert quality_policy.status_code == 201, quality_policy.text
    result = client.post("/api/v1/gates/versioned", json={
        "policy_id": "m6-accept-quality-policy", "policy_version": "1",
        "run_id": run_id, "scoring_pass_id": pass_id,
    }).json()
    assert result["decision"] == "quality_fail"
    assert result["exit_code"] == 1
    rule_map = {rule["rule_id"]: rule for rule in result["rule_results"]}
    assert rule_map["key"]["status"] == "fail"
    gate_result_id = result["gate_result_id"]

    calls_before = len(provider.calls)
    again = client.post("/api/v1/gates/versioned", json={
        "policy_id": "m6-accept-quality-policy", "policy_version": "1",
        "run_id": run_id, "scoring_pass_id": pass_id,
    }).json()
    assert again["gate_result_id"] == gate_result_id
    assert again["conclusion_hash"] == result["conclusion_hash"]
    stored = client.get(f"/api/v1/gates/results/{gate_result_id}").json()
    assert stored["decision"] == "quality_fail"
    export_json = client.get(
        f"/api/v1/gates/results/{gate_result_id}/export", params={"format": "json"},
    )
    assert export_json.status_code == 200
    assert export_json.json()["exit_code"] == 1
    export_junit = client.get(
        f"/api/v1/gates/results/{gate_result_id}/export", params={"format": "junit"},
    )
    assert export_junit.status_code == 200
    assert "<testsuite" in export_junit.text
    assert len(provider.calls) == calls_before

    comparison = client.get("/api/v1/comparisons", params={
        "baseline": run_id, "candidate": run_id,
        "baseline_pass": pass_id, "candidate_pass": pass_id,
    }).json()
    assert comparison["level"] == "comparable"
    assert comparison["case_diff"]["changed"] == []

    rescored = client.post(f"/api/v1/runs/{run_id}/rescore")
    assert rescored.status_code == 200, rescored.text
    baseline_after = client.get("/api/v1/baselines/m6-accept-baseline").json()
    assert baseline_after["entries"][0]["ref"]["scoring_pass_id"] == pass_id
    still = client.post("/api/v1/gates/versioned", json={
        "policy_id": "m6-accept-quality-policy", "policy_version": "1",
        "run_id": run_id, "scoring_pass_id": pass_id,
        "baseline_id": "m6-accept-baseline",
    }).json()
    assert still["gate_result_id"] != gate_result_id
    assert still["rule_results"]
    assert len(provider.calls) == calls_before


def test_missing_sample_blocks_full_coverage_gate(slice_env):
    """A03：case 3 硬失败 → stop_run → case 4 not_attempted；覆盖不足不放行。"""
    client, application, provider = slice_env
    provider.fail_on = {"m6-accept-003"}
    spec = {
        "experiment_id": "m6-accept-missing",
        "version": "1",
        "task_ref": {"suite": "direct-llm",
                     "scenario_version": "m6-accept-dataset@1"},
        "factors": {"model_profile": ["m6-accept-model"]},
        "repeats": 1,
        "budget_policy": {"max_total_calls": 12},
        "created_by": "m6-integration", "reason": "partial run",
    }
    created = client.post("/api/v1/experiments", json=spec)
    assert created.status_code == 202, created.text
    run_id = created.json()["cells"][0]["run_id"]
    executed = _drain_runs(application, 1, provider=provider)
    assert len(executed) == 1

    snapshot = client.get(f"/api/v1/runs/{run_id}/report-snapshot")
    assert snapshot.status_code == 200, snapshot.text
    snap = snapshot.json()
    counts = snap["counts"]
    # 缺失留在分母：selected 仍是 4；003 调用失败（暂态错误逐题继续，004 仍
    # 执行 → no_expectation），失败不因共同子集被过滤。
    assert counts["selected"] == 4
    assert counts["call_failed"] >= 1
    assert counts["judged"] + counts["call_failed"] + counts["no_expectation"] == 4
    assert snap["coverage"] is None or snap["coverage"] < 1.0

    # 完整覆盖 Gate：min_coverage=1.0 → insufficient_evidence（退出码 5）。
    policy = client.post("/api/v1/gate-policies", json={
        "policy_id": "m6-accept-cov-policy", "version": "1",
        "rules": [
            {"rule_id": "cov", "kind": "coverage", "min_coverage": 1.0},
            {"rule_id": "acc", "kind": "metric_threshold", "metric_id": "accuracy",
             "operator": "gte", "threshold": 0.0},
        ],
        "created_by": "m6-integration", "reason": "coverage policy",
    })
    assert policy.status_code == 201, policy.text
    passes = client.get(f"/api/v1/runs/{run_id}/scoring-passes").json()
    pass_id = passes["items"][0]["id"]
    gate = client.post("/api/v1/gates/versioned", json={
        "policy_id": "m6-accept-cov-policy", "policy_version": "1",
        "run_id": run_id, "scoring_pass_id": pass_id,
    })
    assert gate.status_code == 200, gate.text
    assert gate.json()["decision"] == "insufficient_evidence"
    assert gate.json()["exit_code"] == 5


def test_cancel_experiment_only_touches_owned_runs(slice_env):
    """A20 公共入口：取消实验 A 不影响实验 B。"""
    client, application, provider = slice_env
    _run_experiment(slice_env, experiment_id="m6-accept-expA")
    _run_experiment(slice_env, experiment_id="m6-accept-expB")
    cancelled = client.post(
        "/api/v1/experiments/m6-accept-expA/cancel", json={"reason": "m6 test"},
    )
    assert cancelled.status_code == 200, cancelled.text
    status_a = client.get("/api/v1/experiments/m6-accept-expA").json()
    status_b = client.get("/api/v1/experiments/m6-accept-expB").json()
    a_runs = [cell["run_id"] for cell in status_a["cells"] if cell.get("run_id")]
    b_runs = [cell["run_id"] for cell in status_b["cells"] if cell.get("run_id")]
    for run_id in a_runs:
        if run_id:
            view = client.get(f"/api/v1/runs/{run_id}").json()
            assert view["status"] in ("completed", "failed", "cancelled")
    for run_id in b_runs:
        if run_id:
            view = client.get(f"/api/v1/runs/{run_id}").json()
            assert view["status"] != "cancelled"
