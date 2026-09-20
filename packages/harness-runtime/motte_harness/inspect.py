"""M4-T11：Inspect 原始日志只读导入。

不执行 Inspect：无 task/solver/scorer/插件/pickle 加载路径；输入受限
（受控文本内容 + 大小/行数/评分器数量上限）；原始内容先 hash 冻结；
同内容重导幂等（import_id 由内容 hash 派生）；原生分数标记 imported
（score_source=inspect-native），与平台重评 pass 区分。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .parsers.inspect import (
    InspectLogError,
    PARSER_VERSION,
    SUPPORTED_SCHEMA,
    parse_inspect_log,
)

IMPORT_STORE_ROOT = Path("var/inspect-imports")


class InspectHarness:
    """只读导入门面（历史占位的 run() 不再提供 dry-run 假行为）。"""

    name = "inspect-importer"

    def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise InspectLogError(
            "INSPECT_EXECUTION_UNSUPPORTED",
            "inspect execution is not supported; use import_inspect_log for "
            "read-only log imports",
        )


def import_inspect_log(content: str, *, name: str | None = None) -> dict[str, Any]:
    """只读导入：解析 + 冻结 + 幂等登记（不进入 Dispatcher）。"""
    source_bytes = content.encode("utf-8")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    import_id = f"inspect-{source_sha256[:32]}"
    record_path = IMPORT_STORE_ROOT / f"{import_id}.json"

    parsed = parse_inspect_log(content)

    record: dict[str, Any] = {
        "import_id": import_id,
        "name": name,
        "source_sha256": f"sha256:{source_sha256}",
        "source_bytes": len(source_bytes),
        "schema": SUPPORTED_SCHEMA,
        "parser_version": PARSER_VERSION,
        "score_source": "inspect-native",
        "imported": True,
        "header_model": parsed["header"].get("model"),
        "samples": parsed["samples"],
        "sample_count": len(parsed["samples"]),
        "coverage": parsed["coverage"],
    }
    idempotent = record_path.exists()
    if idempotent:
        existing = json.loads(record_path.read_text(encoding="utf-8"))
        if existing.get("source_sha256") != record["source_sha256"]:
            raise InspectLogError(
                "IMPORT_CONFLICT",
                f"import id collision with different content: {import_id}",
            )
    else:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8",
        )
    return {**record, "idempotent": idempotent}


def load_import(import_id: str) -> dict[str, Any] | None:
    if not import_id.startswith("inspect-") or "/" in import_id or "\\" in import_id:
        return None
    path = IMPORT_STORE_ROOT / f"{import_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "IMPORT_STORE_ROOT",
    "InspectHarness",
    "InspectLogError",
    "import_inspect_log",
    "load_import",
]
