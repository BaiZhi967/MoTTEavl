"""Immutable publication routes never recalculate stored exports."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_sdk.comparisons import ComparisonService
from motte_storage.platform import platform_for
from motte_storage.run_store import InMemoryRunStore
from motte_storage.statistical_reports import StatisticalReportConflict
from tests.sdk.test_m6_comparison_service import append_pass, make_run, score_row
from tests.sdk.test_statistical_reports import _seed

PATH = "/api/v1/statistical-reports"
BODY = {"baseline_run_id": "base", "candidate_run_id": "candidate",
        "baseline_pass_id": "old-base", "candidate_pass_id": "old-candidate"}


@pytest.fixture
def publication(monkeypatch):
    from motte_provider.transport import HTTPTransport

    def forbidden(*args, **kwargs):
        pytest.fail("statistical publication/read must not invoke providers")

    monkeypatch.setattr(HTTPTransport, "post_json_detailed", forbidden)
    monkeypatch.setattr(HTTPTransport, "post_sse", forbidden)
    store = _seed(InMemoryRunStore())
    return store, TestClient(create_app(store=store))


def test_api_publication_and_errors(publication, monkeypatch):
    store, client = publication
    first = client.post(PATH, json=BODY)
    assert first.status_code == 200
    published = first.json()
    assert client.post(PATH, json=BODY).json() == published
    path = f"{PATH}/{published['report_id']}"
    assert client.get(path).json() == published
    assert len(store.statistical_reports.list()) == 1
    assert client.get(PATH).status_code == 405
    for method in ("put", "patch", "delete"):
        assert getattr(client, method)(path).status_code == 405
    assert client.post(path, json=published).status_code == 405
    assert client.get(path, params={"format": "csv"}).status_code == 422
    assert client.get(f"{PATH}/missing").status_code == 404

    def conflict(*args, **kwargs):
        raise StatisticalReportConflict("immutable conflict")

    monkeypatch.setattr(store.statistical_reports, "put", conflict)
    response = client.post(PATH, json=BODY)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STATISTICAL_REPORT_CONFLICT"
    platform_for(store).meta.set("maintenance", "active")
    assert client.post(PATH, json=BODY).status_code == 503
    assert client.get(path).json() == published
    platform_for(store).meta.delete("maintenance")

    row = store.statistical_reports._rows[published["report_id"]]
    store.statistical_reports._rows[published["report_id"]] = (row[0], "{}", row[2])
    response = client.get(path)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STATISTICAL_REPORT_CORRUPT"


@pytest.mark.parametrize("overrides,status", [
    ({"k": 0}, 422), ({"k": -1}, 422), ({"k": True}, 422),
    ({"k": "2"}, 422), ({"k": 1.5}, 422),
    ({"body": {}}, 422), ({"policy": {}}, 422), ({"result": {}}, 422),
    ({"report_id": "forged"}, 422), ({"published_at": "now"}, 422),
    ({"baseline_run_id": " "}, 422), ({"allowed_factors": [""]}, 422),
    ({"allowed_factors": ["unknown"]}, 422),
    ({"baseline_pass_id": "old-candidate"}, 422),
    ({"candidate_pass_id": "old-base"}, 422),
    ({"baseline_run_id": "missing"}, 404), ({"candidate_run_id": "missing"}, 404),
    ({"baseline_pass_id": "missing"}, 404), ({"candidate_pass_id": "missing"}, 404),
])
def test_invalid_publication_leaves_no_report(publication, overrides, status):
    store, client = publication
    response = client.post(PATH, json={**BODY, **overrides})
    assert response.status_code == status, response.text
    assert store.statistical_reports.list() == []


def test_absent_current_pass_is_not_found(publication):
    store, client = publication
    make_run(store, "unscored", case_ids=["a"])
    response = client.post(PATH, json={"baseline_run_id": "unscored", "candidate_run_id": "candidate"})
    assert response.status_code == 404


@pytest.mark.parametrize("inapplicable", [False, True])
def test_json_junit_have_identical_stored_envelope(publication, monkeypatch, inapplicable):
    store, client = publication
    if inapplicable:
        append_pass(store, "base", "missing-score", [])
    body = {**BODY, "baseline_pass_id": "missing-score" if inapplicable else "old-base"}
    response = client.post(PATH, json=body)
    assert response.status_code == 200, response.text
    published = response.json()
    path = f"{PATH}/{published['report_id']}"
    from motte_sdk.export import statistical_report_to_json, statistical_report_to_junit
    before_xml = statistical_report_to_junit(published)
    append_pass(store, "base", "new-base", [score_row("a", passed=False)])
    store.case_runs._rows[("base", "a")][1]["result"]["cost"]["total"] = 900

    def forbidden(*args, **kwargs):
        pytest.fail("stored publication export must not recalculate or resolve current")

    monkeypatch.setattr(ComparisonService, "paired_statistics", forbidden)
    monkeypatch.setattr(store.runs, "get", forbidden)
    monkeypatch.setattr(store.scoring_passes, "get", forbidden)
    from motte_eval.statistics import STATISTICAL_POLICY_V1
    monkeypatch.setitem(STATISTICAL_POLICY_V1, "bootstrap_seed", 1)
    monkeypatch.setitem(STATISTICAL_POLICY_V1, "confidence", 0.5)
    json_export = client.get(path).json()
    xml_response = client.get(path, params={"format": "junit"})
    assert xml_response.status_code == 200
    assert xml_response.headers["content-type"].startswith("application/xml")
    assert xml_response.text == before_xml
    root = ET.fromstring(xml_response.text)
    assert json.loads(root.findtext("system-out")) == json_export == published
    assert statistical_report_to_json(published) == published
    properties = {p.attrib["name"]: p.attrib["value"] for p in root.findall("properties/property")}
    for field in ("refs", "input_digest", "policy_hash", "implementation_version", "unit", "k", "seed", "iterations", "missing_pairs"):
        assert field in properties
    assert properties["report_id"] == published["report_id"]
    assert properties["published_at"] == published["published_at"]
    assert properties["schema_version"] == "1"
    assert properties["interpretation"] == "descriptive_statistics_not_quality_gate"
    assert (root.find(".//skipped") is not None) is inapplicable
    assert root.find(".//failure") is None
    assert root.find(".//error") is None
    exported = statistical_report_to_json(published)
    exported["body"]["result"]["inputs"]["allowed_factors"].append("changed")
    assert statistical_report_to_json(published) == json_export
    with pytest.raises(ValueError):
        statistical_report_to_json(exported)
    with pytest.raises(ValueError):
        statistical_report_to_junit(exported)


def test_dynamic_comparison_never_publishes(publication, monkeypatch):
    store, client = publication
    from motte_sdk.service import RunService

    def forbidden(*args, **kwargs):
        pytest.fail("read-only comparison must not run providers or create work")

    monkeypatch.setattr(RunService, "create_run", forbidden)
    assert client.get("/api/v1/comparisons/statistics", params={"baseline": "base", "candidate": "candidate"}).status_code == 200
    assert client.get("/api/v1/comparisons", params={"baseline": "base", "candidate": "candidate"}).status_code == 200
    assert store.statistical_reports.list() == []


def test_publication_refuses_stored_corruption_and_guard_conflict(publication, monkeypatch):
    from motte_storage.operation_locks import MaintenanceConflict

    store, client = publication
    first = client.post(PATH, json=BODY).json()
    row = store.statistical_reports._rows[first["report_id"]]
    store.statistical_reports._rows[first["report_id"]] = (row[0], "{}", row[2])
    response = client.post(PATH, json=BODY)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STATISTICAL_REPORT_CORRUPT"

    def conflict(*args, **kwargs):
        raise MaintenanceConflict("another process owns maintenance")

    monkeypatch.setattr(store.statistical_reports, "put", conflict)
    response = client.post(PATH, json=BODY)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MAINTENANCE_MODE"


@pytest.mark.parametrize("field,literal", [
    ("k", "1e309"), ("k", "-1e309"), ("k", "NaN"),
    ("k", "Infinity"), ("k", "-Infinity"),
    ("baseline_run_id", "1e309"), ("candidate_pass_id", "NaN"),
    ("allowed_factors", "[1e309]"), ("extra", '{"nested": NaN}'),
])
def test_nonfinite_request_inputs_have_finite_422_response(publication, field, literal):
    store, client = publication
    body = {key: value for key, value in BODY.items() if key != field}
    raw_body = json.dumps(body)[:-1] + f', "{field}": {literal}' + "}"
    response = client.post(PATH, content=raw_body, headers={"content-type": "application/json"})
    assert response.status_code == 422, response.text
    errors = json.loads(response.text, parse_constant=lambda value: pytest.fail(value))["detail"]
    assert errors
    assert all(set(error) == {"loc", "msg", "type"} for error in errors)
    assert store.statistical_reports.list() == []


def test_other_route_validation_keeps_default_response(publication):
    store, client = publication
    response = client.post("/api/v1/runs", json={"unexpected": "keep-default-input"})
    assert response.status_code == 422
    assert any(error.get("input") == "keep-default-input" for error in response.json()["detail"])
    assert store.statistical_reports.list() == []


def test_openapi_describes_statistical_report_error_envelopes(publication):
    _store, client = publication
    schema = client.app.openapi()
    published = schema["paths"][PATH]["post"]["responses"]
    retrieved = schema["paths"][PATH + "/{report_id}"]["get"]["responses"]
    assert set(published) == {"200", "404", "409", "422", "503"}
    assert set(retrieved) == {"200", "404", "409", "422"}
    error_schema = published["404"]["content"]["application/json"]["schema"]
    assert error_schema["required"] == ["error"]
    detail = error_schema["properties"]["error"]
    assert set(detail["required"]) == {"code", "message"}
    assert detail["properties"] == {"code": {"type": "string"}, "message": {"type": "string"}}
    for responses, statuses in ((published, ("409", "503")), (retrieved, ("404", "409"))):
        for status in statuses:
            assert responses[status]["content"]["application/json"]["schema"] == error_schema
    assert published["422"]["content"]["application/json"]["schema"]["anyOf"] == [
        {"$ref": "#/components/schemas/HTTPValidationError"}, error_schema,
    ]
    assert "HTTPValidationError" in schema["components"]["schemas"]
