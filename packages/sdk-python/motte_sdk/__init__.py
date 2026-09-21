"""motte_sdk 包入口（M7 协议 §1.5：``import motte_sdk`` 零副作用）。

- 不建 DB、不启 Worker/Provider、不读环境凭据；HTTP 客户端依赖只有 ``httpx``。
- 执行栈模块（executor/service/dispatcher/…）懒导入：``from motte_sdk import
  MotteClient`` 不触发执行栈导入；``RunExecutor`` 经 PEP 562 ``__getattr__``
  首次访问时才加载（既有导出名保持可用）。
"""
from typing import TYPE_CHECKING, Any

from .client import MotteClient, StreamState
from .client_errors import (
    ApiError,
    AuthenticationError,
    ConflictError,
    ConnectError,
    MaintenanceError,
    MotteClientError,
    NetworkError,
    NotFoundError,
    OperationCancelled,
    PermissionDeniedError,
    RateLimitedError,
    ReadTimeout,
    ServerError,
    TransportError,
    UnsupportedCapabilityError,
    ValidationError,
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
    TERMINAL_RUN_STATUSES,
    is_terminal_status,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from .executor import RunExecutor

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ApiError",
    "AuthenticationError",
    "Baseline",
    "Capabilities",
    "Comparison",
    "ConflictError",
    "ConnectError",
    "EventsSnapshot",
    "Experiment",
    "Gate",
    "MaintenanceError",
    "MotteClient",
    "MotteClientError",
    "NetworkError",
    "NotFoundError",
    "OperationCancelled",
    "PermissionDeniedError",
    "RateLimitedError",
    "ReadTimeout",
    "Report",
    "RunExecutor",
    "RunList",
    "RunView",
    "ScoringPassList",
    "ServerError",
    "StreamState",
    "TERMINAL_RUN_STATUSES",
    "TransportError",
    "UnsupportedCapabilityError",
    "ValidationError",
    "WaitTimeout",
    "classify",
    "is_retryable",
    "is_terminal_status",
]


def __getattr__(name: str) -> Any:
    if name == "RunExecutor":
        from .executor import RunExecutor

        return RunExecutor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
