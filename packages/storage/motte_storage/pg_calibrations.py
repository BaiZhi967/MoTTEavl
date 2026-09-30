"""PostgreSQL calibration repository: shared validation, one transaction per group."""
from contextlib import contextmanager, nullcontext

from .calibrations import SQLiteCalibrations, calibration_publication_guard


class PgCalibrations(SQLiteCalibrations):
    def __init__(self, dsn):
        self._dsn = dsn
        self._placeholder = '%s'

    @contextmanager
    def _transaction(self, write=False):
        from .postgres import _connect

        with calibration_publication_guard(self._store) if write else nullcontext():
            with _connect(self._dsn) as connection:
                if write:
                    # Serialize absent-key inserts as well as all multi-source writes.
                    connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                       ('motteavl:calibration-sources',))
                else:
                    connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                yield connection
