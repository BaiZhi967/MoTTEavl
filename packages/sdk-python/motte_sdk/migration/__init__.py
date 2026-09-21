"""M7 历史迁移管线（协议 docs/protocols/sdk-and-migration.md frozen@1 §5）。

来源包加载与安全边界（sources）→ dry-run 计划（plan）→ checkpoint 化的
apply/resume（apply）→ 受限回退（rollback）。导入的 Run 只读不可执行：
终态、``origin=imported``、Dispatcher 拒领（协议 §5.3）。
"""
from .apply import SourceChangedError, apply_import
from .plan import imported_run_id, plan_import
from .rollback import RollbackNotConfirmed, rollback_import
from .sources import (
    LoadedSource,
    RejectedSourceRecord,
    SourcePackageError,
    SourceRecord,
    load_source_package,
    package_content_sha256,
)

__all__ = [
    "LoadedSource",
    "RejectedSourceRecord",
    "RollbackNotConfirmed",
    "SourceChangedError",
    "SourcePackageError",
    "SourceRecord",
    "apply_import",
    "imported_run_id",
    "load_source_package",
    "package_content_sha256",
    "plan_import",
    "rollback_import",
]
