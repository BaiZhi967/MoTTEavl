"""F05/F16 反例：Builtin Target adapter 的实际执行期限与观察契约。

这些用例断言的是**真实调用数与状态**，不是"等了多少毫秒"：

* adapter 必须把剩余期限交给 runtime；过期响应里的写工具不会被调用，
  也不会再发第二次模型请求；
* runtime 已经终止时，adapter 的 observe 必须读出状态，停止确认与事实一致。

时间证据只来自同步屏障（BarrierProvider）与可控时钟（ControllableClock），
两者都不依赖机器快慢。
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from motte_agent.builtin_react import BuiltinReActRuntime
from motte_sdk.scenario_target import BuiltinTargetSession

MANIFEST = {"agent_config": {"mode": "legacy-json"}, "provider": {"model": "offline"}}


class ControllableClock:
    """可控单调时钟；只有测试显式推进它才会前进。"""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def tool_decision(tool: str, arguments: dict | None = None) -> dict:
    return {
        "content": json.dumps(
            {"action": "tool", "tool": tool, "input": arguments or {}}, ensure_ascii=False
        )
    }


def final_answer(answer: str) -> dict:
    return {"content": json.dumps({"action": "final", "answer": answer}, ensure_ascii=False)}


def join_model_threads(timeout: float = 10.0) -> list[threading.Thread]:
    """等待被放弃的模型调用线程真正结束（运行时的线程有固定名字）。"""
    joined: list[threading.Thread] = []
    for thread in list(threading.enumerate()):
        if thread.name == "agent-model-call":
            thread.join(timeout=timeout)
            joined.append(thread)
    return joined


class BarrierProvider:
    """第一轮模型调用阻塞在屏障上；屏障只在 adapter 已按期限返回之后才释放。

    因此"响应在期限之后才到达"是**屏障保证**的事实，不是计时巧合。
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def complete(self, request):  # noqa: ANN001, ARG002 - Provider 协议
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            self.release.wait(5.0)
            return tool_decision("write", {"path": "report.json"})
        return final_answer("late second call")


def _patched_provider(complete):  # noqa: ANN001
    return patch(
        "motte_sdk.agent_backend.build_agent_provider",
        return_value=SimpleNamespace(provider=SimpleNamespace(complete=complete)),
    )


def test_adapter_cuts_the_send_at_the_deadline_and_never_runs_the_write_tool():
    provider = BarrierProvider()
    writes: list = []
    with _patched_provider(provider.complete):
        session = BuiltinTargetSession(
            MANIFEST, {"write": lambda arguments: writes.append(arguments) or "written"}
        )
        session.begin()
        deadline = time.monotonic() + 0.5
        result = session.send("go", deadline=deadline)

        # 模型调用确实开始了，而且在我们返回时还没有产生响应（屏障未释放）。
        assert provider.entered.wait(5.0)
        assert provider.calls == 1
        assert not provider.release.is_set()

        assert result["termination_reason"] == "per_call_timeout"
        assert result["timeout"] is True
        assert result["stopped"] is True
        assert result["interrupt"]["confirmed"] is True
        assert writes == []

        # 释放屏障：迟到的响应完整到达 runtime 之后，写工具仍然没有被调用，
        # 也没有出现第二次模型请求。
        provider.release.set()
        assert join_model_threads()
        assert writes == []
        assert provider.calls == 1

    observed = session.observe()
    assert observed["state"] == "terminated"
    assert session.interrupt("operator")["confirmed"] is True


def test_an_expired_model_response_never_reaches_a_write_tool():
    clock = ControllableClock()
    tool_calls: list = []
    model_calls: list = []

    def complete(request):  # noqa: ANN001
        model_calls.append(request)
        # 响应到达时已经越过期限：工具执行边界必须再次核验期限。
        clock.advance(5.0)
        return tool_decision("write", {"path": "report.json"})

    runtime = BuiltinReActRuntime(
        complete,
        {"write": lambda arguments: tool_calls.append(arguments) or "written"},
        mode="legacy-json",
        now=clock,
    )
    runtime.begin()
    outcome = runtime.send("go", deadline=clock() + 1.0)

    assert outcome["termination_reason"] == "per_call_timeout"
    assert tool_calls == []
    assert len(model_calls) == 1
    assert runtime.observe()["state"] == "terminated"


def test_an_already_expired_deadline_issues_no_model_request():
    clock = ControllableClock()
    model_calls: list = []

    def complete(request):  # noqa: ANN001
        model_calls.append(request)
        return final_answer("should never happen")

    runtime = BuiltinReActRuntime(complete, {}, mode="legacy-json", now=clock)
    runtime.begin()
    outcome = runtime.send("go", deadline=clock() - 1.0)

    assert outcome["termination_reason"] == "per_call_timeout"
    assert model_calls == []
    assert runtime.observe()["state"] == "terminated"


def test_observe_reads_the_runtime_state_and_stop_confirmation_matches_facts():
    model_calls: list = []

    def complete(request):  # noqa: ANN001
        model_calls.append(request)
        return final_answer("done")

    with _patched_provider(complete):
        session = BuiltinTargetSession(MANIFEST, {})
        assert session.begin()["state"] == "active"
        turn = session.send("hello")
        assert turn["termination_reason"] == "final_answer"

        observed = session.observe()
        assert observed["state"] == "active"  # 正常 final answer 只结束当前 turn
        assert observed["turns"] == 1
        assert observed["tool_calls"] == 0
        assert observed["termination_reason"] == "final_answer"
        assert observed["session_id"]

        assert session.close() == {"state": "closed"}
        observed = session.observe()
        assert observed["state"] == "closed"
        assert observed["closed"] is True
        # 已关闭的会话同样是"无法再运行"的事实：停止确认保持 true。
        assert session.interrupt("again")["confirmed"] is True

    # 已终止的会话关闭后状态仍是事实（terminated），不会被伪造成别的东西
    with _patched_provider(complete):
        stopped = BuiltinTargetSession(MANIFEST, {})
        stopped.begin()
        stop = stopped.interrupt("operator")
        assert stop["confirmed"] is True
        assert stop["state"] == "terminated"
        assert stopped.observe()["state"] == "terminated"
        assert stopped.close() == {"state": "terminated"}
        assert stopped.observe()["closed"] is True
        assert stopped.interrupt("again")["confirmed"] is True
    assert len(model_calls) == 1


def test_a_never_started_session_cannot_claim_a_confirmed_stop():
    def complete(request):  # noqa: ANN001, ARG002 - Provider 协议
        raise AssertionError("no model request may be issued before begin()")

    with _patched_provider(complete):
        session = BuiltinTargetSession(MANIFEST, {})
        # 没有 begin 就没有运行中的运行时状态：不能声称已停止。
        assert session.observe()["state"] == "idle"
        assert session.interrupt("operator")["confirmed"] is False
