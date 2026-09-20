"""review round-2：证据内容读取的编码与脱敏边界（M3-R2-13、R2-07 的 SDK 侧）。

反例（review R2-13）：合法 UTF-8 字节 ``("a" * 65535 + "中" + "z")`` 配正确
sha256，修复前返回 ``verified=true``、``truncated=true``、``encoding=binary``、
``text=None``——预算边界切在多字节字符中间，被当成"整个文件不是 UTF-8"。
"""
from __future__ import annotations

import hashlib

import pytest

from motte_sdk.terminalbench import MAX_DISPLAY_TEXT, read_artifact_text

#: 合成哨兵：形状像密钥，用来验证展示脱敏（不是真实凭据）。
SYNTHETIC_KEY = "sk-review-sentinel-0123456789abcdef"


class _Reader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read_bytes(self, artifact_id: str) -> bytes:
        if artifact_id != "frozen/log":
            raise FileNotFoundError(artifact_id)
        return self.payload


def _ref(payload: bytes, **overrides) -> dict:
    ref = {
        "artifact_id": "frozen/log",
        "kind": "harbor-trial-log",
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "complete": True,
        "truncated": False,
    }
    ref.update(overrides)
    return ref


def test_budget_boundary_does_not_turn_a_utf8_log_into_binary() -> None:
    """预算正好切在多字节字符中间：保留截断标记，编码仍是 UTF-8。"""
    payload = ("a" * (MAX_DISPLAY_TEXT - 1) + "中" + "z").encode("utf-8")
    result = read_artifact_text(_Reader(payload), _ref(payload))
    assert result["verified"] is True
    assert result["truncated"] is True
    assert result["encoding"] == "utf-8", result
    assert result["text"] is not None
    assert result["text"].startswith("a" * 16)
    assert "\ufffd" not in result["text"], "不得用替换字符掩盖边界问题"
    # 预算内的完整字符都要保留；结尾的半个字符被丢弃（不算进正文）。
    assert result["text"] == "a" * (MAX_DISPLAY_TEXT - 1)


def test_emoji_at_the_budget_boundary_is_kept_intact() -> None:
    """4 字节字符（emoji）不能被预算切一半：完整字符保留，被切掉的整字丢弃。"""
    # (a) emoji 完整落在预算内：必须原样保留。
    inside = "x" * (MAX_DISPLAY_TEXT - 4) + "🚀" + "tail"
    payload = inside.encode("utf-8")
    result = read_artifact_text(_Reader(payload), _ref(payload))
    assert result["encoding"] == "utf-8", result
    assert result["truncated"] is True
    assert result["text"] == "x" * (MAX_DISPLAY_TEXT - 4) + "🚀"
    assert "\ufffd" not in result["text"]

    # (b) emoji 被预算切开：整字丢弃，不产生替换字符、也不判成二进制。
    split = "x" * (MAX_DISPLAY_TEXT - 1) + "🚀" + "tail"
    payload = split.encode("utf-8")
    result = read_artifact_text(_Reader(payload), _ref(payload))
    assert result["encoding"] == "utf-8", result
    assert result["truncated"] is True
    assert result["text"] == "x" * (MAX_DISPLAY_TEXT - 1)
    assert "\ufffd" not in result["text"]


def test_chinese_content_within_budget_is_not_truncated() -> None:
    """预算内的中文内容：完整返回、不截断、编码正确。"""
    payload = "步骤一：完成\n步骤二：完成\n中文日志".encode("utf-8")
    result = read_artifact_text(_Reader(payload), _ref(payload))
    assert result["truncated"] is False
    assert result["encoding"] == "utf-8"
    assert "中文日志" in result["text"]


def test_invalid_utf8_is_still_reported_as_binary() -> None:
    """真正非法的 UTF-8 仍然如实标成 binary，不给伪造文本。"""
    payload = b"\xff\xfe\x00\x01binary-payload"
    result = read_artifact_text(_Reader(payload), _ref(payload))
    assert result["encoding"] == "binary"
    assert result["text"] is None
    assert result["verified"] is True
    assert result["note"], "二进制要给可读原因"


def test_display_fields_are_redacted_but_identity_is_kept() -> None:
    """内容与 note 走同一脱敏边界；身份与 hash 逐字保留（R2-07 SDK 侧）。"""
    payload = (f"claude: using {SYNTHETIC_KEY}\n").encode("utf-8")
    ref = _ref(payload, note=f"source file mentioned {SYNTHETIC_KEY}")
    result = read_artifact_text(_Reader(payload), ref)
    assert SYNTHETIC_KEY not in (result["text"] or "")
    assert SYNTHETIC_KEY not in (result["note"] or "")
    assert "[REDACTED-SECRET]" in result["text"]
    assert result["sha256"] == ref["sha256"]
    assert result["artifact_id"] == ref["artifact_id"]
    assert result["size_bytes"] == len(payload)


def test_missing_content_is_honest() -> None:
    """引用存在但内容不可读：给原因，不返回无法验证的文本。"""
    result = read_artifact_text(_Reader(b""), _ref(b"x", artifact_id="missing/log"))
    assert result["text"] is None
    assert result["verified"] is False
    assert result["note"]


@pytest.mark.parametrize("char", ["中", "🚀"])
@pytest.mark.parametrize("remaining", [1, 2, 3, 4])
@pytest.mark.parametrize("tail_length", [2, 5000])
def test_long_utf8_tail_never_skips_bytes_or_exceeds_budget(char, remaining, tail_length):
    prefix = "a" * (MAX_DISPLAY_TEXT - remaining)
    payload = (prefix + char + "z" * tail_length).encode()
    result = read_artifact_text(_Reader(payload), _ref(payload))
    assert result["encoding"] == "utf-8"
    assert len(result["text"].encode()) <= MAX_DISPLAY_TEXT
    expected = prefix
    if len(char.encode()) <= remaining:
        expected += char + "z" * min(tail_length, remaining - len(char.encode()))
    assert result["text"] == expected
    assert result["verified"] is True


def test_invalid_utf8_with_long_tail_is_not_hidden_by_truncation():
    payload = b"prefix\xff" + b"a" * MAX_DISPLAY_TEXT
    result = read_artifact_text(_Reader(payload), _ref(payload))
    assert result["encoding"] == "binary"
    assert result["text"] is None
