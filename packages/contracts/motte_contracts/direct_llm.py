"""版本化、离线的 Direct LLM 评测契约。无数据集下载、无模型调用。

Direct LLM 是「通用直连评测」：题面即 prompt，逐题直接调用模型，用确定性评分器判定是否通过。
v1 数据来自本地 JSONL（项目内也内置若干样例）；来源与许可证由 provenance 显式记录。契约保证的是：

- **导入即冻结**：整份 JSONL 逐行校验后落成不可变 ``数据集名@版本``，题数写进记录，
  运行时的分母只从记录里取，不靠运行时数行。
- **评分器显式版本化**：数据集声明一个 ``scorer``（``exact`` / ``contains`` / ``regex``），
  实现由 ``scorer_version`` 钉住；单题可在 ``metadata.scorer`` 覆盖数据集默认值。
- **无期望即无判定**：没有 ``expected`` 的题记为 ``no_expectation``，不进 accuracy 分母。

记录用顶层 ``eval`` 键区分套件（GSM8K 用 ``benchmark``）：``suite``/``id``/``version`` 三者同时
匹配才算本套件记录；不匹配的记录按普通资源处理，不执行 Direct LLM 严格 schema 校验。
所有带版本的数据集和场景仍由资源仓库统一执行 insert-only 不可变语义。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .dataset import validate_jsonl
from .identity import dataset_fingerprint
from .selection import digest

SUITE = "direct-llm"
# 数据集里的 eval 描述（id/version 一起构成套件判定，改动等于换契约）。
DATASET_ID = "direct-llm-prompts"
DATASET_VERSION = 1
CONTRACT_VERSION = 1
PLUGIN_VERSION = "1"
EVAL_KEY = "eval"
# 题面原样透传，不加套件专属后缀（GSM8K 的「以 #### 结尾」指令不适用于通用评测）。
PROMPT_VERSION = "direct-llm-verbatim-v1"
SCORER_VERSION = "direct-llm-answer-v1"
SCORERS = ("exact", "contains", "regex")
DEFAULT_SCORER = "exact"
SELECTION_ALL = "all-rows-in-file-order"
MAX_OUTPUT_TOKENS = 1024
MAX_RETRIES = 0
# 导入允许的输入键：题面二选一，其余可选。
_INPUT_KEYS = ("input", "prompt")
_OPTIONAL_KEYS = ("expected", "scorer", "case_id")
_NAME = re.compile(r"[0-9A-Za-z._-]{1,64}")


def _eval_of(record: Any) -> dict[str, Any] | None:
    """套件判定：``eval`` 描述的 suite/id/version 三者同时匹配才算已登记记录。"""
    if not isinstance(record, dict):
        return None
    eval_spec = record.get(EVAL_KEY)
    if not isinstance(eval_spec, dict):
        return None
    version = eval_spec.get("version")
    if (
        eval_spec.get("suite") != SUITE
        or eval_spec.get("id") != DATASET_ID
        or type(version) is not int
        or version != DATASET_VERSION
    ):
        return None
    return eval_spec


def is_dataset(record: Any) -> bool:
    return _eval_of(record) is not None and not isinstance(record.get("dataset"), str)


def is_scenario(record: Any) -> bool:
    return _eval_of(record) is not None and isinstance(record.get("dataset"), str)


def _preset(case_count: int, scorer: str) -> dict[str, Any]:
    return {
        "suite": SUITE,
        "id": DATASET_ID,
        "version": DATASET_VERSION,
        "selected_count": case_count,
        "selection": SELECTION_ALL,
        "scorer": scorer,
        "prompt_version": PROMPT_VERSION,
        "scorer_version": SCORER_VERSION,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_retries": MAX_RETRIES,
    }


def normalize_scorer(value: Any) -> str:
    if value in (None, ""):
        return DEFAULT_SCORER
    if value not in SCORERS:
        raise ValueError(f"unsupported scorer: {value!r} (expected one of {', '.join(SCORERS)})")
    return str(value)


def _case_id_of(raw: dict[str, Any], name: str, index: int) -> str:
    candidate = raw.get("case_id")
    if candidate in (None, ""):
        return f"{name}-{index:04d}"
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"invalid case_id at source line {index + 1}")
    return candidate.strip()


def _input_of(raw: dict[str, Any], line_no: int) -> str:
    present = [raw[key] for key in _INPUT_KEYS if key in raw]
    if len(present) != 1 or not isinstance(present[0], str) or not present[0].strip():
        raise ValueError(
            f"invalid input/prompt at source line {line_no}: exactly one non-empty "
            f"string of {', '.join(_INPUT_KEYS)} is required"
        )
    return present[0]


def _parse_lines(raw: bytes, name: str) -> list[dict[str, Any]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("source file must be UTF-8") from error
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as error:
            raise ValueError(f"invalid JSON at source line {line_no}") from error
        if not isinstance(row, dict):
            raise ValueError(f"invalid case at source line {line_no}: expected a JSON object")
        unknown = sorted(set(row) - set(_INPUT_KEYS) - set(_OPTIONAL_KEYS))
        if unknown:
            raise ValueError(f"unknown keys at source line {line_no}: {', '.join(unknown)}")
        expected = row.get("expected")
        if expected is not None and (not isinstance(expected, str) or not expected.strip()):
            raise ValueError(
                f"invalid expected at source line {line_no}: must be a non-empty string"
            )
        rows.append(
            {
                "case_id": _case_id_of(row, name, len(rows)),
                "input": _input_of(row, line_no),
                "expected": expected,
                "scorer": None
                if row.get("scorer") in (None, "")
                else normalize_scorer(row["scorer"]),
                "source_line": line_no,
            }
        )
    if not rows:
        raise ValueError("source file must contain at least one case")
    return rows


def import_direct_llm_jsonl(
    raw: bytes,
    *,
    name: str,
    version: str,
    license_id: str,
    scorer: str | None = None,
    source: str = "local-jsonl",
    synthetic: bool = False,
) -> dict[str, Any]:
    """校验整份本地 JSONL，落成不可变的 Direct LLM 数据集记录。

    每行是 ``{"input"|"prompt": str, "expected"?: str, "scorer"?: str, "case_id"?: str}``；
    未给 ``case_id`` 时按序生成 ``{name}-0000``。整份文件逐行校验，任何一行不合法即整体拒绝。
    """
    if not all(
        isinstance(value, str) and value.strip() for value in (name, version, license_id, source)
    ):
        raise ValueError("name, version, license and source are required")
    if not _NAME.fullmatch(name):
        raise ValueError("dataset name must be 1-64 characters of [0-9A-Za-z._-]")
    rows = _parse_lines(raw, name)
    dataset_scorer = normalize_scorer(scorer)
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if row["case_id"] in seen:
            raise ValueError(f"duplicate case_id: {row['case_id']}")
        seen.add(row["case_id"])
        metadata: dict[str, Any] = {"source_line": row["source_line"]}
        if row["scorer"] is not None:
            metadata["scorer"] = row["scorer"]
        case: dict[str, Any] = {
            "case_id": row["case_id"],
            "input": row["input"],
            "metadata": metadata,
        }
        if row["expected"] is not None:
            case["expected"] = row["expected"]
        cases.append(case)
    record = {
        "name": name,
        "version": version,
        EVAL_KEY: _preset(len(cases), dataset_scorer),
        "provenance": {
            "source": source,
            "license": license_id,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "source_line_count": len(rows),
            "synthetic": synthetic,
        },
        "cases": cases,
        "cases_sha256": digest(cases),
    }
    validate_dataset(record)
    return record


def _check_cases(cases: Any) -> list[Any]:
    if not isinstance(cases, list) or not cases:
        raise ValueError("dataset must contain at least one case")
    # 复用 canonical Case JSONL 校验（形状与重复 case_id）。
    validated = validate_jsonl("\n".join(json.dumps(case) for case in cases))
    lines: list[int] = []
    for case in validated.cases:
        metadata = case.metadata or {}
        unknown = sorted(set(metadata) - {"source_line", "scorer"})
        line = metadata.get("source_line")
        if (
            not case.case_id
            or not isinstance(case.input, str)
            or not case.input.strip()
            or unknown
            or type(line) is not int
            or line < 1
            or (
                case.expected is not None
                and (not isinstance(case.expected, str) or not case.expected.strip())
            )
        ):
            raise ValueError("invalid case/input/expected/source line")
        normalize_scorer(metadata.get("scorer"))
        lines.append(line)
    if lines != sorted(set(lines)):
        raise ValueError("source line numbers must be unique and ascending")
    return list(validated.cases)


def _validate_version_markers(record: dict[str, Any], *, scenario: bool) -> None:
    if "contract_version" in record:
        contract_version = record["contract_version"]
        if type(contract_version) is not int or contract_version != CONTRACT_VERSION:
            raise ValueError("direct-llm v1 contract_version must be the integer 1 when provided")
    if scenario:
        if "plugin_version" in record:
            plugin_version = record["plugin_version"]
            if not isinstance(plugin_version, str) or plugin_version != PLUGIN_VERSION:
                raise ValueError(
                    'direct-llm v1 plugin_version must be the string "1" when provided'
                )
    elif "plugin_version" in record:
        raise ValueError("direct-llm dataset cannot declare scenario plugin_version")


def validate_dataset(record: dict[str, Any]) -> None:
    _validate_version_markers(record, scenario=False)
    eval_spec = _eval_of(record)
    if eval_spec is None or isinstance(record.get("dataset"), str):
        raise ValueError("unsupported direct-llm dataset preset/version")
    if not record.get("name") or not record.get("version"):
        raise ValueError("dataset name/version are required")
    cases = _check_cases(record.get("cases"))
    dataset_scorer = normalize_scorer(eval_spec.get("scorer"))
    for case in cases:
        scorer = normalize_scorer((case.metadata or {}).get("scorer") or dataset_scorer)
        if scorer == "regex" and case.expected is not None:
            # 正则在导入期编译校验：运行期不会因为坏正则失败，评分是纯函数。
            try:
                re.compile(case.expected)
            except re.error as error:
                raise ValueError(f"invalid regex for case {case.case_id}: {error}") from error
    if record[EVAL_KEY] != _preset(len(cases), dataset_scorer):
        raise ValueError("dataset selected count does not match preset")
    if record.get("cases_sha256") != digest(record["cases"]):
        raise ValueError("selected cases hash mismatch")
    fingerprint = record.get("dataset_fingerprint")
    if fingerprint is not None and fingerprint != dataset_fingerprint(record):
        raise ValueError("dataset fingerprint mismatch")
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("source provenance is required")
    source_lines = provenance.get("source_line_count")
    if (
        type(source_lines) is not int
        or source_lines < len(cases)
        or type(provenance.get("synthetic")) is not bool
        or not all(
            isinstance(provenance.get(key), str) and provenance[key].strip()
            for key in ("source", "license")
        )
        or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("source_sha256", "")))
    ):
        raise ValueError("invalid source provenance")


def scenario_for(dataset: dict[str, Any], *, version: str) -> dict[str, Any]:
    """场景名与数据集名一致：Direct LLM 没有 gsm8k 那样的 scope 维度，一对一同版本映射。"""
    validate_dataset(dataset)
    return {
        "name": dataset["name"],
        "version": version,
        "mode": "direct-llm",
        EVAL_KEY: dict(dataset[EVAL_KEY]),
        "dataset": f"{dataset['name']}@{dataset['version']}",
    }


def validate_scenario(record: dict[str, Any]) -> None:
    _validate_version_markers(record, scenario=True)
    eval_spec = _eval_of(record)
    count = eval_spec.get("selected_count") if eval_spec is not None else None
    if (
        eval_spec is None
        or record.get("mode") != "direct-llm"
        or not record.get("name")
        or not record.get("version")
        or not isinstance(record.get("dataset"), str)
        or "@" not in record["dataset"]
        or type(count) is not int
        or count < 1
    ):
        raise ValueError("invalid direct-llm scenario/preset/dataset reference")
    normalize_scorer(eval_spec.get("scorer"))


def effective_scorers(dataset: dict[str, Any]) -> dict[str, str]:
    """每题的生效评分器：``metadata.scorer`` 覆盖数据集级默认值。"""
    dataset_scorer = normalize_scorer((dataset.get(EVAL_KEY) or {}).get("scorer"))
    return {
        case["case_id"]: normalize_scorer(
            (case.get("metadata") or {}).get("scorer") or dataset_scorer
        )
        for case in dataset["cases"]
    }
