"""fd 锚定的受控目录读取（M2 review R06，M3 复用）。

信任边界固定在调用方给定的目录：沿目录描述符逐组件 ``O_NOFOLLOW`` 打开，
symlink 不进入受控清单（根目录本身是 symlink 也拒绝），文件读取先 ``fstat``
校验大小再读全量。任务包准备、Runner 输出冻结与 Parser 共用这一个实现，
避免每个 benchmark 各自维护一套路径安全逻辑。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Callable


class TrustedPathError(ValueError):
    """受控目录读取失败（越界、symlink、超限、非常规文件）。"""


ErrorFactory = Callable[[str], Exception]


def _default_error_factory(message: str) -> Exception:
    return TrustedPathError(message)


class TrustedDir:
    """fd 锚定的受信目录读取。

    ``error_factory`` 让调用方保留自己的异常类型（历史消息前缀不变）。
    """

    def __init__(self, path: Path | str, *, error_factory: ErrorFactory | None = None) -> None:
        self._raise = error_factory or _default_error_factory
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._fd = os.open(Path(path), flags)
        except OSError as error:
            raise self._raise(
                f"path-escape: trusted root rejected: {path}: {error}",
            ) from error

    def __enter__(self) -> TrustedDir:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def list_files(self) -> tuple[list[str], list[str]]:
        """受信目录下的常规文件与被排除的 symlink（posix 相对路径）。

        symlink 不进入受控清单；调用方对输出边界内的 symlink 需要显式拒绝
        （防止静默丢结果/篡改聚合）。
        """
        out: list[str] = []
        symlinks: list[str] = []
        self._walk(self._fd, "", out, symlinks)
        return (sorted(out), sorted(symlinks))

    def _walk(
        self, dir_fd: int, prefix: str, out: list[str], symlinks: list[str],
    ) -> None:
        for name in os.listdir(dir_fd):
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                symlinks.append(prefix + name)
                continue
            if stat.S_ISDIR(info.st_mode):
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                child = os.open(name, flags, dir_fd=dir_fd)
                try:
                    self._walk(child, prefix + name + "/", out, symlinks)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                out.append(prefix + name)

    def read_bytes(self, rel: str, *, max_bytes: int) -> bytes:
        parts = [part for part in PurePosixPath(rel).parts if part]
        if not parts:
            raise self._raise(f"path-escape: not a file: {rel}")
        dir_fd = os.dup(self._fd)
        try:
            for part in parts[:-1]:
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    child = os.open(part, flags, dir_fd=dir_fd)
                except OSError as error:
                    raise self._raise(
                        f"path-escape: component rejected: {part}: {error}",
                    ) from error
                os.close(dir_fd)
                dir_fd = child
            file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                file_fd = os.open(parts[-1], file_flags, dir_fd=dir_fd)
            except OSError as error:
                raise self._raise(
                    f"path-escape: file rejected: {parts[-1]}: {error}",
                ) from error
        finally:
            os.close(dir_fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise self._raise(f"path-escape: not a regular file: {rel}")
            if info.st_size > max_bytes:
                raise self._raise(
                    f"file-size: {rel} is {info.st_size} bytes > limit {max_bytes}",
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(file_fd, 1 << 16)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise self._raise(
                        f"file-size: {rel} exceeds limit {max_bytes} while reading",
                    )
        finally:
            os.close(file_fd)
