import json
from dataclasses import dataclass


@dataclass(frozen=True)
class PriceTable:
    """一次请求发生时的价格快照；未知价格保持 None，绝不猜测。"""

    version: str
    input_per_million: float | None = None
    output_per_million: float | None = None


def parse_price_table(data: dict | None) -> PriceTable | None:
    if not data:
        return None
    version = data.get("version")
    if not version:
        raise ValueError("price table requires a version")
    return PriceTable(
        version=str(version),
        input_per_million=_optional_price(data.get("input_per_million")),
        output_per_million=_optional_price(data.get("output_per_million")),
    )


def load_price_table(path: str) -> PriceTable:
    with open(path, encoding="utf-8") as handle:
        return parse_price_table(json.load(handle))


def _optional_price(value) -> float | None:
    if value is None:
        return None
    price = float(value)
    if price < 0:
        raise ValueError("price cannot be negative")
    return price


def estimate_cost(table: PriceTable | None, usage: dict) -> float | None:
    if table is None or table.input_per_million is None or table.output_per_million is None:
        return None
    return (
        usage.get("prompt_tokens", 0) * table.input_per_million
        + usage.get("completion_tokens", 0) * table.output_per_million
    ) / 1_000_000


def cost_detail(table: PriceTable | None, usage: dict) -> dict | None:
    """成本明细；价格未知时返回 None（显示 unknown），并保留 price_table_version 供追溯。"""
    if table is None:
        return None
    total = estimate_cost(table, usage)
    if total is None:
        return None
    return {
        "total": round(total, 8),
        "price_table_version": table.version,
        "input_per_million": table.input_per_million,
        "output_per_million": table.output_per_million,
    }
