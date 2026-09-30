"""Merged Stats/Judge routes retain one scoped validation dispatcher."""
import ast
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_storage.run_store import InMemoryRunStore


@pytest.mark.parametrize(
    "path,body,code",
    [
        ("/api/v1/judge-calibrations", {"calibration": None, "PRIVATE_INPUT": True}, "CALIBRATION_CONTRACT_INVALID"),
        ("/api/v1/judge-calibrations/missing/versions/v1/jobs", {"request": {"PRIVATE_INPUT": float("inf")}}, "CALIBRATION_CONTRACT_INVALID"),
        ("/api/v1/judges", {"qualification_id": float("nan")}, "JUDGE_CONTRACT_INVALID"),
        ("/api/v1/judges/preflight", {"qualification_id": float("inf")}, "JUDGE_CONTRACT_INVALID"),
        ("/api/v1/statistical-reports", {"k": float("inf")}, None),
    ],
)
def test_scoped_validation_handles_nonfinite_inputs_without_echo(path, body, code):
    with TestClient(create_app(InMemoryRunStore())) as client:
        response = client.post(path, content=json.dumps(body), headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert "PRIVATE_INPUT" not in response.text
    if code is None:
        assert "detail" in response.json()
        assert all(set(issue) <= {"loc", "msg", "type"} for issue in response.json()["detail"])
    else:
        assert response.json()["error"]["code"] == code


def test_unrelated_validation_keeps_fastapi_default_shape():
    with TestClient(create_app(InMemoryRunStore())) as client:
        response = client.post("/api/v1/runs", json={"unexpected": True})
    assert response.status_code == 422
    assert "detail" in response.json() and "error" not in response.json()


def test_request_validation_dispatch_has_one_registration():
    source = Path(__file__).parents[2] / "apps/api/app/main.py"
    tree = ast.parse(source.read_text())
    registrations = [
        decorator for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "exception_handler"
        and any(isinstance(arg, ast.Name) and arg.id == "RequestValidationError" for arg in decorator.args)
    ]
    assert len(registrations) == 1
