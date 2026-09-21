"""M5-T02：版本化 Fixture 契约（内容、隔离与清理政策）。

FixtureSpec 描述一个**已发布、不可变**的初始状态资源：业务初态、允许的业务
工具、目标可见字段投影、隔离与清理政策，以及内容 hash。它不描述运行时实例：
实例身份、初始化进度与受控资源清单在 motte_scenario.state.FixtureInstance。

设计约束（与 M5 执行计划 M5-T02 一致）：

* lifecycle 只允许 published：草稿不进入版本仓库，运行期解析再拒绝一次。
* content_hash 由 fixture_content_hash 计算，覆盖除 lifecycle / published_at /
  content_hash 以外的一切字段；存储地址与生命周期元数据不改变内容身份，任何
  业务初态、允许工具或政策改动一定改变 hash。
* initial_data 必须是规范 JSON：NaN/Infinity 与非 JSON 类型在契约层拒绝，否则
  稳定序列化与跨语言 hash 都不成立。
* isolation 默认 per_case。per_run（多 Case 共享同一实例根）在没有显式租约
  协议之前由运行期具名拒绝，不允许悄悄共享。
* visible_fields 是目标可见投影的**显式白名单**：checker 真值、gold、隐藏断言
  的字段名不允许出现在里面（is_hidden_field）。
"""
from __future__ import annotations

import math
import re
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from .identity import canonical_json_bytes, canonical_sha256
from .messages import Contract
from .workflow import FIXTURE_KINDS

FIXTURE_SCHEMA_VERSION = 1

FIXTURE_ISOLATIONS: tuple[str, ...] = ("per_case", "per_run")
FIXTURE_CLEANUP_POLICIES: tuple[str, ...] = ("delete_owned", "retain")
FIXTURE_LIFECYCLES: tuple[str, ...] = ("draft", "published", "deprecated")

#: 实例受控资源清单里允许出现的资源种类。
CONTROLLED_RESOURCE_KINDS: tuple[str, ...] = (
    "instance_marker",
    "state_file",
    "directory",
    "sqlite_file",
    "private_root",
)

#: 目标可见投影绝不允许暴露的字段名片段（checker 真值 / gold / 隐藏断言）。
HIDDEN_FIELD_MARKERS: tuple[str, ...] = (
    "gold", "checker", "hidden", "oracle", "answer_key", "expected", "truth", "assertion",
)
HIDDEN_KEY_PREFIX = "_"

_REFERENCE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class FixtureDataError(ValueError):
    """初始数据形状非法；带稳定 code，运行期据此具名拒绝。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def is_hidden_field(name: Any) -> bool:
    """该字段是否属于目标不可见侧（gold / checker / 隐藏断言）。"""
    key = str(name).strip().lower()
    if key.startswith(HIDDEN_KEY_PREFIX):
        return True
    return any(marker in key for marker in HIDDEN_FIELD_MARKERS)


def _validate_relative_shape(path: Any) -> str:
    """相对 posix 路径形状；与 motte_sandbox.workspace.validate_relative_path 同规则。

    契约层不能反向依赖沙箱包，因此这里做同样的形状拒绝（绝对路径、反斜杠、
    盘符形状、.. 段）；权威检查仍在运行期的受控根上执行一次。
    """
    if not isinstance(path, str) or not path:
        raise FixtureDataError("unsafe_path", "fixture file path must be a non-empty string")
    if path.startswith(("/", "\\")) or "\\" in path or _WINDOWS_DRIVE.match(path):
        raise FixtureDataError("unsafe_path", f"fixture file path must be relative posix: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise FixtureDataError("unsafe_path", f"fixture file path escapes its root: {path!r}")
    return path


def _validate_identifier(name: Any, what: str) -> str:
    if not isinstance(name, str) or _IDENTIFIER.match(name) is None:
        raise FixtureDataError(
            "invalid_initial_data", f"{what} name must be a plain identifier: {name!r}"
        )
    return name


def _file_text(relative: Any, content: Any) -> str:
    if not isinstance(content, str):
        raise FixtureDataError(
            "invalid_initial_data",
            f"file {relative!r} content must be text, not {type(content).__name__}",
        )
    return content


def validate_initial_data(kind: str, initial_data: Mapping[str, Any]) -> dict[str, Any]:
    """按 kind 校验并规范化初始数据；非法形状抛 FixtureDataError。

    json 的业务初态是自由字典；files 只接受 files（相对路径 -> 文本）；
    sqlite 只接受 schema（CREATE 语句）与 rows（表 -> 行）。fixture 不能声明
    外部数据库地址：没有任何 kind 接受 dsn / path 这类存储地址字段。
    """
    if not isinstance(initial_data, Mapping):
        raise FixtureDataError("invalid_initial_data", "initial_data must be a mapping")
    data = dict(initial_data)
    if kind == "json":
        return data
    if kind == "files":
        unknown = sorted(set(data) - {"files"})
        if unknown:
            raise FixtureDataError(
                "unexpected_initial_data_field",
                "files fixtures accept only 'files'; unexpected: " + ", ".join(unknown),
            )
        files = data.get("files", {})
        if not isinstance(files, Mapping):
            raise FixtureDataError("invalid_initial_data", "files must map relative paths to text")
        normalized_files: dict[str, str] = {}
        for relative, content in files.items():
            normalized_files[_validate_relative_shape(relative)] = _file_text(relative, content)
        return {"files": normalized_files}
    if kind == "sqlite":
        unknown = sorted(set(data) - {"schema", "rows"})
        if unknown:
            raise FixtureDataError(
                "unexpected_initial_data_field",
                "sqlite fixtures accept only 'schema' and 'rows'; unexpected: "
                + ", ".join(unknown),
            )
        schema = data.get("schema", [])
        if not isinstance(schema, (list, tuple)):
            raise FixtureDataError("invalid_initial_data", "sqlite schema must be a list")
        schema_statements: list[str] = []
        for statement in schema:
            if not isinstance(statement, str):
                raise FixtureDataError(
                    "invalid_initial_data", "sqlite schema entries must be strings"
                )
            if not statement.strip().upper().startswith("CREATE "):
                raise FixtureDataError(
                    "invalid_initial_data",
                    "sqlite schema only accepts CREATE statements: " + repr(statement[:40]),
                )
            schema_statements.append(statement)
        rows = data.get("rows", {})
        if not isinstance(rows, Mapping):
            raise FixtureDataError("invalid_initial_data", "sqlite rows must map tables to rows")
        normalized_rows: dict[str, list[dict[str, Any]]] = {}
        for table, table_rows in rows.items():
            _validate_identifier(table, "sqlite table")
            if not isinstance(table_rows, (list, tuple)):
                raise FixtureDataError(
                    "invalid_initial_data", f"sqlite rows for {table!r} must be a list"
                )
            rows_out: list[dict[str, Any]] = []
            for row in table_rows:
                if not isinstance(row, Mapping):
                    raise FixtureDataError(
                        "invalid_initial_data", f"sqlite row for {table!r} must be a mapping"
                    )
                for column in row:
                    _validate_identifier(column, "sqlite column")
                rows_out.append(dict(row))
            normalized_rows[str(table)] = rows_out
        return {"schema": schema_statements, "rows": normalized_rows}
    raise FixtureDataError("invalid_initial_data", f"unknown fixture kind: {kind!r}")


def _reject_bool(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("fixture version must be an integer, not a boolean")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("fixture version must be finite")
    return value


class ControlledResource(Contract):
    """实例受控资源清单的一条：路径相对实例根，绝不携带绝对地址。"""

    kind: Literal["instance_marker", "state_file", "directory", "sqlite_file", "private_root"]
    path: str
    purpose: str

    @field_validator("path")
    @classmethod
    def _relative(cls, value: str) -> str:
        try:
            return _validate_relative_shape(value)
        except FixtureDataError as error:
            raise ValueError(str(error)) from error

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("controlled resource purpose must be a non-empty string")
        return value


class FixtureSpec(Contract):
    """已发布的不可变 Fixture 版本资源；存储主键为 (fixture_id, version)。"""

    fixture_id: str
    version: int = Field(default=1, strict=True)
    schema_version: int = Field(default=FIXTURE_SCHEMA_VERSION, strict=True)
    kind: Literal["json", "files", "sqlite"]
    description: str | None = None
    initial_data: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    visible_fields: tuple[str, ...] = ()
    isolation: Literal["per_case", "per_run"] = "per_case"
    cleanup: Literal["delete_owned", "retain"] = "delete_owned"
    lifecycle: str = "published"
    published_at: str = Field(min_length=1)
    content_hash: str | None = None

    @field_validator("fixture_id")
    @classmethod
    def _fixture_id(cls, value: str) -> str:
        if _REFERENCE.match(value) is None:
            raise ValueError("fixture_id is not a valid resource name: " + repr(value))
        return value

    @field_validator("version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> Any:
        _reject_bool(value)
        if not isinstance(value, int) or value < 1:
            raise ValueError("fixture version must be a positive integer")
        return value

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version(cls, value: Any) -> Any:
        _reject_bool(value)
        return value

    @field_validator("published_at")
    @classmethod
    def _published_at(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("fixture published_at must be a non-empty timestamp")
        return value

    @field_validator("initial_data")
    @classmethod
    def _canonical(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            canonical_json_bytes(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "fixture initial_data must be canonical JSON: " + str(error)
            ) from error
        return value

    @field_validator("allowed_tools")
    @classmethod
    def _allowed_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if not isinstance(name, str) or _REFERENCE.match(name) is None:
                raise ValueError("allowed_tools entry is not a tool name: " + repr(name))
        return tuple(dict.fromkeys(value))

    @field_validator("visible_fields")
    @classmethod
    def _visible_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if not isinstance(name, str) or _FIELD_NAME.match(name) is None:
                raise ValueError("visible_fields entry is not a plain field name: " + repr(name))
            if is_hidden_field(name):
                # 显式白名单也不能把 checker 真值 / gold / 隐藏断言暴露给目标
                raise ValueError(
                    "visible_fields must not name hidden fixture truth: " + repr(name)
                )
        return tuple(dict.fromkeys(value))

    @field_validator("lifecycle")
    @classmethod
    def _published_only(cls, value: str) -> str:
        if value != "published":
            raise ValueError("fixture versions publish immediately and are immutable")
        return value

    @field_validator("content_hash")
    @classmethod
    def _content_hash_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("sha256:") or len(value) != 71:
            raise ValueError("fixture content_hash must be a canonical sha256 digest")
        return value

    @model_validator(mode="after")
    def _structure(self) -> FixtureSpec:
        if self.schema_version != FIXTURE_SCHEMA_VERSION:
            raise ValueError(
                "unsupported fixture schema_version "
                + str(self.schema_version)
                + "; expected "
                + str(FIXTURE_SCHEMA_VERSION)
            )
        if self.kind not in FIXTURE_KINDS:
            raise ValueError("unknown fixture kind: " + repr(self.kind))
        return self

    @property
    def ref(self) -> str:
        return f"{self.fixture_id}@{self.version}"

    def effective_content_hash(self) -> str:
        computed = fixture_content_hash(self)
        if self.content_hash is not None and self.content_hash != computed:
            raise ValueError("fixture content_hash does not match its content")
        return computed

    def normalized_initial_data(self) -> dict[str, Any]:
        """按 kind 校验后的初始数据；形状非法时抛 FixtureDataError。"""
        return validate_initial_data(self.kind, self.initial_data)


def fixture_content_hash(record: Any) -> str:
    """内容身份：排除存储地址与生命周期元数据。

    与 workflow_content_hash 同一形状：draft 与已发布版本对同一内容得到同一
    hash，任何业务初态 / 允许工具 / 隔离与清理政策改动都会改变 hash。
    """
    if hasattr(record, "model_dump"):
        record = record.model_dump(mode="json")
    payload = {
        key: value
        for key, value in dict(record).items()
        if key not in {"lifecycle", "published_at", "content_hash"}
    }
    return canonical_sha256(payload)


__all__ = [
    "CONTROLLED_RESOURCE_KINDS",
    "FIXTURE_CLEANUP_POLICIES",
    "FIXTURE_ISOLATIONS",
    "FIXTURE_LIFECYCLES",
    "FIXTURE_SCHEMA_VERSION",
    "HIDDEN_FIELD_MARKERS",
    "ControlledResource",
    "FixtureDataError",
    "FixtureSpec",
    "fixture_content_hash",
    "is_hidden_field",
    "validate_initial_data",
]
