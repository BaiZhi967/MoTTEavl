"""M7 单用户安全边界测试（A17）：Origin/Host/CSRF/CORS/auth/脱敏。

基础 Host/Origin/token 行为在 tests/api/test_m7_platform.py 已有直接断言；
本文件覆盖完整方法矩阵、CORS 白名单、凭据脱敏与错误路径不泄漏请求体。

凭据形状的测试输入一律运行期拼接合成（不是可用凭据，也不在源码中出现完整
密钥形状字面量）。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from motte_storage.run_store import InMemoryRunStore

from apps.api.app.main import create_app

CREATE_BODY = {
    "scenario_version": "json_extract@1",
}


def _fake_key() -> str:
    # 合成的秘密形状值（仅用于断言脱敏行为；源码中不出现完整字面量）。
    return "sk" + "-" + "a1b2c3d4e5f6g7h8"


def _client(**kwargs) -> TestClient:
    return TestClient(create_app(InMemoryRunStore(), **kwargs))


def test_untrusted_origin_rejected_across_all_write_methods():
    for method, path in (
        ("POST", "/api/v1/runs"),
        ("PUT", "/api/v1/providers/p"),
        ("DELETE", "/api/v1/providers/p"),
    ):
        client = _client()
        response = client.request(
            method, path, json=CREATE_BODY if method == "POST" else None,
            headers={"Origin": "https://untrusted.example"},
        )
        assert response.status_code == 403, (method, path, response.status_code)
        assert response.json()["error"]["code"] == "ORIGIN_REJECTED"
        # 拒绝发生在业务之前：没有创建任何 Run
        assert client.get("/api/v1/runs").json()["total"] == 0


def test_get_with_untrusted_origin_is_allowed_but_writes_still_blocked():
    """读请求不受 Origin 限制（不可信页面可以看，但不能触发执行）。"""
    client = _client()
    read = client.get("/api/v1/runs", headers={"Origin": "https://untrusted.example"})
    assert read.status_code == 200
    write = client.post(
        "/api/v1/runs", json=CREATE_BODY, headers={"Origin": "https://untrusted.example"}
    )
    assert write.status_code == 403


def test_untrusted_origin_cannot_touch_credentials():
    client = _client()
    put = client.put(
        "/api/v1/credentials/p", json={"ref": "env:NAME"},
        headers={"Origin": "https://untrusted.example"},
    )
    assert put.status_code == 403
    assert put.json()["error"]["code"] == "ORIGIN_REJECTED"


def test_allowed_origins_config_enables_cross_site_writes():
    client = _client(allowed_origins=["https://console.internal"])
    ok = client.post(
        "/api/v1/runs", json=CREATE_BODY,
        headers={"Origin": "https://console.internal"},
    )
    assert ok.status_code == 202
    # 其他 Origin 仍被拒
    blocked = client.post(
        "/api/v1/runs", json=CREATE_BODY,
        headers={"Origin": "https://other.example"},
    )
    assert blocked.status_code == 403


def test_cors_preflight_only_served_for_allowed_origins():
    client = _client(allowed_origins=["https://console.internal"])
    allowed = client.options(
        "/api/v1/runs",
        headers={
            "Origin": "https://console.internal",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers.get("access-control-allow-origin") == "https://console.internal"
    disallowed = client.options(
        "/api/v1/runs",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert disallowed.status_code == 400  # Host 校验在前：无 Host 白名单匹配


def test_token_required_on_all_api_routes_but_health_and_capabilities():
    client = _client(api_token="boundary-test-token")
    for path in ("/api/v1/runs", "/api/v1/baselines", "/api/v1/gate-policies"):
        response = client.get(path)
        assert response.status_code == 401, path
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/capabilities").status_code == 200


def test_error_responses_never_echo_request_body():
    """422/409 错误体不回显请求内容（prompt/凭据不进错误响应）。"""
    client = _client()
    key = _fake_key()
    secret_body = {
        "scenario_version": "no-such@9",
        "manifest": {"provider": {"api_key": key}},
    }
    response = client.post("/api/v1/runs", json=secret_body)
    assert response.status_code in (403, 422)
    assert key not in response.text


def test_sse_stream_redacts_secret_shaped_values():
    """SSE 公共流出口脱敏：持久 trace 原文含秘密形状时，流出前被替换。"""
    from motte_contracts.compat import adapt_legacy_trace_event
    from motte_trace.redaction import redact_secrets

    key = _fake_key()
    bearer = "Bearer " + "jwtheader.payloadsig"
    event = {
        "run_id": "r", "seq": 1, "type": "model_response",
        "payload": {"api_key": key, "note": bearer},
    }
    redacted = adapt_legacy_trace_event(redact_secrets(event))
    serialized = str(redacted)
    assert key not in serialized
    assert "jwtheader.payloadsig" not in serialized
