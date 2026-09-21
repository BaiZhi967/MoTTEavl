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
from .fixtures import (
    CleanupReport,
    FixtureConflict,
    FixtureError,
    FixtureOwnershipError,
    FixturePolicyError,
    FixturePrepareError,
    FixtureRuntime,
    FixtureUnavailable,
    PostgresTestTarget,
    PostgresTestVerdict,
    ResetTarget,
    load_fixture,
    publish_fixture,
    require_postgres_test_target,
    validate_postgres_test_target,
)
from .state import (
    ControlledResource,
    ControlledRoot,
    FixtureInstance,
    PrivateTruth,
    StateSnapshot,
    is_hidden_field,
    is_link_path,
    minimal_visible_fields,
    owner_token_digest,
    snapshot_state,
    state_content_hash,
)

__version__ = "0.1.0"

__all__ = [
    "CleanupReport",
    "CompiledCondition",
    "CompiledWorkflow",
    "ConditionError",
    "ConditionOutcome",
    "ControlledResource",
    "ControlledRoot",
    "FixtureConflict",
    "FixtureError",
    "FixtureInstance",
    "FixtureOwnershipError",
    "FixturePolicyError",
    "FixturePrepareError",
    "FixtureRuntime",
    "FixtureUnavailable",
    "PostgresTestTarget",
    "PostgresTestVerdict",
    "PrivateTruth",
    "ResetTarget",
    "StateSnapshot",
    "WorkflowResolutionError",
    "compile_condition",
    "compile_workflow",
    "evaluate_assertions",
    "evaluate_condition",
    "is_hidden_field",
    "is_link_path",
    "load_fixture",
    "minimal_visible_fields",
    "owner_token_digest",
    "publish_fixture",
    "require_postgres_test_target",
    "resolve_workflow_ref",
    "snapshot_state",
    "state_content_hash",
    "validate_postgres_test_target",
]
