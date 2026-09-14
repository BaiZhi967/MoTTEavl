from motte_agent.builtin_react import BuiltinReActRuntime
def test_no_tool_returns_answer(): assert BuiltinReActRuntime().run("hi")=="hi"
def test_budget_is_enforced():
 r=BuiltinReActRuntime(max_steps=0)
 assert r.run("x") == "budget_exceeded"
