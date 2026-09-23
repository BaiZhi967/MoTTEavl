"""R23：``0008_trials`` 降级不得静默删除 Trial 证据。

反例覆盖：Trial 计划/结果或 trial-scoped attempt 仍在时，普通 downgrade 必须
拒绝并给出可执行说明；空库（或未迁移的库）才允许降级；``DOWN_STATEMENTS``
语义不变（测试夹具仍用它逆序清理）。

守卫本身用 SQLAlchemy bind 判定，因此可以用 SQLite 引擎做无 PG 的单元测试；
真实 PostgreSQL 的旧库升级/空库降级/有数据拒绝另由 ``MOTTE_PG_DSN`` 门控测试覆盖。
"""
from __future__ import annotations

import importlib.util
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from sqlalchemy import create_engine, inspect, text

from motte_storage.migrations import revision_ids

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
VERSION_FILE = MIGRATIONS_DIR / "versions" / "0008_trials.py"
HEAD = revision_ids()[-1]
PREVIOUS = "0007_benchmark_datasets"
TRIAL_DOWNGRADE_STEPS = len(revision_ids()) - 1 - revision_ids().index(PREVIOUS)

#: 迁移形状的最小复刻（列名与 0008 一致）；单行字面语句 + 绑定参数。
_TRIALS_DDL = "CREATE TABLE trials (trial_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_key TEXT NOT NULL, repeat_index INTEGER NOT NULL, status TEXT NOT NULL, plan_hash TEXT NOT NULL, payload TEXT NOT NULL, result_payload TEXT, created_at TEXT NOT NULL, finished_at TEXT)"  # noqa: E501
_ATTEMPTS_DDL = "CREATE TABLE case_attempts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, case_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, trial_id TEXT NOT NULL DEFAULT '')"  # noqa: E501
_LEGACY_ATTEMPTS_DDL = "CREATE TABLE case_attempts (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, case_id TEXT NOT NULL, attempt_no INTEGER NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL)"  # noqa: E501
_INSERT_TRIAL = "INSERT INTO trials(trial_id, run_id, task_key, repeat_index, status, plan_hash, payload, created_at) VALUES (:trial_id, 'run-1', 'task-1', 0, 'pending', 'sha256:plan', '{}', 'now')"  # noqa: E501
_INSERT_ATTEMPT = "INSERT INTO case_attempts(id, run_id, case_id, attempt_no, status, revision, payload, trial_id) VALUES (:id, 'run-1', 'case-1', 1, 'prepared', 1, '{}', :trial_id)"  # noqa: E501


def _load_version_module() -> Any:
    spec = importlib.util.spec_from_file_location(VERSION_FILE.stem, VERSION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _guard() -> Any:
    """``downgrade_blockers`` 必须存在，否则普通降级会静默删证据（R23）。"""
    blockers = getattr(_load_version_module(), "downgrade_blockers", None)
    assert callable(blockers), "0008_trials must expose downgrade_blockers(bind)"
    return blockers


@contextmanager
def _fake_bind(
    *, trial_rows: int = 0, trial_attempts: int = 0, plain_attempts: int = 0,
    with_tables: bool = True, with_trial_column: bool = True,
) -> Iterator[Any]:
    """SQLite 引擎上的假 bind：与真实 PG 一样给守卫一个 SQLAlchemy Connection。"""
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            if with_tables:
                connection.execute(text(_TRIALS_DDL))
                connection.execute(text(
                    _ATTEMPTS_DDL if with_trial_column else _LEGACY_ATTEMPTS_DDL
                ))
                for index in range(trial_rows):
                    connection.execute(text(_INSERT_TRIAL), {"trial_id": f"trial-{index}"})
                for index in range(trial_attempts):
                    connection.execute(
                        text(_INSERT_ATTEMPT),
                        {"id": f"attempt-t{index}", "trial_id": f"trial-{index}"},
                    )
                for index in range(plain_attempts):
                    connection.execute(
                        text(_INSERT_ATTEMPT), {"id": f"attempt-p{index}", "trial_id": ""},
                    )
                connection.commit()
            yield connection
    finally:
        engine.dispose()


@contextmanager
def _operations_context(connection: Any) -> Iterator[None]:
    """让迁移模块里的 ``alembic.op`` 代理指向这个连接（不跑任何真实迁移）。"""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with Operations.context(MigrationContext.configure(connection)):
        yield


def test_down_statements_keep_fixture_semantics() -> None:
    """DOWN_STATEMENTS 仍是夹具逆序清理用的三条语句（语义不变）。"""
    module = _load_version_module()
    assert module.DOWN_STATEMENTS == (
        "DROP INDEX IF EXISTS case_attempts_trial_idx",
        "ALTER TABLE case_attempts DROP COLUMN IF EXISTS trial_id",
        "DROP TABLE IF EXISTS trials",
    )


def test_downgrade_blockers_reports_frozen_trials() -> None:
    blockers = _guard()
    with _fake_bind(trial_rows=2) as bind:
        found = blockers(bind)
    assert len(found) == 1
    assert "trials" in found[0] and "2" in found[0]


def test_downgrade_blockers_reports_trial_scoped_attempts() -> None:
    blockers = _guard()
    with _fake_bind(trial_attempts=1, plain_attempts=3) as bind:
        found = blockers(bind)
    assert len(found) == 1
    assert "case_attempts" in found[0] and "1" in found[0]


def test_downgrade_blockers_reports_both_kinds_of_evidence() -> None:
    blockers = _guard()
    with _fake_bind(trial_rows=1, trial_attempts=2) as bind:
        found = blockers(bind)
    assert len(found) == 2
    assert any("trials" in item for item in found)
    assert any("case_attempts" in item for item in found)


def test_downgrade_blockers_allows_empty_and_unmigrated_databases() -> None:
    """空库、未迁移的库、以及没有 trial_id 列的旧 schema 都可以安全降级。"""
    blockers = _guard()
    with _fake_bind(plain_attempts=2) as bind:
        assert blockers(bind) == []
    with _fake_bind(with_tables=False) as bind:
        assert blockers(bind) == []
    with _fake_bind(with_trial_column=False) as bind:
        assert blockers(bind) == []


def test_downgrade_refuses_while_trial_evidence_exists() -> None:
    """downgrade() 有证据时抛 RuntimeError，并写清要导出/清理什么。"""
    module = _load_version_module()
    with _fake_bind(trial_rows=1, trial_attempts=1) as bind:
        with _operations_context(bind):
            with pytest.raises(RuntimeError) as error:
                module.downgrade()
        assert inspect(bind).has_table("trials"), "拒绝降级不得删除任何证据"
    message = str(error.value)
    assert "trials" in message and "case_attempts" in message and "trial_id" in message
    assert "export" in message.lower() and "delete" in message.lower()


def test_downgrade_proceeds_when_no_trial_evidence_exists(monkeypatch) -> None:
    """空库放行：守卫不阻止降级，DOWN_STATEMENTS 逐条执行。"""
    module = _load_version_module()
    monkeypatch.setattr(module, "DOWN_STATEMENTS", (
        "DROP TABLE IF EXISTS trials",
        "DROP TABLE IF EXISTS case_attempts",
    ))
    with _fake_bind(plain_attempts=1) as bind:
        with _operations_context(bind):
            module.downgrade()
        assert not inspect(bind).has_table("trials")
        assert not inspect(bind).has_table("case_attempts")


@pytest.mark.skipif(
    not os.environ.get("MOTTE_PG_DSN"),
    reason="real PostgreSQL required (MOTTE_PG_DSN)",
)
def test_postgres_trials_downgrade_guard(isolated_pg_database: str) -> None:
    """真实 PostgreSQL：旧库升级 → 有证据拒绝降级 → 清空后空库降级 → 回到 head。"""
    from uuid import uuid4

    from motte_contracts.trial import compute_task_key, trial_id_for
    from motte_storage.migrations import current, downgrade, upgrade
    from motte_storage.pg_audit_store import PgTrials

    dsn = isolated_pg_database
    assert upgrade(dsn) == HEAD, "独立空库必须能升级到当前 head"

    import psycopg

    run_id = f"pg-downgrade-{uuid4().hex}"
    task_key = compute_task_key(
        source_id="tb2-sample", dataset_revision="rev-1",
        normalized_relative_path="sample/downgrade", task_content_hash="sha256:d" * 8,
    )
    plan = {
        "trial_id": trial_id_for(
            run_id=run_id, task_key=task_key, repeat_index=0,
            agent_config_hash="sha256:agent", environment_hash="sha256:env",
        ),
        "run_id": run_id, "task_key": task_key, "repeat_index": 0,
        "agent_config_hash": "sha256:agent", "environment_hash": "sha256:env",
    }
    trials = PgTrials(dsn)
    assert [item["status"] for item in trials.create_plans([plan])] == ["created"]

    with pytest.raises(RuntimeError, match="trials"):
        # Cross newer revisions too, so this continues testing the 0008 guard.
        downgrade(dsn, steps=TRIAL_DOWNGRADE_STEPS)
    assert current(dsn) == HEAD, "拒绝降级后版本不变"
    assert trials.get(str(plan["trial_id"])) is not None, "拒绝降级不得删除证据"

    # 清空本测试专用库的 Trial 证据后，空库降级必须放行。
    connection = psycopg.connect(dsn)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM trials")
                cursor.execute("DELETE FROM case_attempts WHERE trial_id <> ''")
    finally:
        connection.close()

    assert downgrade(dsn, steps=TRIAL_DOWNGRADE_STEPS) == PREVIOUS
    try:
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.trials')")
                assert cursor.fetchone()[0] is None, "0008 降级必须删掉 trials 表"
    finally:
        assert upgrade(dsn) == HEAD
