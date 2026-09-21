"""Published and inline runtime budgets must be enforceable, not just parseable."""
import pytest
from pydantic import ValidationError
from motte_contracts.runtime import RuntimeProfile, RuntimeProfileVersion


@pytest.mark.parametrize("contract", [RuntimeProfile, RuntimeProfileVersion])
@pytest.mark.parametrize("runtime,budgets", [
    ("pi-agent@1", {"total_timeout": "nan"}),
    ("pi-agent@1", {"total_timeout": float("nan")}),
    ("pi-agent@1", {"total_timeout": float("inf")}),
    ("pi-agent@1", {"total_timeout": 10**1000}),
    ("codex-cli@1", {"idle_timeout": -(10**1000)}),
    ("pi-agent@1", {"total_timeout": 0}),
    ("pi-agent@1", {"max_steps": -1}),
    ("pi-agent@1", {"max_steps": True}),
    ("pi-agent@1", {"max_tool_calls": 1.5}),
    ("pi-agent@1", {"max_cost_usd": 0}),
    ("pi-agent@1", {"unrecognized_budget": 1}),
    ("claude-cli@1", {"max_steps": 2}),
    ("codex-cli@1", {"idle_timeout": -1}),
    ("codex-cli@1", {"idle_timeout": "nan"}),
])
def test_rejects_unenforceable_budgets(contract, runtime, budgets):
    payload = {"runtime": runtime, "budgets": budgets}
    if contract is RuntimeProfileVersion:
        payload.update(name="bounded", version="1", published_at="2026-09-21T00:00:00Z")
    with pytest.raises(ValidationError):
        contract.model_validate(payload)


def test_zero_tool_calls_is_explicitly_preserved():
    profile = RuntimeProfile(runtime="pi-agent@1", budgets={"max_tool_calls": 0})
    assert profile.budgets == {"max_tool_calls": 0}
