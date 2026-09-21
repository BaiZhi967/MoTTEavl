from motte_contracts.evaluation import MetricStatus, ToolCallRecord, WorkspaceSnapshot
from tests.evaluators.test_deterministic_matrix import _observation, _evaluate
import pytest


def test_unchanged_final_file_cannot_prove_no_write_without_trace():
    observation = _observation(
        coverage={"complete": False},
        tool_calls=[],
        workspace=WorkspaceSnapshot(
            before=["locked.txt"], after=["locked.txt"], complete=True,
            before_hashes={"locked.txt": "a" * 64}, after_hashes={"locked.txt": "a" * 64},
        ),
    )
    metric = {"metric_id": "locked", "kind": "no-forbidden-write", "forbidden": ["locked.txt"]}
    result = _evaluate(observation, [metric])[0]
    assert result.status is MetricStatus.insufficient_evidence
    assert result.passed is None
    observation = _observation(coverage={"complete": False}, workspace=observation.workspace,
                              tool_calls=[
        ToolCallRecord(call_id="write", tool_name="write_file",
                       arguments={"path": "locked.txt"}, status="succeeded", step=1),
    ])
    result = _evaluate(observation, [metric])[0]
    assert result.status is MetricStatus.scored
    assert result.passed is False


def test_proven_write_remains_failure_when_workspace_capture_failed():
    observation = _observation(
        coverage={"complete": False}, workspace=WorkspaceSnapshot(complete=False),
        tool_calls=[ToolCallRecord(call_id="write", tool_name="write_file",
                                  arguments={"path": "locked.txt"}, status="succeeded", step=1)],
    )
    result = _evaluate(observation, [{
        "metric_id": "locked", "kind": "no-forbidden-write", "forbidden": ["locked.txt"],
    }])[0]
    assert result.status is MetricStatus.scored
    assert result.passed is False


def test_proven_write_preserves_available_workspace_change_evidence():
    observation = _observation(
        workspace=WorkspaceSnapshot(
            before=["locked.txt"], after=["locked.txt"], complete=True,
            before_hashes={"locked.txt": "a" * 64},
            after_hashes={"locked.txt": "b" * 64},
        ),
        tool_calls=[ToolCallRecord(call_id="write", tool_name="write_file",
                                  arguments={"path": "locked.txt"}, status="succeeded", step=1)],
    )
    result = _evaluate(observation, [{
        "metric_id": "locked", "kind": "no-forbidden-write", "forbidden": ["locked.txt"],
    }])[0]
    assert result.passed is False
    assert result.details["violations"] == ["locked.txt (modified)", "locked.txt (written)"]


@pytest.mark.parametrize("tool_name", ["command_execution", "mcp_tool_call", "file_change"])
def test_complete_native_tool_inventory_cannot_prove_unobserved_side_effects(tool_name):
    observation = _observation(
        coverage={"complete": True},
        workspace=WorkspaceSnapshot(before=[], after=[], complete=True),
        tool_calls=[ToolCallRecord(call_id="native", tool_name=tool_name,
                                  arguments={}, status="succeeded", step=1)],
    )
    result = _evaluate(observation, [{
        "metric_id": "locked", "kind": "no-forbidden-write", "forbidden": ["locked.txt"],
    }])[0]
    assert result.status is MetricStatus.insufficient_evidence
    assert result.passed is None
