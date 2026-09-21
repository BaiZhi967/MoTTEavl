"""M6 存储行为测试：Experiments / GateStore / BaselineStore 三后端同语义。

- 参数化 memory / sqlite / pg；pg 组需 MOTTE_PG_DSN，未设置时整组 skip。
- 覆盖：spec/cell/policy/result 的不可变幂等与异内容冲突、cell 分配状态机
  （claim/complete/cancel/reset/fail/superseding）、gate 追加只读、
  baseline 契约校验 + 默认指针 CAS + 切换历史。
- A07 并发：两个线程（sqlite 为两个连接、pg 为两个实例）对同一 pending
  cell 并发 claim，恰一个成功。
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from pydantic import ValidationError

from motte_contracts.comparison import BaselineSnapshot, DefaultBaselinePointer
from motte_storage.baseline_store import (
    BaselineConflict,
    MemoryBaselineStore,
    SQLiteBaselineStore,
)
from motte_storage.experiments import MemoryExperiments, SQLiteExperiments
from motte_storage.gate_store import MemoryGateStore, SQLiteGateStore

#: PG 清理时需要清空的 M6 证据表（迁移 0014 建的全部表）。
_M6_TABLES = (
    "experiment_specs",
    "experiment_cells",
    "gate_policies",
    "gate_results",
    "m6_baselines",
    "default_baselines",
    "default_baseline_history",
)

_BACKENDS = [
    "memory",
    "sqlite",
    pytest.param(
        "pg",
        marks=pytest.mark.skipif(
            not os.environ.get("MOTTE_PG_DSN"),
            reason="set MOTTE_PG_DSN to run PostgreSQL integration tests",
        ),
    ),
]


class M6Stores(NamedTuple):
    """一个后端下的三件套 + 并发用第二访问点。"""

    backend: str
    experiments: Any
    gates: Any
    baselines: Any
    experiments_peer: Any


@pytest.fixture(scope="module")
def pg_dsn():
    """空库起底 → alembic head（照抄 test_postgres_e2e 的建库方式）。

    测完清空 M6 表：本模块只拥有这些表的数据，不触碰其他证据表。
    """
    import importlib.util

    import psycopg

    from motte_storage.migrations import MIGRATIONS_DIR, current, revision_ids, upgrade
    from motte_storage.postgres import normalize_dsn

    dsn = normalize_dsn(os.environ["MOTTE_PG_DSN"])
    versions = MIGRATIONS_DIR / "versions"
    # 文件名不一定等于 revision id；按模块内声明的 revision 索引，
    # 升级/降级顺序以 revision_ids() 为准。
    by_revision: dict[str, Any] = {}
    for version_file in sorted(versions.glob("*.py")):
        spec = importlib.util.spec_from_file_location(version_file.stem, version_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        by_revision[str(module.revision)] = module
    modules = []
    for revision in reversed(revision_ids()):  # 新版本先回退
        if revision not in by_revision:
            raise AssertionError(f"migration module missing for revision {revision}")
        modules.append(by_revision[revision])

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            for module in modules:
                for statement in module.DOWN_STATEMENTS:
                    # 0002 的 runs 列在已回退的 0001 库上不存在。
                    if "ALTER TABLE runs DROP COLUMN" in statement:
                        cursor.execute("SELECT to_regclass('public.runs')")
                        if cursor.fetchone()[0] is None:
                            continue
                    cursor.execute(statement)
            cursor.execute("DROP TABLE IF EXISTS schema_migrations")
            cursor.execute("DROP TABLE IF EXISTS alembic_version")
        connection.commit()

    head = revision_ids()[-1]
    assert current(dsn) is None
    assert upgrade(dsn) == head
    assert upgrade(dsn) == head  # 幂等

    yield dsn

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            for table in _M6_TABLES:
                cursor.execute("DELETE FROM " + table)
        connection.commit()


@pytest.fixture(params=_BACKENDS)
def m6(request: pytest.FixtureRequest, tmp_path: Path) -> M6Stores:
    backend = request.param
    if backend == "memory":
        lock = threading.RLock()
        experiments: Any = MemoryExperiments(lock)
        return M6Stores(
            backend,
            experiments,
            MemoryGateStore(lock),
            MemoryBaselineStore(lock),
            experiments,
        )
    if backend == "sqlite":
        path = str(tmp_path / "m6.db")
        return M6Stores(
            backend,
            SQLiteExperiments(path),
            SQLiteGateStore(path),
            SQLiteBaselineStore(path),
            SQLiteExperiments(path),  # 第二个连接，同库
        )
    from motte_storage.pg_m6 import PgBaselineStore, PgExperiments, PgGateStore

    dsn = request.getfixturevalue("pg_dsn")
    return M6Stores(
        backend,
        PgExperiments(dsn),
        PgGateStore(dsn),
        PgBaselineStore(dsn),
        PgExperiments(dsn),  # 第二个连接，同库
    )


def _spec(
    *,
    experiment_id: str = "exp-a",
    version: str = "1",
    created_by: str = "m6-tests",
) -> dict[str, Any]:
    return {
        "experiment_id": experiment_id,
        "version": version,
        "created_by": created_by,
        "factor_space": {"model": ["m-primary", "m-challenger"]},
        "repeat_count": 1,
    }


def _cell(
    cell_id: str,
    *,
    experiment_id: str = "exp-a",
    version: str = "1",
    factor_assignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "cell_id": cell_id,
        "experiment_id": experiment_id,
        "experiment_version": version,
        "allocation_status": "pending",
        "repeat_index": 0,
        "factor_assignment": {"model": "m-primary"}
        if factor_assignment is None
        else factor_assignment,
    }


def _policy(
    *,
    policy_id: str = "quality",
    version: str = "1",
    rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "policy_id": policy_id,
        "version": version,
        "lifecycle": "active",
        "rules": [{"metric": "accuracy", "min_pass_rate": 0.8}] if rules is None else rules,
    }


def _gate_result(
    result_id: str,
    *,
    policy_id: str = "policy-a",
    passed: bool = True,
) -> dict[str, Any]:
    return {
        "gate_result_id": result_id,
        "policy_id": policy_id,
        "passed": passed,
        "checked_at": "2026-09-21T00:00:00Z",
    }


def _snapshot(
    baseline_id: str = "bl-1",
    *,
    reason: str = "freeze after gate pass",
    metrics: dict[str, float] | None = None,
) -> dict[str, Any]:
    """合法 BaselineSnapshot（契约校验后按 JSON 形状 dump：entries 为 list）。

    ``model_dump()`` 会保留 tuple 形状的 entries；JSON 落盘（sqlite/pg）读回
    是 list，tuple != list 会让同内容幂等比较失真，因此统一取 JSON 形状。
    """
    return BaselineSnapshot.model_validate(
        {
            "baseline_id": baseline_id,
            "entries": [
                {
                    "cell_key": "exp-a@1:repeat-0",
                    "ref": {
                        "run_id": "run-1",
                        "scoring_pass_id": "sp-1",
                        "report_schema": "run-report@1",
                        "evidence_hash": "sha256:" + "1" * 64,
                    },
                },
            ],
            "comparison_policy_hash": "sha256:" + "2" * 64,
            "eligibility": "formal",
            "created_by": "m6-tests",
            "reason": reason,
            "created_at": "2026-09-21T00:00:00Z",
            "metrics": {"accuracy": 0.82} if metrics is None else metrics,
        }
    ).model_dump(mode="json")


def _pointer(
    scope: str,
    baseline_id: str,
    *,
    reason: str = "rotate default baseline",
) -> dict[str, Any]:
    return DefaultBaselinePointer(
        scope=scope,
        baseline_id=baseline_id,
        updated_by="m6-tests",
        reason=reason,
        comparison_policy_hash="sha256:" + "2" * 64,
        updated_at="2026-09-21T00:00:00Z",
    ).model_dump()


# -- experiments: spec -------------------------------------------------------


def test_spec_put_is_idempotent_for_same_content(m6):
    spec = _spec()
    assert m6.experiments.put_spec(spec) == spec
    assert m6.experiments.put_spec(spec) == spec
    assert m6.experiments.get_spec("exp-a", "1") == spec


def test_spec_put_rejects_different_content_for_same_key(m6):
    m6.experiments.put_spec(_spec())
    with pytest.raises(ValueError, match="immutable"):
        m6.experiments.put_spec(_spec(created_by="someone-else"))
    assert m6.experiments.get_spec("exp-a", "1") == _spec()


# -- experiments: cell -------------------------------------------------------


def test_cell_put_is_idempotent_for_same_content(m6):
    cell = _cell("cell-a")
    assert m6.experiments.put_cell(cell) == cell
    assert m6.experiments.put_cell(cell) == cell
    assert m6.experiments.get_cell("cell-a") == cell


def test_cell_put_rejects_different_content_for_same_cell_id(m6):
    m6.experiments.put_cell(_cell("cell-a"))
    with pytest.raises(ValueError, match="conflict"):
        m6.experiments.put_cell(_cell("cell-a", factor_assignment={"model": "m-challenger"}))
    assert m6.experiments.get_cell("cell-a") == _cell("cell-a")


def test_claim_allocate_complete_lifecycle(m6):
    experiments = m6.experiments
    experiments.put_cell(_cell("cell-lc"))
    assert experiments.claim_cell("cell-lc") is True
    assert experiments.get_cell("cell-lc")["allocation_status"] == "allocating"
    assert experiments.claim_cell("cell-lc") is False
    completed = experiments.complete_cell("cell-lc", "run-42")
    assert completed["allocation_status"] == "allocated"
    stored = experiments.get_cell("cell-lc")
    assert stored["run_id"] == "run-42"
    assert stored["allocation_status"] == "allocated"


def test_claim_missing_cell_raises_keyerror(m6):
    with pytest.raises(KeyError):
        m6.experiments.claim_cell("cell-missing")


def test_cancel_pending_cell_allowed_but_allocated_rejected(m6):
    experiments = m6.experiments
    experiments.put_cell(_cell("cell-cancel-pending"))
    cancelled = experiments.cancel_cell("cell-cancel-pending")
    assert cancelled["allocation_status"] == "cancelled"
    experiments.put_cell(_cell("cell-cancel-allocated"))
    experiments.claim_cell("cell-cancel-allocated")
    experiments.complete_cell("cell-cancel-allocated", "run-7")
    with pytest.raises(ValueError, match="cancelled"):
        experiments.cancel_cell("cell-cancel-allocated")


def test_reset_allocating_only_transitions_from_allocating(m6):
    experiments = m6.experiments
    experiments.put_cell(_cell("cell-reset"))
    assert experiments.reset_allocating("cell-reset") is False
    assert experiments.get_cell("cell-reset")["allocation_status"] == "pending"
    experiments.claim_cell("cell-reset")
    assert experiments.reset_allocating("cell-reset") is True
    assert experiments.get_cell("cell-reset")["allocation_status"] == "pending"


def test_fail_cell_preserves_failure_reason(m6):
    experiments = m6.experiments
    experiments.put_cell(_cell("cell-fail"))
    experiments.claim_cell("cell-fail")
    failed = experiments.fail_cell("cell-fail", "provider quota exceeded")
    assert failed["allocation_status"] == "failed"
    assert failed["failure_reason"] == "provider quota exceeded"
    stored = experiments.get_cell("cell-fail")
    assert stored["allocation_status"] == "failed"
    assert stored["failure_reason"] == "provider quota exceeded"


def test_record_superseding_appends_and_keeps_initial_run(m6):
    experiments = m6.experiments
    experiments.put_cell(_cell("cell-sup"))
    experiments.claim_cell("cell-sup")
    experiments.complete_cell("cell-sup", "run-initial")
    experiments.record_superseding("cell-sup", "run-retry-1")
    experiments.record_superseding("cell-sup", "run-retry-2")
    stored = experiments.get_cell("cell-sup")
    assert stored["run_id"] == "run-initial"
    assert stored["superseding_run_ids"] == ["run-retry-1", "run-retry-2"]


def test_list_cells_filters_by_experiment_and_version(m6):
    experiments = m6.experiments
    experiments.put_cell(_cell("cell-a1", experiment_id="exp-a", version="1"))
    experiments.put_cell(_cell("cell-a2", experiment_id="exp-a", version="2"))
    experiments.put_cell(_cell("cell-b1", experiment_id="exp-b", version="1"))
    assert {cell["cell_id"] for cell in experiments.list_cells("exp-a")} == {
        "cell-a1",
        "cell-a2",
    }
    assert {cell["cell_id"] for cell in experiments.list_cells("exp-a", "2")} == {"cell-a2"}
    assert {cell["cell_id"] for cell in experiments.list_cells("exp-b", "1")} == {"cell-b1"}
    assert experiments.list_cells("exp-none") == []


# -- gate_store --------------------------------------------------------------


def test_policy_put_is_idempotent_and_immutable(m6):
    gates = m6.gates
    policy = _policy()
    assert gates.put_policy(policy) == policy
    assert gates.put_policy(policy) == policy
    with pytest.raises(ValueError, match="immutable"):
        gates.put_policy(_policy(rules=[{"metric": "accuracy", "min_pass_rate": 0.9}]))
    assert gates.get_policy("quality", "1") == policy


def test_deprecate_policy_flips_lifecycle_only_and_is_idempotent(m6):
    gates = m6.gates
    policy = _policy()
    gates.put_policy(policy)
    deprecated = gates.deprecate_policy("quality", "1")
    assert deprecated["lifecycle"] == "deprecated"
    expected = {**policy, "lifecycle": "deprecated"}
    assert gates.get_policy("quality", "1") == expected  # payload 其余不变
    assert gates.deprecate_policy("quality", "1") == expected  # 再弃用幂等
    assert gates.get_policy("quality", "1") == expected


def test_deprecate_missing_policy_raises_keyerror(m6):
    with pytest.raises(KeyError):
        m6.gates.deprecate_policy("missing", "1")


def test_result_put_is_idempotent_and_append_only(m6):
    gates = m6.gates
    result = _gate_result("gr-1")
    assert gates.put_result(result) == result
    assert gates.put_result(result) == result
    with pytest.raises(ValueError, match="append-only"):
        gates.put_result(_gate_result("gr-1", passed=False))
    # 重复求值不改写原结果（协议 §1）
    assert gates.get_result("gr-1") == result


def test_list_results_filters_by_policy_and_applies_limit(m6):
    gates = m6.gates
    for index in range(3):
        gates.put_result(_gate_result(f"gr-a-{index}", policy_id="policy-a"))
    gates.put_result(_gate_result("gr-b-0", policy_id="policy-b"))
    all_results = gates.list_results()
    assert len(all_results) == 4
    policy_a = gates.list_results(policy_id="policy-a")
    assert {item["gate_result_id"] for item in policy_a} == {
        "gr-a-0",
        "gr-a-1",
        "gr-a-2",
    }
    assert policy_a[0]["gate_result_id"] == "gr-a-2"  # 最新在前
    limited = gates.list_results(policy_id="policy-a", limit=2)
    assert [item["gate_result_id"] for item in limited] == ["gr-a-2", "gr-a-1"]


def test_get_missing_result_returns_none(m6):
    assert m6.gates.get_result("no-such-result") is None


# -- baseline_store ----------------------------------------------------------


def test_baseline_put_roundtrip(m6):
    payload = _snapshot("bl-round")
    assert m6.baselines.put(payload) == payload
    fetched = m6.baselines.get("bl-round")
    assert fetched == payload
    entry = fetched["entries"][0]
    assert entry["cell_key"] == "exp-a@1:repeat-0"
    assert entry["ref"] == {
        "run_id": "run-1",
        "scoring_pass_id": "sp-1",
        "report_schema": "run-report@1",
        "evidence_hash": "sha256:" + "1" * 64,
    }
    assert fetched["metrics"] == {"accuracy": 0.82}
    assert m6.baselines.get("bl-missing") is None


def test_baseline_put_rejects_contract_violations(m6):
    invalid = _snapshot("bl-bad")
    del invalid["reason"]
    with pytest.raises(ValidationError):
        m6.baselines.put(invalid)
    assert m6.baselines.get("bl-bad") is None


def test_baseline_put_same_id_conflict_and_same_content_idempotent(m6):
    baselines = m6.baselines
    payload = _snapshot("bl-imm")
    assert baselines.put(payload) == payload
    assert baselines.put(payload) == payload
    with pytest.raises(BaselineConflict) as conflict:
        baselines.put(_snapshot("bl-imm", reason="a different freeze reason"))
    assert conflict.value.code == "BASELINE_IMMUTABLE"
    assert baselines.get("bl-imm") == payload


def test_set_default_cas_semantics(m6):
    baselines = m6.baselines
    baselines.put(_snapshot("bl-cas-1"))
    baselines.put(_snapshot("bl-cas-2", reason="second freeze"))
    # 首次：expected_current=None，position=1
    first = baselines.set_default(_pointer("global", "bl-cas-1"))
    assert first["baseline_id"] == "bl-cas-1"
    assert first["position"] == 1
    # 已存在默认再不带 expected → 冲突
    with pytest.raises(BaselineConflict) as no_expected:
        baselines.set_default(_pointer("global", "bl-cas-2"))
    assert no_expected.value.code == "CAS_CONFLICT"
    # expected 匹配 → position=2
    second = baselines.set_default(
        _pointer("global", "bl-cas-2"),
        expected_current="bl-cas-1",
    )
    assert second["position"] == 2
    # expected 不匹配 → CAS_CONFLICT
    with pytest.raises(BaselineConflict) as stale:
        baselines.set_default(_pointer("global", "bl-cas-1"), expected_current="bl-cas-1")
    assert stale.value.code == "CAS_CONFLICT"
    assert baselines.get_default("global")["baseline_id"] == "bl-cas-2"


def test_default_history_records_switches_in_position_order(m6):
    baselines = m6.baselines
    baselines.put(_snapshot("bl-h-1"))
    baselines.put(_snapshot("bl-h-2", reason="rotation"))
    baselines.set_default(_pointer("global", "bl-h-1"))
    baselines.set_default(_pointer("global", "bl-h-2"), expected_current="bl-h-1")
    history = baselines.default_history("global")
    assert [item["baseline_id"] for item in history] == ["bl-h-1", "bl-h-2"]
    assert [item["position"] for item in history] == [1, 2]
    latest = baselines.get_default("global")
    assert latest["baseline_id"] == "bl-h-2"
    assert latest["position"] == 2


# -- 并发（A07）--------------------------------------------------------------


def test_concurrent_claim_yields_exactly_one_winner(m6):
    """两个访问点（sqlite/pg 为两个连接）并发 claim 同一 pending cell。"""
    experiments = m6.experiments
    experiments.put_cell(_cell("cell-race"))
    barrier = threading.Barrier(2)

    def claim(store: Any) -> bool:
        barrier.wait(timeout=10)
        return bool(store.claim_cell("cell-race"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(claim, m6.experiments),
            pool.submit(claim, m6.experiments_peer),
        ]
        outcomes = sorted(future.result(timeout=20) for future in futures)
    assert outcomes == [False, True]
    stored = experiments.get_cell("cell-race")
    assert stored is not None
    assert stored["allocation_status"] == "allocating"
