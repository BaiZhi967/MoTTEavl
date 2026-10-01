"""Truthful history on synthetic disposable Memory/SQLite/PostgreSQL stores."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_storage import trace_retention as retention
from motte_storage import trace_retention_models as models
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore

NOW = datetime(2026, 9, 30, tzinfo=UTC)
CONFIG = models.TraceRetentionConfig(enabled=True, retention_days=7)
RUN = 'history-run'


def seed(store, monkeypatch):
    monkeypatch.setattr(models, 'utc_now', lambda: NOW - timedelta(days=20))
    store.runs.create({'id': RUN, 'status': 'completed', 'scenario_version': 'replay@1',
                       'manifest': {}, 'case_ids': []})
    for _ in range(4):
        store.events.append({'run_id': RUN, 'type': 'note', 'payload': {}})
    monkeypatch.setattr(models, 'utc_now', lambda: NOW)


def trim(store, root):
    plan = retention.plan_trace_retention(store, config=CONFIG)
    return retention.apply_trace_retention(store, root, plan, config=CONFIG, confirm=True)


@pytest.fixture(params=['memory', 'sqlite', 'postgres'])
def history(request, tmp_path, monkeypatch):
    backend = request.param
    if backend == 'postgres':
        from motte_storage.postgres import create_postgres_run_store
        store = create_postgres_run_store(request.getfixturevalue('isolated_pg_database'), migrate=True)
    else:
        store = SQLiteRunStore(tmp_path / 'history.db')
    seed(store, monkeypatch)
    root = tmp_path / 'artifacts'
    root.mkdir()
    result = trim(store, root)
    if backend == 'memory':
        # Memory cannot durably apply. Import only this test's synthetic receipt
        # and retained state to exercise its ordinary reader/append contract.
        store = InMemoryRunStore()
        seed(store, monkeypatch)
        with store.events._lock:
            store.events._events[RUN] = store.events._events[RUN][3:]
            receipt = result.receipts[0]
            store.trace_archives._rows[receipt.archive_id] = receipt
    return store, root, TestClient(create_app(store=store))


def frames(response):
    assert response.status_code == 200
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


def test_historical_cursor_is_partial(history):
    store, _, client = history
    path = f'/api/v1/runs/{RUN}/events'
    assert client.get(path + '/snapshot?after=0').json()['partial'] is True
    assert client.get(path + '/snapshot?after=3').json()['partial'] is False
    assert frames(client.get(path))[0] == {
        'type': 'gap', 'after': 0, 'next_seq': 4, 'partial': True,
    }
    assert frames(client.get(path, headers={'Last-Event-ID': '1'}))[0] == {
        'type': 'gap', 'after': 1, 'next_seq': 4, 'partial': True,
    }
    assert frames(client.get(path, headers={'Last-Event-ID': '3'}))[0]['seq'] == 4
    window = store.events.read_window(RUN, 99)
    assert window.events == [] and window.trimmed_through == 3
    window.events.append({'seq': 999})
    assert store.events.read_window(RUN, 0).events == store.events.list_after(RUN, 0)


def test_empty_and_paginated_history_remain_honest(history, monkeypatch):
    store, _, client = history
    for seq in range(5, 8):
        assert store.events.append({'run_id': RUN, 'type': 'note'})['seq'] == seq
    path = f'/api/v1/runs/{RUN}/events/snapshot'
    first = client.get(path, params={'after': 0, 'limit': 1}).json()
    assert first['partial'] and first['has_more'] and first['next_after'] == 4
    following = client.get(path, params={'after': first['next_after'], 'limit': 1}).json()
    assert not following['partial'] and following['events'][0]['seq'] == 5
    empty = client.get(path, params={'after': 7}).json()
    assert empty['events'] == [] and not empty['partial'] and not empty['has_more']
    assert 'stored_at' not in first['events'][0]
    monkeypatch.setattr('apps.api.app.main.SSE_EVENTS_PER_POLL', 1)
    streamed = frames(client.get(f'/api/v1/runs/{RUN}/events'))
    assert [row['seq'] for row in streamed if row['type'] != 'gap'] == [4, 5, 6, 7]
    assert [row for row in streamed if row['type'] == 'gap'] == [
        {'type': 'gap', 'after': 0, 'next_seq': 4, 'partial': True},
    ]


def test_repeated_retention_and_active_append(history):
    store, root, client = history
    if not hasattr(store.events, '_events'):
        assert store.events.append({'run_id': RUN, 'type': 'note'})['seq'] == 5
        assert trim(store, root).trimmed_events == 1
        assert store.events.read_window(RUN, 0).trimmed_through == 4
    current = store.runs.get(RUN)
    store.runs.update({**current, 'status': 'running'}, expected_revision=current['revision'],
                      expected_status='completed')
    next_seq = store.events.list_for_run(RUN)[-1]['seq'] + 1
    assert store.events.append({'run_id': RUN, 'type': 'note'})['seq'] == next_seq
    assert client.get(f'/api/v1/runs/{RUN}/events/snapshot').json()['partial'] is True


def test_receipt_without_returned_rows_is_still_partial(history):
    store, _, client = history
    # Model an independently missing retained tail in a disposable legacy fixture.
    if hasattr(store.events, '_events'):
        with store.events._lock:
            store.events._events[RUN] = []
    elif hasattr(store, 'dsn'):
        from psycopg import connect
        with connect(store.dsn) as connection:
            connection.execute('DELETE FROM trace_events WHERE run_id=%s', (RUN,))
    else:
        import sqlite3
        with sqlite3.connect(store.events._path) as connection:
            connection.execute('DELETE FROM trace_events WHERE run_id=?', (RUN,))
    path = f'/api/v1/runs/{RUN}/events'
    snapshot = client.get(path + '/snapshot').json()
    assert snapshot['events'] == [] and snapshot['partial'] is True
    assert frames(client.get(path)) == [
        {'type': 'gap', 'after': 0, 'next_seq': 4, 'partial': True},
    ]


def test_legacy_unexplained_first_gap_remains_partial(tmp_path, monkeypatch):
    store = InMemoryRunStore()
    seed(store, monkeypatch)
    store.events._events[RUN] = store.events._events[RUN][2:]
    client = TestClient(create_app(store=store))
    path = f'/api/v1/runs/{RUN}/events'
    assert client.get(path + '/snapshot').json()['partial'] is True
    assert frames(client.get(path))[0]['next_seq'] == 3


@pytest.mark.parametrize('backend', ['sqlite', 'postgres'])
def test_concurrent_trim_and_read_window_share_one_snapshot(backend, tmp_path, monkeypatch, request):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread
    from tests.storage.test_trace_retention_transactions import ObservedConnection

    if backend == 'postgres':
        from motte_storage import postgres
        store = postgres.create_postgres_run_store(
            request.getfixturevalue('isolated_pg_database'), migrate=True,
        )
        module = postgres
    else:
        from motte_storage import run_store
        store = SQLiteRunStore(tmp_path / 'concurrent.db')
        module = run_store
    seed(store, monkeypatch)
    root = tmp_path / 'artifacts'
    root.mkdir()
    reading, release = Event(), Event()
    read_thread = None
    original = module._connect
    def observe(when, statement, connection):
        if (current_thread().ident == read_thread and when == 'after'
                and 'SELECT' in statement and 'trace_events' in statement):
            reading.set()
            assert release.wait(10), 'reader snapshot was not released'
    monkeypatch.setattr(module, '_connect', lambda *a, **k: ObservedConnection(original(*a, **k), observe))
    def read():
        nonlocal read_thread
        read_thread = current_thread().ident
        return store.events.read_window(RUN, 0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(read)
        try:
            assert reading.wait(5)
            writer = pool.submit(trim, store, root)
            # SQLite WAL permits the trim to commit while the SELECT is paused.
            # PG retains the reader transaction's table lock until release.
            if backend == 'sqlite':
                assert writer.result(timeout=8).trimmed_events == 3
        finally:
            release.set()
        before = reader.result(timeout=5)
        assert writer.result(timeout=10).trimmed_events == 3
    assert (before.trimmed_through, [row['seq'] for row in before.events]) == (0, [1, 2, 3, 4])
    after = store.events.read_window(RUN, 0)
    assert (after.trimmed_through, [row['seq'] for row in after.events]) == (3, [4])


@pytest.mark.parametrize('mode', ['local', 'server'])
@pytest.mark.parametrize('snapshot', [False, True])
def test_cli_preserves_authoritative_retention_warning(history, monkeypatch, capsys, mode, snapshot):
    from motte_cli.main import main
    from motte_sdk import MotteClient
    from motte_sdk.service import RunService

    store, _, client = history
    monkeypatch.delenv('ALL_PROXY', raising=False)
    monkeypatch.delenv('all_proxy', raising=False)
    monkeypatch.setattr('motte_cli.runops._service', lambda args: RunService(store))
    monkeypatch.setattr('motte_cli.remote.build_client', lambda args: MotteClient(
        'http://testserver', transport=client._transport,
    ))
    options = ['--api-url', 'http://testserver'] if mode == 'server' else []
    assert main(['run-events', RUN, '--mode', mode, *options, *(['--snapshot'] if snapshot else [])]) == 0
    captured = capsys.readouterr()
    warning = json.loads(captured.err)
    assert warning['code'] == 'TRACE_HISTORY_PARTIAL' and warning['partial'] is True
    assert warning['trimmed_through'] == 3
    rows = json.loads(captured.out) if snapshot else [json.loads(line) for line in captured.out.splitlines()]
    assert len(rows) == 1 and rows[0]['seq'] == 4
