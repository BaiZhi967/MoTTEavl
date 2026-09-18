"""Versioned, offline GSM8K benchmark contract. No dataset download or model calls.

两个 scope（预设）：

- ``smoke``：官方 test split 的前 20 题（``gsm8k-20``），流水线冒烟用，成本可控。
- ``full``：官方 test split 的全部题目（``gsm8k-full``），题数等于源文件行数，
  在导入时写死进不可变数据集记录，运行时的分母只从记录里取。

数据集版本不可变；每次运行可以在 ``manifest.case_selection`` 里再选一个子集
（``all`` / ``ids`` / ``random``），选中结果与随机种子写进运行快照，报告分母用运行自己的题数。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
from decimal import Decimal
from typing import Any

from .dataset import validate_jsonl

SCORER_VERSION = "gsm8k-final-decimal-v1"
PROMPT_VERSION = "gsm8k-zero-shot-v1"
SMOKE_COUNT = 20
SELECTION_FIRST_N = "first-n-in-file-order"
SELECTION_ALL = "all-rows-in-file-order"
# 运行级题目选择（manifest.case_selection）与它写进 provenance 的元数据键。
CASE_SELECTION_KEY = "case_selection"
RUN_SELECTION_KEY = "run_selection"
CASE_SELECTION_MODES = ("all", "ids", "random")
_SEED = re.compile(r"[0-9a-fA-F]{8,64}")


def _preset(benchmark_id: str, selected_count: int | None, selection: str) -> dict[str, Any]:
    return {
        "id": benchmark_id, "version": 1, "selected_count": selected_count, "selection": selection,
        "prompt_version": PROMPT_VERSION, "scorer_version": SCORER_VERSION,
        "max_output_tokens": 1024, "max_retries": 0,
    }


# scope → 预设模板；full 的 selected_count 在导入时确定为源文件行数。
PRESETS: dict[str, dict[str, Any]] = {
    "smoke": _preset("gsm8k-20", SMOKE_COUNT, SELECTION_FIRST_N),
    "full": _preset("gsm8k-full", None, SELECTION_ALL),
}
SCOPES = tuple(PRESETS)
# 场景名后缀：数据集名 + 后缀 = 场景名，overview 与 UI 靠它区分 scope。
SCENARIO_SUFFIX = {"smoke": "smoke", "full": "full"}
# 兼容导出：既有调用点仍指冒烟预设。
BENCHMARK = PRESETS["smoke"]["id"]
PRESET = PRESETS["smoke"]

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


def preset_for(scope: str) -> dict[str, Any]:
    preset = PRESETS.get(scope)
    if preset is None:
        raise ValueError(f"unsupported benchmark scope: {scope}")
    return preset


def expected_benchmark(scope: str, case_count: int) -> dict[str, Any]:
    """该 scope 的数据集必须携带的 benchmark 描述；full 的题数由实际选中数决定。"""
    preset = dict(preset_for(scope))
    if preset["selection"] == SELECTION_ALL:
        if case_count < 1:
            raise ValueError("full scope requires at least one case")
        preset["selected_count"] = case_count
    elif case_count != preset["selected_count"]:
        raise ValueError("dataset selected count does not match preset")
    return preset


def scope_of(record: dict[str, Any]) -> str | None:
    """从记录（数据集或场景）的 benchmark 描述反查 scope，未知预设返回 None。"""
    benchmark = record.get("benchmark")
    if not isinstance(benchmark, dict):
        return None
    for scope, preset in PRESETS.items():
        if benchmark.get("id") == preset["id"] and benchmark.get("version") == preset["version"]:
            return scope
    return None


def scenario_name(dataset_name: str, scope: str) -> str:
    preset_for(scope)
    return f"{dataset_name}-{SCENARIO_SUFFIX[scope]}"


def is_benchmark_scenario(name: Any) -> bool:
    """基准场景名的唯一判定：以已登记的 scope 后缀结尾。"""
    return isinstance(name, str) and any(
        name.endswith(f"-{suffix}") for suffix in set(SCENARIO_SUFFIX.values()))


def is_benchmark(record: dict[str, Any]) -> bool:
    return scope_of(record) is not None


def select_cases(dataset: dict[str, Any], selection: Any = None) -> dict[str, Any]:
    """把运行级题目选择解析成确定性结果 + 可复核元数据。

    ``selection`` 省略/None 表示整份数据集：

    - ``{"mode": "all"}``
    - ``{"mode": "ids", "case_ids": [...]}``（必须都在数据集里，重复项自动去重，按数据集顺序输出）
    - ``{"mode": "random", "count": N, "seed": "hex"}``（省略 seed 时生成并回填）

    随机抽样用 ``random.Random(int(seed, 16))`` 播种，同一 seed + 同一数据集必得同一子集，
    因此运行快照里的 seed 就是复现凭据。返回 ``{"mode","count","case_ids","seed","case_ids_sha256"}``。
    """
    all_ids = [case["case_id"] for case in dataset["cases"]]
    if selection is None:
        selection = {"mode": "all"}
    if not isinstance(selection, dict):
        raise ValueError(f"{CASE_SELECTION_KEY} must be an object")
    mode = selection.get("mode") or "all"
    seed: str | None = None
    if mode == "all":
        selected = list(all_ids)
    elif mode == "ids":
        requested = selection.get("case_ids")
        if not isinstance(requested, list) or not requested:
            raise ValueError(f"{CASE_SELECTION_KEY}.case_ids must be a non-empty list")
        known = set(all_ids)
        unknown = next((item for item in requested
                        if not isinstance(item, str) or item not in known), None)
        if unknown is not None:
            raise ValueError(f"case not in dataset: {unknown!r}")
        wanted = set(requested)
        selected = [case_id for case_id in all_ids if case_id in wanted]
    elif mode == "random":
        count = selection.get("count")
        if type(count) is not int or not 0 < count <= len(all_ids):
            raise ValueError(f"{CASE_SELECTION_KEY}.count must be between 1 and {len(all_ids)}")
        candidate = selection.get("seed")
        if candidate in (None, ""):
            seed = secrets.token_hex(8)
        elif isinstance(candidate, str) and _SEED.fullmatch(candidate):
            seed = candidate
        else:
            raise ValueError(f"{CASE_SELECTION_KEY}.seed must be 8-64 hex characters")
        picked = set(random.Random(int(seed, 16)).sample(all_ids, count))
        selected = [case_id for case_id in all_ids if case_id in picked]
    else:
        raise ValueError(f"unsupported {CASE_SELECTION_KEY} mode: {mode}")
    return {"mode": mode, "count": len(selected), "case_ids": selected, "seed": seed,
            "case_ids_sha256": digest(selected)}


def _positive_int(value: Any) -> int | None:
    # bool 是 int 的子类，必须排除，否则 True 会被当成 1 题分母。
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def run_selected_count(provenance: Any) -> int | None:
    """运行的实际分母（题数）：优先运行级 run_selection.count，旧运行回落到数据集级 selected_count。"""
    if not isinstance(provenance, dict):
        return None
    run_selection = provenance.get(RUN_SELECTION_KEY)
    if isinstance(run_selection, dict):
        count = _positive_int(run_selection.get("count"))
        if count is not None:
            return count
    return _positive_int(provenance.get("selected_count"))


def import_official_jsonl(
    raw: bytes, *, name: str, version: str, revision: str, license_id: str, scope: str = "smoke",
    source: str = "https://github.com/openai/grade-school-math",
    synthetic: bool = False,
) -> dict[str, Any]:
    """Validate the entire local official-format test file, then select rows for the scope."""
    if not all(isinstance(v, str) and v.strip() for v in (name, version, revision, license_id, source)):
        raise ValueError("name, version, source, pinned revision and license are required")
    preset = preset_for(scope)
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
    if preset["selection"] == SELECTION_ALL:
        selected = rows
    else:
        count = preset["selected_count"]
        if len(rows) < count:
            raise ValueError(f"source must contain at least {count} cases")
        selected = rows[:count]
    benchmark = expected_benchmark(scope, len(selected))
    cases = [{"case_id": f"gsm8k-test-{i:04d}", "input": row["question"],
              "expected": format(final_number(row["answer"]), "f"),
              "metadata": {"source_line": i + 1}} for i, row in enumerate(selected)]
    record = {
        "name": name, "version": version, "benchmark": benchmark,
        "provenance": {"source": source, "revision": revision, "license": license_id,
                       "split": "test", "source_sha256": hashlib.sha256(raw).hexdigest(),
                       "source_line_count": len(rows), "synthetic": synthetic},
        "cases": cases, "cases_sha256": digest(cases),
    }
    validate_dataset(record)
    return record


def validate_dataset(record: dict[str, Any]) -> None:
    scope = scope_of(record)
    if scope is None:
        raise ValueError("unsupported benchmark preset/version")
    if not record.get("name") or not record.get("version"):
        raise ValueError("dataset name/version are required")
    cases = record.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("dataset selected count does not match preset")
    if record["benchmark"] != expected_benchmark(scope, len(cases)):
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
    source_lines, selected = provenance.get("source_line_count"), len(cases)
    selected_all = record["benchmark"]["selection"] == SELECTION_ALL
    line_count_ok = (source_lines == selected if selected_all
                     else type(source_lines) is int and source_lines >= selected)
    if (provenance.get("split") != "test" or not line_count_ok
            or type(provenance.get("synthetic")) is not bool
            or not all(isinstance(provenance.get(k), str) and provenance[k].strip()
                       for k in ("source", "revision", "license"))
            or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("source_sha256", "")))):
        raise ValueError("invalid source provenance")
    if not provenance["synthetic"] and not re.fullmatch(r"[0-9a-fA-F]{40}", provenance["revision"]):
        raise ValueError("official revision must be a pinned commit hash")


def scenario_for(dataset: dict[str, Any], *, name: str, version: str) -> dict[str, Any]:
    validate_dataset(dataset)
    return {"name": name, "version": version, "mode": "direct-llm",
            "benchmark": dict(dataset["benchmark"]),
            "dataset": f"{dataset['name']}@{dataset['version']}"}


def validate_scenario(record: dict[str, Any]) -> None:
    benchmark = record.get("benchmark")
    count = benchmark.get("selected_count") if isinstance(benchmark, dict) else None
    if (scope_of(record) is None or record.get("mode") != "direct-llm"
            or not record.get("name") or not record.get("version")
            or not isinstance(record.get("dataset"), str) or "@" not in record["dataset"]
            or type(count) is not int or count < 1):
        raise ValueError("invalid benchmark scenario/preset/dataset reference")
