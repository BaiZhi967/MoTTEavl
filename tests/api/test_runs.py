import pytest

fastapi = pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from apps.api.app.main import app, runs


def test_create_run_returns_queued_run():
    runs.clear()
    response = TestClient(app).post('/api/v1/runs', json={'scenario_version': 'json_extract@1'})
    assert response.status_code == 202
    assert response.json()['status'] == 'queued'


def test_create_run_rejects_unsupported_capability():
    runs.clear()
    response = TestClient(app).post('/api/v1/runs', json={'scenario_version': 'vision@1'})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'MODEL_CAPABILITY_UNSUPPORTED'
    assert runs == {}
