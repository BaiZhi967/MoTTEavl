"""Offline managed-source conversion, import, and prepare orchestration."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from motte_contracts.dataset_sources import SourceSpec
from motte_contracts.direct_llm_v2 import (
    CONTRACT_VERSION,
    DATASET_ID,
    DATASET_VERSION,
    SELECTION_ALL,
    SUITE,
    case_ids_sha256,
    converter_config_sha256,
    scorer_config_sha256,
)
from motte_contracts.identity import canonical_sha256
from motte_sdk.dataset_sources import artifact_cache_path
from motte_sdk.direct_llm_v2 import (
    normalize_direct_llm_v2_dataset,
    persist_direct_llm_v2_dataset,
    resolve_direct_llm_v2_manifest,
)
from motte_sdk.managed_sources import (
    ConversionResult,
    ConverterRegistry,
    ManagedSourceError,
    convert_cached_source,
    import_cached_source,
    prepare_source,
)
from motte_storage.resource_store import InMemoryResourceStore, ResourceConflictError

REVISION = "a" * 40
BODY = b'{"id": 1}\n'
ARTIFACT_SHA = hashlib.sha256(BODY).hexdigest()


def _source(status: str = "pending") -> SourceSpec:
    approved = status == "approved"
    scope = "public" if approved else ("restricted" if status == "restricted" else "blocked")
    return SourceSpec.model_validate(
        {
            "schema_version": 1,
            "id": "fixture-source",
            "label": "Fixture source",
            "description": "Original test source",
            "tier": "managed-public",
            "governance": {
                "status": status,
                "distribution_scope": scope,
                "stable_eligible": approved,
                "reviewer": "reviewer" if approved else None,
                "reviewed_at": "2026-09-19" if approved else None,
                "decision_notes": "fixture governance",
            },
            "links": {
                "homepage": "https://example.test/source",
                "repository": "https://example.test/source",
                "dataset_card": None,
                "citation": None,
            },
            "upstream": {
                "revision": {"kind": "git-commit", "resolver": "fixture", "value": REVISION},
                "artifacts": [
                    {
                        "logical_name": "fixture.jsonl",
                        "url": "https://example.test/fixture.jsonl",
                        "format": "jsonl",
                        "sha256": ARTIFACT_SHA,
                        "bytes": len(BODY),
                        "max_bytes": 1024,
                        "required": True,
                    }
                ],
            },
            "license": {
                "data": {
                    "declared_ids": ["MIT"],
                    "verified_spdx": "MIT" if approved else None,
                    "evidence_urls": ["https://example.test/license"],
                },
                "code": {
                    "declared_ids": ["MIT"],
                    "verified_spdx": "MIT" if approved else None,
                    "evidence_urls": ["https://example.test/license"],
                },
                "commercial_use": "allowed" if approved else "unknown",
                "redistribution": "allowed" if approved else "unknown",
                "attribution": "required" if approved else "unknown",
                "share_alike": "none" if approved else "unknown",
                "review_notes": "fixture",
            },
            "conversion": {
                "converter": {"id": "fixture-converter", "version": "1"},
                "splits": ["test"],
                "default_split": "test",
                "prompt_version": "fixture-v1",
                "scorer": {"id": "exact", "version": "1"},
                "profiles": ["full"],
                "optional_dependency": None,
            },
            "safety": {
                "network_entrypoint": "cli-only",
                "allowed_protocols": ["https"],
                "trust_remote_code": False,
                "online_rows_fallback": False,
                "executable_upstream_code": False,
                "archive_auto_extract": False,
            },
            "official_comparability": {"status": "not-established", "notes": "fixture"},
            "blockers": [] if approved else ["fixture pending"],
        }
    )


def _dataset() -> dict[str, Any]:
    scorer_config = {"case_sensitive": True, "normalize_whitespace": False}
    case_ids = ["fixture-case-1"]
    cases = [
        {
            "case_id": case_ids[0],
            "input": "What is the fixture answer?",
            "expected": "ok",
            "metadata": {
                "source_line": 2,
                "source_id": "fixture-row-1",
                "language": "en",
                "subject": None,
                "category": "fixture",
                "difficulty": None,
                "split": "test",
                "tags": ["fixture"],
                "template_family": "fixture-v1",
            },
        }
    ]
    profiles = [{
        "name": "full",
        "strategy": "all-fixed-ids",
        "count": 1,
        "case_ids": case_ids,
        "case_ids_sha256": case_ids_sha256(case_ids),
        "dimensions": ["category"],
        "seed": None,
    }]
    artifacts = [{
        "logical_name": "fixture-input",
        "url": "https://example.test/fixture-input.jsonl",
        "sha256": ARTIFACT_SHA,
        "bytes": len(BODY),
    }]
    config = {"fixture": True}
    return {
        "name": "fixture-managed-direct",
        "version": "1",
        "contract_version": CONTRACT_VERSION,
        "eval": {
            "suite": SUITE,
            "id": DATASET_ID,
            "version": DATASET_VERSION,
            "selected_count": 1,
            "selection": SELECTION_ALL,
            "scorer": {
                "id": "exact",
                "version": "1",
                "config": scorer_config,
                "config_sha256": scorer_config_sha256(scorer_config),
            },
            "prompt_version": "fixture-v1",
            "max_output_tokens": 16,
            "max_retries": 0,
        },
        "provenance": {
            "source_id": "fixture-source",
            "source_kind": "managed-public",
            "homepage": "https://example.test/source",
            "upstream_revision": REVISION,
            "artifacts": artifacts,
            "artifact_manifest_sha256": canonical_sha256(artifacts),
            "license": {
                "id": "MIT",
                "status": "approved",
                "evidence_urls": ["https://example.test/license"],
                "commercial_use": "allowed",
                "redistribution": "allowed",
                "reviewed_at": "2026-09-19",
            },
            "converter": {
                "id": "fixture-converter",
                "version": "1",
                "config": config,
                "config_sha256": converter_config_sha256(config),
            },
            "synthetic": True,
        },
        "cases": cases,
        "profiles": profiles,
    }


def _write_cache(source: SourceSpec, root: Path) -> None:
    _write_artifact(source, root, "fixture.jsonl", BODY)


def _write_artifact(source: SourceSpec, root: Path, logical_name: str, body: bytes) -> None:
    path = artifact_cache_path(source, REVISION, logical_name, cache_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _converter(*, artifacts, source, revision, config):
    del artifacts, source, revision
    return ConversionResult(deepcopy(_dataset()), input_count=config.get("input_count", 1))


def _registry() -> ConverterRegistry:
    return ConverterRegistry({("fixture-converter", "1"): _converter})


class _Response:
    status = 200
    headers = {"Content-Length": str(len(BODY))}
    final_url = "https://example.test/fixture.jsonl"

    def __init__(self) -> None:
        self._remaining = BODY

    def read(self, size: int = -1) -> bytes:
        del size
        result, self._remaining = self._remaining, b""
        return result

    def close(self) -> None:
        return None


class _Transport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def open(self, url: str, *, timeout: float) -> _Response:
        del timeout
        self.calls.append(url)
        return _Response()


def _convert(source, root, *, allow=True, registry=None, config=None):
    _write_cache(source, root)
    return convert_cached_source(
        source.id,
        REVISION,
        {"input_count": 1, **(config or {})},
        allow,
        registry={source.id: source},
        cache_root=root,
        converter_registry=registry or _registry(),
    )


def test_lazy_missing_converter_is_structured_and_import_safe(tmp_path: Path):
    source = _source()
    _write_cache(source, tmp_path)
    with pytest.raises(ManagedSourceError) as error:
        convert_cached_source(
            source.id, REVISION, {"input_count": 1}, True,
            registry={source.id: source}, cache_root=tmp_path,
            converter_registry=ConverterRegistry(),
        )
    assert error.value.code == "CONVERTER_UNAVAILABLE"


def test_truthfulqa_lazy_builtin_wrapper_converts_a_real_csv_fixture(tmp_path: Path):
    body = (
        b"Question,Best Answer,Best Incorrect Answer,Category,Type\n"
        b"Which one,True answer,False answer,knowledge,fact\n"
    )
    payload = _source("approved").model_dump(mode="json")
    payload["id"] = "truthfulqa-fixture"
    artifact = payload["upstream"]["artifacts"][0]
    artifact.update({
        "logical_name": "TruthfulQA.csv",
        "format": "csv",
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "max_bytes": 4096,
        "url": "https://example.test/truthfulqa.csv",
    })
    payload["conversion"]["converter"] = {
        "id": "truthfulqa-binary-to-direct",
        "version": "1",
    }
    payload["conversion"]["profiles"] = ["smoke", "regression", "full"]
    source = SourceSpec.model_validate(payload)
    _write_artifact(source, tmp_path, "TruthfulQA.csv", body)
    config = {
        "expected_rows": 1,
        "profile_targets": {"smoke": 1, "regression": 1, "full": 1},
        "input_count": 1,
        "max_bytes": 4096,
        "timeout": 3.0,
    }
    receipt = convert_cached_source(
        source.id,
        REVISION,
        config,
        False,
        registry={source.id: source},
        cache_root=tmp_path,
    )
    assert receipt["governance"] == {
        "status": "approved",
        "allow_experimental": False,
        "publishable": True,
    }
    assert receipt["dataset"]["provenance"]["license"] == {
        "id": "MIT",
        "status": "approved",
        "evidence_urls": ["https://example.test/license"],
        "commercial_use": "allowed",
        "redistribution": "allowed",
        "reviewed_at": "2026-09-19",
    }
    assert receipt["converter"]["id"] == "truthfulqa-binary-to-direct"
    assert receipt["dataset"]["provenance"]["converter"]["config"]["license_adapter_id"] == "unknown"
    assert receipt["dataset"]["provenance"]["converter"]["config"]["license_authority_id"] == "MIT"
    assert receipt["dataset"]["provenance"]["converter"]["config"]["license_mapping"] == (
        "builtin-placeholder-to-source-declared-license"
    )
    assert receipt["dataset"]["provenance"]["converter"]["config"]["governance_status"] == "approved"
    assert len(receipt["dataset"]["cases"]) == 1
    resources = InMemoryResourceStore()
    published = import_cached_source(
        source.id,
        REVISION,
        {**config, "version": "1"},
        resources=resources,
        actor="release-test",
        registry={source.id: source},
        cache_root=tmp_path,
    )
    assert published["publication_id"] == published["publication_audit"]["id"]
    assert len(resources.datasets.list()) == 1
    assert len(resources.scenarios.list()) == 1
    assert len(resources.publications.list()) == 1
    with pytest.raises(ManagedSourceError) as unsupported:
        convert_cached_source(
            source.id,
            REVISION,
            {"expected_rows": 1, "split": "validation"},
            True,
            registry={source.id: source},
            cache_root=tmp_path,
        )
    assert unsupported.value.code == "SOURCE_CONFIG_INVALID"

    bad_payload = deepcopy(payload)
    bad_payload["id"] = "truthfulqa-version-fixture"
    bad_payload["conversion"]["converter"]["version"] = "999"
    bad_source = SourceSpec.model_validate(bad_payload)
    _write_artifact(bad_source, tmp_path, "TruthfulQA.csv", body)
    with pytest.raises(ManagedSourceError) as unavailable:
        convert_cached_source(
            bad_source.id,
            REVISION,
            {"expected_rows": 1, "profile_targets": {"smoke": 1, "regression": 1, "full": 1}},
            True,
            registry={bad_source.id: bad_source},
            cache_root=tmp_path,
        )
    assert unavailable.value.code == "CONVERTER_UNAVAILABLE"


def test_adapter_license_id_tampering_and_incomplete_source_fail_closed(tmp_path: Path):
    body = (
        b"Question,Best Answer,Best Incorrect Answer,Category,Type\n"
        b"Which one,True answer,False answer,knowledge,fact\n"
    )
    payload = _source("approved").model_dump(mode="json")
    payload["id"] = "truthfulqa-license-fixture"
    artifact = payload["upstream"]["artifacts"][0]
    artifact.update({
        "logical_name": "TruthfulQA.csv",
        "format": "csv",
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "max_bytes": 4096,
        "url": "https://example.test/truthfulqa-license.csv",
    })
    payload["conversion"]["converter"] = {
        "id": "truthfulqa-binary-to-direct",
        "version": "1",
    }
    source = SourceSpec.model_validate(payload)
    _write_artifact(source, tmp_path, "TruthfulQA.csv", body)
    valid = convert_cached_source(
        source.id,
        REVISION,
        {"expected_rows": 1, "profile_targets": {"smoke": 1, "regression": 1, "full": 1}},
        False,
        registry={source.id: source},
        cache_root=tmp_path,
    )

    def tampered_converter(**kwargs):
        del kwargs
        result = deepcopy(valid["dataset"])
        result["provenance"]["license"]["id"] = "MIT-tampered"
        return result

    with pytest.raises(ManagedSourceError) as tampered:
        convert_cached_source(
            source.id,
            REVISION,
            {"input_count": 1},
            False,
            registry={source.id: source},
            cache_root=tmp_path,
            converter_registry=ConverterRegistry({("truthfulqa-binary-to-direct", "1"): tampered_converter}),
        )
    assert tampered.value.code == "SOURCE_LICENSE_INVALID"

    incomplete_payload = deepcopy(payload)
    incomplete_payload["id"] = "truthfulqa-incomplete-license"
    incomplete_payload["license"]["data"]["declared_ids"] = []
    incomplete_source = SourceSpec.model_validate(incomplete_payload)
    _write_artifact(incomplete_source, tmp_path, "TruthfulQA.csv", body)
    with pytest.raises(ManagedSourceError) as incomplete:
        convert_cached_source(
            incomplete_source.id,
            REVISION,
            {"expected_rows": 1, "profile_targets": {"smoke": 1, "regression": 1, "full": 1}},
            True,
            registry={incomplete_source.id: incomplete_source},
            cache_root=tmp_path,
        )
    assert incomplete.value.code == "SOURCE_LICENSE_INVALID"


def test_mmlu_lazy_builtin_wrapper_uses_named_splits_and_runner_evidence(tmp_path: Path):
    runner_revision = "b" * 40
    row = {
        "question_id": 1,
        "question": "Which option is correct?",
        "options": ["one", "two"],
        "answer": "A",
        "answer_index": 0,
        "cot_content": "Because one.",
        "category": "biology",
        "src": "fixture",
    }
    test_body = json.dumps([row]).encode()
    validation_row = {**row, "question_id": 2}
    validation_body = json.dumps([validation_row]).encode()
    runner_body = b"{}"
    payload = _source("pending").model_dump(mode="json")
    payload["id"] = "mmlu-fixture"
    payload["conversion"]["converter"] = {
        "id": "mmlu-pro-5shot-cot-to-direct",
        "version": "1",
    }
    payload["conversion"]["profiles"] = ["smoke", "regression", "full"]
    payload["upstream"]["artifacts"] = [
        {
            "logical_name": "test.json",
            "url": f"https://example.test/dataset/{REVISION}/test.json",
            "format": "json",
            "sha256": hashlib.sha256(test_body).hexdigest(),
            "bytes": len(test_body),
            "max_bytes": 4096,
            "required": True,
        },
        {
            "logical_name": "validation.json",
            "url": f"https://example.test/dataset/{REVISION}/validation.json",
            "format": "json",
            "sha256": hashlib.sha256(validation_body).hexdigest(),
            "bytes": len(validation_body),
            "max_bytes": 4096,
            "required": True,
        },
        {
            "logical_name": "runner.json",
            "url": f"https://example.test/runner/{runner_revision}/runner.json",
            "format": "json",
            "sha256": hashlib.sha256(runner_body).hexdigest(),
            "bytes": len(runner_body),
            "max_bytes": 4096,
            "required": True,
        },
    ]
    source = SourceSpec.model_validate(payload)
    _write_artifact(source, tmp_path, "test.json", test_body)
    _write_artifact(source, tmp_path, "validation.json", validation_body)
    _write_artifact(source, tmp_path, "runner.json", runner_body)
    receipt = convert_cached_source(
        source.id,
        REVISION,
        {
            "runner_revision": runner_revision,
            "expected_test_rows": 1,
            "expected_validation_rows": 1,
            "expected_categories": 1,
            "expected_category_names": ["biology"],
            "demonstrations_per_category": 1,
            "profile_targets": {"smoke": 1, "regression": 1, "full": 1},
            "max_bytes": 4096,
            "timeout": 3.0,
        },
        True,
        registry={source.id: source},
        cache_root=tmp_path,
    )
    assert receipt["converter"]["id"] == "mmlu-pro-5shot-cot-to-direct"
    assert len(receipt["dataset"]["cases"]) == 1


def test_pending_source_only_converts_experimentally_and_receipt_is_not_publishable(tmp_path: Path):
    source = _source("pending")
    with pytest.raises(ManagedSourceError) as blocked:
        _convert(source, tmp_path, allow=False)
    assert blocked.value.code == "SOURCE_LICENSE_BLOCKED"
    receipt = _convert(source, tmp_path)
    assert receipt["governance"]["publishable"] is False
    assert receipt["rejected"] == 0
    assert receipt["dataset_fingerprint"] == receipt["dataset"]["dataset_fingerprint"]


def test_approved_blockers_and_internal_sources_are_not_publishable(tmp_path: Path):
    blocked_payload = _source("approved").model_dump(mode="json")
    blocked_payload["id"] = "approved-with-blocker"
    blocked_payload["blockers"] = ["new evidence required"]
    blocked_source = SourceSpec.model_validate(blocked_payload)
    converter_calls: list[str] = []

    def should_not_convert(**kwargs):
        converter_calls.append("called")
        del kwargs
        return _dataset()

    with pytest.raises(ManagedSourceError) as early:
        convert_cached_source(
            blocked_source.id,
            REVISION,
            {"input_count": 1},
            False,
            registry={blocked_source.id: blocked_source},
            cache_root=tmp_path / "empty-cache",
            converter_registry=ConverterRegistry({("fixture-converter", "1"): should_not_convert}),
        )
    assert early.value.code == "SOURCE_LICENSE_BLOCKED"
    assert converter_calls == []
    receipt = _convert(blocked_source, tmp_path, allow=True)
    assert receipt["governance"]["status"] == "approved"
    assert receipt["governance"]["publishable"] is False
    with pytest.raises(ManagedSourceError) as blocked:
        import_cached_source(
            blocked_source.id,
            REVISION,
            {"input_count": 1},
            resources=InMemoryResourceStore(),
            actor="test",
            registry={blocked_source.id: blocked_source},
            cache_root=tmp_path,
            converter_registry=_registry(),
        )
    assert blocked.value.code == "SOURCE_NOT_READY"

    internal_payload = _source("pending").model_dump(mode="json")
    internal_payload["id"] = "approved-internal-fixture"
    internal_payload["tier"] = "generated-internal"
    internal_payload["governance"].update({"status": "approved-internal", "distribution_scope": "internal-only"})
    internal_payload["safety"].update({"network_entrypoint": "none", "allowed_protocols": []})
    internal_source = SourceSpec.model_validate(internal_payload)
    receipt = _convert(internal_source, tmp_path, allow=True)
    assert receipt["governance"]["status"] == "approved-internal"
    assert receipt["governance"]["publishable"] is False


def test_pending_and_restricted_prepare_reject_before_network(tmp_path: Path):
    transport = _Transport()
    for status in ("pending", "restricted"):
        source = _source(status)
        with pytest.raises(ManagedSourceError) as error:
            prepare_source(
                source.id, REVISION, {"input_count": 1},
                resources=InMemoryResourceStore(), actor="test",
                registry={source.id: source}, cache_root=tmp_path,
                transport=transport, converter_registry=_registry(),
            )
        assert error.value.code in {"SOURCE_LICENSE_BLOCKED", "SOURCE_OVERRIDE_UNVERIFIED"}
    assert transport.calls == []


def test_approved_prepare_publishes_three_records_atomically_and_is_idempotent(tmp_path: Path):
    source = _source("approved")
    resources = InMemoryResourceStore()
    transport = _Transport()
    ticks = iter(["2026-09-19T00:00:00+00:00", "2026-09-19T00:00:01+00:00"])
    kwargs = {
        "source_id": source.id,
        "revision": REVISION,
        "config": {"input_count": 1, "version": "1"},
        "resources": resources,
        "actor": "test",
        "registry": {source.id: source},
        "cache_root": tmp_path,
        "transport": transport,
        "converter_registry": _registry(),
        "clock": lambda: next(ticks),
    }
    first = prepare_source(**kwargs)
    second = prepare_source(**kwargs)
    assert first["publication_id"] == second["publication_id"]
    assert len(transport.calls) == 1
    assert len(resources.datasets.list()) == 1
    assert len(resources.scenarios.list()) == 1
    assert len(resources.publications.list()) == 1


def test_approved_import_uses_cache_only_and_publishes_idempotently(tmp_path: Path):
    source = _source("approved")
    _write_cache(source, tmp_path)
    resources = InMemoryResourceStore()
    receipt = import_cached_source(
        source.id, REVISION, {"input_count": 1, "version": "1"},
        resources=resources, actor="test", registry={source.id: source},
        cache_root=tmp_path, converter_registry=_registry(),
        clock=lambda: "2026-09-19T00:00:00+00:00",
    )
    again = import_cached_source(
        source.id, REVISION, {"input_count": 1, "version": "1"},
        resources=resources, actor="test", registry={source.id: source},
        cache_root=tmp_path, converter_registry=_registry(),
        clock=lambda: "2026-09-19T00:00:01+00:00",
    )
    assert receipt["publication_id"] == again["publication_id"]
    assert len(resources.datasets.list()) == 1
    assert len(resources.scenarios.list()) == 1
    assert len(resources.publications.list()) == 1


def test_new_run_rejects_published_dataset_with_forged_registered_provenance(
    tmp_path: Path, monkeypatch,
):
    from motte_contracts.direct_llm_v2 import scenario_for_v2
    from motte_sdk.publication import publication_audit
    import motte_sdk.dataset_sources as source_registry

    source = _source("approved")
    dataset = deepcopy(_dataset())
    artifact = source.upstream.artifacts[0]
    dataset["provenance"].update({
        "source_id": source.id,
        "source_kind": source.tier,
        "homepage": source.links.homepage,
        "upstream_revision": "b" * 40,
        "artifacts": [{
            "logical_name": artifact.logical_name,
            "url": artifact.url,
            "sha256": artifact.sha256,
            "bytes": artifact.bytes,
        }],
    })
    dataset["provenance"].pop("artifact_manifest_sha256", None)
    dataset = normalize_direct_llm_v2_dataset(dataset)
    scenario = scenario_for_v2(dataset, version="1")
    audit = publication_audit(
        dataset, scenario, {"fixture": "forged-provenance"},
        actor="pytest", entrypoint="sdk-test", published_at="2026-09-19T00:00:00Z",
    )
    resources = InMemoryResourceStore()
    persist_direct_llm_v2_dataset(dataset, resources, version="1", publication=audit)
    monkeypatch.setattr(source_registry, "load_registry", lambda: {source.id: source})

    with pytest.raises(ValueError, match="SOURCE_LICENSE_INVALID"):
        resolve_direct_llm_v2_manifest(scenario, {"profile": "full"}, resources)


def test_managed_publication_identity_is_independent_of_cache_root(tmp_path: Path):
    source = _source("approved")
    roots = (tmp_path / "cache-a", tmp_path / "cache-b")
    stores = (InMemoryResourceStore(), InMemoryResourceStore())
    receipts = []
    for index, (root, resources) in enumerate(zip(roots, stores, strict=True)):
        _write_cache(source, root)
        receipts.append(import_cached_source(
            source.id,
            REVISION,
            {"input_count": 1},
            resources=resources,
            actor="test",
            registry={source.id: source},
            cache_root=root,
            converter_registry=_registry(),
            clock=lambda index=index: f"2026-09-19T00:00:0{index}Z",
        ))

    first, second = receipts
    assert first["artifact_paths"] != second["artifact_paths"]
    assert first["publication_id"] == second["publication_id"]
    assert first["publication_audit"]["receipt"] == second["publication_audit"]["receipt"]
    assert first["publication_audit"]["receipt_sha256"] == second["publication_audit"]["receipt_sha256"]
    assert "artifact_paths" not in first["publication_audit"]["receipt"]


def test_managed_import_auto_versions_changed_content_and_reuses_identical_content(
    tmp_path: Path,
):
    source = _source("approved")
    _write_cache(source, tmp_path)
    resources = InMemoryResourceStore()

    def changed_converter(**kwargs):
        del kwargs
        dataset = deepcopy(_dataset())
        dataset["cases"][0]["expected"] = "changed"
        return ConversionResult(dataset, input_count=1)

    common = {
        "source_id": source.id,
        "revision": REVISION,
        "config": {"input_count": 1},
        "resources": resources,
        "actor": "test",
        "registry": {source.id: source},
        "cache_root": tmp_path,
    }
    first = import_cached_source(
        **common, converter_registry=_registry(),
        clock=lambda: "2026-09-19T00:00:00Z",
    )
    changed_registry = ConverterRegistry({("fixture-converter", "1"): changed_converter})
    second = import_cached_source(
        **common, converter_registry=changed_registry,
        clock=lambda: "2026-09-19T00:00:01Z",
    )
    repeated = import_cached_source(
        **common, converter_registry=changed_registry,
        clock=lambda: "2026-09-19T00:00:02Z",
    )

    assert first["publication_audit"]["dataset"] == "fixture-managed-direct@1"
    assert second["publication_audit"]["dataset"] == "fixture-managed-direct@2"
    assert second["publication_audit"]["scenario"] == "fixture-managed-direct@2"
    assert first["publication_id"] != second["publication_id"]
    assert repeated["publication_id"] == second["publication_id"]
    assert repeated["dataset"]["version"] == "2"
    assert len(resources.datasets.list()) == 2
    assert len(resources.scenarios.list()) == 2
    assert len(resources.publications.list()) == 2
    assert all("artifact_paths" not in item["receipt"] for item in resources.publications.list())


def test_converter_invalid_hash_filter_and_cache_fail_without_orphans(tmp_path: Path):
    source = _source("approved")
    _write_cache(source, tmp_path)

    def failing_converter(**kwargs):
        del kwargs
        raise RuntimeError("boom")

    def invalid_converter(**kwargs):
        del kwargs
        result = deepcopy(_dataset())
        result["cases"] = [{}]
        return result

    for converter, expected in ((failing_converter, "CONVERTER_FAILED"), (invalid_converter, "DATASET_INVALID")):
        with pytest.raises(ManagedSourceError) as error:
            _convert(
                source, tmp_path, registry=ConverterRegistry({("fixture-converter", "1"): converter})
            )
        assert error.value.code == expected

    def rejected(**kwargs):
        del kwargs
        return ConversionResult(deepcopy(_dataset()), input_count=2, rejected=1)

    with pytest.raises(ManagedSourceError) as filtered:
        _convert(
            source, tmp_path,
            registry=ConverterRegistry({("fixture-converter", "1"): rejected}),
            config={"input_count": 2},
        )
    assert filtered.value.code == "CONVERSION_REJECTED"

    path = artifact_cache_path(source, REVISION, "fixture.jsonl", cache_root=tmp_path)
    path.write_bytes(b"tampered")
    with pytest.raises(ManagedSourceError) as corrupt:
        convert_cached_source(
            source.id, REVISION, {"input_count": 1}, True,
            registry={source.id: source}, cache_root=tmp_path, converter_registry=_registry(),
        )
    assert corrupt.value.code == "SOURCE_CACHE_CORRUPT"


def test_publication_conflict_leaves_no_orphan(tmp_path: Path):
    source = _source("approved")
    resources = InMemoryResourceStore()
    first = prepare_source(
        source.id, REVISION, {"input_count": 1, "version": "1"},
        resources=resources, actor="test", registry={source.id: source},
        cache_root=tmp_path, transport=_Transport(), converter_registry=_registry(),
    )
    conflict = deepcopy(first["dataset"])
    conflict["cases"][0]["expected"] = "different"
    conflict.pop("cases_sha256", None)
    conflict.pop("dataset_fingerprint", None)
    conflict = normalize_direct_llm_v2_dataset(conflict)
    from motte_contracts.direct_llm_v2 import scenario_for_v2
    from motte_sdk.publication import publication_audit

    scenario = scenario_for_v2(conflict, version="1")
    publication = publication_audit(
        conflict, scenario, {"fixture": "conflict"},
        actor="test", entrypoint="cli", published_at="2026-09-19T00:00:00Z",
    )
    with pytest.raises(ResourceConflictError):
        resources.publish_dataset_scenario(conflict, scenario, publication=publication)
    assert len(resources.datasets.list()) == 1
    assert len(resources.scenarios.list()) == 1
    assert len(resources.publications.list()) == 1
