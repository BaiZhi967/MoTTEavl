import json

from motte_agent.builtin_react import BuiltinReActRuntime


def scripted_complete(responses):
    """按顺序返回固定 content 的 envelope；同时记录收到的请求供断言。"""
    requests = []
    queue = list(responses)

    def complete(request):
        requests.append(request)
        return {"content": queue.pop(0)}

    complete.requests = requests
    return complete


def test_final_answer_without_tools():
    complete = scripted_complete([json.dumps({"action": "final", "answer": "hi"})])
    runtime = BuiltinReActRuntime(complete)
    result = runtime.run("say hi")
    assert result == {"status": "completed", "answer": "hi", "steps": 1}
    assert [event["type"] for event in runtime.events] == ["step_started", "final_answer"]


def test_tool_call_flow_with_observation_feedback():
    calls = []
    complete = scripted_complete(
        [
            json.dumps({"action": "tool", "tool": "add", "input": {"a": 1, "b": 2}}),
            json.dumps({"action": "final", "answer": 3}),
        ]
    )
    runtime = BuiltinReActRuntime(complete, {"add": lambda arg: calls.append(arg) or arg["a"] + arg["b"]})
    result = runtime.run("1+2=?")
    assert result["status"] == "completed"
    assert result["answer"] == 3
    assert calls == [{"a": 1, "b": 2}]
    assert [event["type"] for event in runtime.events] == [
        "step_started",
        "tool_call",
        "tool_result",
        "step_started",
        "final_answer",
    ]
    # 第二轮请求包含 observation 回灌
    second_round = complete.requests[1].messages
    assert any("observation: 3" in message.content for message in second_round)


def test_unknown_tool_is_denied_as_observation():
    complete = scripted_complete(
        [
            json.dumps({"action": "tool", "tool": "missing", "input": 1}),
            json.dumps({"action": "final", "answer": "fallback"}),
        ]
    )
    runtime = BuiltinReActRuntime(complete)
    result = runtime.run("x")
    assert result["status"] == "completed"
    assert "tool_denied" in [event["type"] for event in runtime.events]
    assert "tool not available: missing" in complete.requests[1].messages[-1].content


def test_tool_error_does_not_kill_loop():
    def explode(_arg):
        raise RuntimeError("boom")

    complete = scripted_complete(
        [
            json.dumps({"action": "tool", "tool": "explode", "input": None}),
            json.dumps({"action": "final", "answer": "recovered"}),
        ]
    )
    runtime = BuiltinReActRuntime(complete, {"explode": explode})
    result = runtime.run("x")
    assert result["answer"] == "recovered"
    assert "tool error: boom" in complete.requests[1].messages[-1].content


def test_invalid_responses_consume_budget_and_end_in_budget_exceeded():
    complete = scripted_complete(["not json", "also not json"])
    runtime = BuiltinReActRuntime(complete, max_steps=2)
    result = runtime.run("x")
    assert result == {"status": "budget_exceeded", "answer": None, "steps": 2}
    types = [event["type"] for event in runtime.events]
    assert types.count("invalid_response") == 2
    assert types[-1] == "budget_exceeded"


def test_system_prompt_lists_tools():
    complete = scripted_complete([json.dumps({"action": "final", "answer": 1})])
    runtime = BuiltinReActRuntime(complete, {"calc": lambda a: a})
    runtime.run("x")
    assert "calc" in complete.requests[0].system
