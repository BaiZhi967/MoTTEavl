"""M7 SDK wait/SSE 事件流测试（A05；协议 docs/protocols/sdk-and-migration.md §1.4/§2）。

覆盖：seq 去重与乱序交付、断线重连不丢不重、gap 帧 partial 标记 + snapshot 补齐、
终态对账（含强制 mismatch）、needs_review 终态原样返回、wait 超时不暗中 cancel、
cancel_event、SSE 出口脱敏。
"""
from __future__ import annotations

import json
import sqlite3
import threading

import httpx
import pytest
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore

from apps.api.app.main import create_app
from motte_sdk import (
    MotteClient,
    OperationCancelled,
    WaitTimeout,
    is_terminal_status,
)
from tests.sdk.sync_asgi import SyncASGITransport

BASE_URL = "http://testserver"
RUN_ID = "run-a05"


class CountingTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.calls: list[tuple[str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        return self.inner.handle_request(request)

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p in self.calls if m == method and p == path)

    def count_where(self, predicate) -> int:
        return sum(1 for call in self.calls if predicate(*call))


class _CutStream(httpx.SyncByteStream):
    """先产出正常分片，随后模拟连接中断（抛 httpx.ReadError）。"""

    def __init__(self, prefix: bytes) -> None:
        self._prefix = prefix

    def __iter__(self):
        yield self._prefix
        raise httpx.ReadError("connection reset mid-stream")


class CutStreamTransport(httpx.BaseTransport):
    """第一次 events 请求在完整 SSE 内容的中途断流，后续放行（真实重连语义）。"""

    def __init__(self, inner: httpx.BaseTransport, cut_after_frames: int) -> None:
        self.inner = inner
        self.cut_after_frames = cut_after_frames
        self.events_calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/events"):
            self.events_calls += 1
            response = self.inner.handle_request(request)
            body = response.read()
            if self.events_calls == 1:
                frames = body.split(b"\n\n")
                prefix = b"\n\n".join(frames[: self.cut_after_frames]) + b"\n\n"
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_CutStream(prefix),
                )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        return self.inner.handle_request(request)


class CannedSSETransport(httpx.BaseTransport):
    """events 端点返回固定 SSE 内容（其余请求走真实 app）。"""

    def __init__(self, inner: httpx.BaseTransport, body: bytes) -> None:
        self.inner = inner
        self.body = body
        self.events_calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/events"):
            self.events_calls += 1
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=self.body
            )
        return self.inner.handle_request(request)


def _sse(seq: int, *, status: str | None = None, event: str | None = None,
         payload: dict | None = None) -> str:
    envelope = {
        "protocol": "motte.trace",
        "schema_version": 2,
        "run_id": RUN_ID,
        "seq": seq,
        "type": "note",
        "payload": {"status": status, **(payload or {})} if status else (payload or {}),
    }
    prefix = f"event: {event}\n" if event else ""
    return prefix + f"id: {seq}\ndata: {json.dumps(envelope)}\n\n"


def _seed_run(store, *, status: str = "queued") -> None:
    store.runs.create(
        {
            "id": RUN_ID,
            "status": status,
            "revision": 1,
            "scenario_version": "replay@1",
            "manifest": {},
            "case_ids": [],
        },
        event={"run_id": RUN_ID, "type": "queued", "status": "queued"},
    )


def _append_events(store, total: int) -> None:
    """把 run 的事件补到 ``total`` 条（seed 的 create 事件已占 seq=1）。"""
    for index in range(2, total + 1):
        store.events.append(
            {"run_id": RUN_ID, "type": "progress", "payload": {"step": index}}
        )


def _set_status(store, status: str) -> None:
    current = store.runs.get(RUN_ID)
    store.runs.save({**current, "status": status})


def _client(transport, **kwargs):
    kwargs.setdefault("poll_interval", 0.05)
    return MotteClient(BASE_URL, transport=transport, **kwargs)


def _collect(client, **kwargs):
    events = []
    state_box = []
    for event, state in client.stream_events(RUN_ID, **kwargs):
        events.append(event)
        state_box.append(state)
    return events, (state_box[-1] if state_box else None)


# ------------------------------------------------------------- 去重与乱序


def test_duplicate_and_out_of_order_frames_are_deduped_and_sorted():
    store = InMemoryRunStore()
    app = create_app(store=store)
    _seed_run(store)
    _append_events(store, 4)
    _set_status(store, "completed")
    # 传输层乱序 + 重复：2,1,4,2,3,1
    canned = _sse(2) + _sse(1) + _sse(4) + _sse(2) + _sse(3) + _sse(1)
    client = _client(CannedSSETransport(SyncASGITransport(app), canned.encode()))
    events, state = _collect(client, deadline=10)
    assert [event["seq"] for event in events] == [1, 2, 3, 4]  # 排序交付、恰好一次
    assert state is not None
    assert state.finished is True
    assert state.partial is False
    assert state.reconnects == 0
    assert state.reconciliation_mismatch is False


# ------------------------------------------------------------ 断线重连


def test_mid_stream_cut_reconnects_without_loss_or_duplication():
    store = InMemoryRunStore()
    app = create_app(store=store)
    _seed_run(store)
    _append_events(store, 4)
    _set_status(store, "completed")
    transport = CutStreamTransport(SyncASGITransport(app), cut_after_frames=2)
    client = _client(transport)
    events, state = _collect(client, deadline=10)
    assert [event["seq"] for event in events] == [1, 2, 3, 4]  # 不丢、不重
    assert state.reconnects >= 1
    assert state.finished is True
    assert state.reconciliation_mismatch is False
    assert transport.events_calls == 2  # 恰好一次重连


# ---------------------------------------------------------------- gap 补齐


def test_gap_frame_marks_partial_and_snapshot_fills(tmp_path):
    db_path = tmp_path / "a05-gap.db"
    store = SQLiteRunStore(db_path)
    app = create_app(store=store)
    _seed_run(store)
    _append_events(store, 4)
    _set_status(store, "completed")
    # 模拟事件清理：删除 seq=1，重连 after=0 时服务端先发 motte-gap。
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM trace_events WHERE run_id = ? AND seq = 1", (RUN_ID,))
        connection.commit()

    counting = CountingTransport(SyncASGITransport(app))
    client = _client(counting)
    events, state = _collect(client, deadline=10)
    # snapshot 能补到的（现存 seq 2,3,4）补齐；被清理的 seq 1 如实保持 partial。
    assert [event["seq"] for event in events] == [2, 3, 4]
    assert state.partial is True
    assert state.gaps == [{"type": "gap", "after": 0, "next_seq": 2, "partial": True}]
    assert counting.count("GET", f"/api/v1/runs/{RUN_ID}/events/snapshot") >= 1
    assert state.finished is True


# ------------------------------------------------------------ 终态对账


def test_terminal_reconciliation_reports_mismatch_honestly():
    store = InMemoryRunStore()
    app = create_app(store=store)
    _seed_run(store)
    _append_events(store, 4)
    _set_status(store, "completed")
    # 服务端有 4 条事件，但流只送到 seq=2（强制制造不一致）。
    canned = _sse(1) + _sse(2)
    client = _client(CannedSSETransport(SyncASGITransport(app), canned.encode()))
    events, state = _collect(client, deadline=10)
    assert [event["seq"] for event in events] == [1, 2]
    assert state.finished is True
    assert state.reconciliation_mismatch is True
    assert state.reconciliation_facts["stream_last_seq"] == 2
    assert state.reconciliation_facts["server_events_after_stream"] == [3, 4]
    # 对账读取了 run 与 report（默认当前 pass）。
    assert state.run_status == "completed"
    assert state.report is not None
    assert state.report["run_id"] == RUN_ID


def test_clean_terminal_stream_reconciles_consistently():
    store = InMemoryRunStore()
    app = create_app(store=store)
    _seed_run(store)
    _append_events(store, 4)
    _set_status(store, "completed")
    client = _client(SyncASGITransport(app))
    events, state = _collect(client, deadline=10)
    assert [event["seq"] for event in events] == [1, 2, 3, 4]
    assert state.reconciliation_mismatch is False
    assert state.last_seq == 4
    assert state.report is not None


# --------------------------------------------------------- needs_review / wait


def test_needs_review_is_terminal_and_returned_as_is():
    store = InMemoryRunStore()
    app = create_app(store=store)
    _seed_run(store)
    _set_status(store, "needs_review")
    counting = CountingTransport(SyncASGITransport(app))
    client = _client(counting)
    run = client.wait_for_run(RUN_ID)
    assert run["status"] == "needs_review"  # 终态：原样返回，不自动 retry
    assert is_terminal_status(run["status"])
    # 流也在 needs_review 终态后干净关闭。
    events, state = _collect(client, deadline=10)
    assert state.finished is True
    assert state.run_status == "needs_review"
    # 无任何写调用（不自动 retry / cancel）。
    assert counting.count_where(lambda m, _p: m == "POST") == 0


def test_wait_timeout_raises_waittimeout_and_never_cancels():
    store = InMemoryRunStore()
    app = create_app(store=store)
    _seed_run(store)  # 保持 queued
    counting = CountingTransport(SyncASGITransport(app))
    client = _client(counting)
    with pytest.raises(WaitTimeout) as excinfo:
        client.wait_for_run(RUN_ID, timeout=0.3, poll_interval=0.05)
    assert excinfo.value.run is not None
    assert excinfo.value.run["status"] == "queued"
    # 超时只停止等待：run 仍 queued，且没有发出任何 cancel 调用。
    assert store.runs.get(RUN_ID)["status"] == "queued"
    assert counting.count_where(lambda m, p: m == "POST" and p.endswith("/cancel")) == 0


def test_wait_cancel_event_raises_operation_cancelled():
    store = InMemoryRunStore()
    app = create_app(store=store)
    _seed_run(store)
    client = _client(SyncASGITransport(app))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelled):
        client.wait_for_run(RUN_ID, timeout=5, poll_interval=0.05, cancel_event=cancel)


def test_stream_cancel_event_raises_operation_cancelled():
    store = InMemoryRunStore()
    app = create_app(store=store)
    _seed_run(store)
    client = _client(SyncASGITransport(app))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelled):
        for _event, _state in client.stream_events(RUN_ID, deadline=10, cancel_event=cancel):
            pass


def test_wait_returns_completed_run():
    store = InMemoryRunStore()
    app = create_app(store=store)
    _seed_run(store)
    _set_status(store, "completed")
    client = _client(SyncASGITransport(app))
    run = client.wait_for_run(RUN_ID, timeout=5)
    assert run["status"] == "completed"


# ----------------------------------------------------------------- 脱敏


def test_stream_events_are_redacted_server_side():
    store = InMemoryRunStore()
    app = create_app(store=store)
    _seed_run(store)
    secret = "sk-test1234567890abcdef"
    store.events.append(
        {"run_id": RUN_ID, "type": "model_response", "payload": {"note": f"key {secret}"}}
    )
    _set_status(store, "completed")
    client = _client(SyncASGITransport(app))
    events, state = _collect(client, deadline=10)
    assert state.finished is True
    serialized = json.dumps(events)
    assert secret not in serialized
    assert "REDACTED" in serialized
