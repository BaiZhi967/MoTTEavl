"""Disposable PostgreSQL database for migration and transaction tests.

The caller supplies a test-cluster DSN. No rows in its original database are
changed; each requesting test owns and drops only the random database it made.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest


def isolated_database_uri(source_dsn: str, database: str) -> str:
    """Keep the URL form while removing libpq query keys overriding the path."""
    from psycopg.conninfo import conninfo_to_dict

    parsed = urlsplit(source_dsn)
    query = urlencode([
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"dbname", "database"}
    ])
    target = urlunsplit(parsed._replace(path="/" + database, query=query))
    if conninfo_to_dict(target).get("dbname") != database:
        raise ValueError("disposable PostgreSQL target database identity mismatch")
    return target


def require_loopback_pg_cluster(source_dsn: str) -> str:
    """Reject query-level host overrides before creating disposable databases."""
    from psycopg.conninfo import conninfo_to_dict
    from motte_storage.postgres import normalize_dsn

    dsn = normalize_dsn(source_dsn)
    params = conninfo_to_dict(dsn)
    loopback = {"localhost", "127.0.0.1", "::1"}
    if (params.get("host") not in loopback
            or params.get("hostaddr") not in (None, "", *loopback)
            or params.get("service")):
        raise ValueError("disposable PostgreSQL fixture requires an effective loopback host")
    return dsn


@pytest.fixture
def isolated_pg_database() -> str:
    import psycopg
    from psycopg import sql

    source_dsn = os.environ.get("MOTTE_PG_DSN")
    if not source_dsn:
        pytest.skip("set MOTTE_PG_DSN to use a disposable PostgreSQL test cluster")
    try:
        source_dsn = require_loopback_pg_cluster(source_dsn)
    except ValueError as error:
        pytest.skip(str(error))
    database = "m8_test_" + uuid4().hex
    dsn = isolated_database_uri(source_dsn, database)
    with psycopg.connect(source_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        with psycopg.connect(dsn) as target, target.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            assert cursor.fetchone()[0] == database
        yield dsn
    finally:
        with psycopg.connect(source_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
