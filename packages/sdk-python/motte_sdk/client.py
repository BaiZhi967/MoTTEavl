"""M7 同步 SDK 客户端（协议 docs/protocols/sdk-and-migration.md frozen@1 §1/§2）。

- 依赖只有 ``httpx``（协议 §1.5）；导入本模块零副作用（不建 DB、不读环境凭据）。
- 惰性能力握手（§1.2）：首次请求前 GET /api/v1/capabilities，进程内缓存。
- 重试矩阵（§1.4）：GET 在网络失败/超时/5xx/429/503 上自动重试（指数退避 + 抖动，
  尊重 Retry-After）；POST/PUT/DELETE 绝不自动重试——带 request_key 的创建由
  调用方整键重送原 body，由服务端幂等收口；422/auth/403/404/409/unsupported
  恒不重试。
- ``follow_redirects=False``（协议 §10 重定向泄漏规则）：重定向视为 TransportError
  分类处理，不自动跟随。
- 诊断只写 stderr 且默认关闭；凭据与完整请求 body 不进任何日志或异常文本。
"""
from __future__ import annotations

import json
import random
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .client_errors import (
    ApiError,
    ConnectError,
    MotteClientError,
    NetworkError,
    OperationCancelled,
    RateLimitedError,
    ReadTimeout,
    TransportError,
    UnsupportedCapabilityError,
    WaitTimeout,
    classify,
    is_retryable,
)
from .client_types import (
    Baseline,
    Capabilities,
    Comparison,
    EventsSnapshot,
    Experiment,
    Gate,
    Report,
    RunList,
    RunView,
    ScoringPassList,
    is_terminal_status,
)

_API_PREFIX = "/api/v1"
_EXPECTED_API_VERSION = "v1"
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_READ_METHODS = frozenset({"GET", "HEAD"})

__all__ = ["MotteClient", "StreamState"]


def _map_transport_error(error: BaseException) -> TransportError:
    """httpx 传输异常 → 协议 §1.1 typed TransportError 子类。"""
    if isinstance(error, httpx.ConnectTimeout):
        return ConnectError(f"connect timeout: {error}")
    if isinstance(error, httpx.ConnectError):
        return ConnectError(f"connect failed: {error}")
    if isinstance(error, httpx.ReadTimeout):
        return ReadTimeout(f"read timeout while awaiting response: {error}")
    if isinstance(error, httpx.TimeoutException):
        return ReadTimeout(f"request timed out: {error}")
    if isinstance(error, httpx.TransportError):
        return NetworkError(f"network failure: {error}")
    return NetworkError(str(error))


def _redirect_error(response: httpx.Response) -> NetworkError:
    location = response.headers.get("location", "")
    target = location.split("/")[2] if "://" in location else (location or "<unknown>")
    error = NetworkError(
        "server returned a redirect; the client refuses to follow redirects "
        f"(protocol §10) — target host: {target}"
    )
    error.is_redirect = True  # type: ignore[attr-defined]  # noqa: B010
    return error


def _parse_retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # HTTP-Date 形式不解析，退回指数退避
    return seconds if seconds >= 0 else None


@dataclass(frozen=True)
class _SSEFrame:
    event: str | None
    id: str | None
    data: str


def parse_sse_frames(lines: Iterable[str]) -> Iterator[_SSEFrame]:
    """纯函数 SSE 帧解析（不含网络）：注释行忽略，空行分帧，容忍尾帧无空行。"""
    event: str | None = None
    frame_id: str | None = None
    data_lines: list[str] = []

    def _dispatch() -> _SSEFrame | None:
        nonlocal event, frame_id, data_lines
        if event is None and frame_id is None and not data_lines:
            return None
        frame = _SSEFrame(event, frame_id, "\n".join(data_lines))
        event, frame_id, data_lines = None, None, []
        return frame

    for line in lines:
        if line == "":
            frame = _dispatch()
            if frame is not None:
                yield frame
            continue
        if line.startswith(":"):
            continue  # 心跳/注释（": ping"）
        name, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if name == "event":
            event = value
        elif name == "id":
            frame_id = value
        elif name == "data":
            data_lines.append(value)
    frame = _dispatch()
    if frame is not None:
        yield frame


def _iter_decoded_lines(response: httpx.Response) -> Iterator[str]:
    """按字节读流并按 utf-8 解码成行（不依赖响应头的 charset 声明）。"""
    buffer = b""
    for chunk in response.iter_bytes():
        buffer += chunk
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                break
            line = buffer[:index]
            buffer = buffer[index + 1 :]
            yield line.rstrip(b"\r").decode("utf-8", errors="replace")
    if buffer:
        yield buffer.rstrip(b"\r").decode("utf-8", errors="replace")
    yield ""  # 确保末尾没有空行的帧也能分发


def _event_status(event: Mapping[str, Any]) -> str | None:
    status = event.get("status")
    if isinstance(status, str):
        return status
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        status = payload.get("status")
        if isinstance(status, str):
            return status
    return None


def _event_seq(frame: _SSEFrame, payload: Mapping[str, Any]) -> int | None:
    for candidate in (frame.id, payload.get("seq")):
        try:
            seq = int(str(candidate))
        except (TypeError, ValueError):
            continue
        if seq > 0:
            return seq
    return None


def _payload_seq(event: Mapping[str, Any]) -> int | None:
    value = event.get("seq")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


@dataclass
class StreamState:
    """stream_events 的流状态（同一可变实例贯穿整条流，终态后仍可读取）。"""

    run_id: str
    run_status: str | None = None
    partial: bool = False
    last_seq: int = 0
    events_yielded: int = 0
    reconnects: int = 0
    gaps: list[dict[str, Any]] = field(default_factory=list)
    finished: bool = False
    reconciliation_mismatch: bool = False
    reconciliation_facts: dict[str, Any] | None = None
    report: dict[str, Any] | None = None


class MotteClient:
    """同步 MoTTEavl API 客户端（握手缓存有锁；HTTP 客户端可在多线程间共享）。"""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 30.0,
        overall_deadline: float = 120.0,
        retries: int = 3,
        poll_interval: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        log_debug: bool = False,
        backoff_base: float = 0.05,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.overall_deadline = overall_deadline
        self.retries = max(0, int(retries))
        self.poll_interval = poll_interval
        self.log_debug = log_debug
        self.backoff_base = backoff_base
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        headers = {"accept": "application/json"}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        # follow_redirects=False（协议 §10）：重定向不自动跟随，见 _redirect_error。
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=read_timeout,
                pool=connect_timeout,
            ),
            follow_redirects=False,
            transport=transport,
            headers=headers,
        )
        self._capabilities: Capabilities | None = None
        self._handshake_lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MotteClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _debug(self, message: str) -> None:
        """诊断只走 stderr 且默认关闭；不含凭据与请求 body。"""
        if self.log_debug:
            print(f"[motte-sdk] {message}", file=sys.stderr)

    # ---------------------------------------------------------- capabilities

    def capabilities(self) -> Capabilities:
        """能力握手结果（惰性获取，进程内缓存；协议 §1.2）。"""
        return self._ensure_capabilities()

    def _ensure_capabilities(self) -> Capabilities:
        if self._capabilities is not None:
            return self._capabilities
        with self._handshake_lock:
            if self._capabilities is None:
                payload = self._request_json(
                    "GET", f"{_API_PREFIX}/capabilities", handshake=True
                )
                caps = Capabilities.from_payload(payload)
                if caps.api_version != _EXPECTED_API_VERSION:
                    raise MotteClientError(
                        f"server api_version {caps.api_version!r} is not supported "
                        f"(expected {_EXPECTED_API_VERSION!r}); refusing to guess "
                        "(protocol §1.2)"
                    )
                self._capabilities = caps
        return self._capabilities

    def _require_features(self, *features: str) -> None:
        caps = self._ensure_capabilities()
        missing = [feature for feature in features if not caps.supports(feature)]
        if missing:
            raise UnsupportedCapabilityError(missing)

    # ------------------------------------------------------------- transport

    def _timeout_for(self, remaining: float | None) -> httpx.Timeout:
        """剩余总期限 clamp 进分段超时（协议 §1.4 三段 timeout）。"""
        if remaining is None:
            return httpx.Timeout(
                connect=self._connect_timeout,
                read=self._read_timeout,
                write=self._read_timeout,
                pool=self._connect_timeout,
            )
        return httpx.Timeout(
            connect=min(self._connect_timeout, remaining),
            read=min(self._read_timeout, remaining),
            write=min(self._read_timeout, remaining),
            pool=min(self._connect_timeout, remaining),
        )

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        exponential = self.backoff_base * (2 ** min(attempt, 6))
        return exponential + random.uniform(0, self.backoff_base)

    def _classify_response(self, response: httpx.Response) -> ApiError | None:
        """非 2xx/3xx 响应 → typed ApiError；重定向 → NetworkError(is_redirect)。"""
        if response.status_code in _REDIRECT_STATUSES:
            raise _redirect_error(response)
        if response.status_code < 400:
            return None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        request_id = response.headers.get("x-request-id") or response.headers.get(
            "request-id"
        )
        return classify(
            response.status_code,
            payload if isinstance(payload, Mapping) else None,
            request_id=request_id,
            retry_after=_parse_retry_after(response),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        handshake: bool = False,
    ) -> dict[str, Any]:
        """单次 JSON 请求，含协议 §1.4 重试矩阵。

        GET/HEAD：TransportError/5xx/429/503 自动重试（退避 + 抖动，尊重
        Retry-After）。POST/PUT/DELETE：绝不自动重试（可能重复付费）；带
        request_key 的创建由调用方整键重送原 body，服务端幂等收口。
        """
        if not handshake:
            self._ensure_capabilities()
        deadline_at = time.monotonic() + self.overall_deadline
        attempts = self.retries + 1 if method in _READ_METHODS else 1
        query = {key: value for key, value in (params or {}).items() if value is not None}
        last_error: MotteClientError
        for attempt in range(attempts):
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise WaitTimeout(
                    f"{method} {path} exceeded overall deadline of "
                    f"{self.overall_deadline}s before completing"
                )
            can_retry = attempt + 1 < attempts
            try:
                response = self._http.request(
                    method,
                    path,
                    params=query,
                    json=json_body,
                    timeout=self._timeout_for(remaining),
                )
            except httpx.HTTPError as error:
                last_error = _map_transport_error(error)
                if isinstance(last_error, NetworkError) and getattr(
                    last_error, "is_redirect", False
                ):
                    raise last_error from error
                if not can_retry or not is_retryable(last_error):
                    raise last_error
            else:
                api_error = self._classify_response(response)
                if api_error is None:
                    try:
                        payload = response.json()
                    except ValueError as error:
                        raise MotteClientError(
                            f"non-JSON response from {method} {path} "
                            f"(http {response.status_code})"
                        ) from error
                    return payload if isinstance(payload, dict) else {"value": payload}
                last_error = api_error
                if not can_retry or not is_retryable(last_error):
                    raise last_error
            retry_after = (
                last_error.retry_after
                if isinstance(last_error, RateLimitedError)
                else None
            )
            delay = min(self._retry_delay(attempt, retry_after), max(remaining, 0.0))
            self._debug(
                f"retrying {method} {path} in {delay:.3f}s "
                f"(attempt {attempt + 2}/{attempts})"
            )
            time.sleep(max(0.0, delay))
        raise last_error  # pragma: no cover - 循环必然 return 或 raise

    def _get(self, path: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("GET", path, params=params)

    def _post(self, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("POST", path, json_body=body or {})

    @staticmethod
    def _segment(value: object) -> str:
        return quote(str(value), safe="")

    # ------------------------------------------------------------ health/runs

    def health(self) -> dict[str, Any]:
        """探活（协议 §10：/health 免认证，可先于能力握手）。"""
        return self._request_json("GET", "/health", handshake=True)

    def create_run(
        self,
        scenario_version: str,
        manifest: Mapping[str, Any] | None = None,
        case_ids: Iterable[str] | None = None,
        *,
        request_key: str | None = None,
    ) -> RunView:
        """创建 Run；``request_key`` 启用幂等创建（协议 §1.3，服务端唯一权威）。"""
        if request_key is not None:
            self._require_features("idempotent_run_create")
        body: dict[str, Any] = {
            "scenario_version": scenario_version,
            "manifest": dict(manifest or {}),
            "case_ids": list(case_ids or []),
        }
        if request_key is not None:
            body["request_key"] = request_key
        return RunView.from_payload(self._post(f"{_API_PREFIX}/runs", body))

    def get_run(self, run_id: str) -> RunView:
        return RunView.from_payload(self._get(f"{_API_PREFIX}/runs/{self._segment(run_id)}"))

    def list_runs(self, status: str | None = None) -> RunList:
        return RunList.from_payload(
            self._get(f"{_API_PREFIX}/runs", params={"status": status})
        )

    def cancel_run(self, run_id: str, reason: str | None = None) -> RunView:
        body: dict[str, Any] = {}
        if reason is not None:
            body["reason"] = reason
        return RunView.from_payload(
            self._post(f"{_API_PREFIX}/runs/{self._segment(run_id)}/cancel", body)
        )

    def retry_run(self, run_id: str) -> RunView:
        return RunView.from_payload(
            self._post(f"{_API_PREFIX}/runs/{self._segment(run_id)}/retry")
        )

    def rescore_run(self, run_id: str) -> RunView:
        return RunView.from_payload(
            self._post(f"{_API_PREFIX}/runs/{self._segment(run_id)}/rescore")
        )

    # ------------------------------------------------------------- reporting

    def run_events_snapshot(
        self, run_id: str, *, after: int = 0, limit: int = 500
    ) -> EventsSnapshot:
        """SSE 断线/缺口的持久查询（协议 §2；capability: events_snapshot）。"""
        self._require_features("events_snapshot")
        return EventsSnapshot.from_payload(
            self._get(
                f"{_API_PREFIX}/runs/{self._segment(run_id)}/events/snapshot",
                params={"after": after, "limit": limit},
            )
        )

    def run_report(self, run_id: str, scoring_pass_id: str | None = None) -> Report:
        return Report.from_payload(
            self._get(
                f"{_API_PREFIX}/runs/{self._segment(run_id)}/report",
                params={"scoring_pass_id": scoring_pass_id},
            )
        )

    def list_scoring_passes(self, run_id: str) -> ScoringPassList:
        return ScoringPassList.from_payload(
            self._get(f"{_API_PREFIX}/runs/{self._segment(run_id)}/scoring-passes")
        )

    # ----------------------------------------------------------- experiments

    def experiment_preview(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        return self._post(f"{_API_PREFIX}/experiments/preview", dict(spec))

    def experiment_create(
        self, spec: Mapping[str, Any], *, request_key: str | None = None
    ) -> dict[str, Any]:
        body = dict(spec)
        if request_key is not None:
            body["_request_key"] = request_key
        return self._post(f"{_API_PREFIX}/experiments", body)

    def experiment_status(
        self, experiment_id: str, version: str | None = None
    ) -> Experiment:
        return Experiment.from_payload(
            self._get(
                f"{_API_PREFIX}/experiments/{self._segment(experiment_id)}",
                params={"version": version},
            )
        )

    def experiment_cancel(
        self,
        experiment_id: str,
        version: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if version is not None:
            body["version"] = version
        if reason is not None:
            body["reason"] = reason
        return self._post(
            f"{_API_PREFIX}/experiments/{self._segment(experiment_id)}/cancel", body
        )

    # ------------------------------------------------ comparison / regression

    def compare(
        self, baseline_run_id: str, candidate_run_id: str, **params: Any
    ) -> Comparison:
        """比较资格（GET /comparisons；额外参数透传：factors/baseline_pass/…）。"""
        query: dict[str, Any] = {
            "baseline": baseline_run_id,
            "candidate": candidate_run_id,
            **params,
        }
        return Comparison.from_payload(
            self._get(f"{_API_PREFIX}/comparisons", params=query)
        )

    def classify_regression(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
        *,
        baseline_pass: str | None = None,
        candidate_pass: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"{_API_PREFIX}/regressions",
            params={
                "baseline": baseline_run_id,
                "candidate": candidate_run_id,
                "baseline_pass": baseline_pass,
                "candidate_pass": candidate_pass,
            },
        )

    # -------------------------------------------------------------- baselines

    def create_baseline(
        self,
        baseline_id: str,
        entries: list[Mapping[str, Any]],
        *,
        policy: Mapping[str, Any] | None = None,
        created_by: str | None = None,
        reason: str | None = None,
        source_note: str | None = None,
    ) -> Baseline:
        body: dict[str, Any] = {
            "baseline_id": baseline_id,
            "entries": [dict(entry) for entry in entries],
        }
        if policy is not None:
            body["policy"] = dict(policy)
        if created_by is not None:
            body["created_by"] = created_by
        if reason is not None:
            body["reason"] = reason
        if source_note is not None:
            body["source_note"] = source_note
        return Baseline.from_payload(self._post(f"{_API_PREFIX}/baselines", body))

    def list_baselines(self, limit: int = 100) -> list[Baseline]:
        payload = self._get(f"{_API_PREFIX}/baselines", params={"limit": limit})
        return [
            Baseline.from_payload(item)
            for item in payload.get("items", [])
            if isinstance(item, Mapping)
        ]

    def get_baseline(self, baseline_id: str) -> Baseline:
        return Baseline.from_payload(
            self._get(f"{_API_PREFIX}/baselines/{self._segment(baseline_id)}")
        )

    def set_default_baseline(
        self,
        scope: str,
        baseline_id: str,
        *,
        expected_current: str | None = None,
        updated_by: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"scope": scope, "baseline_id": baseline_id}
        if expected_current is not None:
            body["expected_current"] = expected_current
        if updated_by is not None:
            body["updated_by"] = updated_by
        if reason is not None:
            body["reason"] = reason
        return self._post(f"{_API_PREFIX}/baselines/default", body)

    # ------------------------------------------------------------------ gates

    def publish_gate_policy(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._post(f"{_API_PREFIX}/gate-policies", dict(payload))

    def evaluate_gate_versioned(
        self,
        run_id: str,
        policy_id: str,
        policy_version: str,
        *,
        scoring_pass_id: str | None = None,
        baseline_id: str | None = None,
        allowed_factors: tuple[str, ...] | None = None,
    ) -> Gate:
        body: dict[str, Any] = {
            "run_id": run_id,
            "policy_id": policy_id,
            "policy_version": policy_version,
        }
        if scoring_pass_id is not None:
            body["scoring_pass_id"] = scoring_pass_id
        if baseline_id is not None:
            body["baseline_id"] = baseline_id
        if allowed_factors is not None:
            body["allowed_factors"] = list(allowed_factors)
        return Gate.from_payload(self._post(f"{_API_PREFIX}/gates/versioned", body))

    def get_gate_result(self, gate_result_id: str) -> Gate:
        return Gate.from_payload(
            self._get(f"{_API_PREFIX}/gates/results/{self._segment(gate_result_id)}")
        )

    def export_gate(self, gate_result_id: str, format: str = "json") -> Any:
        """导出 Gate 结果（capability: gate_export）；json → dict，junit → 透传。"""
        self._require_features("gate_export")
        return self._get(
            f"{_API_PREFIX}/gates/results/{self._segment(gate_result_id)}/export",
            params={"format": format},
        )

    # -------------------------------------------------------- wait and stream

    def wait_for_run(
        self,
        run_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """轮询直到终态；``needs_review`` 原样返回（用户处理，不自动 retry）。

        超时只停止等待（WaitTimeout 附最后已知 Run），**绝不暗中 cancel 远端 Run**
        （协议 §1.4）。
        """
        interval = poll_interval if poll_interval is not None else self.poll_interval
        deadline_at = time.monotonic() + timeout if timeout is not None else None
        run = self.get_run(run_id).raw
        while not is_terminal_status(run.get("status")):
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled(f"wait for run {run_id} cancelled by caller")
            now = time.monotonic()
            if deadline_at is not None and now >= deadline_at:
                raise WaitTimeout(
                    f"timed out waiting for run {run_id} "
                    f"(last status: {run.get('status')!r})",
                    run=run,
                )
            if deadline_at is not None:
                interval = min(interval, max(0.05, deadline_at - now))
            time.sleep(max(0.0, interval))
            run = self.get_run(run_id).raw
        return run

    def stream_events(
        self,
        run_id: str,
        *,
        after: int = 0,
        on_event: Callable[[dict[str, Any], StreamState], None] | None = None,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> Iterator[tuple[dict[str, Any], StreamState]]:
        """SSE 事件流（协议 §2）：seq 去重、乱序排序交付、断线重连、gap 补齐。

        - 命名事件 ``motte-gap`` → ``state.partial = True`` 并用 snapshot 端点补齐；
          snapshot 也补不出的段保持 partial（如实上报，不假装完整）。
        - 终态（服务端关闭流）后做终态对账：读 run 与 report，核对最后事件 seq ==
          流内最大 seq；不一致置 ``state.reconciliation_mismatch = True`` 并附事实。
        - 每个产出项是 ``(event, state)``；state 是贯穿整条流的同一可变实例，
          迭代结束后仍可读取终态字段。
        """
        self._require_features("sse_cursor", "events_snapshot")
        state = StreamState(run_id=run_id)
        cursor = max(0, int(after))
        seen: set[int] = set()
        buffered: dict[int, dict[str, Any]] = {}
        deadline_at = time.monotonic() + deadline if deadline is not None else None
        path = f"{_API_PREFIX}/runs/{self._segment(run_id)}/events"

        def _check_cancel() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelled(f"event stream for run {run_id} cancelled")

        def _remaining() -> float | None:
            return None if deadline_at is None else deadline_at - time.monotonic()

        def _deadline_expired() -> bool:
            left = _remaining()
            return left is not None and left <= 0

        def _flush() -> Iterator[tuple[dict[str, Any], StreamState]]:
            """只按 seq 严格连续交付（乱序先缓冲，排序后交付；协议 §2）。"""
            nonlocal cursor
            while cursor + 1 in buffered:
                event = buffered.pop(cursor + 1)
                cursor += 1
                state.last_seq = cursor
                state.events_yielded += 1
                status = _event_status(event)
                if status is not None:
                    state.run_status = status
                if on_event is not None:
                    on_event(event, state)
                yield event, state

        def _ingest(event: Mapping[str, Any], seq: int) -> None:
            if seq in seen:
                return  # 重复帧（重连/服务端重放）按 seq 去重
            seen.add(seq)
            buffered[seq] = dict(event)

        def _handle_gap(gap: Mapping[str, Any]) -> None:
            nonlocal cursor
            state.partial = True
            state.gaps.append(dict(gap))
            next_seq = gap.get("next_seq")
            if isinstance(next_seq, int) and next_seq > cursor + 1:
                # cursor+1 .. next_seq-1 已被清理（服务端权威声明），游标前移。
                cursor = next_seq - 1
            self._debug(
                f"motte-gap on run {run_id}: after={gap.get('after')} "
                f"next_seq={next_seq}; filling from events snapshot"
            )
            snapshot = self.run_events_snapshot(run_id, after=cursor)
            for event in snapshot.events:
                seq = _payload_seq(event)
                if seq is not None:
                    _ingest(event, seq)
            if snapshot.partial:
                state.partial = True

        def _deliver(event: dict[str, Any], seq: int) -> tuple[dict[str, Any], StreamState]:
            nonlocal cursor
            if seq > cursor:
                cursor = seq
            state.last_seq = max(state.last_seq, seq)
            state.events_yielded += 1
            status = _event_status(event)
            if status is not None:
                state.run_status = status
            if on_event is not None:
                on_event(event, state)
            return event, state

        def _reconcile(run_view: RunView) -> None:
            facts: dict[str, Any] = {"run_status": run_view.status}
            mismatch = False
            try:
                report = self.run_report(run_id)
                state.report = report.raw
                facts["report_status"] = report.status
            except MotteClientError as error:
                facts["report_error"] = {
                    "http_status": error.http_status,
                    "code": getattr(error, "code", None),
                }
            probe = self.run_events_snapshot(run_id, after=cursor)
            extra = [event.get("seq") for event in probe.events]
            if extra:
                mismatch = True
                facts["stream_last_seq"] = cursor
                facts["server_events_after_stream"] = extra[:50]
            if not run_view.is_terminal:
                mismatch = True
                facts["run_status_not_terminal"] = run_view.status
            state.reconciliation_mismatch = mismatch
            state.reconciliation_facts = facts
            self._debug(
                f"reconciliation for run {run_id}: mismatch={mismatch}, last_seq={cursor}"
            )

        while True:
            _check_cancel()
            if _deadline_expired():
                last_run = None
                try:
                    last_run = self.get_run(run_id).raw
                except MotteClientError:  # 探活失败不掩盖超时本身
                    pass
                raise WaitTimeout(
                    f"event stream for run {run_id} exceeded deadline (last_seq={cursor})",
                    run=last_run,
                )
            stream_closed_cleanly = False
            failure: MotteClientError | None = None
            try:
                with self._http.stream(
                    "GET",
                    path,
                    params={"after": cursor},
                    headers={"Last-Event-ID": str(cursor), "accept": "text/event-stream"},
                    timeout=self._timeout_for(_remaining()),
                ) as response:
                    api_error = self._classify_response(response)
                    if api_error is not None:
                        raise api_error
                    for frame in parse_sse_frames(_iter_decoded_lines(response)):
                        _check_cancel()
                        if frame.event == "motte-gap":
                            try:
                                gap = json.loads(frame.data)
                            except ValueError:
                                continue
                            if isinstance(gap, dict):
                                _handle_gap(gap)
                            yield from _flush()
                            continue
                        if not frame.data:
                            continue
                        try:
                            payload = json.loads(frame.data)
                        except ValueError:
                            self._debug(f"unparseable SSE frame on run {run_id}: skipped")
                            continue
                        if not isinstance(payload, dict):
                            continue
                        seq = _event_seq(frame, payload)
                        if seq is None:
                            self._debug(
                                f"SSE frame without usable seq on run {run_id}: skipped"
                            )
                            continue
                        _ingest(payload, seq)
                        yield from _flush()
                stream_closed_cleanly = True
            except httpx.HTTPError as error:
                failure = _map_transport_error(error)
            except ApiError as error:
                if not is_retryable(error):
                    raise
                failure = error
            if stream_closed_cleanly:
                run_view = self.get_run(run_id)
                state.run_status = run_view.status
                if run_view.is_terminal:
                    # 收尾：交付缓冲里残留的事件（按 seq 排序），然后终态对账。
                    for seq in sorted(buffered):
                        yield _deliver(buffered.pop(seq), seq)
                    _reconcile(run_view)
                    state.finished = True
                    return
                self._debug(
                    f"stream closed before terminal state on run {run_id}; reconnecting"
                )
            else:
                assert failure is not None
                state.reconnects += 1
                self._debug(
                    f"stream failure on run {run_id} (reconnect #{state.reconnects}): "
                    f"{type(failure).__name__}"
                )
            # 重连退避（尊重 deadline 与 cancel）。
            backoff = min(self.backoff_base * 2, 1.0)
            left = _remaining()
            if left is not None:
                backoff = min(backoff, max(0.0, left))
            slept = 0.0
            while slept < backoff:
                _check_cancel()
                if _deadline_expired():
                    raise WaitTimeout(
                        f"event stream for run {run_id} exceeded deadline while "
                        f"reconnecting (last_seq={cursor})"
                    )
                step = min(0.05, backoff - slept)
                time.sleep(step)
                slept += step
