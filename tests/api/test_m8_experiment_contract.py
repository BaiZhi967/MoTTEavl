"""M8 experiment API preflight and durable idempotency, with real create_app."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_sdk.direct_llm import import_direct_llm_split
from tests.sdk.test_m6_experiments import make_service, spec_payload


def test_a03_ten_cases_two_cells_cap_five_has_zero_executable_side_effects() -> None:
    store, _run_service, service = make_service()
    raw = ("\n".join(json.dumps({"case_id": f"case-{index}", "input": f"Question {index}",
                                  "expected": str(index)}) for index in range(10)) + "\n").encode()
    scenario = import_direct_llm_split(
        raw, name="m8-http-ten", version="1", license_id="internal-sample",
        resources=service.resources, synthetic=True,
    )["scenario"]
    client = TestClient(create_app(store, service.resources))
    payload = spec_payload(
        task_ref={"suite": "direct-llm", "scenario_version": scenario},
        factors={"model_profile": ("model-a", "model-b")}, repeats=1,
        budget_policy={"max_total_calls": 5},
    )
    preview = client.post("/api/v1/experiments/preview", json=payload)
    assert preview.status_code == 200
    assert preview.json()["max_potential_calls"] == 20
    assert {item["code"] for item in preview.json()["violations"]} == {"BUDGET_EXCEEDED"}
    created = client.post("/api/v1/experiments", json=payload)
    assert created.status_code == 422
    assert "max_potential_calls 20" in created.text
    assert store.experiments.list_specs() == []
    assert store.experiments.list_cells("exp-alpha") == []
    assert store.runs.list() == []


def test_a05_request_key_conflict_is_http_409_after_app_rebuild() -> None:
    store, _run_service, service = make_service()
    payload = spec_payload(factors={"model_profile": ("model-a",)}, repeats=1)
    first = TestClient(create_app(store, service.resources)).post(
        "/api/v1/experiments", json={**payload, "request_key": "m8-http-key"},
    )
    assert first.status_code == 202, first.text
    rebuilt = TestClient(create_app(store, service.resources))
    replay = rebuilt.post("/api/v1/experiments", json={**payload, "request_key": "m8-http-key"})
    assert replay.status_code == 202, replay.text
    assert [cell["run_id"] for cell in replay.json()["cells"]] == [
        cell["run_id"] for cell in first.json()["cells"]
    ]
    changed = rebuilt.post(
        "/api/v1/experiments", json={**payload, "experiment_id": "exp-other",
                                     "version": "v2", "request_key": "m8-http-key"},
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "REQUEST_KEY_CONFLICT"
    assert store.experiments.list_specs("exp-other") == []
    assert len(store.runs.list()) == 1
