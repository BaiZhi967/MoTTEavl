"""Real PG gates run only in per-test isolated disposable databases."""
import pytest
from alembic import command

from motte_storage.migrations import alembic_config, current
from motte_storage.postgres import create_postgres_run_store
from tests.storage.test_calibrations import RepositoryContract, migration


class TestPostgresCalibrations(RepositoryContract):
    @pytest.fixture
    def store(self, isolated_pg_database):
        command.upgrade(alembic_config(isolated_pg_database), '0017_judge_calibrations')
        return create_postgres_run_store(isolated_pg_database)


def test_pg_migration_preserves_0016_and_blocks_evidence_downgrade(isolated_pg_database):
    import psycopg
    dsn = isolated_pg_database
    command.upgrade(alembic_config(dsn), '0016_statistical_reports')
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO statistical_reports VALUES ('prior', 'preserved', 'time')")
    command.upgrade(alembic_config(dsn), '0017_judge_calibrations')
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO judge_calibration_versions VALUES ('cal', '1', 'digest', 'damaged evidence')")
    with pytest.raises(RuntimeError, match='judge_calibration_versions'):
        command.downgrade(alembic_config(dsn), '0016_statistical_reports')
    assert current(dsn) == '0017_judge_calibrations'
    with psycopg.connect(dsn) as connection:
        assert connection.execute('SELECT body FROM statistical_reports').fetchone()[0] == 'preserved'


from tests.storage.test_calibrations import CorruptionContract


class TestPostgresCalibrationCorruption(CorruptionContract):
    @pytest.fixture
    def store(self, isolated_pg_database):
        command.upgrade(alembic_config(isolated_pg_database), '0017_judge_calibrations')
        return create_postgres_run_store(isolated_pg_database)


from tests.storage.test_calibrations import TransactionFaultContract


class TestPostgresCalibrationTransactions(TransactionFaultContract):
    @pytest.fixture
    def store(self, isolated_pg_database):
        command.upgrade(alembic_config(isolated_pg_database), '0017_judge_calibrations')
        return create_postgres_run_store(isolated_pg_database)


@pytest.mark.parametrize('isolation', ['REPEATABLE READ', 'SERIALIZABLE'])
def test_pg_downgrade_requires_read_committed(isolated_pg_database, isolation):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text
    from motte_storage.migrations import _psycopg_url
    dsn = isolated_pg_database
    command.upgrade(alembic_config(dsn), '0017_judge_calibrations')
    engine = create_engine(_psycopg_url(dsn), isolation_level=isolation)
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match='READ COMMITTED'):
                migration().downgrade()
            assert connection.execute(text("SELECT to_regclass('public.judge_calibration_versions')")).scalar_one()
    finally:
        engine.dispose()


def test_pg_active_maintenance_refuses_before_table_locks(isolated_pg_database):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine
    from motte_storage.maintenance import begin_maintenance, end_maintenance
    from motte_storage.migrations import _psycopg_url
    dsn = isolated_pg_database
    command.upgrade(alembic_config(dsn), '0017_judge_calibrations')
    store = create_postgres_run_store(dsn)
    engine = create_engine(_psycopg_url(dsn), connect_args={'options': '-c lock_timeout=1000'})
    lease = begin_maintenance(store)
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match='maintenance.*active'):
                migration().downgrade()
    finally:
        end_maintenance(store, owner=lease['owner'])
        engine.dispose()
    command.downgrade(alembic_config(dsn), '0016_statistical_reports')
    assert current(dsn) == '0016_statistical_reports'


def test_pg_downgrade_observes_inflight_calibration_commit(isolated_pg_database):
    from concurrent.futures import ThreadPoolExecutor
    import psycopg
    from tests.storage.test_statistical_reports_pg import _wait_for_peer_lock
    dsn = isolated_pg_database
    command.upgrade(alembic_config(dsn), '0017_judge_calibrations')
    row = ('cal', '1', 'digest', 'preserve even corrupt evidence')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with psycopg.connect(dsn) as writer:
            writer.execute('INSERT INTO judge_calibration_versions VALUES (%s, %s, %s, %s)', row)
            future = pool.submit(command.downgrade, alembic_config(dsn), '0016_statistical_reports')
            try:
                _wait_for_peer_lock(writer.execute, future)
                writer.commit()
                with pytest.raises(RuntimeError, match='judge_calibration_versions'):
                    future.result(timeout=10)
            finally:
                writer.rollback()
    with psycopg.connect(dsn) as connection:
        assert connection.execute('SELECT * FROM judge_calibration_versions').fetchall() == [row]


def test_pg_write_waits_between_empty_count_and_drop(isolated_pg_database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import psycopg
    from sqlalchemy import create_engine, text
    from motte_storage.migrations import _psycopg_url
    from tests.storage.test_statistical_reports_pg import _wait_for_peer_lock
    dsn = isolated_pg_database
    command.upgrade(alembic_config(dsn), '0017_judge_calibrations')
    module = migration()
    original = module.downgrade_blockers
    pending = []
    def insert():
        with psycopg.connect(dsn) as writer:
            writer.execute("INSERT INTO judge_calibration_versions VALUES ('cal', '1', 'digest', 'evidence')")
    engine = create_engine(_psycopg_url(dsn))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            def after_counts(bind):
                blockers = original(bind)
                assert blockers == []
                future = pool.submit(insert)
                pending.append(future)
                _wait_for_peer_lock(lambda sql: bind.execute(text(sql)), future)
                return blockers
            monkeypatch.setattr(module, 'downgrade_blockers', after_counts)
            with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
            with pytest.raises(psycopg.errors.UndefinedTable):
                pending[0].result(timeout=10)
    finally:
        engine.dispose()


def test_pg_publication_guard_excludes_gc_before_validation(isolated_pg_database):
    from motte_storage.calibrations import calibration_publication_guard
    from motte_storage.maintenance import begin_maintenance, end_maintenance
    from motte_storage.operation_locks import MaintenanceConflict
    dsn = isolated_pg_database
    command.upgrade(alembic_config(dsn), '0017_judge_calibrations')
    store = create_postgres_run_store(dsn)
    with calibration_publication_guard(store):
        with pytest.raises(MaintenanceConflict):
            begin_maintenance(create_postgres_run_store(dsn), reason='gc')
    lease = begin_maintenance(store, reason='gc')
    try:
        with pytest.raises(MaintenanceConflict):
            with calibration_publication_guard(store):
                pytest.fail('publication guard ignored active GC')
    finally:
        end_maintenance(store, owner=lease['owner'])


from tests.storage.test_calibrations import CalibrationEvidenceContract


class TestPostgresCalibrationEvidence(CalibrationEvidenceContract):
    @pytest.fixture
    def store(self, isolated_pg_database):
        command.upgrade(alembic_config(isolated_pg_database), '0017_judge_calibrations')
        return create_postgres_run_store(isolated_pg_database)


def test_pg_invocation_facade_exposes_required_calibration_edge():
    """No-server interface gate; behavioral coverage below requires disposable PG."""
    from motte_storage.pg_audit_store import PgInvocations
    assert callable(getattr(PgInvocations('postgresql://localhost/not-connected'), 'list_for_job', None))


def test_pg_invocations_for_calibration_job_are_scoped_and_detached(isolated_pg_database):
    from tests.storage.test_scoring_jobs import invocation_record
    dsn = isolated_pg_database
    command.upgrade(alembic_config(dsn), '0017_judge_calibrations')
    store = create_postgres_run_store(dsn)
    owner = {'kind': 'calibration', 'calibration_job_id': 'fixture-group', 'sample_id': 'sample-1'}
    first = {**invocation_record('first', owner=owner), 'job_id': 'first-child'}
    second = {**invocation_record('second', owner=owner), 'job_id': 'second-child'}
    saved = store.invocations.create(first)
    store.invocations.create(second)
    found = store.invocations.list_for_job('first-child')
    assert found == [saved]
    found[0]['request_summary']['mutated'] = True
    assert store.invocations.list_for_job('first-child') == [saved]
    assert store.invocations.list_for_job('absent-child') == []
    assert store.runs.list() == []
