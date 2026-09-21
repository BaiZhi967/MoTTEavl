"""Per-case controlled workspace for builtin agent file tools.

每个 Case 独立目录；工具只接受受控相对路径。安全边界：
- 拒绝绝对路径、``..``、反斜杠与盘符形状；
- 逐组件拒绝 symlink（打开用 ``O_NOFOLLOW``，写前检查父链），防逃逸与 TOCTOU 替换；
- 工作区目录链（anchor 之下的每个组件）必须是真实目录：预先存在的
  symlink 指向外部目录时拒绝创建，cleanup 前重新校验清理对象归属（R3 #3）；
- 拒绝设备/管道/套接字文件；
- 目录链在支持 os.open(dir_fd=...) 的平台逐组件按 fd 打开，其他平台用同一组
  规则（逐组件 lstat + 解析后归属校验）的便携实现；
- 每文件 / 总量 / 文件数配额在写入前强制；
- 不挂载宿主凭据、不提供网络（文件工具本身无网络面）。
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


#: os.open(dir_fd=...) / O_DIRECTORY 的平台能力。
#: Windows 上 os.open 根本打不开目录（实测连 C:\Windows 也是 EACCES），
#: os.mkdir/os.open 的 dir_fd= 也不受支持，所以 fd 链必须是可选路径。
_DIR_FD_SUPPORTED = os.name == "posix" and hasattr(os, "O_DIRECTORY")


def _is_reparse_point(info: os.stat_result) -> bool:
    """链接或 Windows 重解析点（junction）。POSIX 上等价于 S_ISLNK。"""
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def is_link_path(path: str | Path) -> bool:
    """路径本身是否是链接/重解析点；不存在或不可访问时为 False。"""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return _is_reparse_point(info)


class WorkspacePolicyError(ValueError):
    """路径越界 / 设备文件 / 配额超限；带稳定 code 供分类。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkspaceQuotas:
    max_file_bytes: int = 1_000_000
    max_total_bytes: int = 10_000_000
    max_files: int = 200


def validate_relative_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise WorkspacePolicyError("invalid_path", "path must be a non-empty string")
    if path.startswith(("/", "\\")) or "\\" in path:
        raise WorkspacePolicyError("invalid_path", f"path must be relative posix: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise WorkspacePolicyError("path_escape", f"path escapes the workspace: {path!r}")
    if len(path) > 512:
        raise WorkspacePolicyError("invalid_path", "path too long")
    return path


class CaseWorkspace:
    """One case's workspace directory under a run-scoped controlled root.

    ``anchor`` 是受信前缀（如平台配置的 workspace 根）：anchor 自身允许是
    symlink（操作者配置，例如 /tmp），但 anchor 之下的每个组件必须是不含
    symlink 的真实目录。``self.root`` 保持未解析形态，cleanup 只删除归属
    校验通过的对象。
    """

    def __init__(
        self, root: str | Path, *, quotas: WorkspaceQuotas | None = None,
        anchor: str | Path | None = None,
    ) -> None:
        root_path = Path(root)
        self._anchor = Path(anchor) if anchor is not None else root_path.parent
        self.root = root_path
        self.quotas = quotas or WorkspaceQuotas()
        # 先验证已有组件，再逐级创建缺失目录（R4 #6）：不存在"先递归 mkdir、
        # 后校验"造成的越界副作用——预置 symlink 指向 victim 时不会在 victim
        # 里创建任何东西。
        self._anchor_real, self._root_real = self._ensure_chain(self._anchor)

    # ---------------------------------------------------------------- 目录链安全

    def _relative_to_anchor(self, anchor: Path) -> Path:
        try:
            return self.root.relative_to(anchor)
        except ValueError as error:
            raise WorkspacePolicyError(
                "workspace_chain_invalid",
                f"workspace root {self.root} is not under anchor {anchor}",
            ) from error

    @staticmethod
    def _chain_error(parent_fd: int, part: str, error: OSError) -> WorkspacePolicyError:
        import errno

        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            # ELOOP=Linux 的 O_NOFOLLOW symlink；macOS 对 symlink+O_DIRECTORY
            # 返回 ENOTDIR——用 fstatat(follow_symlinks=False) 区分真因
            try:
                info = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                info = None
            if info is not None and stat.S_ISLNK(info.st_mode):
                return WorkspacePolicyError(
                    "symlink_rejected",
                    f"workspace chain component is a symlink: {part!r}",
                )
        return WorkspacePolicyError(
            "workspace_chain_invalid",
            f"workspace chain component rejected: {part!r} ({error})",
        )

    def _open_or_create_component(self, parent_fd: int, part: str, *, create: bool) -> int:
        """相对父目录 fd 打开一个组件；缺失时（允许的话）单级创建后重开。

        一切按 fd 相对执行，不按路径重新解析父目录（R5 #2）：父目录在校验后
        被替换成 symlink，也无法把创建/打开重定向到外部目录。O_NOFOLLOW +
        O_DIRECTORY 拒绝 symlink 与非目录组件（错误码平台差异见 _chain_error）。
        """
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(part, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise WorkspacePolicyError(
                    "workspace_chain_invalid",
                    f"workspace chain component missing: {part!r}",
                ) from None
        except OSError as error:
            raise self._chain_error(parent_fd, part, error) from error
        try:
            os.mkdir(part, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass  # 竞态放置：重开并按当前对象类型判定
        except OSError as error:
            raise WorkspacePolicyError(
                "workspace_chain_invalid",
                f"cannot create workspace component {part!r}: {error}",
            ) from error
        try:
            return os.open(part, flags, dir_fd=parent_fd)
        except OSError as error:
            raise self._chain_error(parent_fd, part, error) from error

    def _open_chain_fd(self, anchor: Path, *, create: bool) -> tuple[int, Path, Path]:
        """从 anchor 逐级（按 fd）打开/创建到 root；返回 (root_fd, anchor_real, root_real)。

        返回的 root_fd 指向最终验证过的目录 inode，调用方负责 close。
        """
        anchor.mkdir(parents=True, exist_ok=True)  # anchor 是受信配置前缀
        anchor_real = anchor.resolve()
        current_path = anchor_real
        fd = os.open(anchor, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            for part in self._relative_to_anchor(anchor).parts:
                child = self._open_or_create_component(fd, part, create=create)
                os.close(fd)
                fd = child
                current_path = current_path / part
        except BaseException:
            os.close(fd)
            raise
        return fd, anchor_real, current_path

    # ------------------------------------------------- 便携链（无 dir_fd 的平台）
    #
    # 验收 F-03：fd 链在 Windows 上必然失败，M1 的 agent 文件任务因此从不调用
    # 模型就失败（PermissionError: [Errno 13] 'var\agent-workspaces'），错误还被
    # 归类成 network。便携实现用同一组规则代替 fd 链：逐组件 lstat 拒绝链接与
    # 非目录，最后要求解析后的根仍落在受信 anchor 之内。对外 API 与错误码不变。

    @staticmethod
    def _check_component(path: Path, part: str) -> None:
        try:
            info = os.lstat(path)
        except OSError as error:
            raise WorkspacePolicyError(
                "workspace_chain_invalid",
                f"cannot inspect workspace component {part!r}: {error}",
            ) from error
        if _is_reparse_point(info):
            raise WorkspacePolicyError(
                "symlink_rejected",
                f"workspace chain component is a symlink: {part!r}",
            )
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspacePolicyError(
                "workspace_chain_invalid",
                f"workspace chain component is not a directory: {part!r}",
            )

    def _resolved_root(self, anchor_real: Path) -> Path:
        root_real = self.root.resolve()
        if root_real != anchor_real and anchor_real not in root_real.parents:
            raise WorkspacePolicyError(
                "path_escape",
                f"resolved workspace root escapes its anchor: {root_real}",
            )
        return root_real

    def _ensure_chain_portable(self, anchor: Path) -> tuple[Path, Path]:
        anchor.mkdir(parents=True, exist_ok=True)  # anchor 是受信配置前缀
        anchor_real = anchor.resolve()
        current = anchor_real
        for part in self._relative_to_anchor(anchor).parts:
            current = current / part
            if not current.exists() and not is_link_path(current):
                try:
                    current.mkdir()
                except FileExistsError:
                    pass  # 竞态放置：下面按当前对象类型判定
            self._check_component(current, part)
        return anchor_real, self._resolved_root(anchor_real)

    def _validate_chain_portable(self, anchor: Path) -> tuple[Path, Path]:
        anchor_real = anchor.resolve()
        current = anchor_real
        for part in self._relative_to_anchor(anchor).parts:
            current = current / part
            if not current.exists() and not is_link_path(current):
                raise WorkspacePolicyError(
                    "workspace_chain_invalid",
                    f"workspace chain component missing: {part!r}",
                )
            self._check_component(current, part)
        return anchor_real, self._resolved_root(anchor_real)

    def _validate_chain(self, anchor: Path) -> tuple[Path, Path]:
        """纯校验（不创建，按 fd 打开）：anchor 之下到 root 全部为真实目录。

        预先存在 symlink（含 root 本身指向外部目录）在此拒绝；返回
        (anchor 解析后路径, root 解析后路径) 供归属比较与清理前复核使用。
        """
        if not _DIR_FD_SUPPORTED:
            return self._validate_chain_portable(anchor)
        fd, anchor_real, root_real = self._open_chain_fd(anchor, create=False)
        os.close(fd)
        return anchor_real, root_real

    def _ensure_chain(self, anchor: Path) -> tuple[Path, Path]:
        """验证已有组件 + 单级创建缺失目录（全部相对受信 fd 执行）。"""
        if not _DIR_FD_SUPPORTED:
            return self._ensure_chain_portable(anchor)
        fd, anchor_real, root_real = self._open_chain_fd(anchor, create=True)
        os.close(fd)
        return anchor_real, root_real

    # ---------------------------------------------------------------- 路径安全

    def _safe_target(self, path: str) -> Path:
        validate_relative_path(path)
        target = self.root / path
        # 逐组件检查：任何已存在的组件不得是 symlink（防检查后替换 / 链接逃逸）
        current = self.root
        for part in PurePosixPath(path).parts:
            current = current / part
            if not current.exists() and not current.is_symlink():
                continue
            info = current.lstat()
            if _is_reparse_point(info):
                raise WorkspacePolicyError(
                    "symlink_rejected", f"symlink components are not allowed: {part!r}"
                )
        # 归属比较用解析后的真实根（self.root 保持未解析形态供 cleanup 使用）
        resolved = target.resolve(strict=False)
        if self._root_real != resolved and self._root_real not in resolved.parents:
            raise WorkspacePolicyError("path_escape", f"resolved path escapes workspace: {path!r}")
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if _is_reparse_point(info):
                raise WorkspacePolicyError("symlink_rejected", "symlink target rejected")
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise WorkspacePolicyError(
                    "device_rejected", f"only regular files are allowed: {path!r}"
                )
        return target

    # ---------------------------------------------------------------- 工具面

    def read_text(self, path: str, *, max_bytes: int | None = None) -> str:
        target = self._safe_target(path)
        limit = max_bytes or self.quotas.max_file_bytes
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags)
        except OSError as error:
            raise WorkspacePolicyError(
                "read_failed", f"cannot open {path!r}: {error.strerror or error}"
            ) from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise WorkspacePolicyError("device_rejected", f"not a regular file: {path!r}")
            if info.st_size > limit:
                raise WorkspacePolicyError(
                    "file_too_large", f"{path!r} is {info.st_size} bytes > limit {limit}"
                )
            with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
                fd = -1  # ownership moved
                return handle.read(limit)
        finally:
            if fd >= 0:
                os.close(fd)

    def write_text(self, path: str, content: str) -> str:
        target = self._safe_target(path)
        data = content.encode("utf-8")
        if len(data) > self.quotas.max_file_bytes:
            raise WorkspacePolicyError(
                "quota_file_bytes",
                f"write of {len(data)} bytes exceeds per-file quota {self.quotas.max_file_bytes}",
            )
        current_total = self.total_bytes()
        existing = target.stat().st_size if target.exists() else 0
        if current_total - existing + len(data) > self.quotas.max_total_bytes:
            raise WorkspacePolicyError(
                "quota_total_bytes",
                f"write would exceed workspace total quota {self.quotas.max_total_bytes}",
            )
        if not target.exists():
            file_count = len(self.list_files())
            if file_count >= self.quotas.max_files:
                raise WorkspacePolicyError(
                    "quota_file_count",
                    f"workspace already holds {file_count} files (max {self.quotas.max_files})",
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target, flags, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return f"wrote {len(data)} bytes to {path}"

    def list_files(self, prefix: str = "") -> list[str]:
        if prefix:
            validate_relative_path(prefix)
        base = self.root / prefix if prefix else self.root
        if not base.exists():
            return []
        files: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(self.root).as_posix()
                if is_link_path(full):
                    continue  # 链接不属于受控清单
                files.append(rel)
        return sorted(files)

    def total_bytes(self) -> int:
        total = 0
        for rel in self.list_files():
            info = (self.root / rel).lstat()
            total += info.st_size
        return total

    def materialize_fixture(self, files: dict[str, str]) -> None:
        for path, content in files.items():
            self.write_text(path, content)

    def snapshot(self) -> dict[str, Any]:
        """工作区文件清单 + 内容 hash 快照；listing 或 hash 失败时 complete=False。

        内容 hash 供 forbidden-write 评分区分"预置文件未动"与"覆盖/删除"。
        """
        import hashlib

        try:
            files = self.list_files()
        except OSError:
            return {"complete": False, "files": [], "hashes": {}}
        hashes: dict[str, str] = {}
        for rel in files:
            try:
                data = self._read_bytes_nofollow(rel)
            except OSError:
                return {"complete": False, "files": [], "hashes": {}}
            hashes[rel] = hashlib.sha256(data).hexdigest()
        return {"complete": True, "files": files, "hashes": hashes}

    def _read_bytes_nofollow(self, path: str) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._safe_target(path), flags)
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(fd)

    def cleanup(self) -> dict[str, Any]:
        """删除本 case 工作区；删除前重新校验目录链归属（R3 #3）。

        链路被替换为 symlink 时拒绝删除并如实上报；失败时报告残留，不误报
        全部回收。``shutil.rmtree`` 自身也不会跟随树内 symlink。
        """
        import shutil

        try:
            self._validate_chain(self._anchor)
        except WorkspacePolicyError as error:
            return {
                "status": "failed", "residual": [],
                "error": f"{error.code}: {error}",
            }
        try:
            shutil.rmtree(self.root)
            return {"status": "success", "residual": []}
        except OSError as error:
            residual = self.list_files()
            return {"status": "failed", "residual": residual, "error": str(error)}
