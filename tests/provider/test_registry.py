"""适配器注册表：注册/分发/重复注册/未知 kind。"""
import pytest

from motte_provider import config  # noqa: F401 — 导入即注册内置适配器
from motte_provider.registry import AdapterSpec, adapter_for, register, registered_kinds, smoke_kinds, unregister


def test_builtin_adapters_are_registered():
    kinds = registered_kinds()
    assert "openai_compatible" in kinds
    assert "replay" in kinds


def test_smoke_kinds_only_include_smoke_supported():
    kinds = smoke_kinds()
    assert "openai_compatible" in kinds
    assert "anthropic_messages" in kinds
    assert "openai_responses" in kinds
    assert "replay" not in kinds


def test_validate_dispatches_to_registered_adapter():
    with pytest.raises(ValueError, match="provider config requires base_url"):
        config.validate_provider_config({"kind": "openai_compatible", "model": "m"})


def test_unknown_kind_reports_supported_list():
    with pytest.raises(ValueError, match="supported"):
        adapter_for("mystery_protocol")


def test_duplicate_registration_is_rejected():
    spec = AdapterSpec(kind="__dup__", validate=lambda c: None, build=lambda c, cases: None)
    register(spec)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register(AdapterSpec(kind="__dup__", validate=lambda c: None, build=lambda c, cases: None))
    finally:
        unregister("__dup__")


def test_custom_kind_is_dispatchable():
    built = []

    def build(c, cases):
        built.append((c, cases))
        return "provider-object"

    spec = AdapterSpec(kind="__custom__", validate=lambda c: None, build=build)
    register(spec)
    try:
        provider = config.build_provider({"kind": "__custom__"}, {"c1": {}})
        assert provider == "provider-object"
        assert built == [({"kind": "__custom__"}, {"c1": {}})]
    finally:
        unregister("__custom__")
