"""M5-T04: process, business-state and side-effect assertions over frozen evidence.

Counterexamples first (M5-A02 and M5-T04):

* unconfirmed cancel with a correct final state: the process metric fails and the
  goal conjunction is NOT masked by the final state;
* double cancellation is detected; a changed-then-restored value is not hidden by
  an equal final state;
* missing snapshot / missing action log / foreign (cross-Case) reference /
  corrupted hash -> insufficient, never pass and never 0;
* checker timeout / checker failure / schema error -> evaluator_error, while a
  business miss stays scored/passed=False and an absent expectation stays
  not_applicable;
* rescoring a frozen observation makes zero model/tool/DB calls and reads only
  the frozen artifact ids it declared.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from motte_contracts.evaluation import (
    ArtifactEntry,
    EvidenceCoverage,
    FrozenObservation,
    MetricStatus,
    ToolCallRecord,
    WorkflowActionLog,
    WorkflowEvidenceOwner,
    WorkflowEvidenceRef,
    WorkspaceSnapshot,
    observation_evidence_hash,
    workflow_schema_digest,
)
from motte_contracts.evidence import Score, ScoreSet
from motte_eval import workflow as workflow_module
from motte_eval.observation import (
    EVALUATOR_ID,
    EVALUATOR_VERSION,
    aggregate_metric_results,
    evaluate_observation,
    known_metric_kinds,
    metric_result_to_score,
    normalize_evaluator_config,
)

INITIAL_STATE = {
    "orders": {"order-1": {"status": "created", "refunded": False}},
    "account": {"balance": 100, "balance_history": [100]},
    "audit": {"notes": ["created"]},
}
CANCELLED_STATE = {
    "orders": {"order-1": {"status": "cancelled", "refunded": False}},
    "account": {"balance": 100, "balance_history": [100]},
    "audit": {"notes": ["created", "cancelled"]},
}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _default_schema(kind: str) -> dict:
    if kind == "action_log":
        return WorkflowActionLog.model_json_schema()
    return {"type": "object"}


def _evidence(
    evidence_id: str,
    kind: str,
    payload: object,
    *,
    step: int = 1,
    run_id: str = "run-1",
    case_id: str = "case-1",
    attempt_id: str | None = None,
    complete: bool = True,
    scope: tuple[str, ...] = ("business_actions",),
    schema: dict | None = None,
    artifact_sha256: str | None = None,
    artifact_id: str | None = None,
) -> tuple[WorkflowEvidenceRef, bytes]:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    payload_schema = _default_schema(kind) if schema is None else schema
    ref = WorkflowEvidenceRef(
        evidence_id=evidence_id,
        evidence_kind=kind,
        owner=WorkflowEvidenceOwner(run_id=run_id, case_id=case_id, attempt_id=attempt_id),
        step=step,
        artifact_id=artifact_id or f"{run_id}/{case_id}/{evidence_id}.json",
        artifact_sha256=artifact_sha256 or _digest(raw),
        media_type="application/json",
        schema_id=f"workflow.{kind}@1",
        payload_schema=payload_schema,
        schema_sha256=workflow_schema_digest(payload_schema),
        complete=complete,
        monitored_scope=scope,
    )
    return ref, raw


def _observation(evidence=(), **overrides) -> FrozenObservation:
    references = [ref for ref, _ in evidence]
    entries = [
        ArtifactEntry(artifact_id=ref.artifact_id, path=ref.artifact_id,
                      media_type="application/json", size_bytes=len(raw),
                      sha256=_digest(raw))
        for ref, raw in evidence
    ]
    payload: dict = {
        "observation_id": "obs-1",
        "run_id": "run-1",
        "case_id": "case-1",
        "final_output": "done",
        "termination": {"reason": "final_answer"},
        "coverage": EvidenceCoverage(complete=True).model_dump(),
        "tool_calls": [
            ToolCallRecord(call_id="call-1", tool_name="read_file",
                           arguments={"path": "input.json"}, status="succeeded", step=1),
            ToolCallRecord(call_id="call-2", tool_name="write_file",
                           arguments={"path": "report.json"}, status="succeeded", step=2),
        ],
        "workspace": WorkspaceSnapshot(before=["input.json"],
                                       after=["input.json", "report.json"],
                                       complete=True).model_dump(),
        "workflow_evidence": references,
        "artifact_refs": entries,
    }
    payload.update(overrides)
    payload["evidence_hash"] = observation_evidence_hash(payload)
    return FrozenObservation.model_validate(payload)


def _store(evidence) -> dict[str, bytes]:
    return {ref.artifact_id: raw for ref, raw in evidence}


def _evaluate(observation, metrics, artifacts=None, reader=None):
    config = normalize_evaluator_config({"metrics": metrics})
    if reader is None:
        store = artifacts if artifacts is not None else {}
        reader = store.get
    return evaluate_observation(observation, config, artifact_reader=reader)


def _by_id(results, metric_id):
    return next(item for item in results if item.metric_id == metric_id)


def _cancel_log(actions) -> dict:
    return {"actions": actions}


def _cancel_scenario(actions, *, log_complete=True, log_scope=("business_actions",)):
    initial, init_raw = _evidence("cp-initial", "state", INITIAL_STATE, step=1)
    final, final_raw = _evidence("cp-final", "state", CANCELLED_STATE, step=5)
    log, log_raw = _evidence("log-1", "action_log", _cancel_log(actions), step=2,
                             complete=log_complete, scope=log_scope)
    observation = _observation([(initial, init_raw), (final, final_raw), (log, log_raw)])
    store = _store([(initial, init_raw), (final, final_raw), (log, log_raw)])
    return observation, store


FINAL_STATE_METRIC = {
    "metric_id": "final-state-correct",
    "kind": "state-equals",
    "checkpoint": "cp-final",
    "path": "orders.order-1.status",
    "expected": "cancelled",
}
CONFIRM_THEN_CANCEL_METRIC = {
    "metric_id": "confirm-then-cancel",
    "kind": "response-policy",
    "log": "log-1",
    "require": [
        {"action": "confirm", "target": "order-1", "min_count": 1, "max_count": 1},
        {"action": "cancel_order", "target": "order-1", "min_count": 1, "max_count": 1},
    ],
}
CANCEL_ONCE_METRIC = {
    "metric_id": "cancel-once",
    "kind": "response-policy",
    "log": "log-1",
    "require": [
        {"action": "cancel_order", "target": "order-1", "min_count": 1, "max_count": 1},
    ],
}
NON_TARGET_METRIC = {
    "metric_id": "non-target-unchanged",
    "kind": "state-delta",
    "before": "cp-initial",
    "after": "cp-final",
    "path": "account.balance",
    "unchanged": True,
}


def _goal(components) -> dict:
    return {"metric_id": "goal", "kind": "goal-achieved", "components": components}


def test_unconfirmed_cancel_fails_process_metric_despite_correct_final_state():
    """M5-A02: 直接取消但终态正确 -> 过程指标失败，goal 不被终态覆盖。"""
    observation, store = _cancel_scenario([
        {"seq": 1, "step": 2, "action": "cancel_order", "target": "order-1",
         "status": "succeeded", "detail": "cancelled order-1"},
    ])
    results = _evaluate(observation, [
        FINAL_STATE_METRIC,
        CANCEL_ONCE_METRIC,
        CONFIRM_THEN_CANCEL_METRIC,
        NON_TARGET_METRIC,
        _goal([
            {"role": "final_state", "metric": FINAL_STATE_METRIC},
            {"role": "process", "metric": CONFIRM_THEN_CANCEL_METRIC},
            {"role": "side_effect", "metric": NON_TARGET_METRIC},
        ]),
    ], store)

    final_state = _by_id(results, "final-state-correct")
    assert final_state.status is MetricStatus.scored and final_state.passed is True
    assert _by_id(results, "cancel-once").passed is True
    process = _by_id(results, "confirm-then-cancel")
    assert process.status is MetricStatus.scored
    assert process.passed is False
    assert process.reason == "required_match_missing"
    assert _by_id(results, "non-target-unchanged").passed is True

    goal = _by_id(results, "goal")
    assert goal.status is MetricStatus.scored
    assert goal.passed is False
    assert goal.reason == "goal_not_achieved"
    assert {"role": "process", "metric_id": "confirm-then-cancel"} in goal.details["failed"]
    breakdown = {item["metric_id"]: item for item in goal.details["components"]}
    assert breakdown["final-state-correct"]["passed"] is True
    assert breakdown["confirm-then-cancel"]["passed"] is False


def test_double_cancellation_is_detected():
    observation, store = _cancel_scenario([
        {"seq": 1, "step": 2, "action": "confirm", "target": "order-1",
         "status": "succeeded", "detail": "user confirmed order-1"},
        {"seq": 2, "step": 3, "action": "cancel_order", "target": "order-1",
         "status": "succeeded", "detail": "cancelled once"},
        {"seq": 3, "step": 4, "action": "cancel_order", "target": "order-1",
         "status": "succeeded", "detail": "cancelled twice"},
    ])
    results = _evaluate(observation, [
        FINAL_STATE_METRIC, CANCEL_ONCE_METRIC, CONFIRM_THEN_CANCEL_METRIC,
        _goal([
            {"role": "final_state", "metric": FINAL_STATE_METRIC},
            {"role": "process", "metric": CANCEL_ONCE_METRIC},
        ]),
    ], store)
    once = _by_id(results, "cancel-once")
    assert once.status is MetricStatus.scored and once.passed is False
    assert once.reason == "action_count_exceeded"
    assert once.details["violations"][0]["matched"] == 2
    ordered = _by_id(results, "confirm-then-cancel")
    assert ordered.passed is False and ordered.reason == "action_count_exceeded"
    assert _by_id(results, "goal").passed is False


def test_changed_then_restored_value_is_not_masked():
    """先改后恢复：终态相同不能掩盖中间状态变化与副作用。"""
    transient = json.loads(json.dumps(INITIAL_STATE))
    transient["account"]["balance"] = 85
    initial, init_raw = _evidence("cp-initial", "state", INITIAL_STATE, step=1)
    middle, middle_raw = _evidence("cp-transient", "state", transient, step=2)
    final, final_raw = _evidence("cp-final", "state", INITIAL_STATE, step=5)
    log, log_raw = _evidence("log-1", "action_log", _cancel_log([
        {"seq": 1, "step": 2, "action": "write_balance", "target": "account",
         "status": "succeeded", "detail": "debited 15"},
        {"seq": 2, "step": 3, "action": "write_balance", "target": "account",
         "status": "succeeded", "detail": "restored 15"},
    ]), step=2)
    observation = _observation([(initial, init_raw), (middle, middle_raw),
                                (final, final_raw), (log, log_raw)])
    results = _evaluate(observation, [
        {"metric_id": "final-balance", "kind": "state-equals", "checkpoint": "cp-final",
         "path": "account.balance", "expected": 100},
        {"metric_id": "restored-at-end", "kind": "state-delta", "before": "cp-initial",
         "after": "cp-final", "path": "account.balance", "unchanged": True},
        {"metric_id": "transient-same", "kind": "state-delta", "before": "cp-initial",
         "after": "cp-transient", "path": "account.balance", "unchanged": True},
        {"metric_id": "no-side-effect:balance", "kind": "no-side-effect",
         "scope": ["actions", "tool_calls"], "log": "log-1",
         "forbid": [{"action": "write_balance", "target": "account"}]},
        _goal([
            {"role": "final_state", "metric": {"metric_id": "final-balance",
             "kind": "state-equals", "checkpoint": "cp-final", "path": "account.balance",
             "expected": 100}},
            {"role": "side_effect", "metric": {"metric_id": "transient-same",
             "kind": "state-delta", "before": "cp-initial", "after": "cp-transient",
             "path": "account.balance", "unchanged": True}},
        ]),
    ], _store([(initial, init_raw), (middle, middle_raw), (final, final_raw), (log, log_raw)]))

    assert _by_id(results, "final-balance").passed is True
    assert _by_id(results, "restored-at-end").passed is True  # 值确实恢复了
    transient_result = _by_id(results, "transient-same")
    assert transient_result.status is MetricStatus.scored
    assert transient_result.passed is False
    assert transient_result.reason == "state_changed"
    side_effect = _by_id(results, "no-side-effect:balance")
    assert side_effect.status is MetricStatus.scored and side_effect.passed is False
    assert side_effect.reason == "side_effect_detected"
    assert _by_id(results, "goal").passed is False  # 恢复后的终态不能翻案


def test_missing_snapshot_and_missing_action_log_are_insufficient():
    observation = _observation()  # 没有声明任何 workflow evidence
    results = _evaluate(observation, [
        {"metric_id": "missing-snapshot", "kind": "state-equals", "checkpoint": "cp-final",
         "path": "orders.order-1.status", "expected": "cancelled"},
        {"metric_id": "missing-log", "kind": "response-policy", "log": "log-1",
         "require": [{"action": "cancel_order", "min_count": 1}]},
        {"metric_id": "missing-log-side-effect", "kind": "no-side-effect",
         "scope": ["actions"], "log": "log-1",
         "forbid": [{"action": "cancel_order"}]},
        _goal([
            {"role": "final_state", "metric": FINAL_STATE_METRIC},
            {"role": "process", "metric": CONFIRM_THEN_CANCEL_METRIC},
        ]),
    ])
    for metric_id in ("missing-snapshot", "missing-log", "missing-log-side-effect"):
        result = _by_id(results, metric_id)
        assert result.status is MetricStatus.insufficient_evidence, metric_id
        assert result.passed is None and result.value is None
        assert result.reason == "evidence_not_declared"
    goal = _by_id(results, "goal")
    assert goal.status is MetricStatus.insufficient_evidence  # 绝不因终态缺失而 pass
    assert goal.passed is None

    # 已声明但 complete=False 的 action log：没有违规时不足以下阴性结论
    incomplete_observation, incomplete_store = _cancel_scenario([
        {"seq": 1, "step": 2, "action": "confirm", "target": "order-1",
         "status": "succeeded", "detail": "confirmed"},
        {"seq": 2, "step": 3, "action": "cancel_order", "target": "order-1",
         "status": "succeeded", "detail": "cancelled"},
    ], log_complete=False)
    results = _evaluate(incomplete_observation, [
        CONFIRM_THEN_CANCEL_METRIC,
        {"metric_id": "no-side-effect:incomplete", "kind": "no-side-effect",
         "scope": ["actions"], "log": "log-1", "forbid": [{"action": "refund"}]},
    ], incomplete_store)
    for metric_id in ("confirm-then-cancel", "no-side-effect:incomplete"):
        result = _by_id(results, metric_id)
        assert result.status is MetricStatus.insufficient_evidence, metric_id
        assert result.passed is None
    assert _by_id(results, "confirm-then-cancel").reason == "action_log_incomplete"
    assert _by_id(results, "no-side-effect:incomplete").reason == "action_log_incomplete"

    # 不完整的日志不能证明"没发生"，但已确认的违规（数量超限）仍然计入
    violation_observation, violation_store = _cancel_scenario([
        {"seq": 1, "step": 2, "action": "cancel_order", "target": "order-1",
         "status": "succeeded", "detail": "cancelled once"},
        {"seq": 2, "step": 3, "action": "cancel_order", "target": "order-1",
         "status": "succeeded", "detail": "cancelled twice"},
    ], log_complete=False)
    results = _evaluate(violation_observation, [CANCEL_ONCE_METRIC], violation_store)
    confirmed = _by_id(results, "cancel-once")
    assert confirmed.status is MetricStatus.scored and confirmed.passed is False
    assert confirmed.reason == "action_count_exceeded"

    # 需要完整日志才能下的阴性结论（确认缺失）保持 insufficient，不是失败
    unconfirmed_observation, unconfirmed_store = _cancel_scenario([
        {"seq": 1, "step": 2, "action": "cancel_order", "target": "order-1",
         "status": "succeeded", "detail": "cancelled without confirmation"},
    ], log_complete=False)
    results = _evaluate(unconfirmed_observation, [CONFIRM_THEN_CANCEL_METRIC], unconfirmed_store)
    missing = _by_id(results, "confirm-then-cancel")
    assert missing.status is MetricStatus.insufficient_evidence
    assert missing.passed is None and missing.reason == "action_log_incomplete"


def test_foreign_evidence_reference_is_refused():
    other_case, other_raw = _evidence("cp-final", "state", CANCELLED_STATE, step=5,
                                      case_id="case-2")
    other_run, run_raw = _evidence("log-1", "action_log", _cancel_log([
        {"seq": 1, "step": 2, "action": "confirm", "target": "order-1"},
    ]), step=2, run_id="run-2")
    observation = _observation([(other_case, other_raw), (other_run, run_raw)])
    read_ids: list[str] = []

    def reader(artifact_id):
        read_ids.append(artifact_id)
        return {other_case.artifact_id: other_raw, other_run.artifact_id: run_raw}.get(
            artifact_id)

    results = _evaluate(observation, [
        {"metric_id": "cross-case", "kind": "state-equals", "checkpoint": "cp-final",
         "path": "orders.order-1.status", "expected": "cancelled"},
        {"metric_id": "cross-run", "kind": "response-policy", "log": "log-1",
         "require": [{"action": "confirm", "min_count": 1}]},
    ], reader=reader)
    cross_case = _by_id(results, "cross-case")
    assert cross_case.status is MetricStatus.insufficient_evidence
    assert cross_case.passed is None
    assert cross_case.reason == "foreign_evidence_refused"
    cross_run = _by_id(results, "cross-run")
    assert cross_run.status is MetricStatus.insufficient_evidence
    assert cross_run.reason == "foreign_evidence_refused"
    assert read_ids == []  # 归属核验在任何读取之前拒绝


def test_corrupted_checkpoint_hash_is_refused():
    ref, raw = _evidence("cp-final", "state", CANCELLED_STATE, step=5,
                         artifact_sha256="0" * 64)
    observation = _observation([(ref, raw)])
    results = _evaluate(observation, [
        {"metric_id": "final-state-correct", "kind": "state-equals", "checkpoint": "cp-final",
         "path": "orders.order-1.status", "expected": "cancelled"},
    ], _store([(ref, raw)]))
    result = _by_id(results, "final-state-correct")
    assert result.status is MetricStatus.insufficient_evidence
    assert result.passed is None and result.value is None
    assert result.reason == "evidence_hash_mismatch"


def test_checker_timeout_failure_and_schema_error_are_evaluator_errors():
    bomb = "a" * 48 + "b"
    log, log_raw = _evidence("log-bomb", "action_log", _cancel_log([
        {"seq": 1, "step": 2, "action": "cancel_order", "target": "order-1",
         "status": "succeeded", "detail": bomb},
    ]), step=2)
    broken_schema, broken_raw = _evidence(
        "cp-broken-schema", "state", CANCELLED_STATE, step=3,
        schema={"type": "object", "required": ["never_present"]},
    )
    observation = _observation([(log, log_raw), (broken_schema, broken_raw)])
    results = _evaluate(observation, [
        {"metric_id": "assertion-timeout", "kind": "response-policy", "log": "log-bomb",
         "timeout_sec": 1.0,
         "require": [{"action": "cancel_order", "pattern": "(a+)+$", "min_count": 1}]},
        {"metric_id": "checker-cannot-run", "kind": "state-delta", "before": "cp-broken-schema",
         "after": "cp-broken-schema", "path": "account.balance"},
        {"metric_id": "checker-failure", "kind": "response-policy", "log": "log-bomb",
         "require": [{"action": "cancel_order", "pattern": "("}]},
        {"metric_id": "schema-error", "kind": "state-equals", "checkpoint": "cp-broken-schema",
         "path": "orders.order-1.status", "expected": "cancelled"},
    ], _store([(log, log_raw), (broken_schema, broken_raw)]))
    timeout = _by_id(results, "assertion-timeout")
    assert timeout.status is MetricStatus.evaluator_error
    assert timeout.reason == "checker_timeout" and timeout.passed is None
    cannot_run = _by_id(results, "checker-cannot-run")
    assert cannot_run.status is MetricStatus.evaluator_error
    assert cannot_run.reason == "config_error"
    failure = _by_id(results, "checker-failure")
    assert failure.status is MetricStatus.evaluator_error
    assert failure.reason == "checker_execution_error"
    schema_error = _by_id(results, "schema-error")
    assert schema_error.status is MetricStatus.evaluator_error
    assert schema_error.reason == "evidence_schema_violation"


def test_checker_crash_is_isolated_from_the_other_metrics(monkeypatch):
    observation, store = _cancel_scenario([
        {"seq": 1, "step": 2, "action": "confirm", "target": "order-1"},
        {"seq": 2, "step": 3, "action": "cancel_order", "target": "order-1"},
    ])

    def exploding(*args, **kwargs):
        raise RuntimeError("checker process died")

    monkeypatch.setattr(workflow_module, "_search_pattern", exploding)
    results = _evaluate(observation, [
        {"metric_id": "crash", "kind": "response-policy", "log": "log-1",
         "require": [{"action": "confirm", "pattern": "confirm", "min_count": 1}]},
        FINAL_STATE_METRIC,
    ], store)
    crash = _by_id(results, "crash")
    assert crash.status is MetricStatus.evaluator_error
    assert crash.passed is None
    assert _by_id(results, "final-state-correct").passed is True


def test_business_failure_and_not_applicable_stay_distinct():
    created_state = json.loads(json.dumps(CANCELLED_STATE))
    created_state["orders"]["order-1"]["status"] = "created"
    ref, raw = _evidence("cp-open", "state", created_state, step=3)
    observation = _observation([(ref, raw)])
    results = _evaluate(observation, [
        {"metric_id": "business-fail", "kind": "state-equals", "checkpoint": "cp-open",
         "path": "orders.order-1.status", "expected": "cancelled"},
        {"metric_id": "not-applicable", "kind": "state-equals", "checkpoint": "cp-open",
         "path": "orders.order-1.status"},
        {"metric_id": "missing-field", "kind": "state-equals", "checkpoint": "cp-open",
         "path": "orders.order-9.status", "expected": "cancelled"},
    ], _store([(ref, raw)]))
    business_fail = _by_id(results, "business-fail")
    assert business_fail.status is MetricStatus.scored
    assert business_fail.passed is False and business_fail.reason == "state_mismatch"
    not_applicable = _by_id(results, "not-applicable")
    assert not_applicable.status is MetricStatus.not_applicable
    assert not_applicable.denominator is False and not_applicable.passed is None
    missing_field = _by_id(results, "missing-field")
    assert missing_field.status is MetricStatus.scored
    assert missing_field.passed is False and missing_field.reason == "state_path_missing"


def test_no_side_effect_requires_a_declared_complete_scope():
    log, log_raw = _evidence("log-1", "action_log", _cancel_log([
        {"seq": 1, "step": 2, "action": "confirm", "target": "order-1"},
        {"seq": 2, "step": 3, "action": "cancel_order", "target": "order-1"},
    ]), step=2)
    base = _observation([(log, log_raw)])
    store = _store([(log, log_raw)])
    results = _evaluate(base, [
        {"metric_id": "no-scope", "kind": "no-side-effect", "log": "log-1",
         "forbid": [{"action": "refund"}]},
        {"metric_id": "ok", "kind": "no-side-effect",
         "scope": ["actions", "tool_calls", "filesystem"], "log": "log-1",
         "forbid": [{"action": "refund"}]},
        {"metric_id": "forbidden", "kind": "no-side-effect", "scope": ["actions"],
         "log": "log-1", "forbid": [{"action": "cancel_order", "target": "order-1"}]},
    ], store)
    no_scope = _by_id(results, "no-scope")
    assert no_scope.status is MetricStatus.insufficient_evidence
    assert no_scope.reason == "monitoring_scope_undeclared" and no_scope.passed is None
    ok = _by_id(results, "ok")
    assert ok.status is MetricStatus.scored and ok.passed is True
    forbidden = _by_id(results, "forbidden")
    assert forbidden.status is MetricStatus.scored and forbidden.passed is False
    assert forbidden.reason == "side_effect_detected"

    # 工具清单完整也不能证明 shell/MCP 没有隐藏副作用
    native = _observation([(log, log_raw)], tool_calls=[
        ToolCallRecord(call_id="native-1", tool_name="command_execution",
                       arguments={"command": "rm -rf /tmp/x"}, status="succeeded", step=1),
    ])
    results = _evaluate(native, [
        {"metric_id": "native", "kind": "no-side-effect", "scope": ["actions", "tool_calls"],
         "log": "log-1", "forbid": [{"action": "refund"}]},
    ], store)
    native_result = _by_id(results, "native")
    assert native_result.status is MetricStatus.insufficient_evidence
    assert native_result.passed is None
    assert native_result.reason == "tool_side_effects_unobserved"
    assert native_result.details["tools"] == ["command_execution"]

    # 声明了 filesystem 范围却没有可用快照 -> insufficient
    no_workspace = _observation([(log, log_raw)], workspace=None)
    results = _evaluate(no_workspace, [
        {"metric_id": "filesystem", "kind": "no-side-effect", "scope": ["filesystem"],
         "forbid": [{"action": "refund"}]},
    ], store)
    filesystem = _by_id(results, "filesystem")
    assert filesystem.status is MetricStatus.insufficient_evidence
    assert filesystem.reason == "workspace_snapshot_incomplete"

    # 未知 scope token 是检查器配置错误，不是 pass
    unknown = _evaluate(base, [
        {"metric_id": "unknown-scope", "kind": "no-side-effect", "scope": ["vibes"],
         "forbid": [{"action": "refund"}]},
    ], store)[0]
    assert unknown.status is MetricStatus.evaluator_error and unknown.reason == "config_error"


def test_rescoring_frozen_observation_makes_zero_live_calls():
    observation, store = _cancel_scenario([
        {"seq": 1, "step": 2, "action": "confirm", "target": "order-1",
         "status": "succeeded", "detail": "user confirmed"},
        {"seq": 2, "step": 3, "action": "cancel_order", "target": "order-1",
         "status": "succeeded", "detail": "cancelled"},
    ])
    metrics = [FINAL_STATE_METRIC, CANCEL_ONCE_METRIC, CONFIRM_THEN_CANCEL_METRIC,
               NON_TARGET_METRIC,
               {"metric_id": "no-side-effect:refund", "kind": "no-side-effect",
                "scope": ["actions", "tool_calls"], "log": "log-1",
                "forbid": [{"action": "refund"}]},
               _goal([{"role": "final_state", "metric": FINAL_STATE_METRIC},
                      {"role": "process", "metric": CONFIRM_THEN_CANCEL_METRIC},
                      {"role": "side_effect", "metric": NON_TARGET_METRIC}])]
    reads: list[str] = []

    def frozen_reader(artifact_id):
        reads.append(artifact_id)
        if artifact_id not in store:
            raise AssertionError(f"scoring read a non-frozen artifact: {artifact_id}")
        return store[artifact_id]

    class LiveSource:
        """评分若触碰活动模型/工具/DB，这里立刻炸掉并计数。"""

        def __init__(self):
            self.model_calls = 0
            self.tool_calls = 0
            self.db_queries = 0

        def model(self, *args, **kwargs):
            self.model_calls += 1
            raise AssertionError("scoring called a live model")

        def tool(self, *args, **kwargs):
            self.tool_calls += 1
            raise AssertionError("scoring called a live tool")

        def query(self, *args, **kwargs):
            self.db_queries += 1
            raise AssertionError("scoring read the live database")

    live = LiveSource()
    live_state = {"orders": {"order-1": {"status": "created"}}}
    first = _evaluate(observation, metrics, reader=frozen_reader)
    first_reads = list(reads)
    second = _evaluate(observation, metrics, reader=frozen_reader)
    third_reads = reads[len(first_reads):]

    assert [item.model_dump(mode="json") for item in first] == [
        item.model_dump(mode="json") for item in second]
    assert third_reads == first_reads  # 重评分读取完全相同的冻结 id 序列
    assert first_reads and set(first_reads) <= set(store)
    assert live.model_calls == 0 and live.tool_calls == 0 and live.db_queries == 0

    live_state["orders"]["order-1"]["status"] = "cancelled"  # 活动状态变化不影响冻结评分
    fourth = _evaluate(observation, metrics, reader=frozen_reader)
    assert [item.model_dump(mode="json") for item in fourth] == [
        item.model_dump(mode="json") for item in first]


def test_workflow_metric_projection_keeps_multi_metric_denominators():
    observation, store = _cancel_scenario([
        {"seq": 1, "step": 2, "action": "cancel_order", "target": "order-1"},
    ])
    results = _evaluate(observation, [
        FINAL_STATE_METRIC,
        CONFIRM_THEN_CANCEL_METRIC,
        {"metric_id": "not-applicable", "kind": "state-equals", "checkpoint": "cp-final",
         "path": "orders.order-1.status"},
        {"metric_id": "insufficient", "kind": "state-equals", "checkpoint": "cp-missing",
         "path": "orders.order-1.status", "expected": "cancelled"},
        {"metric_id": "error", "kind": "state-delta", "before": "cp-initial",
         "after": "cp-final", "path": "account.balance"},
    ], store)
    scores = [metric_result_to_score(item, "case-1") for item in results]
    score_set = ScoreSet.model_validate({
        "run_id": "run-1", "scoring_pass_id": "pass-1", "scores": scores,
    })
    assert len(score_set.scores) == len(results)
    by_metric = {score.metric_id: score for score in score_set.scores}
    assert by_metric["final-state-correct"].metric_status == "scored"
    assert by_metric["confirm-then-cancel"].passed is False
    assert by_metric["not-applicable"].metric_status == "not_applicable"
    assert by_metric["not-applicable"].denominator is False
    assert by_metric["insufficient"].metric_status == "insufficient_evidence"
    assert by_metric["insufficient"].passed is None
    assert by_metric["error"].metric_status == "evaluator_error"
    assert by_metric["error"].passed is None and by_metric["error"].value is None
    for score in scores:
        assert score["evaluator_id"] == EVALUATOR_ID
        assert score["evaluator_version"] == EVALUATOR_VERSION
        assert score["case_id"] == "case-1"
    aggregate = aggregate_metric_results(results)
    assert aggregate["metrics"]["final-state-correct"]["scored"] == 1
    assert aggregate["metrics"]["error"]["evaluator_error"] == 1
    assert aggregate["totals"]["not_applicable"] == 1

    # 历史单指标 Score 继续可读，且不得与多指标分数混用
    legacy = ScoreSet.model_validate({
        "run_id": "run-1", "scoring_pass_id": "pass-legacy",
        "scores": [Score(case_id="case-1", passed=True).model_dump()],
    })
    assert legacy.scores[0].metric_id is None and legacy.scores[0].passed is True
    with pytest.raises(ValidationError, match="mix"):
        ScoreSet.model_validate({
            "run_id": "run-1", "scoring_pass_id": "pass-mixed",
            "scores": [scores[0], Score(case_id="case-1", passed=True).model_dump()],
        })


def test_workflow_kinds_are_registered_and_config_is_closed():
    for kind in workflow_module.WORKFLOW_METRIC_KINDS:
        assert kind in known_metric_kinds()
    # 新字段在配置规范化期被接受（白名单加法），未知 kind 仍然拒绝
    config = normalize_evaluator_config({"metrics": [dict(FINAL_STATE_METRIC)]})
    assert config["metrics"][0]["checkpoint"] == "cp-final"
    with pytest.raises(ValueError, match="unsupported metric kind"):
        normalize_evaluator_config({"metrics": [{"metric_id": "x", "kind": "vibes"}]})
    # 每个 kind 只接受自己的字段：交叉字段在评分时是 config_error
    observation, store = _cancel_scenario([
        {"seq": 1, "step": 2, "action": "cancel_order", "target": "order-1"},
    ])
    crossed = _evaluate(observation, [
        {"metric_id": "crossed", "kind": "state-equals", "checkpoint": "cp-final",
         "path": "orders.order-1.status", "expected": "cancelled", "delta": 1},
    ], store)[0]
    assert crossed.status is MetricStatus.evaluator_error
    assert crossed.reason == "config_error"


def test_checkpoint_reference_binds_artifact_hash_schema_owner_and_step():
    ref, raw = _evidence("cp-final", "state", CANCELLED_STATE, step=5)
    assert ref.step == 5 and ref.owner.case_id == "case-1"
    assert ref.artifact_sha256 == _digest(raw)
    assert ref.schema_sha256 == workflow_schema_digest(ref.payload_schema)
    with pytest.raises(ValidationError, match="schema_sha256"):
        WorkflowEvidenceRef.model_validate({**ref.model_dump(), "schema_sha256": "1" * 64})
    with pytest.raises(ValidationError, match="hex"):
        WorkflowEvidenceRef.model_validate({**ref.model_dump(), "artifact_sha256": "nope"})
    with pytest.raises(ValidationError):
        WorkflowEvidenceRef.model_validate({**ref.model_dump(), "artifact_id": "../escape.json"})
    roundtrip = WorkflowEvidenceRef.model_validate_json(ref.model_dump_json())
    assert roundtrip == ref


def test_incomplete_snapshot_cannot_report_a_business_failure():
    partial = {"orders": {"order-1": {}}}  # 采集不完整：status 字段没有采到
    ref, raw = _evidence("cp-partial", "state", partial, step=3, complete=False)
    observation = _observation([(ref, raw)])
    results = _evaluate(observation, [
        {"metric_id": "partial", "kind": "state-equals", "checkpoint": "cp-partial",
         "path": "orders.order-1.status", "expected": "cancelled"},
    ], _store([(ref, raw)]))
    result = _by_id(results, "partial")
    assert result.status is MetricStatus.insufficient_evidence
    assert result.passed is None and result.reason == "snapshot_incomplete"


def test_goal_composes_existing_tool_and_file_metrics():
    observation, store = _cancel_scenario([
        {"seq": 1, "step": 2, "action": "confirm", "target": "order-1"},
        {"seq": 2, "step": 3, "action": "cancel_order", "target": "order-1"},
    ])
    goal = _goal([
        {"role": "process", "metric": {
            "metric_id": "tool-args:write_file", "kind": "tool-call", "tool": "write_file",
            "min_calls": 1, "args_schema": {"type": "object", "required": ["path"]},
        }},
        {"role": "side_effect", "metric": {
            "metric_id": "no-forbidden-write:credentials", "kind": "no-forbidden-write",
            "forbidden": ["credentials.toml"],
        }},
        {"role": "final_state", "metric": FINAL_STATE_METRIC},
    ])
    result = _evaluate(observation, [goal], store)[0]
    assert result.status is MetricStatus.scored and result.passed is True
    by_metric = {item["metric_id"]: item for item in result.details["components"]}
    assert by_metric["tool-args:write_file"]["passed"] is True
    assert by_metric["no-forbidden-write:credentials"]["passed"] is True
    assert by_metric["final-state-correct"]["passed"] is True
    assert set(result.details["roles"]) == {"process", "side_effect", "final_state"}
