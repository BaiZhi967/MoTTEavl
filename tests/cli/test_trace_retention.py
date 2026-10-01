"""Explicit local operations against disposable synthetic databases only."""
from __future__ import annotations

import importlib
import json

import pytest

from motte_cli.main import main
from motte_contracts.identity import canonical_json_bytes
from motte_storage.run_store import SQLiteRunStore
from tests.api.test_trace_retention import CONFIG, RUN, seed


def invoke(capsys, *args):
    try:
        code = main(['trace-retention', *args])
    except SystemExit as error:
        code = error.code
    output = capsys.readouterr()
    return code, output.out, output.err


def test_cli_is_explicit_and_local_only(tmp_path, capsys, monkeypatch):
    db = tmp_path / 'db.sqlite'
    store = SQLiteRunStore(db)
    seed(store, monkeypatch)
    before = store.events.list_for_run(RUN)
    target = tmp_path / 'plan.json'
    monkeypatch.setenv('MOTTE_TRACE_RETENTION_ENABLED', 'true')
    monkeypatch.setenv('MOTTE_TRACE_RETENTION_DAYS', '7')
    code, out, err = invoke(capsys, 'plan', '--db', str(db), '--output', str(target))
    assert code == 0, err
    plan = json.loads(target.read_bytes())
    assert plan['prefixes'] == [] and plan['cutoff'] is None
    assert plan['config'] == {'enabled': False, 'retention_days': None}
    assert target.read_bytes() == canonical_json_bytes(plan)
    config = tmp_path / 'config.json'
    config.write_text(CONFIG.model_dump_json())
    root = tmp_path / 'artifacts'
    root.mkdir()
    args = ('apply', '--db', str(db), '--config', str(config), '--plan', str(target),
            '--artifacts-root', str(root))
    assert invoke(capsys, *args)[0] == 2
    assert invoke(capsys, *args, '--confirm', '--mode', 'server')[0] == 2
    assert store.events.list_for_run(RUN) == before
    assert store.trace_archives.list() == []
    assert list(root.iterdir()) == []
    remote_target = tmp_path / 'remote-plan.json'
    assert invoke(capsys, 'plan', '--mode', 'server', '--output', str(remote_target))[0] == 2
    assert not remote_target.exists()


def test_cli_applies_only_saved_exact_plan(tmp_path, capsys, monkeypatch):
    db = tmp_path / 'db.sqlite'
    store = SQLiteRunStore(db)
    seed(store, monkeypatch)
    root = tmp_path / 'artifacts'
    root.mkdir()
    config = tmp_path / 'config.json'
    config.write_text(CONFIG.model_dump_json())
    plan = tmp_path / 'plan.json'
    planning = ('plan', '--db', str(db), '--config', str(config), '--output', str(plan))
    assert invoke(capsys, *planning)[0] == 0
    assert len(json.loads(plan.read_bytes())['prefixes']) == 1
    current = store.runs.get(RUN)
    store.runs.update(current, expected_revision=current['revision'], expected_status='completed')
    applying = ('apply', '--db', str(db), '--config', str(config), '--plan', str(plan),
                '--artifacts-root', str(root), '--confirm')
    assert invoke(capsys, *applying)[0] == 2
    assert len(store.events.list_for_run(RUN)) == 4
    assert invoke(capsys, *planning)[0] == 0
    code, out, err = invoke(capsys, *applying)
    assert code == 0, err
    assert json.loads(out)['trimmed_events'] == 3
    assert [row['seq'] for row in store.events.list_for_run(RUN)] == [4]
    code, out, err = invoke(capsys, *applying)
    assert code == 0, err
    assert json.loads(out)['trimmed_events'] == 0


@pytest.mark.parametrize('config', [{}, {'enabled': True}, {'enabled': True, 'retention_days': True}])
def test_cli_invalid_or_disabled_apply_leaves_store_untouched(tmp_path, capsys, monkeypatch, config):
    db = tmp_path / 'db.sqlite'
    store = SQLiteRunStore(db)
    seed(store, monkeypatch)
    config_file = tmp_path / 'config.json'
    config_file.write_text(json.dumps(config))
    missing = tmp_path / 'missing.json'
    assert invoke(capsys, 'apply', '--db', str(db), '--config', str(config_file),
                  '--plan', str(missing), '--artifacts-root', str(tmp_path), '--confirm')[0] == 2
    assert len(store.events.list_for_run(RUN)) == 4


def test_sdk_reexports_identical_retention_interfaces():
    module = importlib.import_module('motte_sdk.trace_retention')
    from motte_storage import trace_retention, trace_retention_models
    for name in ('plan_trace_retention', 'apply_trace_retention', 'collect_trace_protection'):
        assert getattr(module, name) is getattr(trace_retention, name)
    for name in ('TraceRetentionConfig', 'TraceRetentionPlan', 'TraceRetentionResult',
                 'TraceEventWindow', 'TraceArchiveReceipt', 'TracePrefix', 'TraceProtection'):
        assert getattr(module, name) is getattr(trace_retention_models, name)


def test_plan_does_not_create_or_upgrade_sqlite_database(tmp_path, capsys):
    import sqlite3
    db = tmp_path / 'legacy.sqlite'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE legacy (value TEXT)')
    before = db.read_bytes()
    output = tmp_path / 'plan.json'
    assert invoke(capsys, 'plan', '--db', str(db), '--output', str(output))[0] == 2
    assert db.read_bytes() == before
    assert not output.exists()
    absent = tmp_path / 'missing.sqlite'
    assert invoke(capsys, 'plan', '--db', str(absent), '--output', str(output))[0] == 2
    assert not absent.exists()


def test_plan_executes_no_ddl_on_current_database(tmp_path, capsys, monkeypatch):
    import sqlite3
    db = tmp_path / 'current.sqlite'
    store = SQLiteRunStore(db)
    seed(store, monkeypatch)
    with sqlite3.connect(db) as connection:
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    before = db.read_bytes()
    statements = []
    connect = sqlite3.connect
    def observe(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', observe)
    config = tmp_path / 'config.json'
    config.write_text(CONFIG.model_dump_json())
    assert invoke(capsys, 'plan', '--db', str(db), '--config', str(config),
                  '--output', str(tmp_path / 'plan.json'))[0] == 0
    assert not [sql for sql in statements if sql.strip().split()[0].upper() in
                {'CREATE', 'ALTER', 'DROP', 'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}]
    assert db.read_bytes() == before


@pytest.mark.parametrize('alias', ['same', 'symlink', 'hardlink'])
def test_plan_output_cannot_overwrite_source_database(tmp_path, capsys, alias):
    import os
    db = tmp_path / 'runs.sqlite'
    store = SQLiteRunStore(db)
    output = db
    if alias != 'same':
        output = tmp_path / 'alias.json'
        if alias == 'symlink':
            output.symlink_to(db)
        else:
            os.link(db, output)
    before = db.read_bytes()
    assert invoke(capsys, 'plan', '--db', str(db), '--output', str(output))[0] == 2
    assert db.read_bytes() == before
    assert store.trace_archives.list() == []


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
@pytest.mark.parametrize('output_alias', ['direct', 'symlink', 'hardlink'])
@pytest.mark.parametrize('selector_alias', [False, True])
def test_plan_preserves_resolved_database_sidecars(
        tmp_path, capsys, monkeypatch, suffix, output_alias, selector_alias):
    """A symlinked selector still protects the real WAL and committed rows."""
    import os
    import sqlite3
    from pathlib import Path

    monkeypatch.setenv('MOTTE_STORAGE', 'sqlite')
    monkeypatch.delenv('MOTTE_CLI_MODE', raising=False)
    database = tmp_path / 'real.sqlite'
    SQLiteRunStore(database)
    selector = tmp_path / 'selected.sqlite'
    selector.symlink_to(database)
    keeper = sqlite3.connect(database)
    try:
        keeper.execute("INSERT INTO motte_meta(meta_key, meta_value) VALUES ('sidecar-probe', 'committed')")
        keeper.commit()
        wal = Path(str(database) + '-wal')
        wal_before = wal.read_bytes()
        assert wal_before[:4] in (bytes.fromhex('377f0682'), bytes.fromhex('377f0683'))
        protected = Path(str(database) + suffix)
        if suffix == '-journal':
            # A reserved rollback-journal name is protected even in WAL mode.
            protected.write_bytes(b'synthetic sidecar sentinel')
        before = protected.read_bytes()
        output = protected
        if output_alias != 'direct':
            output = tmp_path / 'output.json'
            if output_alias == 'symlink':
                output.symlink_to(protected)
            else:
                os.link(protected, output)
        code, out, error = invoke(capsys, 'plan', '--db', str(selector if selector_alias else database),
                                   '--output', str(output))
        assert code == 2, (out, error)
        assert protected.read_bytes() == before
        assert wal.read_bytes() == wal_before
        assert keeper.execute(
            "SELECT meta_value FROM motte_meta WHERE meta_key='sidecar-probe'"
        ).fetchone() == ('committed',)
    finally:
        # A broken implementation truncates the still-mapped SHM fixture. Restore
        # only these disposable bytes before close so RED is an assertion rather
        # than SIGBUS in SQLite's cleanup. Success must already preserve bytes.
        if suffix == '-shm' and protected.read_bytes() != before:
            protected.write_bytes(before)
        keeper.close()
        if suffix == '-journal':
            # Remove the deliberately synthetic journal marker before opening
            # SQLite again; it is not a valid recoverable rollback journal.
            protected.unlink()
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as reopened:
        assert reopened.execute(
            "SELECT meta_value FROM motte_meta WHERE meta_key='sidecar-probe'"
        ).fetchone() == ('committed',)


@pytest.mark.parametrize('operation', ['plan', 'unconfirmed_apply', 'confirmed_apply'])
def test_sqlite_retention_has_no_new_optional_postgres_dependency(
        tmp_path, capsys, monkeypatch, operation):
    import builtins
    # Isolate the new CLI dependency edge; the inherited eager storage factory
    # imports are repaired separately. This is not a clean-wheel installation test.
    from motte_cli import trace_retention
    from motte_sdk import trace_retention as sdk_retention
    from motte_storage import factory, maintenance

    monkeypatch.setenv('MOTTE_STORAGE', 'sqlite')
    monkeypatch.delenv('MOTTE_CLI_MODE', raising=False)
    db = tmp_path / 'runs.sqlite'
    store = SQLiteRunStore(db)
    seed(store, monkeypatch)
    config = tmp_path / 'config.json'
    config.write_text(CONFIG.model_dump_json())
    plan = tmp_path / 'plan.json'
    plan.write_text(sdk_retention.plan_trace_retention(store, config=CONFIG).model_dump_json())
    root = tmp_path / 'artifacts'
    root.mkdir()
    real_import = builtins.__import__
    blocked = []
    def without_optional_driver(name, *args, **kwargs):
        if name == 'psycopg' or name.startswith('psycopg.'):
            blocked.append(name)
            raise ModuleNotFoundError("No module named 'psycopg'")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_optional_driver)
    if operation == 'plan':
        args = ('plan', '--db', str(db), '--output', str(plan))
        expected = 0
    else:
        args = ('apply', '--db', str(db), '--config', str(config), '--plan', str(plan),
                '--artifacts-root', str(root))
        if operation == 'confirmed_apply':
            args += ('--confirm',)
            expected = 0
        else:
            expected = 2
    assert invoke(capsys, *args)[0] == expected
    assert blocked == []
    assert len(store.events.list_for_run(RUN)) == (1 if operation == 'confirmed_apply' else 4)


def test_postgres_retention_cli_reports_actual_driver_failure(
        isolated_pg_database, tmp_path, capsys, monkeypatch):
    from pathlib import Path
    from motte_storage.postgres import create_postgres_run_store
    from psycopg import connect

    store = create_postgres_run_store(isolated_pg_database, migrate=True)
    seed(store, monkeypatch)
    monkeypatch.setenv('MOTTE_STORAGE', 'postgres')
    monkeypatch.setenv('MOTTE_PG_DSN', isolated_pg_database)
    monkeypatch.delenv('MOTTE_CLI_MODE', raising=False)
    config = tmp_path / 'config.json'
    config.write_text(CONFIG.model_dump_json())
    target = tmp_path / 'plan.json'
    assert invoke(capsys, 'plan', '--config', str(config), '--output', str(target))[0] == 0
    assert len(json.loads(target.read_bytes())['prefixes']) == 1
    target.unlink()
    before = store.events.list_for_run(RUN)
    # Break only the disposable fixture schema to exercise psycopg's real
    # UndefinedTable exception at the new CLI boundary, without mock exceptions.
    with connect(isolated_pg_database) as connection:
        connection.execute('DROP TABLE trace_archive_receipts')
    code, out, error = invoke(capsys, 'plan', '--config', str(config), '--output', str(target))
    assert code == 2 and out == ''
    assert json.loads(error)['error']['code'] == 'TRACE_RETENTION_REJECTED'
    assert 'trace_archive_receipts' in json.loads(error)['error']['message']
    assert not target.exists() and store.events.list_for_run(RUN) == before
