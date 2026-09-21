"""M7 SDK 客户端契约测试（A02/A03 + 能力握手与版本兼容）。

对应协议 docs/protocols/sdk-and-migration.md frozen@1 的 §1.1-§1.5：
错误分类、重试矩阵（GET 自动重试 / POST 绝不自动重试）、幂等创建的响应丢失
恢复、能力协商缺失、api_version 不匹配、``import motte_sdk`` 零副作用。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from motte_storage.run_store import InMemoryRunStore, SQLiteRunStore

from apps.api.app.main import create_app
from motte_sdk import (
    AuthenticationError,
    ConflictError,
    ConnectError,
    MotteClient,
    MotteClientError,
    NotFoundError,
    ReadTimeout,
    ServerError,
    TransportError,
    UnsupportedCapabilityError,
    ValidationError,
)
from tests.sdk.sync_asgi import SyncASGITransport

BASE_URL = "http://testserver"


def _replay_body(**overrides):
    body = {
        "scenario_version": "replay@1",
        "manifest": {
            "replay_fixture": {"case-1": {"output": "ok", "expected": "ok"}},
        },
        "case_ids": ["case-1"],
    }
    body.update(overrides)
    return body


class CountingTransport(httpx.BaseTransport):
    """包装真实 ASGI transport，按 (method, path) 计数（A02/A03 断言依据）。"""

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


class FlakyTransport(httpx.BaseTransport):
    """故障注入：对匹配 (method, path 前缀) 的前 N 次请求制造网络故障或状态码。"""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        *,
        method: str,
        path_prefix: str,
        times: int = 1,
        mode: str = "drop_response",
    ) -> None:
        self.inner = inner
        self.method = method
        self.path_prefix = path_prefix
        self.times = times
        self.mode = mode

    def _matches(self, request: httpx.Request) -> bool:
        return (
            request.method == self.method
            and request.url.path.startswith(self.path_prefix)
            and self.times > 0
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._matches(request):
            self.times -= 1
            if self.mode == "connect_error":
                raise httpx.ConnectError("connection refused")
            if self.mode == "read_timeout":
                raise httpx.ReadTimeout("read timed out")
            if self.mode == "status_500":
                return httpx.Response(
                    500, json={"error": {"code": "INTERNAL", "message": "boom"}}
                )
            if self.mode == "status_429":
                return httpx.Response(
                    429,
                    headers={"retry-after": "0"},
                    json={"error": {"code": "RATE_LIMITED", "message": "slow down"}},
                )
            # drop_response：服务端已处理（真实落库），但响应在回程丢失。
            self.inner.handle_request(request)
            raise httpx.ReadTimeout("response lost after the server processed it")
        return self.inner.handle_request(request)


def _client(transport, **kwargs):
    return MotteClient(BASE_URL, transport=transport, **kwargs)


def _asgi_app(**app_kwargs):
    store = app_kwargs.pop("store", None) or InMemoryRunStore()
    app = create_app(store=store, **app_kwargs)
    return app, store


# ------------------------------------------------------------- 握手与版本兼容


def test_lazy_capability_handshake_against_real_app():
    app, _store = _asgi_app()
    counting = CountingTransport(SyncASGITransport(app))
    client = _client(counting)
    caps = client.capabilities()
    assert caps.api_version == "v1"
    assert caps.name == "motteavl"
    assert caps.supports("idempotent_run_create")
    assert caps.supports("events_snapshot")
    assert counting.count("GET", "/api/v1/capabilities") == 1
    # 缓存至进程（实例）结束：后续请求不再重复握手。
    client.list_runs()
    with pytest.raises(NotFoundError, match="run not found"):
        client.get_run("run-does-not-exist")
    assert counting.count("GET", "/api/v1/capabilities") == 1


def test_api_version_mismatch_is_hard_error():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/capabilities"
        return httpx.Response(
            200,
            json={
                "name": "motteavl",
                "api_version": "v9",
                "app_version": "0.1.0",
                "features": {"sse_cursor": True},
                "limits": {},
            },
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(MotteClientError) as excinfo:
        client.list_runs()
    assert not isinstance(excinfo.value, UnsupportedCapabilityError)
    assert "api_version" in str(excinfo.value)


def test_missing_feature_raises_unsupported_capability():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "name": "motteavl",
                    "api_version": "v1",
                    "app_version": "0.1.0",
                    "features": {"sse_cursor": True, "events_snapshot": False},
                    "limits": {},
                },
            )
        return httpx.Response(404, json={"error": {"code": "RUN_NOT_FOUND", "message": "x"}})

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        client.run_events_snapshot("run-1")
    assert excinfo.value.missing == ["events_snapshot"]

    with pytest.raises(UnsupportedCapabilityError) as create_info:
        client.create_run("replay@1", request_key="k-1")
    assert create_info.value.missing == ["idempotent_run_create"]


def test_unknown_feature_keys_are_ignored():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "name": "motteavl",
                    "api_version": "v1",
                    "app_version": "9.9.9",
                    "features": {
                        "sse_cursor": True,
                        "events_snapshot": True,
                        "idempotent_run_create": True,
                        "brand_new_feature": True,  # 未知键：前向兼容，忽略
                    },
                    "limits": {"events_snapshot_limit": 500},
                    "future_top_level": {"x": 1},
                },
            )
        return httpx.Response(
            202, json={"id": "run-1", "status": "queued", "scenario_version": "replay@1"}
        )

    client = _client(httpx.MockTransport(handler))
    caps = client.capabilities()
    assert caps.extra["future_top_level"] == {"x": 1}
    assert caps.features["brand_new_feature"] is True  # 保留但不解释
    run = client.create_run("replay@1", request_key="k-1")  # 已知键齐全
    assert run.id == "run-1"


# ------------------------------------------------------------- 基本 CRUD 冒烟


def test_run_crud_roundtrip_against_real_app():
    app, _store = _asgi_app()
    client = _client(SyncASGITransport(app))
    created = client.create_run(
        "replay@1",
        manifest={"replay_fixture": {"case-1": {"output": "ok", "expected": "ok"}}},
        case_ids=["case-1"],
    )
    assert created.status == "queued"
    fetched = client.get_run(created.id)
    assert fetched.id == created.id
    listing = client.list_runs()
    assert listing.total == 1
    assert listing.items[0].id == created.id
    health = client.health()
    assert health == {"status": "ok"}


# ------------------------------------------------------------------ A02 错误分类


def test_a02_validation_error_422_is_classified_and_never_resent():
    app, _store = _asgi_app()
    counting = CountingTransport(SyncASGITransport(app))
    client = _client(counting)
    with pytest.raises(ValidationError) as excinfo:
        client.create_run("nope@1")
    error = excinfo.value
    assert error.http_status == 422
    assert error.code == "SCENARIO_NOT_FOUND"
    # POST 绝不自动重试（协议 §1.4）：422 只发出一次。
    assert counting.count("POST", "/api/v1/runs") == 1


def test_a02_authentication_error_401_classified():
    app, _store = _asgi_app(api_token="secret-token")
    client = _client(SyncASGITransport(app))
    with pytest.raises(AuthenticationError) as excinfo:
        client.create_run("replay@1")
    assert excinfo.value.http_status == 401
    assert excinfo.value.code == "AUTH_REQUIRED"
    # /health 与 /capabilities 免认证（协议 §10），握手先行成功。
    assert client.health() == {"status": "ok"}
    # 带令牌的客户端可用。
    authorized = MotteClient(
        BASE_URL, token="secret-token", transport=SyncASGITransport(app)
    )
    assert authorized.create_run("replay@1").status == "queued"


def test_a02_connect_error_classified():
    app, _store = _asgi_app()
    flaky = FlakyTransport(
        SyncASGITransport(app),
        method="GET",
        path_prefix="/api/v1/runs",
        times=99,
        mode="connect_error",
    )
    client = _client(flaky, retries=0)
    with pytest.raises(ConnectError) as excinfo:
        client.get_run("run-1")
    assert isinstance(excinfo.value, TransportError)
    assert isinstance(excinfo.value, MotteClientError)


def test_a02_read_timeout_classified():
    app, _store = _asgi_app()
    flaky = FlakyTransport(
        SyncASGITransport(app),
        method="GET",
        path_prefix="/api/v1/runs",
        times=99,
        mode="read_timeout",
    )
    client = _client(flaky, retries=0)
    with pytest.raises(ReadTimeout):
        client.get_run("run-1")


def test_get_retries_on_5xx_then_succeeds():
    app, _store = _asgi_app()
    counting = CountingTransport(
        FlakyTransport(
            SyncASGITransport(app),
            method="GET",
            path_prefix="/api/v1/runs",
            times=1,
            mode="status_500",
        )
    )
    client = _client(counting, retries=3)
    created = client.create_run("replay@1")
    fetched = client.get_run(created.id)  # 第一次 500，重试成功
    assert fetched.id == created.id
    assert counting.count("GET", f"/api/v1/runs/{created.id}") == 2


def test_get_respects_retry_after_on_429():
    app, _store = _asgi_app()
    counting = CountingTransport(
        FlakyTransport(
            SyncASGITransport(app),
            method="GET",
            path_prefix="/api/v1/runs",
            times=1,
            mode="status_429",
        )
    )
    client = _client(counting, retries=2)
    created = client.create_run("replay@1")
    assert client.get_run(created.id).id == created.id
    assert counting.count("GET", f"/api/v1/runs/{created.id}") == 2


def test_post_is_never_auto_retried_on_5xx():
    app, _store = _asgi_app()
    counting = CountingTransport(
        FlakyTransport(
            SyncASGITransport(app),
            method="POST",
            path_prefix="/api/v1/runs",
            times=1,
            mode="status_500",
        )
    )
    client = _client(counting, retries=3)
    with pytest.raises(ServerError):
        client.create_run("replay@1")
    # POST 不自动重试：一次 500 即抛出。
    assert counting.count("POST", "/api/v1/runs") == 1


# --------------------------------------------------- A03 响应丢失与幂等重放


def test_a03_response_loss_then_idempotent_replay(tmp_path):
    """服务端已处理但响应丢失：SDK 抛 TransportError（不自动重试 POST），
    调用方整键重送原 body → 同一 run id（idempotent_replay），恰好 2 次服务端 POST、
    store 中只有 1 个 run。"""
    store = SQLiteRunStore(tmp_path / "a03.db")
    app = create_app(store=store)
    counting = CountingTransport(
        FlakyTransport(
            SyncASGITransport(app),
            method="POST",
            path_prefix="/api/v1/runs",
            times=1,
            mode="drop_response",
        )
    )
    client = _client(counting, retries=3)
    body = _replay_body(request_key="payment-safe-1")

    with pytest.raises(TransportError) as excinfo:
        client.create_run(**_body_kwargs(body))
    assert isinstance(excinfo.value, ReadTimeout)
    assert counting.count("POST", "/api/v1/runs") == 1

    # 调用方决定重送（同一 body + 同一 request_key）：服务端幂等收口。
    replayed = client.create_run(**_body_kwargs(body))
    assert counting.count("POST", "/api/v1/runs") == 2
    assert replayed.idempotent_replay is True
    assert client.list_runs().total == 1
    assert len(store.runs.list()) == 1


def test_a03_same_request_key_different_body_conflicts(tmp_path):
    store = SQLiteRunStore(tmp_path / "a03-conflict.db")
    app = create_app(store=store)
    client = _client(SyncASGITransport(app))
    body = _replay_body(request_key="key-2")
    client.create_run(**_body_kwargs(body))
    changed = _replay_body(request_key="key-2")
    changed["case_ids"] = ["case-1", "case-2"]
    changed["manifest"]["replay_fixture"]["case-2"] = {"output": "ok", "expected": "ok"}
    with pytest.raises(ConflictError) as excinfo:
        client.create_run(**_body_kwargs(changed))
    assert excinfo.value.http_status == 409
    assert excinfo.value.code == "REQUEST_KEY_CONFLICT"
    assert client.list_runs().total == 1


def _body_kwargs(body):
    return {
        "scenario_version": body["scenario_version"],
        "manifest": body["manifest"],
        "case_ids": body["case_ids"],
        "request_key": body.get("request_key"),
    }


# ----------------------------------------------------------------- 秘密不泄漏


def test_error_strings_never_contain_request_body_or_secrets():
    app, _store = _asgi_app()
    client = _client(SyncASGITransport(app))
    secret = "sk-test1234567890abcdef"
    body = {
        "scenario_version": "replay@1",
        "manifest": {"provider": {"kind": "replay", "api_key": secret}},
        "case_ids": ["case-1"],
    }
    with pytest.raises(ValidationError) as excinfo:
        client.create_run(**_body_kwargs(body))
    assert excinfo.value.code == "CREDENTIALS_REJECTED"
    assert secret not in str(excinfo.value)
    assert secret not in repr(excinfo.value)

    # 422 detail 列表形状同样不回显输入值（classify 只取 loc/msg/type）。
    from motte_sdk.client_errors import classify

    leaky_detail = {
        "detail": [
            {
                "loc": ["body", "manifest"],
                "msg": "bad value",
                "type": "value_error",
                "input": {"provider": {"api_key": secret}},
            }
        ]
    }
    classified = classify(422, leaky_detail)
    assert secret not in str(classified)
    assert classified.details[0]["field"] == "body.manifest"


# --------------------------------------------------------- 导入零副作用（§1.5）


def test_import_motte_sdk_is_zero_side_effect(tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    script = (
        "import json, os, sys\n"
        "before = dict(os.environ)\n"
        "import motte_sdk\n"
        "after = dict(os.environ)\n"
        "assert before == after, 'import motte_sdk must not mutate the environment'\n"
        "assert motte_sdk.__version__ == '0.1.0'\n"
        "assert 'motte_sdk.executor' not in sys.modules, 'execution stack leaked'\n"
        "assert 'motte_storage' not in sys.modules, 'storage leaked into import'\n"
        "assert not os.path.exists('var'), os.listdir('.')\n"
        "client = motte_sdk.MotteClient('http://testserver')\n"
        "assert client is not None\n"
        "executor = motte_sdk.RunExecutor  # 懒导入仍可用\n"
        "assert executor is not None\n"
        "print(json.dumps({'ok': True}))\n"
    )
    env = dict(os.environ)
    sdk_path = str(repo_root / "packages" / "sdk-python")
    env["PYTHONPATH"] = sdk_path + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}
    assert not (tmp_path / "var").exists()
