"""合成假 Runner：测试与离线验收的确定性进程 Job 实现。

不是官方 C-Eval/CMMLU 内容；只产出可预测的受控输出与副作用，用于验证
Job 生命周期、超时、取消与采集边界。行为由环境变量驱动：

- ``MOTTE_WORK_DIR``（必需）：受控工作目录，输出写在这里。
- ``MOTTE_FAKE_MODE``：ok（4 条全对，exit 0）/ fail（3 条部分结果，exit 1）/
  noise（stdout 超 2 MiB 后按 ok）/ spawn_child（派生 sleeper 子进程后挂起）/
  slow_ok（1.5s 后写 1 条，exit 0）/ hang（挂起）/ opencompass_ok（写
  OpenCompass 形态输出树，exit 0）/ opencompass_partial（按 runner-config
  的前 N 题写输出树，exit 3）。
- ``MOTTE_FAKE_DATASET``：opencompass_* 模式的命名空间（ceval/cmmlu，
  默认 ceval），决定结果文件名前缀与学科。
- ``MOTTE_FAKE_PREDICTIONS``：opencompass_partial 模式的逐题预测（逗号
  分隔，如 ``B,C,A``），不足的题目留空。
- ``MOTTE_FAKE_SUBJECT``：opencompass_* 模式使用的学科（默认按数据集取
  ceval=logic / cmmlu=logical）。
- ``--launch-token``：把 supervisor 的 launch_token 嵌入 argv，供跨会话
  身份核验（只在 argv 中的 token 可核验）。
- ``--sleep``：sleeper 模式，供测试扮演无关存活进程或 Job 子进程。

终态模式都会原子写 ``.motte-job-complete`` 完成标记（review R10：恢复
路径只信标记）；挂起类模式不写标记。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

MODULE = "motte_benchmark.fake_runner"

_FULL_RECORDS: list[dict[str, Any]] = [
    {"case_id": "s-a:1", "status": "succeeded", "output": {"prediction": "A"}},
    {"case_id": "s-a:2", "status": "succeeded", "output": {"prediction": "B"}},
    {"case_id": "s-a:3", "status": "succeeded", "output": {"prediction": "C"}},
    {"case_id": "s-a:4", "status": "succeeded", "output": {"prediction": "D"}},
]

_MARKER = ".motte-job-complete"


def self_argv(*extra: str) -> list[str]:
    """以当前解释器构造本模块的 ``-m`` 调用参数（不经 shell）。"""
    return [sys.executable, "-m", MODULE, *extra]


def _write_marker(work: Path, exit_code: int) -> None:
    partial = work / (_MARKER + ".partial")
    partial.write_text(
        json.dumps({"exit_code": exit_code, "completed": True}), encoding="utf-8",
    )
    partial.replace(work / _MARKER)


async def _spawn_sleeper_pid() -> int:
    proc = await asyncio.create_subprocess_exec(*self_argv("--sleep"))
    return proc.pid


def _fake_subject(dataset: str) -> str:
    return "logical" if dataset == "cmmlu" else "logic"


def _write_opencompass_tree(
    base: Path, *, dataset: str, subject: str, rows: list[dict[str, Any]],
) -> None:
    """按 OpenCompass 形态写 ``<base>/results/<model>/<dataset>-<subject>.json``。"""
    results_dir = base / "results" / "mock-model"
    results_dir.mkdir(parents=True, exist_ok=True)
    details: dict[str, Any] = {}
    for position, row in enumerate(rows):
        details[str(position)] = {
            "prompt": f"{subject}-P{position}",
            "origin_prediction": f"答案为 {row['prediction']}" if row["prediction"] else "",
            "predictions": row["prediction"] or "",
            "references": row.get("gold") or "",
        }
    accuracy = 100.0 * sum(1 for row in rows if row.get("correct")) / len(rows) if rows else 0.0
    (results_dir / f"{dataset}-{subject}.json").write_text(
        json.dumps({"accuracy": accuracy, "details": details}, ensure_ascii=False),
        encoding="utf-8",
    )


def _opencompass_partial_rows(work: Path, dataset: str, subject: str) -> list[dict[str, Any]]:
    """从冻结 runner-config 读前 N 题，按 MOTTE_FAKE_PREDICTIONS 产出结果。"""
    config = json.loads((work / "runner-config.json").read_text(encoding="utf-8"))
    cases = [
        case for case in config.get("cases") or []
        if isinstance(case, dict) and str(case.get("subject")) == subject
    ]
    limit = int(os.environ.get("MOTTE_FAKE_CASE_LIMIT", "3"))
    predictions = [
        item.strip() for item in os.environ.get("MOTTE_FAKE_PREDICTIONS", "").split(",") if item.strip()
    ]
    rows: list[dict[str, Any]] = []
    for position, case in enumerate(cases[:limit]):
        prediction = predictions[position] if position < len(predictions) else ""
        rows.append({
            "case_id": str(case.get("case_id")),
            "prediction": prediction,
            "gold": None,
            "correct": False,
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=MODULE)
    parser.add_argument("--launch-token", dest="launch_token", default=None)
    parser.add_argument("--sleep", action="store_true")
    args = parser.parse_args(argv)

    if args.sleep:
        time.sleep(600)
        return 0

    work_env = os.environ.get("MOTTE_WORK_DIR", "")
    work = Path(work_env).resolve() if work_env else None
    if work is None or not work.is_dir():
        print("MOTTE_WORK_DIR must be an existing directory", file=sys.stderr)
        return 2
    mode = os.environ.get("MOTTE_FAKE_MODE", "ok")

    if mode == "spawn_child":
        pid = asyncio.run(_spawn_sleeper_pid())
        (work / "child.pid").write_text(str(pid), encoding="utf-8")

    if mode == "noise":
        sys.stdout.write("x" * (2 * 1024 * 1024))
        sys.stdout.flush()

    if mode in ("ok", "fail", "noise"):
        records = _FULL_RECORDS[:3] if mode == "fail" else list(_FULL_RECORDS)
        (work / "results.json").write_text(
            json.dumps({"records": records}), encoding="utf-8",
        )
        _write_marker(work, 1 if mode == "fail" else 0)
        return 1 if mode == "fail" else 0

    if mode == "partial_quiet":
        # 写部分 results.json 但不写完成标记（review R10 反例）：恢复路径
        # 只能保持不确定，不能凭部分输出升级为成功。
        (work / "results.json").write_text(
            json.dumps({"records": _FULL_RECORDS[:2]}), encoding="utf-8",
        )
        return 0

    if mode == "slow_ok":
        time.sleep(1.5)
        (work / "results.json").write_text(
            json.dumps({"records": [
                {"case_id": "s-a:1", "status": "succeeded", "output": {"prediction": "A"}},
            ]}),
            encoding="utf-8",
        )
        _write_marker(work, 0)
        return 0

    if mode in ("opencompass_ok", "opencompass_partial", "opencompass_ts_ok"):
        dataset = os.environ.get("MOTTE_FAKE_DATASET", "ceval")
        subject = os.environ.get("MOTTE_FAKE_SUBJECT") or _fake_subject(dataset)
        if mode == "opencompass_ok":
            # 写 OpenCompass 形态输出树（迁移 parser 的真实解析路径）。
            rows = [
                {"case_id": f"{subject}-0", "prediction": "B", "gold": "B", "correct": True},
                {"case_id": f"{subject}-1", "prediction": "A", "gold": "C", "correct": False},
            ]
            exit_code = 0
        elif mode == "opencompass_ts_ok":
            # 固定版 CLI 的真实形态：--work-dir 下按时间戳建实验目录
            # （review R2-02），无 experiment.json 指针 → 解析侧唯一候选自动发现。
            rows = [
                {"case_id": f"{subject}-0", "prediction": "B", "gold": "B", "correct": True},
            ]
            exit_code = 0
            _write_opencompass_tree(
                work / "outputs" / "20260920_090000",
                dataset=dataset, subject=subject, rows=rows,
            )
            _write_marker(work, exit_code)
            return exit_code
        else:
            rows = _opencompass_partial_rows(work, dataset, subject)
            exit_code = int(os.environ.get("MOTTE_FAKE_EXIT_CODE", "3"))
        _write_opencompass_tree(
            work / "outputs", dataset=dataset, subject=subject, rows=rows,
        )
        _write_marker(work, exit_code)
        return exit_code

    time.sleep(600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
