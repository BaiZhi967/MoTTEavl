"""M2-T09：C-Eval 装配共享 Gate（M6-Lite 同一服务，无 ceval 专用引擎）。

反例与期望：
- 同集同 Profile（模型不同）→ 可比，质量 Gate 可通过。
- 漏一题 → coverage 规则不通过（10 选 9 判）。
- 不同 extractor 版本 → 质量指标不可比，比较返回具体原因。
- 读取全程 0 次 Runner/Judge 调用。
"""
from motte_sdk.comparisons import ComparisonService
from motte_storage.run_store import InMemoryRunStore
from motte_sdk.service import RunService


def _ceval_run(service, model, case_ids, scores_by_case, extractor="e1", revision="rev-1"):
    manifest = {
        "model": model,
        "execution": {"backend_id": "external-benchmark", "backend_version": "1"},
        "external_benchmark": {
            "adapter_id": "ceval-opencompass", "adapter_version": "1",
            "runner_version": "r", "dataset_revision": revision,
            "environment_digest": "d",
            "profile": {
                "benchmark_id": "ceval", "benchmark_version": "1",
                "answer_extractor": extractor, "extractor_version": "1",
                "prompt_template_version": "p1",
            },
            "limits": {},
        },
    }
    run = service.create_run("ceval-external@1", manifest, case_ids=case_ids)
    for case_id, passed in scores_by_case.items():
        service.store.case_runs.upsert({
            "run_id": run["id"], "case_id": case_id,
            "outcome": "responded", "result": {"prediction": "A", "gold": "A"},
        })
    # 固定报告（review R12）：比较/Gate 消费不可变 ScoringPass，而非现场
    # 重算 case_runs——没有 pass 的 Run 不能冒充已固定报告。
    service._append_scoring_pass(
        run["id"],
        [{"case_id": case_id, "passed": passed}
         for case_id, passed in scores_by_case.items()],
        source="test-fixation",
        final_status="completed",
    )
    return run


def _service_with_runs():
    service = RunService(InMemoryRunStore())
    cases = [f"c{index}" for index in range(1, 11)]
    base_scores = {case_id: True for case_id in cases}
    baseline = _ceval_run(service, "m1", cases, base_scores)
    candidate = _ceval_run(service, "m2", cases, base_scores)
    return service, baseline, candidate, cases


def test_ceval_uses_shared_gate():
    service, baseline, candidate, cases = _service_with_runs()
    comparisons = ComparisonService(service.store)

    # 同集同 Profile、模型为允许变量：可比。
    report = comparisons.compare(baseline["id"], candidate["id"], allowed_factors=["model"])
    assert report.eligible is True

    # Gate：全量判对 + 阈值 0.5 → 通过（同一 M6-Lite Gate，无 ceval 引擎）。
    gate = comparisons.evaluate_gate(
        candidate["id"],
        policy={
            "metric": "accuracy", "op": "gte", "threshold": 0.5,
            "required_coverage": 1.0, "require_cost_known": False,
            "require_comparable": True,
        },
        baseline_run_id=baseline["id"],
    )
    assert gate["passed"] is True
    assert gate["schema"].startswith("gate-lite@")

    # 漏一题：选择集仍是 10，只落 9 条结果 → coverage 规则不通过。
    short = _ceval_run(service, "m3", cases, {case_id: True for case_id in cases[:9]})
    gate_short = comparisons.evaluate_gate(
        short["id"],
        policy={
            "metric": "accuracy", "op": "gte", "threshold": 0.5,
            "required_coverage": 1.0, "require_cost_known": False,
            "require_comparable": True,
        },
        baseline_run_id=baseline["id"],
    )
    assert gate_short["passed"] is False
    by_id = {rule["id"]: rule for rule in gate_short["rules"]}
    assert by_id["coverage"]["passed"] is False


def test_different_extractor_is_not_comparable():
    service, baseline, _candidate, cases = _service_with_runs()
    other = _ceval_run(
        service, "m2", cases, {case_id: True for case_id in cases}, extractor="e2",
    )
    comparisons = ComparisonService(service.store)
    report = comparisons.compare(baseline["id"], other["id"], allowed_factors=["model"])
    assert report.eligible is False
    assert any("EXTRACTOR_OR_PROMPT_CHANGED" in reason for reason in report.reasons)
    gate = comparisons.evaluate_gate(
        other["id"],
        policy={
            "metric": "accuracy", "op": "gte", "threshold": 0.0,
            "required_coverage": 1.0, "require_cost_known": False,
            "require_comparable": True,
        },
        baseline_run_id=baseline["id"],
    )
    assert gate["passed"] is False
    by_id = {rule["id"]: rule for rule in gate["rules"]}
    assert by_id["comparable"]["passed"] is False


def test_reads_execute_no_runner_or_judge_calls():
    service, baseline, candidate, _cases = _service_with_runs()
    comparisons = ComparisonService(service.store)
    calls: list[str] = []
    comparisons.call_probe = lambda label: calls.append(label)
    comparisons.compare(baseline["id"], candidate["id"], allowed_factors=["model"])
    comparisons.evaluate_gate(
        candidate["id"],
        policy={"metric": "accuracy", "op": "gte", "threshold": 0.0,
                "required_coverage": 1.0, "require_cost_known": False,
                "require_comparable": False},
    )
    assert calls == []  # GET/report/compare/gate 全程只读
