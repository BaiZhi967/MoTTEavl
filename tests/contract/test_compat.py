from motte_contracts.compat import (
    adapt_legacy_report,
    adapt_legacy_run,
    adapt_legacy_trace_event,
)
from motte_contracts.events import TraceEvent
from motte_contracts.report import RunReport
from motte_contracts.run import Run


def test_legacy_run_without_revision_projects_as_schema_v1():
    legacy = {
        "id": "run-7",
        "scenario_version": "replay@1",
        "status": "completed",
        "manifest": {},
        "scores": [{"case_id": "case-1", "passed": True}],
    }
    projected = adapt_legacy_run(legacy)
    parsed = Run.model_validate(projected)
    assert parsed.schema_version == 1
    assert parsed.revision == 0
    assert "revision" not in legacy


def test_legacy_flat_trace_event_moves_details_into_payload():
    legacy = {
        "run_id": "run-7",
        "seq": 3,
        "type": "model_response",
        "case_id": "case-1",
        "usage": {"total_tokens": 4},
    }
    parsed = TraceEvent.model_validate(adapt_legacy_trace_event(legacy))
    assert parsed.schema_version == 1
    assert parsed.payload == {"case_id": "case-1", "usage": {"total_tokens": 4}}
    assert "payload" not in legacy


def test_legacy_report_without_scoring_pass_remains_readable():
    legacy = {
        "run_id": "run-7",
        "scenario_version": "replay@1",
        "status": "completed",
        "generated_at": "2026-01-01T00:00:00Z",
        "summary": {"cases": 1, "scored": 1, "passed": 1, "failed": 0, "pass_rate": 1.0},
        "cost": {"total": None, "price_table_versions": []},
        "scores": [{"case_id": "case-1", "passed": True}],
    }
    parsed = RunReport.model_validate(adapt_legacy_report(legacy))
    assert parsed.schema_version == 1
    assert parsed.scoring_pass_id is None
