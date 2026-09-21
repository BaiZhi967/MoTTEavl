"""旧 Scenario DSL 的只读兼容转换（M5-T01）。

本模块**只读解析**旧结构并输出新的 Workflow 草稿、字段映射与诊断；它不导入
也不执行旧代码。

旧引擎的固定来源（交接记录）：BaiZhi967/llm_agent__evaluation_platform
commit b661bcdf83e1c3dfb8d6062ee78817d249e86a4c，
workers/scenario-runner/src/scenario_runner/engine.py。该文件在本机不存在，
因此本转换器按**已文档化的字段与语义**实现（见
docs/migration/workflow-parity.md 的对照矩阵），不复制其中不可搬运的实现：

* 旧引擎用 eval(expression) 求值条件——本模块改用 ast 只读解析 + 白名单映射；
  Python 表达式语法之外的任何形式都产生诊断，绝不送进 eval。
* 旧引擎用 shell=True 执行 setup/cleanup——本模块明确拒绝并产生诊断。
* 旧引擎在调用返回后才检查 send 超时——新引擎在动作前核验预算并用受控进程
  实际停止（T03）。

转换不完整时 publishable 为 False，且 to_publishable_workflow() 具名拒绝，
不产生“近似可运行”的已发布版本。运行时执行计数恒为 0。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Mapping

from motte_contracts.workflow import WORKFLOW_SCHEMA_VERSION, WorkflowVersion

#: 旧表达式的根标识符 → 新条件根。未列出的根一律诊断拒绝。
LEGACY_ROOT_MAP: dict[str, str] = {
    "state": "state",
    "context": "state",
    "session": "state",
    "result": "state",
    "tool_results": "tool_results",
    "tools": "tool_results",
    "steps": "step_results",
    "step_results": "step_results",
}

#: 旧字段 → 新字段的语义对照（保留/改名/拒绝都写在这里，供迁移文档引用）。
FIELD_MAPPING: tuple[dict[str, str], ...] = (
    {"legacy": "given.fixtures", "workflow": "fixture_refs + fixture_draft", "semantics": "preserved"},
    {"legacy": "given.initial_state", "workflow": "fixture_draft.initial_state", "semantics": "preserved"},
    {"legacy": "given.conversation_history", "workflow": "fixture_draft.conversation_history", "semantics": "preserved"},
    {"legacy": "steps[].when.send_message", "workflow": "steps[].kind=send_message", "semantics": "preserved"},
    {"legacy": "steps[].when.invoke_tool", "workflow": "steps[].kind=invoke_fixture_tool", "semantics": "renamed"},
    {"legacy": "steps[].expect", "workflow": "steps[].assertions", "semantics": "preserved-as-assertion"},
    {"legacy": "steps[].checkpoint", "workflow": "steps[].kind=checkpoint", "semantics": "preserved"},
    {"legacy": "steps[].branch", "workflow": "steps[].kind=branch (when/action)", "semantics": "preserved-order"},
    {"legacy": "steps[].loop", "workflow": "steps[].kind=loop (max_iterations required)", "semantics": "bounded"},
    {"legacy": "final_assertions", "workflow": "completion_assertions", "semantics": "renamed"},
    {"legacy": "limits", "workflow": "limits", "semantics": "preserved"},
    {"legacy": "setup", "workflow": None, "semantics": "refused"},
    {"legacy": "cleanup", "workflow": None, "semantics": "refused"},
    {"legacy": "user_simulator", "workflow": None, "semantics": "refused"},
    {"legacy": "checker", "workflow": None, "semantics": "refused"},
)

_REFUSED_LEGACY_FIELDS = ("setup", "cleanup", "user_simulator", "checker", "on_error", "hooks")


class ConversionIncompleteError(ValueError):
    """转换存在阻断诊断，不得发布。"""

    def __init__(self, diagnostics: Any) -> None:
        items = tuple(
            item.as_dict() if hasattr(item, "as_dict") else dict(item)
            for item in diagnostics
        )
        blocking = sorted({item["code"] for item in items if item["severity"] == "error"})
        super().__init__(
            "legacy conversion is incomplete; refusing to publish an approximate workflow "
            f"({len(blocking)} blocking diagnostic code(s): " + ", ".join(blocking) + ")"
        )
        self.diagnostics = items


@dataclass
class ConversionDiagnostic:
    code: str
    severity: str
    path: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        }


@dataclass
class ConversionResult:
    """转换结果：草稿、诊断、映射表与只读来源收据。"""

    workflow: dict[str, Any]
    fixture_draft: dict[str, Any]
    diagnostics: tuple[ConversionDiagnostic, ...]
    mapping: tuple[dict[str, str], ...] = FIELD_MAPPING
    source: dict[str, Any] = field(default_factory=dict)
    runs_executed: int = 0

    @property
    def publishable(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    @property
    def blocking_codes(self) -> tuple[str, ...]:
        return tuple(sorted({item.code for item in self.diagnostics if item.severity == "error"}))

    def diagnostic_dicts(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.diagnostics]

    def to_publishable_workflow(self, *, published_at: str | None = None) -> WorkflowVersion:
        """只有无阻断诊断时才产出可发布契约；否则具名拒绝。

        发布时间是**发布事件**的元数据，不由转换器发明：调用方显式传入，或
        旧记录已带 published_at。内容 hash 在发布时计算并固定。
        """
        if not self.publishable:
            raise ConversionIncompleteError(self.diagnostics)
        stamp = published_at or self.workflow.get("published_at")
        if not isinstance(stamp, str) or not stamp:
            raise ConversionIncompleteError(self.diagnostics + (ConversionDiagnostic(
                "LEGACY_PUBLISHED_AT_REQUIRED", "error", "published_at",
                "publication requires an explicit published_at timestamp",
            ),))
        record = {**self.workflow, "published_at": stamp}
        try:
            workflow = WorkflowVersion.model_validate(record)
        except ValueError as error:
            raise ConversionIncompleteError(self.diagnostics + (ConversionDiagnostic(
                "LEGACY_CONTRACT_INVALID", "error", "workflow", str(error),
            ),)) from error
        from motte_contracts.workflow import workflow_content_hash

        return WorkflowVersion.model_validate(
            {**record, "content_hash": workflow_content_hash(workflow)}
        )


class _ExpressionRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError) as error:
        raise _ExpressionRefused(
            "LEGACY_EXPRESSION_UNSUPPORTED",
            "only literal comparison values are convertible",
        ) from error


def _path_of(node: ast.AST) -> str:
    segments: list[str] = []
    cursor = node
    while isinstance(cursor, ast.Attribute):
        segments.append(cursor.attr)
        cursor = cursor.value
    if isinstance(cursor, ast.Name):
        segments.append(cursor.id)
    else:
        raise _ExpressionRefused(
            "LEGACY_EXPRESSION_UNSUPPORTED",
            "expression path must be a plain attribute chain",
        )
    segments.reverse()
    root = LEGACY_ROOT_MAP.get(segments[0])
    if root is None:
        raise _ExpressionRefused(
            "LEGACY_EXPRESSION_ROOT_FORBIDDEN",
            f"legacy expression root {segments[0]!r} is not a permitted data path",
        )
    mapped = [root, *segments[1:]]
    if len(mapped) < 2:
        raise _ExpressionRefused(
            "LEGACY_EXPRESSION_UNSUPPORTED",
            "expression must address a field below its root",
        )
    return ".".join(mapped)


def _condition_from_expression(expression: str) -> dict[str, Any]:
    """只读解析旧表达式字符串；语法外的一切都拒绝，绝不 eval。"""
    if not isinstance(expression, str) or not expression.strip():
        raise _ExpressionRefused("LEGACY_EXPRESSION_UNSUPPORTED", "expression must be a non-empty string")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise _ExpressionRefused(
            "LEGACY_EXPRESSION_UNSUPPORTED", f"expression is not parseable: {error.msg}"
        ) from error
    return _condition_from_node(tree.body)


def _condition_from_node(node: ast.AST) -> dict[str, Any]:
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left, op, right = node.left, node.ops[0], node.comparators[0]
        if isinstance(op, ast.Eq):
            return {"op": "eq", "path": _path_of(left), "value": _literal(right)}
        if isinstance(op, ast.NotEq):
            return {"op": "ne", "path": _path_of(left), "value": _literal(right)}
        if isinstance(op, ast.In):
            value = _literal(right)
            if not isinstance(value, (list, tuple)):
                raise _ExpressionRefused(
                    "LEGACY_EXPRESSION_UNSUPPORTED", "operator 'in' requires a literal container"
                )
            return {"op": "in", "path": _path_of(left), "value": list(value)}
        if isinstance(op, (ast.Is, ast.IsNot)) and _literal(right) is None:
            inner = {"op": "exists", "path": _path_of(left)}
            if isinstance(op, ast.Is):
                raise _ExpressionRefused(
                    "LEGACY_EXPRESSION_UNSUPPORTED",
                    "'is None' has no permitted equivalent; convert to not(exists(...)) explicitly",
                )
            return inner
        raise _ExpressionRefused(
            "LEGACY_EXPRESSION_UNSUPPORTED",
            "only ==, !=, in and 'is not None' comparisons are convertible",
        )
    if isinstance(node, ast.Attribute):
        return {"op": "exists", "path": _path_of(node)}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return {"op": "not", "conditions": [_condition_from_node(node.operand)]}
    if isinstance(node, ast.BoolOp):
        op = "and" if isinstance(node.op, ast.And) else "or" if isinstance(node.op, ast.Or) else None
        if op is None:
            raise _ExpressionRefused(
                "LEGACY_EXPRESSION_UNSUPPORTED", "only boolean and/or are convertible"
            )
        return {"op": op, "conditions": [_condition_from_node(item) for item in node.values]}
    raise _ExpressionRefused(
        "LEGACY_EXPRESSION_UNSUPPORTED",
        "expression uses unsupported syntax (calls, subscripts, arithmetic and f-strings are refused)",
    )


def _convert_condition(raw: Any) -> dict[str, Any]:
    """结构化条件原样透传（由 WorkflowVersion 再校验），字符串走只读解析。"""
    if isinstance(raw, Mapping):
        if "op" in raw:
            return dict(raw)
        raise _ExpressionRefused(
            "LEGACY_CONDITION_UNSUPPORTED",
            "structured conditions require an explicit op field",
        )
    if isinstance(raw, str):
        return _condition_from_expression(raw)
    raise _ExpressionRefused(
        "LEGACY_CONDITION_UNSUPPORTED",
        "condition must be a structured object or a convertible expression string",
    )


def convert_legacy_scenario(
    record: Mapping[str, Any],
    *,
    source: Mapping[str, Any] | None = None,
    workflow_id: str | None = None,
    version: str | None = None,
) -> ConversionResult:
    """把旧 DSL 记录转换为 Workflow 草稿 + 诊断 + 映射。"""
    diagnostics: list[ConversionDiagnostic] = []
    if not isinstance(record, Mapping):
        raise TypeError("legacy scenario record must be a mapping")
    fixture_draft: dict[str, Any] = {"kind": "json", "initial_state": {}, "allowed_tools": []}

    def report(code: str, severity: str, path: str, message: str) -> None:
        diagnostics.append(ConversionDiagnostic(code, severity, path, message))

    def convert_condition(raw: Any, path: str) -> dict[str, Any] | None:
        try:
            return _convert_condition(raw)
        except _ExpressionRefused as error:
            report(error.code, "error", path, str(error))
            return None

    for name in _REFUSED_LEGACY_FIELDS:
        if record.get(name):
            report(
                "LEGACY_FIELD_REFUSED",
                "error",
                name,
                f"legacy field {name!r} has no permitted equivalent and is refused",
            )
    for name in sorted(set(record) - _KNOWN_LEGACY_FIELDS):
        report(
            "LEGACY_FIELD_UNKNOWN",
            "warning",
            name,
            f"legacy field {name!r} is not part of the documented DSL and was not converted",
        )

    given = record.get("given")
    if given is not None:
        if not isinstance(given, Mapping):
            report("LEGACY_FIELD_MALFORMED", "error", "given", "given must be an object")
        else:
            for name in sorted(set(given) - {"fixtures", "initial_state", "conversation_history"}):
                report(
                    "LEGACY_FIELD_UNKNOWN",
                    "warning",
                    f"given.{name}",
                    f"legacy field {name!r} is not part of the documented DSL and was not converted",
                )
            fixtures = given.get("fixtures")
            if fixtures is not None:
                if isinstance(fixtures, Mapping):
                    fixture_draft.update({str(k): v for k, v in fixtures.items()})
                else:
                    report("LEGACY_FIELD_MALFORMED", "error", "given.fixtures", "fixtures must be an object")
            initial_state = given.get("initial_state")
            if initial_state is not None:
                if isinstance(initial_state, Mapping):
                    fixture_draft["initial_state"] = dict(initial_state)
                else:
                    report(
                        "LEGACY_FIELD_MALFORMED",
                        "error",
                        "given.initial_state",
                        "initial_state must be an object",
                    )
            history = given.get("conversation_history")
            if history is not None:
                if isinstance(history, list) and all(isinstance(item, Mapping) for item in history):
                    fixture_draft["conversation_history"] = [dict(item) for item in history]
                else:
                    report(
                        "LEGACY_FIELD_MALFORMED",
                        "error",
                        "given.conversation_history",
                        "conversation_history must be a list of message objects",
                    )

    identifiers: set[str] = set()

    def convert_steps(raw_steps: Any, path: str) -> list[dict[str, Any]]:
        if raw_steps is None:
            return []
        if not isinstance(raw_steps, list):
            report("LEGACY_FIELD_MALFORMED", "error", path, "steps must be a list")
            return []
        converted: list[dict[str, Any]] = []
        for index, item in enumerate(raw_steps):
            where = f"{path}[{index}]"
            if not isinstance(item, Mapping):
                report("LEGACY_FIELD_MALFORMED", "error", where, "step must be an object")
                continue
            step = convert_step(item, where)
            if step is not None:
                converted.append(step)
        return converted

    def step_identifier(item: Mapping[str, Any], where: str) -> str:
        raw = item.get("step_id") or item.get("id") or item.get("name")
        if not isinstance(raw, str) or not raw:
            report(
                "LEGACY_STEP_ID_MISSING",
                "error",
                where,
                "legacy steps require step_id/id/name to preserve stable identity",
            )
            return ""
        if raw in identifiers:
            report(
                "LEGACY_STEP_ID_DUPLICATE",
                "error",
                where,
                f"duplicate step identifier {raw!r}; nested step ids must be globally unique",
            )
        identifiers.add(raw)
        return raw

    def convert_step(item: Mapping[str, Any], where: str) -> dict[str, Any] | None:
        step_id = step_identifier(item, where)
        if not step_id:
            return None
        timeout = item.get("timeout") or item.get("timeout_sec")
        base: dict[str, Any] = {"step_id": step_id}
        if timeout is not None:
            base["timeout_sec"] = timeout
        failure_policy = item.get("failure_policy")
        if failure_policy is not None:
            base["failure_policy"] = failure_policy
        when = item.get("when")
        expect = item.get("expect")
        assertions: list[dict[str, Any]] = []
        if expect is not None:
            raw_assertions = expect if isinstance(expect, list) else [expect]
            for index, raw in enumerate(raw_assertions):
                converted = convert_condition(raw, f"{where}.expect[{index}]")
                if converted is not None:
                    assertions.append(converted)
        base["assertions"] = assertions

        if item.get("step_kind") == "checkpoint" or (when is None and "checkpoint" in item):
            base["kind"] = "checkpoint"
            if isinstance(item.get("checkpoint"), Mapping):
                label = item["checkpoint"].get("label")
                if isinstance(label, str):
                    base["label"] = label
            return base
        if when is None:
            if assertions:
                base["kind"] = "assert"
                return base
            report(
                "LEGACY_STEP_ACTION_MISSING",
                "error",
                where,
                "legacy step declares neither when (action) nor expect (assertion)",
            )
            return None
        if not isinstance(when, Mapping):
            report("LEGACY_FIELD_MALFORMED", "error", f"{where}.when", "when must be an object")
            return None

        if "send_message" in when or "message" in when:
            message = when.get("send_message", when.get("message"))
            if not isinstance(message, str) or not message:
                report(
                    "LEGACY_FIELD_MALFORMED",
                    "error",
                    f"{where}.when",
                    "send_message requires a non-empty message",
                )
                return None
            base.update({"kind": "send_message", "message": message})
            if isinstance(when.get("input_ref"), str):
                base["input_ref"] = when["input_ref"]
            return base
        if "invoke_tool" in when:
            invoke = when["invoke_tool"]
            if not isinstance(invoke, Mapping):
                report(
                    "LEGACY_FIELD_MALFORMED",
                    "error",
                    f"{where}.when.invoke_tool",
                    "invoke_tool must be an object with name/arguments",
                )
                return None
            name = invoke.get("name") or invoke.get("tool")
            if not isinstance(name, str) or not name:
                report(
                    "LEGACY_FIELD_MALFORMED",
                    "error",
                    f"{where}.when.invoke_tool",
                    "invoke_tool requires a tool name",
                )
                return None
            arguments = invoke.get("arguments") or {}
            if not isinstance(arguments, Mapping):
                report(
                    "LEGACY_FIELD_MALFORMED",
                    "error",
                    f"{where}.when.invoke_tool.arguments",
                    "arguments must be an object",
                )
                return None
            base.update({
                "kind": "invoke_fixture_tool",
                "tool": name,
                "arguments": dict(arguments),
                "tool_mode": invoke.get("mode", "real"),
            })
            return base
        if "branch" in when:
            branch = when["branch"]
            if not isinstance(branch, Mapping):
                report("LEGACY_FIELD_MALFORMED", "error", f"{where}.when.branch", "branch must be an object")
                return None
            condition = convert_condition(branch.get("condition"), f"{where}.when.branch.condition")
            if condition is None:
                return None
            then_steps = convert_steps(branch.get("then", branch.get("steps")), f"{where}.then")
            else_steps = convert_steps(branch.get("else"), f"{where}.else")
            base.update({"kind": "branch", "when": condition, "then_steps": then_steps, "else_steps": else_steps})
            return base
        if "loop" in when:
            loop = when["loop"]
            if not isinstance(loop, Mapping):
                report("LEGACY_FIELD_MALFORMED", "error", f"{where}.when.loop", "loop must be an object")
                return None
            iterations = loop.get("max_iterations")
            if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < 1:
                report(
                    "LEGACY_LOOP_UNBOUNDED",
                    "error",
                    f"{where}.when.loop.max_iterations",
                    "legacy loop without a positive max_iterations has no bounded equivalent",
                )
                return None
            until = loop.get("until")
            converted_until = None
            if until is not None:
                converted_until = convert_condition(until, f"{where}.loop.until")
                if converted_until is None:
                    return None
            body = convert_steps(loop.get("steps", loop.get("body")), f"{where}.loop")
            base.update({
                "kind": "loop",
                "max_iterations": iterations,
                "body": body,
                **({"until": converted_until} if converted_until is not None else {}),
            })
            return base
        if "fixture_event" in when or "trigger_event" in when:
            event = when.get("fixture_event", when.get("trigger_event"))
            if isinstance(event, str) and event:
                base.update({"kind": "trigger_fixture_event", "event": event})
                payload = when.get("payload")
                if isinstance(payload, Mapping):
                    base["payload"] = dict(payload)
                return base
            report(
                "LEGACY_FIELD_MALFORMED",
                "error",
                f"{where}.when",
                "fixture event requires a non-empty event name",
            )
            return None
        report(
            "LEGACY_ACTION_UNSUPPORTED",
            "error",
            f"{where}.when",
            "when must declare send_message, invoke_tool, branch, loop or fixture_event",
        )
        return None

    steps = convert_steps(record.get("steps"), "steps")
    completion: list[dict[str, Any]] = []
    raw_final = record.get("final_assertions", record.get("completion_assertions"))
    if raw_final is not None:
        raw_items = raw_final if isinstance(raw_final, list) else [raw_final]
        for index, raw in enumerate(raw_items):
            converted = convert_condition(raw, f"final_assertions[{index}]")
            if converted is not None:
                completion.append(converted)

    limits = record.get("limits")
    if not isinstance(limits, Mapping):
        report(
            "LEGACY_LIMITS_MISSING",
            "error",
            "limits",
            "workflow requires explicit max_total_steps/max_turns/wall_time_sec limits",
        )
        limits_payload: dict[str, Any] = {}
    else:
        limits_payload = {}
        for field_name in ("max_total_steps", "max_turns", "wall_time_sec"):
            if field_name in limits:
                limits_payload[field_name] = limits[field_name]
            else:
                report(
                    "LEGACY_LIMITS_MISSING",
                    "error",
                    f"limits.{field_name}",
                    f"workflow requires an explicit {field_name} bound",
                )

    resolved_id = workflow_id or str(record.get("workflow_id") or record.get("scenario_id") or "")
    resolved_version = str(version or record.get("version") or "")
    workstation: dict[str, Any] = {
        "workflow_id": resolved_id,
        "version": resolved_version,
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "fixture_refs": [
            {
                "fixture_id": resolved_id or "legacy-fixture",
                "version": 1,
                "kind": "json",
            }
        ] if resolved_id else [],
        "steps": steps,
        "completion_assertions": completion,
        "limits": limits_payload,
    }
    description = record.get("description")
    if isinstance(description, str) and description:
        workstation["description"] = description
    failure_policy = record.get("failure_policy")
    if failure_policy is not None:
        workstation["failure_policy"] = failure_policy
    if not steps:
        report("LEGACY_STEPS_EMPTY", "error", "steps", "legacy scenario declares no convertible steps")

    return ConversionResult(
        workflow=workstation,
        fixture_draft=fixture_draft,
        diagnostics=tuple(diagnostics),
        source=dict(source or {}),
    )


_KNOWN_LEGACY_FIELDS = frozenset({
    "scenario_id", "workflow_id", "version", "description", "given", "steps",
    "final_assertions", "completion_assertions", "limits", "failure_policy",
    *_REFUSED_LEGACY_FIELDS,
})
