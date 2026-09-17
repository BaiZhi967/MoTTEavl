"""manifest 引用解析：连接/模型/价格表展开与合并优先级。"""
import pytest

from motte_sdk.resolve import (
    ManifestResolutionError,
    find_secret_paths,
    resolve_manifest,
    validate_resolved_manifest,
)
from motte_storage.resource_store import InMemoryResourceStore


@pytest.fixture
def resources():
    store = InMemoryResourceStore()
    store.providers.put({
        "name": "openai-main",
        "kind": "openai_compatible",
        "base_url": "https://api.openai.test/v1",
        "credentials": "openai-main",
        "timeout": 15,
    })
    store.models.put({
        "id": "gpt-4o-mini",
        "provider": "openai-main",
        "model": "gpt-4o-mini-2024-07-18",
        "capabilities": {},
        "parameters": {"temperature": 0.2, "top_p": None},
    })
    store.price_tables.put({
        "model_id": "gpt-4o-mini", "version": "2024-09",
        "input_per_million": 0.15, "output_per_million": 0.6,
    })
    store.price_tables.put({
        "model_id": "gpt-4o-mini", "version": "2024-10",
        "input_per_million": 0.15, "output_per_million": 0.6,
    })
    return store


def test_model_reference_expands_connection_model_params_and_latest_price(resources):
    manifest = resolve_manifest({"model": "gpt-4o-mini"}, resources)
    provider = manifest["provider"]
    assert provider["kind"] == "openai_compatible"
    assert provider["base_url"] == "https://api.openai.test/v1"
    assert provider["model"] == "gpt-4o-mini-2024-07-18"
    assert provider["parameters"] == {"temperature": 0.2}
    assert provider["price_table"]["version"] == "2024-10"
    assert provider["name"] == "openai-main"


def test_price_table_version_natural_sort(resources):
    store = resources
    store.price_tables.put({
        "model_id": "gpt-4o-mini", "version": "2025-2",
        "input_per_million": 1, "output_per_million": 1,
    })
    manifest = resolve_manifest({"model": "gpt-4o-mini"}, store)
    assert manifest["provider"]["price_table"]["version"] == "2025-2"


def test_pinned_price_table_version(resources):
    manifest = resolve_manifest({"model": "gpt-4o-mini", "price_table_version": "2024-09"}, resources)
    assert manifest["provider"]["price_table"]["version"] == "2024-09"


def test_pinned_price_table_version_missing(resources):
    with pytest.raises(ManifestResolutionError) as error:
        resolve_manifest({"model": "gpt-4o-mini", "price_table_version": "1999-01"}, resources)
    assert error.value.code == "PRICE_TABLE_NOT_FOUND"


def test_manifest_parameters_override_profile_defaults(resources):
    manifest = resolve_manifest(
        {"model": "gpt-4o-mini", "parameters": {"temperature": 0.9}}, resources
    )
    assert manifest["provider"]["parameters"]["temperature"] == 0.9


def test_provider_name_with_legacy_model_payload(resources):
    resources.providers.put({
        "name": "legacy", "kind": "openai_compatible",
        "base_url": "http://localhost:8001/v1", "model": "m",
        "price_table": {"version": "v1", "input_per_million": 1, "output_per_million": 1},
    })
    manifest = resolve_manifest({"provider": "legacy"}, resources)
    assert manifest["provider"]["model"] == "m"
    assert manifest["provider"]["price_table"]["version"] == "v1"


def test_model_and_provider_name_must_match(resources):
    resources.models.put({
        "id": "claude-sonnet", "provider": "anthropic-main", "capabilities": {},
    })
    with pytest.raises(ManifestResolutionError) as error:
        resolve_manifest({"provider": "openai-main", "model": "claude-sonnet"}, resources)
    assert error.value.code == "MODEL_PROVIDER_CONFLICT"


def test_model_reference_with_inline_provider_conflicts(resources):
    with pytest.raises(ManifestResolutionError) as error:
        resolve_manifest(
            {"provider": {"kind": "openai_compatible", "base_url": "https://x.test", "model": "m"},
             "model": "gpt-4o-mini"},
            resources,
        )
    assert error.value.code == "MODEL_PROVIDER_CONFLICT"


def test_unknown_provider_and_model(resources):
    with pytest.raises(ManifestResolutionError) as error:
        resolve_manifest({"provider": "missing"}, resources)
    assert error.value.code == "RESOURCE_NOT_FOUND"
    with pytest.raises(ManifestResolutionError) as error:
        resolve_manifest({"model": "missing"}, resources)
    assert error.value.code == "MODEL_NOT_FOUND"


def test_disabled_provider_and_model_are_rejected(resources):
    resources.providers.put({
        "name": "off", "kind": "openai_compatible",
        "base_url": "https://x.test", "enabled": False,
    })
    with pytest.raises(ManifestResolutionError) as error:
        resolve_manifest({"provider": "off"}, resources)
    assert error.value.code == "PROVIDER_DISABLED"

    resources.models.put({
        "id": "paused", "provider": "openai-main", "capabilities": {}, "enabled": False,
    })
    with pytest.raises(ManifestResolutionError) as error:
        resolve_manifest({"model": "paused"}, resources)
    assert error.value.code == "MODEL_DISABLED"


def test_stored_provider_with_plaintext_secret_is_rejected(resources):
    resources.providers.put({
        "name": "leaky", "kind": "openai_compatible", "base_url": "https://x.test", "model": "m",
        "api_key": "sk-leaked",
    })
    with pytest.raises(ManifestResolutionError) as error:
        resolve_manifest({"provider": "leaky"}, resources)
    assert error.value.code == "CREDENTIALS_REJECTED"


def test_inline_provider_passes_through_and_input_not_mutated(resources):
    inline = {"kind": "openai_compatible", "base_url": "https://x.test", "model": "m"}
    manifest = {"provider": dict(inline)}
    resolved = resolve_manifest(manifest, resources)
    assert resolved["provider"] == inline
    assert manifest["provider"] == inline


def test_validate_resolved_manifest_runs_strict_precheck(resources):
    manifest = resolve_manifest({"model": "gpt-4o-mini"}, resources)
    validate_resolved_manifest(manifest)  # 有效配置不抛
    bad = {"provider": {"kind": "openai_compatible", "base_url": "https://x.test", "model": "m",
                        "parameters": {"temperature": 99}}}
    with pytest.raises(Exception, match="temperature"):
        validate_resolved_manifest(bad)


def test_find_secret_paths_is_recursive():
    body = {"manifest": {"nested": [{"api_key": "x", "ok": 1}], "x-api-key": "y"}}
    assert set(find_secret_paths(body)) == {
        "$.manifest.nested[0].api_key",
        "$.manifest.x-api-key",
    }
