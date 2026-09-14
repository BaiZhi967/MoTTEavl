"""PostgreSQL storage boundary.

The local foundation intentionally does not install a PostgreSQL driver.  This
module validates configuration and fails explicitly until an application adds
one of the supported optional drivers, rather than silently falling back to
SQLite or pretending that a connection exists.
"""

from __future__ import annotations

import importlib.util
from urllib.parse import urlparse


class UnsupportedStorageError(RuntimeError):
    """Raised when PostgreSQL storage cannot be used in the current install."""


def _validate_dsn(dsn: str) -> None:
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("PostgreSQL DSN must use postgres:// or postgresql://")
    if not parsed.hostname:
        raise ValueError("PostgreSQL DSN must include a hostname")


class PostgresRepository:
    """Reserved repository boundary for a future PostgreSQL implementation."""

    def __init__(self, dsn: str) -> None:
        _validate_dsn(dsn)
        self.dsn = dsn
        if importlib.util.find_spec("psycopg") is None:
            raise UnsupportedStorageError(
                "PostgreSQL storage requires the optional 'psycopg' package; "
                "install motte-storage[postgres] to enable it"
            )
        raise UnsupportedStorageError(
            "PostgresRepository driver support is not enabled in this foundation release"
        )


def create_postgres_repository(dsn: str) -> PostgresRepository:
    """Construct the PostgreSQL boundary after validating its DSN."""

    return PostgresRepository(dsn)
