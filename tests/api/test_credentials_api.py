import tomllib

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

API_KEY = "sk-live-0123456789abcdef"


def client_with_credentials_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_CREDENTIALS_PATH", str(tmp_path / "credentials.toml"))
    return TestClient(create_app(InMemoryRunStore(), resource_store=InMemoryResourceStore()))


def test_set_credential_writes_file_and_masks_response(tmp_path, monkeypatch):
    client = client_with_credentials_file(tmp_path, monkeypatch)

    saved = client.put("/api/v1/credentials/local-vllm", json={"api_key": API_KEY})
    assert saved.status_code == 200
    assert saved.json() == {"profile": "local-vllm", "key_hint": "sk-l...cdef"}
    assert API_KEY not in saved.text

    credentials_file = tmp_path / "credentials.toml"
    assert tomllib.loads(credentials_file.read_text(encoding="utf-8"))["local-vllm"]["api_key"] == API_KEY

    updated = client.put("/api/v1/credentials/local-vllm", json={"api_key": f"  {API_KEY}  "})
    assert updated.status_code == 200
    assert tomllib.loads(credentials_file.read_text(encoding="utf-8"))["local-vllm"]["api_key"] == API_KEY


def test_list_credentials_returns_masked_profiles(tmp_path, monkeypatch):
    client = client_with_credentials_file(tmp_path, monkeypatch)
    assert client.get("/api/v1/credentials").json() == {"items": [], "total": 0}

    client.put("/api/v1/credentials/alpha", json={"api_key": API_KEY})
    client.put("/api/v1/credentials/short", json={"api_key": "tiny-key"})

    listed = client.get("/api/v1/credentials").json()
    assert [item["profile"] for item in listed["items"]] == ["alpha", "short"]
    assert listed["items"][0]["key_hint"] == "sk-l...cdef"
    assert listed["items"][1]["key_hint"] == "***"  # ≤8 字符只回 ***
    assert API_KEY not in str(listed)


def test_set_credential_rejects_empty_or_non_string_key(tmp_path, monkeypatch):
    client = client_with_credentials_file(tmp_path, monkeypatch)

    for body in ({"api_key": ""}, {"api_key": "   "}, {"api_key": 123}, {}):
        rejected = client.put("/api/v1/credentials/local-vllm", json=body)
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "CONTRACT_INVALID"

    assert not (tmp_path / "credentials.toml").exists()


def test_provider_creation_still_rejects_inline_api_key(tmp_path, monkeypatch):
    client = client_with_credentials_file(tmp_path, monkeypatch)

    leaked = client.post(
        "/api/v1/providers",
        json={"name": "bad", "kind": "openai_compatible", "base_url": "http://x", "api_key": API_KEY},
    )
    assert leaked.status_code == 422
    assert leaked.json()["error"]["code"] == "CREDENTIALS_REJECTED"
    assert not (tmp_path / "credentials.toml").exists()
