"""合成假 Runner：测试与离线验收的确定性进程 Job 实现。

不是官方 C-Eval/CMMLU 内容；只产出可预测的 ``results.json`` 与受控副作用，
用于验证 Job 生命周期、超时、取消与采集边界。行为由环境变量驱动：

- ``MOTTE_WORK_DIR``（必需）：受控工作目录，``results.json`` 写在这里。
- ``MOTTE_FAKE_MODE``：ok（4 条全对，exit 0）/ fail（3 条部分结果，exit 1）/
  noise（stdout 超 2 MiB 后按 ok）/ spawn_child（派生 sleeper 子进程后挂起）/
  slow_ok（1.5s 后写 1 条，exit 0）/ hang（挂起）。
- ``--launch-token``：把 supervisor 的 launch_token 嵌入 argv，供跨会话
  身份核验（只在 argv 中的 token 可核验）。
- ``--sleep``：sleeper 模式，供测试扮演无关存活进程或 Job 子进程。
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


def self_argv(*extra: str) -> list[str]:
    """以当前解释器构造本模块的 ``-m`` 调用参数（不经 shell）。"""
    return [sys.executable, "-m", MODULE, *extra]


async def _spawn_sleeper_pid() -> int:
    proc = await asyncio.create_subprocess_exec(*self_argv("--sleep"))
    return proc.pid


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
        return 1 if mode == "fail" else 0

    if mode == "slow_ok":
        time.sleep(1.5)
        (work / "results.json").write_text(
            json.dumps({"records": [
                {"case_id": "s-a:1", "status": "succeeded", "output": {"prediction": "A"}},
            ]}),
            encoding="utf-8",
        )
        return 0

    time.sleep(600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
