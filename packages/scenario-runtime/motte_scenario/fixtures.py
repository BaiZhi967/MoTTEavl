"""M5-T02：Fixture 所有权、状态隔离与清理。

FixtureRuntime 负责一个 CaseAttempt 的 Fixture 生命周期：
begin → materialize（= prepare）→（运行期读写）→ snapshot / reset → cleanup。
它不新建资源仓库表，也不认识存储实现：发布走 duck-typed 的
put(record) / get(*keys) 语义（.fixtures 间接层与裸仓库都支持），存储接线由
后续工作包在真实 ResourceStore 上完成。

硬约束（与 M5 执行计划 M5-T02 一致）：

* 每个实例一个受控根：json 状态文件、受控文件树、独立 SQLite 文件都在该根
  之下；两个 Case 即使业务 ID 相同也各有实例根与数据库文件，绝无共享。
* 每个操作先校验 owner token 摘要与受控根归属：root 必须仍在 anchor 之下、
  解析后不逃逸、且没有链接组件；reset 不接受任意 DSN、任意路径或别的 Case
  的资源。
* PostgreSQL 只接受显式声明的测试实例 + 独立 namespace；条件不成立时报告
  unavailable，绝不重置任意用户 DSN（首批也没有 PG 后端，因此宁可不可用）。
* snapshot 用稳定序列化产出内容 hash：同一状态必然同一 hash。
* cleanup 区分 success / residual / unknown：只删除校验过的自身资源，不清空
  公共目录；中断或归属无法证明时保留资源供复核，并把原始异常与清理异常一起
  落盘成证据。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from motte_contracts.fixture import (
    ControlledResource,
    FixtureDataError,
    FixtureSpec,
    fixture_content_hash,
)
from motte_sandbox.workspace import WorkspacePolicyError, validate_relative_path

from .state import (
    ControlledRoot,
    FixtureInstance,
    PrivateTruth,
    StateSnapshot,
    init_sqlite_state,
    is_link_path,
    list_sqlite_tables,
    minimal_visible_fields,
    owner_token_digest,
    read_json_state,
    snapshot_state,
    write_json_state,
)

# --------------------------------------------------------------------- 错误


class FixtureError(ValueError):
    """Fixture 生命周期错误；带稳定 code 供分类与断言。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FixtureOwnershipError(FixtureError):
    """owner token 或受控根归属不成立：操作被拒绝，且不修改任何资源。"""


class FixturePolicyError(FixtureError):
    """路径 / 目标 / 工具政策拒绝。"""


class FixtureUnavailable(FixtureError):
    """条件无法证明（例如非测试 PG 实例）：报告 unavailable，不尝试执行。"""


class FixtureConflict(FixtureError):
    """同版本异内容；已发布版本不可覆盖。"""


class FixturePrepareError(RuntimeError):
    """prepare 失败；携带失败的实例与自动清理证据（A03）。"""

    code = "prepare_failed"

    def __init__(
        self,
        message: str,
        *,
        instance: FixtureInstance,
        cleanup: "CleanupReport",
        original: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.instance = instance
        self.cleanup = cleanup
        self.original = original


# ----------------------------------------------------------- PostgreSQL gate

POSTGRES_SYSTEM_SCHEMAS = frozenset(
    {"public", "pg_catalog", "information_schema", "pg_toast", "pg_temp"}
)
#: 数据库名 / namespace 必须带显式测试标记；生产实例一律不可用。
_TEST_NAME_MARKER = re.compile(r"(^|[_-])(test|tests|ci|sandbox|ephemeral|fixture)([_-]|$)")
_PRODUCTION_NAME_MARKER = re.compile(r"(prod|production|live|staging)")
_NAMESPACE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_POSTGRES_SCHEMES = ("postgres", "postgresql")


@dataclass(frozen=True)
class PostgresTestTarget:
    """显式声明的 PG 测试目标：测试实例 DSN + 独立 namespace。"""

    dsn: str
    namespace: str
    declared_test: bool = False


@dataclass(frozen=True)
class PostgresTestVerdict:
    available: bool
    code: str
    reason: str


def validate_postgres_test_target(target: PostgresTestTarget | Mapping[str, Any]) -> PostgresTestVerdict:
    """证明“显式测试实例 + 独立 namespace”；无法证明即 unavailable。

    只做静态证明，不连接、不探测。生产标记（prod / live / staging）直接拒绝：
    不允许把生产 DSN “临时改名”后重置。
    """
    if isinstance(target, Mapping):
        target = PostgresTestTarget(
            dsn=str(target.get("dsn") or ""),
            namespace=str(target.get("namespace") or ""),
            declared_test=bool(target.get("declared_test", False)),
        )
    if not target.declared_test:
        return PostgresTestVerdict(
            False,
            "test_declaration_required",
            "PostgreSQL fixtures require an explicit declared_test=true declaration "
            "from the operator; a DSN alone is never proof of a test instance",
        )
    dsn = (target.dsn or "").strip()
    if not dsn:
        return PostgresTestVerdict(False, "dsn_invalid", "PostgreSQL DSN is required")
    parsed = urlsplit(dsn)
    if parsed.scheme.split("+")[0].lower() not in _POSTGRES_SCHEMES:
        return PostgresTestVerdict(
            False, "dsn_invalid", f"not a PostgreSQL DSN: scheme {parsed.scheme!r}"
        )
    database = parsed.path.lstrip("/")
    if not database:
        return PostgresTestVerdict(False, "dsn_invalid", "PostgreSQL DSN must name a database")
    lowered = database.lower()
    if _PRODUCTION_NAME_MARKER.search(lowered):
        return PostgresTestVerdict(
            False,
            "production_instance",
            f"database {database!r} carries a production marker; refusing to reset it",
        )
    if _TEST_NAME_MARKER.search(lowered) is None:
        return PostgresTestVerdict(
            False,
            "not_a_test_instance",
            f"database {database!r} carries no explicit test marker "
            "(test / ci / sandbox / ephemeral / fixture)",
        )
    namespace = (target.namespace or "").strip()
    if not namespace:
        return PostgresTestVerdict(
            False, "namespace_required", "a separate explicit namespace is required"
        )
    if _NAMESPACE.match(namespace) is None:
        return PostgresTestVerdict(
            False, "namespace_invalid", f"namespace is not a plain identifier: {namespace!r}"
        )
    if namespace.lower() in POSTGRES_SYSTEM_SCHEMAS:
        return PostgresTestVerdict(
            False,
            "namespace_not_separate",
            f"namespace {namespace!r} is a shared/system namespace, not a separate one",
        )
    if _TEST_NAME_MARKER.search(namespace.lower()) is None:
        return PostgresTestVerdict(
            False,
            "namespace_not_test",
            f"namespace {namespace!r} carries no explicit test marker",
        )
    return PostgresTestVerdict(
        True, "available", f"declared test instance with separate namespace {namespace!r}"
    )


def require_postgres_test_target(target: PostgresTestTarget | Mapping[str, Any]) -> str:
    """gate 通过返回 namespace；否则抛 FixtureUnavailable（绝不触碰该 DSN）。"""
    verdict = validate_postgres_test_target(target)
    if not verdict.available:
        raise FixtureUnavailable(verdict.code, verdict.reason)
    if isinstance(target, Mapping):
        return str(target.get("namespace") or "")
    return target.namespace


# ------------------------------------------------------------- reset target


@dataclass(frozen=True)
class ResetTarget:
    """reset 的显式目标：自身受控资源，或一个被 gate 检查的 PG 目标。"""

    kind: Literal["resource", "postgres"]
    path: str | None = None
    dsn: str | None = None
    namespace: str | None = None
    declared_test: bool = False


def _coerce_reset_target(target: Any) -> ResetTarget:
    if isinstance(target, ResetTarget):
        return target
    if isinstance(target, str):
        if "://" in target:
            return ResetTarget(kind="postgres", dsn=target)
        return ResetTarget(kind="resource", path=target)
    if isinstance(target, Mapping):
        unknown = sorted(set(target) - {"path", "dsn", "namespace", "declared_test"})
        if unknown:
            raise FixturePolicyError(
                "reset_target_invalid",
                "reset target accepts path / dsn / namespace / declared_test; unexpected: "
                + ", ".join(str(item) for item in unknown),
            )
        if "dsn" in target:
            return ResetTarget(
                kind="postgres",
                dsn=str(target["dsn"]),
                namespace=str(target["namespace"]) if target.get("namespace") else None,
                declared_test=bool(target.get("declared_test", False)),
            )
        if "path" in target:
            return ResetTarget(kind="resource", path=str(target["path"]))
    raise FixturePolicyError(
        "reset_target_invalid",
        "reset target must be a relative controlled path or an explicit PostgreSQL target",
    )


# ------------------------------------------------------------ cleanup 证据


@dataclass(frozen=True)
class CleanupReport:
    """清理结果：success / residual / unknown，附原始异常与清理异常。"""

    instance_id: str
    status: Literal["success", "residual", "unknown"]
    deleted: tuple[str, ...] = ()
    residual: tuple[str, ...] = ()
    evidence_path: str = ""
    reason: str | None = None
    original_error: str | None = None
    cleanup_error: str | None = None
    interrupted: bool = False
    recorded_at: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "status": self.status,
            "deleted": list(self.deleted),
            "residual": list(self.residual),
            "reason": self.reason,
            "original_error": self.original_error,
            "cleanup_error": self.cleanup_error,
            "interrupted": self.interrupted,
            "recorded_at": self.recorded_at,
        }


def _error_text(error: Any) -> str | None:
    if error is None:
        return None
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    return str(error)


def _initial_data(spec: FixtureSpec) -> dict[str, Any]:
    try:
        return spec.normalized_initial_data()
    except FixtureDataError as error:
        raise FixturePolicyError(error.code, str(error)) from error


def _coerce_spec(record: Any) -> FixtureSpec:
    if isinstance(record, FixtureSpec):
        return record
    if isinstance(record, Mapping):
        try:
            return FixtureSpec.model_validate(dict(record))
        except Exception as error:
            raise FixtureError("fixture_invalid", f"invalid fixture record: {error}") from error
    raise FixtureError("fixture_invalid", "fixture must be a FixtureSpec or a mapping")


# -------------------------------------------------------------- 发布与读取


def _fixture_store(repository: Any) -> Any:
    """duck-typed 仓库：有 .fixtures 就用它，否则仓库对象本身。"""
    store = getattr(repository, "fixtures", None)
    return store if store is not None else repository


def publish_fixture(repository: Any, spec: FixtureSpec | Mapping[str, Any]) -> FixtureSpec:
    """把一条 Fixture 记录发布进 duck-typed 仓库，并回读校验内容身份。

    只要求仓库有 put(record) / get(*keys) 语义；同版本同内容幂等，同版本异
    内容由仓库的不可变语义拒绝（本函数也会在回读 hash 不一致时具名冲突）。
    """
    record = _coerce_spec(spec)
    if record.lifecycle != "published":
        raise FixtureError(
            "fixture_not_published", f"fixture {record.ref} is {record.lifecycle!r}"
        )
    digest = fixture_content_hash(record)
    if record.content_hash is not None and record.content_hash != digest:
        raise FixtureConflict(
            "fixture_content_hash_mismatch",
            f"fixture {record.ref} content_hash does not match its content",
        )
    published = record.model_copy(update={"content_hash": digest})
    store = _fixture_store(repository)
    put = getattr(store, "put", None)
    if not callable(put):
        raise FixtureError(
            "fixture_store_invalid", "the fixture repository must expose put(record)/get(*keys)"
        )
    put(published.model_dump(mode="json"))
    stored = load_fixture(repository, published.fixture_id, published.version)
    if stored.content_hash != digest:
        raise FixtureConflict(
            "fixture_version_conflict",
            f"fixture {published.ref} already exists with different content "
            f"({stored.content_hash} != {digest})",
        )
    return published


def load_fixture(repository: Any, fixture_id: str, version: int) -> FixtureSpec:
    """读取一条已发布 Fixture 并校验内容 hash 自洽；失败即具名拒绝。"""
    store = _fixture_store(repository)
    get = getattr(store, "get", None)
    if not callable(get):
        raise FixtureError(
            "fixture_store_invalid", "the fixture repository must expose get(*keys)"
        )
    record = get(fixture_id, str(version))
    if record is None:
        raise FixtureError(
            "fixture_not_found", f"fixture {fixture_id}@{version} is not published"
        )
    try:
        spec = FixtureSpec.model_validate(dict(record))
    except Exception as error:
        raise FixtureError(
            "fixture_record_invalid", f"fixture {fixture_id}@{version} record is invalid: {error}"
        ) from error
    if spec.lifecycle != "published":
        raise FixtureError(
            "fixture_not_published", f"fixture {spec.ref} is {spec.lifecycle!r}"
        )
    digest = fixture_content_hash(spec)
    if spec.content_hash != digest:
        raise FixtureError(
            "fixture_content_hash_mismatch",
            f"fixture {spec.ref} content hash drift: recorded {spec.content_hash} "
            f"but content hashes to {digest}",
        )
    return spec


# ------------------------------------------------------------------ 运行期


class FixtureRuntime:
    """一个 anchor 之下的 Fixture 生命周期；实例彼此独立，绝不共享根。"""

    def __init__(self, *, anchor: str | Path, clock: Any = None) -> None:
        self.anchor = Path(anchor)
        self._clock = clock or _utc_now

    # ------------------------------------------------------------- 身份

    @property
    def evidence_dir(self) -> Path:
        return self.anchor / "_cleanup"

    def _instance_root(self, spec: FixtureSpec, run_id: str, case_id: str, attempt_id: str) -> tuple[str, Path]:
        digest = hashlib.sha256(
            "\x1f".join(
                (spec.fixture_id, str(spec.version), run_id, case_id, attempt_id)
            ).encode("utf-8")
        ).hexdigest()[:16]
        instance_id = f"{spec.fixture_id}-v{spec.version}-{digest}"
        return instance_id, self.anchor / "instances" / instance_id

    def begin(
        self,
        spec: FixtureSpec | Mapping[str, Any],
        *,
        run_id: str,
        case_id: str,
        attempt_id: str,
        owner_token: str,
        business_id: str | None = None,
    ) -> FixtureInstance:
        """建立实例身份与受控根，写入所有权标记；不做任何业务初始化。"""
        record = _coerce_spec(spec)
        if record.lifecycle != "published":
            raise FixtureError(
                "fixture_not_published", f"fixture {record.ref} is {record.lifecycle!r}"
            )
        for name, value in (("run_id", run_id), ("case_id", case_id), ("attempt_id", attempt_id)):
            if not isinstance(value, str) or not value.strip():
                raise FixturePolicyError(
                    "identity_incomplete", f"{name} must be a non-empty string"
                )
        if not isinstance(owner_token, str) or not owner_token:
            raise FixtureOwnershipError("owner_token_required", "an owner token is required")
        if record.isolation != "per_case":
            # 共享实例根需要显式租约协议（谁可重置、谁可清理、并发使用如何判定）。
            # 在本包没有该协议之前具名拒绝，绝不允许两个 Case 悄悄共享。
            raise FixturePolicyError(
                "isolation_unsupported",
                f"fixture {record.ref} declares isolation={record.isolation!r}; "
                "per-run shared fixtures need an explicit lease protocol and are not "
                "supported here, so cases never share a fixture root",
            )
        _initial_data(record)  # 形状非法时在落盘之前拒绝

        instance_id, root = self._instance_root(record, run_id, case_id, attempt_id)
        ControlledRoot(root, anchor=self.anchor)  # 复用沙箱规则的建链与归属校验
        instance = FixtureInstance(
            instance_id=instance_id,
            run_id=run_id,
            case_id=case_id,
            attempt_id=attempt_id,
            owner_token=owner_token,
            root=root,
            spec=record,
            business_id=business_id,
            stage="preparing",
            completed_steps=("marker",),
            resources=(
                ControlledResource(
                    kind="instance_marker", path="_instance.json", purpose="ownership"
                ),
            ),
            created_at=self._clock(),
        )
        if instance.marker_path.exists():
            existing = json.loads(instance.marker_path.read_text(encoding="utf-8"))
            if existing.get("owner_token_sha256") != owner_token_digest(owner_token):
                raise FixtureOwnershipError(
                    "owner_token_mismatch",
                    f"instance {instance_id} already exists under a different owner token",
                )
        try:
            write_json_state(instance.marker_path, instance.marker_record())
        except BaseException:
            # 标记写失败也必须回收刚建立的受控根，避免留下无归属目录
            ControlledRoot(root, anchor=self.anchor, create=False).remove_tree()
            raise
        return instance

    def materialize(self, instance: FixtureInstance, *, owner_token: str) -> FixtureInstance:
        """按 kind 写入业务初态（json 状态 / 文件树 / 独立 SQLite 文件）。"""
        self._check_owner_token(instance, owner_token)
        controlled = ControlledRoot(instance.root, anchor=self.anchor, create=False)
        spec = instance.spec
        resources = [resource for resource in instance.resources if resource.kind == "instance_marker"]
        if spec.kind == "json":
            write_json_state(instance.state_path, spec.initial_data)
            resources.append(
                ControlledResource(
                    kind="state_file", path="state/state.json", purpose="fixture_state"
                )
            )
        elif spec.kind == "files":
            data = _initial_data(spec)
            for relative, content in data["files"].items():
                controlled.write_text(f"files/{relative}", content)
            instance.files_root.mkdir(parents=True, exist_ok=True)
            resources.append(
                ControlledResource(kind="directory", path="files", purpose="fixture_files")
            )
        else:
            data = _initial_data(spec)
            init_sqlite_state(instance.sqlite_path, data)
            resources.append(
                ControlledResource(
                    kind="sqlite_file", path="db/fixture.sqlite", purpose="fixture_database"
                )
            )
        instance.private_root.mkdir(parents=True, exist_ok=True)
        resources.append(
            ControlledResource(kind="private_root", path="private", purpose="private_truth")
        )
        return instance.advance(
            stage="prepared",
            completed_steps=(*instance.completed_steps, "materialize", "manifest"),
            resources=tuple(resources),
        )

    def prepare(
        self,
        spec: FixtureSpec | Mapping[str, Any],
        *,
        run_id: str,
        case_id: str,
        attempt_id: str,
        owner_token: str,
        business_id: str | None = None,
    ) -> FixtureInstance:
        """begin + materialize；中途失败仍然进入清理并留下证据（A03）。"""
        instance = self.begin(
            spec,
            run_id=run_id,
            case_id=case_id,
            attempt_id=attempt_id,
            owner_token=owner_token,
            business_id=business_id,
        )
        try:
            return self.materialize(instance, owner_token=owner_token)
        except BaseException as error:
            failed = instance.advance(
                stage="failed", prepare_error=f"{type(error).__name__}: {error}"
            )
            report = self._cleanup_after_failure(failed, owner_token=owner_token, error=error)
            raise FixturePrepareError(
                str(error), instance=failed, cleanup=report, original=error
            ) from error

    # ------------------------------------------------------------- 快照

    def snapshot(self, instance: FixtureInstance, *, owner_token: str) -> StateSnapshot:
        """稳定序列化的状态快照；同一状态必然同一内容 hash。"""
        self._require_owned_root(instance, owner_token)
        try:
            return snapshot_state(
                instance.spec.kind,
                root=instance.root,
                state_path=instance.state_path,
                files_root=instance.files_root,
                sqlite_path=instance.sqlite_path,
            )
        except FileNotFoundError as error:
            raise FixturePolicyError(
                "state_unreadable", f"fixture state is missing: {error}"
            ) from error
        except WorkspacePolicyError as error:
            raise FixturePolicyError(error.code, str(error)) from error

    # ------------------------------------------------------------- 重置

    def reset(
        self,
        instance: FixtureInstance,
        *,
        owner_token: str,
        target: Any = None,
    ) -> StateSnapshot:
        """重置自身初态；只接受受控根内的已声明资源或通过 gate 的 PG 目标。"""
        self._require_owned_root(instance, owner_token)
        if target is None:
            self._reset_owned_kind(instance)
            return self.snapshot(instance, owner_token=owner_token)
        requested = _coerce_reset_target(target)
        if requested.kind == "postgres":
            require_postgres_test_target(
                PostgresTestTarget(
                    dsn=requested.dsn or "",
                    namespace=requested.namespace or "",
                    declared_test=requested.declared_test,
                )
            )
            # gate 通过也只说明“这是一个显式测试目标”；本包没有 PG 后端，
            # 于是继续报告 unavailable，而不是去重置一个真实数据库。
            raise FixtureUnavailable(
                "postgres_backend_not_implemented",
                "PostgreSQL fixtures are not materialized in this work package; the "
                "declared test target was accepted by the gate but nothing was touched",
            )
        relative = self._declared_resource_path(instance, requested.path or "")
        self._reset_resource(instance, relative)
        return self.snapshot(instance, owner_token=owner_token)

    def _declared_resource_path(self, instance: FixtureInstance, relative: str) -> str:
        try:
            validate_relative_path(relative)
        except WorkspacePolicyError as error:
            raise FixturePolicyError(error.code, str(error)) from error
        declared = {resource.path for resource in instance.resources}
        if relative not in declared:
            raise FixturePolicyError(
                "resource_not_owned",
                f"{relative!r} is not a declared controlled resource of "
                f"{instance.instance_id}; reset never touches foreign paths",
            )
        return relative

    def _reset_owned_kind(self, instance: FixtureInstance) -> None:
        if instance.spec.kind == "json":
            write_json_state(instance.state_path, instance.spec.initial_data)
            return
        if instance.spec.kind == "files":
            self._reset_resource(instance, "files")
            return
        if instance.spec.kind == "sqlite":
            self._reset_resource(instance, "db/fixture.sqlite")
            return
        raise FixturePolicyError(
            "fixture_kind_unsupported", f"fixture kind {instance.spec.kind!r} cannot be reset"
        )

    def _reset_resource(self, instance: FixtureInstance, relative: str) -> None:
        resource = instance.resource(relative)
        if resource is None:
            raise FixturePolicyError(
                "resource_not_owned",
                f"{relative!r} is not a declared controlled resource of {instance.instance_id}",
            )
        if resource.kind == "state_file":
            write_json_state(instance.state_path, instance.spec.initial_data)
            return
        if resource.kind == "directory":
            controlled = ControlledRoot(instance.root, anchor=self.anchor, create=False)
            removed, error = controlled.remove_tree(relative)
            if not removed:
                raise FixturePolicyError(
                    "resource_not_removable",
                    f"cannot reset {relative!r}: {error or 'removal refused'}",
                )
            data = _initial_data(instance.spec)
            instance.files_root.mkdir(parents=True, exist_ok=True)
            for child, content in data["files"].items():
                controlled.write_text(f"{relative}/{child}", content)
            return
        if resource.kind == "sqlite_file":
            controlled = ControlledRoot(instance.root, anchor=self.anchor, create=False)
            try:
                database = controlled.safe_target(relative)
            except WorkspacePolicyError as error:
                raise FixturePolicyError(error.code, str(error)) from error
            if is_link_path(database):
                raise FixturePolicyError(
                    "resource_not_removable", f"refusing to reset a linked file: {relative!r}"
                )
            if database.exists():
                database.unlink()
            init_sqlite_state(database, _initial_data(instance.spec))
            return
        raise FixturePolicyError(
            "resource_not_resettable",
            f"controlled resource {relative!r} ({resource.kind}) cannot be reset by a case",
        )

    # ------------------------------------------------------------- 清理

    def cleanup(
        self,
        instance: FixtureInstance,
        *,
        owner_token: str,
        original_error: Any = None,
        interrupted: bool = False,
    ) -> CleanupReport:
        """只删除校验过的自身资源；中断/归属不可证明时保留并落盘证据。"""
        self._check_owner_token(instance, owner_token)
        root = Path(instance.root)
        original_text = _error_text(original_error)
        declared = tuple(resource.path for resource in instance.resources) or (".",)

        if not root.exists() and not is_link_path(root):
            return self._record_cleanup(
                instance,
                status="success",
                deleted=(),
                residual=(),
                reason="already_absent",
                original_error=original_text,
                cleanup_error=None,
                interrupted=interrupted,
            )
        ok, code, reason = self._verify_owned_root(instance)
        if not ok:
            return self._record_cleanup(
                instance,
                status="unknown",
                deleted=(),
                residual=declared,
                reason=f"{code}: {reason}",
                original_error=original_text,
                cleanup_error=None,
                interrupted=interrupted,
            )
        if interrupted:
            return self._record_cleanup(
                instance,
                status="unknown",
                deleted=(),
                residual=declared,
                reason="interrupted with unknown state; resources retained for review",
                original_error=original_text,
                cleanup_error=None,
                interrupted=True,
            )
        if instance.spec.cleanup == "retain":
            return self._record_cleanup(
                instance,
                status="unknown",
                deleted=(),
                residual=declared,
                reason="cleanup policy 'retain': resources retained for review",
                original_error=original_text,
                cleanup_error=None,
                interrupted=interrupted,
            )

        cleanup_error: str | None = None
        try:
            removed, remove_error = self._remove_root(root)
        except BaseException as error:  # 清理实现自身抛错也必须留下证据
            removed, remove_error = False, _error_text(error)
        if remove_error:
            cleanup_error = remove_error
        if removed and not root.exists() and not is_link_path(root):
            return self._record_cleanup(
                instance,
                status="success",
                deleted=declared,
                residual=(),
                reason=None,
                original_error=original_text,
                cleanup_error=cleanup_error,
                interrupted=interrupted,
            )
        residual = tuple(path for path in declared if (root / path).exists()) or declared
        return self._record_cleanup(
            instance,
            status="residual",
            deleted=(),
            residual=residual,
            reason="owned resources could not be fully removed",
            original_error=original_text,
            cleanup_error=cleanup_error or "removal left residual content",
            interrupted=interrupted,
        )

    def _remove_root(self, root: Path) -> tuple[bool, str | None]:
        """删除一个校验过的受控根（测试故障注入的接缝）。"""
        return ControlledRoot(root, anchor=self.anchor, create=False).remove_tree()

    def _cleanup_after_failure(
        self, instance: FixtureInstance, *, owner_token: str, error: BaseException
    ) -> CleanupReport:
        try:
            return self.cleanup(instance, owner_token=owner_token, original_error=error)
        except BaseException as cleanup_error:  # 清理自身拒绝执行也要留证
            return CleanupReport(
                instance_id=instance.instance_id,
                status="unknown",
                residual=tuple(resource.path for resource in instance.resources) or (".",),
                reason="cleanup could not run after a prepare failure",
                original_error=_error_text(error),
                cleanup_error=_error_text(cleanup_error),
                recorded_at=self._clock(),
            )

    def _record_cleanup(
        self,
        instance: FixtureInstance,
        *,
        status: Literal["success", "residual", "unknown"],
        deleted: tuple[str, ...],
        residual: tuple[str, ...],
        reason: str | None,
        original_error: str | None,
        cleanup_error: str | None,
        interrupted: bool,
    ) -> CleanupReport:
        report = CleanupReport(
            instance_id=instance.instance_id,
            status=status,
            deleted=tuple(deleted),
            residual=tuple(residual),
            reason=reason,
            original_error=original_error,
            cleanup_error=cleanup_error,
            interrupted=interrupted,
            recorded_at=self._clock(),
        )
        evidence_path = self.evidence_dir / f"{instance.instance_id}.json"
        record = report.to_record()
        record.update(
            {
                "fixture_ref": instance.spec.ref,
                "kind": instance.spec.kind,
                "run_id": instance.run_id,
                "case_id": instance.case_id,
                "attempt_id": instance.attempt_id,
                "root": str(instance.root),
            }
        )
        write_json_state(evidence_path, record)
        return replace(report, evidence_path=str(evidence_path))

    # ------------------------------------------------- 归属校验与可见面

    def _check_owner_token(self, instance: FixtureInstance, owner_token: Any) -> None:
        if not isinstance(owner_token, str) or not owner_token:
            raise FixtureOwnershipError("owner_token_required", "an owner token is required")
        if owner_token_digest(owner_token) != owner_token_digest(instance.owner_token):
            raise FixtureOwnershipError(
                "owner_token_mismatch",
                f"instance {instance.instance_id} is not owned by the supplied token",
            )

    def _verify_owned_root(self, instance: FixtureInstance) -> tuple[bool, str, str]:
        root = Path(instance.root)
        try:
            relative = root.relative_to(self.anchor)
        except ValueError:
            return (
                False,
                "root_outside_anchor",
                f"instance root {root} is not under the fixture anchor {self.anchor}",
            )
        if not relative.parts:
            return False, "root_outside_anchor", "instance root must be a child of the anchor"
        if not root.exists() and not is_link_path(root):
            return False, "root_missing", f"instance root {root} does not exist"
        try:
            anchor_real = self.anchor.resolve()
            root_real = root.resolve()
        except OSError as error:
            return False, "root_unverifiable", f"cannot resolve the instance root: {error}"
        if root_real != anchor_real and anchor_real not in root_real.parents:
            return (
                False,
                "root_outside_anchor",
                f"instance root resolves outside the anchor: {root_real}",
            )
        if is_link_path(root):
            return (
                False,
                "root_is_link",
                f"instance root {root} is a link; ownership cannot be proven",
            )
        if not root.is_dir():
            return False, "root_not_a_directory", f"instance root {root} is not a directory"
        marker = root / "_instance.json"
        if is_link_path(marker) or not marker.is_file():
            return False, "marker_missing", f"ownership marker {marker} is missing"
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return False, "marker_unreadable", f"cannot read the ownership marker: {error}"
        if recorded.get("instance_id") != instance.instance_id:
            return (
                False,
                "marker_mismatch",
                f"ownership marker does not belong to {instance.instance_id}",
            )
        if recorded.get("owner_token_sha256") != owner_token_digest(instance.owner_token):
            return (
                False,
                "marker_mismatch",
                "ownership marker was written for a different owner token",
            )
        return True, "", ""

    def _require_owned_root(self, instance: FixtureInstance, owner_token: Any) -> None:
        self._check_owner_token(instance, owner_token)
        ok, code, reason = self._verify_owned_root(instance)
        if not ok:
            raise FixtureOwnershipError(code, reason)

    def visible_root(self, instance: FixtureInstance, *, owner_token: str) -> Path:
        """目标 / 业务工具可见的根：json 状态目录、文件树或数据库目录。"""
        self._require_owned_root(instance, owner_token)
        return instance.visible_root()

    def list_visible_files(self, instance: FixtureInstance, *, owner_token: str) -> list[str]:
        visible = self.visible_root(instance, owner_token=owner_token)
        controlled = ControlledRoot(visible, anchor=instance.root, create=False)
        return controlled.list_files()

    def visible_state(self, instance: FixtureInstance, *, owner_token: str) -> dict[str, Any]:
        """目标可见投影：只带平台声明的业务字段，绝无 checker 真值。"""
        self._require_owned_root(instance, owner_token)
        if instance.spec.kind == "json":
            try:
                state = read_json_state(instance.state_path)
            except FileNotFoundError as error:
                raise FixturePolicyError(
                    "state_unreadable", f"fixture state is missing: {error}"
                ) from error
            if not isinstance(state, Mapping):
                raise FixturePolicyError(
                    "state_unreadable", "json fixture state must be an object"
                )
            return minimal_visible_fields(
                state, allowed=instance.spec.visible_fields or None
            )
        if instance.spec.kind == "files":
            return {"files": self.list_visible_files(instance, owner_token=owner_token)}
        return {"tables": list_sqlite_tables(instance.sqlite_path)}

    def visible_tool_result(
        self, instance: FixtureInstance, *, owner_token: str, tool: str
    ) -> dict[str, Any]:
        """业务工具结果：工具必须在 allowed_tools 里，结果只带最小可见字段。"""
        if tool not in instance.spec.allowed_tools:
            raise FixturePolicyError(
                "tool_not_allowed",
                f"{tool!r} is not declared in allowed_tools of {instance.spec.ref}",
            )
        return self.visible_state(instance, owner_token=owner_token)

    def private_truth(self, instance: FixtureInstance, *, owner_token: str) -> PrivateTruth:
        """checker 真值 / gold / 隐藏断言的私有根（与可见根不同）。"""
        self._require_owned_root(instance, owner_token)
        return PrivateTruth(instance.private_root, anchor=instance.root)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "CleanupReport",
    "FixtureConflict",
    "FixtureError",
    "FixtureOwnershipError",
    "FixturePolicyError",
    "FixturePrepareError",
    "FixtureRuntime",
    "FixtureUnavailable",
    "PostgresTestTarget",
    "PostgresTestVerdict",
    "ResetTarget",
    "load_fixture",
    "publish_fixture",
    "require_postgres_test_target",
    "validate_postgres_test_target",
]
