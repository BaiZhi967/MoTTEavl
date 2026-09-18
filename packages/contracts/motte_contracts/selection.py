"""运行级题目选择的共享契约（GSM8K 与 Direct LLM 套件共用，纯函数、无 IO）。

数据集规模在导入时确定且不可变；每次运行可以在 ``manifest.case_selection`` 里再选一个子集
（``all`` / ``ids`` / ``random``），选中结果与随机种子写进运行快照，报告分母用运行自己的题数。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
from typing import Any

# 运行级题目选择（manifest.case_selection）与它写进 provenance 的元数据键。
CASE_SELECTION_KEY = "case_selection"
RUN_SELECTION_KEY = "run_selection"
CASE_SELECTION_MODES = ("all", "ids", "random")
_SEED = re.compile(r"[0-9a-fA-F]{8,64}")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


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
