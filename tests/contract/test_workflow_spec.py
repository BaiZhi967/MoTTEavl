"""M5-T01 契约反例：七种 step、受限条件、全局预算与版本化发布。

这些用例证明的是**拒绝行为**与确定性编译，不是字段声明存在：
每个反例都要求抛出具体错误码/异常类型，合法样例要求两次编译得到同一
快照与 hash（零付费动作、零执行）。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from motte_contracts.workflow import (
    MAX_CONDITION_NODES,
    MAX_LOOP_ITERATIONS,
    MAX_STEP_NESTING_DEPTH,
    Condition,
    WorkflowVersion,
    workflow_content_hash,
)
from motte_scenario.compiler import (
    WorkflowResolutionError,
    compile_workflow,
    resolve_workflow_ref,
)
from motte_scenario.conditions import ConditionError, compile_condition, evaluate_condition

PUBLISHED_AT = "2026-09-21T00:00:00Z"


def checkpoint(step_id: str, **overrides):
    payload = {
        "step_id": step_id,
        "kind": "checkpoint",
        "assertions": [{"op": "eq", "path": "state.order.status", "value": "active"}],
    }
    payload.update(overrides)
    return payload


def minimal_workflow(**overrides):
    payload = {
        "workflow_id": "order-cancel-confirmed",
        "version": "1",
        "published_at": PUBLISHED_AT,
        "steps": [
            {"step_id": "request", "kind": "send_message", "message": "请取消订单 order-1"},
            checkpoint("before-confirm"),
        ],
        "limits": {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 30},
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------- 正向：编译确定


def test_valid_minimal_workflow_compiles_deterministically():
    first = compile_workflow(minimal_workflow())
    second = compile_workflow(minimal_workflow())
    assert first.content_hash == second.content_hash
    assert first.snapshot == second.snapshot
    assert first.ref == "order-cancel-confirmed@1"
    assert first.limits.max_total_steps == 20
    assert first.target_requirements.multi_turn is True
    snapshot = first.snapshot_for_run()
    assert snapshot["content_hash"] == first.content_hash
    assert snapshot["step_ids"] == ["request", "before-confirm"]
    assert snapshot["snapshot"] == first.snapshot


def test_all_seven_step_kinds_are_accepted():
    workflow = WorkflowVersion.model_validate(minimal_workflow(steps=[
        {"step_id": "send", "kind": "send_message", "message": "hello"},
        {"step_id": "tool", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
         "arguments": {"order_id": "order-1"}, "tool_mode": "mock",
         "assertions": [{"op": "eq", "path": "tool_results.orders.cancel.ok", "value": True}]},
        {"step_id": "check", "kind": "assert",
         "assertions": [{"op": "exists", "path": "state.order"}]},
        {"step_id": "mark", "kind": "checkpoint", "label": "after-send"},
        {"step_id": "branch", "kind": "branch",
         "when": {"op": "eq", "path": "state.confirmed", "value": True},
         "then_steps": [{"step_id": "then-send", "kind": "send_message", "message": "confirmed"}],
         "else_steps": [{"step_id": "else-event", "kind": "trigger_fixture_event",
                         "event": "confirm_timeout", "payload": {"after_sec": 1}}]},
        {"step_id": "loop", "kind": "loop", "max_iterations": 3,
         "until": {"op": "eq", "path": "state.order.status", "value": "cancelled"},
         "body": [{"step_id": "loop-send", "kind": "send_message", "message": "retry"}]},
        {"step_id": "event", "kind": "trigger_fixture_event", "event": "user_confirmed"},
    ]))
    assert {step.kind for step in workflow.steps} == {
        "send_message", "invoke_fixture_tool", "assert", "checkpoint", "branch", "loop",
        "trigger_fixture_event",
    }
    # when 是动作判定，assertions 是紧随动作的断言：两者不混淆。
    branch = workflow.steps[4]
    assert branch.when.path == "state.confirmed"
    assert branch.assertions == ()


# --------------------------------------------------------------- 反例：结构与预算


def test_duplicate_step_ids_are_rejected_across_nesting():
    with pytest.raises(ValidationError, match="duplicate step_id"):
        WorkflowVersion.model_validate(minimal_workflow(steps=[
            {"step_id": "dup", "kind": "send_message", "message": "first"},
            {"step_id": "outer", "kind": "loop", "max_iterations": 2, "body": [
                {"step_id": "dup", "kind": "send_message", "message": "nested"},
            ]},
        ]))


def test_loop_without_max_iterations_is_rejected():
    with pytest.raises(ValidationError):
        WorkflowVersion.model_validate(minimal_workflow(steps=[
            {"step_id": "loop", "kind": "loop",
             "body": [{"step_id": "inner", "kind": "checkpoint"}]},
        ]))


def test_loop_iterations_beyond_ceiling_are_rejected():
    with pytest.raises(ValidationError, match="max_iterations"):
        WorkflowVersion.model_validate(minimal_workflow(steps=[
            {"step_id": "loop", "kind": "loop", "max_iterations": MAX_LOOP_ITERATIONS + 1,
             "body": [{"step_id": "inner", "kind": "checkpoint"}]},
        ]))


@pytest.mark.parametrize("limits", [
    {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": float("nan")},
    {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": float("inf")},
    {"max_total_steps": True, "max_turns": 4, "wall_time_sec": 30},
    {"max_total_steps": 20, "max_turns": True, "wall_time_sec": 30},
    {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": True},
    {"max_total_steps": 10 ** 9, "max_turns": 4, "wall_time_sec": 30},
    {"max_total_steps": 20, "max_turns": 10 ** 9, "wall_time_sec": 30},
    {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 10 ** 9},
    {"max_total_steps": 0, "max_turns": 4, "wall_time_sec": 30},
    {"max_total_steps": -1, "max_turns": 4, "wall_time_sec": 30},
    {"max_total_steps": 20, "max_turns": 4},
    {"max_total_steps": 20, "wall_time_sec": 30},
])
def test_invalid_budgets_are_rejected(limits):
    with pytest.raises(ValidationError):
        WorkflowVersion.model_validate(minimal_workflow(limits=limits))


def test_nesting_depth_bomb_is_rejected():
    step = {"step_id": "leaf", "kind": "send_message", "message": "x"}
    for index in range(MAX_STEP_NESTING_DEPTH + 2):
        step = {"step_id": f"loop-{index}", "kind": "loop", "max_iterations": 2, "body": [step]}
    with pytest.raises(ValidationError, match="nesting depth"):
        WorkflowVersion.model_validate(minimal_workflow(steps=[step]))


def test_forward_input_reference_is_rejected():
    with pytest.raises(ValidationError, match="before it produced output"):
        WorkflowVersion.model_validate(minimal_workflow(steps=[
            {"step_id": "second", "kind": "send_message", "message": "b", "input_ref": "first"},
            {"step_id": "first", "kind": "send_message", "message": "a"},
        ]))


def test_unknown_input_reference_is_rejected():
    with pytest.raises(ValidationError, match="references unknown step"):
        WorkflowVersion.model_validate(minimal_workflow(steps=[
            {"step_id": "only", "kind": "send_message", "message": "a", "input_ref": "nope"},
        ]))


def test_assert_step_requires_assertions():
    with pytest.raises(ValidationError, match="at least one assertion"):
        WorkflowVersion.model_validate(minimal_workflow(steps=[
            {"step_id": "bare", "kind": "assert"},
        ]))


def test_unknown_step_kind_is_rejected():
    with pytest.raises(ValidationError):
        WorkflowVersion.model_validate(minimal_workflow(steps=[
            {"step_id": "shell", "kind": "run_shell", "command": "rm -rf /"},
        ]))


def test_content_hash_mismatch_is_rejected():
    record = minimal_workflow(content_hash="sha256:" + "0" * 64)
    with pytest.raises(WorkflowResolutionError, match="content_hash"):
        compile_workflow(record)


def test_draft_lifecycle_is_rejected():
    with pytest.raises(ValidationError, match="publish immediately"):
        WorkflowVersion.model_validate(minimal_workflow(lifecycle="draft"))


def test_content_hash_ignores_lifecycle_and_timestamp_but_tracks_content():
    first = WorkflowVersion.model_validate(minimal_workflow())
    second = WorkflowVersion.model_validate(
        minimal_workflow(published_at="2027-01-01T00:00:00Z")
    )
    assert workflow_content_hash(first) == workflow_content_hash(second)
    third = WorkflowVersion.model_validate(minimal_workflow(
        steps=[
            {"step_id": "request", "kind": "send_message", "message": "different"},
            checkpoint("before-confirm"),
        ],
    ))
    assert workflow_content_hash(first) != workflow_content_hash(third)


# --------------------------------------------------------------- 反例：受限条件


@pytest.mark.parametrize("condition", [
    {"op": "eq", "path": "__class__.__mro__", "value": 1},
    {"op": "eq", "path": "state.__dict__.get", "value": 1},
    {"op": "eq", "path": "state.order[0]", "value": 1},
    {"op": "eq", "path": "state.order.status()", "value": 1},
    {"op": "eq", "path": "state.0", "value": 1},
])
def test_forbidden_condition_paths_are_rejected(condition):
    with pytest.raises(ValidationError):
        Condition.model_validate(condition)


@pytest.mark.parametrize("raw", [
    {"op": "eq", "path": "env.SECRET", "value": 1},
    {"op": "eq", "path": "os.system", "value": "ls"},
    {"op": "exists", "path": "importlib.import_module"},
    {"op": "eq", "path": "request.body", "value": 1},
])
def test_condition_roots_are_enforced_at_compile_time(raw):
    # 这些路径的段名合法（契约层只做词法校验），但根不在许可集合里：
    # 编译期必须拒绝，绝不静默当作 false。
    compiled = None
    with pytest.raises(ConditionError, match="root"):
        compiled = compile_condition(Condition.model_validate(raw))
    assert compiled is None


def test_condition_depth_bomb_is_rejected():
    condition = {"op": "exists", "path": "state.leaf"}
    for _ in range(30):
        condition = {"op": "not", "conditions": [condition]}
    with pytest.raises(ConditionError, match="depth"):
        compile_condition(condition)


def test_condition_node_bomb_is_rejected():
    condition = {
        "op": "and",
        "conditions": [{"op": "exists", "path": f"state.field_{index}"}
                       for index in range(MAX_CONDITION_NODES + 5)],
    }
    with pytest.raises(ConditionError, match="node count"):
        compile_condition(condition)


def test_malformed_condition_shapes_are_rejected():
    for raw in (
        {"op": "eq", "value": 1},
        {"op": "and", "conditions": [{"op": "exists", "path": "state.a"}]},
        {"op": "not", "conditions": []},
        {"op": "not", "conditions": [{"op": "exists", "path": "state.a"},
                                     {"op": "exists", "path": "state.b"}]},
        {"op": "in", "path": "state.a", "value": "not-a-list"},
        {"op": "exists", "path": "state.a", "value": 1},
        {"op": "eval", "path": "state.a"},
    ):
        with pytest.raises(ValidationError):
            Condition.model_validate(raw)


def test_missing_runtime_field_is_an_error_not_false():
    compiled = compile_condition({"op": "eq", "path": "state.order.status", "value": "active"})
    with pytest.raises(ConditionError, match="absent"):
        evaluate_condition(compiled, {"state": {"order": {}}})
    # exists 是唯一把缺失当作否定的操作符。
    exists = compile_condition({"op": "exists", "path": "state.order.status"})
    outcome = evaluate_condition(exists, {"state": {"order": {}}})
    assert outcome.satisfied is False
    satisfied = evaluate_condition(compiled, {"state": {"order": {"status": "active"}}})
    assert satisfied.satisfied is True


def test_condition_evaluation_never_treats_bool_as_int():
    compiled = compile_condition({"op": "eq", "path": "state.flag", "value": 1})
    assert evaluate_condition(compiled, {"state": {"flag": True}}).satisfied is False
    assert evaluate_condition(compiled, {"state": {"flag": 1}}).satisfied is True


def test_condition_never_indexes_sequences_or_attributes():
    # 列表/字符串不是可索引容器：走到序列上再取子字段是明确错误，不是 None。
    with pytest.raises(ValidationError, match="plain identifiers"):
        Condition.model_validate({"op": "eq", "path": "state.items.0", "value": 1})
    compiled = compile_condition({"op": "eq", "path": "state.items.first", "value": 1})
    with pytest.raises(ConditionError, match="absent"):
        evaluate_condition(compiled, {"state": {"items": ["a"]}})


def test_condition_in_operator_requires_type_match():
    compiled = compile_condition({"op": "in", "path": "state.status", "value": ["a", "b"]})
    assert evaluate_condition(compiled, {"state": {"status": "a"}}).satisfied is True
    assert evaluate_condition(compiled, {"state": {"status": "c"}}).satisfied is False
    assert evaluate_condition(compiled, {"state": {"status": 1}}).satisfied is False


# --------------------------------------------------------------- 反例：资源解析


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def get(self, *key):
        return self._rows.get(tuple(key))


class _FakeResources:
    def __init__(self, workflows=None, fixtures=None):
        self.workflows = _FakeStore(workflows or {})
        if fixtures is not None:
            self.fixtures = _FakeStore(fixtures)


def test_resolve_ref_rejects_missing_and_malformed_references():
    resources = _FakeResources()
    with pytest.raises(WorkflowResolutionError, match="name@version"):
        resolve_workflow_ref("order-cancel-confirmed", resources)
    with pytest.raises(WorkflowResolutionError, match="not found"):
        resolve_workflow_ref("order-cancel-confirmed@1", resources)


def test_resolve_ref_rejects_draft_records_in_the_store():
    record = minimal_workflow()
    record["lifecycle"] = "draft"
    resources = _FakeResources(workflows={("order-cancel-confirmed", "1"): record})
    with pytest.raises(WorkflowResolutionError):
        resolve_workflow_ref("order-cancel-confirmed@1", resources)


def test_resolve_ref_rejects_unpublished_fixture_reference():
    record = minimal_workflow(fixture_refs=[
        {"fixture_id": "order-state", "version": 1, "kind": "json"},
    ])
    resources = _FakeResources(
        workflows={("order-cancel-confirmed", "1"): record}, fixtures={},
    )
    with pytest.raises(WorkflowResolutionError, match="unpublished fixture"):
        resolve_workflow_ref("order-cancel-confirmed@1", resources)


def test_resolve_ref_accepts_published_fixture_and_checks_hash_and_kind():
    record = minimal_workflow(fixture_refs=[
        {"fixture_id": "order-state", "version": 1, "kind": "json",
         "content_hash": "sha256:" + "a" * 64},
    ])
    resources = _FakeResources(
        workflows={("order-cancel-confirmed", "1"): record},
        fixtures={("order-state", "1"): {
            "fixture_id": "order-state", "version": "1", "kind": "json",
            "content_hash": "sha256:" + "a" * 64,
        }},
    )
    compiled = resolve_workflow_ref("order-cancel-confirmed@1", resources)
    assert compiled.content_hash.startswith("sha256:")

    drifted = _FakeResources(
        workflows={("order-cancel-confirmed", "1"): record},
        fixtures={("order-state", "1"): {
            "fixture_id": "order-state", "version": "1", "kind": "json",
            "content_hash": "sha256:" + "b" * 64,
        }},
    )
    with pytest.raises(WorkflowResolutionError, match="hash"):
        resolve_workflow_ref("order-cancel-confirmed@1", drifted)

    wrong_kind = _FakeResources(
        workflows={("order-cancel-confirmed", "1"): record},
        fixtures={("order-state", "1"): {
            "fixture_id": "order-state", "version": "1", "kind": "sqlite",
            "content_hash": "sha256:" + "a" * 64,
        }},
    )
    with pytest.raises(WorkflowResolutionError, match="kind"):
        resolve_workflow_ref("order-cancel-confirmed@1", wrong_kind)
