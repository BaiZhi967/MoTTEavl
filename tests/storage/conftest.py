"""Disposable PostgreSQL database for migration and transaction tests.

The caller supplies a test-cluster DSN. No rows in its original database are
changed; each requesting test owns and drops only the random database it made.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest


@pytest.fixture
def isolated_pg_database() -> str:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from motte_storage.postgres import normalize_dsn

    source_dsn = os.environ.get("MOTTE_PG_DSN")
    if not source_dsn:
        pytest.skip("set MOTTE_PG_DSN to use a disposable PostgreSQL test cluster")
    source_dsn = normalize_dsn(source_dsn)
    database = "m8_test_" + uuid4().hex
    dsn = make_conninfo(source_dsn, dbname=database)
    with psycopg.connect(source_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        yield dsn
    finally:
        with psycopg.connect(source_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
