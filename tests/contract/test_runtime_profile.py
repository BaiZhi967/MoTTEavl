"""M4-T01：Runtime 契约、model_control 条件校验与 backend 双校验。

反例先行：未知 kind/transport/model_control、字段 allowlist 之外的设置、
platform-controlled 缺发布模型、runner-configured 缺原生模型配置、
externally-managed 缺凭据引用都必须拒绝；声明不能扩大工具权限。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from motte_contracts.runtime import (
    MODEL_CONTROL_MODES,
    RuntimeDefinition,
    RuntimeProfile,
    RuntimeToolControl,
    RuntimeVersion,
    runtime_readiness,
    validate_runtime_settings,
)

from motte_sdk.execution_backends import ExecutionBackendError


def _definition(**overrides):
    base = {
        "kind": "pi-bridge",
        "transport": "bridge-stdio-jsonl",
        "upstream_version": "@mariozechner/pi-agent-core@0.73.1",
        "adapter_version": "pi-bridge@1",
        "parser_version": "pi-jsonl-v2",
        "config_schema": {
            "properties": {
                "model": {"type": "string"},
                "system_prompt": {"type": "string"},
                "max_steps": {"type": "integer"},
            },
            "required": ["model"],
        },
        "supported_modes": ["batch"],
        "interactive": False,
        "model_control": "runner-configured",
        "tool_control": {
            "enforcement": "bridge-sandbox",
            "enforcement_owner": "platform",
            "tools": ["read_file", "write_file", "list_files"],
        },
        "evidence_capabilities": {"session_events": True, "usage": True},
    }
    base.update(overrides)
    return RuntimeDefinition.model_validate(base)


class TestRuntimeDefinitionContract:
    def test_runtime_definition_rejects_unknown_kind_and_transport(self):
        with pytest.raises(ValidationError):
            _definition(kind="random-agent")
        with pytest.raises(ValidationError):
            _definition(transport="carrier-pigeon")

    def test_runtime_definition_rejects_unknown_model_control(self):
        with pytest.raises(ValidationError):
            _definition(model_control="wishful")

    def test_runtime_definition_interactive_flag_must_match_modes(self):
        with pytest.raises(ValidationError):
            _definition(interactive=True)
        with pytest.raises(ValidationError):
            _definition(supported_modes=["batch", "interactive"])

    def test_runtime_definition_rejects_unknown_evidence_capabilities(self):
        with pytest.raises(ValidationError):
            _definition(evidence_capabilities={"telepathy": True})

    def test_runtime_definition_rejects_bad_config_schema(self):
        with pytest.raises(ValidationError):
            _definition(config_schema={"properties": {}})
        with pytest.raises(ValidationError):
            _definition(config_schema={
                "properties": {"model": {"type": "string"}},
                "required": ["missing_key"],
            })

    def test_runtime_version_is_published_only(self):
        definition = _definition()
        version = RuntimeVersion.model_validate({
            "name": "pi-agent", "version": "1", "definition": definition.model_dump(),
            "published_at": "2026-09-20T00:00:00Z",
        })
        assert version.lifecycle == "published"
        with pytest.raises(ValidationError):
            RuntimeVersion.model_validate({
                "name": "pi-agent", "version": "1",
                "definition": definition.model_dump(),
                "lifecycle": "draft",
                "published_at": "2026-09-20T00:00:00Z",
            })


class TestRuntimeProfileAndSettings:
    def test_runtime_profile_rejects_bad_ref_and_workspace(self):
        with pytest.raises(ValidationError):
            RuntimeProfile.model_validate({"runtime": "pi-agent"})
        with pytest.raises(ValidationError):
            RuntimeProfile.model_validate({
                "runtime": "pi-agent@1", "workspace": {"source": "host-home"},
            })
        with pytest.raises(ValidationError):
            RuntimeProfile.model_validate({
                "runtime": "pi-agent@1", "workspace": {"source": "per-case-temp", "root": "/tmp"},
            })
        with pytest.raises(ValidationError):
            RuntimeProfile.model_validate({
                "runtime": "pi-agent@1", "workspace": {"source": "pinned-path"},
            })

    def test_runtime_profile_credential_refs_are_names_only(self):
        with pytest.raises(ValidationError):
            RuntimeProfile.model_validate({
                "runtime": "pi-agent@1",
                "credential_refs": ["sk-live-abcdefabcdefabcdef"],
            })

    def test_runtime_settings_allowlist_rejects_unknown_and_type_errors(self):
        definition = _definition()
        schema = definition.config_schema
        with pytest.raises(ValueError, match="unknown fields"):
            validate_runtime_settings(schema, {"model": "m1", "surprise": 1})
        with pytest.raises(ValueError, match="missing required fields"):
            validate_runtime_settings(schema, {"system_prompt": "s"})
        with pytest.raises(ValueError):
            validate_runtime_settings(schema, {"model": "m1", "max_steps": "many"})
        validated = validate_runtime_settings(schema, {"model": "m1", "max_steps": 3})
        assert validated["model"] == "m1"

    def test_runtime_readiness_layering_requires_ordering(self):
        with pytest.raises(ValueError):
            runtime_readiness(
                installed=False, protocol_ready=True, execution_ready=False
            )
        with pytest.raises(ValueError):
            runtime_readiness(
                installed=True, protocol_ready=False, execution_ready=True
            )
        state = runtime_readiness(
            installed=True, protocol_ready=True, execution_ready=False,
            reasons={"execution_ready": "no scripted-task evidence yet"},
        )
        assert state["reasons"]["execution_ready"].startswith("no scripted")


class TestRuntimeModelControlValidation:
    """主断言：test_runtime_model_control_validation 的分项反例。"""

    def _manifest(self, definition, *, provider=None, model=None, profile=None, snapshot=True):
        manifest = {
            "runtime": "pi-agent@1",
            "cases": {"case-1": {"input": "write hello.txt"}},
        }
        if provider is not None:
            manifest["provider"] = provider
        if model is not None:
            manifest["model"] = model
        manifest["runtime_profile"] = profile if profile is not None else {
            "runtime": "pi-agent@1", "native_settings": {"model": "scripted-1"},
        }
        if snapshot:
            manifest["runtime_snapshot"] = {
                "name": "pi-agent", "version": "1",
                "content_hash": "sha256:" + "0" * 64,
                "lifecycle": "published",
                "model_control": definition.model_control,
                "kind": definition.kind,
                "transport": definition.transport,
                "tool_enforcement": definition.tool_control.enforcement,
                "config_schema": definition.config_schema,
            }
        return manifest

    def test_platform_controlled_without_published_model_rejected(self):
        from motte_sdk.runtime_backends import validate_runtime_manifest

        definition = _definition(model_control="platform-controlled")
        manifest = self._manifest(definition)
        with pytest.raises(ExecutionBackendError) as error:
            validate_runtime_manifest(manifest, backend_id="pi-agent")
        assert error.value.code == "RUNTIME_MODEL_REQUIRED"

    def test_platform_controlled_with_resolved_model_accepted(self):
        from motte_sdk.runtime_backends import validate_runtime_manifest

        definition = _definition(model_control="platform-controlled")
        manifest = self._manifest(
            definition,
            provider={"kind": "replay"},
            model="published-model",
        )
        validate_runtime_manifest(manifest, backend_id="pi-agent")

    def test_runner_configured_without_native_model_rejected(self):
        from motte_sdk.runtime_backends import validate_runtime_manifest

        definition = _definition(model_control="runner-configured")
        manifest = self._manifest(
            definition, profile={"runtime": "pi-agent@1", "native_settings": {}},
        )
        with pytest.raises(ExecutionBackendError) as error:
            validate_runtime_manifest(manifest, backend_id="pi-agent")
        assert error.value.code == "RUNTIME_MODEL_CONFIG_REQUIRED"

    def test_runner_configured_does_not_require_provider_kind(self):
        from motte_sdk.runtime_backends import validate_runtime_manifest

        definition = _definition(model_control="runner-configured")
        manifest = self._manifest(definition)
        validate_runtime_manifest(manifest, backend_id="pi-agent")

    def test_externally_managed_without_credential_refs_rejected(self):
        from motte_sdk.runtime_backends import validate_runtime_manifest

        definition = _definition(model_control="externally-managed")
        manifest = self._manifest(definition)
        with pytest.raises(ExecutionBackendError) as error:
            validate_runtime_manifest(manifest, backend_id="pi-agent")
        assert error.value.code == "RUNTIME_AUTH_REQUIRED"

    def test_externally_managed_with_credential_ref_accepted(self):
        from motte_sdk.runtime_backends import validate_runtime_manifest

        definition = _definition(model_control="externally-managed")
        manifest = self._manifest(
            definition,
            profile={
                "runtime": "pi-agent@1",
                "native_settings": {},
                "credential_refs": ["claude-cli-login"],
            },
        )
        validate_runtime_manifest(manifest, backend_id="pi-agent")

    def test_missing_snapshot_or_mismatched_runtime_rejected(self):
        from motte_sdk.runtime_backends import validate_runtime_manifest

        definition = _definition()
        with pytest.raises(ExecutionBackendError) as error:
            validate_runtime_manifest(
                self._manifest(definition, snapshot=False), backend_id="pi-agent",
            )
        assert error.value.code == "RUNTIME_SNAPSHOT_REQUIRED"
        with pytest.raises(ExecutionBackendError):
            validate_runtime_manifest(
                {**self._manifest(definition), "runtime": "claude-cli@1"},
                backend_id="pi-agent",
            )

    def test_capability_declaration_cannot_grant_tool_permissions(self):
        from motte_sdk.runtime_backends import validate_runtime_manifest

        definition = _definition()
        manifest = self._manifest(
            definition,
            profile={
                "runtime": "pi-agent@1",
                "native_settings": {"model": "scripted-1"},
                # 声明超越 runtime 定义的工具，不因出现在 manifest 而获得授权
                "tool_grant": {"tools": ["shell", "docker"]},
            },
        )
        with pytest.raises(ExecutionBackendError) as error:
            validate_runtime_manifest(manifest, backend_id="pi-agent")
        assert error.value.code == "RUNTIME_TOOL_GRANT_INVALID"

    def test_unenforced_tool_boundary_requires_explicit_ack(self):
        from motte_sdk.runtime_backends import validate_runtime_manifest

        definition = _definition(
            tool_control=RuntimeToolControl(
                enforcement="not-enforced", enforcement_owner="runner",
            ).model_dump(),
        )
        manifest = self._manifest(definition)
        with pytest.raises(ExecutionBackendError) as error:
            validate_runtime_manifest(manifest, backend_id="pi-agent")
        assert error.value.code == "RUNTIME_TOOL_BOUNDARY_UNENFORCED"
        validate_runtime_manifest(
            {**manifest, "runtime_accept_unenforced_tools": True},
            backend_id="pi-agent",
        )


class TestModelControlModes:
    def test_documented_modes_are_canonical(self):
        assert MODEL_CONTROL_MODES == (
            "platform-controlled", "runner-configured", "externally-managed",
        )
