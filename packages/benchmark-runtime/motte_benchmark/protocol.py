"""benchmark-runtime 层协议：错误类型与共享限制默认值。

adapter 操作协议（prepare/start/poll/interrupt/collect/cleanup）以
``motte_contracts.external_job.ExternalJobAdapter`` 为准；本模块只补充
运行时错误码与默认限制。
"""
from __future__ import annotations

from typing import Any


class BenchmarkRuntimeError(RuntimeError):
    """外部 Job 运行时错误；``code`` 进入证据，message 面向操作员。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# 与 ExternalJobSpec.limits 对齐的默认限制；None 表示不设限。
DEFAULT_JOB_LIMITS: dict[str, Any] = {
    # stdout/stderr 各自保留的尾部字节数（超出即丢弃头部并标记 truncated）。
    "max_output_bytes": 1_048_576,
    # results.json 单文件上限（防超大 JSON/半写炸弹）。
    "max_result_bytes": 10_485_760,
    # interrupt 的 TERM→KILL 宽限秒数。
    "interrupt_grace_seconds": 5.0,
    # supervisor 轮询间隔与总执行期限。公开创建路径未声明时也给挂起 Job
    # 一个有限上界（review R05：不能无限占用资源/持续调用模型）。
    "poll_interval_seconds": 0.1,
    "max_wall_seconds": 3600.0,
}
