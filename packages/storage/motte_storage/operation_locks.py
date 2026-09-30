"""Process-scoped locks for maintenance and ArtifactStore mutation.

Lock files live beside the database/artifact root, never among artifact evidence.
All locks are non-blocking: callers fail closed rather than hang or overwrite a
snapshot while an operator holds maintenance. OS locks are released on crash.
"""
from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator


class MaintenanceConflict(RuntimeError):
    """Another operation owns a maintenance or evidence mutation lock."""


def artifact_lock_path(root: str | Path) -> Path:
    root = Path(root).resolve()
    return root.with_name(root.name + ".motte.lock")


@contextmanager
def _windows_file_lock(handle, *, shared: bool) -> Iterator[None]:
    # LockFileEx supports real shared locks; CRT locking has no shared mode.
    # https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER(Overlapped)
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, pointer]
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, pointer]
    kernel.UnlockFileEx.restype = wintypes.BOOL
    descriptor = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
    overlapped = Overlapped()
    flags = 1 | (0 if shared else 2)  # FAIL_IMMEDIATELY, optionally EXCLUSIVE_LOCK
    if not kernel.LockFileEx(descriptor, flags, 0, 1, 0, ctypes.byref(overlapped)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not kernel.UnlockFileEx(descriptor, 0, 1, 0, ctypes.byref(overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def file_lock(path: str | Path, *, label: str, shared: bool = False) -> Iterator[None]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle, ExitStack() as locks:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                locks.enter_context(_windows_file_lock(handle, shared=shared))
            else:
                import fcntl

                mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                locks.callback(fcntl.flock, handle.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise MaintenanceConflict(f"{label} is held by another operation") from error
        yield


# Only explicit, live owner tokens can authorize maintenance's own file deletes.
# PID prevents a forked child from inheriting permission from its parent.
_ARTIFACT_OWNERS: dict[tuple[int, str, str], str] = {}


@contextmanager
def artifact_maintenance_lock(root: str | Path, owner: str, reason: str) -> Iterator[None]:
    root = str(Path(root).resolve())
    with file_lock(artifact_lock_path(root), label="artifact maintenance"):
        key = (os.getpid(), root, owner)
        _ARTIFACT_OWNERS[key] = reason
        try:
            yield
        finally:
            _ARTIFACT_OWNERS.pop(key, None)


@contextmanager
def artifact_mutation(root: str | Path, *, maintenance_owner: str | None = None) -> Iterator[None]:
    if maintenance_owner is not None:
        key = (os.getpid(), str(Path(root).resolve()), maintenance_owner)
        if _ARTIFACT_OWNERS.get(key) not in {"gc", "rollback", "import_rollback"}:
            raise MaintenanceConflict("artifact deletion requires the live maintenance owner")
        yield
        return
    with file_lock(artifact_lock_path(root), label="artifact mutation", shared=True):
        yield


# Archive writes are deliberately separate from artifact_mutation's deletion
# allowlist. A capability binds a DB store, PID, calling thread, root and token;
# a reason string or an artifact-only lock never grants this authority.
_TRACE_ARCHIVE_CAPABILITIES: dict[tuple[int, int, str, str], tuple[str, object]] = {}


def _trace_archive_owner(store, root: str, owner: str) -> None:
    from .maintenance import _ACTIVE_MAINTENANCE, _store_identity

    held = _ACTIVE_MAINTENANCE.get(owner)
    if (held is None or held[0] != _store_identity(store) or held[3] != os.getpid()
            or held[4] != root
            or _ARTIFACT_OWNERS.get((os.getpid(), root, owner)) != "trace_retention"):
        raise MaintenanceConflict("archive write requires the live store/root maintenance owner")


@contextmanager
def trace_archive_write_capability(
    store, *, artifacts_root: str | Path, maintenance_owner: str,
) -> Iterator[None]:
    """Grant only archive creation while this store's existing maintenance is live.

    The existing maintenance lock remains held across file I/O so another local
    thread cannot release the lease mid-write. A fork inherits neither authority
    nor permission to delete evidence. This context performs no database writes.
    """
    from threading import get_ident
    from .maintenance import _MAINTENANCE_LOCK, _store_identity

    root = str(Path(artifacts_root).resolve())
    key = (os.getpid(), get_ident(), root, maintenance_owner)
    # A fork from another thread may inherit this RLock permanently owned by a
    # vanished thread. Reject inherited/absent authority before touching it.
    _trace_archive_owner(store, root, maintenance_owner)
    with _MAINTENANCE_LOCK:
        _trace_archive_owner(store, root, maintenance_owner)
        if key in _TRACE_ARCHIVE_CAPABILITIES:
            raise MaintenanceConflict("archive write capability is already active")
        _TRACE_ARCHIVE_CAPABILITIES[key] = (_store_identity(store), store)
        try:
            yield
        finally:
            _TRACE_ARCHIVE_CAPABILITIES.pop(key, None)


@contextmanager
def trace_archive_mutation(root: str | Path, *, maintenance_owner: str) -> Iterator[None]:
    from threading import get_ident
    from .maintenance import _MAINTENANCE_LOCK, _store_identity

    root = str(Path(root).resolve())
    key = (os.getpid(), get_ident(), root, maintenance_owner)
    capability = _TRACE_ARCHIVE_CAPABILITIES.get(key)
    if capability is None or capability[0] != _store_identity(capability[1]):
        raise MaintenanceConflict("archive write requires a live store-bound capability")
    _trace_archive_owner(capability[1], root, maintenance_owner)
    with _MAINTENANCE_LOCK:
        capability = _TRACE_ARCHIVE_CAPABILITIES.get(key)
        if capability is None or capability[0] != _store_identity(capability[1]):
            raise MaintenanceConflict("archive write requires a live store-bound capability")
        _trace_archive_owner(capability[1], root, maintenance_owner)
        yield
