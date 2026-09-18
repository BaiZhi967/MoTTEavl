import json

from fastapi.testclient import TestClient
from motte_contracts.direct_llm import import_direct_llm_jsonl, scenario_for as direct_scenario
from motte_contracts.gsm8k import import_official_jsonl, scenario_for as gsm_scenario
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

from apps.api.app.main import create_app


def _client_with_both_suites():
    resources = InMemoryResourceStore()
    direct = import_direct_llm_jsonl(
        (json.dumps({"input": "Say yes"}) + "\n").encode(),
        name="direct-probe",
        version="1",
        license_id="test",
        scorer="exact",
        source="test",
        synthetic=True,
    )
    gsm = import_official_jsonl(
        (json.dumps({"question": "1+1?", "answer": "reason\n#### 2"}) + "\n").encode(),
        name="gsm-probe",
        version="1",
        revision="a" * 40,
        license_id="test",
        scope="full",
        source="test",
        synthetic=True,
    )
    direct_s = direct_scenario(direct, version="1")
    gsm_s = gsm_scenario(gsm, name="gsm-probe-full", version="1")
    resources.datasets.put(direct)
    resources.datasets.put(gsm)
    resources.scenarios.put(direct_s)
    resources.scenarios.put(gsm_s)
    return TestClient(create_app(InMemoryRunStore(), resources)), direct, direct_s, gsm, gsm_s


def test_gsm8k_routes_reject_direct_llm_resources():
    client, direct, direct_s, _, _ = _client_with_both_suites()
    dataset_ref = f"{direct['name']}@{direct['version']}"
    scenario_ref = f"{direct_s['name']}@{direct_s['version']}"

    cases = client.get("/api/v1/benchmarks/gsm8k/cases", params={"dataset": dataset_ref})
    run = client.post("/api/v1/benchmarks/gsm8k/runs", json={"scenario": scenario_ref, "model": "m"})

    assert cases.status_code == 422
    assert cases.json()["error"]["code"] == "SUITE_MISMATCH"
    assert run.status_code == 422
    assert run.json()["error"]["code"] == "SUITE_MISMATCH"


def test_direct_llm_routes_reject_gsm8k_resources():
    client, _, _, gsm, gsm_s = _client_with_both_suites()
    dataset_ref = f"{gsm['name']}@{gsm['version']}"
    scenario_ref = f"{gsm_s['name']}@{gsm_s['version']}"

    cases = client.get("/api/v1/benchmarks/direct-llm/cases", params={"dataset": dataset_ref})
    run = client.post("/api/v1/benchmarks/direct-llm/runs", json={"scenario": scenario_ref, "model": "m"})

    assert cases.status_code == 422
    assert cases.json()["error"]["code"] == "SUITE_MISMATCH"
    assert run.status_code == 422
    assert run.json()["error"]["code"] == "SUITE_MISMATCH"
