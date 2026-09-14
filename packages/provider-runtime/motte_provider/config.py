"""Run manifest 中 provider 配置的校验与构造（API 创建预检与 Worker 执行共用）。"""
from __future__ import annotations

import os
from typing import Any

from .capabilities import UnsupportedParameterError, validate_parameters
from .openai_compatible import SUPPORTED_PARAMETERS, CaseDrivenProvider, OpenAICompatibleProvider
from .pricing import parse_price_table
from .transport import HTTPTransport

PROVIDER_KINDS = ("replay", "openai_compatible")


def validate_provider_config(config: dict[str, Any]) -> None:
    """strict 预检：不合法的配置在任何付费调用之前失败。"""
    kind = config.get("kind")
    if kind not in PROVIDER_KINDS:
        raise ValueError(f"unsupported provider kind: {kind!r}")
    if kind != "openai_compatible":
        return
    for required in ("base_url", "model"):
        if not config.get(required):
            raise ValueError(f"provider config requires {required}")
    validate_parameters(config.get("parameters") or {}, SUPPORTED_PARAMETERS)
    parse_price_table(config.get("price_table"))


def build_case_provider(
    config: dict[str, Any],
    cases: dict[str, dict[str, Any]] | None = None,
    *,
    api_key: str | None = None,
) -> CaseDrivenProvider:
    validate_provider_config(config)
    if api_key is None:
        env_name = config.get("api_key_env", "OPENAI_API_KEY")
        api_key = os.environ.get(env_name) or None
    transport = HTTPTransport(
        config["base_url"],
        api_key,
        timeout=config.get("timeout", 30.0),
        max_retries=config.get("max_retries", 2),
    )
    provider = OpenAICompatibleProvider(
        transport,
        config["model"],
        parameters=config.get("parameters"),
        price_table=parse_price_table(config.get("price_table")),
    )
    return CaseDrivenProvider(provider, cases or {})
