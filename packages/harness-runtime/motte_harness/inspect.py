"""M4-T11：Inspect 原生 EvalLog 只读导入。

不执行 Inspect：无 task/solver/scorer/插件/pickle 加载路径；输入受限
（受控文本内容 + 大小/样本/评分器数量上限）；**原始内容整体冻结**到
导入仓（``<import_id>.source``，可复核/重解析，M4 review R28）；导入身份
由稳定源身份（eval 任务/创建时间/模型/样本数）派生——同身份同内容重导
幂等，同身份**异内容**冲突（IMPORT_IDENTITY_CONFLICT），不再把改分后的
同一来源当作新导入；原生分数标记 imported（score_source=inspect-native），
与平台重评 pass 区分。
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


def _import_id_for(identity: dict[str, Any], source_sha256: str) -> str:
    """身份键派生 import_id：只依赖稳定源身份，不随内容微调漂移。

    内容 hash 单独保存在记录里——同身份不同内容构成
    IMPORT_IDENTITY_CONFLICT，而不是静默产生新导入（M4 review R28）。
    """
    del source_sha256
    identity_text = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()
    return f"inspect-{digest[:32]}"


def import_inspect_log(content: str, *, name: str | None = None) -> dict[str, Any]:
    """只读导入：解析 + 冻结 + 幂等登记（不进入 Dispatcher）。"""
    source_bytes = content.encode("utf-8")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    parsed = parse_inspect_log(content)
    identity = dict(parsed["identity"])
    import_id = _import_id_for(identity, source_sha256)
    record_path = IMPORT_STORE_ROOT / f"{import_id}.json"
    raw_path = IMPORT_STORE_ROOT / f"{import_id}.source"

    record: dict[str, Any] = {
        "import_id": import_id,
        "name": name,
        "source_sha256": f"sha256:{source_sha256}",
        "source_bytes": len(source_bytes),
        "source_identity": identity,
        "raw_frozen": str(raw_path),
        "raw_sha256": f"sha256:{source_sha256}",
        "schema": SUPPORTED_SCHEMA,
        "parser_version": PARSER_VERSION,
        "score_source": "inspect-native",
        "imported": True,
        "header_model": parsed["header"].get("model"),
        "header_status": parsed["header"].get("status"),
        "samples": parsed["samples"],
        "sample_count": len(parsed["samples"]),
        "unknown_fields": parsed["unknown_fields"],
        "coverage": parsed["coverage"],
    }
    idempotent = record_path.exists()
    if idempotent:
        existing = json.loads(record_path.read_text(encoding="utf-8"))
        if existing.get("source_sha256") != record["source_sha256"]:
            raise InspectLogError(
                "IMPORT_IDENTITY_CONFLICT",
                f"the same inspect log identity was already imported with "
                f"different content: {import_id} "
                f"(existing {existing.get('source_sha256')}, "
                f"incoming {record['source_sha256']})",
            )
        return {**existing, "idempotent": True}
    record_path.parent.mkdir(parents=True, exist_ok=True)
    # 原始内容先冻结（R28）：复核/重解析的单一事实源。
    raw_path.write_text(content, encoding="utf-8")
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )
    return {**record, "idempotent": False}


def load_import(import_id: str) -> dict[str, Any] | None:
    if not import_id.startswith("inspect-") or "/" in import_id or "\\" in import_id:
        return None
    path = IMPORT_STORE_ROOT / f"{import_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_import_raw(import_id: str) -> str | None:
    """读取冻结的原始日志内容（只读复核入口，R28）。"""
    if not import_id.startswith("inspect-") or "/" in import_id or "\\" in import_id:
        return None
    path = IMPORT_STORE_ROOT / f"{import_id}.source"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


__all__ = [
    "IMPORT_STORE_ROOT",
    "InspectHarness",
    "InspectLogError",
    "import_inspect_log",
    "load_import",
    "load_import_raw",
]
