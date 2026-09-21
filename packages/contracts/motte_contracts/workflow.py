"""M5-T01：版本化 Workflow DSL 契约。

一个 WorkflowVersion 描述 Scenario 如何**逐步驱动**一个 Target：步骤序列、
受限条件、Fixture 引用、Target 能力要求与全局预算。它不是第二份 Scenario
身份，也不新建调度器——ScenarioVersion 引用 WorkflowVersion，Run 仍走既有
RunDispatcher/CaseAttempt 链路。

设计约束（与 M5 执行计划第 4 节一致）：

* 七类步骤齐备且结构受控：send_message / invoke_fixture_tool / assert /
  checkpoint / branch / loop / trigger_fixture_event。branch/loop 的子步骤
  使用同一 union。
* 所有嵌套 step_id 全局唯一；嵌套深度与节点总数有固定上限。
* loop 必须声明 max_iterations；整个 Workflow 必须有正有限的
  max_total_steps / max_turns / wall_time_sec。
* 条件只支持许可数据路径上的 eq/ne/in/exists/and/or/not；不执行 eval、
  import、属性调用或 shell。深度与节点数在编译期校验。
* when 保留**动作**语义（branch 的判定条件），expect 是紧随动作的断言，
  两者不得互相误译。
"""
from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal, Union

from pydantic import Field, field_validator, model_validator

from .identity import canonical_sha256
from .messages import Contract

WORKFLOW_SCHEMA_VERSION = 1

STEP_KINDS: tuple[str, ...] = (
    "send_message",
    "invoke_fixture_tool",
    "assert",
    "checkpoint",
    "branch",
    "loop",
    "trigger_fixture_event",
)

CONDITION_OPS: tuple[str, ...] = ("eq", "ne", "in", "exists", "and", "or", "not")
#: 条件只能读取这些根字段；输入数据不是状态真值。
CONDITION_ROOTS: tuple[str, ...] = ("state", "tool_results", "step_results")

MAX_CONDITION_DEPTH = 8
MAX_CONDITION_NODES = 64
MAX_STEP_NESTING_DEPTH = 4
MAX_WORKFLOW_STEPS = 256
MAX_LOOP_ITERATIONS = 64
WALL_TIME_CEILING_SEC = 3600.0
MAX_TURNS_CEILING = 128
MAX_TOTAL_STEPS_CEILING = 2048

FAILURE_POLICIES: tuple[str, ...] = ("stop_case", "continue_for_evidence")
FIXTURE_KINDS: tuple[str, ...] = ("json", "files", "sqlite")
TOOL_MODES: tuple[str, ...] = ("real", "mock", "replay", "deny")
WORKFLOW_LIFECYCLES: tuple[str, ...] = ("draft", "published", "deprecated")
TARGET_EVIDENCE_KINDS: tuple[str, ...] = ("events", "invocations", "artifacts", "usage")

#: 单个路径段：字母开头，允许数字/下划线/连字符；禁止 dunder 与索引语法。
_PATH_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_REFERENCE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")


def _reject_bool(value: Any) -> Any:
    """bool 是 int 的子类；数值预算字段必须显式拒绝 True/False。"""
    if isinstance(value, bool):
        raise ValueError("budget/limit values must be numbers, not booleans")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("budget/limit values must be finite")
    return value


class Condition(Contract):
    """结构化条件节点。

    eq/ne 比较 path 与 value；in 要求 value 是非空标量列表；exists 只判断
    路径是否存在（value 必须缺省）；and/or 需要至少两个子条件；not 需要恰好
    一个子条件。字段组合在契约层校验，路径白名单、深度与节点数在编译期
    （motte_scenario.conditions）校验。
    """

    op: Literal["eq", "ne", "in", "exists", "and", "or", "not"]
    path: str | None = None
    value: Any = None
    conditions: tuple[Condition, ...] = ()

    @field_validator("path")
    @classmethod
    def _path_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("condition path must be a non-empty string")
        segments = value.split(".")
        if any(_PATH_SEGMENT.match(segment) is None for segment in segments):
            raise ValueError(
                "condition path segments must be plain identifiers: " + repr(value)
            )
        if value.startswith("_"):
            raise ValueError("condition path must not start with an underscore")
        return value

    @model_validator(mode="after")
    def _op_shape(self) -> Condition:
        if self.op in ("eq", "ne", "in"):
            if self.path is None:
                raise ValueError("condition op " + repr(self.op) + " requires a path")
            if self.conditions:
                raise ValueError("condition op " + repr(self.op) + " takes no nested conditions")
            if self.op == "in":
                if not isinstance(self.value, (list, tuple)):
                    raise ValueError("condition op 'in' requires a list value")
                if not self.value:
                    raise ValueError("condition op 'in' requires a non-empty list value")
                if any(isinstance(item, (dict, list, tuple)) for item in self.value):
                    raise ValueError("condition op 'in' accepts scalar values only")
            elif self.value is not None and isinstance(self.value, (dict, list, tuple)):
                raise ValueError("condition op " + repr(self.op) + " accepts scalar values only")
        elif self.op == "exists":
            if self.path is None:
                raise ValueError("condition op 'exists' requires a path")
            if self.conditions or self.value is not None:
                raise ValueError("condition op 'exists' takes no value or nested conditions")
        elif self.op in ("and", "or"):
            if self.path is not None or self.value is not None:
                raise ValueError("condition op " + repr(self.op) + " takes no path or value")
            if len(self.conditions) < 2:
                raise ValueError("condition op " + repr(self.op) + " requires at least two conditions")
        else:  # not
            if self.path is not None or self.value is not None:
                raise ValueError("condition op 'not' takes no path or value")
            if len(self.conditions) != 1:
                raise ValueError("condition op 'not' requires exactly one condition")
        return self


class WorkflowLimits(Contract):
    """全局预算：正、有限、有上限，且不受 bool 冒充。"""

    max_total_steps: int = Field(strict=True)
    max_turns: int = Field(strict=True)
    wall_time_sec: float

    @field_validator("max_total_steps", "max_turns", mode="before")
    @classmethod
    def _positive_int(cls, value: Any) -> Any:
        _reject_bool(value)
        if not isinstance(value, int) or value <= 0:
            raise ValueError("workflow limits must be positive integers")
        return value

    @field_validator("wall_time_sec", mode="before")
    @classmethod
    def _positive_float(cls, value: Any) -> Any:
        _reject_bool(value)
        if not isinstance(value, (int, float)) or value <= 0 or not math.isfinite(value):
            raise ValueError("workflow wall_time_sec must be a positive finite number")
        return float(value)

    @model_validator(mode="after")
    def _ceilings(self) -> WorkflowLimits:
        if self.max_total_steps > MAX_TOTAL_STEPS_CEILING:
            raise ValueError(
                "max_total_steps exceeds the workflow ceiling " + str(MAX_TOTAL_STEPS_CEILING)
            )
        if self.max_turns > MAX_TURNS_CEILING:
            raise ValueError("max_turns exceeds the workflow ceiling " + str(MAX_TURNS_CEILING))
        if self.wall_time_sec > WALL_TIME_CEILING_SEC:
            raise ValueError(
                "wall_time_sec exceeds the workflow ceiling " + str(WALL_TIME_CEILING_SEC)
            )
        return self


class TargetRequirements(Contract):
    """Workflow 对 Target 的**能力要求**；由创建期 preflight 与目标声明比对。

    没有多轮 send 能力的外部 CLI 不能伪装成可执行本 Workflow：能力不符在
    创建期拒绝，不静默改选其他 Target（M5-G01/G07）。
    """

    multi_turn: bool = True
    min_turns: int = Field(default=1, strict=True)
    required_tools: tuple[str, ...] = ()
    tool_modes: tuple[Literal["real", "mock", "replay", "deny"], ...] = ("real",)
    interrupt: bool = False
    skill_injection: bool = False
    evidence: tuple[Literal["events", "invocations", "artifacts", "usage"], ...] = ("events",)

    @field_validator("min_turns", mode="before")
    @classmethod
    def _positive_turns(cls, value: Any) -> Any:
        _reject_bool(value)
        if not isinstance(value, int) or value < 1:
            raise ValueError("target_requirements.min_turns must be a positive integer")
        return value

    @field_validator("required_tools")
    @classmethod
    def _tool_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if _REFERENCE.match(name) is None:
                raise ValueError(
                    "target_requirements.required_tools entry is not a name: " + repr(name)
                )
        return tuple(dict.fromkeys(value))

    @model_validator(mode="after")
    def _consistent(self) -> TargetRequirements:
        if not self.multi_turn and self.min_turns > 1:
            raise ValueError("min_turns > 1 requires multi_turn")
        unknown = sorted(set(self.tool_modes) - set(TOOL_MODES))
        if unknown:
            raise ValueError("unknown tool modes: " + ",".join(unknown))
        return self


class FixtureRef(Contract):
    """对已发布 Fixture 的固定引用；content_hash 缺失时在发布期补齐。"""

    fixture_id: str
    version: int = Field(default=1, strict=True)
    kind: Literal["json", "files", "sqlite"]
    content_hash: str | None = None
    allowed_tools: tuple[str, ...] = ()

    @field_validator("fixture_id")
    @classmethod
    def _fixture_id(cls, value: str) -> str:
        if _REFERENCE.match(value) is None:
            raise ValueError("fixture_id is not a valid resource name: " + repr(value))
        return value

    @field_validator("version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> Any:
        _reject_bool(value)
        if not isinstance(value, int) or value < 1:
            raise ValueError("fixture version must be a positive integer")
        return value

    @field_validator("content_hash")
    @classmethod
    def _hash_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("sha256:") or len(value) != 71:
            raise ValueError("fixture content_hash must be a canonical sha256 digest")
        return value


class _StepBase(Contract):
    step_id: str
    kind: str
    timeout_sec: float | None = None
    assertions: tuple[Condition, ...] = ()
    failure_policy: Literal["stop_case", "continue_for_evidence"] = "stop_case"

    @field_validator("step_id")
    @classmethod
    def _step_id(cls, value: str) -> str:
        if _REFERENCE.match(value) is None:
            raise ValueError("step_id is not a valid identifier: " + repr(value))
        return value

    @field_validator("timeout_sec", mode="before")
    @classmethod
    def _timeout(cls, value: Any) -> Any:
        if value is None:
            return None
        _reject_bool(value)
        if not isinstance(value, (int, float)) or value <= 0 or not math.isfinite(value):
            raise ValueError("step timeout_sec must be a positive finite number")
        return float(value)


class SendMessageStep(_StepBase):
    """向 Target 发一轮业务消息；正常 final answer 只结束**本 turn**。"""

    kind: Literal["send_message"]
    message: str
    input_ref: str | None = None

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("send_message requires a non-empty message")
        return value


class InvokeFixtureToolStep(_StepBase):
    """平台侧直接调用受控 fixture 工具（不经过模型）。"""

    kind: Literal["invoke_fixture_tool"]
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    tool_mode: Literal["real", "mock", "replay", "deny"] = "real"

    @field_validator("tool")
    @classmethod
    def _tool(cls, value: str) -> str:
        if _REFERENCE.match(value) is None:
            raise ValueError("invoke_fixture_tool.tool is not a valid name: " + repr(value))
        return value


class AssertStep(_StepBase):
    """紧跟动作的断言；不产生副作用。"""

    kind: Literal["assert"]

    @model_validator(mode="after")
    def _requires_assertions(self) -> AssertStep:
        if not self.assertions:
            raise ValueError("assert steps require at least one assertion")
        return self


class CheckpointStep(_StepBase):
    """冻结当前状态快照；checkpoint 是审计点，不是自动续跑授权。"""

    kind: Literal["checkpoint"]
    label: str | None = None


class BranchStep(_StepBase):
    """when 是**动作判定**（选择执行哪棵子树），不是紧随动作的断言。"""

    kind: Literal["branch"]
    when: Condition
    then_steps: tuple[StepSpec, ...]
    else_steps: tuple[StepSpec, ...] = ()

    @model_validator(mode="after")
    def _non_empty(self) -> BranchStep:
        if not self.then_steps and not self.else_steps:
            raise ValueError("branch requires at least one then/else step")
        return self


class LoopStep(_StepBase):
    """有界循环：max_iterations 必填，until 提前收敛。"""

    kind: Literal["loop"]
    max_iterations: int = Field(strict=True)
    body: tuple[StepSpec, ...]
    until: Condition | None = None

    @field_validator("max_iterations", mode="before")
    @classmethod
    def _iterations(cls, value: Any) -> Any:
        _reject_bool(value)
        if not isinstance(value, int) or value < 1:
            raise ValueError("loop max_iterations must be a positive integer")
        if value > MAX_LOOP_ITERATIONS:
            raise ValueError(
                "loop max_iterations exceeds the workflow ceiling " + str(MAX_LOOP_ITERATIONS)
            )
        return value

    @model_validator(mode="after")
    def _non_empty(self) -> LoopStep:
        if not self.body:
            raise ValueError("loop requires at least one body step")
        return self


class TriggerFixtureEventStep(_StepBase):
    """向 fixture 注入一个确定性事件（例如外部确认到达）。"""

    kind: Literal["trigger_fixture_event"]
    event: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event")
    @classmethod
    def _event(cls, value: str) -> str:
        if _REFERENCE.match(value) is None:
            raise ValueError("trigger_fixture_event.event is not a valid name: " + repr(value))
        return value


StepSpec = Annotated[
    Union[
        SendMessageStep,
        InvokeFixtureToolStep,
        AssertStep,
        CheckpointStep,
        BranchStep,
        LoopStep,
        TriggerFixtureEventStep,
    ],
    Field(discriminator="kind"),
]


def _child_steps(step: Any) -> list[Any]:
    if isinstance(step, BranchStep):
        return [*step.then_steps, *step.else_steps]
    if isinstance(step, LoopStep):
        return list(step.body)
    return []


def _iter_steps(steps: Any) -> list[Any]:
    """深度优先展开全部步骤（含 branch/loop 子树），保持声明顺序。"""
    collected: list[Any] = []
    stack = list(steps)[::-1]
    while stack:
        step = stack.pop()
        collected.append(step)
        stack.extend(reversed(_child_steps(step)))
    return collected


def step_nesting_depth(steps: Any, depth: int = 1) -> int:
    deepest = depth
    for step in steps:
        children = _child_steps(step)
        if children:
            deepest = max(deepest, step_nesting_depth(children, depth + 1))
    return deepest


class WorkflowVersion(Contract):
    """已发布的不可变 Workflow 版本资源。

    存储主键为 (workflow_id, version)。lifecycle 只允许 published：草稿不
    进入版本仓库（与 RuntimeVersion 一致），运行期解析额外拒绝非发布记录作为
    纵深防御。
    """

    workflow_id: str
    version: str
    schema_version: int = Field(default=WORKFLOW_SCHEMA_VERSION, strict=True)
    description: str | None = None
    fixture_refs: tuple[FixtureRef, ...] = ()
    target_requirements: TargetRequirements = Field(default_factory=TargetRequirements)
    steps: tuple[StepSpec, ...]
    completion_assertions: tuple[Condition, ...] = ()
    limits: WorkflowLimits
    failure_policy: Literal["stop_case", "continue_for_evidence"] = "stop_case"
    lifecycle: str = "published"
    published_at: str = Field(min_length=1)
    content_hash: str | None = None

    @field_validator("workflow_id")
    @classmethod
    def _workflow_id(cls, value: str) -> str:
        if _REFERENCE.match(value) is None:
            raise ValueError("workflow_id is not a valid resource name: " + repr(value))
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not value or not str(value).strip():
            raise ValueError("workflow version must be a non-empty string")
        return str(value)

    @field_validator("lifecycle")
    @classmethod
    def _published_only(cls, value: str) -> str:
        if value != "published":
            raise ValueError("workflow versions publish immediately and are immutable")
        return value

    @field_validator("content_hash")
    @classmethod
    def _content_hash_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("sha256:") or len(value) != 71:
            raise ValueError("workflow content_hash must be a canonical sha256 digest")
        return value

    @model_validator(mode="after")
    def _structure(self) -> WorkflowVersion:
        if self.schema_version != WORKFLOW_SCHEMA_VERSION:
            raise ValueError(
                "unsupported workflow schema_version "
                + str(self.schema_version)
                + "; expected "
                + str(WORKFLOW_SCHEMA_VERSION)
            )
        if not self.steps:
            raise ValueError("workflow requires at least one step")
        flattened = _iter_steps(self.steps)
        if len(flattened) > MAX_WORKFLOW_STEPS:
            raise ValueError(
                "workflow declares "
                + str(len(flattened))
                + " steps, exceeding "
                + str(MAX_WORKFLOW_STEPS)
            )
        seen: dict[str, int] = {}
        for index, step in enumerate(flattened):
            if step.step_id in seen:
                raise ValueError(
                    "duplicate step_id "
                    + repr(step.step_id)
                    + " (first at index "
                    + str(seen[step.step_id])
                    + ")"
                )
            seen[step.step_id] = index
        depth = step_nesting_depth(self.steps)
        if depth > MAX_STEP_NESTING_DEPTH:
            raise ValueError(
                "step nesting depth "
                + str(depth)
                + " exceeds the workflow ceiling "
                + str(MAX_STEP_NESTING_DEPTH)
            )
        # 引用只能指向**先声明的**步骤，避免前向/自引用造成的隐式环。
        for index, step in enumerate(flattened):
            reference = getattr(step, "input_ref", None)
            if reference is None:
                continue
            if reference not in seen:
                raise ValueError(
                    "step " + repr(step.step_id) + " references unknown step " + repr(reference)
                )
            if seen[reference] >= index:
                raise ValueError(
                    "step "
                    + repr(step.step_id)
                    + " references "
                    + repr(reference)
                    + " before it produced output"
                )
        if len({ref.fixture_id for ref in self.fixture_refs}) != len(self.fixture_refs):
            raise ValueError("fixture_refs must reference distinct fixture ids")
        return self

    def effective_content_hash(self) -> str:
        computed = workflow_content_hash(self)
        if self.content_hash is not None and self.content_hash != computed:
            raise ValueError("workflow content_hash does not match its content")
        return computed


def workflow_content_hash(record: Any) -> str:
    """内容身份：排除存储地址与生命周期元数据。

    draft 与已发布版本对同一内容得到同一 hash，因此草稿编辑不会伪装成新
    版本内容；反过来，任何内容改动都会改变 hash。
    """
    if hasattr(record, "model_dump"):
        record = record.model_dump(mode="json")
    payload = {
        key: value
        for key, value in dict(record).items()
        if key not in {"lifecycle", "published_at", "content_hash"}
    }
    return canonical_sha256(payload)


Condition.model_rebuild()
BranchStep.model_rebuild()
LoopStep.model_rebuild()
WorkflowVersion.model_rebuild()
