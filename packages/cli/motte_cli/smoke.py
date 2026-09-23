"""live smoke：由操作者显式启动的一次真实 Provider 调用与证据记录。

密钥只从凭据文件或环境变量读取，绝不进入报告、日志或 markdown 记录。
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from motte_contracts.messages import Message, ModelRequest
from motte_provider.base import ProviderCallError
from motte_provider.config import adapter_for, smoke_kinds
from motte_provider.pricing import PriceTable
from motte_provider.transport import HTTPTransport


def supported_smoke_providers() -> tuple[str, ...]:
    """注册表中支持冒烟的 kind（含连字符别名的兼容拼写）。"""
    kinds = smoke_kinds()
    return kinds + tuple(kind.replace("_", "-") for kind in kinds)


def _smoke_meta(provider_kind: str, model: str, base_url: str, price_table: PriceTable | None) -> dict[str, Any]:
    return {
        "provider": provider_kind,
        "model": model,
        "base_url": base_url,
        "timestamp": datetime.now(UTC).isoformat(),
        "price_table_version": price_table.version if price_table else None,
    }


def run_live_smoke(
    provider_kind: str,
    model: str,
    *,
    base_url: str,
    api_key: str | None,
    prompt: str,
    price_table: PriceTable | None = None,
    timeout: float = 30.0,
    max_retries: int = 2,
    transport: HTTPTransport | None = None,
) -> dict[str, Any]:
    """执行一次真实调用并返回脱敏报告；失败时抛 ProviderCallError（带证据）。"""
    if provider_kind not in supported_smoke_providers():
        raise ValueError(f"unsupported smoke provider: {provider_kind!r}")
    kind = provider_kind.replace("-", "_")
    provider_cls = adapter_for(kind).provider_cls
    if transport is None:
        transport = HTTPTransport(
            base_url, api_key, timeout=timeout, max_retries=max_retries,
            **adapter_for(kind).transport_kwargs,
        )
    provider = provider_cls(transport, model, price_table=price_table)
    request = ModelRequest(model=model, messages=[Message(role="user", content=prompt)])
    envelope = provider.complete(request)
    return {"smoke": _smoke_meta(provider_kind, model, base_url, price_table), "result": envelope}


def execute_smoke(
    provider_kind: str,
    model: str,
    *,
    base_url: str,
    api_key: str | None,
    prompt: str,
    price_table: PriceTable | None = None,
    timeout: float = 30.0,
    max_retries: int = 2,
    transport: HTTPTransport | None = None,
) -> tuple[dict[str, Any], int]:
    """run_live_smoke 的安全包装：失败也返回带错误分类的报告，不抛 ProviderCallError。"""
    try:
        report = run_live_smoke(
            provider_kind,
            model,
            base_url=base_url,
            api_key=api_key,
            prompt=prompt,
            price_table=price_table,
            timeout=timeout,
            max_retries=max_retries,
            transport=transport,
        )
    except ProviderCallError as error:
        report = {"smoke": _smoke_meta(provider_kind, model, base_url, price_table), "result": error.evidence}
        return report, 1
    return report, 0


def smoke_markdown(report: dict[str, Any]) -> str:
    """把报告渲染为 markdown 记录小节（无密钥）。"""
    smoke = report["smoke"]
    result = report["result"]
    metering = result["metering"]
    lines = [
        f"## {smoke['timestamp']} {smoke['provider']}/{smoke['model']}",
        "",
        f"- base_url: `{smoke['base_url']}`",
        f"- 状态：{'completed' if 'error' not in result else 'failed'}",
        f"- latency_ms: {metering['latency_ms']}，attempts: {metering['attempts']}，retry_count: {metering['retry_count']}",
    ]
    if "error" in result:
        lines.append(f"- 错误分类：{result['error']['class']}，message: {result['error']['message']}")
    else:
        usage = result.get("usage", {})
        lines.append(
            f"- usage: prompt_tokens={usage.get('prompt_tokens')} completion_tokens={usage.get('completion_tokens')}"
        )
        cost = result.get("cost")
        if cost:
            lines.append(
                f"- cost: total={cost['total']} {cost.get('currency') or 'USD'}"
                f"（price_table_version={cost['price_table_version']}）"
            )
        else:
            lines.append("- cost: unknown（未提供价格表）")
    return "\n".join(lines) + "\n"


def record_live_smoke(report: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    with target.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(smoke_markdown(report))


def resolve_api_key(env_name: str | None = None, *, profile: str | None = None) -> str:
    """live-smoke 密钥解析：--credentials profile 优先，回退 --api-key-env 环境变量。"""
    key = resolve_smoke_key(profile, env_name=env_name)
    if not key:
        hint = f"python -m motte_cli credentials set {profile}" if profile else f"环境变量 {env_name}"
        print(f"live-smoke 需要密钥（{hint}；密钥绝不落盘）", file=sys.stderr)
        raise SystemExit(2)
    return key


def resolve_smoke_key(profile: str | None, *, env_name: str | None = None) -> str | None:
    from motte_provider.credentials import resolve_api_key as resolve

    return resolve(profile, env_name=env_name)
