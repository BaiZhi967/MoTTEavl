"""Per-case controlled workspace for builtin agent file tools.

每个 Case 独立目录；工具只接受受控相对路径。安全边界：
- 拒绝绝对路径、``..``、反斜杠与盘符形状；
- 逐组件拒绝 symlink（打开用 ``O_NOFOLLOW``，写前检查父链），防逃逸与 TOCTOU 替换；
- 拒绝设备/管道/套接字文件；
- 每文件 / 总量 / 文件数配额在写入前强制；
- 不挂载宿主凭据、不提供网络（文件工具本身无网络面）。
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


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
    """One case's workspace directory under a run-scoped controlled root."""

    def __init__(
        self, root: str | Path, *, quotas: WorkspaceQuotas | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.quotas = quotas or WorkspaceQuotas()
        self.root.mkdir(parents=True, exist_ok=True)

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
            if stat.S_ISLNK(info.st_mode):
                raise WorkspacePolicyError(
                    "symlink_rejected", f"symlink components are not allowed: {part!r}"
                )
        resolved = target.resolve(strict=False)
        if self.root != resolved and self.root not in resolved.parents:
            raise WorkspacePolicyError("path_escape", f"resolved path escapes workspace: {path!r}")
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode):
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
                if full.is_symlink():
                    continue  # 符号链接不属于受控清单
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
        """工作区文件清单快照；listing 自身失败时 complete=False。"""
        try:
            files = self.list_files()
            return {"complete": True, "files": files}
        except OSError:
            return {"complete": False, "files": []}

    def cleanup(self) -> dict[str, Any]:
        """删除本 case 工作区；失败时报告残留，不误报全部回收。"""
        import shutil

        try:
            shutil.rmtree(self.root)
            return {"status": "success", "residual": []}
        except OSError as error:
            residual = self.list_files()
            return {"status": "failed", "residual": residual, "error": str(error)}
