"""本地凭据文件：读写、权限、三级解析优先级、掩码。"""
import os
import stat

import pytest

from motte_provider.credentials import (
    credentials_path,
    load_credentials,
    mask,
    remove_profile,
    resolve_api_key,
    save_api_key,
)


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path, monkeypatch):
    path = tmp_path / "credentials.toml"
    monkeypatch.setenv("MOTTE_CREDENTIALS_PATH", str(path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return path


def test_save_and_load_roundtrip_with_owner_only_permissions():
    path = save_api_key("openai-main", "sk-test-12345678")
    assert path == credentials_path()
    profiles = load_credentials()
    assert profiles["openai-main"]["api_key"] == "sk-test-12345678"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_preserves_other_profiles():
    save_api_key("openai-main", "sk-aaaaaaaaaa")
    save_api_key("anthropic-main", "sk-ant-bbbbbbbb")
    profiles = load_credentials()
    assert set(profiles) == {"openai-main", "anthropic-main"}


def test_remove_profile():
    save_api_key("openai-main", "sk-aaaaaaaaaa")
    assert remove_profile("openai-main") is True
    assert load_credentials() == {}
    assert remove_profile("openai-main") is False


def test_missing_file_loads_empty():
    assert load_credentials() == {}


def test_resolution_priority_explicit_over_file_over_env(_isolated_credentials, monkeypatch):
    save_api_key("openai-main", "sk-from-file-000000")
    monkeypatch.setenv("MY_KEY", "sk-from-env")
    assert resolve_api_key("openai-main", explicit="sk-explicit") == "sk-explicit"
    assert resolve_api_key("openai-main", env_name="MY_KEY") == "sk-from-file-000000"
    assert resolve_api_key("other-profile", env_name="MY_KEY") == "sk-from-env"
    assert resolve_api_key(None) is None


def test_build_case_provider_prefers_credentials_profile(_isolated_credentials):
    from motte_provider.config import build_case_provider

    save_api_key("openai-main", "sk-from-file-000000")
    provider = build_case_provider(
        {"kind": "openai_compatible", "name": "openai-main", "base_url": "https://api.example.test/v1", "model": "m"},
        {},
    )
    assert provider.provider.transport._api_key == "sk-from-file-000000"


def test_build_case_provider_falls_back_to_env(_isolated_credentials, monkeypatch):
    from motte_provider.config import build_case_provider

    monkeypatch.setenv("MY_KEY", "sk-from-env")
    provider = build_case_provider(
        {"kind": "openai_compatible", "base_url": "https://api.example.test/v1", "model": "m", "api_key_env": "MY_KEY"},
        {},
    )
    assert provider.provider.transport._api_key == "sk-from-env"


def test_insecure_permissions_warn_but_load(tmp_path, monkeypatch, capsys):
    path = tmp_path / "loose.toml"
    path.write_text('[openai-main]\napi_key = "sk-aaaaaaaaaa"\n', encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o644)
        profiles = load_credentials(path)
        assert profiles["openai-main"]["api_key"] == "sk-aaaaaaaaaa"
        assert "权限" in capsys.readouterr().err


def test_mask_hides_middle():
    assert mask("sk-test-12345678") == "sk-t...5678"
    assert mask("short") == "***"


def test_toml_roundtrip_with_special_characters():
    save_api_key('we"ird', 'sk-with-"quote"-and\\slash')
    profiles = load_credentials()
    assert profiles['we"ird']["api_key"] == 'sk-with-"quote"-and\\slash'
