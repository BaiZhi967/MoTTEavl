"""Cross-process execution lock shared by Worker, Celery, and inline CLI dispatch."""
from __future__ import annotations

import os
from contextlib import AbstractContextManager
from pathlib import Path
from typing import IO, Any


class WorkerAlreadyRunning(RuntimeError):
    pass


class WorkerExecutionLock(AbstractContextManager["WorkerExecutionLock"]):
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        backend: str | None = None,
        postgres_dsn: str | None = None,
    ) -> None:
        self._db_path = db_path
        self._backend = backend
        self._postgres_dsn = postgres_dsn
        self._file: IO[bytes] | None = None
        self._connection: Any = None
        self.identity = ""

    def __enter__(self) -> "WorkerExecutionLock":
        backend = self._backend or (
            "sqlite" if self._db_path is not None else os.environ.get("MOTTE_STORAGE", "sqlite")
        )
        if backend == "postgres":
            self._acquire_postgres(self._postgres_dsn)
        elif backend == "sqlite":
            self._acquire_sqlite()
        else:
            raise ValueError(f"unsupported storage backend for execution lock: {backend!r}")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._connection is not None:
            try:
                with self._connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("motteavl:executor",))
            finally:
                self._connection.close()
                self._connection = None
        if self._file is not None:
            try:
                self._unlock_file(self._file)
            finally:
                self._file.close()
                self._file = None

    def _acquire_sqlite(self) -> None:
        path = Path(self._db_path or os.environ.get("MOTTE_DB_PATH", "var/runs.db")).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(f"{path}.worker.lock")
        handle = lock_path.open("a+b")
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            self._lock_file(handle)
        except BaseException:
            handle.close()
            raise
        self._file = handle
        self.identity = str(path)

    def _acquire_postgres(self, dsn: str | None = None) -> None:
        from motte_storage.postgres import _connect, normalize_dsn

        dsn = dsn or os.environ.get("MOTTE_PG_DSN") or os.environ.get("DATABASE_URL")
        if not dsn:
            raise ValueError("postgres execution lock requires MOTTE_PG_DSN or DATABASE_URL")
        connection = _connect(normalize_dsn(dsn))
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", ("motteavl:executor",))
            acquired = bool(cursor.fetchone()[0])
        if not acquired:
            connection.close()
            raise WorkerAlreadyRunning("another PostgreSQL executor holds the lock")
        self._connection = connection
        self.identity = "postgresql:motteavl:executor"

    @staticmethod
    def _lock_file(handle: IO[bytes]) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise WorkerAlreadyRunning("another SQLite executor holds the lock") from error
            return
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise WorkerAlreadyRunning("another SQLite executor holds the lock") from error

    @staticmethod
    def _unlock_file(handle: IO[bytes]) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def worker_execution_lock(
    db_path: str | Path | None = None,
    *,
    backend: str | None = None,
    postgres_dsn: str | None = None,
) -> WorkerExecutionLock:
    return WorkerExecutionLock(db_path, backend=backend, postgres_dsn=postgres_dsn)
