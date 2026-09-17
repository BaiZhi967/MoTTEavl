"""Versioned, offline GSM8K smoke contract. No dataset download or model calls."""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Any

from .dataset import validate_jsonl

BENCHMARK = "gsm8k-20"
PROMPT_VERSION = "gsm8k-zero-shot-v1"
SCORER_VERSION = "gsm8k-final-decimal-v1"
PRESET: dict[str, Any] = {
    "id": BENCHMARK,
    "version": 1,
    "selected_count": 20,
    "selection": "first-n-in-file-order",
    "prompt_version": PROMPT_VERSION,
    "scorer_version": SCORER_VERSION,
    "max_output_tokens": 1024,
    "max_retries": 0,
}
# ASCII digits only; commas must group exactly three digits. No exponent/currency/fractions.
_NUMBER = r"[+-]?(?:(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?|\.[0-9]+)"
_FINAL = re.compile(r"####[ \t]+(" + _NUMBER + r")[ \t]*")


def final_number(text: Any) -> Decimal | None:
    """Parse only the final nonblank line, never guess from intermediate numbers."""
    if not isinstance(text, str) or not text.strip():
        return None
    match = _FINAL.fullmatch(text.rstrip().splitlines()[-1])
    return Decimal(match[1].replace(",", "")) if match else None


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def build_prompt(question: str) -> str:
    return question + "\n\nSolve the problem step by step. End with a final line in exactly this format: #### <number>"


def is_benchmark(record: dict[str, Any]) -> bool:
    benchmark = record.get("benchmark")
    return isinstance(benchmark, dict) and benchmark.get("id") == BENCHMARK


def import_official_jsonl(
    raw: bytes, *, name: str, version: str, revision: str, license_id: str,
    source: str = "https://github.com/openai/grade-school-math",
    synthetic: bool = False,
) -> dict[str, Any]:
    """Validate the entire local official-format test file, select the first preset N rows."""
    if not all(isinstance(v, str) and v.strip() for v in (name, version, revision, license_id, source)):
        raise ValueError("name, version, source, pinned revision and license are required")
    if not synthetic and not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ValueError("official source revision must be a pinned 40-character commit hash")
    rows = []
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        try:
            row = json.loads(line)
        except ValueError as error:
            raise ValueError(f"invalid JSON at source line {line_no}") from error
        if (not isinstance(row, dict) or set(row) != {"question", "answer"}
                or not isinstance(row["question"], str) or not row["question"].strip()
                or final_number(row["answer"]) is None):
            raise ValueError(f"invalid official question/answer at source line {line_no}")
        rows.append(row)
    count = PRESET["selected_count"]
    if len(rows) < count:
        raise ValueError(f"source must contain at least {count} cases")
    cases = [{"case_id": f"gsm8k-test-{i:04d}", "input": row["question"],
              "expected": format(final_number(row["answer"]), "f"),
              "metadata": {"source_line": i + 1}} for i, row in enumerate(rows[:count])]
    record = {
        "name": name, "version": version, "benchmark": dict(PRESET),
        "provenance": {"source": source, "revision": revision, "license": license_id,
                       "split": "test", "source_sha256": hashlib.sha256(raw).hexdigest(),
                       "source_line_count": len(rows), "synthetic": synthetic},
        "cases": cases, "cases_sha256": digest(cases),
    }
    validate_dataset(record)
    return record


def validate_dataset(record: dict[str, Any]) -> None:
    if record.get("benchmark") != PRESET:
        raise ValueError("unsupported benchmark preset/version")
    if not record.get("name") or not record.get("version"):
        raise ValueError("dataset name/version are required")
    cases = record.get("cases")
    if not isinstance(cases, list) or len(cases) != PRESET["selected_count"]:
        raise ValueError("dataset selected count does not match preset")
    # Reuse the canonical Case JSONL validator (schema and duplicate ids).
    validated = validate_jsonl("\n".join(json.dumps(case) for case in cases))
    for i, case in enumerate(validated.cases):
        if (case.case_id != f"gsm8k-test-{i:04d}" or not isinstance(case.input, str)
                or not case.input.strip() or final_number(f"#### {case.expected}") is None
                or case.metadata != {"source_line": i + 1}):
            raise ValueError("invalid selected case/order/expected/source line")
    if record.get("cases_sha256") != digest(cases):
        raise ValueError("selected cases hash mismatch")
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("source provenance is required")
    if (provenance.get("split") != "test"
            or type(provenance.get("source_line_count")) is not int
            or provenance["source_line_count"] < len(cases)
            or type(provenance.get("synthetic")) is not bool
            or not all(isinstance(provenance.get(k), str) and provenance[k].strip()
                       for k in ("source", "revision", "license"))
            or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("source_sha256", "")))):
        raise ValueError("invalid source provenance")
    if not provenance["synthetic"] and not re.fullmatch(r"[0-9a-fA-F]{40}", provenance["revision"]):
        raise ValueError("official revision must be a pinned commit hash")


def scenario_for(dataset: dict[str, Any], *, name: str, version: str) -> dict[str, Any]:
    validate_dataset(dataset)
    return {"name": name, "version": version, "mode": "direct-llm", "benchmark": dict(PRESET),
            "dataset": f"{dataset['name']}@{dataset['version']}"}


def validate_scenario(record: dict[str, Any]) -> None:
    if (record.get("benchmark") != PRESET or record.get("mode") != "direct-llm"
            or not record.get("name") or not record.get("version")
            or not isinstance(record.get("dataset"), str) or "@" not in record["dataset"]):
        raise ValueError("invalid benchmark scenario/preset/dataset reference")
