from __future__ import annotations

from typing import Any


class ReplayProvider:
    def __init__(self, fixture: dict[str, dict[str, Any]]) -> None:
        self.fixture = fixture

    def invoke(self, case_id: str) -> dict[str, Any]:
        if case_id not in self.fixture:
            raise KeyError(case_id)
        return self.fixture[case_id]["output"]

    def expected_for(self, case_id: str) -> Any:
        return self.fixture.get(case_id, {}).get("expected")


def run_replay(run_id: str, cases: dict[str, dict[str, Any]], provider: ReplayProvider) -> dict[str, Any]:
    trace: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    for case_id, case in cases.items():
        trace.append({"run_id": run_id, "type": "case_started", "case_id": case_id})
        output = provider.invoke(case_id)
        trace.append({"run_id": run_id, "type": "model_response", "case_id": case_id, "output": output})
        passed = output == case.get("expected")
        scores.append({"case_id": case_id, "passed": passed})
        trace.append({"run_id": run_id, "type": "score", "case_id": case_id, "passed": passed})
    trace.append({"run_id": run_id, "type": "completed"})
    return {"run_id": run_id, "status": "completed", "trace": trace, "scores": scores}
