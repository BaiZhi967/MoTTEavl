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
