"""M5-T02 状态层：受控根、稳定序列化、快照与私有真值。

路径安全**复用** packages/sandbox/motte_sandbox/workspace.py 的规则与错误分类
（validate_relative_path / WorkspacePolicyError）：相对 posix 路径、拒绝 ..、
拒绝反斜杠与盘符形状、逐组件拒绝链接、只操作归属校验过的对象。

CaseWorkspace 的目录链实现基于 os.open(dir_fd)，Windows 不支持，因此受控根
在支持的平台直接委托 CaseWorkspace 建链与删除，否则使用同一组规则的便携实现
（同样的拒绝项，逐组件 lstat + resolved 归属校验，删除前扫描树内链接）。
两条路径对外暴露同一个 ControlledRoot 接口，运行期逻辑不区分平台。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from motte_contracts.fixture import (
    ControlledResource,
    FixtureSpec,
    is_hidden_field,
    validate_initial_data,
)
from motte_contracts.identity import canonical_json_bytes, canonical_sha256
from motte_sandbox.workspace import (
    CaseWorkspace,
    WorkspacePolicyError,
    validate_relative_path,
)

#: os.open(dir_fd=...) 的平台能力：POSIX 可用，Windows 不可用。
_DIR_FD_SUPPORTED = os.name == "posix" and hasattr(os, "O_DIRECTORY")

DEFAULT_MAX_FILE_BYTES = 1_000_000

#: 实例初始化进度允许的阶段：created -> preparing -> prepared（失败 failed）。
FIXTURE_INSTANCE_STAGES: tuple[str, ...] = (
    "created", "preparing", "prepared", "failed", "cleaned", "retained",
)


# --------------------------------------------------------------------- 链接判定


def _is_reparse_point(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def is_link_path(path: str | Path) -> bool:
    """路径本身是否是链接/重解析点（含 Windows junction）。

    Python 在 Windows 上把 junction 报成目录，因此必须看重解析属性，不能只看
    S_ISLNK：这是“链接根不能被当作受控根”的判定入口。
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return _is_reparse_point(info)


# ------------------------------------------------------------------- 受控根


class ControlledRoot:
    """一个受控根目录：所有读写只经相对 posix 路径，逐组件拒绝链接。"""

    def __init__(
        self, root: str | Path, *, anchor: str | Path | None = None, create: bool = True
    ) -> None:
        self.root = Path(root)
        self.anchor = Path(anchor) if anchor is not None else self.root.parent
        #: 校验/建链所用后端："sandbox"（复用 CaseWorkspace 的 fd 链）或 "portable"。
        self.backend = "portable"
        if create:
            self._ensure_created()
        else:
            self._validate_existing()

    # ------------------------------------------------------------ 建链与校验

    def _relative_parts(self) -> tuple[str, ...]:
        try:
            relative = self.root.relative_to(self.anchor)
        except ValueError as error:
            raise WorkspacePolicyError(
                "workspace_chain_invalid",
                f"controlled root {self.root} is not under anchor {self.anchor}",
            ) from error
        parts = tuple(part for part in relative.parts if part not in ("", "."))
        if not parts or any(part == ".." for part in relative.parts):
            raise WorkspacePolicyError(
                "workspace_chain_invalid",
                f"controlled root must be a child of its anchor: {self.root}",
            )
        return parts

    @staticmethod
    def _check_component(path: Path, part: str) -> None:
        try:
            info = os.lstat(path)
        except OSError as error:
            raise WorkspacePolicyError(
                "workspace_chain_invalid", f"cannot inspect {part!r}: {error}"
            ) from error
        if _is_reparse_point(info):
            raise WorkspacePolicyError(
                "symlink_rejected", f"controlled root component is a link: {part!r}"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspacePolicyError(
                "workspace_chain_invalid",
                f"controlled root component is not a directory: {part!r}",
            )

    def _ensure_created(self) -> None:
        parts = self._relative_parts()
        self.anchor.mkdir(parents=True, exist_ok=True)
        if _DIR_FD_SUPPORTED:
            try:
                # 复用沙箱实现：anchor 之下逐组件按 fd 打开/单级创建并拒绝链接。
                CaseWorkspace(self.root, anchor=self.anchor)
                self.backend = "sandbox"
                return
            except NotImplementedError:  # pragma: no cover - 平台能力兜底
                self.backend = "portable"
        current = self.anchor
        for part in parts:
            current = current / part
            if not current.exists() and not is_link_path(current):
                try:
                    current.mkdir()
                except FileExistsError:
                    pass  # 竞态放置：下面按当前对象类型判定
            self._check_component(current, part)
        self._resolved_inside_anchor()

    def _validate_existing(self) -> None:
        parts = self._relative_parts()
        for index, part in enumerate(parts):
            current = self.anchor.joinpath(*parts[: index + 1])
            if not current.exists() and not is_link_path(current):
                raise WorkspacePolicyError(
                    "workspace_chain_invalid",
                    f"controlled root component missing: {part!r}",
                )
            self._check_component(current, part)
        self._resolved_inside_anchor()

    def _resolved_inside_anchor(self) -> Path:
        anchor_real = self.anchor.resolve()
        root_real = self.root.resolve()
        if root_real != anchor_real and anchor_real not in root_real.parents:
            raise WorkspacePolicyError(
                "path_escape",
                f"controlled root resolves outside its anchor: {root_real}",
            )
        return root_real

    # ------------------------------------------------------------------ 路径

    def safe_target(self, relative: str) -> Path:
        """校验并返回受控根内的目标；越界 / 链接 / 设备文件一律拒绝。"""
        validate_relative_path(relative)  # 复用沙箱规则
        target = self.root / relative
        current = self.root
        for part in PurePosixPath(relative).parts:
            current = current / part
            if not current.exists() and not is_link_path(current):
                continue
            info = os.lstat(current)
            if _is_reparse_point(info):
                raise WorkspacePolicyError(
                    "symlink_rejected", f"symlink components are not allowed: {part!r}"
                )
        root_real = self.root.resolve()
        resolved = target.resolve(strict=False)
        if resolved != root_real and root_real not in resolved.parents:
            raise WorkspacePolicyError(
                "path_escape", f"resolved path escapes the controlled root: {relative!r}"
            )
        if target.exists() or is_link_path(target):
            info = os.lstat(target)
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise WorkspacePolicyError(
                    "device_rejected", f"only regular files and directories are allowed: {relative!r}"
                )
        return target

    def exists(self, relative: str) -> bool:
        try:
            target = self.safe_target(relative)
        except WorkspacePolicyError:
            return False
        return target.exists() or is_link_path(target)

    # ------------------------------------------------------------------ 读写

    def write_text(self, relative: str, content: str) -> str:
        target = self.safe_target(relative)
        data = content.encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        handle = os.open(target, flags, 0o600)
        try:
            os.write(handle, data)
        finally:
            os.close(handle)
        return f"wrote {len(data)} bytes to {relative}"

    def read_bytes(self, relative: str, *, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> bytes:
        target = self.safe_target(relative)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            handle = os.open(target, flags)
        except OSError as error:
            raise WorkspacePolicyError(
                "read_failed", f"cannot open {relative!r}: {error.strerror or error}"
            ) from error
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode):
                raise WorkspacePolicyError("device_rejected", f"not a regular file: {relative!r}")
            if info.st_size > max_bytes:
                raise WorkspacePolicyError(
                    "file_too_large", f"{relative!r} exceeds the read limit {max_bytes}"
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(handle, 1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(handle)

    def read_text(self, relative: str, *, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> str:
        return self.read_bytes(relative, max_bytes=max_bytes).decode("utf-8", errors="replace")

    # ---------------------------------------------------------------- 清单

    def list_files(self, prefix: str = "") -> list[str]:
        base_relative = validate_relative_path(prefix) if prefix else ""
        base = self.safe_target(base_relative) if base_relative else self.root
        if not base.is_dir():
            return []
        collected: list[str] = []
        self._walk(base, collected)
        return sorted(collected)

    def _walk(self, directory: Path, collected: list[str]) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return
        for entry in entries:
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_reparse_point(info):
                continue  # 链接不属于受控清单，也不递归进入
            if stat.S_ISDIR(info.st_mode):
                self._walk(path, collected)
            else:
                collected.append(path.relative_to(self.root).as_posix())

    def link_entries(self, relative: str = "") -> list[str]:
        """树内链接组件清单；非空即归属无法证明（不删除、只保留）。"""
        base = self.safe_target(relative) if relative else self.root
        found: list[str] = []
        if is_link_path(base):
            return [base.relative_to(self.root).as_posix() if base != self.root else "."]
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            for name in [*dirnames, *filenames]:
                path = Path(dirpath) / name
                if is_link_path(path):
                    found.append(path.relative_to(self.root).as_posix())
        return sorted(found)

    def snapshot(self) -> dict[str, Any]:
        """文件清单 + 逐文件内容 hash；任一读取失败即 complete=False。"""
        try:
            files = self.list_files()
        except OSError:
            return {"complete": False, "files": [], "hashes": {}}
        hashes: dict[str, str] = {}
        for relative in files:
            try:
                data = self.read_bytes(relative)
            except OSError:
                return {"complete": False, "files": [], "hashes": {}}
            hashes[relative] = hashlib.sha256(data).hexdigest()
        return {"complete": True, "files": files, "hashes": hashes}

    # ---------------------------------------------------------------- 删除

    def remove_tree(self, relative: str = "") -> tuple[bool, str | None]:
        """只删除校验过的自身资源；树内出现链接时拒绝并保留。"""
        target = self.safe_target(relative) if relative else self.root
        if not target.exists() and not is_link_path(target):
            return True, None
        if is_link_path(target):
            return False, f"refusing to remove a link: {target}"
        links = self.link_entries(relative)
        if links:
            return False, "refusing to remove a tree containing links: " + ", ".join(links)
        try:
            if _DIR_FD_SUPPORTED:
                # 复用沙箱清理：删除前重新校验目录链归属，rmtree 自身不跟随链接。
                result = CaseWorkspace(target, anchor=target.parent).cleanup()
                if result.get("status") != "success":
                    return False, str(result.get("error") or "sandbox cleanup failed")
            else:
                shutil.rmtree(target)
        except OSError as error:
            return False, str(error)
        if target.exists() or is_link_path(target):
            return False, "removal left residual content"
        return True, None


# --------------------------------------------------------------- 稳定序列化


def write_json_state(path: str | Path, data: Any) -> str:
    """原子写入规范 JSON（排序键、无 NaN）；返回写入路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(data)
    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=target.parent, prefix=".tmp-", delete=False
    )
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, target)
    return str(target)


def read_json_state(path: str | Path) -> Any:
    return canonical_json_loads(Path(path).read_bytes())


def canonical_json_loads(payload: bytes | str) -> Any:
    return json.loads(payload)


def state_content_hash(payload: Any) -> str:
    return canonical_sha256(payload)


# ------------------------------------------------------------------ SQLite


def _sqlite_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def sqlite_jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes_hex__": bytes(value).hex()}
    return value


def init_sqlite_state(path: str | Path, data: Mapping[str, Any]) -> None:
    """在受控根内重建一个独立 SQLite 文件；只接受 CREATE 语句与参数化行。

    fixture_meta 只标记“这是 fixture 数据库”，不写实例身份：实例身份来自文件
    路径与所有权标记，而相同初态的 Twin 实例必须得到相同快照 hash。
    """
    normalized = validate_initial_data("sqlite", data)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    connection = _sqlite_connection(target)
    try:
        connection.execute(
            "CREATE TABLE fixture_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for statement in normalized["schema"]:
            connection.execute(statement)
        for table, rows in normalized["rows"].items():
            for row in rows:
                columns = list(row)
                if not columns:
                    continue
                column_sql = ", ".join(f'"{column}"' for column in columns)
                placeholders = ", ".join("?" for _ in columns)
                connection.execute(
                    f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})',
                    [row[column] for column in columns],
                )
        connection.commit()
    finally:
        connection.close()


def list_sqlite_tables(path: str | Path) -> list[str]:
    target = Path(path)
    if not target.exists():
        return []
    connection = _sqlite_connection(target)
    try:
        return [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
    finally:
        connection.close()


def snapshot_sqlite_state(path: str | Path) -> dict[str, Any]:
    """逻辑转储（表、列、排序后的行），与页布局无关：同一状态必然同一 hash。"""
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(str(target))
    connection = _sqlite_connection(target)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        payload: dict[str, Any] = {}
        for table in tables:
            columns = [
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            rows = [
                [sqlite_jsonable(value) for value in row]
                for row in connection.execute(f'SELECT * FROM "{table}"')
            ]
            rows.sort(key=canonical_json_bytes)
            payload[table] = {"columns": columns, "rows": rows}
        return payload
    finally:
        connection.close()


# ------------------------------------------------------------------ 快照


@dataclass(frozen=True)
class StateSnapshot:
    """稳定序列化的状态快照；同一状态必然同一 content_hash。"""

    kind: str
    root: str
    content_hash: str
    payload: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "root": self.root,
            "content_hash": self.content_hash,
            "payload": self.payload,
        }


def snapshot_state(
    kind: str,
    *,
    root: str | Path,
    state_path: str | Path | None = None,
    files_root: str | Path | None = None,
    sqlite_path: str | Path | None = None,
) -> StateSnapshot:
    if kind == "json":
        if state_path is None:
            raise ValueError("json fixtures need a state_path")
        payload = {"kind": "json", "state": read_json_state(state_path)}
    elif kind == "files":
        if files_root is None:
            raise ValueError("files fixtures need a files_root")
        files_control = ControlledRoot(
            files_root, anchor=Path(files_root).parent, create=False
        )
        listing = files_control.snapshot()
        if not listing["complete"]:
            raise WorkspacePolicyError(
                "state_unreadable", f"cannot read the whole file tree under {files_root}"
            )
        payload = {"kind": "files", "files": listing["hashes"]}
    elif kind == "sqlite":
        if sqlite_path is None:
            raise ValueError("sqlite fixtures need a sqlite_path")
        payload = {"kind": "sqlite", "database": snapshot_sqlite_state(sqlite_path)}
    else:
        raise ValueError(f"unknown fixture kind: {kind!r}")
    return StateSnapshot(
        kind=kind,
        root=str(root),
        content_hash=state_content_hash(payload),
        payload=payload,
    )


# -------------------------------------------------------- 目标可见投影


def project_visible_value(value: Any) -> Any:
    """递归投影一个 JSON 值：任何层级的隐藏键都整键剔除。

    只过滤顶层是不够的（F07）：`{"order": {"gold": ...}}` 这类嵌套真值会随
    可见字段一起进入 Target 的工具结果、checkpoint 与 observation。字典按键
    剔除、列表逐项递归，标量原样返回；映射结果保持普通 dict，序列变成 list
    （JSON 形状保持一致，canonical 序列化不受影响）。
    """
    if isinstance(value, Mapping):
        return {
            str(key): project_visible_value(item)
            for key, item in value.items()
            if not is_hidden_field(key)
        }
    if isinstance(value, (list, tuple)):
        return [project_visible_value(item) for item in value]
    return value


def hidden_field_paths(value: Any, *, prefix: str = "") -> list[str]:
    """递归找出**仍然存在**的隐藏键路径（防御性检查，正常应恒为空）。

    投影之后仍在的隐藏键说明有一条可见数据通道没有按同一规则过滤；给出
    具体路径（`order.gold` / `items[0].checker_truth`）便于定位与取证。
    """
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if is_hidden_field(key):
                found.append(path)
            found.extend(hidden_field_paths(item, prefix=path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(hidden_field_paths(item, prefix=f"{prefix}[{index}]"))
    return found


def minimal_visible_fields(
    payload: Mapping[str, Any], *, allowed: Sequence[str] | None = None
) -> dict[str, Any]:
    """业务工具结果的最小可见投影：隐藏字段（含嵌套）永远不出现。

    allowed 是平台作者声明的白名单（顺序即输出顺序）；即使它错误地写了隐藏
    字段名，这里也不会带出真值（纵深防御，契约层另有拒绝）。白名单只约束
    顶层字段，值的内部一律递归投影。
    """
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    keys: Iterable[str] = allowed if allowed is not None else payload.keys()
    visible: dict[str, Any] = {}
    for key in keys:
        if key not in payload or is_hidden_field(key):
            continue
        visible[str(key)] = project_visible_value(payload[key])
    return visible


# ------------------------------------------------------------ 私有真值


class PrivateTruth:
    """checker 真值 / gold / 隐藏断言的私有存储。

    与目标可见空间是不同的受控根：目标只能列出 / 读取可见根，私有根既不参与
    可见清单，也不被业务工具结果引用。
    """

    def __init__(self, root: str | Path, *, anchor: str | Path | None = None) -> None:
        self._control = ControlledRoot(root, anchor=anchor)

    @property
    def root(self) -> Path:
        return self._control.root

    def write(self, relative: str, content: str) -> str:
        return self._control.write_text(relative, content)

    def read(self, relative: str) -> str:
        return self._control.read_text(relative)

    def list(self, prefix: str = "") -> list[str]:
        return self._control.list_files(prefix)

    def contains(self, relative: str) -> bool:
        return self._control.exists(relative)


# ------------------------------------------------------------ 实例身份


def owner_token_digest(owner_token: str) -> str:
    """owner token 只以 sha256 摘要落盘；比较也只在摘要上做定长比较。"""
    return "sha256:" + hashlib.sha256(str(owner_token).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FixtureInstance:
    """一个 CaseAttempt 的 Fixture 实例身份 + 初始化进度 + 受控资源清单。

    实例根由 run/case/attempt 身份派生，与业务 ID 无关：两个 Case 即使业务 ID
    相同也不会共享目录、数据库文件或状态。
    """

    instance_id: str
    run_id: str
    case_id: str
    attempt_id: str
    owner_token: str
    root: Path
    spec: FixtureSpec
    business_id: str | None = None
    stage: str = "created"
    completed_steps: tuple[str, ...] = ()
    resources: tuple[ControlledResource, ...] = ()
    prepare_error: str | None = None
    created_at: str = ""

    @property
    def kind(self) -> str:
        return self.spec.kind

    @property
    def fixture_id(self) -> str:
        return self.spec.fixture_id

    @property
    def version(self) -> int:
        return self.spec.version

    @property
    def marker_path(self) -> Path:
        return self.root / "_instance.json"

    @property
    def state_path(self) -> Path:
        return self.root / "state" / "state.json"

    @property
    def files_root(self) -> Path:
        return self.root / "files"

    @property
    def sqlite_path(self) -> Path:
        return self.root / "db" / "fixture.sqlite"

    @property
    def private_root(self) -> Path:
        return self.root / "private"

    def visible_root(self) -> Path:
        if self.kind == "json":
            return self.state_path.parent
        if self.kind == "files":
            return self.files_root
        return self.sqlite_path.parent

    @property
    def session_scope(self) -> str:
        """TargetSession / 状态容器的隔离作用域（M5-T03 必须以它为键）。

        run + case + attempt + 实例身份一起构成作用域：两个 Case 即使业务 ID
        相同也不会共享模型历史、已执行 call id 或状态容器。
        """
        return f"{self.run_id}/{self.case_id}/{self.attempt_id}/{self.instance_id}"

    def resource(self, relative: str) -> ControlledResource | None:
        for resource in self.resources:
            if resource.path == relative:
                return resource
        return None

    def advance(
        self,
        *,
        stage: str | None = None,
        completed_steps: tuple[str, ...] | None = None,
        resources: tuple[ControlledResource, ...] | None = None,
        prepare_error: str | None = None,
    ) -> FixtureInstance:
        return replace(
            self,
            stage=stage if stage is not None else self.stage,
            completed_steps=(
                completed_steps if completed_steps is not None else self.completed_steps
            ),
            resources=resources if resources is not None else self.resources,
            prepare_error=prepare_error if prepare_error is not None else self.prepare_error,
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "fixture_id": self.fixture_id,
            "version": self.version,
            "kind": self.kind,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "attempt_id": self.attempt_id,
            "business_id": self.business_id,
            "root": str(self.root),
            "session_scope": self.session_scope,
            "stage": self.stage,
            "completed_steps": list(self.completed_steps),
            "resources": [resource.model_dump(mode="json") for resource in self.resources],
            "prepare_error": self.prepare_error,
            "created_at": self.created_at,
        }

    def marker_record(self) -> dict[str, Any]:
        """落盘的所有权标记：只放 token 摘要，不放 token 本身。"""
        return {
            "instance_id": self.instance_id,
            "fixture_id": self.fixture_id,
            "version": self.version,
            "kind": self.kind,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "attempt_id": self.attempt_id,
            "business_id": self.business_id,
            "owner_token_sha256": owner_token_digest(self.owner_token),
            "created_at": self.created_at,
        }


__all__ = [
    "ControlledResource",
    "ControlledRoot",
    "FIXTURE_INSTANCE_STAGES",
    "FixtureInstance",
    "PrivateTruth",
    "StateSnapshot",
    "hidden_field_paths",
    "is_hidden_field",
    "is_link_path",
    "list_sqlite_tables",
    "minimal_visible_fields",
    "owner_token_digest",
    "project_visible_value",
    "read_json_state",
    "snapshot_sqlite_state",
    "snapshot_state",
    "state_content_hash",
    "write_json_state",
]
