"""M5-R9 验证工具：按失败 node ID 集合比较两个提交，而不是比较失败总数。

审查指出："开发汇报的当前 316 failed 对基线 315 failed，只是失败总数净差；
不是只有一个新增失败的证明。必须比较失败 node ID 集合，并对新增／消失／共同
失败逐项归因。"

用法：
    uv run python scripts/m5_failure_diff.py --base base.xml --head head.xml [--json out.json]

两个 XML 由 pytest --junitxml 生成。输出：
- added    = head 失败 − base 失败   （本轮引入的失败，必须逐项归因或修复）
- removed  = base 失败 − head 失败   （本轮修好的失败）
- common   = base 失败 ∩ head 失败   （既有失败）
- counts   = 三类的数量与 base/head 各自总数
退出码：added 非空时为 1（新增失败必须在验收前处理或给出环境原因）。
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def failed_node_ids(path: str | Path) -> set[str]:
    """从 JUnit XML 提取失败的 node id 集合。

    只把非通过、非跳过的用例算作失败：skipped 与 expected-failure 不算。
    没有 testcase 的 suite 级 error 也会被计入（用 classname::name 或文件名）。
    """
    tree = ET.parse(path)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    failures: set[str] = set()
    for suite in suites:
        for case in suite.iter("testcase"):
            bad = any(child.tag in ("failure", "error") for child in case)
            if not bad:
                continue
            classname = case.get("classname") or ""
            name = case.get("name") or ""
            node = f"{classname}::{name}" if classname else name
            failures.add(node)
    return failures


def diff_sets(base: set[str], head: set[str]) -> dict[str, list[str]]:
    return {
        "added": sorted(head - base),
        "removed": sorted(base - head),
        "common": sorted(base & head),
    }


def render(result: dict[str, list[str]], base_total: int, head_total: int) -> str:
    lines = [
        f"base failures: {base_total}",
        f"head failures: {head_total}",
        f"net delta:     {head_total - base_total:+d}  (NOT evidence on its own)",
        "",
        f"added   ({len(result['added'])}): failures introduced by this commit",
        *[f"  + {node}" for node in result["added"]],
        f"removed ({len(result['removed'])}): failures fixed by this commit",
        *[f"  - {node}" for node in result["removed"]],
        f"common  ({len(result['common'])}): pre-existing failures",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="compare pytest JUnit failure sets")
    parser.add_argument("--base", required=True, help="JUnit XML of the clean baseline")
    parser.add_argument("--head", required=True, help="JUnit XML of the candidate commit")
    parser.add_argument("--json", dest="json_out", default=None)
    args = parser.parse_args(argv)

    base = failed_node_ids(args.base)
    head = failed_node_ids(args.head)
    result = diff_sets(base, head)
    text = render(result, len(base), len(head))
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {"base_total": len(base), "head_total": len(head), **result},
                ensure_ascii=False, indent=2, sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
    return 1 if result["added"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
