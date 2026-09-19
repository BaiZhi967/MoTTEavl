_SECRET_KEYS = {
    "authorization",
    "x-api-key",
    "api-key",
    "api_key",
    "access-token",
    "refresh-token",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "password",
    "openai_api_key",
    "anthropic_api_key",
    "moonshot_api_key",
    "zhipuai_api_key",
    "deepseek_api_key",
}

# 值形状的秘密（哨兵/泄漏检测）：命中即整段替换，保留命中标记不保留正文。
import re as _re

_SECRET_VALUE_PATTERNS = tuple(_re.compile(pattern) for pattern in (
    r"sk-[A-Za-z0-9_-]{8,}",
    r"ghp_[A-Za-z0-9]{20,}",
    r"gho_[A-Za-z0-9]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"Bearer\s+[A-Za-z0-9._-]{16,}",
    r"xoxb-[A-Za-z0-9-]{10,}",
))


def redact_text(value: str) -> str:
    """Replace secret-shaped substrings with a hit marker (content never kept)."""
    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub("[REDACTED-SECRET]", value)
    return value


def redact_secrets(value):
    """键名 + 值形状双重脱敏：dict/list 递归，字符串按值模式替换。"""
    if isinstance(value, dict):
        return {
            k: (
                "[REDACTED]"
                if k.lower() in _SECRET_KEYS
                or any(x in k.lower() for x in ("key", "secret", "token", "authorization"))
                else redact_secrets(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact(value):
    if isinstance(value, dict):
        return {
            k: (
                "[REDACTED]"
                if k.lower() in _SECRET_KEYS
                or any(x in k.lower() for x in ("key", "secret", "token", "authorization"))
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
