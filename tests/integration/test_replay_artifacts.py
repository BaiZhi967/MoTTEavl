from motte_sdk.replay_run import ReplayProvider, run_replay


def test_replay_run_produces_deterministic_trace_and_score():
    fixture = {"case-1": {"output": {"name": "Ada"}, "expected": {"name": "Ada"}}}
    first = run_replay("run-a", fixture, ReplayProvider(fixture))
    second = run_replay("run-b", fixture, ReplayProvider(fixture))
    assert first["status"] == second["status"] == "completed"
    assert first["scores"] == second["scores"] == [{"case_id": "case-1", "passed": True}]
    assert [event["type"] for event in first["trace"]] == ["case_started", "model_response", "score", "completed"]
