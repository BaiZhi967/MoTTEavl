"""实验编排服务（M6）：ExperimentSpec → Cell → 既有 RunService 的 Run。

Experiment 只做三件事（协议 docs/protocols/experiments-and-comparison.md §1）：

1. ``preview``：零创建的矩阵展开与护栏检查（max_cells / budget）；
2. ``create`` / ``allocate``：按 cell 走存储原语 claim → create_run → complete，
   崩溃窗口由 ``allocate`` 重入恢复（allocating 且无 Run → reset 重领；
   allocating 且 Run 已存在 → 补 complete，不重建）；
3. ``cancel`` / ``retry_cell``：只作用于本实验拥有的 Run——取消走
   ``RunService.cancel``，retry 走 superseding 子 Run（原结果不消失）。

执行主权仍在 RunDispatcher/Worker：本模块不建第二套队列、不直接调 Provider、
不评分、不移动 current 指针。
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import product
from threading import RLock
from typing import Any, Callable

from motte_contracts.experiment import (
    ExperimentCell,
    ExperimentSpec,
    FactorAssignment,
    FactorValue,
    compute_cell_id,
)
from motte_storage.integrity import RunConflictError

#: 本服务能组装 manifest 的套件；其余套件诚实拒绝，不假装支持。
SUPPORTED_SUITES: frozenset[str] = frozenset({"direct-llm", "gsm8k"})
SUPPORTED_FACTORS: dict[str, frozenset[str]] = {
    # Direct's frozen prompt is the dataset case input (verbatim); it has no
    # published prompt/runtime/skill selector to compile into a request.
    "direct-llm": frozenset({"model_profile", "reasoning_level"}),
    "gsm8k": frozenset({"model_profile", "reasoning_level"}),
}

#: Cell → Run 身份前缀：恢复/并发路径必须得到同一个 run_id。
_RUN_ID_PREFIX = "run-exp-"

#: 进度统计展示的 cell 状态（顺序即展示顺序）。
PROGRESS_STATUSES: tuple[str, ...] = (
    "pending",
    "allocating",
    "allocated",
    "failed",
    "cancelled",
)


def deterministic_run_id(cell_id: str) -> str:
    """cell_id → 稳定 run_id：``"run-exp-" + <hex 前 24 位>``。

    cell_id 形如 ``sha256:<hex>``；同 cell 在崩溃恢复、并发领取、幂等重放
    下都派生出同一个 run_id，这是"每 cell 恰好一个 initial Run"的前提。
    """
    if ":" not in cell_id:
        raise ValueError(f"cell_id must look like 'sha256:<hex>', got {cell_id!r}")
    return _RUN_ID_PREFIX + cell_id.split(":", 1)[1][:24]


class ExperimentError(ValueError):
    """实验操作失败（API 层按 code 映射状态码；是 ValueError 便于既有 except 复用）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _default_clock() -> str:
    return datetime.now(UTC).isoformat()


def _expand_matrix(spec: ExperimentSpec) -> list[tuple[FactorAssignment, int]]:
    """矩阵（因素名排序后 itertools.product）× repeats 展开。

    空因素表合法：product 为空时退化为 ``repeats`` 个全默认 cell。
    """
    factor_names = sorted(spec.factors)
    combos = list(product(*(spec.factors[name] for name in factor_names))) or [()]
    expanded: list[tuple[FactorAssignment, int]] = []
    for combo in combos:
        assignment = FactorAssignment(
            values=tuple(
                FactorValue(factor=name, value=value)
                for name, value in zip(factor_names, combo, strict=True)
            )
        )
        for repeat_index in range(spec.repeats):
            expanded.append((assignment, repeat_index))
    return expanded


def _violations(spec: ExperimentSpec, max_calls: int) -> list[dict[str, Any]]:
    """规模护栏（超限整体拒绝，不先排队一部分）。"""
    found: list[dict[str, Any]] = []
    cell_count = spec.cell_count()
    if cell_count > spec.max_cells:
        found.append(
            {
                "code": "MATRIX_TOO_LARGE",
                "message": (f"cell_count {cell_count} exceeds max_cells {spec.max_cells}"),
                "cell_count": cell_count,
                "max_cells": spec.max_cells,
            }
        )
    budget = spec.budget_policy
    if budget.max_total_calls < max_calls:
        found.append(
            {
                "code": "BUDGET_EXCEEDED",
                "message": (
                    f"max_potential_calls {max_calls} exceeds "
                    f"budget max_total_calls {budget.max_total_calls}"
                ),
                "max_potential_calls": max_calls,
                "max_total_calls": budget.max_total_calls,
            }
        )
    unsupported: list[str] = []
    if budget.max_total_tokens is not None:
        unsupported.append("budget_policy.max_total_tokens")
    if budget.max_cost_usd is not None or budget.cost_known_required:
        unsupported.append("budget_policy.max_cost_usd/cost_known_required")
    if spec.stop_policy.on_first_failure or spec.stop_policy.max_failures is not None:
        unsupported.append("stop_policy.failure")
    if spec.stop_policy.wall_clock_seconds is not None:
        unsupported.append("stop_policy.wall_clock_seconds")
    if spec.trials_per_run is not None:
        unsupported.append("trials_per_run for direct-llm")
    if spec.evaluation_ref.scoring != "default" or spec.evaluation_ref.scoring_pass_hint:
        unsupported.append("evaluation_ref.scoring")
    if unsupported:
        found.append({"code": "POLICY_UNSUPPORTED", "message": ", ".join(unsupported)
                      + " has no enforced Direct experiment consumer"})
    return found


def _budget_view(budget: Any) -> dict[str, Any]:
    """预算是上限声明，不是已知费用：缺样本不补 0（协议 §2 缺失语义）。"""
    view: dict[str, Any] = {
        "max_total_calls": budget.max_total_calls,
        "max_total_tokens": budget.max_total_tokens,
        "cost_known": "unknown_until_run",
        "known_cost_usd": None,
        "unknown": True,
    }
    if budget.max_cost_usd is not None:
        view["max_cost_usd"] = budget.max_cost_usd
    return view


#: controlled_conditions 的合法键（fail-closed）：只有能安全进 Run manifest
#: 的条件允许声明；未知条件在 preview/create 即拒绝，不在分配期才爆
#: RESOLVED_MANIFEST_INVALID。
_CONTROLLED_CONDITION_KEYS: frozenset[str] = frozenset({
    "parameters",           # dict：并入 manifest.parameters（如 max_output_tokens）
    "max_output_tokens",    # int：等价 parameters.max_output_tokens 简写
})


def _validate_controlled_conditions(spec: ExperimentSpec) -> None:
    # None 值是显式"不设置"，进 manifest 时跳过，不参与键白名单判定。
    declared = {
        key for key, value in spec.controlled_conditions.items() if value is not None
    }
    unknown = sorted(declared - _CONTROLLED_CONDITION_KEYS)
    if unknown:
        raise ExperimentError(
            "CONTROLLED_CONDITION_INVALID",
            "controlled_conditions keys not consumable by the run manifest: "
            + ",".join(unknown)
            + " (known: " + ",".join(sorted(_CONTROLLED_CONDITION_KEYS)) + ")",
        )


def _validate_supported_suite(spec: ExperimentSpec) -> None:
    suite = spec.task_ref.get("suite")
    if suite not in SUPPORTED_SUITES:
        raise ExperimentError(
            "SUITE_UNSUPPORTED",
            f"task_ref.suite {suite!r} cannot be assembled into a run manifest "
            f"(supported: {','.join(sorted(SUPPORTED_SUITES))})",
        )
    if not spec.task_ref.get("scenario_version"):
        raise ExperimentError(
            "EXPERIMENT_INVALID",
            "task_ref requires a nonempty scenario_version",
        )
    unconsumed = sorted(set(spec.factors) - SUPPORTED_FACTORS[suite])
    if unconsumed:
        raise ExperimentError(
            "FACTOR_UNSUPPORTED",
            f"suite {suite!r} cannot consume factors: {','.join(unconsumed)}",
        )
    if "model_profile" not in spec.factors:
        raise ExperimentError("FACTOR_REQUIRED", "model_profile is required for this suite")
    _validate_controlled_conditions(spec)


def _validate_task_resource(spec: ExperimentSpec, resources: Any) -> None:
    """A missing or mismatched managed scenario must not become an empty generic Run."""
    from motte_sdk.benchmark_plugins import plugin_for_scenario

    name, sep, version = spec.task_ref["scenario_version"].rpartition("@")
    scenario = resources.scenarios.get(name, version) if sep else None
    if scenario is None:
        raise ExperimentError("SCENARIO_NOT_FOUND", spec.task_ref["scenario_version"])
    try:
        identity = plugin_for_scenario(scenario)
    except ValueError as error:
        raise ExperimentError("SCENARIO_INVALID", str(error)) from error
    if identity is None or identity[0] != spec.task_ref["suite"]:
        raise ExperimentError(
            "SUITE_MISMATCH",
            f"scenario {spec.task_ref['scenario_version']} is {identity}, "
            f"not {spec.task_ref['suite']}",
        )


class ExperimentService:
    """编排既有 RunService/Worker 的实验服务（零第二套调度器）。"""

    def __init__(
        self,
        store: Any,
        run_service: Any,
        *,
        resources: Any = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.run_service = run_service
        # 资源仓库（datasets/scenarios/providers/models）：Cell 的 Run 走与
        # 普通 Run 创建**同一条** prepare_run 解析链（TOCTOU 重校验在这里）。
        self.resources = resources
        self._clock = clock or _default_clock
        self._allocation_lock = RLock()
        from motte_storage.platform import platform_for

        # Existing persistent request registry, namespaced by operation below.
        self._requests = platform_for(store).requests

    # ------------------------------------------------------------------ preview

    def preview(self, spec_payload: dict[str, Any]) -> dict[str, Any]:
        """零创建预览：校验、展开矩阵、护栏检查。不触碰任何存储。"""
        spec = ExperimentSpec.model_validate(spec_payload)
        return self._compile(spec)

    def _compile(self, spec: ExperimentSpec) -> dict[str, Any]:
        """Read-only preflight shared by preview, create and pending allocation."""
        from motte_sdk.resolve import ManifestResolutionError, prepare_run

        _validate_supported_suite(spec)
        expanded = _expand_matrix(spec)
        cells = [
            {
                "cell_id": compute_cell_id(
                    spec.experiment_id,
                    spec.version,
                    assignment,
                    repeat_index,
                ),
                "factor_assignment": assignment.as_dict(),
                "repeat_index": repeat_index,
            }
            for assignment, repeat_index in expanded
        ]
        max_potential_calls = 0
        case_counts: set[int] = set()
        if self.resources is None:
            raise ExperimentError("RESOURCE_UNRESOLVED", "experiment resources are unavailable")
        _validate_task_resource(spec, self.resources)
        for (assignment, _repeat_index), cell in zip(expanded, cells, strict=True):
            try:
                resolved_manifest, case_ids = prepare_run(
                    spec.task_ref["scenario_version"], self._build_manifest(spec, {
                        "factor_assignment": assignment.model_dump(mode="json"),
                    }),
                    [], self.resources,
                )
            except ManifestResolutionError as error:
                raise ExperimentError(error.code, str(error)) from error
            case_counts.add(len(case_ids))
            provider = resolved_manifest.get("provider") or {}
            retries = provider.get("max_retries", 0) if isinstance(provider, dict) else 0
            if type(retries) is not int or retries < 0:
                raise ExperimentError("BUDGET_UNRESOLVED", "provider retry limit is unknown")
            max_potential_calls += len(case_ids) * (1 + retries)
        case_count = next(iter(case_counts)) if len(case_counts) == 1 else None
        violations = _violations(spec, max_potential_calls)
        return {
            "experiment_id": spec.experiment_id,
            "version": spec.version,
            "cells": cells,
            "cell_count": len(cells),
            "max_potential_calls": max_potential_calls,
            "case_count": case_count,
            "case_count_resolved": case_count is not None,
            "budget": _budget_view(spec.budget_policy),
            "violations": violations,
        }

    # ------------------------------------------------------------------- create

    def create(
        self,
        spec_payload: dict[str, Any],
        *,
        request_key: str | None = None,
    ) -> dict[str, Any]:
        """发布 spec（幂等）+ 铺 cell（幂等）+ 分配。超限整体拒绝。"""
        with self._allocation_lock:
            return self._create_locked(spec_payload, request_key=request_key)

    def _create_locked(
        self, spec_payload: dict[str, Any], *, request_key: str | None,
    ) -> dict[str, Any]:
        spec = ExperimentSpec.model_validate(spec_payload)
        content_hash = spec.content_hash()
        registry_key = "experiment:create:" + request_key if request_key is not None else None
        resource_ref = f"{spec.experiment_id}@{spec.version}"
        if registry_key is not None:
            seen = self._requests.get(registry_key)
            if seen is not None and (seen["canonical_hash"] != content_hash
                                     or seen["run_id"] != resource_ref):
                raise ExperimentError(
                    "REQUEST_KEY_CONFLICT",
                    f"request_key {request_key!r} already created different spec content",
                )
        compiled = self._compile(spec)
        violations = compiled["violations"]
        if violations:
            raise ExperimentError(
                "EXPERIMENT_INVALID",
                "; ".join(f"{item['code']}: {item['message']}" for item in violations),
            )
        _validate_supported_suite(spec)
        if registry_key is not None:
            # Atomic cross-process reservation before any executable object.
            from motte_storage.platform import RequestConflict

            try:
                self._requests.bind(registry_key, content_hash, resource_ref)
            except RequestConflict as error:
                raise ExperimentError("REQUEST_KEY_CONFLICT", str(error)) from error
        # JSON 规范化（tuple→list）：与 sqlite/pg 落盘读回的形状一致，
        # 幂等重放不会因容器类型误判"异内容"（与 baseline store 同一规则）。
        dump = spec.model_dump(mode="json")
        stored = self.store.experiments.get_spec(spec.experiment_id, spec.version)
        created = stored is None
        if stored is None or stored != dump:
            # 同 (id,version) 异内容：透传存储层的 ValueError。
            self.store.experiments.put_spec(dump)
        for assignment, repeat_index in _expand_matrix(spec):
            self._ensure_cell(spec, assignment, repeat_index)
        allocation = self.allocate(spec.experiment_id, spec.version)
        return {
            "experiment_id": spec.experiment_id,
            "version": spec.version,
            "created": created,
            "spec": dump,
            **allocation,
        }

    # ----------------------------------------------------------------- allocate

    def allocate(self, experiment_id: str, version: str) -> dict[str, Any]:
        """可重入的分配/恢复入口：每 cell 恰好一个 initial Run。"""
        with self._allocation_lock:
            return self._allocate_locked(experiment_id, version)

    def _allocate_locked(self, experiment_id: str, version: str) -> dict[str, Any]:
        stored = self.store.experiments.get_spec(experiment_id, version)
        if stored is None:
            raise ExperimentError(
                "EXPERIMENT_NOT_FOUND",
                f"experiment {experiment_id}@{version} not found",
            )
        spec = ExperimentSpec.model_validate(stored)
        _validate_supported_suite(spec)
        if any(cell.get("allocation_status") in ("pending", "allocating") for cell in
               self.store.experiments.list_cells(experiment_id, version)):
            violations = self._compile(spec)["violations"]
            if violations:
                raise ExperimentError(
                    "EXPERIMENT_INVALID",
                    "; ".join(f"{item['code']}: {item['message']}" for item in violations),
                )
        allocated = 0
        skipped = 0
        failures: list[dict[str, str]] = []
        for cell in self.store.experiments.list_cells(experiment_id, version):
            outcome, reason = self._allocate_one(spec, cell)
            if outcome == "allocated":
                allocated += 1
            elif outcome == "failed":
                failures.append({"cell_id": cell["cell_id"], "reason": reason or ""})
            else:
                skipped += 1
        return {
            "experiment_id": experiment_id,
            "version": version,
            "allocated": allocated,
            "skipped_existing": skipped,
            "failed": failures,
            "cells": [
                self._cell_view(cell)
                for cell in self.store.experiments.list_cells(experiment_id, version)
            ],
        }

    def _allocate_one(
        self,
        spec: ExperimentSpec,
        cell: dict[str, Any],
    ) -> tuple[str, str | None]:
        cell_id = cell["cell_id"]
        status = cell.get("allocation_status", "pending")
        if status in ("allocated", "cancelled", "failed"):
            # 恢复不重建；显式 retry 是唯一重跑路径（协议 §1）。
            return "skipped", None
        if status == "allocating":
            run_id = deterministic_run_id(cell_id)
            if self.store.runs.get(run_id) is not None:
                # 崩溃窗口（create_run 后、complete_cell 前）：补 complete，不重建。
                if self._complete_claimed_cell(cell_id, run_id):
                    return "allocated", None
                return "skipped", None
            # claim 后、create_run 前崩溃：确认无 Run 后 reset 重领。
            self.store.experiments.reset_allocating(cell_id)
            status = "pending"
        if status != "pending":
            return "skipped", None
        if not self.store.experiments.claim_cell(cell_id):
            return "skipped", None  # 并发对手正在领
        run_id = deterministic_run_id(cell_id)
        try:
            self._create_cell_run(spec, cell, run_id)
        except RunConflictError:
            pass  # 同 run_id 已存在（并发/恢复）：幂等复用。
        except Exception as error:  # noqa: BLE001 - 单 cell 失败不吞：落账并汇总
            self._fail_cell_safely(cell_id, run_id, error)
            return "failed", f"{type(error).__name__}: {error}"
        if self._complete_claimed_cell(cell_id, run_id):
            return "allocated", None
        return "skipped", None

    def _complete_claimed_cell(self, cell_id: str, run_id: str) -> bool:
        """allocating → allocated；并发对手已用同 run_id 完成视为成功。"""
        try:
            self.store.experiments.complete_cell(cell_id, run_id)
            return True
        except ValueError:
            current = self.store.experiments.get_cell(cell_id)
            if (
                current is not None
                and current.get("allocation_status") == "allocated"
                and current.get("run_id") == run_id
            ):
                return True
            raise

    def _fail_cell_safely(self, cell_id: str, run_id: str, error: Exception) -> None:
        try:
            self.store.experiments.fail_cell(cell_id, str(error))
        except ValueError:
            current = self.store.experiments.get_cell(cell_id) or {}
            if current.get("allocation_status") not in ("allocated", "failed"):
                raise

    def _create_cell_run(
        self,
        spec: ExperimentSpec,
        cell: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        # 与普通 Run 创建同一条 prepare_run 解析链（场景/模型/快照/选择展开
        # 与 TOCTOU 重校验都复用；不建第二条私有装配路径）。
        from motte_sdk.resolve import ManifestResolutionError, prepare_run

        scenario = spec.task_ref["scenario_version"]
        _validate_task_resource(spec, self.resources)
        manifest = self._build_manifest(spec, cell)
        try:
            prepared, case_ids = prepare_run(scenario, manifest, [], self.resources)
        except ManifestResolutionError as error:
            raise ExperimentError(error.code, str(error)) from error
        return self.run_service.create_run(
            scenario_version=scenario,
            manifest=prepared,
            case_ids=case_ids,
            requested_manifest=manifest,
            run_id=run_id,
        )

    def _build_manifest(
        self,
        spec: ExperimentSpec,
        cell: dict[str, Any],
    ) -> dict[str, Any]:
        """Suite-specific manifest assembly, followed by the shared prepare_run chain."""
        _validate_supported_suite(spec)
        assignment = FactorAssignment.model_validate(
            cell["factor_assignment"],
        ).as_dict()
        suite = spec.task_ref["suite"]
        if suite == "direct-llm":
            manifest: dict[str, Any] = {"model": assignment["model_profile"]}
        elif suite == "gsm8k":
            # GSM8K's standalone route accepts the same model/reasoning selectors
            # but its own plugin freezes dataset, prompt, scorer and case selection.
            manifest = {"model": assignment["model_profile"]}
        else:
            raise ExperimentError("SUITE_UNSUPPORTED", str(suite))
        if assignment.get("reasoning_level") is not None:
            manifest["reasoning_level"] = assignment["reasoning_level"]
        for key, value in spec.controlled_conditions.items():
            if value is None:
                continue
            if key == "parameters" and isinstance(value, dict):
                merged = dict(manifest.get("parameters") or {})
                merged.update(value)
                manifest["parameters"] = merged
            elif key == "max_output_tokens":
                merged = dict(manifest.get("parameters") or {})
                merged.setdefault("max_output_tokens", value)
                manifest["parameters"] = merged
        if spec.selected_case_keys:
            manifest["case_selection"] = {
                "mode": "ids",
                "case_ids": list(spec.selected_case_keys),
            }
        return manifest

    def _ensure_cell(
        self,
        spec: ExperimentSpec,
        assignment: FactorAssignment,
        repeat_index: int,
    ) -> None:
        payload = self._cell_payload(spec, assignment, repeat_index)
        cell_id = payload["cell_id"]
        existing = self.store.experiments.get_cell(cell_id)
        if existing is None:
            try:
                self.store.experiments.put_cell(payload)
                return
            except ValueError:
                # 并发对手已插入（可能已推进状态）：重新读取后按身份字段校验。
                existing = self.store.experiments.get_cell(cell_id)
                if existing is None:
                    raise
        for key in (
            "experiment_id",
            "experiment_version",
            "repeat_index",
            "factor_assignment",
            "resolved_spec_hash",
        ):
            if existing.get(key) != payload.get(key):
                raise ValueError("cell content conflict: " + cell_id)

    @staticmethod
    def _cell_payload(
        spec: ExperimentSpec,
        assignment: FactorAssignment,
        repeat_index: int,
    ) -> dict[str, Any]:
        cell = ExperimentCell(
            cell_id=compute_cell_id(
                spec.experiment_id,
                spec.version,
                assignment,
                repeat_index,
            ),
            experiment_id=spec.experiment_id,
            experiment_version=spec.version,
            factor_assignment=assignment,
            repeat_index=repeat_index,
            resolved_spec_hash=spec.content_hash(),
        )
        # JSON 规范化（factor_assignment.values tuple→list），与存储读回一致。
        return cell.model_dump(mode="json")

    # ------------------------------------------------------------------- status

    def status(self, experiment_id: str, version: str | None = None) -> dict[str, Any]:
        stored = self._resolve_spec(experiment_id, version)
        resolved_version = stored["version"]
        cells = self.store.experiments.list_cells(experiment_id, resolved_version)
        return {
            "experiment_id": experiment_id,
            "version": resolved_version,
            "spec": stored,
            "cell_count": len(cells),
            "progress": self._progress(cells),
            "cells": [self._cell_view(cell) for cell in cells],
        }

    def _resolve_spec(
        self,
        experiment_id: str,
        version: str | None,
    ) -> dict[str, Any]:
        specs = self.store.experiments.list_specs(experiment_id)
        if not specs:
            raise ExperimentError(
                "EXPERIMENT_NOT_FOUND",
                f"experiment {experiment_id!r} not found",
            )
        if version is None:
            return specs[-1]  # list_specs 按 (id, version) 排序，取最新。
        for item in specs:
            if item.get("version") == version:
                return item
        raise ExperimentError(
            "EXPERIMENT_NOT_FOUND",
            f"experiment {experiment_id}@{version} not found",
        )

    # ------------------------------------------------------------------- cancel

    def cancel(
        self,
        experiment_id: str,
        version: str | None = None,
        *,
        reason: str = "experiment cancelled",
    ) -> dict[str, Any]:
        """只作用于本实验：pending/allocating cell 落 cancelled，自有 Run 走
        RunService.cancel；其他实验/游离 Run 一概不碰。"""
        stored = self._resolve_spec(experiment_id, version)
        resolved_version = stored["version"]
        cancelled_cells: list[str] = []
        cancelled_runs: list[str] = []
        for cell in self.store.experiments.list_cells(experiment_id, resolved_version):
            cell_id = cell["cell_id"]
            cell_status = cell.get("allocation_status", "pending")
            if cell_status == "pending":
                self.store.experiments.cancel_cell(cell_id)
                cancelled_cells.append(cell_id)
            elif cell_status == "allocating":
                run_id = deterministic_run_id(cell_id)
                if self._cancel_owned_run(run_id, reason):
                    cancelled_runs.append(run_id)
                self.store.experiments.cancel_cell(cell_id)
                cancelled_cells.append(cell_id)
            elif cell_status == "allocated":
                run_id = cell.get("run_id")
                if run_id and self._cancel_owned_run(run_id, reason):
                    cancelled_runs.append(run_id)
                # 自有 Run 已取消（或已是终态）：cell 状态同步为 cancelled，
                # 让实验进度如实反映"该单元不再会产生结果"。
                if run_id:
                    self.store.experiments.cancel_allocated_cell(cell_id)
                    cancelled_cells.append(cell_id)
        cells = self.store.experiments.list_cells(experiment_id, resolved_version)
        return {
            "experiment_id": experiment_id,
            "version": resolved_version,
            "reason": reason,
            "cancelled_at": self._clock(),
            "cancelled_cells": cancelled_cells,
            "cancelled_runs": cancelled_runs,
            "progress": self._progress(cells),
            "cells": [self._cell_view(cell) for cell in cells],
        }

    def _cancel_owned_run(self, run_id: str, reason: str) -> bool:
        run = self.store.runs.get(run_id)
        if run is None:
            return False
        terminal = getattr(self.run_service, "TERMINAL", None) or {
            "completed",
            "failed",
            "cancelled",
            "unsupported",
            "profile_stale",
            "needs_review",
        }
        if run.get("status") in terminal:
            return False
        self.run_service.cancel(run_id, reason=reason)
        return True

    # -------------------------------------------------------------- retry_cell

    def retry_cell(self, cell_id: str, *, reason: str) -> dict[str, Any]:
        """显式 retry：superseding 子 Run（parent 指向原 Run），原结果不消失，
        cell 分配状态保持 allocated（协议 §1 operator retry）。"""
        cell = self.store.experiments.get_cell(cell_id)
        if cell is None:
            raise ExperimentError("CELL_NOT_FOUND", f"cell {cell_id!r} not found")
        stored = self.store.experiments.get_spec(
            cell["experiment_id"],
            cell["experiment_version"],
        )
        if stored is None:
            raise ExperimentError(
                "EXPERIMENT_NOT_FOUND",
                f"experiment {cell['experiment_id']}@{cell['experiment_version']} not found",
            )
        spec = ExperimentSpec.model_validate(stored)
        original_run_id = cell.get("run_id")
        if not original_run_id:
            raise ExperimentError(
                "CELL_NOT_ALLOCATED",
                f"cell {cell_id} has no initial run to supersede",
            )
        superseding = list(cell.get("superseding_run_ids") or ())
        retry_run_id = deterministic_run_id(cell_id) + "-r" + str(len(superseding) + 1)
        run = self.run_service.create_run(
            scenario_version=spec.task_ref["scenario_version"],
            manifest=self._build_manifest(spec, cell),
            case_ids=(),
            run_id=retry_run_id,
            parent_run_id=original_run_id,
        )
        self.store.experiments.record_superseding(cell_id, retry_run_id)
        refreshed = self.store.experiments.get_cell(cell_id) or cell
        return {
            "cell_id": cell_id,
            "run_id": run["id"],
            "parent_run_id": original_run_id,
            "superseding_run_ids": list(refreshed.get("superseding_run_ids") or ()),
            "allocation_status": refreshed.get("allocation_status"),
            "reason": reason,
            "retried_at": self._clock(),
        }

    # -------------------------------------------------------------------- views

    @staticmethod
    def _progress(cells: list[dict[str, Any]]) -> dict[str, int]:
        counts = {name: 0 for name in PROGRESS_STATUSES}
        for cell in cells:
            key = cell.get("allocation_status", "pending")
            if key in counts:
                counts[key] += 1
        return counts

    @staticmethod
    def _cell_view(cell: dict[str, Any]) -> dict[str, Any]:
        assignment = cell.get("factor_assignment") or {}
        try:
            factors = FactorAssignment.model_validate(assignment).as_dict()
        except ValueError:
            factors = dict(assignment)
        return {
            "cell_id": cell.get("cell_id"),
            "experiment_id": cell.get("experiment_id"),
            "experiment_version": cell.get("experiment_version"),
            "repeat_index": cell.get("repeat_index", 0),
            "factor_assignment": factors,
            "allocation_status": cell.get("allocation_status", "pending"),
            "run_id": cell.get("run_id"),
            "superseding_run_ids": list(cell.get("superseding_run_ids") or ()),
            "failure_reason": cell.get("failure_reason"),
        }
