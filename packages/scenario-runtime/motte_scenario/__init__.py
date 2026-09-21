"""M5 场景执行包：受限条件、Workflow 编译、Fixture 与有界引擎。

本包在第一个有真实实现与测试的工作包（T01）时创建，并登记真实 runtime
依赖（motte-contracts）。它不新建 Run 调度器：Scenario 作为既有
ExecutionBackend 的一种运行形态驱动 Target。
"""
from .compiler import (
    CompiledWorkflow,
    WorkflowResolutionError,
    compile_workflow,
    resolve_workflow_ref,
)
from .conditions import (
    CompiledCondition,
    ConditionError,
    ConditionOutcome,
    compile_condition,
    evaluate_assertions,
    evaluate_condition,
)

__version__ = "0.1.0"

__all__ = [
    "CompiledCondition",
    "CompiledWorkflow",
    "ConditionError",
    "ConditionOutcome",
    "WorkflowResolutionError",
    "compile_condition",
    "compile_workflow",
    "evaluate_assertions",
    "evaluate_condition",
    "resolve_workflow_ref",
]
