"""M5-T10：校准集、逐项分歧、实验性状态与人工修订。

关键反例：
- 合成候选样本永远不会变成 human_reviewed；没有人工标签时报告标 not_run。
- 未校准 / 配置变化的 Judge 是 experimental，不进阻断门禁；新校准的 pass 不会让
  旧的 experimental pass 获得资格（资格按所选 pass 的 spec hash 精确查找）。
- 人工修订生成完整新 ScoreSet（未变指标带来源复制），并发 CAS 不覆盖较新 current。
- 读取校准/修订历史零模型调用；人工修订不花 Judge 费用。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from motte_eval.calibration import (
    CalibrationCall,
    CalibrationError,
    CalibrationSample,
    HumanReviewRequired,
    ManualRevisionConflict,
    ManualRevisionRequest,
    ManualScoreChange,
    QualificationRegistry,
    apply_manual_revision_to_store,
    build_calibration_report,
    build_calibration_set,
    build_manual_revision,
    candidate_sample,
    manual_revision_pass,
    pass_gate_eligibility,
    qualification_record,
    qualify_judge,
    repeat_plan,
    require_human_reviewed,
    review_sample,
)
from motte_eval.judge import (
    JudgeSpec,
    build_judge_input,
    build_judge_spec,
    parse_judge_output,
)
from motte_contracts.evaluation import EvidenceRef
from motte_eval.rubrics import (
    CalibrationPolicy,
    calibration_policy_sha256,
    policy_for,
    validate_policy,
)
from motte_storage.run_store import SQLiteRunStore

RUBRIC_ID = "answer-quality"
RUBRIC_VERSION = "1"
CRITERIA = ("task_completion", "constraint_adherence", "evidence_grounding")
KINDS = ("clear_pass", "clear_fail", "borderline", "missing_evidence", "injection")
OBSERVATION = {
    "schema_version": 1,
    "observation_id": "obs-1",
    "run_id": "run-1",
    "case_id": "case-1",
    "final_output": "alpha",
    "termination": {"reason": "final_answer"},
    "coverage": {"complete": True},
    "event_refs": [{"kind": "event", "run_id": "run-1", "locator": "12"}],
    "artifact_refs": [],
    "evidence_hash": "sha256:" + "a" * 64,
}


def judge_spec(**overrides) -> JudgeSpec:
    payload = {
        "judge_profile_id": "judge-answer-quality",
        "model": "judge-model",
        "rubric_id": RUBRIC_ID,
        "rubric_version": RUBRIC_VERSION,
        "budget": {"max_calls": 4},
    }
    payload.update(overrides)
    return build_judge_spec(**payload)


def outcome_for(spec: JudgeSpec, text: str):
    bundle = build_judge_input(spec, OBSERVATION)
    return parse_judge_output(spec, text, bundle)


def criteria_json(passed: dict[str, bool], *, evidence: list[str] | None = None) -> str:
    return json.dumps({"criteria": [
        {"criterion_id": name, "passed": bool(value), "reason": "r",
         "evidence": evidence if evidence is not None else ["event:12"]}
        for name, value in passed.items()
    ]})


def human_samples(count_per_kind: int = 6) -> list[CalibrationSample]:
    samples: list[CalibrationSample] = []
    for kind in KINDS:
        for index in range(count_per_kind):
            sample = candidate_sample(
                f"{kind}-{index}", kind, f"output {kind} {index}",
                rubric_id=RUBRIC_ID, rubric_version=RUBRIC_VERSION, model="judge-model",
                labelling_notes="synthetic candidate for protocol verification",
            )
            samples.append(review_sample(
                sample, annotator="annotator-a", reviewer="reviewer-b",
                expected_criteria={name: kind == "clear_pass" for name in CRITERIA},
                reviewed_at="2026-09-21T00:00:00+00:00",
                labelling_notes="human reviewed",
            ))
    return samples


def calibration_with(samples: list[CalibrationSample], **overrides):
    calibration_id = overrides.pop("calibration_id", "cal-1")
    version = overrides.pop("version", "1")
    payload = {
        "rubric_id": RUBRIC_ID, "rubric_version": RUBRIC_VERSION,
        "model": "judge-model", "samples": samples,
    }
    payload.update(overrides)
    return build_calibration_set(calibration_id, version, **payload)


def matching_observed(calibration, spec: JudgeSpec) -> dict:
    """按人类金标作答的观测结果（用于制造 0 分歧的成功校准）。"""
    return {
        sample.sample_id: outcome_for(
            spec, criteria_json(sample.expected_criteria),
        )
        for sample in calibration.samples
    }


def measurement_calls(calibration, observed: dict) -> list[CalibrationCall]:
    """重复评分 + 一次正反位置对比：每次都是一条带费用的调用记录。"""
    calls: list[CalibrationCall] = []
    for index, sample in enumerate(calibration.samples):
        outcome = observed[sample.sample_id]
        criteria = {
            judgement.criterion_id: bool(judgement.passed)
            for judgement in outcome.judgements
            if judgement.outcome == "scored"
        }
        calls.append(CalibrationCall(
            call_id=f"single-{index}", sample_id=sample.sample_id, kind="single",
            judge_job_id=f"sjob-s{index}", invocation_id=f"inv-s{index}",
            outcome="succeeded", status=outcome.status, criteria=criteria,
            cost_usd=0.001, price_table_version="price-1",
        ))
        calls.append(CalibrationCall(
            call_id=f"repeat-{index}", sample_id=sample.sample_id, kind="repeat",
            judge_job_id=f"sjob-r{index}", invocation_id=f"inv-r{index}",
            outcome="succeeded", status=outcome.status, criteria=dict(criteria),
            cost_usd=0.001, price_table_version="price-1",
        ))
    calls.append(CalibrationCall(
        call_id="swap-fwd", sample_id="pair-sample", kind="order_forward",
        judge_job_id="sjob-fwd", invocation_id="inv-fwd", outcome="succeeded",
        status="ok", presentation_order=["cand-a", "cand-b"],
        winner_candidate_id="cand-a", cost_usd=0.002,
        price_table_version="price-1",
    ))
    calls.append(CalibrationCall(
        call_id="swap-rev", sample_id="pair-sample", kind="order_reverse",
        judge_job_id="sjob-rev", invocation_id="inv-rev", outcome="succeeded",
        status="ok", presentation_order=["cand-b", "cand-a"],
        winner_candidate_id="cand-a", cost_usd=0.002,
        price_table_version="price-1",
    ))
    return calls


# ------------------------------------------------------------------ 人审诚实性

def test_synthetic_candidates_never_count_as_human_reviewed():
    sample = candidate_sample(
        "cand-1", "clear_pass", "looks fine",
        rubric_id=RUBRIC_ID, rubric_version=RUBRIC_VERSION, model="judge-model",
    )
    assert sample.status == "candidate"
    assert sample.source == "synthetic_candidate"
    assert sample.is_human_reviewed is False

    tampered = sample.model_dump(mode="json")
    tampered.update({
        "status": "human_reviewed", "annotator": "someone", "reviewed_by": "someone",
        "reviewed_at": "now", "expected_criteria": {"task_completion": True},
    })
    with pytest.raises(ValueError, match="synthetic candidates can never"):
        CalibrationSample.model_validate(tampered)

    calibration = calibration_with([sample])
    covered, reasons = calibration.covers()
    assert covered is False
    assert any("human-reviewed samples 0" in reason for reason in reasons)
    with pytest.raises(HumanReviewRequired):
        require_human_reviewed(calibration)

    report = build_calibration_report(calibration, observed={})
    assert report.human_reviewed_count == 0
    assert report.candidate_only_count == 1
    assert report.qualified is False and report.experimental is True
    assert report.gate_eligible is False
    assert any("no human labels" in reason for reason in report.not_run)


def test_review_sample_requires_a_named_human_act():
    sample = candidate_sample(
        "cand-2", "clear_fail", "wrong",
        rubric_id=RUBRIC_ID, rubric_version=RUBRIC_VERSION, model="judge-model",
    )
    with pytest.raises(HumanReviewRequired, match="named annotator"):
        review_sample(sample, annotator="", reviewer="b",
                      expected_criteria={"task_completion": False}, reviewed_at="now")
    with pytest.raises(HumanReviewRequired, match="labelled criterion"):
        review_sample(sample, annotator="a", reviewer="b",
                      expected_criteria={}, reviewed_at="now")
    reviewed = review_sample(
        sample, annotator="a", reviewer="b",
        expected_criteria={"task_completion": False}, reviewed_at="now",
    )
    assert reviewed.is_human_reviewed
    assert reviewed.source == "human"
    assert reviewed.content_sha256 != sample.content_sha256


def test_first_batch_requires_thirty_reviewed_samples_over_five_kinds():
    ok = calibration_with(human_samples(6))
    covered, reasons = ok.covers()
    assert covered is True, reasons
    assert len(ok.human_reviewed()) == 30
    assert ok.kind_counts() == {kind: 6 for kind in KINDS}
    require_human_reviewed(ok)

    short = calibration_with(human_samples(5))
    covered, reasons = short.covers()
    assert covered is False
    assert any("human-reviewed samples 25 < required 30" in reason for reason in reasons)
    assert any("clear_pass samples 5 < required 6" in reason for reason in reasons)


# ------------------------------------------------------------------ 报告

def test_report_outputs_per_criterion_confusion_and_error_rates():
    calibration = calibration_with(human_samples(6))
    spec = judge_spec()
    observed = {}
    for index, sample in enumerate(calibration.samples):
        position = index % 6
        if position == 5:
            observed[sample.sample_id] = outcome_for(spec, '{"refused": true}')
        elif position == 4:
            # 通过但完全没有证据：必须是 insufficient_evidence，不是 pass。
            observed[sample.sample_id] = outcome_for(
                spec, criteria_json({name: True for name in CRITERIA}, evidence=[]),
            )
        elif position == 3:
            # 与金标相反：全部判通过。非 clear_pass 的样本因此产生 false_pass。
            observed[sample.sample_id] = outcome_for(
                spec, criteria_json({name: True for name in CRITERIA}),
            )
        else:
            observed[sample.sample_id] = outcome_for(
                spec, criteria_json(sample.expected_criteria),
            )
    calls = [
        CalibrationCall(
            call_id=f"c-{index}", sample_id=sample.sample_id, kind="single",
            judge_job_id=f"sjob-{index}", invocation_id=f"inv-{index}",
            outcome="succeeded", status=getattr(observed[sample.sample_id], "status", None),
            criteria=sample.expected_criteria, cost_usd=0.001,
            price_table_version="price-1",
        )
        for index, sample in enumerate(calibration.samples)
    ]
    report = build_calibration_report(
        calibration, observed=observed, calls=calls,
        generated_at="2026-09-21T00:00:00+00:00",
    )

    assert report.sample_count == 30 and report.human_reviewed_count == 30
    assert report.call_count == 30
    assert report.cost == {
        "known": True, "total_usd": pytest.approx(0.03), "priced_calls": 30,
        "price_table_versions": ["price-1"],
    }
    completion = next(
        item for item in report.per_criterion if item.criterion_id == "task_completion"
    )
    # 30 条样本：5 拒答 + 5 缺证据 + 20 可比较（其中 4 条与金标相反）。
    assert completion.refusals == 5
    assert completion.missing_evidence == 5
    assert completion.compared == 20
    assert completion.true_pass == 4
    assert completion.true_fail == 12
    assert completion.false_pass == 4
    assert completion.false_fail == 0
    assert completion.disagreements == 4
    assert completion.agreement_rate == pytest.approx(0.8)
    assert report.disagreement_rate == pytest.approx(0.2)
    # 比率的分母是逐 criterion 判断数（30 样本 x 3 criterion = 90）。
    assert report.error_rate == pytest.approx(0.0)
    assert report.refusal_rate == pytest.approx(15 / 90)
    assert report.missing_evidence_rate == pytest.approx(15 / 90)
    # 分歧超过固定阈值 → 不能进入阻断门禁。
    assert report.qualified is False and report.gate_eligible is False
    assert any("disagreement rate" in reason for reason in report.reasons)
    assert report.not_run == []


def test_policy_thresholds_are_fixed_and_not_relaxable():
    registered = policy_for(RUBRIC_ID, RUBRIC_VERSION)
    assert registered.max_disagreement_rate == 0.15
    with pytest.raises(Exception, match="fixed"):
        validate_policy(CalibrationPolicy(
            rubric_id=RUBRIC_ID, rubric_version=RUBRIC_VERSION,
            max_disagreement_rate=0.99,
        ))


def test_repeat_stability_and_position_swap_are_measured_from_calls():
    calls = [
        CalibrationCall(
            call_id="a1", sample_id="s1", kind="repeat", judge_job_id="j1",
            invocation_id="i1", outcome="succeeded", status="ok",
            criteria={"task_completion": True}, cost_usd=0.001,
        ),
        CalibrationCall(
            call_id="a2", sample_id="s1", kind="repeat", judge_job_id="j2",
            invocation_id="i2", outcome="succeeded", status="ok",
            criteria={"task_completion": True}, cost_usd=0.001,
        ),
        CalibrationCall(
            call_id="b1", sample_id="s2", kind="repeat", judge_job_id="j3",
            invocation_id="i3", outcome="succeeded", status="ok",
            criteria={"task_completion": True}, cost_usd=0.001,
        ),
        CalibrationCall(
            call_id="b2", sample_id="s2", kind="repeat", judge_job_id="j4",
            invocation_id="i4", outcome="succeeded", status="missing_evidence",
            criteria={}, cost_usd=0.001,
        ),
        CalibrationCall(
            call_id="f1", sample_id="p1", kind="order_forward", judge_job_id="j5",
            invocation_id="i5", outcome="succeeded", status="ok",
            presentation_order=["cand-a", "cand-b"], winner_candidate_id="cand-a",
            cost_usd=0.002,
        ),
        CalibrationCall(
            call_id="r1", sample_id="p1", kind="order_reverse", judge_job_id="j6",
            invocation_id="i6", outcome="succeeded", status="ok",
            presentation_order=["cand-b", "cand-a"], winner_candidate_id="cand-b",
            cost_usd=0.002,
        ),
    ]
    calibration = calibration_with(human_samples(1))
    report = build_calibration_report(calibration, observed={}, calls=calls)
    assert report.repeat_stability["measured"] is True
    assert report.repeat_stability["samples"] == 2
    assert report.repeat_stability["stable"] == 1
    assert report.repeat_stability["rate"] == pytest.approx(0.5)
    assert report.repeat_stability["unstable_samples"] == ["s2"]
    assert report.position_swap["measured"] is True
    assert report.position_swap["pairs"] == 1
    assert report.position_swap["consistent"] == 0
    assert report.position_swap["rate"] == 0.0
    assert report.call_count == 6
    assert report.cost["known"] is True


def test_repeat_plan_counts_every_repeat_and_order():
    plan = repeat_plan(["s1", "s2"], repeats=3, swap=False)
    # 第一次是普通评分，其余两次是显式重复；每次都会产生一次计费调用。
    assert [item["kind"] for item in plan] == ["single", "repeat", "repeat"] * 2
    swap_plan = repeat_plan(["s1"], repeats=1, swap=True)
    assert sorted(item["kind"] for item in swap_plan) == [
        "order_forward", "order_reverse", "single",
    ]
    with pytest.raises(CalibrationError, match="repeats"):
        repeat_plan(["s1"], repeats=0)


# ------------------------------------------------------------------ 资格与门禁

def test_qualified_calibration_requires_measured_repeats_and_swap():
    spec = judge_spec(calibration_version="1")
    calibration = calibration_with(
        human_samples(6), judge_spec_sha256=spec.spec_sha256,
    )
    observed = matching_observed(calibration, spec)
    report = build_calibration_report(
        calibration, observed=observed, calls=measurement_calls(calibration, observed),
    )
    assert report.disagreement_rate == pytest.approx(0.0)
    assert report.repeat_stability["rate"] == pytest.approx(1.0)
    assert report.position_swap["rate"] == pytest.approx(1.0)
    assert report.qualified is True
    assert report.experimental is False and report.gate_eligible is True
    assert report.reasons == []


def test_gate_eligibility_is_per_pass_and_never_inherited():
    experimental_spec = judge_spec()
    calibration = calibration_with(
        human_samples(6), judge_spec_sha256=experimental_spec.spec_sha256,
    )
    observed = matching_observed(calibration, experimental_spec)
    report = build_calibration_report(
        calibration, observed=observed, calls=measurement_calls(calibration, observed),
    )
    assert report.qualified is True

    registry = QualificationRegistry()
    older_pass = {
        "source": "judge",
        "judge": {
            "spec_sha256": experimental_spec.spec_sha256,
            "rubric_id": RUBRIC_ID, "rubric_version": RUBRIC_VERSION,
            "model": "judge-model", "calibration_version": None,
        },
    }
    before = pass_gate_eligibility(older_pass, registry)
    assert before["gate_eligible"] is False
    assert before["experimental"] is True
    assert "no calibration qualification" in before["reason"]

    registry.register(qualification_record(report))
    # 没有 calibration_version 的旧 pass 仍然不合格：资格不按模型/rubric 名字继承。
    assert registry.lookup(
        judge_spec_sha256=experimental_spec.spec_sha256, rubric_id=RUBRIC_ID,
        rubric_version=RUBRIC_VERSION, model="judge-model", calibration_version=None,
    ) is None
    assert pass_gate_eligibility(older_pass, registry)["gate_eligible"] is False

    calibrated_spec = judge_spec(calibration_version="1")
    calibrated_calibration = calibration_with(
        human_samples(6), judge_spec_sha256=calibrated_spec.spec_sha256,
        calibration_id="cal-2", version="2",
    )
    observed2 = matching_observed(calibrated_calibration, calibrated_spec)
    report2 = build_calibration_report(
        calibrated_calibration, observed=observed2,
        calls=measurement_calls(calibrated_calibration, observed2),
    )
    report2 = report2.model_copy(update={
        "judge_spec_sha256": calibrated_spec.spec_sha256,
        "calibration_id": calibrated_calibration.calibration_id,
        "calibration_version": calibrated_calibration.version,
    })
    registry.register(qualification_record(report2))
    fresh_pass = {
        "source": "judge",
        "judge": {
            "spec_sha256": calibrated_spec.spec_sha256,
            "rubric_id": RUBRIC_ID, "rubric_version": RUBRIC_VERSION,
            "model": "judge-model", "calibration_version": "2",
        },
    }
    assert pass_gate_eligibility(fresh_pass, registry)["gate_eligible"] is True
    # 新校准的 pass 不能让旧的 experimental pass 获得门禁资格。
    assert pass_gate_eligibility(older_pass, registry)["gate_eligible"] is False

    # 非 judge pass 不参与校准资格判定。
    assert pass_gate_eligibility({"source": "initial"}, registry)["reason"].startswith(
        "pass is not judge-scored"
    )
    # rubric 版本变化后旧资格不匹配。
    other = {"source": "judge", "judge": {**fresh_pass["judge"], "rubric_version": "2"}}
    assert pass_gate_eligibility(other, registry)["gate_eligible"] is False


def test_qualify_judge_flags_missing_measurements():
    calibration = calibration_with(human_samples(6))
    report = build_calibration_report(calibration, observed={})
    assert report.qualified is False
    assert any("does not pin a judge spec hash" in reason for reason in report.reasons)
    assert any("no human-labelled criteria could be compared" in reason
               for reason in report.reasons)
    assert any("repeated scoring stability was not measured" in reason
               for reason in report.reasons)
    assert any("pairwise position-swap consistency was not measured" in reason
               for reason in report.reasons)
    assert report.not_run == []  # 这批样本确有人工标签，缺的只是测量项
    again = qualify_judge(report)
    assert again.qualified is False and again.experimental is True
    assert again.reasons == report.reasons


# ------------------------------------------------------------------ 人工修订

def make_run_and_pass(store, run_id: str = "run-1") -> dict:
    store.runs.create({
        "id": run_id, "schema_version": 2, "revision": 1,
        "scenario_version": "replay@1", "status": "completed",
        "manifest": {}, "requested_manifest": {}, "case_ids": ["case-1"],
        "created_at": "2026-09-21T00:00:00+00:00",
        "updated_at": "2026-09-21T00:00:00+00:00",
    }, event={"run_id": run_id, "type": "queued", "status": "completed"})
    scores = [
        {
            "case_id": "case-1", "metric_id": metric_id,
            "evaluator_id": "agent-deterministic", "evaluator_version": "1",
            "metric_status": "scored", "value": 1.0 if passed else 0.0,
            "passed": passed, "denominator": True, "details": {"original": True},
        }
        for metric_id, passed in (
            ("task_completion", True), ("constraint_adherence", True),
            ("evidence_grounding", False),
        )
    ]
    record = {
        "id": "pass-source", "run_id": run_id, "scorer_id": "agent-deterministic",
        "scorer_version": "1", "created_at": "2026-09-21T00:00:00+00:00",
        "source": "initial", "source_run_revision": 1,
        "previous_pass_id": None, "summary": {"scores": 3, "passed": 2},
        "scores": scores,
    }
    run = store.runs.get(run_id)
    return store.scoring_passes.append(
        record, scores, expected_run_revision=run["revision"],
        expected_run_status="completed",
    )


def test_manual_revision_builds_a_complete_new_scoreset_with_provenance(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    source = make_run_and_pass(store)
    request = ManualRevisionRequest(
        run_id="run-1", source_pass_id="pass-source", actor="operator",
        reason="evidence shows the answer did address the constraint",
        changes=[ManualScoreChange(
            case_id="case-1", metric_id="constraint_adherence", passed=False,
            reason="contradicted by artifact report.json",
        )],
        expected_current_pass_id="pass-source",
    )
    built = build_manual_revision(
        source, request, revision_id="pass-rev-1",
        created_at="2026-09-21T01:00:00+00:00", current_pass_id="pass-source",
    )
    assert len(built["scores"]) == 3
    assert built["pass"]["source"] == "manual_revision"
    assert built["pass"]["previous_pass_id"] == "pass-source"
    assert built["pass"]["scorer_id"] == "manual-revision"
    assert built["pass"]["scorer_version"] == "1+manual"
    changed = next(
        row for row in built["scores"] if row["metric_id"] == "constraint_adherence"
    )
    assert changed["passed"] is False
    assert changed["metric_status"] == "scored"
    assert changed["details"]["manual_revision"]["changed"] is True
    copied = next(
        row for row in built["scores"] if row["metric_id"] == "task_completion"
    )
    assert copied["passed"] is True and copied["value"] == 1.0
    assert copied["details"]["manual_revision"]["copied"] is True
    assert copied["details"]["manual_revision"]["source_pass_id"] == "pass-source"
    assert copied["details"]["original"] is True
    assert built["summary"]["changed_metrics"] == 1
    assert built["summary"]["copied_metrics"] == 2
    assert len(built["diffs"]) == 1
    assert built["diffs"][0]["before"]["passed"] is True
    assert built["diffs"][0]["after"]["passed"] is False

    with pytest.raises(CalibrationError, match="unknown metrics"):
        build_manual_revision(
            source,
            ManualRevisionRequest(
                run_id="run-1", source_pass_id="pass-source", actor="a", reason="r",
                changes=[ManualScoreChange(case_id="case-1", metric_id="nope",
                                           passed=True)],
                expected_current_pass_id="pass-source",
            ),
            revision_id="pass-rev-x", created_at="now", current_pass_id="pass-source",
        )


def test_manual_revision_cas_and_concurrency_never_overwrite_newer_current(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    make_run_and_pass(store)
    first = apply_manual_revision_to_store(
        store,
        ManualRevisionRequest(
            run_id="run-1", source_pass_id="pass-source", actor="operator",
            reason="fix one metric",
            changes=[ManualScoreChange(case_id="case-1",
                                       metric_id="evidence_grounding", passed=True,
                                       reason="evidence present")],
            expected_current_pass_id="pass-source",
        ),
        revision_id="pass-rev-1", created_at="2026-09-21T01:00:00+00:00",
    )
    assert first["pass"]["id"] == "pass-rev-1"
    assert store.runs.get("run-1")["current_scoring_pass_id"] == "pass-rev-1"
    assert manual_revision_pass(store.scoring_passes.get("pass-rev-1"))["diffs"][0][
        "metric_id"
    ] == "evidence_grounding"
    assert manual_revision_pass(store.scoring_passes.get("pass-source")) is None

    # 重复使用同一个 expected current 必须冲突，不能覆盖较新的 current。
    with pytest.raises(ManualRevisionConflict):
        apply_manual_revision_to_store(
            store,
            ManualRevisionRequest(
                run_id="run-1", source_pass_id="pass-source", actor="operator",
                reason="stale retry",
                changes=[ManualScoreChange(case_id="case-1", metric_id="task_completion",
                                           passed=False)],
                expected_current_pass_id="pass-source",
            ),
            revision_id="pass-rev-stale", created_at="2026-09-21T02:00:00+00:00",
        )
    assert store.runs.get("run-1")["current_scoring_pass_id"] == "pass-rev-1"

    # 并发两个修订：显式 CAS（current pass 检查 + Run revision CAS）只有一个赢。
    def revise(revision_id: str) -> str:
        try:
            apply_manual_revision_to_store(
                store,
                ManualRevisionRequest(
                    run_id="run-1", source_pass_id="pass-rev-1", actor="operator",
                    reason="concurrent edit",
                    changes=[ManualScoreChange(case_id="case-1",
                                               metric_id="task_completion",
                                               passed=False)],
                    expected_current_pass_id="pass-rev-1",
                ),
                revision_id=revision_id, created_at="2026-09-21T03:00:00+00:00",
            )
            return "won"
        except (ManualRevisionConflict, Exception) as error:  # noqa: BLE001
            return f"conflict:{type(error).__name__}" if not isinstance(
                error, ManualRevisionConflict
            ) else "conflict:ManualRevisionConflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(revise, ["pass-rev-a", "pass-rev-b"]))
    assert sorted(outcome.split(":")[0] for outcome in outcomes) == ["conflict", "won"]
    passes = store.scoring_passes.list_for_run("run-1")
    # source + 第一次修订 + 并发修订的胜者：没有记录丢失，current 指向胜者。
    assert len(passes) == 3
    assert passes[-1]["source"] == "manual_revision"
    assert len({row["id"] for row in passes}) == 3
    assert store.runs.get("run-1")["current_scoring_pass_id"] == passes[-1]["id"]


def test_calibration_and_revision_reads_cost_no_judge_money(tmp_path):
    from motte_sdk.scoring_jobs import ScoringJobService

    class ExplodingProvider:
        """任何一次模型调用都会让只读路径立刻失败。"""

        def complete(self, request):  # noqa: ANN001
            raise AssertionError("a judge call was issued from a read-only path")

    store = SQLiteRunStore(tmp_path / "runs.db")
    source = make_run_and_pass(store)
    service = ScoringJobService(
        store, provider_factory=lambda model: ExplodingProvider(),
    )

    calibration = calibration_with(human_samples(6))
    report = build_calibration_report(calibration, observed={})
    assert report.call_count == 0
    manual_revision_pass(source)
    service.history("run-1")
    service.list_for_run("run-1")
    assert manual_revision_pass(store.scoring_passes.get("pass-source")) is None

    applied = apply_manual_revision_to_store(
        store,
        ManualRevisionRequest(
            run_id="run-1", source_pass_id="pass-source", actor="operator",
            reason="human fix", changes=[ManualScoreChange(
                case_id="case-1", metric_id="evidence_grounding", passed=False,
                reason="no evidence",
            )],
            expected_current_pass_id="pass-source",
        ),
        revision_id="pass-rev-manual", created_at="2026-09-21T01:00:00+00:00",
    )
    assert applied["pass"]["source"] == "manual_revision"
    assert applied["pass"]["summary"]["manual_revision"]["changed_metrics"] == 1
    # 人工修订没有 judge 身份：它不会带来任何 Judge 费用，也不宣称做过验证。
    assert applied["pass"]["scorer_id"] == "manual-revision"
    assert "judge" not in applied["pass"]


# ---------------------------------------------------------------- R3：CAS 与资格

class _ReadHook:
    """确定性交错：在第 N 次「CAS 依据读取」之后、返回之前插入竞争请求。"""

    def __init__(self, trigger, at: int = 2) -> None:
        self._trigger = trigger
        self._at = at
        self.reads = 0
        self.fired = False

    def note(self) -> None:
        self.reads += 1
        if self.reads == self._at and not self.fired:
            self.fired = True
            self._trigger()


class _CasRepo:
    """代理一个仓储；只对构成 CAS 依据的读取计数（runs.get / passes.current）。"""

    def __init__(self, inner, hook: _ReadHook, counted: set[str]) -> None:
        self._inner = inner
        self._hook = hook
        self._counted = counted

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if name in self._counted:
            def counted(*args, **kwargs):
                self._hook.note()
                return attribute(*args, **kwargs)
            return counted
        return attribute


def interleaved_store(store, trigger):
    """把一次竞争修订插进 CAS 窗口的正中间。"""
    hook = _ReadHook(trigger)

    class Wrapper:
        runs = _CasRepo(store.runs, hook, {"get"})
        scoring_passes = _CasRepo(store.scoring_passes, hook, {"current"})

    return Wrapper()


def revision_request(actor: str, *, source: str = "pass-source") -> ManualRevisionRequest:
    return ManualRevisionRequest(
        run_id="run-1", source_pass_id=source, actor=actor,
        reason=f"{actor} fix",
        changes=[ManualScoreChange(
            case_id="case-1", metric_id="task_completion", passed=False,
            reason="contradicted by evidence",
        )],
        expected_current_pass_id=source,
    )


def test_competing_revisions_share_one_cas_window_and_only_one_wins(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    make_run_and_pass(store)

    def competing() -> None:
        apply_manual_revision_to_store(
            store, revision_request("operator-b"),
            revision_id="pass-rev-b", created_at="2026-09-21T02:00:00+00:00",
        )

    hooked = interleaved_store(store, competing)
    # 请求 A 已经读到 current=pass-source；B 在它写入之前完整跑完。
    with pytest.raises(ManualRevisionConflict):
        apply_manual_revision_to_store(
            hooked, revision_request("operator-a"),
            revision_id="pass-rev-a", created_at="2026-09-21T03:00:00+00:00",
        )

    # 只有一个胜者：B 的 current 不被 A 覆盖。
    assert store.runs.get("run-1")["current_scoring_pass_id"] == "pass-rev-b"
    assert [item["id"] for item in store.scoring_passes.list_for_run("run-1")] == [
        "pass-source", "pass-rev-b",
    ]
    # 失败请求没有半个 ScoreSet、孤立 pass 或伪成功事件。
    assert store.scoring_passes.get("pass-rev-a") is None
    assert store.score_sets.list_for_pass("pass-rev-a") == []
    events = [
        event for event in store.events.list_for_run("run-1")
        if event.get("scoring_pass_id") == "pass-rev-a"
    ]
    assert events == []
    # 原 subject 证据不变。
    source = store.scoring_passes.get("pass-source")
    assert source["source"] == "initial"
    assert [row["passed"] for row in source["scores"]] == [True, True, False]


def test_manual_revision_keeps_the_source_pass_and_baseline_untouched(tmp_path):
    store = SQLiteRunStore(tmp_path / "runs.db")
    make_run_and_pass(store)
    before = store.scoring_passes.get("pass-source")
    rows_before = store.score_sets.list_for_pass("pass-source")

    applied = apply_manual_revision_to_store(
        store,
        ManualRevisionRequest(
            run_id="run-1", source_pass_id="pass-source", actor="operator",
            reason="human review found the evidence",
            evidence=[EvidenceRef(kind="artifact", run_id="run-1", locator="report.json")],
            changes=[ManualScoreChange(
                case_id="case-1", metric_id="evidence_grounding", passed=True,
                reason="artifact report.json contains the call",
            )],
            expected_current_pass_id="pass-source",
        ),
        revision_id="pass-rev-manual", created_at="2026-09-21T01:00:00+00:00",
    )
    summary = applied["pass"]["manual_revision"]
    assert summary["actor"] == "operator"
    assert summary["reason"] == "human review found the evidence"
    assert summary["source_pass_id"] == "pass-source"
    assert summary["evidence"][0]["locator"] == "report.json"
    assert applied["pass"]["previous_pass_id"] == "pass-source"
    assert applied["pass"]["source"] == "manual_revision"
    # 旧 pass 内容逐字节不变，baseline 不追随 current。
    assert store.scoring_passes.get("pass-source") == before
    assert store.score_sets.list_for_pass("pass-source") == rows_before
    assert store.runs.get("run-1")["current_scoring_pass_id"] == "pass-rev-manual"


def coverage_calibration(spec: JudgeSpec, *, kinds: dict[str, int]):
    samples: list[CalibrationSample] = []
    for kind, count in kinds.items():
        for index in range(count):
            sample = candidate_sample(
                f"{kind}-{index}", kind, f"output {kind} {index}",
                rubric_id=RUBRIC_ID, rubric_version=RUBRIC_VERSION, model="judge-model",
                labelling_notes="synthetic candidate for protocol verification",
            )
            samples.append(review_sample(
                sample, annotator="annotator-a", reviewer="reviewer-b",
                expected_criteria={name: kind == "clear_pass" for name in CRITERIA},
                reviewed_at="2026-09-21T00:00:00+00:00",
                labelling_notes="human reviewed",
            ))
    return calibration_with(samples, judge_spec_sha256=spec.spec_sha256)


def test_qualification_consumes_the_full_coverage_result():
    spec = judge_spec(calibration_version="1")
    calibration = coverage_calibration(spec, kinds={"clear_pass": 30})
    observed = matching_observed(calibration, spec)
    report = build_calibration_report(
        calibration, observed=observed, calls=measurement_calls(calibration, observed),
    )

    # 30 条 clear_pass、其余四类为 0：样本总数够，覆盖不足，绝不 gate eligible。
    assert report.human_reviewed_count == 30
    assert report.kind_counts["clear_fail"] == 0
    assert report.coverage["covered"] is False
    assert report.qualified is False
    assert report.experimental is True
    assert report.gate_eligible is False
    assert any("clear_fail" in reason for reason in report.reasons)
    assert any("missing_evidence" in reason for reason in report.reasons)
    assert report.not_run == []  # 人工标签存在，缺的是覆盖与质量


def test_repeat_stability_below_the_policy_is_not_qualified():
    spec = judge_spec(calibration_version="1")
    calibration = calibration_with(human_samples(6), judge_spec_sha256=spec.spec_sha256)
    observed = matching_observed(calibration, spec)
    calls = measurement_calls(calibration, observed)
    # 一个样本的重复评分与第一次不同：稳定率 29/30 < policy 要求。
    calls[1] = calls[1].model_copy(update={"criteria": {"task_completion": False}})
    report = build_calibration_report(calibration, observed=observed, calls=calls)

    assert report.repeat_stability["rate"] == pytest.approx(29 / 30)
    assert report.qualified is False
    assert report.experimental is True
    assert report.gate_eligible is False
    reason = next(
        item for item in report.reasons if "repeat stability" in item
    )
    assert "29/30" in reason or "0.966" in reason


def test_policy_thresholds_enter_the_report_and_qualification_hash():
    spec = judge_spec(calibration_version="1")
    calibration = calibration_with(human_samples(6), judge_spec_sha256=spec.spec_sha256)
    observed = matching_observed(calibration, spec)
    report = build_calibration_report(
        calibration, observed=observed, calls=measurement_calls(calibration, observed),
    )
    assert report.qualified is True, report.reasons
    assert report.policy_sha256 == calibration_policy_sha256(report.policy)
    assert report.policy.min_repeat_stability_rate == 1.0

    record = qualification_record(report)
    assert record.policy_sha256 == report.policy_sha256
    registry = QualificationRegistry()
    registry.register(record)
    assert registry.lookup(
        judge_spec_sha256=spec.spec_sha256, rubric_id=RUBRIC_ID,
        rubric_version=RUBRIC_VERSION, model="judge-model", calibration_version="1",
        policy_sha256=report.policy_sha256,
    ) is not None
    # 阈值变了就是另一份资格：绝不继承旧政策下的资格。
    loosened = CalibrationPolicy(
        rubric_id=RUBRIC_ID, rubric_version=RUBRIC_VERSION,
        min_repeat_stability_rate=0.5,
    )
    assert calibration_policy_sha256(loosened) != report.policy_sha256
    assert registry.lookup(
        judge_spec_sha256=spec.spec_sha256, rubric_id=RUBRIC_ID,
        rubric_version=RUBRIC_VERSION, model="judge-model", calibration_version="1",
        policy_sha256=calibration_policy_sha256(loosened),
    ) is None
    # 新 model / rubric / spec hash 同样不匹配。
    assert registry.lookup(
        judge_spec_sha256=spec.spec_sha256, rubric_id=RUBRIC_ID,
        rubric_version=RUBRIC_VERSION, model="other-model", calibration_version="1",
    ) is None
    assert registry.lookup(
        judge_spec_sha256="sha256:" + "0" * 64, rubric_id=RUBRIC_ID,
        rubric_version=RUBRIC_VERSION, model="judge-model", calibration_version="1",
    ) is None


def test_qualification_is_read_through_the_real_published_pass(tmp_path):
    """资格必须能按真实发布的 pass 读取，而不是测试专用字典。"""
    from motte_sdk.scoring_jobs import ScoringJobRequest, ScoringJobService

    class ScriptedProvider:
        def __init__(self, content: str) -> None:
            self.content = content
            self.calls: list = []

        def complete(self, request):  # noqa: ANN001 - 协议方法
            self.calls.append(request)
            return {
                "provider": "scripted", "model": request.model, "content": self.content,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "cost": None, "response_id": "resp-1", "metering": {"attempts": 1},
            }

    spec = judge_spec(calibration_version="1")
    calibration = calibration_with(human_samples(6), judge_spec_sha256=spec.spec_sha256)
    observed = matching_observed(calibration, spec)
    report = build_calibration_report(
        calibration, observed=observed, calls=measurement_calls(calibration, observed),
    )
    assert report.qualified is True, report.reasons

    registry = QualificationRegistry()
    registry.register(qualification_record(report))

    store = SQLiteRunStore(tmp_path / "runs.db")
    make_run_and_pass(store)
    provider = ScriptedProvider(criteria_json({name: True for name in CRITERIA}))
    service = ScoringJobService(store, provider_factory=lambda model: provider)
    submitted = service.submit(ScoringJobRequest.model_validate({
        "request_key": "qual-1",
        "judge_spec": spec,
        "mode": "single",
        "run_id": "run-1",
        "source_pass_id": "pass-source",
        "observations": {"case-1": OBSERVATION},
        "authorisation": {
            "authorised": True, "actor": "operator", "max_calls": 4,
        },
    }))
    result = service.claim_and_run()
    assert result["status"] == "completed"
    published = store.scoring_passes.get(result["receipt"]["scoring_pass_id"])
    assert published["source"] == "judge"
    # pass.judge 必须带可被资格读取的 rubric 身份，而不是只有拼接字符串。
    assert published["judge"]["rubric_id"] == RUBRIC_ID
    assert published["judge"]["rubric_version"] == RUBRIC_VERSION
    assert published["judge"]["rubric"] == f"{RUBRIC_ID}@{RUBRIC_VERSION}"
    assert published["judge"]["spec_sha256"] == spec.spec_sha256

    eligible = pass_gate_eligibility(published, registry)
    assert eligible["gate_eligible"] is True
    assert eligible["experimental"] is False
    assert eligible["qualification_id"] == qualification_record(report).qualification_id
    assert len(provider.calls) == 1

    # 换模型 / 换 spec hash 的 pass 不会继承这份资格。
    other = registry.eligible(
        judge_spec_sha256="sha256:" + "0" * 64, rubric_id=RUBRIC_ID,
        rubric_version=RUBRIC_VERSION, model="judge-model", calibration_version="1",
    )
    assert other["gate_eligible"] is False and other["experimental"] is True
    assert submitted["request_key"] == "qual-1"


