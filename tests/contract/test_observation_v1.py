"""M1-T01：Observation/MetricResult 契约演进与旧读兼容。

验收反例：
- 旧 name/value Observation 往返不变；新 FrozenObservation 显式版本化。
- 同一 Case 两个 metric 合法；完全重复的 metric key 拒绝。
- NaN/Infinity、外部 evidence ref 拒绝；evidence_hash 绑定冻结视图。
- 旧 Score（无 metric 字段）继续可读；legacy 与 multi-metric 分数不得混用。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from motte_contracts.evidence import Observation, Score, ScoreSet, ScoringPass
from motte_contracts.evaluation import (
    AgentResult,
    ArtifactEntry,
    EvidenceCoverage,
    EvidenceRef,
    EvaluatorSpec,
    FrozenObservation,
    InvocationKind,
    InvocationRecord,
    MetricResult,
    MetricStatus,
    TerminationRecord,
    observation_evidence_hash,
)


def _observation_payload(**overrides):
    payload = {
        "observation_id": "obs-1",
        "run_id": "run-1",
        "case_id": "case-1",
        "attempt_id": "attempt-1",
        "final_output": "done",
        "termination": {"reason": "final_answer"},
        "event_refs": [{"kind": "event", "run_id": "run-1", "locator": "3"}],
        "artifact_refs": [{
            "artifact_id": "run-1/case-1/report.json",
            "path": "report.json",
            "media_type": "application/json",
            "size_bytes": 21,
            "sha256": "a" * 64,
            "available": True,
        }],
        "coverage": {
            "complete": True, "events_captured": 3, "artifacts_captured": 1,
            "artifacts_expected": 1, "missing": [],
        },
        "usage": None,
    }
    payload.update(overrides)
    return payload


def _frozen_observation(**overrides) -> FrozenObservation:
    payload = _observation_payload(**overrides)
    payload["evidence_hash"] = observation_evidence_hash(payload)
    return FrozenObservation.model_validate(payload)


def _metric(metric_id: str, **overrides) -> MetricResult:
    fields = {
        "metric_id": metric_id,
        "status": MetricStatus.scored,
        "value": 1.0,
        "passed": True,
        "evaluator_id": "agent-deterministic",
        "evaluator_version": "1",
    }
    fields.update(overrides)
    return MetricResult.model_validate(fields)


def _metric_score(metric_id: str, **overrides) -> Score:
    fields = {
        "case_id": "case-1",
        "passed": True,
        "metric_id": metric_id,
        "evaluator_id": "agent-deterministic",
        "evaluator_version": "1",
        "metric_status": "scored",
    }
    fields.update(overrides)
    return Score.model_validate(fields)


def test_legacy_and_multimetric_roundtrip():
    # 旧 Observation：name/value/source 读协议保持不变
    legacy = Observation(name="answer", value="42", source="model")
    assert Observation.model_validate_json(legacy.model_dump_json()) == legacy
    assert Observation.model_validate({"name": "x", "value": None}) == Observation(name="x", value=None)

    # 新 FrozenObservation：显式 schema_version + 冻结 hash
    frozen = _frozen_observation()
    assert frozen.schema_version == 1
    assert FrozenObservation.model_validate_json(frozen.model_dump_json()) == frozen
    assert frozen.evidence_hash.startswith("sha256:")

    # 同一 Case 两个 metric 合法
    metric_a = _metric("file-content:report.json")
    metric_b = _metric("tool-call:read_file", value=2.0, unit="calls")
    for metric in (metric_a, metric_b):
        assert MetricResult.model_validate_json(metric.model_dump_json()) == metric

    score_set = ScoreSet.model_validate({
        "run_id": "run-1",
        "scoring_pass_id": "pass-1",
        "scores": [
            _metric_score("file-content:report.json"),
            _metric_score("tool-call:read_file", passed=False),
        ],
    })
    assert len(score_set.scores) == 2
    scoring_pass = ScoringPass.model_validate({
        "id": "pass-1", "run_id": "run-1", "scorer_id": "agent-deterministic",
        "scorer_version": "1", "scores": [score_set.scores[0].model_dump(),
                                          score_set.scores[1].model_dump()],
    })
    assert ScoringPass.model_validate_json(scoring_pass.model_dump_json()) == scoring_pass

    # 旧单指标 Score 继续可读（无 metric 字段）
    old_score = Score.model_validate({"case_id": "case-1", "passed": True})
    assert old_score.metric_id is None and old_score.evaluator_id is None
    legacy_set = ScoreSet.model_validate({
        "run_id": "run-1", "scoring_pass_id": "pass-2", "scores": [old_score.model_dump()],
    })
    assert legacy_set.scores[0].passed is True


def test_duplicate_metric_key_rejected():
    with pytest.raises(ValidationError, match="unique"):
        ScoreSet.model_validate({
            "run_id": "run-1",
            "scoring_pass_id": "pass-1",
            "scores": [
                _metric_score("file-content:report.json"),
                _metric_score("file-content:report.json", passed=False),
            ],
        })


def test_mixed_legacy_and_metric_scores_rejected():
    with pytest.raises(ValidationError, match="mix"):
        ScoreSet.model_validate({
            "run_id": "run-1",
            "scoring_pass_id": "pass-1",
            "scores": [
                _metric_score("file-content:report.json"),
                Score(case_id="case-1", passed=True).model_dump(),
            ],
        })


def test_metric_fields_partial_rejected():
    # multi-metric 集合内每个 score 都必须带完整 metric 身份
    with pytest.raises(ValidationError, match="metric_id"):
        ScoreSet.model_validate({
            "run_id": "run-1",
            "scoring_pass_id": "pass-1",
            "scores": [_metric_score("file-content:report.json"),
                       Score(case_id="case-1", passed=True, evaluator_id="agent-deterministic",
                             evaluator_version="1").model_dump()],
        })


def test_nan_and_infinity_rejected():
    with pytest.raises(ValidationError):
        Score(case_id="case-1", value=float("nan"))
    with pytest.raises(ValidationError):
        _metric("x", value=float("inf"))
    with pytest.raises(ValueError, match="NaN|Infinity|finite"):
        observation_evidence_hash({"value": float("nan")})
    with pytest.raises(ValueError, match="NaN|Infinity|finite"):
        observation_evidence_hash({"nested": {"value": float("-inf")}})


def test_external_evidence_ref_rejected():
    with pytest.raises(ValidationError):
        EvidenceRef(kind="url", run_id="run-1", locator="https://example.com/evil")
    with pytest.raises(ValidationError):
        EvidenceRef(kind="artifact", run_id="run-1", locator="../outside.txt")
    with pytest.raises(ValidationError):
        EvidenceRef(kind="artifact", run_id="run-1", locator="/etc/passwd")
    with pytest.raises(ValidationError):
        EvidenceRef(kind="artifact", run_id="run-1", locator="a\\b.txt")
    with pytest.raises(ValidationError):
        EvidenceRef(kind="event", run_id="run-1", locator="not-a-seq")


def test_evidence_hash_binds_frozen_view():
    payload_a = _observation_payload()
    hash_a = observation_evidence_hash(payload_a)
    payload_b = _observation_payload()
    assert observation_evidence_hash(payload_b) == hash_a
    changed = _observation_payload(final_output="changed")
    assert observation_evidence_hash(changed) != hash_a
    # recorded_at 是审计字段，不改变评分输入视图
    with_time = _observation_payload(recorded_at="2026-09-19T00:00:00+00:00")
    assert observation_evidence_hash(with_time) == hash_a


def test_metric_status_not_scored_cannot_pass_or_fabricate_value():
    with pytest.raises(ValidationError, match="passed"):
        _metric("x", status=MetricStatus.insufficient_evidence, value=None, passed=True)
    with pytest.raises(ValidationError, match="value"):
        _metric("x", status=MetricStatus.evaluator_error, value=0.0, passed=None)
    unknown = _metric("x", status=MetricStatus.insufficient_evidence, value=None, passed=None)
    assert unknown.passed is None and unknown.value is None


def test_evaluator_spec_roundtrip():
    spec = EvaluatorSpec.model_validate({
        "evaluator_id": "agent-deterministic", "version": "1",
        "config": {"metrics": ["file-content"]},
        "required_evidence": ["final_output", "artifacts"],
        "missing_policy": "insufficient_evidence",
        "metric_ids": ["file-content:report.json"],
    })
    assert EvaluatorSpec.model_validate_json(spec.model_dump_json()) == spec
    with pytest.raises(ValidationError):
        EvaluatorSpec.model_validate({
            "evaluator_id": "x", "version": "1", "config": {},
            "missing_policy": "silently-pass",
        })


def test_agent_result_and_termination():
    result = AgentResult.model_validate({
        "final_output": "done", "termination_reason": "final_answer",
        "evidence_coverage": {"complete": True},
    })
    assert AgentResult.model_validate_json(result.model_dump_json()) == result
    for reason in ("final_answer", "max_steps", "max_tool_calls", "wall_time",
                   "token_limit", "cost_limit", "cancelled", "error"):
        assert TerminationRecord(reason=reason).reason == reason
    with pytest.raises(ValidationError):
        TerminationRecord(reason="felt-like-it")


def test_invocation_record_boundaries():
    settled = InvocationRecord.model_validate({
        "id": "inv-1", "run_id": "run-1", "case_id": "case-1",
        "kind": "tool", "tool_name": "write_file", "step": 2,
        "status": "settled", "outcome": "succeeded",
        "request_summary": {"path": "report.json"},
        "settled_at": "2026-09-19T00:00:00+00:00",
    })
    assert InvocationRecord.model_validate_json(settled.model_dump_json()) == settled
    # 非 settled 不得携带 outcome；settled 必须有 outcome
    with pytest.raises(ValidationError, match="outcome"):
        InvocationRecord.model_validate({
            "id": "inv-2", "run_id": "run-1", "case_id": "case-1",
            "kind": "model", "step": 1, "status": "dispatching", "outcome": "succeeded",
        })
    with pytest.raises(ValidationError, match="outcome"):
        InvocationRecord.model_validate({
            "id": "inv-3", "run_id": "run-1", "case_id": "case-1",
            "kind": "model", "step": 1, "status": "settled",
        })
    with pytest.raises(ValidationError, match="tool_name"):
        InvocationRecord.model_validate({
            "id": "inv-4", "run_id": "run-1", "case_id": "case-1",
            "kind": "tool", "step": 1, "status": "prepared",
        })


def test_invocation_kind_enum_values():
    assert InvocationKind("model") == "model"
    assert InvocationKind("tool") == "tool"
    with pytest.raises(ValueError):
        InvocationKind("judge")


def test_artifact_entry_safety():
    with pytest.raises(ValidationError):
        ArtifactEntry(artifact_id="../escape", path="report.json")
    with pytest.raises(ValidationError):
        ArtifactEntry(artifact_id="run-1/report.json", path="/absolute/path.json")
    with pytest.raises(ValidationError):
        ArtifactEntry(artifact_id="run-1/report.json", path="a/../b.json")
    entry = ArtifactEntry(artifact_id="run-1/report.json", path="report.json", available=False)
    assert entry.sha256 is None and entry.available is False
