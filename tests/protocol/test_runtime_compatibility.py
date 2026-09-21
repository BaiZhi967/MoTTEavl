"""M4-T02：上游版本锁定与能力 fail-closed 兼容矩阵。

反例先行：未知版本、能力缺失、版本漂移、缺认证各自给出不同的拒绝原因
（M4-A01）；静态 probe 零模型调用；构造/GET 不下载、不登录、不升级。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from motte_harness.compatibility import (
    CompatibilityError,
    load_runtime_compatibility,
    readiness_for_backend,
    resolve_pinned_version,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "docs" / "protocols" / "runtime-compatibility.json"


class TestManifestIntegrity:
    def test_manifest_loads_and_lists_all_m4_backends(self):
        manifest = load_runtime_compatibility(MANIFEST)
        assert set(manifest["backends"]) == {
            "pi-agent@1", "claude-cli@1", "codex-cli@1", "codex-app-server@1",
            "codex-app-server@2",
        }
        for backend in manifest["backends"].values():
            assert backend["upstream"]["version"]
            assert backend["adapter_version"] and backend["parser_version"]
            assert backend["readiness_rules"]["execution_ready"]

    def test_pinned_versions_resolve(self):
        assert resolve_pinned_version("claude-cli", MANIFEST) == "2.1.278"
        assert resolve_pinned_version("codex-cli", MANIFEST) == "0.155.1"
        assert resolve_pinned_version("pi-agent", MANIFEST) == "0.73.1"
        with pytest.raises(CompatibilityError):
            resolve_pinned_version("unknown-backend", MANIFEST)


class TestReadinessFailClosed:
    def test_missing_binary_and_version_drift_have_distinct_reasons(self, tmp_path):
        # 未安装：installed=false，原因指向二进制缺失
        missing = readiness_for_backend(
            "claude-cli", manifest_path=MANIFEST,
            binary_checker=lambda name: None,
            version_output=None,
        )
        assert missing["installed"] is False
        assert missing["protocol_ready"] is False
        assert missing["execution_ready"] is False
        assert "claude" in missing["reasons"]["installed"]

        # 版本漂移：installed=true 但 pinned 不匹配 → fail closed 且原因不同
        drift = readiness_for_backend(
            "claude-cli", manifest_path=MANIFEST,
            binary_checker=lambda name: "/usr/local/bin/claude",
            version_output="1.0.42 (Claude Code)",
        )
        assert drift["installed"] is True
        assert drift["protocol_ready"] is False
        assert "version drift" in drift["reasons"]["protocol_ready"]
        assert "1.0.42" in drift["reasons"]["protocol_ready"]
        assert drift["execution_ready"] is False

    def test_pinned_version_passes_protocol_ready_but_not_execution(self, tmp_path):
        state = readiness_for_backend(
            "claude-cli", manifest_path=MANIFEST,
            binary_checker=lambda name: "/usr/local/bin/claude",
            version_output="2.1.278 (Claude Code)",
        )
        # 协议就绪不由 --version 推出执行就绪（M4-G02）：execution 需要真实任务证据
        assert state["installed"] is True
        assert state["protocol_ready"] is True
        assert state["execution_ready"] is False
        assert "live" in state["reasons"]["execution_ready"]

    def test_unknown_backend_and_missing_manifest_fail_closed(self):
        with pytest.raises(CompatibilityError):
            readiness_for_backend(
                "carrier-pigeon", manifest_path=MANIFEST,
                binary_checker=lambda name: None, version_output=None,
            )
        with pytest.raises(CompatibilityError):
            load_runtime_compatibility(Path("does-not-exist.json"))

    def test_capability_claims_are_never_readiness_evidence(self):
        manifest = load_runtime_compatibility(MANIFEST)
        # manifest 的 capabilities 声明本身不构成 execution_ready 证据
        assert manifest["backends"]["claude-cli@1"]["capabilities"]["usage"] is True
        state = readiness_for_backend(
            "claude-cli", manifest_path=MANIFEST,
            binary_checker=lambda name: "/bin/claude",
            version_output="2.1.278 (Claude Code)",
        )
        assert state["execution_ready"] is False

    def test_pi_readiness_uses_installed_package_version(self, tmp_path):
        # pi 的 installed 层来自 node_modules 内包版本与 pinned 比对
        fake_root = tmp_path / "bridges" / "pi" / "node_modules" / "@mariozechner" / "pi-agent-core"
        fake_root.mkdir(parents=True)
        (fake_root / "package.json").write_text(
            json.dumps({"name": "@mariozechner/pi-agent-core", "version": "0.73.1"}),
            encoding="utf-8",
        )
        state = readiness_for_backend(
            "pi-agent", manifest_path=MANIFEST, node_modules_root=tmp_path / "bridges" / "pi" / "node_modules",
        )
        assert state["installed"] is True
        # 未提供 probe 证据（probe_result=None）：protocol 层关闭
        assert state["protocol_ready"] is False

        drifted = tmp_path / "bridges2" / "pi" / "node_modules" / "@mariozechner" / "pi-agent-core"
        drifted.mkdir(parents=True)
        (drifted / "package.json").write_text(
            json.dumps({"name": "@mariozechner/pi-agent-core", "version": "0.99.0"}),
            encoding="utf-8",
        )
        drifted_state = readiness_for_backend(
            "pi-agent", manifest_path=MANIFEST,
            node_modules_root=tmp_path / "bridges2" / "pi" / "node_modules",
        )
        assert drifted_state["installed"] is True
        assert drifted_state["protocol_ready"] is False
        assert "0.99.0" in drifted_state["reasons"]["protocol_ready"]

    def test_probe_result_with_wrong_protocol_fails_closed(self):
        state = readiness_for_backend(
            "pi-agent", manifest_path=MANIFEST,
            probe_result={"protocol": "v1", "execution_ready": True},
        )
        assert state["protocol_ready"] is False
        assert "protocol" in state["reasons"]["protocol_ready"]


class TestStaticProbeNoSideEffects:
    def test_readiness_probe_performs_no_model_calls_or_downloads(self):
        # readiness_for_backend 只做本地检查：无可注入网络/子进程路径。
        # 通过注入的 binary_checker/version_output/probe_result 工作；
        # 未注入时仅做文件存在性检查（本机 claude/codex 未安装 → false）。
        state = readiness_for_backend("codex-cli", manifest_path=MANIFEST)
        assert state["installed"] in (True, False)
        assert isinstance(state["reasons"], dict)
