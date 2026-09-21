"""M5-T02：Fixture 所有权、状态隔离与清理（G04/G09、A03/A08）。

这些用例断言的是**真实行为**：受控根下真正落盘的文件、SQLite 里真实的行、
磁盘上的清理证据、被拒绝操作后仍然完好的外部对象。不做“字段存在即通过”
的声明式检查。

Windows 上 os.symlink 需要特权，因此链接攻击用 NTFS junction 复现
（cmd mklink /J），POSIX 上仍用 symlink；两者在受控根里都是链接组件。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from motte_contracts.fixture import (
    CONTROLLED_RESOURCE_KINDS,
    FIXTURE_CLEANUP_POLICIES,
    FIXTURE_ISOLATIONS,
    FixtureSpec,
    fixture_content_hash,
)
from motte_sandbox.workspace import WorkspacePolicyError
from motte_scenario.fixtures import (
    FixtureConflict,
    FixtureError,
    FixtureOwnershipError,
    FixturePrepareError,
    FixtureRuntime,
    FixtureUnavailable,
    load_fixture,
    publish_fixture,
    validate_postgres_test_target,
)
from motte_scenario.state import (
    FIXTURE_INSTANCE_STAGES,
    ControlledRoot,
    minimal_visible_fields,
)

PUBLISHED_AT = "2026-09-21T00:00:00Z"


# ------------------------------------------------------------------ 夹具与工具


def json_spec(**overrides) -> FixtureSpec:
    payload: dict = {
        "fixture_id": "order-state",
        "version": 1,
        "kind": "json",
        "initial_data": {
            "order_id": "order-1",
            "status": "active",
            "expected_status": "cancelled",
            "_checker_truth": {"status": "cancelled"},
        },
        "allowed_tools": ("orders.get", "orders.cancel"),
        "visible_fields": ("order_id", "status"),
        "published_at": PUBLISHED_AT,
    }
    payload.update(overrides)
    return FixtureSpec.model_validate(payload)


def files_spec(files: dict | None = None, **overrides) -> FixtureSpec:
    payload: dict = {
        "fixture_id": "order-files",
        "version": 1,
        "kind": "files",
        "initial_data": {
            "files": {"orders/order-1.json": json.dumps({"status": "active"})},
        },
        "allowed_tools": ("orders.get",),
        "published_at": PUBLISHED_AT,
    }
    if files is not None:
        payload["initial_data"] = {"files": files}
    payload.update(overrides)
    return FixtureSpec.model_validate(payload)


def sqlite_spec(rows: dict | None = None, **overrides) -> FixtureSpec:
    payload: dict = {
        "fixture_id": "order-db",
        "version": 1,
        "kind": "sqlite",
        "initial_data": {
            "schema": [
                "CREATE TABLE orders (id TEXT PRIMARY KEY, status TEXT NOT NULL)"
            ],
            "rows": {"orders": [{"id": "order-1", "status": "active"}]},
        },
        "allowed_tools": ("orders.get", "orders.cancel"),
        "published_at": PUBLISHED_AT,
    }
    if rows is not None:
        payload["initial_data"]["rows"] = rows
    payload.update(overrides)
    return FixtureSpec.model_validate(payload)


@pytest.fixture
def runtime(tmp_path) -> FixtureRuntime:
    return FixtureRuntime(anchor=tmp_path / "fixtures")


def prepare(runtime: FixtureRuntime, spec: FixtureSpec, **overrides):
    payload = {
        "run_id": "run-1",
        "case_id": "case-1",
        "attempt_id": "attempt-1",
        "owner_token": "owner-a",
        "business_id": "order-1",
    }
    payload.update(overrides)
    return runtime.prepare(spec, **payload)


def sqlite_execute(path, statement: str, parameters: tuple = ()) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def sqlite_query(path, statement: str, parameters: tuple = ()) -> list:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(statement, parameters).fetchall()
    finally:
        connection.close()


def make_dir_link(link: Path, target: Path) -> None:
    """建立目录链接：POSIX 用 symlink，Windows 无特权时用 junction。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError, AttributeError) as error:
        if sys.platform != "win32":
            pytest.skip(f"cannot create a directory symlink: {error}")
    link.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create a directory link: {result.stdout}{result.stderr}")


class FakeFixtureStore:
    """duck-typed 仓库：任何有 put(record)/get(*keys) 语义的对象都应可用。"""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict] = {}

    def put(self, record: dict) -> dict:
        key = (str(record["fixture_id"]), str(record["version"]))
        existing = self.records.get(key)
        if existing is not None and existing != record:
            raise ValueError("immutable version: same version with different content")
        self.records[key] = dict(record)
        return dict(record)

    def get(self, *keys) -> dict | None:
        return self.records.get((str(keys[0]), str(keys[1])))


class StoreWithFixtures:
    """模拟 ResourceStore 形状：版本资源挂在 .fixtures 下。"""

    def __init__(self, store: FakeFixtureStore) -> None:
        self.fixtures = store


class SilentlyImmutableStore(FakeFixtureStore):
    """幂等但会静默忽略异内容写入的仓库：运行期必须自己回读发现冲突。"""

    def put(self, record: dict) -> dict:
        key = (str(record["fixture_id"]), str(record["version"]))
        self.records.setdefault(key, dict(record))
        return dict(self.records[key])


def failing_sqlite_spec() -> FixtureSpec:
    """schema 没有 missing_table：初始化执行到一半才失败（不是形状预检拒绝）。"""
    return sqlite_spec(rows={"missing_table": [{"id": "order-1"}]})


# ------------------------------------------------------------- 契约与发布语义


def test_fixture_spec_contract_rejects_drafts_bad_versions_and_hash_drift():
    with pytest.raises(ValidationError):
        json_spec(lifecycle="draft")
    with pytest.raises(ValidationError):
        json_spec(version=0)
    with pytest.raises(ValidationError):
        json_spec(version=True)  # bool 不冒充 int
    with pytest.raises(ValidationError):
        json_spec(content_hash="sha256:deadbeef")
    with pytest.raises(ValidationError):
        json_spec(kind="postgres")
    with pytest.raises(ValidationError):
        json_spec(initial_data={"bad": float("nan")})

    spec = json_spec()
    digest = fixture_content_hash(spec)
    assert digest.startswith("sha256:") and len(digest) == 71
    published = FixtureSpec.model_validate(
        {**spec.model_dump(mode="json"), "content_hash": digest}
    )
    assert published.effective_content_hash() == digest
    drifted = FixtureSpec.model_validate(
        {**spec.model_dump(mode="json"), "content_hash": "sha256:" + "1" * 64}
    )
    with pytest.raises(ValueError, match="does not match"):
        drifted.effective_content_hash()
    changed = json_spec(initial_data={"order_id": "order-1", "status": "cancelled"})
    assert fixture_content_hash(changed) != digest


def test_publish_fixture_works_against_a_duck_typed_repository():
    store = FakeFixtureStore()
    spec = json_spec()
    published = publish_fixture(store, spec)
    assert published.content_hash == fixture_content_hash(spec)
    assert store.get("order-state", 1)["content_hash"] == published.content_hash
    # 同内容幂等：不会产生第二个版本身份
    assert publish_fixture(store, spec).content_hash == published.content_hash
    assert len(store.records) == 1

    loaded = load_fixture(store, "order-state", 1)
    assert loaded.content_hash == published.content_hash
    assert loaded.effective_content_hash() == published.content_hash
    assert loaded.allowed_tools == ("orders.get", "orders.cancel")

    changed = json_spec(initial_data={"order_id": "order-1", "status": "cancelled"})
    # 仓库自身的不可变语义错误必须原样冒泡，不被吞掉
    with pytest.raises(ValueError, match="immutable version"):
        publish_fixture(store, changed)

    # 仓库静默忽略异内容写入时，运行期自己回读发现冲突
    silent = SilentlyImmutableStore()
    publish_fixture(silent, json_spec())
    with pytest.raises(FixtureConflict):
        publish_fixture(silent, changed)

    # .fixtures 间接层（真实 ResourceStore 的形状）同样可用
    wrapped = publish_fixture(
        StoreWithFixtures(store), json_spec(fixture_id="other-state")
    )
    assert wrapped.content_hash
    assert store.get("other-state", 1) is not None

    # 读出被篡改的 hash 必须拒绝，不能“带病运行”
    store.records[("tampered", "1")] = {
        "fixture_id": "tampered",
        "version": 1,
        "kind": "json",
        "initial_data": {},
        "published_at": PUBLISHED_AT,
        "content_hash": "sha256:" + "0" * 64,
    }
    with pytest.raises(FixtureError):
        load_fixture(store, "tampered", 1)
    with pytest.raises(FixtureError):
        load_fixture(store, "missing", 1)


# ------------------------------------------------------------------ 生命周期


def test_prepare_materializes_owned_state_and_marker(runtime):
    instance = prepare(runtime, json_spec())
    root = Path(instance.root)

    assert root.is_dir()
    assert instance.stage == "prepared"
    assert instance.completed_steps == ("marker", "materialize", "manifest")
    assert {resource.kind for resource in instance.resources} >= {
        "instance_marker", "state_file", "private_root",
    }
    assert {resource.kind for resource in instance.resources} <= set(CONTROLLED_RESOURCE_KINDS)
    assert instance.spec.isolation in FIXTURE_ISOLATIONS
    assert instance.spec.cleanup in FIXTURE_CLEANUP_POLICIES
    # 路径安全复用沙箱：POSIX 走 CaseWorkspace 的 fd 链，Windows 无 dir_fd 时
    # 走同一组规则的便携实现。
    root_control = ControlledRoot(Path(instance.root), anchor=runtime.anchor, create=False)
    assert root_control.backend == ("sandbox" if os.name == "posix" else "portable")
    state = json.loads((root / "state" / "state.json").read_text(encoding="utf-8"))
    assert state["order_id"] == "order-1"
    assert state["status"] == "active"

    marker_text = (root / "_instance.json").read_text(encoding="utf-8")
    marker = json.loads(marker_text)
    assert marker["instance_id"] == instance.instance_id
    assert marker["case_id"] == "case-1"
    assert marker["run_id"] == "run-1"
    assert marker["attempt_id"] == "attempt-1"
    assert instance.owner_token not in marker_text  # 只落盘摘要，不落盘 token
    assert runtime.snapshot(instance, owner_token="owner-a").content_hash.startswith("sha256:")


def test_begin_represents_partial_preparation_and_stays_cleanable(runtime):
    instance = runtime.begin(
        json_spec(),
        run_id="run-1",
        case_id="case-1",
        attempt_id="attempt-1",
        owner_token="owner-a",
        business_id="order-1",
    )
    # 半成品实例：受控根与所有权标记已落盘，业务初态还没有
    assert instance.stage == "preparing"
    assert instance.stage in FIXTURE_INSTANCE_STAGES
    assert instance.completed_steps == ("marker",)
    assert Path(instance.root).is_dir()
    assert instance.marker_path.exists()
    assert not instance.state_path.exists()

    prepared = runtime.materialize(instance, owner_token="owner-a")
    assert prepared.stage == "prepared"
    assert prepared.completed_steps == ("marker", "materialize", "manifest")
    assert prepared.state_path.exists()
    with pytest.raises(FixtureOwnershipError):
        runtime.materialize(instance, owner_token="owner-b")

    report = runtime.cleanup(prepared, owner_token="owner-a")
    assert report.status == "success"
    assert not Path(instance.root).exists()


def test_cleanup_policy_retain_keeps_resources_for_review(runtime):
    instance = prepare(runtime, json_spec(cleanup="retain"), owner_token="owner-a")
    report = runtime.cleanup(instance, owner_token="owner-a")

    assert report.status == "unknown"
    assert "retain" in (report.reason or "")
    assert Path(instance.root).exists()
    assert (Path(instance.root) / "state" / "state.json").exists()
    evidence = json.loads(Path(report.evidence_path).read_text(encoding="utf-8"))
    assert evidence["status"] == "unknown"
    assert "retain" in evidence["reason"]


def test_two_cases_with_the_same_business_id_use_independent_sqlite_files(runtime):
    spec = sqlite_spec()
    case_a = prepare(runtime, spec, case_id="case-a", owner_token="owner-a")
    case_b = prepare(runtime, spec, case_id="case-b", owner_token="owner-b")

    assert case_a.business_id == case_b.business_id == "order-1"
    assert case_a.instance_id != case_b.instance_id
    assert case_a.session_scope != case_b.session_scope  # T03 的 TargetSession 以它为键
    assert Path(case_a.root) != Path(case_b.root)
    assert Path(case_a.root) not in Path(case_b.root).parents
    assert Path(case_b.root) not in Path(case_a.root).parents
    assert case_a.sqlite_path != case_b.sqlite_path

    # 快照证明：初态一致，A 被改后 B 不变
    baseline = runtime.snapshot(case_b, owner_token="owner-b").content_hash
    assert runtime.snapshot(case_a, owner_token="owner-a").content_hash == baseline

    sqlite_execute(
        case_a.sqlite_path, "INSERT INTO orders (id, status) VALUES ('order-2', 'cancelled')"
    )
    rows_a = sqlite_query(case_a.sqlite_path, "SELECT id FROM orders ORDER BY id")
    rows_b = sqlite_query(case_b.sqlite_path, "SELECT id FROM orders ORDER BY id")
    assert [row[0] for row in rows_a] == ["order-1", "order-2"]
    assert [row[0] for row in rows_b] == ["order-1"]

    sqlite_execute(
        case_a.sqlite_path, "UPDATE orders SET status = 'cancelled' WHERE id = 'order-1'"
    )
    assert runtime.snapshot(case_a, owner_token="owner-a").content_hash != baseline
    assert runtime.snapshot(case_b, owner_token="owner-b").content_hash == baseline


def test_two_cases_with_the_same_business_id_do_not_share_json_state_or_files(runtime):
    json_case_a = prepare(runtime, json_spec(), case_id="case-json-a", owner_token="owner-a")
    json_case_b = prepare(runtime, json_spec(), case_id="case-json-b", owner_token="owner-b")
    (Path(json_case_a.root) / "state" / "state.json").write_text(
        json.dumps({"order_id": "order-1", "status": "cancelled"}), encoding="utf-8"
    )
    state_b = json.loads(
        (Path(json_case_b.root) / "state" / "state.json").read_text(encoding="utf-8")
    )
    assert state_b["status"] == "active"

    files_case_a = prepare(runtime, files_spec(), case_id="case-files-a", owner_token="owner-a")
    files_case_b = prepare(runtime, files_spec(), case_id="case-files-b", owner_token="owner-b")
    a_files = runtime.list_visible_files(files_case_a, owner_token="owner-a")
    b_files = runtime.list_visible_files(files_case_b, owner_token="owner-b")
    assert a_files == b_files == ["orders/order-1.json"]
    (runtime.visible_root(files_case_a, owner_token="owner-a") / "orders" / "leak.json").write_text(
        "{}", encoding="utf-8"
    )
    assert runtime.list_visible_files(files_case_a, owner_token="owner-a") == [
        "orders/leak.json", "orders/order-1.json",
    ]
    assert runtime.list_visible_files(files_case_b, owner_token="owner-b") == [
        "orders/order-1.json",
    ]


def test_mid_prepare_failure_still_cleans_up(runtime):
    shared = runtime.anchor / "shared"
    shared.mkdir(parents=True)
    (shared / "keep.txt").write_text("shared", encoding="utf-8")

    with pytest.raises(FixturePrepareError) as excinfo:
        prepare(runtime, failing_sqlite_spec())
    error = excinfo.value

    assert error.instance.stage == "failed"
    assert error.instance.completed_steps == ("marker",)  # 中途失败，不是完全没开始
    assert "missing_table" in (error.instance.prepare_error or "")
    assert error.cleanup.status == "success"
    assert "missing_table" in (error.cleanup.original_error or "")
    assert "_instance.json" in error.cleanup.deleted
    assert not Path(error.instance.root).exists()
    assert not Path(error.instance.sqlite_path).exists()

    evidence = json.loads(
        Path(error.cleanup.evidence_path).read_text(encoding="utf-8")
    )
    assert evidence["status"] == "success"
    assert evidence["instance_id"] == error.instance.instance_id
    assert "missing_table" in evidence["original_error"]
    assert (shared / "keep.txt").read_text(encoding="utf-8") == "shared"


def test_cleanup_failure_after_prepare_failure_records_both_errors(runtime, monkeypatch):
    def refuse_to_remove(self, root):
        raise OSError("simulated removal failure")

    monkeypatch.setattr(FixtureRuntime, "_remove_root", refuse_to_remove)

    with pytest.raises(FixturePrepareError) as excinfo:
        prepare(runtime, failing_sqlite_spec())
    report = excinfo.value.cleanup

    assert report.status == "residual"
    assert "missing_table" in (report.original_error or "")
    assert "removal failure" in (report.cleanup_error or "")
    assert report.residual
    assert Path(excinfo.value.instance.root).exists()  # 不伪报回收
    evidence = json.loads(Path(report.evidence_path).read_text(encoding="utf-8"))
    assert evidence["status"] == "residual"
    assert "missing_table" in evidence["original_error"]
    assert "removal failure" in evidence["cleanup_error"]


def test_wrong_owner_token_is_refused_and_changes_nothing(runtime):
    instance = prepare(runtime, json_spec(), owner_token="owner-a")
    before = runtime.snapshot(instance, owner_token="owner-a")

    for call in (
        lambda: runtime.snapshot(instance, owner_token="owner-b"),
        lambda: runtime.reset(instance, owner_token="owner-b"),
        lambda: runtime.cleanup(instance, owner_token="owner-b"),
        lambda: runtime.private_truth(instance, owner_token="owner-b"),
    ):
        with pytest.raises(FixtureOwnershipError) as excinfo:
            call()
        assert excinfo.value.code == "owner_token_mismatch"

    after = runtime.snapshot(instance, owner_token="owner-a")
    assert after.content_hash == before.content_hash
    assert (Path(instance.root) / "state" / "state.json").exists()


def test_reset_refuses_foreign_paths_and_other_cases_resources(runtime, tmp_path):
    spec = sqlite_spec()
    case_a = prepare(runtime, spec, case_id="case-a", owner_token="owner-a")
    case_b = prepare(runtime, spec, case_id="case-b", owner_token="owner-b")
    before_b = runtime.snapshot(case_b, owner_token="owner-b").content_hash

    victim = tmp_path / "victim.sqlite"
    victim.write_bytes(b"user-owned-database")
    foreign_targets = (
        {"path": str(victim)},                                       # 任意绝对路径
        {"path": "../case-a/db/fixture.sqlite"},                     # 穿越
        {"path": str(Path(case_b.root) / "db" / "fixture.sqlite")},  # 别的 Case 的资源
        {"path": "db/other.sqlite"},                                 # 未声明的资源
    )
    for target in foreign_targets:
        with pytest.raises(FixtureError) as excinfo:
            runtime.reset(case_a, owner_token="owner-a", target=target)
        assert excinfo.value.code in {
            "invalid_path", "path_escape", "path_outside_instance", "resource_not_owned",
        }, excinfo.value.code
        assert victim.read_bytes() == b"user-owned-database"

    assert runtime.snapshot(case_b, owner_token="owner-b").content_hash == before_b

    # 自己声明的受控资源可以按路径重置
    reset = runtime.reset(case_a, owner_token="owner-a", target={"path": "db/fixture.sqlite"})
    assert reset.content_hash == runtime.snapshot(case_a, owner_token="owner-a").content_hash
    assert sqlite_query(case_a.sqlite_path, "SELECT COUNT(*) FROM orders")[0][0] == 1


def test_postgres_reset_requires_a_declared_test_instance(runtime):
    instance = prepare(runtime, json_spec(), owner_token="owner-a")
    before = runtime.snapshot(instance, owner_token="owner-a").content_hash

    refusal_cases = (
        (
            {"dsn": "postgresql://motte@db.internal:5432/motte",
             "namespace": "fixture_test", "declared_test": True},
            "not_a_test_instance",
        ),
        (
            {"dsn": "postgresql://motte@db.internal:5432/motte_test",
             "namespace": "fixture_test", "declared_test": False},
            "test_declaration_required",
        ),
        (
            {"dsn": "postgresql://motte@db.internal:5432/motte_test",
             "namespace": "public", "declared_test": True},
            "namespace_not_separate",
        ),
        (
            {"dsn": "postgresql://motte@db.internal:5432/motte_prod_live",
             "namespace": "fixture_test", "declared_test": True},
            "production_instance",
        ),
    )
    for target, code in refusal_cases:
        with pytest.raises(FixtureUnavailable) as excinfo:
            runtime.reset(instance, owner_token="owner-a", target=target)
        assert excinfo.value.code == code, (target, excinfo.value.code)

    # 显式测试实例 + 独立 namespace 通过 gate，但首批没有 PG 后端 → 仍然 unavailable
    declared = {
        "dsn": "postgresql://motte@127.0.0.1:5432/motte_test",
        "namespace": "fixture_test",
        "declared_test": True,
    }
    verdict = validate_postgres_test_target(declared)
    assert verdict.available is True
    with pytest.raises(FixtureUnavailable) as excinfo:
        runtime.reset(instance, owner_token="owner-a", target=declared)
    assert excinfo.value.code == "postgres_backend_not_implemented"

    # 被拒绝的 DSN 从未被触碰：状态 hash 与初态一致
    assert runtime.snapshot(instance, owner_token="owner-a").content_hash == before


def test_snapshot_is_deterministic_and_reset_restores_initial_state(runtime):
    spec = json_spec()
    first = prepare(runtime, spec, case_id="case-a", owner_token="owner-a")
    snapshot = runtime.snapshot(first, owner_token="owner-a")
    assert snapshot.content_hash == runtime.snapshot(first, owner_token="owner-a").content_hash
    assert snapshot.payload == runtime.snapshot(first, owner_token="owner-a").payload

    twin = prepare(runtime, spec, case_id="case-b", owner_token="owner-a")
    assert runtime.snapshot(twin, owner_token="owner-a").content_hash == snapshot.content_hash

    state_path = Path(first.root) / "state" / "state.json"
    state_path.write_text(
        json.dumps({"status": "cancelled", "order_id": "order-1"}), encoding="utf-8"
    )
    assert runtime.snapshot(first, owner_token="owner-a").content_hash != snapshot.content_hash

    restored = runtime.reset(first, owner_token="owner-a")
    assert restored.content_hash == snapshot.content_hash
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "active"


def test_file_and_sqlite_snapshots_are_reproducible(runtime):
    files_a = prepare(runtime, files_spec(), case_id="files-a", owner_token="owner-a")
    files_b = prepare(runtime, files_spec(), case_id="files-b", owner_token="owner-b")
    hash_a = runtime.snapshot(files_a, owner_token="owner-a").content_hash
    assert runtime.snapshot(files_a, owner_token="owner-a").content_hash == hash_a
    assert runtime.snapshot(files_b, owner_token="owner-b").content_hash == hash_a
    (runtime.visible_root(files_a, owner_token="owner-a") / "orders" / "order-1.json").write_text(
        json.dumps({"status": "cancelled"}), encoding="utf-8"
    )
    assert runtime.snapshot(files_a, owner_token="owner-a").content_hash != hash_a
    assert runtime.snapshot(files_b, owner_token="owner-b").content_hash == hash_a

    db_a = prepare(runtime, sqlite_spec(), case_id="db-a", owner_token="owner-a")
    db_b = prepare(runtime, sqlite_spec(), case_id="db-b", owner_token="owner-b")
    db_hash = runtime.snapshot(db_a, owner_token="owner-a").content_hash
    assert runtime.snapshot(db_a, owner_token="owner-a").content_hash == db_hash
    assert runtime.snapshot(db_b, owner_token="owner-b").content_hash == db_hash
    sqlite_execute(db_a.sqlite_path, "INSERT INTO orders VALUES ('order-9', 'active')")
    assert runtime.snapshot(db_a, owner_token="owner-a").content_hash != db_hash
    assert runtime.snapshot(db_b, owner_token="owner-b").content_hash == db_hash


def test_private_truth_is_not_visible_to_the_target(runtime):
    instance = prepare(
        runtime,
        files_spec({"orders/order-1.json": json.dumps({"status": "active"})}),
        owner_token="owner-a",
    )
    truth = runtime.private_truth(instance, owner_token="owner-a")
    truth.write("gold/order-1.expected.json", json.dumps({"status": "cancelled"}))
    truth_path = Path(instance.root) / "private" / "gold" / "order-1.expected.json"
    assert truth_path.exists()
    assert truth.list() == ["gold/order-1.expected.json"]
    assert json.loads(truth.read("gold/order-1.expected.json"))["status"] == "cancelled"

    visible_root = runtime.visible_root(instance, owner_token="owner-a")
    assert visible_root not in truth_path.parents
    assert visible_root.resolve() not in truth_path.resolve().parents
    assert runtime.list_visible_files(instance, owner_token="owner-a") == [
        "orders/order-1.json",
    ]
    for path in visible_root.rglob("*"):
        assert "gold" not in path.parts
        assert "private" not in path.parts
    with pytest.raises(WorkspacePolicyError):
        truth.write("../escape.json", "x")

    # 业务可见结果只带最小字段；未授权工具直接拒绝
    json_instance = prepare(
        runtime, json_spec(), case_id="case-visible", owner_token="owner-a"
    )
    result = runtime.visible_tool_result(
        json_instance, owner_token="owner-a", tool="orders.get"
    )
    assert result == {"order_id": "order-1", "status": "active"}
    assert "_checker_truth" not in result and "expected_status" not in result
    with pytest.raises(FixtureError) as excinfo:
        runtime.visible_tool_result(
            json_instance, owner_token="owner-a", tool="orders.delete"
        )
    assert excinfo.value.code == "tool_not_allowed"

    payload = {
        "order_id": "order-1",
        "status": "active",
        "_checker_truth": "cancelled",
        "expected_status": "cancelled",
        "hidden_assertions": [{"op": "eq"}],
    }
    assert minimal_visible_fields(payload) == {"order_id": "order-1", "status": "active"}
    assert minimal_visible_fields(payload, allowed=("order_id",)) == {"order_id": "order-1"}
    # 即使白名单写错，隐藏字段也不会被带出
    assert minimal_visible_fields(
        payload, allowed=("order_id", "_checker_truth", "expected_status")
    ) == {"order_id": "order-1"}


def test_cleanup_removes_only_owned_resources(runtime):
    shared = runtime.anchor / "shared"
    shared.mkdir(parents=True)
    (shared / "keep.txt").write_text("shared", encoding="utf-8")
    case_a = prepare(runtime, json_spec(), case_id="case-a", owner_token="owner-a")
    case_b = prepare(runtime, json_spec(), case_id="case-b", owner_token="owner-b")

    report = runtime.cleanup(case_a, owner_token="owner-a")
    assert report.status == "success"
    assert "_instance.json" in report.deleted
    assert not Path(case_a.root).exists()
    assert (shared / "keep.txt").read_text(encoding="utf-8") == "shared"
    assert runtime.anchor.is_dir()
    evidence = json.loads(Path(report.evidence_path).read_text(encoding="utf-8"))
    assert evidence["status"] == "success"
    assert evidence["deleted"] == list(report.deleted)

    # 兄弟 Case 的资源完好
    assert Path(case_b.root).exists()
    assert (Path(case_b.root) / "state" / "state.json").exists()
    assert runtime.snapshot(case_b, owner_token="owner-b").content_hash.startswith("sha256:")

    # 幂等清理：不误删别的资源，也不伪造删除清单
    second = runtime.cleanup(case_a, owner_token="owner-a")
    assert second.status == "success"
    assert second.deleted == ()
    assert runtime.snapshot(case_b, owner_token="owner-b").content_hash.startswith("sha256:")


def test_cleanup_refuses_a_linked_root_and_retains_resources(runtime, tmp_path):
    instance = prepare(runtime, json_spec(), owner_token="owner-a")
    root = Path(instance.root)
    moved = root.with_name(root.name + "-moved")
    root.rename(moved)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "victim.txt").write_text("victim", encoding="utf-8")
    make_dir_link(root, victim)

    report = runtime.cleanup(instance, owner_token="owner-a")
    assert report.status == "unknown"
    assert report.residual
    assert (victim / "victim.txt").read_text(encoding="utf-8") == "victim"
    assert (moved / "state" / "state.json").exists()  # 保留供复核
    evidence = json.loads(Path(report.evidence_path).read_text(encoding="utf-8"))
    assert evidence["status"] == "unknown"
    assert evidence["reason"]

    for call in (
        lambda: runtime.snapshot(instance, owner_token="owner-a"),
        lambda: runtime.reset(instance, owner_token="owner-a"),
    ):
        with pytest.raises(FixtureOwnershipError):
            call()
    assert (victim / "victim.txt").read_text(encoding="utf-8") == "victim"


def test_cleanup_refuses_a_link_inside_the_owned_tree(runtime, tmp_path):
    instance = prepare(runtime, json_spec(), owner_token="owner-a")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "victim.txt").write_text("victim", encoding="utf-8")
    make_dir_link(Path(instance.root) / "state" / "linked", victim)

    report = runtime.cleanup(instance, owner_token="owner-a")
    assert report.status in {"unknown", "residual"}
    assert (victim / "victim.txt").read_text(encoding="utf-8") == "victim"
    assert Path(instance.root).exists()


def test_interrupted_cleanup_retains_resources_for_review(runtime):
    instance = prepare(runtime, json_spec(), owner_token="owner-a")
    report = runtime.cleanup(instance, owner_token="owner-a", interrupted=True)

    assert report.status == "unknown"
    assert report.interrupted is True
    assert Path(instance.root).exists()
    assert (Path(instance.root) / "state" / "state.json").exists()
    assert report.residual
    evidence = json.loads(Path(report.evidence_path).read_text(encoding="utf-8"))
    assert evidence["interrupted"] is True
    assert evidence["status"] == "unknown"
    assert "interrupt" in evidence["reason"]


def test_prepare_refuses_undeclared_isolation_and_external_sqlite_addresses(runtime):
    with pytest.raises(FixtureError) as excinfo:
        prepare(runtime, json_spec(isolation="per_run"))
    assert excinfo.value.code == "isolation_unsupported"

    # fixture 不能声明外部数据库地址：SQLite 只在自己实例根下创建
    external = sqlite_spec()
    external = FixtureSpec.model_validate(
        {
            **external.model_dump(mode="json"),
            "initial_data": {"path": str(Path("C:/tmp/user.sqlite"))},
        }
    )
    with pytest.raises(FixtureError) as excinfo:
        prepare(runtime, external)
    assert excinfo.value.code == "unexpected_initial_data_field"
