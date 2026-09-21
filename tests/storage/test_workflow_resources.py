"""M5-T01：Workflow 版本资源的三种存储语义与降级守卫。

证明发布是不可变的（同内容幂等 / 异内容冲突 / 不可删除）、内容 hash 自洽是
入库条件，并且引用过 Workflow 的 Run 证据会阻止降级删表。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from motte_contracts.workflow import WorkflowVersion, workflow_content_hash
from motte_scenario.compiler import compile_workflow
from motte_storage.migrations import MIGRATIONS_DIR
from motte_storage.resource_store import (
    InMemoryResourceStore,
    ResourceConflictError,
    SQLiteResourceStore,
)

PUBLISHED_AT = "2026-09-21T00:00:00Z"


def workflow_record(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workflow_id": "order-cancel-confirmed",
        "version": "1",
        "published_at": PUBLISHED_AT,
        "steps": [
            {"step_id": "request", "kind": "send_message", "message": "请取消订单 order-1"},
            {"step_id": "before-confirm", "kind": "checkpoint",
             "assertions": [{"op": "eq", "path": "state.order.status", "value": "active"}]},
        ],
        "limits": {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 30},
    }
    payload.update(overrides)
    model = WorkflowVersion.model_validate(payload)
    return {**payload, "content_hash": workflow_content_hash(model)}


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryResourceStore()
    return SQLiteResourceStore(tmp_path / "resources.db")


def test_publish_is_idempotent_for_identical_content(store):
    first = store.publish_workflow(workflow_record())
    second = store.publish_workflow(workflow_record())
    assert first["content_hash"] == second["content_hash"]
    assert store.workflows.get("order-cancel-confirmed", "1")["content_hash"] == first["content_hash"]


def test_publish_conflicts_when_the_same_version_changes_content(store):
    store.publish_workflow(workflow_record())
    with pytest.raises(ResourceConflictError, match="different content"):
        store.publish_workflow(workflow_record(steps=[
            {"step_id": "request", "kind": "send_message", "message": "不同内容"},
        ]))


def test_published_versions_are_immutable_and_cannot_be_deleted(store):
    store.publish_workflow(workflow_record())
    with pytest.raises(ResourceConflictError, match="immutable"):
        store.workflows.delete("order-cancel-confirmed", "1")
    assert store.workflows.get("order-cancel-confirmed", "1") is not None


def test_store_rejects_a_mismatched_content_hash(store):
    record = workflow_record()
    record["content_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="content_hash"):
        store.publish_workflow(record)


def test_store_rejects_draft_lifecycle(store):
    with pytest.raises(ValueError, match="publish immediately"):
        store.publish_workflow(workflow_record(lifecycle="draft"))


def test_published_workflow_resolves_through_the_public_compiler(store):
    store.publish_workflow(workflow_record())
    compiled = compile_workflow(store.workflows.get("order-cancel-confirmed", "1"))
    snapshot = compiled.snapshot_for_run()
    assert snapshot["ref"] == "order-cancel-confirmed@1"
    assert snapshot["content_hash"] == store.workflows.get(
        "order-cancel-confirmed", "1"
    )["content_hash"]


# ------------------------------------------------------------------ 降级守卫


def _load_migration() -> Any:
    path = MIGRATIONS_DIR / "versions" / "0011_workflow_resources.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine_with(tables: dict[str, str], insert: list[str]) -> Any:
    engine = create_engine("sqlite://")
    connection = engine.connect()
    for name, ddl in tables.items():
        connection.execute(text(ddl))
    for statement in insert:
        connection.execute(text(statement))
    connection.commit()
    return engine, connection


def test_downgrade_guard_allows_an_empty_or_unmigrated_database():
    module = _load_migration()
    engine, connection = _engine_with({}, [])
    try:
        assert module.downgrade_blockers(connection) == []
    finally:
        connection.close()
        engine.dispose()


def test_downgrade_guard_reports_published_workflow_versions():
    module = _load_migration()
    engine, connection = _engine_with(
        {"workflow_versions": "CREATE TABLE workflow_versions (workflow_id TEXT, version TEXT, payload TEXT)"},
        ["INSERT INTO workflow_versions VALUES ('wf', '1', '{}')"],
    )
    try:
        blockers = module.downgrade_blockers(connection)
    finally:
        connection.close()
        engine.dispose()
    assert len(blockers) == 1
    assert "workflow_versions" in blockers[0]


def test_downgrade_guard_reports_runs_that_froze_a_workflow_snapshot():
    module = _load_migration()
    engine, connection = _engine_with(
        {
            "workflow_versions": "CREATE TABLE workflow_versions (workflow_id TEXT, version TEXT, payload TEXT)",
            "runs": "CREATE TABLE runs (id TEXT PRIMARY KEY, manifest TEXT)",
        },
        [
            'INSERT INTO runs VALUES (\'run-1\', \'{"execution": {}}\')',
            'INSERT INTO runs VALUES (\'run-2\', \'{"workflow": {"ref": "wf@1"}}\')',
        ],
    )
    try:
        blockers = module.downgrade_blockers(connection)
    finally:
        connection.close()
        engine.dispose()
    assert len(blockers) == 1
    assert "manifest" in blockers[0] and "1" in blockers[0]


def test_downgrade_refuses_instead_of_dropping_evidence():
    module = _load_migration()
    assert module.DOWN_STATEMENTS == ("DROP TABLE IF EXISTS workflow_versions",)
    assert module.down_revision == "0010_interactive_sessions"
    assert Path(MIGRATIONS_DIR / "versions" / "0011_workflow_resources.py").exists()
