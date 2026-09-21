"""M5-T01 旧 DSL 兼容转换反例。

证明的是诊断如实、语义对照明确、绝不 eval：不支持的旧字段必须出现在诊断
里，转换不完整时不得产出可发布版本，运行计数恒为 0。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from motte_scenario.conversion import (
    FIELD_MAPPING,
    ConversionIncompleteError,
    convert_legacy_scenario,
)

PUBLISHED_AT = "2026-09-21T00:00:00Z"


def legacy(**overrides):
    payload = {
        "scenario_id": "order-cancel-confirmed",
        "version": "1",
        "given": {
            "initial_state": {"order": {"id": "order-1", "status": "active",
                                        "cancellation_count": 0}},
            "conversation_history": [{"role": "user", "content": "你好"}],
        },
        "steps": [
            {"step_id": "request", "when": {"send_message": "请取消订单 order-1"}},
            {"step_id": "before-confirm",
             "expect": ["state.order.status == 'active'",
                        "state.order.cancellation_count == 0"]},
            {"step_id": "confirm", "when": {"send_message": "我确认取消"}},
            {"step_id": "cancel", "when": {"invoke_tool": {"name": "orders.cancel",
                                                           "arguments": {"order_id": "order-1"}}},
             "expect": ["tool_results.orders.cancel.ok == True"]},
            {"step_id": "final", "expect": ["state.order.status == 'cancelled'",
                                            "state.order.cancellation_count == 1"]},
        ],
        "final_assertions": ["steps.request.completed is not None"],
        "limits": {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 30},
    }
    payload.update(overrides)
    return payload


def codes(result):
    return {item.code for item in result.diagnostics}


def test_clean_conversion_is_publishable_and_preserves_semantics():
    result = convert_legacy_scenario(legacy())
    assert result.publishable is True
    assert result.runs_executed == 0
    workflow = result.to_publishable_workflow(published_at=PUBLISHED_AT)
    assert workflow.workflow_id == "order-cancel-confirmed"
    assert workflow.content_hash is not None and workflow.content_hash == workflow.effective_content_hash()
    assert [step.step_id for step in workflow.steps] == [
        "request", "before-confirm", "confirm", "cancel", "final",
    ]
    assert [step.kind for step in workflow.steps] == [
        "send_message", "assert", "send_message", "invoke_fixture_tool", "assert",
    ]
    # expect 是紧随动作的断言，不是 when 的动作判定。
    assert workflow.steps[3].assertions[0].path == "tool_results.orders.cancel.ok"
    assert workflow.completion_assertions[0].path == "step_results.request.completed"
    # conversation_history 作为固定输入数据进入 fixture，不丢也不外泄。
    assert result.fixture_draft["conversation_history"] == [{"role": "user", "content": "你好"}]
    assert result.fixture_draft["initial_state"]["order"]["status"] == "active"


def test_mapping_table_covers_every_documented_legacy_field():
    legacy_fields = {item["legacy"].split(".")[0].split("[")[0] for item in FIELD_MAPPING}
    assert {"given", "steps", "final_assertions", "limits", "setup", "cleanup",
            "user_simulator", "checker"} <= legacy_fields
    assert all("semantics" in item for item in FIELD_MAPPING)


@pytest.mark.parametrize("field, value", [
    ("setup", ["bash -c 'echo hi'"]),
    ("cleanup", ["rm -rf /tmp/x"]),
    ("user_simulator", {"model": "gpt-4o"}),
    ("checker", "lambda state: True"),
])
def test_refused_legacy_features_block_publication(field, value):
    result = convert_legacy_scenario(legacy(**{field: value}))
    assert result.publishable is False
    assert "LEGACY_FIELD_REFUSED" in codes(result)
    assert field in {item.path for item in result.diagnostics}
    with pytest.raises(ConversionIncompleteError) as error:
        result.to_publishable_workflow(published_at=PUBLISHED_AT)
    assert "LEGACY_FIELD_REFUSED" in str(error.value)
    assert result.runs_executed == 0


def test_unknown_legacy_fields_are_reported_as_warnings():
    result = convert_legacy_scenario(legacy(magic_retries=3))
    assert result.publishable is True
    assert "LEGACY_FIELD_UNKNOWN" in codes(result)
    assert any(item.path == "magic_retries" for item in result.diagnostics)


@pytest.mark.parametrize("expression, expected", [
    ("state.order.status == 'cancelled'", {"op": "eq", "path": "state.order.status",
                                           "value": "cancelled"}),
    ("context.retry != 2", {"op": "ne", "path": "state.retry", "value": 2}),
    ("state.status in ['a', 'b']", {"op": "in", "path": "state.status",
                                    "value": ["a", "b"]}),
    ("state.order is not None", {"op": "exists", "path": "state.order"}),
])
def test_expression_conversion_is_narrow_and_read_only(expression, expected):
    result = convert_legacy_scenario(legacy(steps=[
        {"step_id": "check", "expect": [expression]},
    ]))
    assert result.publishable is True
    workflow = result.to_publishable_workflow(published_at=PUBLISHED_AT)
    condition = workflow.steps[0].assertions[0]
    assert condition.op == expected["op"]
    assert condition.path == expected["path"]
    if "value" in expected:
        actual = list(condition.value) if isinstance(condition.value, tuple) else condition.value
        assert actual == expected["value"]


def test_boolean_composition_is_converted_not_evaluated():
    result = convert_legacy_scenario(legacy(steps=[
        {"step_id": "check",
         "expect": ["state.a == 1 and not (state.b == 2)"]},
    ]))
    assert result.publishable is True
    condition = result.to_publishable_workflow(published_at=PUBLISHED_AT).steps[0].assertions[0]
    assert condition.op == "and"
    assert condition.conditions[1].op == "not"


@pytest.mark.parametrize("expression", [
    "__import__('os').system('ls')",
    "open('/etc/passwd').read() == 'x'",
    "state.items[0] == 1",
    "eval('1+1') == 2",
    "state.value + 1 > 2",
    "f'{state.a}' == 'x'",
    "lambda: True",
])
def test_dangerous_expressions_are_refused_without_eval(expression):
    result = convert_legacy_scenario(legacy(steps=[
        {"step_id": "check", "expect": [expression]},
    ]))
    assert result.publishable is False
    assert codes(result) & {"LEGACY_EXPRESSION_UNSUPPORTED",
                            "LEGACY_EXPRESSION_ROOT_FORBIDDEN"}
    assert result.runs_executed == 0


def test_unknown_expression_root_is_refused():
    result = convert_legacy_scenario(legacy(steps=[
        {"step_id": "check", "expect": ["secrets.API_KEY is not None"]},
    ]))
    assert result.publishable is False
    assert "LEGACY_EXPRESSION_ROOT_FORBIDDEN" in codes(result)


def test_unbounded_loop_is_refused():
    result = convert_legacy_scenario(legacy(steps=[
        {"step_id": "spin", "when": {"loop": {"steps": [
            {"step_id": "inner", "when": {"send_message": "again"}},
        ]}}},
    ]))
    assert result.publishable is False
    assert "LEGACY_LOOP_UNBOUNDED" in codes(result)


def test_bounded_loop_and_branch_keep_order():
    result = convert_legacy_scenario(legacy(steps=[
        {"step_id": "decide", "when": {"branch": {
            "condition": "state.confirmed is not None",
            "then": [{"step_id": "t1", "when": {"send_message": "yes"}}],
            "else": [{"step_id": "e1", "when": {"send_message": "no"}}],
        }}},
        {"step_id": "retry", "when": {"loop": {
            "max_iterations": 3,
            "until": "state.order.status == 'cancelled'",
            "steps": [{"step_id": "r1", "when": {"send_message": "retry"}}],
        }}},
    ]))
    assert result.publishable is True
    workflow = result.to_publishable_workflow(published_at=PUBLISHED_AT)
    assert [step.step_id for step in workflow.steps] == ["decide", "retry"]
    branch = workflow.steps[0]
    assert [step.step_id for step in branch.then_steps] == ["t1"]
    assert [step.step_id for step in branch.else_steps] == ["e1"]
    loop = workflow.steps[1]
    assert loop.max_iterations == 3
    assert loop.until.path == "state.order.status"


def test_missing_limits_block_publication():
    result = convert_legacy_scenario(legacy(limits={"max_total_steps": 20}))
    assert result.publishable is False
    assert codes(result) == {"LEGACY_LIMITS_MISSING"}
    assert {item.path for item in result.diagnostics} == {
        "limits.max_turns", "limits.wall_time_sec",
    }


def test_missing_step_identity_blocks_publication():
    result = convert_legacy_scenario(legacy(steps=[
        {"when": {"send_message": "anonymous"}},
    ]))
    assert result.publishable is False
    assert "LEGACY_STEP_ID_MISSING" in codes(result)


def test_duplicate_nested_step_identity_is_diagnosed():
    result = convert_legacy_scenario(legacy(steps=[
        {"step_id": "dup", "when": {"send_message": "a"}},
        {"step_id": "outer", "when": {"loop": {"max_iterations": 2, "steps": [
            {"step_id": "dup", "when": {"send_message": "b"}},
        ]}}},
    ]))
    assert result.publishable is False
    assert "LEGACY_STEP_ID_DUPLICATE" in codes(result)


def test_step_without_action_or_expectation_is_diagnosed():
    result = convert_legacy_scenario(legacy(steps=[{"step_id": "empty"}]))
    assert result.publishable is False
    assert set(result.blocking_codes) == {"LEGACY_STEP_ACTION_MISSING", "LEGACY_STEPS_EMPTY"}


def test_source_receipt_is_carried_without_executing_anything():
    result = convert_legacy_scenario(legacy(), source={
        "commit": "b661bcdf83e1c3dfb8d6062ee78817d249e86a4c",
        "path": "workers/scenario-runner/src/scenario_runner/engine.py",
        "sha256": "11093949c0e9dee75c52217ac6d1ca147249b64670f78037884e503fd8880a94",
    })
    assert result.source["commit"].startswith("b661bcd")
    assert result.runs_executed == 0
    assert isinstance(result.diagnostic_dicts(), list)


def test_converted_workflow_with_refused_features_fails_contract_validation():
    """即使不做诊断检查，草稿本身的畸形也会被契约拒绝（纵深防御）。"""
    result = convert_legacy_scenario(legacy(steps=[
        {"step_id": "check", "when": {"unknown_action": 1}},
    ]))
    with pytest.raises(ConversionIncompleteError):
        result.to_publishable_workflow(published_at=PUBLISHED_AT)
    with pytest.raises(ValidationError):
        from motte_contracts.workflow import WorkflowVersion

        WorkflowVersion.model_validate({**result.workflow, "steps": []})


def test_publication_timestamp_is_not_invented_by_the_converter():
    result = convert_legacy_scenario(legacy())
    assert result.publishable is True
    with pytest.raises(ConversionIncompleteError, match="LEGACY_PUBLISHED_AT_REQUIRED"):
        result.to_publishable_workflow()
    workflow = result.to_publishable_workflow(published_at=PUBLISHED_AT)
    assert workflow.published_at == PUBLISHED_AT
