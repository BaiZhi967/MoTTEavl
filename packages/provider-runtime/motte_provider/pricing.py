from dataclasses import dataclass


@dataclass(frozen=True)
class PriceTable:
    version: str
    input_per_million: float
    output_per_million: float


def estimate_cost(table, usage):
    return (
        usage.get("prompt_tokens", 0) * table.input_per_million
        + usage.get("completion_tokens", 0) * table.output_per_million
    ) / 1_000_000
