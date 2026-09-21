"""M6 ExperimentService 行为测试（A07 并发 / A08 恢复 / A20 取消 / retry / 幂等）。

Cell 的 Run 走与普通 Run 同一条 ``prepare_run`` 解析链，因此夹具播种**真实**
direct-llm 数据集（内置样例）与 provider/模型档案；半分配现场仍用公开存储
原语（put_spec / put_cell / claim_cell / complete_cell）构造，验证服务只编排
既有 RunService、每 cell 恰好一个 initial Run。
"""

from __future__ import annotations

import tempfile
from itertools import product
from pathlib import Path
from threading import Barrier, Thread

import pytest

from motte_contracts.experiment import (
    ExperimentCell,
    ExperimentSpec,
    FactorAssignment,
    FactorValue,
    compute_cell_id,
)
from motte_sdk.experiments import (
    ExperimentError,
    ExperimentService,
    deterministic_run_id,
)
from motte_sdk.service import RunService
from motte_storage.factory import default_content_store
from motte_storage.resource_store import SQLiteResourceStore
from motte_storage.run_store import SQLiteRunStore


def make_service() -> tuple[object, RunService, ExperimentService]:
    """真实 direct-llm 资源环境：内置数据集 + 双模型（含 reasoning levels）。

    Cell 的 Run 创建需要 scenario/dataset/provider/model 全部真实存在
    （prepare_run 的创建期校验与 TOCTOU 重校验都会消费它们）。
    """
    tmp = Path(tempfile.mkdtemp(prefix="m6-exp-sdk-")) / "env.db"
    resources = SQLiteResourceStore(str(tmp), content_store=default_content_store())
    from motte_sdk.direct_llm import import_builtin_dataset

    import_builtin_dataset("direct-llm-exact-answer", resources=resources)
    resources.providers.put({
        "name": "local", "kind": "openai_compatible",
        "base_url": "https://local.test/v1", "model": "unused",
    })
    for model_id in ("model-a", "model-b"):
        resources.models.put({
            "id": model_id, "provider": "local", "model": "probe-1",
            "capabilities": {}, "max_output_tokens": 4096,
            "reasoning": {
                "supported": True, "levels": ["low", "high"],
                "control": '{"reasoning_effort": reasoningLevel}',
                "default_level": "low",
            },
        })
    store = SQLiteRunStore(tmp)
    run_service = RunService(store)
    return store, run_service, ExperimentService(store, run_service, resources=resources)


def spec_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "experiment_id": "exp-alpha",
        "version": "v1",
        "task_ref": {"suite": "direct-llm",
                     "scenario_version": "direct-llm-exact-answer@1"},
        "factors": {
            "model_profile": ("model-a", "model-b"),
            "reasoning_level": ("low", "high"),
        },
        "repeats": 2,
        "trials_per_run": None,
        "controlled_conditions": {"max_output_tokens": 512, "note": None},
        "budget_policy": {"max_total_calls": 2000},
        "created_by": "tester",
        "reason": "unit test",
    }
    payload.update(overrides)
    return payload


def put_manual_cells(
    store: object,
    payload: dict[str, object],
) -> tuple[ExperimentSpec, list[dict[str, object]]]:
    """绕过 create 的分配：手工铺 spec + 全部 cell（模拟半分配崩溃现场）。"""
    spec = ExperimentSpec.model_validate(payload)  # type: ignore[arg-type]
    store.experiments.put_spec(spec.model_dump())
    factor_names = sorted(spec.factors)
    combos = list(product(*(spec.factors[name] for name in factor_names))) or [()]
    cells: list[dict[str, object]] = []
    for repeat_index in range(spec.repeats):
        for combo in combos:
            assignment = FactorAssignment(
                values=tuple(
                    FactorValue(factor=name, value=value)
                    for name, value in zip(factor_names, combo, strict=True)
                )
            )
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
            cells.append(store.experiments.put_cell(cell.model_dump()))
    return spec, cells


def allocate_one_manually(
    store: object,
    run_service: RunService,
    spec: ExperimentSpec,
    cell_id: str,
) -> str:
    """用存储原语完整分配一个 cell（claim → create_run → complete）。"""
    assert store.experiments.claim_cell(cell_id) is True
    run_id = deterministic_run_id(cell_id)
    run_service.create_run(
        scenario_version=spec.task_ref["scenario_version"],
        manifest={"model": "model-a"},
        run_id=run_id,
    )
    store.experiments.complete_cell(cell_id, run_id)
    return run_id


# --------------------------------------------------------------------- preview


def test_preview_expands_matrix_without_touching_store() -> None:
    store, _run_service, service = make_service()
    result = service.preview(spec_payload(trials_per_run=3))
    assert result["cell_count"] == 8  # 2×2 因素 × 2 repeats
    # 真实场景 case 数（内置样例 8 题）参与预算核算：8 cell × 3 trial × 8 题。
    assert result["case_count"] == 8 and result["case_count_resolved"] is True
    assert result["max_potential_calls"] == 8 * 3 * 8
    assert len({cell["cell_id"] for cell in result["cells"]}) == 8
    assert {cell["repeat_index"] for cell in result["cells"]} == {0, 1}
    assert result["violations"] == []
    # 零创建：没有任何 cell / spec / run 落地。
    assert store.experiments.list_cells("exp-alpha") == []
    assert store.experiments.list_specs() == []
    assert store.runs.list() == []


def test_preview_reports_unknown_cost_not_zero() -> None:
    _store, _run_service, service = make_service()
    result = service.preview(
        spec_payload(
            budget_policy={"max_total_calls": 2000, "max_cost_usd": 12.5},
        )
    )
    budget = result["budget"]
    assert budget["max_cost_usd"] == 12.5
    assert budget["cost_known"] == "unknown_until_run"
    assert budget["known_cost_usd"] is None
    assert budget["unknown"] is True


def test_preview_flags_violations_and_create_rejects_wholesale() -> None:
    store, _run_service, service = make_service()
    payload = spec_payload(max_cells=4, budget_policy={"max_total_calls": 10})
    result = service.preview(payload)
    codes = {item["code"] for item in result["violations"]}
    assert codes == {"MATRIX_TOO_LARGE", "BUDGET_EXCEEDED"}
    with pytest.raises(ExperimentError) as excinfo:
        service.create(payload)
    assert excinfo.value.code == "EXPERIMENT_INVALID"
    assert "MATRIX_TOO_LARGE" in str(excinfo.value)
    # 整体拒绝：spec 与 cell 都不落地。
    assert store.experiments.list_specs() == []
    assert store.experiments.list_cells("exp-alpha") == []
    assert store.runs.list() == []


def test_preview_propagates_validation_errors() -> None:
    _store, _run_service, service = make_service()
    with pytest.raises(ValueError):
        service.preview(spec_payload(factors={"unknown_factor": ("x",)}))


# --------------------------------------------------------------------- create


def test_create_is_idempotent_same_cells_no_extra_runs() -> None:
    store, _run_service, service = make_service()
    first = service.create(spec_payload())
    second = service.create(spec_payload())
    assert [c["cell_id"] for c in first["cells"]] == [c["cell_id"] for c in second["cells"]]
    assert len(store.runs.list()) == 8  # 第二次 create 不重复建 Run
    assert second["created"] is False
    assert second["allocated"] == 0
    assert second["skipped_existing"] == 8
    assert all(c["allocation_status"] == "allocated" for c in second["cells"])


def test_direct_llm_manifest_assembly() -> None:
    _store, run_service, service = make_service()
    result = service.create(spec_payload())
    cell = next(
        c
        for c in result["cells"]
        if c["factor_assignment"]["model_profile"] == "model-a"
        and c["factor_assignment"]["reasoning_level"] == "high"
        and c["repeat_index"] == 0
    )
    run = run_service.get_run(cell["run_id"])
    assert run["scenario_version"] == "direct-llm-exact-answer@1"
    assert run["status"] == "queued"
    manifest = run["manifest"]
    assert manifest["model"] == "model-a"
    assert manifest["reasoning_level"] == "high"
    # controlled_conditions 经白名单并入 parameters（max_output_tokens 简写）。
    assert manifest["parameters"]["max_output_tokens"] == 512
    assert "note" not in manifest  # None 跳过，不补假值
    # 未声明 selected_case_keys → 全量展开（prepare_run 展开为内置样例全部题）。
    assert len(run["case_ids"]) == 8
    assert run["manifest"]["cases"] or "cases" in run["manifest"]


def test_unknown_suite_is_rejected_honestly() -> None:
    store, _run_service, service = make_service()
    payload = spec_payload(task_ref={"suite": "harbor", "scenario_version": "harbor@1"})
    with pytest.raises(ExperimentError) as excinfo:
        service.create(payload)
    assert excinfo.value.code == "SUITE_UNSUPPORTED"
    assert "harbor" in str(excinfo.value)
    assert store.runs.list() == []


def test_request_key_conflict_and_plain_spec_conflict() -> None:
    store, _run_service, service = make_service()
    payload = spec_payload()
    service.create(payload, request_key="key-1")
    runs_after_first = len(store.runs.list())
    # 同 key 同内容：幂等返回现有分配状态。
    again = service.create(payload, request_key="key-1")
    assert len(store.runs.list()) == runs_after_first
    assert again["created"] is False
    # 同 key 不同内容：REQUEST_KEY_CONFLICT。
    conflicting = spec_payload(budget_policy={"max_total_calls": 1999})
    with pytest.raises(ExperimentError) as excinfo:
        service.create(conflicting, request_key="key-1")
    assert excinfo.value.code == "REQUEST_KEY_CONFLICT"
    # 不同 key、同 (id@version) 异内容：put_spec 的 ValueError 透传。
    with pytest.raises(ValueError) as spec_conflict:
        service.create(conflicting, request_key="key-2")
    assert type(spec_conflict.value) is ValueError
    assert "immutable" in str(spec_conflict.value)


# ------------------------------------------------------------ A07 concurrency


def test_concurrent_create_gives_each_cell_exactly_one_run() -> None:
    store, _run_service, service = make_service()
    payload = spec_payload()
    barrier = Barrier(2)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            service.create(payload)
        except BaseException as error:  # noqa: BLE001 - 线程内异常必须浮出断言
            errors.append(error)

    threads = [Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    cells = store.experiments.list_cells("exp-alpha", "v1")
    assert len(cells) == 8
    assert all(c["allocation_status"] == "allocated" for c in cells)
    run_ids = [c["run_id"] for c in cells]
    assert len(set(run_ids)) == 8
    assert all(store.runs.get(run_id) is not None for run_id in run_ids)
    assert len(store.runs.list()) == 8


# ----------------------------------------------------------- A08 crash resume


def test_allocate_resumes_half_allocated_crash_without_doubling() -> None:
    store, run_service, service = make_service()
    payload = spec_payload(factors={"model_profile": ("model-a", "model-b")}, repeats=2)
    spec, cells = put_manual_cells(store, payload)
    # cell0：已完整分配（恢复时不得重建）。
    done_run = allocate_one_manually(store, run_service, spec, cells[0]["cell_id"])
    # cell1/cell2：allocating 且无 Run（claim 后、create_run 前崩溃）。
    for cell in (cells[1], cells[2]):
        assert store.experiments.claim_cell(cell["cell_id"]) is True
    # cell3：保持 pending。
    result = service.allocate("exp-alpha", "v1")
    assert result["failed"] == []
    assert len(store.runs.list()) == 4  # cell 数不翻倍
    final = {c["cell_id"]: c for c in store.experiments.list_cells("exp-alpha", "v1")}
    assert all(c["allocation_status"] == "allocated" for c in final.values())
    assert len({c["run_id"] for c in final.values()}) == 4
    assert final[cells[0]["cell_id"]]["run_id"] == done_run  # 已分配的未动
    for cell in final.values():
        assert cell["run_id"] == deterministic_run_id(cell["cell_id"])


def test_allocate_completes_existing_run_instead_of_rebuilding() -> None:
    store, run_service, service = make_service()
    payload = spec_payload(factors={"model_profile": ("model-a",)}, repeats=1)
    spec, cells = put_manual_cells(store, payload)
    cell_id = cells[0]["cell_id"]
    assert store.experiments.claim_cell(cell_id) is True
    run_id = deterministic_run_id(cell_id)
    run_service.create_run(
        scenario_version=spec.task_ref["scenario_version"],
        manifest={"model": "model-a"},
        run_id=run_id,
    )
    result = service.allocate("exp-alpha", "v1")
    assert result["allocated"] == 1
    assert len(store.runs.list()) == 1  # Run 已存在 → 补 complete，不重建
    cell = store.experiments.get_cell(cell_id)
    assert cell["allocation_status"] == "allocated"
    assert cell["run_id"] == run_id


# ----------------------------------------------------------------- A20 cancel


def test_cancel_scopes_to_own_experiment() -> None:
    store, run_service, service = make_service()
    payload_a = spec_payload(
        experiment_id="exp-a",
        factors={"model_profile": ("model-a",)},
        repeats=2,
    )
    spec_a, cells_a = put_manual_cells(store, payload_a)
    # exp-a cell0：allocated（有 Run）；cell1：pending。
    owned_run = allocate_one_manually(store, run_service, spec_a, cells_a[0]["cell_id"])
    # exp-b：正常创建分配，验证不串。
    service.create(
        spec_payload(
            experiment_id="exp-b",
            factors={"model_profile": ("model-b",)},
            repeats=1,
        )
    )
    result = service.cancel("exp-a", reason="operator stopped it")
    # 自有 Run 取消后，已分配的 cell 状态联动为 cancelled（不再产生结果）。
    assert set(result["cancelled_cells"]) == {
        cells_a[0]["cell_id"], cells_a[1]["cell_id"],
    }
    assert result["cancelled_runs"] == [owned_run]
    assert store.runs.get(owned_run)["status"] == "cancelled"
    assert store.experiments.get_cell(cells_a[1]["cell_id"])["allocation_status"] == "cancelled"
    # B 的 Run 与 cell 完全不受影响。
    b_cell = store.experiments.list_cells("exp-b", "v1")[0]
    assert b_cell["allocation_status"] == "allocated"
    assert store.runs.get(b_cell["run_id"])["status"] == "queued"
    progress = result["progress"]
    assert progress["cancelled"] == 2
    assert service.status("exp-a")["progress"]["allocated"] == 0


def test_cancel_allocating_cell_with_existing_run() -> None:
    store, run_service, service = make_service()
    payload = spec_payload(factors={"model_profile": ("model-a",)}, repeats=1)
    spec, cells = put_manual_cells(store, payload)
    cell_id = cells[0]["cell_id"]
    assert store.experiments.claim_cell(cell_id) is True
    run_id = deterministic_run_id(cell_id)
    run_service.create_run(
        scenario_version=spec.task_ref["scenario_version"],
        manifest={"model": "model-a"},
        run_id=run_id,
    )
    result = service.cancel("exp-alpha")
    assert result["cancelled_cells"] == [cell_id]
    assert result["cancelled_runs"] == [run_id]
    assert store.runs.get(run_id)["status"] == "cancelled"
    assert store.experiments.get_cell(cell_id)["allocation_status"] == "cancelled"


# ----------------------------------------------------------------- retry_cell


def test_retry_cell_creates_superseding_run_original_survives() -> None:
    store, run_service, service = make_service()
    result = service.create(
        spec_payload(
            factors={"model_profile": ("model-a",)},
            repeats=1,
        )
    )
    cell = result["cells"][0]
    original_run_id = cell["run_id"]
    retry = service.retry_cell(cell["cell_id"], reason="operator requested")
    new_run_id = retry["run_id"]
    assert new_run_id != original_run_id
    assert new_run_id == deterministic_run_id(cell["cell_id"]) + "-r1"
    assert retry["parent_run_id"] == original_run_id
    stored_run = store.runs.get(new_run_id)
    assert stored_run is not None
    assert stored_run["parent_run_id"] == original_run_id
    assert store.runs.get(original_run_id) is not None  # 原 initial Run 不消失
    refreshed = store.experiments.get_cell(cell["cell_id"])
    assert list(refreshed["superseding_run_ids"]) == [new_run_id]
    assert refreshed["allocation_status"] == "allocated"  # 分配状态不变
    retry_again = service.retry_cell(cell["cell_id"], reason="again")
    assert retry_again["run_id"].endswith("-r2")
    assert retry_again["parent_run_id"] == original_run_id


def test_retry_cell_requires_allocated_run() -> None:
    _store, _run_service, service = make_service()
    with pytest.raises(ExperimentError) as excinfo:
        service.retry_cell("sha256:" + "0" * 64, reason="nope")
    assert excinfo.value.code == "CELL_NOT_FOUND"


# --------------------------------------------------------------------- status


def test_status_reports_spec_cells_and_progress() -> None:
    store, _run_service, service = make_service()
    service.create(spec_payload(experiment_id="exp-s", version="v2"))
    snapshot = service.status("exp-s")
    assert snapshot["version"] == "v2"
    assert snapshot["spec"]["experiment_id"] == "exp-s"
    assert snapshot["cell_count"] == 8
    assert snapshot["progress"] == {
        "pending": 0,
        "allocating": 0,
        "allocated": 8,
        "failed": 0,
        "cancelled": 0,
    }
    assert all(cell["run_id"] for cell in snapshot["cells"])
    # version=None → 最新版本。
    service.create(spec_payload(experiment_id="exp-s", version="v3", repeats=1))
    assert service.status("exp-s")["version"] == "v3"
    with pytest.raises(ExperimentError) as excinfo:
        service.status("exp-s", version="v9")
    assert excinfo.value.code == "EXPERIMENT_NOT_FOUND"


def test_deterministic_run_id_shape() -> None:
    cell_id = "sha256:" + "ab" * 32
    assert deterministic_run_id(cell_id) == "run-exp-" + "ab" * 12
    with pytest.raises(ValueError):
        deterministic_run_id("not-a-hash")
