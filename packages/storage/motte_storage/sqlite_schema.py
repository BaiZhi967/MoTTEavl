"""SQLite 建表与旧库对齐：一份实现，避免各模块各自写不完整的补列逻辑。

背景（验收 F-01）：SQLite 侧没有 Alembic，表由各模块的 CREATE TABLE IF NOT
EXISTS 建立，因此**既有库永远保持它创建时的形状**。各模块曾各自写补列代码，
一旦演进没被覆盖（旧 score_sets 已经有 metric_id 却没有 trial_id；旧 trials /
external_jobs 的主键都不同），补列既不完整也无法修正主键——结果是一个
"能跑完、结算不了"的部署（实测 no such column: repeat_index 与 table
score_sets has no column named trial_id）。

本模块给出唯一入口 create_and_upgrade：建缺失表；把形状不符的既有表对齐到当前
DDL。只做两件事，且都是保数据的：

1. **只缺列且主键一致** → 逐列 ALTER TABLE ... ADD COLUMN（便宜，不动数据）；
2. **主键不同或无法补列** → 按当前 DDL 重建表，按下面的规则搬运旧行。

重建的搬运规则（声明式的，不做猜测）：

* 共有列原样搬过来；
* 目标列在旧表里不存在、又声明了默认值 → 不写进 INSERT，由 DDL 默认值补齐；
* 目标列在旧表里不存在、且没有默认值，同时是 NOT NULL 或主键成员 → 必须有确定
  来源，否则整表对齐失败（宁可拒绝启动，也不伪造一行）。来源按顺序 COALESCE：

  1. 调用方给的 column_sources（模块知道自己旧列叫什么，例如 trial_id <- id）；
  2. 旧行的 payload JSON 里同名键（旧写入本来就把它完整存进 payload）；
  3. 该列类型的中性常量（文本空串 / 数值 0）。

  这三步都取不到"业务真值"，只是在旧行确实没带这个字段时给出一个可读的占位，
  并保留 payload 原文可追溯。

重建时索引必须显式处理：ALTER TABLE ... RENAME 会把旧索引留在被改名的表上，
CREATE INDEX IF NOT EXISTS 因此会跳过它们，最后随旧表一起被删掉。所以索引在
重建后按参考 schema 重新建一遍。
"""
from __future__ import annotations

import sqlite3

__all__ = ["add_missing_columns", "align_legacy_tables", "create_and_upgrade"]

_NUMERIC_TYPES = ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC")


def _shape(connection: sqlite3.Connection, table: str):
    """(列, 主键列, PRAGMA 行)；表不存在时返回空。"""
    rows = [dict(zip(("cid", "name", "type", "notnull", "default", "pk"), row))
            for row in connection.execute("PRAGMA table_info(" + table + ")")]
    columns = [row["name"] for row in rows]
    primary = [row["name"] for row in sorted((r for r in rows if r["pk"]), key=lambda r: r["pk"])]
    return columns, primary, rows


def _reference(connection: sqlite3.Connection, ddl: str):
    """在内存库里执行一次当前 DDL，读出每张表的权威形状与索引。"""
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(ddl)
        tables = [row[0] for row in reference.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        shapes = {}
        for table in tables:
            columns, primary, rows = _shape(reference, table)
            create = reference.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            indexes = [
                {"name": row[0], "sql": row[1]} for row in reference.execute(
                    "SELECT name, sql FROM sqlite_master"
                    " WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                    (table,),
                )
            ]
            shapes[table] = {
                "columns": columns, "primary": primary, "rows": rows,
                "create": create[0] if create else None, "indexes": indexes,
            }
        return shapes
    finally:
        reference.close()


def _addable(row: dict) -> bool:
    """NOT NULL 且没有默认值的列不能用 ALTER 补（SQLite 会说 no default value）。"""
    if not row["notnull"]:
        return True
    return row["default"] is not None


def _needs_fill(row: dict, shape: dict) -> bool:
    """重建时必须显式给值的列。

    SQLite 对 TEXT PRIMARY KEY 报告 notnull=0（历史兼容：非 INTEGER 主键允许
    NULL），所以只看 notnull 会让主键列被漏掉、整表搬成一行 NULL 主键。声明了
    默认值的列不在此列——省略即由 DDL 默认值补齐。
    """
    if row["default"] is not None:
        return False
    return bool(row["notnull"]) or row["name"] in shape["primary"]


def _neutral_constant(row: dict) -> str:
    declared = str(row["type"] or "").upper()
    return "0" if any(declared.startswith(name) for name in _NUMERIC_TYPES) else "''"


def _fill_expression(row: dict, source_columns, sources) -> str:
    """缺失且 NOT NULL 无默认值的列：给出有据可查的取值表达式。"""
    name = row["name"]
    candidates = []
    if name in sources:
        source = sources[name]
        if source not in source_columns:
            raise ValueError(
                "column source " + source + " for " + name + " does not exist in the legacy table"
            )
        candidates.append(source)
    if "payload" in source_columns:
        candidates.append("json_extract(payload, '$." + name + "')")
    candidates.append(_neutral_constant(row))
    return "COALESCE(" + ", ".join(candidates) + ")"


def add_missing_columns(connection: sqlite3.Connection, table: str, shape: dict):
    """补齐缺失列（主键必须已经一致）；返回补过的列名。"""
    columns, _, _ = _shape(connection, table)
    added = []
    for row in shape["rows"]:
        if row["name"] in columns:
            continue
        if not _addable(row):
            raise ValueError(
                table + "." + row["name"]
                + " is NOT NULL without a default; a table rebuild is required"
            )
        default = "" if row["default"] is None else " DEFAULT " + str(row["default"])
        connection.execute(
            "ALTER TABLE " + table + " ADD COLUMN " + row["name"] + " " + row["type"] + default
        )
        added.append(row["name"])
    return added


def _rebuild(connection: sqlite3.Connection, table: str, shape: dict, sources):
    """按当前 DDL 重建表；返回搬过来的列名。"""
    columns, _, _ = _shape(connection, table)
    shared = [name for name in shape["columns"] if name in columns]
    filled = [
        row for row in shape["rows"]
        if row["name"] not in columns and _needs_fill(row, shape)
    ]
    targets = shared + [row["name"] for row in filled]
    expressions = [name for name in shared]
    expressions += [_fill_expression(row, columns, sources) for row in filled]
    legacy = table + "__legacy"
    for index in shape["indexes"]:
        connection.execute("DROP INDEX IF EXISTS " + index["name"])
    connection.execute("ALTER TABLE " + table + " RENAME TO " + legacy)
    connection.execute(shape["create"])
    if targets:
        connection.execute(
            "INSERT INTO " + table + " (" + ", ".join(targets) + ")"
            " SELECT " + ", ".join(expressions) + " FROM " + legacy
        )
    connection.execute("DROP TABLE " + legacy)
    for index in shape["indexes"]:
        connection.execute(index["sql"])
    return targets


def align_legacy_tables(connection: sqlite3.Connection, ddl: str, column_sources=None):
    """把既有表对齐到 ddl 的形状；调用方负责 DDL 已经执行过。"""
    sources = dict(column_sources or {})
    shapes = _reference(connection, ddl)
    plan = []
    for table, shape in sorted(shapes.items()):
        columns, primary, _ = _shape(connection, table)
        if not columns:                      # 表刚由 DDL 建好
            continue
        missing = [name for name in shape["columns"] if name not in columns]
        if not missing and primary == shape["primary"]:
            continue
        rebuild = primary != shape["primary"] or any(
            row["name"] not in columns and not _addable(row) for row in shape["rows"]
        )
        plan.append((table, shape, "rebuild" if rebuild else "add_columns"))
    if not plan:
        return []
    connection.execute("PRAGMA foreign_keys=OFF")
    # 旧行为：RENAME 不重写其他表里的外键引用，否则重建 runs 会留下悬空引用。
    connection.execute("PRAGMA legacy_alter_table=ON")
    upgraded = []
    try:
        with connection:
            for table, shape, mode in plan:
                if mode == "add_columns":
                    changed = add_missing_columns(connection, table, shape)
                else:
                    changed = _rebuild(connection, table, shape, sources.get(table, {}))
                upgraded.append({"table": table, "mode": mode, "columns": changed})
    finally:
        connection.execute("PRAGMA legacy_alter_table=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
    return upgraded


def create_and_upgrade(connection: sqlite3.Connection, ddl: str, column_sources=None):
    """建缺失表并把形状不符的既有表对齐到 ddl；返回对齐记录（可审计）。"""
    connection.executescript(ddl)
    return align_legacy_tables(connection, ddl, column_sources)
