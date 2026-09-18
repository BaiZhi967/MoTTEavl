import json

from motte_contracts.events import TraceEvent
from motte_contracts.jsonschema import dump_json_schema
from motte_contracts.model import ModelProfile


def test_trace_event_round_trips_with_version_and_parent():
    event = TraceEvent(
        protocol="motte.trace",
        schema_version=1,
        run_id="run-1",
        seq=2,
        span_id="span-2",
        parent_span_id="span-1",
        type="tool_call",
        payload={"name": "read_file", "arguments": {"path": "README.md"}},
    )
    assert TraceEvent.model_validate_json(event.model_dump_json()) == event


def test_model_profile_rejects_unknown_fields():
    try:
        ModelProfile(id="m", provider="p", capabilities={}, unexpected=True)
    except Exception:
        return
    raise AssertionError("unknown fields must be rejected")


def test_dump_json_schema_contains_public_contracts():
    schema = dump_json_schema()
    for name in (
        "RunStatus", "Run", "CaseRun", "CaseAttempt", "ExecutionError", "ErrorEnvelope",
        "ExecutionSpec", "EvaluationDescriptor", "ResolvedManifest", "TraceEvent", "Score",
        "ScoringPass", "ScoreSet", "RunCommand", "RunReport", "IdentityPolicy",
        "IdentityEvidence", "IdentityResult", "ModelProfile",
    ):
        assert name in schema
    assert "scenario_version" in schema["Run"]["properties"]
    assert "scenario_id" not in schema["Run"]["properties"]
    assert "needs_review" in schema["RunStatus"]["enum"]
    json.dumps(schema)
