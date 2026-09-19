"""Agent 运行期错误类型。

``AgentFatalError`` 表示"副作用已发生但持久证据边界失败"一类的不确定状态：
运行时不得把它当普通工具错误回灌，必须中止执行；服务层据此隔离（needs_review）。
"""
from __future__ import annotations


class AgentFatalError(Exception):
    """中止执行的不确定状态；``quarantine=True`` 时服务层转入 needs_review。"""

    def __init__(self, message: str, *, quarantine: bool = True, code: str = "AGENT_EVIDENCE_FAILURE") -> None:
        super().__init__(message)
        self.quarantine = quarantine
        self.code = code
