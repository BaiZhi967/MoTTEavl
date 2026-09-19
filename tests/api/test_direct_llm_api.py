"""Direct LLM 评测控制台端点的离线测试（内置样例直接读仓库自带 JSONL，零网络零费用）。

隔离要求：`create_app()` 默认 store 指向开发者本地 SQLite，这里一律显式传内存 store，
避免测试夹具污染本地数据集/场景（见 tests/conftest.py）。
"""

import json

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import _direct_llm_accuracy, create_app
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

CUSTOM = (
    "\n".join(
        [
            json.dumps({"input": "CUSTOM one", "expected": "1"}),
            json.dumps({"input": "CUSTOM two", "expected": "2"}),
            json.dumps({"input": "CUSTOM three"}),
        ]
    )
    + "\n"
)


def _v2_dataset():
    cases = [
        {
            "case_id": f"v2-case-{index}",
            "input": f"Return the number {index}",
            "expected": str(index),
            "metadata": {
                "source_line": index + 1,
                "source_id": f"source-{index}",
                "language": "en",
                "split": "test",
                "tags": ["api-v2"],
            },
        }
        for index in range(2)
    ]
    return {
        "name": "api-direct-v2",
        "version": "1",
        "contract_version": 2,
        "eval": {
            "suite": "direct-llm",
            "id": "direct-llm-prompts",
            "version": 2,
            "selected_count": 2,
            "selection": "all-rows-in-file-order",
            "scorer": {"id": "numeric", "version": "1", "config": {}},
            "prompt_version": "api-v2",
            "max_output_tokens": 32,
            "max_retries": 0,
        },
        "provenance": {
            "source_id": "api-v2",
            "source_kind": "synthetic-test",
            "homepage": None,
            "upstream_revision": "fixture-v1",
            "artifacts": [
                {
                    "logical_name": "fixture",
                    "url": "https://example.test/api-v2.jsonl",
                    "sha256": "a" * 64,
                    "bytes": 2,
                }
            ],
            "license": {
                "id": "test-only",
                "status": "approved-test-only",
                "evidence_urls": [],
                "commercial_use": "test-only",
                "redistribution": "test-only",
                "reviewed_at": None,
            },
            "converter": {"id": "api-v2", "version": "1", "config": {}},
            "synthetic": True,
        },
        "cases": cases,
        "profiles": [
            {
                "name": "smoke",
                "strategy": "fixed",
                "case_ids": [cases[1]["case_id"]],
                "dimensions": [],
                "seed": None,
            },
            {
                "name": "full",
                "strategy": "fixed",
                "case_ids": [case["case_id"] for case in cases],
                "dimensions": [],
                "seed": None,
            },
        ],
    }


def _client(store=None, resources=None):
    application = create_app(
        store or InMemoryRunStore(), resources if resources is not None else InMemoryResourceStore()
    )
    return TestClient(application), application


def _seeded(client, builtin="direct-llm-exact-answer"):
    response = client.post("/api/v1/benchmarks/direct-llm/import", json={"builtin": builtin})
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------ 内置样例


def test_builtins_endpoint_lists_the_shipped_samples():
    client, _ = _client()
    payload = client.get("/api/v1/benchmarks/direct-llm/builtins").json()
    assert [item["id"] for item in payload["items"]] == [
        "direct-llm-exact-answer",
        "direct-llm-classify",
        "direct-llm-json-extract",
    ]
    assert [item["cases"] for item in payload["items"]] == [8, 8, 7]
    assert all(item["importable"] for item in payload["items"])
    assert payload["total"] == 3


# ------------------------------------------------------------------ 导入


def test_import_builtin_then_overview_and_idempotency():
    client, _ = _client()
    receipt = _seeded(client, "direct-llm-classify")
    assert receipt["imported"] == "direct-llm-classify@1"
    assert receipt["scenario"] == "direct-llm-classify@1"
    assert receipt["suite"] == "direct-llm" and receipt["scorer"] == "contains"
    assert receipt["cases"] == 8 and receipt["source"] == "builtin:direct-llm-classify"
    # 同内容重复导入：复用版本，不越攒版本号
    assert _seeded(client, "direct-llm-classify") == receipt

    overview = client.get("/api/v1/benchmarks/direct-llm").json()
    assert overview["total"] == 1
    item = overview["items"][0]
    assert item["scenario"] == "direct-llm-classify@1"
    assert item["dataset"] == "direct-llm-classify@1"
    assert item["suite"] == "direct-llm" and item["cases"] == 8
    assert item["eval"]["scorer"] == "contains"
    assert item["contract_version"] == 1
    assert item["dataset_fingerprint"].startswith("sha256:")
    assert item["profiles"] == []
    assert item["provenance"]["source"] == "builtin:direct-llm-classify"
    assert item["runs"] == []


def test_overview_does_not_trust_conflicting_unvalidated_top_level_marker():
    resources = InMemoryResourceStore()
    client, _ = _client(resources=resources)
    receipt = _seeded(client, "direct-llm-classify")
    dataset_name, _, dataset_version = receipt["imported"].rpartition("@")

    rows = resources.datasets._rows  # type: ignore[attr-defined]
    rows[(dataset_name, dataset_version)]["contract_version"] = 2

    overview = client.get("/api/v1/benchmarks/direct-llm").json()
    assert overview == {"items": [], "total": 0}

    del rows[(dataset_name, dataset_version)]["contract_version"]
    scenario_rows = resources.scenarios._rows  # type: ignore[attr-defined]
    scenario_rows[(dataset_name, dataset_version)]["plugin_version"] = "2"
    overview = client.get("/api/v1/benchmarks/direct-llm").json()
    assert overview == {"items": [], "total": 0}


def test_overview_and_cases_are_version_aware_for_direct_llm_v2():
    from motte_sdk.direct_llm_v2 import (
        normalize_direct_llm_v2_dataset,
        persist_direct_llm_v2_dataset,
    )

    resources = InMemoryResourceStore()
    normalized = normalize_direct_llm_v2_dataset(_v2_dataset())
    persist_direct_llm_v2_dataset(normalized, resources, version="1")
    client, _ = _client(resources=resources)

    overview = client.get("/api/v1/benchmarks/direct-llm").json()
    assert overview["total"] == 1
    item = overview["items"][0]
    assert item["dataset"] == "api-direct-v2@1"
    assert item["contract_version"] == 2
    assert item["dataset_fingerprint"] == normalized["dataset_fingerprint"]
    assert item["eval"]["version"] == 2
    assert item["profiles"] == [
        {
            "name": profile["name"],
            "count": profile["count"],
            "strategy": profile["strategy"],
            "case_ids_sha256": profile["case_ids_sha256"],
        }
        for profile in normalized["profiles"]
    ]
    assert all("case_ids" not in profile for profile in item["profiles"])
    cases = client.get(
        "/api/v1/benchmarks/direct-llm/cases", params={"dataset": "api-direct-v2@1"}
    ).json()
    assert cases["dataset_total"] == 2
    assert [item["scorer"] for item in cases["items"]] == ["numeric@1", "numeric@1"]


def test_import_pasted_jsonl_uses_defaults_and_custom_scorer():
    client, _ = _client()
    response = client.post(
        "/api/v1/benchmarks/direct-llm/import",
        json={"content": CUSTOM, "name": "custom-set", "scorer": "contains"},
    )
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["imported"] == "custom-set@1" and receipt["cases"] == 3
    assert receipt["scorer"] == "contains" and receipt["source"] == "local-jsonl"
    cases = client.get(
        "/api/v1/benchmarks/direct-llm/cases", params={"dataset": "custom-set@1"}
    ).json()
    assert [item["case_id"] for item in cases["items"]] == [
        "custom-set-0000",
        "custom-set-0001",
        "custom-set-0002",
    ]
    assert [item["scorer"] for item in cases["items"]] == ["contains"] * 3
    assert cases["items"][2]["expected"] is None


def test_import_needs_name_when_content_is_anonymous():
    client, _ = _client()
    response = client.post("/api/v1/benchmarks/direct-llm/import", json={"content": CUSTOM})
    assert response.status_code == 201, response.text
    assert response.json()["imported"] == "direct-llm-custom@1"


def test_import_defaults_license_and_rejects_bad_requests():
    client, _ = _client()
    assert client.post("/api/v1/benchmarks/direct-llm/import", json={}).status_code == 422
    both = client.post(
        "/api/v1/benchmarks/direct-llm/import",
        json={"content": CUSTOM, "builtin": "direct-llm-classify"},
    )
    assert both.status_code == 422 and "exactly one" in both.json()["error"]["message"]
    assert (
        client.post("/api/v1/benchmarks/direct-llm/import", json={"content": 3}).status_code == 422
    )
    assert (
        client.post(
            "/api/v1/benchmarks/direct-llm/import", json={"content": CUSTOM, "scorer": "fuzzy"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/benchmarks/direct-llm/import", json={"content": "not json\n"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/benchmarks/direct-llm/import", json={"content": CUSTOM, "license": ""}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/benchmarks/direct-llm/import", json={"content": CUSTOM, "api_key": "sk-x"}
        ).status_code
        == 422
    )
    unknown = client.post("/api/v1/benchmarks/direct-llm/import", json={"builtin": "nope"})
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "BUILTIN_UNAVAILABLE"


def test_import_reports_version_conflict_as_409():
    client, _ = _client()
    body = {"content": CUSTOM, "name": "custom-set", "version": "1"}
    assert client.post("/api/v1/benchmarks/direct-llm/import", json=body).status_code == 201
    changed = json.dumps({"input": "changed", "expected": "9"}) + "\n"
    conflict = client.post(
        "/api/v1/benchmarks/direct-llm/import", json={**body, "content": changed}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "RESOURCE_CONFLICT"


# ------------------------------------------------------------------ 题目浏览


def test_cases_pagination_search_and_missing_dataset():
    client, _ = _client()
    _seeded(client)
    first = client.get(
        "/api/v1/benchmarks/direct-llm/cases",
        params={"dataset": "direct-llm-exact-answer@1", "limit": 3},
    ).json()
    assert first["total"] == 8 and first["dataset_total"] == 8 and first["limit"] == 3
    assert [item["source_line"] for item in first["items"]] == [1, 2, 3]
    assert first["items"][0]["input"].startswith("中国的首都")
    second = client.get(
        "/api/v1/benchmarks/direct-llm/cases",
        params={"dataset": "direct-llm-exact-answer@1", "offset": 6, "limit": 5},
    ).json()
    assert [item["case_id"] for item in second["items"]] == [
        "direct-llm-exact-answer-0006",
        "direct-llm-exact-answer-0007",
    ]
    searched = client.get(
        "/api/v1/benchmarks/direct-llm/cases",
        params={"dataset": "direct-llm-exact-answer@1", "query": "0003"},
    ).json()
    assert (
        searched["total"] == 1 and searched["items"][0]["case_id"] == "direct-llm-exact-answer-0003"
    )
    missing = client.get("/api/v1/benchmarks/direct-llm/cases", params={"dataset": "nope@1"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "DATASET_NOT_FOUND"


# ------------------------------------------------------------------ 发起跑测


def _model(client, model_id="probe", *, context_window=None):
    client.post(
        "/api/v1/providers",
        json={"name": "local", "kind": "openai_compatible", "base_url": "https://local.test/v1"},
    )
    profile = {
        "id": model_id,
        "provider": "local",
        "model": "probe-1",
        "capabilities": {},
        "max_output_tokens": 8192,
    }
    if context_window is not None:
        profile["context_window"] = context_window
    created = client.post("/api/v1/models", json=profile)
    assert created.status_code == 201
    assert client.post(f"/api/v1/models/{model_id}/publish").status_code == 200
    return model_id


def test_run_creation_projects_cases_and_records_the_budget():
    client, _ = _client()
    _seeded(client)
    model = _model(client)
    response = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "direct-llm-exact-answer@1",
            "parameters": {"temperature": 0.3, "max_output_tokens": 256},
            "case_selection": {"mode": "random", "count": 3, "seed": "deadbeef"},
        },
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert len(run["case_ids"]) == 3 and run["status"] == "queued"
    manifest = run["manifest"]
    assert manifest["provider"]["parameters"]["temperature"] == 0.3
    assert manifest["provider"]["parameters"]["max_output_tokens"] == 256
    assert manifest["provider"]["max_retries"] == 0
    assert set(manifest["cases"]) == set(run["case_ids"])
    provenance = manifest["benchmark_provenance"]
    assert provenance["suite"] == "direct-llm" and provenance["scorer"] == "exact"
    assert provenance["max_output_tokens"] == 256
    assert provenance["run_selection"] == {"mode": "random", "count": 3, "seed": "deadbeef"}


def test_run_creation_validates_scenario_model_and_selection():
    client, _ = _client()
    _seeded(client)
    model = _model(client)
    assert client.post("/api/v1/benchmarks/direct-llm/runs", json={}).status_code == 422
    missing_scenario = client.post(
        "/api/v1/benchmarks/direct-llm/runs", json={"model": model, "scenario": "direct-llm-nope@1"}
    )
    assert missing_scenario.status_code == 422
    assert missing_scenario.json()["error"]["code"] == "SCENARIO_NOT_FOUND"
    assert (
        client.post(
            "/api/v1/benchmarks/direct-llm/runs", json={"scenario": "direct-llm-exact-answer@1"}
        ).status_code
        == 422
    )
    bad_id = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "direct-llm-exact-answer@1",
            "case_selection": {"mode": "ids", "case_ids": ["nope"]},
        },
    )
    assert bad_id.status_code == 422 and "not in dataset" in bad_id.json()["error"]["message"]
    bad_budget = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "direct-llm-exact-answer@1",
            "parameters": {"max_output_tokens": 0},
        },
    )
    assert bad_budget.status_code == 422
    bad_level = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={"model": model, "scenario": "direct-llm-exact-answer@1", "reasoning_level": ""},
    )
    assert bad_level.status_code == 422
    bad_parameters = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={"model": model, "scenario": "direct-llm-exact-answer@1", "parameters": 7},
    )
    assert bad_parameters.status_code == 422
    assert (
        client.post(
            "/api/v1/benchmarks/direct-llm/runs",
            json={"model": model, "scenario": "direct-llm-exact-answer@1", "api_key": "sk-x"},
        ).status_code
        == 422
    )


def test_run_creation_derives_scenario_from_dataset_name_and_version():
    client, _ = _client()
    _seeded(client)
    model = _model(client)
    run = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={"model": model, "dataset_name": "direct-llm-exact-answer", "dataset_version": "1"},
    ).json()
    assert run["scenario_version"] == "direct-llm-exact-answer@1"
    assert len(run["case_ids"]) == 8


def _seed_v2(resources):
    from motte_contracts.direct_llm_v2 import scenario_for_v2
    from motte_sdk.direct_llm_v2 import (
        normalize_direct_llm_v2_dataset,
        persist_direct_llm_v2_dataset,
    )
    from motte_sdk.publication import publication_audit

    dataset = normalize_direct_llm_v2_dataset(_v2_dataset())
    scenario = scenario_for_v2(dataset, version="1")
    audit = publication_audit(
        dataset,
        scenario,
        {"source_id": "api-v2", "dataset_fingerprint": dataset["dataset_fingerprint"]},
        actor="pytest",
        entrypoint="api-test",
        published_at="2026-09-19T00:00:00Z",
    )
    return persist_direct_llm_v2_dataset(
        dataset, resources, version="1", publication=audit,
    )


def test_generic_resource_posts_reject_standalone_v2_but_keep_v1():
    from motte_contracts.direct_llm import import_direct_llm_jsonl, scenario_for
    from motte_contracts.direct_llm_v2 import scenario_for_v2
    from motte_sdk.direct_llm_v2 import normalize_direct_llm_v2_dataset

    resources = InMemoryResourceStore()
    client, _ = _client(resources=resources)
    v2 = normalize_direct_llm_v2_dataset(_v2_dataset())
    v2_scenario = scenario_for_v2(v2, version="1")
    for route, record in (("datasets", v2), ("scenarios", v2_scenario)):
        response = client.post(f"/api/v1/{route}", json=record)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "ATOMIC_PUBLICATION_REQUIRED"
    assert resources.datasets.list() == [] and resources.scenarios.list() == []

    v1 = import_direct_llm_jsonl(
        b'{"input":"q","expected":"a"}\n',
        name="api-v1",
        version="1",
        license_id="test-only",
        synthetic=True,
    )
    assert client.post("/api/v1/datasets", json=v1).status_code == 201
    assert client.post("/api/v1/scenarios", json=scenario_for(v1, version="1")).status_code == 201


def test_v2_run_creation_requires_matching_publication_for_both_entrypoints():
    from motte_contracts.direct_llm_v2 import scenario_for_v2
    from motte_sdk.direct_llm_v2 import normalize_direct_llm_v2_dataset
    from motte_sdk.publication import publication_audit

    resources = InMemoryResourceStore()
    dataset = normalize_direct_llm_v2_dataset(_v2_dataset())
    scenario = scenario_for_v2(dataset, version="1")
    resources.datasets.put(dataset)
    resources.scenarios.put(scenario)
    client, _ = _client(resources=resources)
    model = _model(client)

    direct = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "api-direct-v2@1",
            "case_selection": {"mode": "profile", "profile": "smoke"},
        },
    )
    generic = client.post(
        "/api/v1/runs",
        json={
            "scenario_version": "api-direct-v2@1",
            "manifest": {
                "model": model,
                "case_selection": {"mode": "profile", "profile": "smoke"},
            },
        },
    )
    for response in (direct, generic):
        assert response.status_code == 422
        assert "publication audit" in response.json()["error"]["message"]

    unrelated_dataset = {**dataset, "name": "unrelated-v2"}
    unrelated_scenario = {"name": "unrelated-v2", "version": "1"}
    unrelated_audit = publication_audit(
        unrelated_dataset, unrelated_scenario, {"fixture": "unrelated"},
        actor="pytest", entrypoint="api-test", published_at="2026-09-19T00:00:00Z",
    )
    resources.publications.put(unrelated_audit)
    still_blocked = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={"model": model, "scenario": "api-direct-v2@1"},
    )
    assert still_blocked.status_code == 422
    assert "publication audit" in still_blocked.json()["error"]["message"]
    assert client.get("/api/v1/runs").json()["total"] == 0


def test_v2_profile_run_uses_fixed_ids_and_profiles_fail_closed():
    resources = InMemoryResourceStore()
    _seed_v2(resources)
    client, _ = _client(resources=resources)
    model = _model(client)

    response = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "api-direct-v2@1",
            "case_selection": {"mode": "profile", "profile": "smoke"},
        },
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["case_ids"] == ["v2-case-1"]
    assert run["manifest"]["benchmark_provenance"]["plugin_version"] == "2"
    assert run["manifest"]["benchmark_snapshot"]["selection"]["mode"] == "profile"
    assert set(run["manifest"]["cases"]["v2-case-1"]) == {"case_id", "prompt"}

    unknown = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "api-direct-v2@1",
            "case_selection": {"mode": "profile", "profile": "missing"},
        },
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "RUN_CONFIG_INVALID"

    _seeded(client)
    v1_profile = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "direct-llm-exact-answer@1",
            "case_selection": {"mode": "profile", "profile": "smoke"},
        },
    )
    assert v1_profile.status_code == 422
    assert v1_profile.json()["error"]["code"] == "RUN_CONFIG_INVALID"


def test_v2_profile_stale_retry_rebuilds_only_the_profile_request():
    resources = InMemoryResourceStore()
    _seed_v2(resources)
    client, application = _client(resources=resources)
    model = _model(client)
    parent = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "api-direct-v2@1",
            "case_selection": {"mode": "profile", "profile": "smoke"},
        },
    ).json()
    assert parent["case_ids"] == ["v2-case-1"]

    assert (
        client.put(
            "/api/v1/providers/local",
            json={
                "base_url": "https://refreshed.test/v1",
            },
        ).status_code
        == 200
    )
    application.state.run_service.mark_profile_stale(
        parent["id"],
        reason="provider generation changed",
    )
    response = client.post(f"/api/v1/runs/{parent['id']}/retry")
    assert response.status_code == 200, response.text
    child = response.json()
    assert child["case_ids"] == parent["case_ids"]
    assert child["requested_manifest"]["case_selection"] == {
        "mode": "profile",
        "profile": "smoke",
    }
    assert child["manifest"]["case_selection"]["mode"] == "profile"
    assert child["manifest"]["case_selection"]["profile"] == "smoke"


def test_v2_normal_retry_rechecks_current_source_without_rebuilding_snapshot(monkeypatch):
    import motte_sdk.dataset_sources as source_registry

    resources = InMemoryResourceStore()
    _seed_v2(resources)
    client, _ = _client(resources=resources)
    model = _model(client)
    parent = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "api-direct-v2@1",
            "case_selection": {"mode": "profile", "profile": "smoke"},
        },
    ).json()
    assert client.post(f"/api/v1/runs/{parent['id']}/cancel").status_code == 200

    pending = source_registry.load_registry()["ifeval"].model_copy(update={"id": "api-v2"})
    monkeypatch.setattr(source_registry, "load_registry", lambda: {"api-v2": pending})
    blocked = client.post(f"/api/v1/runs/{parent['id']}/retry")
    assert blocked.status_code == 409
    assert "SOURCE_LICENSE_BLOCKED" in blocked.json()["detail"]
    assert client.get("/api/v1/runs").json()["total"] == 1

    monkeypatch.setattr(source_registry, "load_registry", lambda: {})
    response = client.post(f"/api/v1/runs/{parent['id']}/retry")
    assert response.status_code == 200, response.text
    child = response.json()
    assert child["parent_run_id"] == parent["id"]
    assert child["case_ids"] == parent["case_ids"]
    assert child["manifest"]["benchmark_snapshot"] == parent["manifest"]["benchmark_snapshot"]
    assert child["manifest"]["cases"] == parent["manifest"]["cases"]


def test_direct_run_request_rejects_extra_fields_and_mixed_selection_shapes():
    client, _ = _client()
    _seeded(client)
    model = _model(client)
    extra = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "direct-llm-exact-answer@1",
            "unexpected": True,
        },
    )
    assert extra.status_code == 422
    mixed = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "direct-llm-exact-answer@1",
            "case_selection": {"mode": "profile", "profile": "smoke", "case_ids": ["x"]},
        },
    )
    assert mixed.status_code == 422


def test_v2_dry_run_is_side_effect_free_and_reports_context_bounds():
    from motte_contracts.identity import canonical_sha256

    resources = InMemoryResourceStore()
    _seed_v2(resources)
    resources.price_tables.put(
        {
            "model_id": "probe",
            "version": "pricing-v1",
            "currency": "USD",
            "input_per_million": 0.5,
            "output_per_million": 2.0,
        }
    )
    client, application = _client(resources=resources)
    model = _model(client, context_window=128)
    application.state.run_service.create_run = lambda *args, **kwargs: pytest.fail(
        "dry-run must not create a run"
    )

    response = client.post(
        "/api/v1/benchmarks/direct-llm/dry-run",
        json={
            "model": model,
            "scenario": "api-direct-v2@1",
            "case_selection": {"mode": "profile", "profile": "full"},
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["scenario"] == payload["dataset"] == "api-direct-v2@1"
    assert payload["contract_version"] == 2 and payload["plugin_version"] == "2"
    assert payload["selected_count"] == 2 and payload["profile"] == "full"
    assert payload["case_ids_sha256"] == canonical_sha256(["v2-case-0", "v2-case-1"])
    assert payload["max_output_tokens"] == 32
    assert payload["max_total_tokens_upper_bound"] == (
        payload["max_input_tokens_upper_bound"] + payload["max_output_tokens"]
    )
    assert payload["context_window"] == 128
    assert payload["estimation_method"] == "utf8-bytes-plus-structure-v1"
    assert payload["estimated_cost_upper_bound"] == pytest.approx(
        payload["selected_count"]
        * (payload["max_input_tokens_upper_bound"] * 0.5 + 32 * 2.0)
        / 1_000_000
    )
    assert payload["price_table_version"] == "pricing-v1"
    assert payload["currency"] == "USD"
    assert payload["estimated"] is True
    assert client.get("/api/v1/runs").json()["total"] == 0


def test_dry_run_without_context_is_nullable_and_over_limit_fails_before_create():
    resources = InMemoryResourceStore()
    _seed_v2(resources)
    client, _ = _client(resources=resources)
    without_context = _model(client, "without-context")
    response = client.post(
        "/api/v1/benchmarks/direct-llm/dry-run",
        json={
            "model": without_context,
            "scenario": "api-direct-v2@1",
            "case_selection": {"mode": "profile", "profile": "smoke"},
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["max_output_tokens"] == 32
    assert (
        payload["max_input_tokens_upper_bound"],
        payload["max_total_tokens_upper_bound"],
        payload["context_window"],
        payload["estimation_method"],
    ) == (None, None, None, None)
    assert (
        payload["estimated_cost_upper_bound"],
        payload["price_table_version"],
        payload["currency"],
    ) == (None, None, None)

    tiny = _model(client, "tiny-context", context_window=90)
    rejected = client.post(
        "/api/v1/benchmarks/direct-llm/dry-run",
        json={
            "model": tiny,
            "scenario": "api-direct-v2@1",
            "case_selection": {"mode": "profile", "profile": "smoke"},
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "CONTEXT_WINDOW_EXCEEDED"
    assert client.get("/api/v1/runs").json()["total"] == 0


def test_resource_publications_are_typed_and_read_only():
    from motte_sdk.publication import publication_audit

    resources = InMemoryResourceStore()
    dataset = {
        "name": "api-direct-v2", "version": "1",
        "dataset_fingerprint": "sha256:" + "a" * 64,
    }
    scenario = {"name": "api-direct-v2", "version": "1"}
    record = publication_audit(
        dataset, scenario, {"source_id": "api-v2"},
        actor="api-test", entrypoint="pytest", published_at="2026-09-19T00:00:00Z",
    )
    resources.publications.put(record)
    client, _ = _client(resources=resources)
    response = client.get("/api/v1/resource-publications")
    assert response.status_code == 200
    assert response.json() == {"items": [record], "total": 1}
    assert client.post("/api/v1/resource-publications", json=record).status_code == 405
    assert client.delete(f"/api/v1/resource-publications/{record['id']}").status_code in {404, 405}


# ------------------------------------------------------------------ 报告与准确率


def _execute(client, application, run_id, answers):
    service = application.state.run_service
    return service.execute(
        run_id,
        provider=lambda case_id: {
            "content": answers.get(case_id, "?"),
            "usage": {"total_tokens": 7},
            "cost": {"total": 0.001, "price_table_version": "v1"},
        },
    )


def test_report_uses_judged_denominator_and_reports_usage():
    client, application = _client()
    _seeded(client)
    model = _model(client)
    run = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={"model": model, "scenario": "direct-llm-exact-answer@1"},
    ).json()
    answers = {"direct-llm-exact-answer-0000": "北京", "direct-llm-exact-answer-0001": "wrong"}
    result = _execute(client, application, run["id"], answers)
    assert result["status"] == "completed"
    scores = result["scores"]
    assert [score["outcome"] for score in scores[:3]] == ["correct", "wrong_answer", "wrong_answer"]
    assert all(score["judged"] for score in scores)

    report = client.get(f"/api/v1/runs/{run['id']}/report").json()
    summary = report["summary"]
    assert summary["cases"] == 8 and summary["scored"] == 8 and summary["passed"] == 1
    assert summary["failed"] == 7 and summary["judged"] == 8
    assert summary["denominator"] == "judged_cases"
    assert summary["pass_rate"] == 0.125 and summary["scorer_version"] == "direct-llm-answer-v1"
    assert report["cost"] == {
        "total": 0.008,
        "price_table_versions": ["v1"],
        "known_cases": 8,
        "unknown_cases": 0,
    }
    assert report["usage"] == {"total_tokens": 56}
    assert report["benchmark"]["suite"] == "direct-llm"
    assert report["scores"][0]["case_id"] == "direct-llm-exact-answer-0000"


def test_overview_and_report_share_judged_semantics_for_unexpected_call_failure():
    client, application = _client()
    imported = client.post(
        "/api/v1/benchmarks/direct-llm/import", json={"content": CUSTOM, "name": "mixed-set"}
    )
    assert imported.status_code == 201, imported.text
    model = _model(client)
    run = client.post(
        "/api/v1/benchmarks/direct-llm/runs", json={"model": model, "scenario": "mixed-set@1"}
    ).json()

    def provider(case_id):
        if case_id == "mixed-set-0000":
            return {"content": "1"}
        return {"error": {"class": "timeout", "message": "slow"}}

    result = application.state.run_service.execute(run["id"], provider=provider)
    assert result["status"] == "failed"  # 暂态错误继续逐题评分，但运行终态保留失败事实。
    assert [(score["outcome"], score["judged"]) for score in result["scores"]] == [
        ("correct", True),
        ("call_failed", True),
        ("call_failed", False),
    ]

    report = client.get(f"/api/v1/runs/{run['id']}/report").json()
    summary = report["summary"]
    aggregate = summary["aggregate"]
    assert aggregate["selected"] == 3 and aggregate["judged"] == 2
    assert aggregate["correct"] == 1 and aggregate["call_failed"] == 2
    assert aggregate["accuracy"] == pytest.approx(0.5)
    # v1 keeps its historical judged denominator for execution-rate fields.
    assert aggregate["completion"] == pytest.approx(1 / 2)
    assert aggregate["attempt_rate"] == pytest.approx(3 / 2)
    assert summary["judged"] == aggregate["judged"]
    assert summary["accuracy"] == summary["pass_rate"] == aggregate["accuracy"]
    assert summary["failed"] == 2 and "unjudged" not in summary

    overview = client.get("/api/v1/benchmarks/direct-llm").json()
    overview_run = overview["items"][0]["runs"][0]
    assert overview_run["id"] == run["id"]
    assert overview_run["accuracy"] == aggregate["accuracy"]


def test_v2_report_separates_judged_failures_from_unjudged_cases():
    resources = InMemoryResourceStore()
    _seed_v2(resources)
    client, application = _client(resources=resources)
    model = _model(client)
    run = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "api-direct-v2@1",
            "case_selection": {"mode": "profile", "profile": "full"},
        },
    ).json()

    result = application.state.run_service.execute(
        run["id"],
        provider=lambda case_id: (
            {"content": "[ANSWER:0]"}
            if case_id == "v2-case-0"
            else {"error": {"class": "timeout", "message": "slow"}}
        ),
    )
    assert result["status"] == "failed"
    report = client.get(f"/api/v1/runs/{run['id']}/report").json()
    summary = report["summary"]
    assert summary["aggregate"]["selected"] == 2
    assert summary["aggregate"]["judged"] == summary["aggregate"]["correct"] == 1
    assert summary["failed"] == 0
    assert summary["unjudged"] == 1


def test_overview_reports_accuracy_of_completed_runs():
    client, application = _client()
    _seeded(client)
    model = _model(client)
    run = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "direct-llm-exact-answer@1",
            "case_selection": {
                "mode": "ids",
                "case_ids": ["direct-llm-exact-answer-0000", "direct-llm-exact-answer-0001"],
            },
        },
    ).json()
    _execute(client, application, run["id"], {"direct-llm-exact-answer-0000": "北京"})
    overview = client.get("/api/v1/benchmarks/direct-llm").json()
    entries = overview["items"][0]["runs"]
    assert [entry["id"] for entry in entries] == [run["id"]]
    assert entries[0]["accuracy"] == 0.5 and entries[0]["status"] == "completed"


def test_direct_llm_accuracy_helper_denominators():
    scores = [
        {"case_id": "a", "outcome": "correct", "judged": True},
        {"case_id": "b", "outcome": "wrong_answer", "judged": True},
        {"case_id": "c", "outcome": "no_expectation", "judged": False},
    ]
    run = {"manifest": {"benchmark_provenance": {"selected_count": 3}}}
    assert _direct_llm_accuracy(run, scores) == 0.5
    # 分数条数对不上选中题数（运行未跑完）时不给结论
    assert _direct_llm_accuracy(run, scores[:2]) is None
    assert _direct_llm_accuracy({"manifest": {}}, scores) is None
    assert (
        _direct_llm_accuracy(
            {"manifest": {"benchmark_provenance": {"selected_count": 1}}},
            [{"case_id": "c", "outcome": "no_expectation", "judged": False}],
        )
        is None
    )
    assert (
        _direct_llm_accuracy(
            {"manifest": {"benchmark_provenance": {"selected_count": 2}}},
            [
                {"case_id": "a", "outcome": "correct", "judged": True},
                {"case_id": "b", "outcome": "wrong_answer", "judged": True},
            ],
        )
        == 0.5
    )
    # 运行级子集优先于数据集级题数
    assert (
        _direct_llm_accuracy(
            {
                "manifest": {
                    "benchmark_provenance": {"selected_count": 8, "run_selection": {"count": 2}}
                }
            },
            [
                {"case_id": "a", "outcome": "correct", "judged": True},
                {"case_id": "b", "outcome": "correct", "judged": True},
            ],
        )
        == 1.0
    )


def test_rescore_reproduces_scores_without_new_calls():
    client, application = _client()
    _seeded(client)
    model = _model(client)
    run = client.post(
        "/api/v1/benchmarks/direct-llm/runs",
        json={
            "model": model,
            "scenario": "direct-llm-exact-answer@1",
            "case_selection": {"mode": "ids", "case_ids": ["direct-llm-exact-answer-0000"]},
        },
    ).json()
    original = _execute(client, application, run["id"], {"direct-llm-exact-answer-0000": "北京"})
    rescored = client.post(f"/api/v1/runs/{run['id']}/rescore").json()
    assert rescored["scores"] == original["scores"]
