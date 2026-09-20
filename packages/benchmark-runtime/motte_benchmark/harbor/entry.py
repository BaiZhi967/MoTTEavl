"""Harbor Runner 桥接（在固定 Harbor 环境的解释器里执行，M3-T06）。

职责（保持最小）：

1. 读取平台冻结的 ``harbor/config.json`` 与 ``harbor/plan.json``；
2. 用锁定版本的真实 Harbor 模型构造原生 ``JobConfig``——构造即校验，未知
   字段/非法组合在启动前失败，不把"字符串看起来对"当成可执行；
3. 运行 Job，并把 **Harbor 自己的 Job 目录位置**写进
   ``harbor/job-location.json``；证据不在这里复制，由平台侧的受控读取器
   （``TrustedDir``）从该目录冻结，避免两处不一致的副本；
4. 原子写完成标记（含退出码），让平台能区分"跑完了"与"进程消失了"。

本模块不 import 平台其他包（Runner 环境只装 harbor 与 ``motte_benchmark.harbor``
的桥接模块），不读取平台数据库，不使用宿主凭据。所有路径都经过
``safe_child`` / ``safe_join`` 校验，越界立即失败。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

PLAN_NAME = "harbor/plan.json"
CONFIG_NAME = "harbor/config.json"
LOCATION_NAME = "harbor/job-location.json"
COMPLETION_MARKER = ".motte-job-complete"


class RunnerBridgeError(RuntimeError):
    """Runner 侧配置/布局错误（退出码 4/5，绝不静默继续）。"""


def safe_child(base: Path, name: str) -> Path:
    """受控子路径：``name`` 必须是单一安全组件，且结果仍在 ``base`` 内。"""
    if not isinstance(name, str) or not name:
        raise RunnerBridgeError("path component must be a nonempty string")
    if name in (os.curdir, os.pardir) or "/" in name or "\\" in name or "\x00" in name:
        raise RunnerBridgeError(f"unsafe path component: {name!r}")
    root = Path(base).resolve()
    target = root.joinpath(name).resolve()
    if target != root and not target.is_relative_to(root):
        raise RunnerBridgeError(f"path escapes {root}: {name!r}")
    return target


def safe_join(base: Path, relative: str) -> Path:
    """受控相对路径（可含多级）：逐段校验后拼接，保证不越界。"""
    current = Path(base)
    for part in str(relative).replace("\\", "/").split("/"):
        if part:
            current = safe_child(current, part)
    return current


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RunnerBridgeError(f"{path} must contain a JSON object")
    return payload


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_completion_marker(work_dir: Path, exit_code: int, detail: str = "") -> None:
    """原子完成标记：平台凭它区分正常结束与进程消失。"""
    marker = safe_join(work_dir, COMPLETION_MARKER)
    temporary = safe_join(work_dir, COMPLETION_MARKER + ".partial")
    temporary.write_text(
        json.dumps(
            {"exit_code": exit_code, "completed": True, "detail": detail},
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    temporary.replace(marker)


def resolved_task_paths(config: Mapping[str, Any], data_root: Path) -> list[dict[str, Any]]:
    """把冻结配置里的相对任务路径投影成 Harbor 需要的绝对路径。"""
    root = Path(data_root).resolve()
    entries: list[dict[str, Any]] = []
    for entry in config["job"]["tasks"]:
        candidate = safe_join(root, str(entry["path"]))
        if not candidate.is_dir():
            raise RunnerBridgeError(f"task directory is missing: {entry['path']}")
        entries.append({"path": str(candidate), "source": entry.get("source")})
    return entries


def job_paths(config: Mapping[str, Any], work_dir: Path) -> tuple[Path, str]:
    """Job 目录与 Job 名；两者都必须落在受控工作目录内。"""
    work = Path(work_dir).resolve()
    raw_jobs_dir = str(config["job"]["jobs_dir"])
    jobs_dir = safe_join(work, raw_jobs_dir).resolve()
    if jobs_dir != work and work not in jobs_dir.parents:
        raise RunnerBridgeError(f"jobs dir must stay inside the work dir: {jobs_dir}")
    job_name = str(config["job"]["job_name"])
    safe_child(jobs_dir, job_name)
    return jobs_dir, job_name


def build_job_config(config: Mapping[str, Any], data_root: Path, work_dir: Path) -> Any:
    """用真实 Harbor 模型构造并校验原生 JobConfig（锁定版本）。"""
    from harbor.models.job.config import JobConfig  # noqa: PLC0415 - Runner 侧依赖

    jobs_dir, _job_name = job_paths(config, work_dir)
    job = dict(config["job"])
    job["tasks"] = resolved_task_paths(config, data_root)
    job["jobs_dir"] = str(jobs_dir)
    return JobConfig.model_validate(job)


def record_job_location(
    work_dir: Path, jobs_dir: Path, job_name: str, *, reused: bool,
) -> dict[str, Any]:
    """记录 Job 目录位置（证据由平台侧从这个目录冻结）。"""
    job_dir = safe_child(jobs_dir, job_name)
    trials = sorted(
        path.name for path in job_dir.iterdir()
        if path.is_dir() and safe_join(path, "result.json").is_file()
    )
    payload = {
        "job_dir": str(job_dir),
        "jobs_dir": str(jobs_dir),
        "job_name": job_name,
        "trials": trials,
        "reused_existing_job_dir": reused,
    }
    write_json(safe_join(work_dir, LOCATION_NAME), payload)
    return payload


async def run_job(job_config: Any) -> Any:
    from harbor.job import Job  # noqa: PLC0415 - Runner 侧依赖

    job = await Job.create(job_config)
    return await job.run()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MoTTEavl Harbor runner bridge")
    parser.add_argument("--launch-token", default="")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args(list(argv) if argv is not None else None)

    work_dir = Path(os.environ.get("MOTTE_WORK_DIR", "")).resolve()
    if not work_dir.is_dir():
        print("MOTTE_WORK_DIR is required and must exist", file=sys.stderr)
        return 4
    # 启动身份留在 argv/env 里，跨实例可核验（与 M2 R3-03 同款）。
    token = args.launch_token or os.environ.get("MOTTE_LAUNCH_TOKEN", "")
    if not token:
        write_completion_marker(work_dir, 4, "launch token missing")
        print("launch token missing", file=sys.stderr)
        return 4

    try:
        config = load_json(safe_join(work_dir, CONFIG_NAME))
        plan = load_json(safe_join(work_dir, PLAN_NAME))
    except (OSError, ValueError, RunnerBridgeError) as error:
        write_completion_marker(work_dir, 4, f"frozen config unreadable: {error}")
        print(f"frozen config unreadable: {error}", file=sys.stderr)
        return 4

    write_json(safe_join(work_dir, "harbor/launch-identity.json"), {
        "job_id": args.job_id or os.environ.get("MOTTE_JOB_ID", ""),
        "run_id": args.run_id or os.environ.get("MOTTE_RUN_ID", ""),
        "launch_token": token,
        "work_dir": str(work_dir),
        "harbor_version": config.get("harbor_version"),
        "config_hash": config.get("config_hash"),
    })

    data_root = Path(
        os.environ.get("MOTTE_TASK_ROOT") or safe_join(work_dir, "tasks"),
    ).resolve()
    try:
        jobs_dir, job_name = job_paths(config, work_dir)
        job_config = build_job_config(config, data_root, work_dir)
    except Exception as error:  # noqa: BLE001 - 原生校验失败必须显式落库
        write_completion_marker(work_dir, 5, f"native config rejected: {error}")
        print(f"native config rejected: {error}", file=sys.stderr)
        return 5

    if safe_child(jobs_dir, job_name).is_dir():
        # 同一 Job 名已存在：只登记位置，绝不重复执行（与平台恢复语义一致）。
        located = record_job_location(work_dir, jobs_dir, job_name, reused=True)
        write_completion_marker(
            work_dir, 0, f"reused existing job dir; trials={len(located['trials'])}",
        )
        return 0

    try:
        result = asyncio.run(run_job(job_config))
    except Exception as error:  # noqa: BLE001 - 失败仍要保留已产出的证据
        try:
            located = record_job_location(work_dir, jobs_dir, job_name, reused=False)
        except Exception:  # noqa: BLE001 - 连目录都没有时如实记录
            located = {"trials": []}
        write_json(safe_join(work_dir, "harbor/run-error.json"), {
            "error_type": type(error).__name__,
            "message": str(error),
            "observed_trials": len(located["trials"]),
        })
        write_completion_marker(work_dir, 1, f"job failed: {type(error).__name__}")
        print(f"harbor job failed: {error}", file=sys.stderr)
        return 1

    located = record_job_location(work_dir, jobs_dir, job_name, reused=False)
    stats = getattr(result, "stats", None)
    write_json(safe_join(work_dir, "harbor/run-summary.json"), {
        "n_trials": len(located["trials"]),
        "n_completed_trials": getattr(stats, "n_completed_trials", None),
        "n_errored_trials": getattr(stats, "n_errored_trials", None),
        "planned_trial_count": plan.get("planned_trial_count"),
    })
    write_completion_marker(work_dir, 0, f"trials={len(located['trials'])}")
    return 0


if __name__ == "__main__":  # pragma: no cover - Runner 入口
    raise SystemExit(main())
