"""内容寻址的 Skill 资源字节存储（跨 API/Worker 进程持久）。

发布期核验与运行时读取共用同一协议（M5-T06 / R5）::

    put(data: bytes) -> "sha256:<hex>"   # 幂等；同 hash 并发写安全
    get(ref: str) -> bytes | None        # 缺失返回 None；payload 损坏抛 ContentStoreCorruption
    list() -> list[str]                  # 已落库的 ref（排序）
    drop(ref: str) -> None               # 运维/测试用途

* ContentAddressedMemoryStore 是内存实现：同一协议，但只在单进程内有效，仅用于
  测试与草稿预览，**不能**当作发布端的字节保证。
* FileContentStore 把字节落到 <root>/sha256/<前两位>/<其余>，写路径是
  "临时文件 → fsync → 原子 rename"，因此进程在中途被打断时，半个 blob 永远不会
  出现在最终路径上；读取时重新计算 sha256，损坏的 payload 一律拒绝（fail closed），
  而不是把不一致的字节交给调用方。

API 与 Worker 必须指向**同一个** CONTENT_ROOT_ENV 目录（默认 var/skill-content，
与 MOTTE_DB_PATH 同级惯例）；SQLite 与 PostgreSQL 后端都通过共享卷/HostPath 提供该
目录。create_content_store() 读取该变量。
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from threading import Lock
from typing import Protocol, runtime_checkable

#: 内容 ref 的规范形状：sha256:<64 hex>。
CONTENT_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
#: API 与 Worker 共享的内容根目录；默认与 MOTTE_DB_PATH 同级的 var/ 目录。
CONTENT_ROOT_ENV = "MOTTE_SKILL_CONTENT_ROOT"
DEFAULT_CONTENT_ROOT = "var/skill-content"


class ContentStoreError(ValueError):
    """内容存储错误的基类。"""


class ContentStoreCorruption(ContentStoreError):
    """payload 与其内容地址不一致（磁盘损坏/被改写）：绝不返回这些字节。"""


def content_ref(data: bytes) -> str:
    """字节的内容地址。"""
    return "sha256:" + hashlib.sha256(bytes(data)).hexdigest()


def _digest(ref: str) -> str:
    if not isinstance(ref, str) or CONTENT_REF.fullmatch(ref) is None:
        raise ContentStoreError(f"not a content address: {ref!r}")
    return ref.removeprefix("sha256:")


@runtime_checkable
class ContentStore(Protocol):
    """内容寻址存储协议；发布与执行只依赖这两个方法。"""

    def put(self, data: bytes) -> str:  # pragma: no cover - 协议声明
        ...

    def get(self, ref: str) -> bytes | None:  # pragma: no cover - 协议声明
        ...


def is_content_store(store: object) -> bool:
    """存储是否能按内容地址读回字节；None、字符串、无 get 的对象一律 False。

    发布核验只依赖读能力（写入由调用方的 ingest 负责）；没有 get 的对象既不能
    核验字节，也不能当作"没有字节要验"。
    """
    return callable(getattr(store, "get", None))


class ContentAddressedMemoryStore:
    """进程内内容寻址存储：put(bytes) -> "sha256:<hex>" / get(ref) -> bytes | None。"""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        payload = bytes(data)
        ref = content_ref(payload)
        self._blobs.setdefault(ref, payload)
        return ref

    def get(self, ref: str) -> bytes | None:
        return self._blobs.get(ref)

    def list(self) -> list[str]:
        return sorted(self._blobs)

    def drop(self, ref: str) -> None:
        self._blobs.pop(ref, None)


#: Windows 上并发 rename 到同一目标可能被短暂占用（ERROR_ACCESS_DENIED）；
#: 内容相同，因此重试与"目标已是同字节即成功"等价，不改变任何可观察语义。
_COMMIT_ATTEMPTS = 10
_COMMIT_BACKOFF_SECONDS = 0.01


def _commit_blob(source: Path, target: Path) -> None:
    """原子提交；单独成函数，便于测试模拟 "rename 之前进程被打断"。"""
    last: PermissionError | None = None
    for attempt in range(_COMMIT_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:  # pragma: no cover - Windows 平台行为
            last = error
            time.sleep(_COMMIT_BACKOFF_SECONDS * (attempt + 1))
    assert last is not None
    raise last


def _fsync_directory(path: Path) -> None:
    """目录项落盘（best effort：Windows 上无法打开目录句柄）。"""
    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - 平台差异
        return
    try:
        os.fsync(handle)
    except OSError:  # pragma: no cover - 平台差异
        pass
    finally:
        os.close(handle)


def _read_verified(blob: Path, ref: str) -> bytes:
    payload = blob.read_bytes()
    if content_ref(payload) != ref:
        raise ContentStoreCorruption(f"content payload at {ref} does not match its digest")
    return payload


class FileContentStore:
    """文件系统内容寻址存储：写原子、读校验、可跨进程重启读取。"""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)
        self._blob_root = self._root / "sha256"
        self._temp_root = self._root / "tmp"
        self._blob_root.mkdir(parents=True, exist_ok=True)
        self._temp_root.mkdir(parents=True, exist_ok=True)
        self._commit_lock = Lock()

    @property
    def root(self) -> Path:
        return self._root

    def _blob(self, ref: str) -> Path:
        digest = _digest(ref)
        return self._blob_root / digest[:2] / digest

    def put(self, data: bytes) -> str:
        payload = bytes(data)
        ref = content_ref(payload)
        blob = self._blob(ref)
        if blob.is_file():
            try:
                if _read_verified(blob, ref) == payload:
                    return ref
            except (ContentStoreCorruption, OSError):
                pass  # 损坏的 blob 用真实字节覆盖修复
        blob.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=self._temp_root, prefix="blob-")
        temp: Path | None = Path(name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            with self._commit_lock:
                try:
                    _commit_blob(temp, blob)
                except OSError:
                    # 并发写者可能刚好提交了同一份字节：那等价于本次写入成功。
                    if not (blob.is_file() and _read_verified(blob, ref) == payload):
                        raise
            temp = None
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        _fsync_directory(blob.parent)
        return ref

    def get(self, ref: str) -> bytes | None:
        blob = self._blob(ref)
        if not blob.is_file():
            return None
        return _read_verified(blob, ref)

    def list(self) -> list[str]:
        refs: list[str] = []
        for path in self._blob_root.glob("*/*"):
            if path.is_file() and path.parent.name == path.name[:2] and (
                CONTENT_REF.fullmatch(f"sha256:{path.name}") is not None
            ):
                refs.append(f"sha256:{path.name}")
        return sorted(refs)

    def drop(self, ref: str) -> None:
        self._blob(ref).unlink(missing_ok=True)


def create_content_store(root: str | os.PathLike[str] | None = None) -> FileContentStore:
    """API 与 Worker 共用的内容存储工厂。

    根目录优先取显式参数，其次 CONTENT_ROOT_ENV，最后 DEFAULT_CONTENT_ROOT。两个进程
    必须指向同一目录，否则发布期核验通过的字节在 Worker 侧读不到。
    """
    resolved = root or os.environ.get(CONTENT_ROOT_ENV) or DEFAULT_CONTENT_ROOT
    return FileContentStore(resolved)
