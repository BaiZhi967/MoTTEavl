"""Versioned SQL entry point for PostgreSQL migrations."""

INITIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL
);
""".strip()


def migration_sql() -> tuple[str, ...]:
    """Return migrations in application order for external migration runners."""

    return (INITIAL_SCHEMA,)


def apply_migrations(connection) -> None:
    """Apply the bundled SQL using a DB-API connection supplied by the caller."""

    with connection.cursor() as cursor:
        for statement in migration_sql():
            cursor.execute(statement)
    connection.commit()
