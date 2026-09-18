"""最新 commit 解析与 URL 拼接：urlopen 注入假响应，不产生真实网络。"""
import json
import urllib.request

import pytest

from motte_sdk.gsm8k_source import SourceUnavailable, latest_revision, official_url

FILE_SHA = "1" * 40
HEAD_SHA = "2" * 40
FILE_QUERY = "https://api.github.com/repos/openai/grade-school-math/commits?per_page=1&path=grade_school_math/data/test.jsonl"
HEAD_QUERY = "https://api.github.com/repos/openai/grade-school-math/commits?per_page=1"


class _Response:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(routes):
    calls = []

    def open_(request, timeout):
        calls.append(request.full_url)
        payload = routes.get(request.full_url)
        if payload is None:
            raise OSError(f"unexpected url: {request.full_url}")
        return _Response(json.dumps(payload).encode())

    return open_, calls


def test_official_url_requires_pinned_commit():
    assert official_url("a" * 40).endswith("/" + "a" * 40 + "/grade_school_math/data/test.jsonl")
    for bad in ("", "main", "a" * 39, "a" * 41, "z" * 40):
        with pytest.raises(ValueError, match="pinned"):
            official_url(bad)


def test_latest_revision_prefers_the_file_commit(monkeypatch):
    open_, calls = _opener({FILE_QUERY: [{"sha": FILE_SHA}]})
    monkeypatch.setattr(urllib.request, "urlopen", open_)
    assert latest_revision() == FILE_SHA
    assert calls == [FILE_QUERY]


def test_latest_revision_falls_back_to_default_branch_head(monkeypatch):
    open_, calls = _opener({FILE_QUERY: [], HEAD_QUERY: [{"sha": HEAD_SHA}]})
    monkeypatch.setattr(urllib.request, "urlopen", open_)
    assert latest_revision() == HEAD_SHA
    assert calls == [FILE_QUERY, HEAD_QUERY]


def test_latest_revision_reports_transport_failure_once_with_hint(monkeypatch):
    open_, calls = _opener({})
    monkeypatch.setattr(urllib.request, "urlopen", open_)
    with pytest.raises(SourceUnavailable, match="explicit 40-character commit"):
        latest_revision()
    assert calls == [FILE_QUERY]  # 源不可达时不再重试第二个 URL（避免双倍超时）


def test_latest_revision_rejects_unexpected_payload(monkeypatch):
    open_, _ = _opener({FILE_QUERY: [{"sha": "main"}], HEAD_QUERY: {"message": "rate limited"}})
    monkeypatch.setattr(urllib.request, "urlopen", open_)
    with pytest.raises(SourceUnavailable, match="explicit 40-character commit"):
        latest_revision()
