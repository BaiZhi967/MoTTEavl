"""验收 F-02：case 结果为 NULL 时评分必须产出缺口行，而不是崩溃。

真实事故（M1–M5 产品级验收）：前一个 Case 触发 stop_run 之后，未尝试的 Case 在
case_runs 里写的是 result=NULL——**键存在、值为 None**。旧的
row.get("result", {}).get("observation") 里，dict.get 的默认值只在键缺失时生效，
键存在而值为 None 会原样返回，于是 None.get(...) 抛 AttributeError：整条 Run 的
评分 pass 丢失，Run 级错误也被替换成这句无关异常，操作员在 API 与 Web 上都看不到
真正的停止原因（实测原文：运行错误： 'NoneType' object has no attribute 'get'）。

这里不执行任何 Agent：只构造 manifest 快照与 case 行，直接钉住评分侧行为。
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from motte_contracts.agent_tasks import normalize_agent_tasks_dataset, scenario_for_agent_tasks
from motte_sdk.agent_tasks import agent_tasks_scores, resolve_agent_tasks_manifest

DATASET = {
    "name": "gap-tasks",
    "version": "1",
    "cases": [
        {
            "case_id": "case-1",
            "input": "Write a.txt.",
            "fixture": {},
            "expected": {"files": {"a.txt": {"mode": "exact", "expected": "a"}}},
        },
        {
            "case_id": "case-2",
            "input": "Write b.txt.",
            "fixture": {},
            "expected": {"files": {"b.txt": {"mode": "exact", "expected": "b"}}},
        },
    ],
}


def run_view():
    """只做创建期的 manifest 解析，不执行任何 Case。"""
    dataset = normalize_agent_tasks_dataset(deepcopy(DATASET))
    scenario = scenario_for_agent_tasks(dataset)
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
    return {"id": "run-1", "case_ids": ["case-1", "case-2"], "manifest": resolved}


def test_null_results_produce_gap_rows_instead_of_crashing():
    """result=None（键存在、值为 None）不得抛异常，且每题都要有可解释的缺口行。"""
    rows = [
        {"run_id": "run-1", "case_id": "case-1", "result": None, "outcome": "not_attempted"},
        {"run_id": "run-1", "case_id": "case-2", "result": None, "outcome": "not_attempted"},
    ]
    scores = agent_tasks_scores(run_view(), rows)

    assert {score["case_id"] for score in scores} == {"case-1", "case-2"}
    # 缺证据不是"不适用"，也不是通过：语义是 insufficient_evidence
    assert {score["metric_status"] for score in scores} == {"insufficient_evidence"}
    assert {score["reason"] for score in scores} == {"case_not_attempted"}
    assert all(score["passed"] is None for score in scores)
    # 缺口行不进入分母（覆盖不足不能被算成失败或通过）
    assert all(score["denominator"] is False for score in scores)


def test_missing_result_key_is_reported_as_observation_missing():
    """没有 result 键的行走 observation_missing 分支（与 not_attempted 区分）。"""
    rows = [
        {"run_id": "run-1", "case_id": "case-1"},
        {"run_id": "run-1", "case_id": "case-2", "result": None, "outcome": "not_attempted"},
    ]
    scores = agent_tasks_scores(run_view(), rows)

    by_case = {}
    for score in scores:
        by_case.setdefault(score["case_id"], set()).add(score["reason"])
    assert by_case == {"case-1": {"observation_missing"}, "case-2": {"case_not_attempted"}}
    assert all(score["passed"] is None for score in scores)


def test_one_null_case_does_not_hide_the_other_cases_metrics():
    """一题缺结果不影响其余题各自产出指标行（旧实现在这里整批失败）。"""
    rows = [
        {"run_id": "run-1", "case_id": "case-1", "result": None, "outcome": "not_attempted"},
        {"run_id": "run-1", "case_id": "case-2", "result": {}, "outcome": "responded"},
    ]
    scores = agent_tasks_scores(run_view(), rows)

    by_case = {}
    for score in scores:
        by_case.setdefault(score["case_id"], set()).add(score["reason"])
    assert by_case["case-1"] == {"case_not_attempted"}
    # 有结果但没有 observation：既不是"未尝试"，也不能当成通过
    assert by_case["case-2"] == {"observation_missing"}
