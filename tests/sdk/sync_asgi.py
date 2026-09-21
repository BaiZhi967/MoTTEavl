"""测试专用：把 ASGI 应用桥接成同步 ``httpx.BaseTransport``（支持流式响应）。

httpx 的 ``ASGITransport`` 只有 async 实现；SDK 客户端是同步的。这里在一个
后台事件循环线程上运行 ASGI 应用，响应体经 ``queue.Queue`` 惰性推送，因此
``text/event-stream`` 这类长流不会被整段缓冲，断流/截断注入也可以在
``httpx.SyncByteStream`` 层实现。
"""
from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Iterator
from typing import Any

import httpx


class _QueueStream(httpx.SyncByteStream):
    def __init__(self, chunks: queue.Queue) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        while True:
            message = self._chunks.get()
            if message is None:
                return
            if message["type"] == "http.response.body":
                body = message.get("body") or b""
                if body:
                    yield body
                if not message.get("more_body", False):
                    return


class SyncASGITransport(httpx.BaseTransport):
    """在单个后台事件循环线程上串行运行 ASGI 应用（测试规模足够）。"""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="sync-asgi-loop", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)

    def _asgi_scope(self, request: httpx.Request) -> dict[str, Any]:
        host = request.headers.get("host", "testserver")
        host_name, _, port = host.rpartition(":")
        if not host_name or not port.isdigit():
            host_name, port = host, "80"
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme or "http",
            "path": request.url.path,
            "raw_path": request.url.raw_path,
            "query_string": bytes(request.url.query or b""),
            "root_path": "",
            "server": (host_name, int(port)),
            "client": ("testclient", 50000),
            # ASGI 规范要求 header 名小写（httpx 原始头是 Title-Case）。
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
        }

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = b"" if request.method in {"GET", "HEAD"} else request.read()
        chunks: queue.Queue = queue.Queue()
        started = threading.Event()
        state: dict[str, Any] = {}

        async def receive() -> dict[str, Any]:
            # 请求体只投递一次；此后 receive 必须阻塞（真实服务器的语义），
            # 否则 Starlette StreamingResponse 的 listen_for_disconnect 会忙转
            # 饿死事件循环。响应结束后 task group 会取消这里的等待。
            if not state.get("body_sent"):
                state["body_sent"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = message.get("headers", [])
                started.set()
            elif message["type"] == "http.response.body":
                chunks.put(message)

        async def run() -> None:
            try:
                await self._app(self._asgi_scope(request), receive, send)
            except BaseException as error:  # noqa: BLE001 - 转换为调用方异常
                state["error"] = error
                started.set()
            finally:
                chunks.put(None)

        asyncio.run_coroutine_threadsafe(run(), self._loop)
        if not started.wait(timeout=30):
            raise TimeoutError(f"ASGI app did not start responding: {request.url.path}")
        if "error" in state:
            raise state["error"]
        headers = [
            (key.decode("latin-1"), value.decode("latin-1"))
            for key, value in state.get("headers", [])
        ]
        return httpx.Response(
            state["status"], headers=headers, stream=_QueueStream(chunks), request=request
        )
