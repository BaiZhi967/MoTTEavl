"""M5-T02/T06 存储语义：Fixture 与 Skill 版本资源的三存储一致性。

证明不可变性（同内容幂等 / 异内容冲突 / 不可删除）、内容 hash 自洽、Skill
的受控弃用转换确实落库，以及引用这些资源的 Run 证据会阻止降级删表。
"""
from __future__ import annotations

import importlib.util
import json
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from motte_contracts.fixture import FixtureSpec, fixture_content_hash
from motte_storage.migrations import MIGRATIONS_DIR
from motte_storage.resource_store import (
    InMemoryResourceStore,
    ResourceConflictError,
    SQLiteResourceStore,
)

PUBLISHED_AT = "2026-09-21T00:00:00Z"


def fixture_record(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "fixture_id": "order-state",
        "version": 1,
        "kind": "json",
        "initial_data": {"order": {"id": "order-1", "status": "active",
                                   "cancellation_count": 0}},
        "allowed_tools": ["orders.get", "orders.cancel"],
        "visible_fields": ["order"],
        "isolation": "per_case",
        "cleanup": "delete_owned",
        "published_at": PUBLISHED_AT,
        "lifecycle": "published",
    }
    payload.update(overrides)
    model = FixtureSpec.model_validate(payload)
    return {**payload, "content_hash": fixture_content_hash(model)}


def skill_record(**overrides: Any) -> dict[str, Any]:
    from motte_skill.versions import SkillVersion, skill_content_hash

    payload: dict[str, Any] = {
        "skill_id": "cancel-helper",
        "version": "1",
        "kind": "instruction",
        "instruction": "取消订单前必须先取得用户确认。",
        "published_at": PUBLISHED_AT,
        "lifecycle": "published",
        "injection_mode": "context-section",
    }
    payload.update(overrides)
    model = SkillVersion.model_validate(payload)
    return {**payload, "content_hash": skill_content_hash(model)}


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryResourceStore()
    return SQLiteResourceStore(tmp_path / "resources.db")


# ------------------------------------------------------------------ Fixture


def test_fixture_publish_is_idempotent_and_conflicts_on_change(store):
    first = store.publish_fixture(fixture_record())
    second = store.publish_fixture(fixture_record())
    assert first["content_hash"] == second["content_hash"]
    with pytest.raises(ResourceConflictError, match="different content"):
        store.publish_fixture(fixture_record(initial_data={
            "order": {"id": "order-1", "status": "cancelled", "cancellation_count": 1},
        }))


def test_fixture_versions_are_immutable(store):
    store.publish_fixture(fixture_record())
    with pytest.raises(ResourceConflictError, match="immutable"):
        store.fixtures.delete("order-state", "1")


def test_fixture_content_hash_must_be_self_consistent(store):
    record = fixture_record()
    record["content_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="content_hash"):
        store.publish_fixture(record)


# -------------------------------------------------------------------- Skill


def test_skill_publish_is_idempotent_and_conflicts_on_change(store):
    store.publish_skill(skill_record())
    store.publish_skill(skill_record())
    with pytest.raises(ResourceConflictError, match="different content"):
        store.publish_skill(skill_record(instruction="完全不同的指令"))


def test_skill_deprecation_is_the_only_allowed_transition_and_it_persists(store):
    from motte_skill.versions import deprecate_skill

    published = store.publish_skill(skill_record())
    assert published["lifecycle"] == "published"
    deprecated = deprecate_skill(
        store.skills, "cancel-helper", "1",
        deprecated_at="2026-09-22T00:00:00Z", reason="superseded",
    )
    assert deprecated["lifecycle"] == "deprecated"
    stored = store.skills.get("cancel-helper", "1")
    assert stored["lifecycle"] == "deprecated"
    assert stored["deprecated_reason"] == "superseded"
    # 弃用不改变内容身份：历史 Run 读到的 hash 与字节都不变。
    assert stored["content_hash"] == published["content_hash"]
    # 已弃用的版本不能再被"复活"成发布态。
    with pytest.raises(ResourceConflictError):
        store.publish_skill(skill_record())


def test_skill_versions_are_immutable(store):
    store.publish_skill(skill_record())
    with pytest.raises(ResourceConflictError, match="immutable"):
        store.skills.delete("cancel-helper", "1")


# -------------------------------------------------------------------- 降级


def _load_migration(name: str) -> Any:
    path = MIGRATIONS_DIR / "versions" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind(tables: dict[str, str], inserts: list[str]):
    engine = create_engine("sqlite://")
    connection = engine.connect()
    for ddl in tables.values():
        connection.execute(text(ddl))
    for statement in inserts:
        connection.execute(text(statement))
    connection.commit()
    return engine, connection


def test_migration_follows_the_actual_head_and_exposes_down_statements():
    module = _load_migration("0013_scenario_skill_resources.py")
    assert module.down_revision == "0012_scoring_jobs"
    assert module.DOWN_STATEMENTS == (
        "DROP TABLE IF EXISTS skill_versions",
        "DROP TABLE IF EXISTS fixture_versions",
    )


def test_downgrade_guard_allows_empty_databases():
    module = _load_migration("0013_scenario_skill_resources.py")
    engine, connection = _bind({}, [])
    try:
        assert module.downgrade_blockers(connection) == []
    finally:
        connection.close()
        engine.dispose()


def test_downgrade_guard_reports_resource_rows_and_referencing_runs():
    module = _load_migration("0013_scenario_skill_resources.py")
    engine, connection = _bind(
        {
            "fixture_versions": "CREATE TABLE fixture_versions (fixture_id TEXT, version TEXT, payload TEXT)",
            "skill_versions": "CREATE TABLE skill_versions (skill_id TEXT, version TEXT, payload TEXT)",
            "runs": "CREATE TABLE runs (id TEXT PRIMARY KEY, manifest TEXT)",
        },
        [
            "INSERT INTO fixture_versions VALUES ('order-state', '1', '{}')",
            "INSERT INTO skill_versions VALUES ('cancel-helper', '1', '{}')",
            'INSERT INTO runs VALUES (\'run-1\', \'{"execution": {}}\')',
            'INSERT INTO runs VALUES (\'run-2\', \'{"fixture_snapshot": {}}\')',
        ],
    )
    try:
        blockers = module.downgrade_blockers(connection)
    finally:
        connection.close()
        engine.dispose()
    joined = " | ".join(blockers)
    assert "fixture_versions" in joined
    assert "skill_versions" in joined
    assert "fixture_snapshot" in joined
    assert len(blockers) == 3
