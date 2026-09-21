"""单执行 trace（M7-T04，协议 §4）：包裹用户 callable，只执行一次。

- ``@motte_trace(name, *, attributes=..., flush=...)`` 装饰器与
  ``trace_span(name)`` 上下文管理器共用同一实现：被包裹的 callable
  **只执行一次**——任何原因（包括 flush 失败）都不重跑（协议 §4）。
- 嵌套 span：contextvar 栈决定 parent/child；外层上下文退出时一次性 flush。
- 脱敏：所有属性值（含异常消息、事件属性）先过
  ``motte_trace.redaction.redact_secrets`` 再落进 span。
- 截断：单条 span record 序列化 JSON 上限 64KiB；超限时截断字符串值，
  值替换为 ``{"truncated": true, "value": <截断后>}``（键名保留）。
- flush：可选 callable，在外层退出时收到整棵树的 record 列表；flush 抛错
  只记 ``flush_error``（字符串，同时进 root record），**绝不传播、绝不重跑**。
- import 本模块零副作用：不建线程、不读环境、不发网络、不注册全局状态。
"""
from __future__ import annotations

import contextlib
import functools
import json
import time
import uuid
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Mapping

from motte_trace.redaction import redact_secrets

#: 单条 span record 序列化 JSON 的大小上限（协议 §4：64KiB）。
SPAN_RECORD_LIMIT_BYTES = 64 * 1024

#: 截断起点预算：从 8KiB 开始对半收缩，直到整条 record 放进上限。
_TRUNCATION_START_BUDGET = 8192

__all__ = [
    "SPAN_RECORD_LIMIT_BYTES",
    "Span",
    "fit_span_record",
    "last_trace_span",
    "motte_trace",
    "trace_span",
]


def _new_span_id() -> str:
    return uuid.uuid4().hex


def _record_size(record: Mapping[str, Any]) -> int:
    return len(json.dumps(record, default=str, ensure_ascii=False).encode("utf-8"))


def _truncate_strings(value: Any, budget: int) -> Any:
    """把超预算的字符串值换成截断标记；dict/list 递归，其余原样保留。"""
    if isinstance(value, dict):
        return {
            key: (
                {"truncated": True, "value": item[:budget]}
                if isinstance(item, str) and len(item) > budget
                else _truncate_strings(item, budget)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_truncate_strings(item, budget) for item in value]
    return value


def fit_span_record(record: dict[str, Any]) -> dict[str, Any]:
    """单条 record 序列化超 64KiB 时截断字符串值并标记 ``truncated``。

    键名全部保留；只有值被截断。极端的非字符串巨型 payload 兜底丢弃
    attributes/events（仍保留标记），保证 record 上限成立。
    """
    if _record_size(record) <= SPAN_RECORD_LIMIT_BYTES:
        return record
    budget = _TRUNCATION_START_BUDGET
    fitted = _truncate_strings(record, budget)
    while _record_size(fitted) > SPAN_RECORD_LIMIT_BYTES and budget > 0:
        budget //= 2
        fitted = _truncate_strings(record, budget)
    if _record_size(fitted) > SPAN_RECORD_LIMIT_BYTES:
        fitted = dict(fitted)
        fitted["attributes"] = {"truncated": True}
        fitted["events"] = [{"name": "events_dropped", "truncated": True}]
    return fitted


class Span:
    """一次 ``trace_span`` 上下文；退出后 ``record`` 是最终（已截断）形态。

    根 span 额外持有 ``records``（整棵树，子先父后）与 ``flush_error``。
    """

    def __init__(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        flush: Callable[[list[dict[str, Any]]], Any] | None = None,
        parent: "Span | None" = None,
    ) -> None:
        self.name = str(name)
        self.span_id = _new_span_id()
        self.parent_id = parent.span_id if parent is not None else None
        self.attributes: dict[str, Any] = (
            dict(redact_secrets(dict(attributes))) if attributes else {}
        )
        self.events: list[dict[str, Any]] = []
        self.exception: dict[str, str] | None = None
        self.status = "ok"
        self.duration_s: float | None = None
        self.flush_error: str | None = None
        #: 最终 record（上下文退出后可读）。
        self.record: dict[str, Any] | None = None
        self._root: Span = parent._root if parent is not None else self
        # flush 只在外层上下文生效；嵌套 span 传 flush 被忽略。
        self._flush = flush if parent is None else None
        self._records: list[dict[str, Any]] | None = [] if parent is None else None
        self._started = time.time()

    def set_attribute(self, key: str, value: Any) -> None:
        """补记属性：值过脱敏后存储。"""
        self.attributes[str(key)] = redact_secrets(value)

    def add_event(
        self, name: str, attributes: Mapping[str, Any] | None = None
    ) -> None:
        """记一条 span 事件（属性同样脱敏）。"""
        self.events.append({
            "name": str(name),
            "timestamp": time.time(),
            "attributes": dict(redact_secrets(dict(attributes))) if attributes else {},
        })

    def record_exception(self, error: BaseException) -> None:
        """异常捕获：类型 + 消息（消息脱敏），并补一条 exception 事件。"""
        self.status = "error"
        message = redact_secrets(str(error))
        self.exception = {
            "type": type(error).__name__,
            "message": message,
        }
        self.add_event("exception", {
            "type": type(error).__name__,
            "message": message,
        })

    @property
    def records(self) -> list[dict[str, Any]]:
        """整棵树的 record 列表（子先父后；从任意 span 都可读）。"""
        return list(self._root._records or ())

    def _build_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "name": self.name,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "status": self.status,
            "started_at": self._started,
            "duration_s": self.duration_s,
            "attributes": self.attributes,
            "events": self.events,
            "exception": self.exception,
        }
        if self._root is self:
            record["flush_error"] = self.flush_error
        return record


#: 当前 context 的 span 栈（嵌套关系来源）。
_SPAN_STACK: ContextVar[tuple[Span, ...]] = ContextVar(
    "motte_trace_span_stack", default=()
)
#: 当前 context 最近完成的外层 span（读取 flush_error / records 用）。
_LAST_ROOT: ContextVar[Span | None] = ContextVar(
    "motte_trace_last_root", default=None
)


def _finish_span(span: Span) -> None:
    record = span._build_record()
    fitted = fit_span_record(record)
    span.record = fitted
    root = span._root
    if root is not span:
        root._records.append(fitted)
        return
    # 外层退出：flush 一次，失败只记 flush_error（协议 §4：绝不传播/重跑）。
    root._records.append(fitted)
    if root._flush is not None:
        try:
            root._flush(list(root._records))
        except Exception as error:  # noqa: BLE001 — 协议要求吞掉 flush 失败
            root.flush_error = redact_secrets(
                f"{type(error).__name__}: {error}"
            )
            root.record = fit_span_record({**record, "flush_error": root.flush_error})
            root._records[-1] = root.record
    _LAST_ROOT.set(root)


@contextlib.contextmanager
def trace_span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    flush: Callable[[list[dict[str, Any]]], Any] | None = None,
) -> Iterator[Span]:
    """trace 上下文：嵌套由调用栈决定，异常捕获后原样重抛。"""
    stack = _SPAN_STACK.get()
    parent = stack[-1] if stack else None
    span = Span(name, attributes=attributes, flush=flush, parent=parent)
    token = _SPAN_STACK.set(stack + (span,))
    try:
        yield span
    except BaseException as error:
        span.record_exception(error)
        raise
    finally:
        _SPAN_STACK.reset(token)
        span.duration_s = time.time() - span._started
        _finish_span(span)


def motte_trace(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    flush: Callable[[list[dict[str, Any]]], Any] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """装饰器：被包裹的 callable 只执行一次，异常原样穿透。

    返回值即 callable 的返回值；trace 事实通过 ``last_trace_span()``、
    flush 回调或根 span 的 ``records`` 读取。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace_span(name, attributes=attributes, flush=flush):
                return func(*args, **kwargs)  # 只执行一次，绝不重跑

        return wrapper

    return decorator


def last_trace_span() -> Span | None:
    """当前 context 最近一个已完成的外层 span（无则 None）。"""
    return _LAST_ROOT.get()
