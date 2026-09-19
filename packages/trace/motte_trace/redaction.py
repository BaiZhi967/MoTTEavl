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
# 边界用 ASCII 字符类环视而不是 ``\b``：``\b`` 把中文也算作单词字符，
# "密钥是sk-…" 这类中文相邻的密钥会因无边界而漏掉；ASCII 环视既允许中文/
# 标点相邻，又拦住 "ta[sk-]response.txt" 这类内嵌普通词（R4 #2）。
import re as _re

_ASCII_TOKEN = "A-Za-z0-9_-"
_SECRET_VALUE_PATTERNS = tuple(_re.compile(pattern) for pattern in (
    rf"(?<![{_ASCII_TOKEN}])sk-[A-Za-z0-9_-]{{8,}}(?![{_ASCII_TOKEN}])",
    rf"(?<![{_ASCII_TOKEN}])ghp_[A-Za-z0-9]{{20,}}(?![{_ASCII_TOKEN}])",
    rf"(?<![{_ASCII_TOKEN}])gho_[A-Za-z0-9]{{20,}}(?![{_ASCII_TOKEN}])",
    rf"(?<![{_ASCII_TOKEN}])AKIA[0-9A-Z]{{16}}(?![{_ASCII_TOKEN}])",
    r"Bearer\s+[A-Za-z0-9._-]{16,}",
    rf"(?<![{_ASCII_TOKEN}])xoxb-[A-Za-z0-9-]{{10,}}(?![{_ASCII_TOKEN}])",
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
