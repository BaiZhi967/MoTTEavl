"""Optional coding suites reuse the hermetic transport/egress guard fixture."""
from __future__ import annotations

import importlib
import json

import pytest

from tests.cli.test_opencode_integration_checks import wire  # noqa: F401


@pytest.mark.parametrize(("name", "expected_calls"), [
    ("check_direct_v2", 1), ("check_experiment_cell", 1), ("check_skill_ablation", 3),
])
def test_bounded_suite_has_real_persisted_evidence(
    wire, name, expected_calls, monkeypatch,  # noqa: F811
):
    ctx, calls = wire
    module = importlib.import_module("scripts.opencode_go_suite_checks")
    original_json = module._json

    def checked_json(response, status=200):
        assert response.status_code == status, response.text
        return original_json(response, status)

    monkeypatch.setattr(module, "_json", checked_json)
    result = getattr(module, name)(ctx)
    assert result["ok"] is True
    assert len(calls) == expected_calls
    assert "fake-key" not in json.dumps(result)
    if name == "check_direct_v2":
        assert result["contract_version"] == 2
        assert result["profile"] == "smoke"
        assert result["publication_verified"] is True
    else:
        assert result["cell_count"] == expected_calls
        assert result["completed_cells"] == expected_calls
        assert result["idempotent_allocation"] is True
    if name == "check_skill_ablation":
        assert result["arms"] == ["no-skill", "skill-v1", "skill-v2"]
        assert result["skill_snapshot_count"] == 2
        assert result["wire_injection_verified"] is True
        assert len(result["wire_skill_evidence"]) == 3
        assert all(row["matches_expected_arm"] for row in result["wire_skill_evidence"])
        systems = ["\n".join(m["content"] for m in body["messages"]
                              if m["role"] == "system") for _, body in calls]
        assert sum("Coding review instruction v1" in value for value in systems) == 1
        assert sum("Coding review instruction v2" in value for value in systems) == 1
        assert sum("Coding review instruction" not in value for value in systems) == 1


def test_skill_cannot_pass_with_missing_serialized_injection(wire, monkeypatch):  # noqa: F811
    from motte_provider.openai_compatible import OpenAICompatibleProvider

    module = importlib.import_module("scripts.opencode_go_suite_checks")
    original = OpenAICompatibleProvider.build_request_body

    def without_instruction(self, request):
        body = original(self, request)
        for message in body["messages"]:
            if message["role"] == "system":
                # Simulate a provider serialization regression despite intact frozen snapshots.
                message["content"] = "You are a coding assistant."
        return body

    monkeypatch.setattr(OpenAICompatibleProvider, "build_request_body", without_instruction)
    with pytest.raises(RuntimeError):
        module.check_skill_ablation(wire[0])
