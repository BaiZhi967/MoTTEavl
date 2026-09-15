"""初始 schema：ProviderConnection / ModelProfile / PriceTable / DatasetVersion /
ScenarioVersion / Run / CaseRun / TraceEvent / Artifact / Score。

run 执行链四表（runs/case_runs/trace_events/scores）与 SQLiteRunStore 同构；
payload 统一 JSONB，唯一约束保证幂等： (run_id, seq)、(run_id, case_id)。
"""

revision = "0001_initial"
down_revision = None

up = (
    """
    CREATE TABLE provider_connections (
        name TEXT PRIMARY KEY,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE model_profiles (
        id TEXT PRIMARY KEY,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE price_tables (
        model_id TEXT NOT NULL,
        version TEXT NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (model_id, version)
    )
    """,
    """
    CREATE TABLE dataset_versions (
        name TEXT NOT NULL,
        version TEXT NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (name, version)
    )
    """,
    """
    CREATE TABLE scenario_versions (
        name TEXT NOT NULL,
        version TEXT NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (name, version)
    )
    """,
    """
    CREATE TABLE runs (
        position BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        id TEXT NOT NULL UNIQUE,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX runs_status_idx ON runs ((payload->>'status'))
    """,
    """
    CREATE TABLE case_runs (
        run_id TEXT NOT NULL,
        case_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (run_id, case_id)
    )
    """,
    """
    CREATE TABLE trace_events (
        run_id TEXT NOT NULL,
        seq BIGINT NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (run_id, seq)
    )
    """,
    """
    CREATE TABLE scores (
        run_id TEXT NOT NULL,
        case_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        payload JSONB NOT NULL,
        PRIMARY KEY (run_id, case_id)
    )
    """,
    """
    CREATE TABLE artifacts (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        name TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (run_id, name)
    )
    """,
)

down = (
    "DROP TABLE IF EXISTS artifacts",
    "DROP TABLE IF EXISTS scores",
    "DROP TABLE IF EXISTS trace_events",
    "DROP TABLE IF EXISTS case_runs",
    "DROP INDEX IF EXISTS runs_status_idx",
    "DROP TABLE IF EXISTS runs",
    "DROP TABLE IF EXISTS scenario_versions",
    "DROP TABLE IF EXISTS dataset_versions",
    "DROP TABLE IF EXISTS price_tables",
    "DROP TABLE IF EXISTS model_profiles",
    "DROP TABLE IF EXISTS provider_connections",
)
