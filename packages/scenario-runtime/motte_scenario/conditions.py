"""受限条件的编译与求值（M5-T01 / M5-T03）。

条件不是脚本：它只允许在 state / tool_results / step_results 三个根下的
字典路径上做 eq / ne / in / exists 与有界 and / or / not。编译期固定路径
白名单、深度与节点数上限；运行期缺失字段是**明确错误**，不是 false
（M5-A05）。

禁止：eval、import、属性访问、函数调用、shell 片段、索引表达式。路径段只
允许字母开头的普通标识符，因此 dunder 与下标语法在契约层就被拒绝；本模块
再做一次根白名单校验作为纵深防御。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from motte_contracts.workflow import (
    CONDITION_ROOTS,
    MAX_CONDITION_DEPTH,
    MAX_CONDITION_NODES,
    Condition,
)


class ConditionError(ValueError):
    """条件编译或求值失败；code 供结构化错误映射。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CompiledCondition:
    """已校验的条件节点；path 已切成段元组。"""

    op: str
    path: tuple[str, ...] | None
    value: Any
    children: tuple["CompiledCondition", ...]

    def describe(self) -> str:
        if self.path is not None:
            dotted = ".".join(self.path)
            if self.op == "exists":
                return f"exists({dotted})"
            return f"{dotted} {self.op} {self.value!r}"
        inner = ", ".join(child.describe() for child in self.children)
        return f"{self.op}({inner})"


@dataclass(frozen=True)
class ConditionOutcome:
    """一次断言的结构化结果：满足与否、实际值、可读原因。"""

    condition: CompiledCondition
    satisfied: bool
    actual: Any = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "condition": self.condition.describe(),
            "op": self.condition.op,
            "path": ".".join(self.condition.path) if self.condition.path else None,
            "satisfied": self.satisfied,
            "actual": self.actual,
            "reason": self.reason,
        }


def _coerce(condition: Condition | Mapping[str, Any]) -> Condition:
    if isinstance(condition, Condition):
        return condition
    if isinstance(condition, Mapping):
        return Condition.model_validate(dict(condition))
    raise ConditionError(
        "CONDITION_INVALID",
        "condition must be a Condition or mapping, got " + type(condition).__name__,
    )


def compile_condition(
    condition: Condition | Mapping[str, Any],
    *,
    roots: tuple[str, ...] = CONDITION_ROOTS,
    max_depth: int = MAX_CONDITION_DEPTH,
    max_nodes: int = MAX_CONDITION_NODES,
) -> CompiledCondition:
    """把契约条件编译成可求值节点；越界、越权路径与畸形结构在此拒绝。"""
    model = _coerce(condition)
    nodes = 0

    def build(node: Condition, depth: int) -> CompiledCondition:
        nonlocal nodes
        if depth > max_depth:
            raise ConditionError(
                "CONDITION_DEPTH_EXCEEDED",
                f"condition nesting depth {depth} exceeds the limit {max_depth}",
            )
        nodes += 1
        if nodes > max_nodes:
            raise ConditionError(
                "CONDITION_NODES_EXCEEDED",
                f"condition node count exceeds the limit {max_nodes}",
            )
        path: tuple[str, ...] | None = None
        if node.path is not None:
            path = tuple(node.path.split("."))
            if path[0] not in roots:
                raise ConditionError(
                    "CONDITION_PATH_FORBIDDEN",
                    "condition path root must be one of "
                    + ", ".join(roots)
                    + "; got "
                    + repr(path[0]),
                )
            if len(path) > max_depth:
                raise ConditionError(
                    "CONDITION_DEPTH_EXCEEDED",
                    f"condition path depth {len(path)} exceeds the limit {max_depth}",
                )
        children = tuple(build(child, depth + 1) for child in node.conditions)
        return CompiledCondition(op=node.op, path=path, value=node.value, children=children)

    return build(model, 1)


def _resolve(context: Mapping[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    """只在映射键上行走；列表不是可索引容器（不做下标语义）。"""
    cursor: Any = context
    for segment in path:
        if not isinstance(cursor, Mapping):
            return False, None
        if segment not in cursor:
            return False, None
        cursor = cursor[segment]
    return True, cursor


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _same_scalar(left: Any, right: Any) -> bool:
    """标量相等：bool 不冒充 int（True != 1），类型必须一致。"""
    if not (_is_scalar(left) and _is_scalar(right)):
        return False
    return type(left) is type(right) and left == right


def evaluate_condition(
    compiled: CompiledCondition, context: Mapping[str, Any],
) -> ConditionOutcome:
    """求值一个已编译条件；需要值时缺失字段是明确错误。"""
    op = compiled.op
    if op in ("and", "or"):
        outcomes = [evaluate_condition(child, context) for child in compiled.children]
        satisfied = (
            all(outcome.satisfied for outcome in outcomes)
            if op == "and"
            else any(outcome.satisfied for outcome in outcomes)
        )
        return ConditionOutcome(compiled, satisfied, [o.actual for o in outcomes], "")
    if op == "not":
        inner = evaluate_condition(compiled.children[0], context)
        return ConditionOutcome(compiled, not inner.satisfied, inner.actual, "")

    assert compiled.path is not None  # compile_condition guarantees this
    present, actual = _resolve(context, compiled.path)
    dotted = ".".join(compiled.path)
    if op == "exists":
        return ConditionOutcome(compiled, present, actual if present else None, "")
    if not present:
        raise ConditionError(
            "CONDITION_PATH_MISSING",
            "condition path " + repr(dotted) + " is absent from the evaluation context",
        )
    if op == "eq":
        return ConditionOutcome(compiled, _same_scalar(actual, compiled.value), actual, "")
    if op == "ne":
        return ConditionOutcome(compiled, not _same_scalar(actual, compiled.value), actual, "")
    if op == "in":
        allowed = list(compiled.value)
        satisfied = any(_same_scalar(actual, item) for item in allowed)
        return ConditionOutcome(compiled, satisfied, actual, "allowed=" + repr(allowed))
    raise ConditionError("CONDITION_OP_UNSUPPORTED", "unsupported condition op " + repr(op))


def evaluate_assertions(
    conditions: Any, context: Mapping[str, Any],
) -> list[ConditionOutcome]:
    return [evaluate_condition(compile_condition(item), context) for item in conditions]
