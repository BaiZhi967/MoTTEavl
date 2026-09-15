"""CLI credentials 子命令：set/list/remove roundtrip。"""
import pytest

from motte_cli.main import main
from motte_provider.credentials import load_credentials


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTTE_CREDENTIALS_PATH", str(tmp_path / "credentials.toml"))


def test_set_list_remove_roundtrip(monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda *_: "sk-cli-test-0009")
    assert main(["credentials", "set", "openai-main"]) == 0
    assert load_credentials()["openai-main"]["api_key"] == "sk-cli-test-0009"

    assert main(["credentials", "list"]) == 0
    out = capsys.readouterr().out
    assert "openai-main" in out
    assert "sk-cli-test-0009" not in out
    assert "sk-c...0009" in out

    assert main(["credentials", "remove", "openai-main"]) == 0
    assert load_credentials() == {}
    capsys.readouterr()


def test_set_refuses_empty_key(monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda *_: "   ")
    assert main(["credentials", "set", "openai-main"]) == 2
    assert load_credentials() == {}


def test_remove_missing_profile_fails(capsys):
    assert main(["credentials", "remove", "nope"]) == 1
    assert "不存在" in capsys.readouterr().err


def test_list_empty_shows_path(capsys):
    assert main(["credentials", "list"]) == 0
    assert "无凭据" in capsys.readouterr().out
