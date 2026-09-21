"""scenario@1 执行后端：用 Workflow 步骤驱动一个 Target（M5-T05）。

身份分层（M5 执行计划第 2 节第 1 条）：

* 外层 backend 身份固定为 scenario@1，写进 manifest.execution；
* 内层 target 身份来自 manifest.runtime（例如 pi-agent@1）或内置 Agent，
  单独冻结在 manifest.runtime_snapshot / agent_config 里；
* 二者不得混作同一 backend，也不得借 execution 字段整体放松 M4 的身份校验。

流程步骤**不是**子 Run，也**不是**伪 Trial：一个 CaseAttempt 装配一个
FixtureInstance + 一个 TargetSession + 一个引擎实例，共用既有 service attach
的事件、调用日志、取消与 Artifact 通道。

评分装配（R6）：执行器冻结的 FrozenObservation 是唯一评分输入。本模块把
motte_eval.workflow 已注册的过程指标（state-equals / state-delta /
no-side-effect / goal-achieved / response-policy）接进既有评分入口
（RunService._score_results）：指标配置在创建期从已发布 Scenario 的 evaluator
声明冻结进 manifest.resource_snapshots.workflow_evaluator，评分期只读冻结证据与
它声明的产物，不碰业务工具、不调模型。归属不符或证据缺失只产生
insufficient_evidence，绝不发布 pass。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .execution_backends import (
    ExecutionBackendError,
    ExecutionBackendSpec,
    ExecutionHandle,
    register_backend,
)

SCENARIO_BACKEND_ID = "scenario"
SCENARIO_BACKEND_VERSION = "1"

#: 场景 backend 的能力声明：逐 Case 调用；带副作用，重复执行不安全。
SCENARIO_CAPABILITIES: dict[str, bool] = {
    "interactive": False,
    "safe_to_repeat": False,
    "multi_turn": True,
}

#: 创建期冻结的 workflow evaluator 快照在 manifest 里的位置。
WORKFLOW_EVALUATOR_SNAPSHOT_KEY = "workflow_evaluator"


def target_kind_of(manifest: dict[str, Any]) -> str:
    """解析目标身份；实现只有一份（motte_scenario.target_identity）。"""
    from motte_scenario.target_identity import TargetIdentityError, target_kind_of as _kind

    try:
        return _kind(manifest)
    except TargetIdentityError as error:
        raise ExecutionBackendError(error.code, str(error)) from error


def validate_scenario_manifest(manifest: dict[str, Any]) -> None:
    """创建/分派前的场景配置校验：快照齐备、目标能力满足要求、fixture 有 hash。"""
    snapshot = manifest.get("workflow_snapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        raise ExecutionBackendError(
            "SCENARIO_WORKFLOW_REQUIRED",
            "scenario runs require a resolved manifest.workflow_snapshot",
        )
    target = manifest.get("target_snapshot")
    if not isinstance(target, dict) or not target:
        raise ExecutionBackendError(
            "SCENARIO_TARGET_REQUIREMENTS_REQUIRED",
            "scenario runs require manifest.target_snapshot from the workflow contract",
        )
    steps = snapshot.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ExecutionBackendError(
            "SCENARIO_WORKFLOW_INVALID", "workflow snapshot carries no steps"
        )
    # 先校验冻结输入的形状，再校验目标能力：配置错误应当先报出来，能力不足
    # 是另一类错误（错误码不同，客户端可以分别处理）。
    # fixture 需求只有一个判断来源（motte_scenario.executor.workflow_requires_fixture）：
    # 没有 fixture 的流程（纯消息 + 断言）不该被一条空快照规则挡在创建之外，
    # 而声明了 fixture 引用或 fixture 工具/事件步骤的流程在创建期就必须有固定
    # Fixture——不能先接受、执行时才说缺 primary binding（R6 第 5 条）。
    from motte_scenario.executor import workflow_requires_fixture

    declared_fixtures = snapshot.get("fixture_refs") or []
    fixtures = manifest.get("fixture_snapshot")
    if workflow_requires_fixture(snapshot):
        if not isinstance(fixtures, dict) or not fixtures:
            reason = (
                "scenario runs require pinned fixture snapshots for every declared fixture_ref"
                if declared_fixtures else
                "this workflow declares fixture tool/event steps and needs a pinned fixture"
            )
            raise ExecutionBackendError("SCENARIO_FIXTURE_REQUIRED", reason)
    for key, record in (fixtures or {}).items():
        if not isinstance(record, dict) or not record.get("content_hash"):
            raise ExecutionBackendError(
                "SCENARIO_FIXTURE_INVALID",
                "fixture snapshot " + str(key) + " is missing a pinned content hash",
            )
    kind = target_kind_of(manifest)
    from motte_scenario.targets import (
        TargetCapabilityError,
        require_capabilities,
        target_capabilities,
    )

    try:
        capabilities = target_capabilities(kind, manifest)
        require_capabilities(target, capabilities)
    except TargetCapabilityError as error:
        raise ExecutionBackendError(error.code, str(error)) from error
    if kind == "builtin-agent":
        _validate_builtin_agent_target(manifest)


def _validate_builtin_agent_target(manifest: dict[str, Any]) -> None:
    """内置 Agent 目标的最小配置：模式受支持、模型路径已解析。"""
    from motte_contracts.agent_tasks import AGENT_MODES

    config = manifest.get("agent_config")
    if not isinstance(config, dict):
        raise ExecutionBackendError(
            "SCENARIO_TARGET_CONFIG_REQUIRED",
            "builtin-agent scenario runs require manifest.agent_config",
        )
    mode = config.get("mode")
    if mode not in AGENT_MODES:
        raise ExecutionBackendError(
            "SCENARIO_TARGET_MODE_UNSUPPORTED",
            "agent mode must be one of " + ", ".join(AGENT_MODES) + "; got " + repr(mode),
        )
    if not isinstance(manifest.get("provider"), dict):
        raise ExecutionBackendError(
            "SCENARIO_TARGET_PROVIDER_REQUIRED",
            "builtin-agent scenario runs require a resolved provider snapshot",
        )


# ---------------------------------------------------------------- 工具实现注册表

#: 进程级业务工具实现表：fixture_id -> {handlers/mock_handlers/replay_records/events}。
#: 策略（允许的工具、模式、初态）由已发布且冻结的资源决定；这里只登记"谁来实现"，
#: 与 target adapter 注册表同一层语义。未登记即具名拒绝，绝不回退真实 handler。
_SCENARIO_TOOLS: dict[str, dict[str, dict[str, Any]]] = {}


def register_scenario_tools(
    fixture_id: str,
    *,
    handlers: Mapping[str, Callable[..., Any]] | None = None,
    mock_handlers: Mapping[str, Callable[..., Any]] | None = None,
    replay_records: Mapping[str, Mapping[str, Any]] | None = None,
    event_handlers: Mapping[str, Callable[..., Any]] | None = None,
) -> None:
    """登记一个 fixture 的业务工具实现（real / mock / replay 三种来源各自持有）。"""
    if not isinstance(fixture_id, str) or not fixture_id:
        raise ValueError("fixture_id must be a nonempty string")
    _SCENARIO_TOOLS[fixture_id] = {
        "handlers": dict(handlers or {}),
        "mock_handlers": dict(mock_handlers or {}),
        "replay_records": {
            str(key): dict(value) for key, value in (replay_records or {}).items()
        },
        "event_handlers": dict(event_handlers or {}),
    }


def unregister_scenario_tools(fixture_id: str) -> None:
    _SCENARIO_TOOLS.pop(fixture_id, None)


def registered_scenario_fixtures() -> tuple[str, ...]:
    return tuple(sorted(_SCENARIO_TOOLS))


def _sources_for(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """按冻结的 fixture 快照汇总工具来源；两个 fixture 声明同名工具即具名拒绝。"""
    merged: dict[str, dict[str, Any]] = {
        "handlers": {}, "mock_handlers": {}, "replay_records": {}, "event_handlers": {},
    }
    for key, record in sorted((snapshot or {}).items()):
        fixture_id = str((record or {}).get("fixture_id") or str(key).partition("@")[0])
        source = _SCENARIO_TOOLS.get(fixture_id, {})
        for bucket in ("handlers", "mock_handlers", "event_handlers"):
            for name, implementation in (source.get(bucket) or {}).items():
                existing = merged[bucket].get(name)
                if existing is not None and existing is not implementation:
                    raise ExecutionBackendError(
                        "SCENARIO_TOOL_HANDLER_CONFLICT",
                        "fixture tool " + repr(name) + " is implemented by more than one fixture",
                    )
                merged[bucket][name] = implementation
        for name, record_value in (source.get("replay_records") or {}).items():
            existing = merged["replay_records"].get(name)
            if existing is not None and existing != record_value:
                raise ExecutionBackendError(
                    "SCENARIO_TOOL_HANDLER_CONFLICT",
                    "fixture tool " + repr(name) + " has conflicting frozen replay records",
                )
            merged["replay_records"][name] = dict(record_value)
    return merged


def _build_scenario(run: dict[str, Any]) -> ExecutionHandle:
    from motte_scenario.executor import ScenarioCaseExecutor

    manifest = run.get("manifest") or {}
    sources = _sources_for(manifest.get("fixture_snapshot") or {})
    executor = ScenarioCaseExecutor(
        run,
        tool_handlers=sources["handlers"],
        mock_handlers=sources["mock_handlers"],
        replay_records=sources["replay_records"],
        event_handlers=sources["event_handlers"],
    )

    def attach(service: Any, run_id: str) -> None:
        executor.bind_service(service)

    return ExecutionHandle(
        backend_id=SCENARIO_BACKEND_ID,
        backend_version=SCENARIO_BACKEND_VERSION,
        invoke=executor.invoke,
        capabilities=dict(SCENARIO_CAPABILITIES),
        attach=attach,
    )


# ---------------------------------------------------------------- 终态判定


def stop_unconfirmed(result: Any) -> bool:
    """Case 结果是否报告"停止未确认"（M5 全局约束）。

    执行器把停止确定性冻结在 Case 结果里：``scenario.needs_review``、
    ``scenario.status == "needs_review"`` 或
    ``scenario.interrupt.confirmed is False``。Run 级终态据此升级为
    needs_review（现场保留、不自动重放），但**不改写** Case 证据、被保留的
    fixture 或不足证据的评分行。
    """
    if not isinstance(result, Mapping):
        return False
    scenario = result.get("scenario")
    if not isinstance(scenario, Mapping):
        return False
    if scenario.get("needs_review") is True:
        return True
    if scenario.get("status") == "needs_review":
        return True
    interrupt = scenario.get("interrupt")
    return isinstance(interrupt, Mapping) and interrupt.get("confirmed") is False


# ---------------------------------------------------------------- 评分装配

#: 缺失证据时使用的稳定原因码（只产生 insufficient_evidence，绝不伪造 pass）。
OBSERVATION_MISSING = "observation_missing"
OBSERVATION_INVALID = "observation_invalid"
OBSERVATION_HASH_MISMATCH = "observation_hash_mismatch"
OBSERVATION_RUN_MISMATCH = "observation_run_mismatch"
OBSERVATION_CASE_MISMATCH = "observation_case_mismatch"
FOREIGN_EVIDENCE_REFUSED = "foreign_evidence_refused"


class ScenarioScoringError(RuntimeError):
    """冻结的评分配置本身不可用：宁可让 Run 失败，也不发布未经校验的评分。"""


def workflow_evaluator_snapshot(run: Mapping[str, Any]) -> dict[str, Any] | None:
    """已冻结的 workflow evaluator 快照（创建期生成，运行期不再解析资源）。"""
    manifest = run.get("manifest") or {}
    snapshots = manifest.get("resource_snapshots") if isinstance(manifest, Mapping) else None
    spec = (snapshots or {}).get(WORKFLOW_EVALUATOR_SNAPSHOT_KEY)
    return dict(spec) if isinstance(spec, Mapping) else None


def _artifact_reader() -> Any:
    import os

    from motte_storage.artifacts import ArtifactStore

    return ArtifactStore(os.environ.get("ARTIFACT_ROOT", "var/artifacts")).read_bytes


def _gap_metric(metric: Mapping[str, Any], reason: str) -> Any:
    """证据缺口行：insufficient_evidence，不占分母，不带 passed。"""
    from motte_contracts.evaluation import MetricResult, MetricStatus
    from motte_eval.observation import EVALUATOR_ID, EVALUATOR_VERSION

    return MetricResult(
        metric_id=str(metric["metric_id"]),
        status=MetricStatus.insufficient_evidence,
        evaluator_id=EVALUATOR_ID,
        evaluator_version=EVALUATOR_VERSION,
        reason=reason,
        denominator=False,
    )


def _observation_problem(run: Mapping[str, Any], case_id: str, observation: Any) -> str | None:
    """归属校验：任何一项不符即拒评（零调用、零 pass）。"""
    from motte_contracts.evaluation import FrozenObservation, observation_evidence_hash

    if not isinstance(observation, Mapping):
        return OBSERVATION_MISSING
    try:
        frozen = FrozenObservation.model_validate(observation)
    except Exception:  # noqa: BLE001 - 损坏的观察按缺证据处理
        return OBSERVATION_INVALID
    view = {
        key: value for key, value in observation.items()
        if key not in {"evidence_hash", "recorded_at"}
    }
    if observation_evidence_hash(view) != observation.get("evidence_hash"):
        return OBSERVATION_HASH_MISMATCH
    if frozen.run_id != run.get("id"):
        return OBSERVATION_RUN_MISMATCH
    if frozen.case_id != case_id:
        return OBSERVATION_CASE_MISMATCH
    for ref in frozen.workflow_evidence:
        owner = ref.owner
        if owner.run_id != frozen.run_id or owner.case_id != frozen.case_id:
            return FOREIGN_EVIDENCE_REFUSED
        if owner.attempt_id is not None and frozen.attempt_id is not None and (
            owner.attempt_id != frozen.attempt_id
        ):
            return FOREIGN_EVIDENCE_REFUSED
    return None


def _scoring_metrics(run: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """冻结的指标配置；没有声明 evaluator 的 Run 不产生任何评分行。"""
    spec = workflow_evaluator_snapshot(run)
    if spec is None:
        return None
    from motte_eval import workflow as _workflow  # noqa: F401 - 注册 M5 过程指标
    from motte_eval.observation import EvaluatorConfigError, normalize_evaluator_config

    config = spec.get("config")
    try:
        normalized = normalize_evaluator_config(config)
    except EvaluatorConfigError as error:
        # 创建期已经校验过同一条配置；这里失败说明冻结内容被改写。
        raise ScenarioScoringError(
            "frozen workflow evaluator config is invalid: " + str(error)
        ) from error
    return list(normalized["metrics"])


def scenario_scores(
    run: Mapping[str, Any],
    results: list[dict[str, Any]],
    *,
    artifact_reader: Callable[[str], bytes | None] | None = None,
) -> list[dict[str, Any]]:
    """用既有评分入口对 Scenario Run 的冻结证据评分（零模型、零业务工具调用）。

    输入只有 Run 行与已持久化的 Case 行；每个 Case 的
    result.frozen_observation 是唯一证据来源，rescore 因此天然复用同一份证据。
    """
    from motte_contracts.evaluation import FrozenObservation
    from motte_eval import workflow as _workflow  # noqa: F401 - 注册 M5 过程指标
    from motte_eval.observation import evaluate_observation, metric_result_to_score

    metrics = _scoring_metrics(run)
    if metrics is None:
        return []
    reader = artifact_reader if artifact_reader is not None else _artifact_reader()
    selected = {str(case_id) for case_id in (run.get("case_ids") or [])}
    scores: list[dict[str, Any]] = []
    for row in results:
        case_id = row.get("case_id") if isinstance(row, Mapping) else None
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("scenario scoring requires a case_id on every result row")
        if selected and case_id not in selected:
            raise ValueError("result case_id is not selected: " + repr(case_id))
        result = row.get("result") if isinstance(row, Mapping) else None
        observation = result.get("frozen_observation") if isinstance(result, Mapping) else None
        problem = _observation_problem(run, case_id, observation)
        if problem is not None:
            for metric in metrics:
                scores.append(metric_result_to_score(_gap_metric(metric, problem), case_id))
            continue
        frozen = FrozenObservation.model_validate(observation)
        evaluated = evaluate_observation(frozen, {"metrics": metrics}, artifact_reader=reader)
        for metric in evaluated:
            scores.append(metric_result_to_score(metric, case_id))
    return scores


def install_scenario_backend(*, available: bool | None = None) -> ExecutionBackendSpec:
    """注册/更新 scenario@1。

    available=False 时创建期即明确 unavailable，不静默改选其他 backend
    （M5-T01 完成门：可执行 backend 注册前公开执行必须明确不可用）。
    默认沿用本模块的 SCENARIO_BACKEND_AVAILABLE 事实开关。
    """
    if available is None:
        available = SCENARIO_BACKEND_AVAILABLE
    return register_backend(
        ExecutionBackendSpec(
            id=SCENARIO_BACKEND_ID,
            version=SCENARIO_BACKEND_VERSION,
            validate=validate_scenario_manifest,
            build=_build_scenario,
            capabilities=dict(SCENARIO_CAPABILITIES),
            available=available,
            execution_mode="sample",
        ),
        replace=True,
    )


#: M5 执行状态开关。
#:
#: 公共纵向链路已交付并有行为测试（tests/integration/test_scenario_run_backend.py：
#: 公共 API 创建 → 持久 queued Run → 普通 WorkerLoop 领取 → 报告与评分 →
#: 离线 rescore 复用冻结证据）。创建期仍然拒绝不满足能力的配置
#: （目标未注册 / tool_modes 不满足 / fixture 缺失），绝不静默改选
#: replay/direct-llm；显式 install_scenario_backend(available=False) 仍可用于
#: 关闭新执行。
SCENARIO_BACKEND_AVAILABLE = True

# 内置 Agent 目标 adapter 与 backend 一起安装：目标不可用时创建期就拒绝。
try:
    from .scenario_target import install_builtin_target_adapter as _install_builtin_target

    _install_builtin_target()
except ImportError:  # pragma: no cover - motte-agent 缺失时保持目标不可用
    pass

install_scenario_backend(available=SCENARIO_BACKEND_AVAILABLE)
