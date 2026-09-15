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
