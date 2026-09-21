"""Workflow 纯解析器（M5-T01）。

本模块只做**无副作用**的编译：校验已发布 WorkflowVersion、编译全部条件、
固定内容 hash 与版本，拒绝 draft / 缺失 / 越权引用。完整 prepare_run 装配
在 T05 接入；在可执行 backend 注册之前，公开执行必须明确 unavailable，
不得静默改选其他 backend。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from motte_contracts.workflow import (
    BranchStep,
    FixtureRef,
    LoopStep,
    TargetRequirements,
    WorkflowLimits,
    WorkflowVersion,
    workflow_content_hash,
)

from .conditions import CompiledCondition, ConditionError, compile_condition


class WorkflowResolutionError(ValueError):
    """Workflow 引用无法解析为可编译的已发布版本。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _child_steps(step: Any) -> list[Any]:
    if isinstance(step, BranchStep):
        return [*step.then_steps, *step.else_steps]
    if isinstance(step, LoopStep):
        return list(step.body)
    return []


def iter_steps(steps: Any) -> list[Any]:
    """深度优先展开（含 branch/loop 子树），保持声明顺序。"""
    collected: list[Any] = []
    stack = list(steps)[::-1]
    while stack:
        step = stack.pop()
        collected.append(step)
        stack.extend(reversed(_child_steps(step)))
    return collected


def iter_conditions(workflow: WorkflowVersion) -> list[tuple[str, str, Any]]:
    """产出 (step_id, 用途标签, Condition) 三元组，用于一次性编译校验。

    用途标签区分 when（动作判定）与 expect/assert（紧随动作的断言），
    避免二者被互相误译（M5-G02）。
    """
    triples: list[tuple[str, str, Any]] = []
    for index, condition in enumerate(workflow.completion_assertions):
        triples.append(("__completion__", f"completion[{index}]", condition))
    for step in iter_steps(workflow.steps):
        for index, condition in enumerate(getattr(step, "assertions", ())):
            triples.append((step.step_id, f"assertions[{index}]", condition))
        if isinstance(step, BranchStep):
            triples.append((step.step_id, "when", step.when))
        if isinstance(step, LoopStep) and step.until is not None:
            triples.append((step.step_id, "until", step.until))
    return triples


@dataclass(frozen=True)
class CompiledWorkflow:
    """确定性的 Workflow 编译结果；snapshot 是可直接冻结进 Run 的 JSON。"""

    workflow_id: str
    version: str
    ref: str
    schema_version: int
    content_hash: str
    snapshot: dict[str, Any]
    workflow: WorkflowVersion
    steps: tuple[Any, ...]
    limits: WorkflowLimits
    target_requirements: TargetRequirements
    fixture_refs: tuple[FixtureRef, ...]
    conditions: tuple[CompiledCondition, ...]

    def step_by_id(self, step_id: str) -> Any:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(step_id)

    def snapshot_for_run(self) -> dict[str, Any]:
        """进入 Run manifest 的保留快照：固定 ref、hash、版本与能力要求。"""
        return {
            "ref": self.ref,
            "workflow_id": self.workflow_id,
            "version": self.version,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "limits": self.limits.model_dump(mode="json"),
            "target_requirements": self.target_requirements.model_dump(mode="json"),
            "fixture_refs": [ref.model_dump(mode="json") for ref in self.fixture_refs],
            "failure_policy": self.workflow.failure_policy,
            "step_ids": [step.step_id for step in self.steps],
            "snapshot": self.snapshot,
        }


def compile_workflow(
    record: WorkflowVersion | Mapping[str, Any],
    *,
    resources: Any = None,
    allow_draft: bool = False,
) -> CompiledWorkflow:
    """把一条 Workflow 记录编译为固定快照；任何越权/畸形都在此拒绝。"""
    if isinstance(record, WorkflowVersion):
        workflow = record
    elif isinstance(record, Mapping):
        try:
            workflow = WorkflowVersion.model_validate(dict(record))
        except ValueError as error:
            raise WorkflowResolutionError("WORKFLOW_INVALID", str(error)) from error
    else:
        raise WorkflowResolutionError(
            "WORKFLOW_INVALID", "workflow must be a WorkflowVersion or mapping"
        )
    lifecycle = workflow.lifecycle
    if lifecycle != "published" and not allow_draft:
        raise WorkflowResolutionError(
            "WORKFLOW_NOT_PUBLISHED",
            f"workflow {workflow.workflow_id}@{workflow.version} is {lifecycle!r}; "
            "only published versions may execute",
        )
    content_hash = workflow_content_hash(workflow)
    if workflow.content_hash is not None and workflow.content_hash != content_hash:
        raise WorkflowResolutionError(
            "WORKFLOW_CONTENT_HASH_MISMATCH",
            f"workflow {workflow.workflow_id}@{workflow.version} content_hash does not match content",
        )
    try:
        compiled_conditions = tuple(
            compile_condition(condition) for _, _, condition in iter_conditions(workflow)
        )
    except ConditionError as error:
        raise WorkflowResolutionError(error.code, str(error)) from error
    steps = tuple(iter_steps(workflow.steps))
    compiled = CompiledWorkflow(
        workflow_id=workflow.workflow_id,
        version=workflow.version,
        ref=f"{workflow.workflow_id}@{workflow.version}",
        schema_version=workflow.schema_version,
        content_hash=content_hash,
        snapshot=workflow.model_dump(mode="json"),
        workflow=workflow,
        steps=steps,
        limits=workflow.limits,
        target_requirements=workflow.target_requirements,
        fixture_refs=workflow.fixture_refs,
        conditions=compiled_conditions,
    )
    if resources is not None:
        _verify_fixture_refs(compiled, resources)
    return compiled


def _verify_fixture_refs(compiled: CompiledWorkflow, resources: Any) -> None:
    """已发布 Fixture 引用必须命中固定版本与内容 hash（缺失即拒绝）。"""
    store = getattr(resources, "fixtures", None)
    if store is None:
        return
    for ref in compiled.fixture_refs:
        spec = store.get(ref.fixture_id, str(ref.version))
        if spec is None:
            raise WorkflowResolutionError(
                "WORKFLOW_FIXTURE_MISSING",
                f"workflow {compiled.ref} references unpublished fixture "
                f"{ref.fixture_id}@{ref.version}",
            )
        recorded = spec.get("content_hash")
        if ref.content_hash is not None and recorded is not None and ref.content_hash != recorded:
            raise WorkflowResolutionError(
                "WORKFLOW_FIXTURE_HASH_MISMATCH",
                f"workflow {compiled.ref} content hash drift: fixture "
                f"{ref.fixture_id}@{ref.version} is pinned at {ref.content_hash} but the "
                f"store holds {recorded}",
            )
        store_kind = spec.get("kind")
        if store_kind is not None and store_kind != ref.kind:
            raise WorkflowResolutionError(
                "WORKFLOW_FIXTURE_KIND_MISMATCH",
                f"workflow {compiled.ref} expects fixture kind {ref.kind!r} but "
                f"{ref.fixture_id}@{ref.version} is {store_kind!r}",
            )


def resolve_workflow_ref(
    ref: str, resources: Any, *, allow_draft: bool = False,
) -> CompiledWorkflow:
    """把 name@version 引用解析成已发布 Workflow 的编译结果。"""
    if not isinstance(ref, str):
        raise WorkflowResolutionError("WORKFLOW_REF_INVALID", "workflow reference must be a string")
    name, separator, version = ref.rpartition("@")
    if not separator or not name or not version:
        raise WorkflowResolutionError(
            "WORKFLOW_REF_INVALID", f"workflow reference must be name@version: {ref!r}"
        )
    store = getattr(resources, "workflows", None)
    if store is None:
        raise WorkflowResolutionError(
            "WORKFLOW_STORE_MISSING", "workflow resources are not available in this store"
        )
    record = store.get(name, version)
    if record is None:
        raise WorkflowResolutionError("WORKFLOW_NOT_FOUND", f"workflow not found: {ref}")
    return compile_workflow(record, resources=resources, allow_draft=allow_draft)
