"""可控阻塞 Runner：验证 Job 总期限真的被 Supervisor 强制（M3 review R07）。

行为与真实 Harbor Runner 的接口一致（读 ``harbor/config.json`` /
``harbor/plan.json``，写定位文件与 Job 目录里的 Trial 结果），但只做两件事：

1. 在 Job 目录里写下若干**已完成 Trial** 的产物（模拟"第一个 Trial 已完成、
   后续 Trial 仍在运行"的中断场景，review R05/R07 的部分证据）；
2. 阻塞到被 TERM/KILL（默认睡 600 秒）。

不调用模型、不启动容器，只用于期限/中断/部分证据与清理语义的确定性验证。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BLOCK_SECONDS = 600.0


def main() -> int:
    work_dir = Path(os.environ["MOTTE_WORK_DIR"]).resolve()
    config = json.loads((work_dir / "harbor/config.json").read_text(encoding="utf-8"))
    plan = json.loads((work_dir / "harbor/plan.json").read_text(encoding="utf-8"))
    jobs_dir = (work_dir / config["job"]["jobs_dir"]).resolve()
    job_name = str(config["job"]["job_name"])
    job_dir = jobs_dir / job_name
    job_dir.mkdir(parents=True, exist_ok=True)

    # 只有第一个 Trial 完成（review 场景：首个 Trial 完成、后续 Trial 运行中
    # 被中断）；其余计划单元保持"尚未产出"，恢复后必须是 not_attempted。
    tasks = list(plan.get("tasks") or [])
    trial_names: list[str] = []
    completed: list[str] = []
    for index, task in enumerate(tasks):
        relative = str(task["normalized_relative_path"])
        trial_name = f"{relative.rstrip('/').split('/')[-1]}__Block{index:02d}"
        trial_names.append(trial_name)
        if index > 0:
            continue
        trial_dir = job_dir / trial_name
        (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)
        payload = {
            "id": f"blocking-trial-{index}",
            "trial_name": trial_name,
            "task_id": {"path": f"/data/{relative}"},
            "started_at": "2026-09-20T00:00:00+00:00",
            "finished_at": "2026-09-20T00:00:05+00:00",
            "exception_info": None,
            "verifier_result": {"rewards": {"reward": 1.0}},
            "agent_result": {"exit_code": 0},
        }
        (trial_dir / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        (trial_dir / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")
        completed.append(trial_name)

    (work_dir / "harbor").mkdir(parents=True, exist_ok=True)
    (work_dir / "harbor" / "job-location.json").write_text(
        json.dumps({
            "job_dir": str(job_dir),
            "jobs_dir": str(jobs_dir),
            "job_name": job_name,
            "started": True,
            "reused_existing_job_dir": False,
            "trials": completed,
            "trial_names": trial_names,
            "compose_projects": [f"{name.lower()}__env" for name in trial_names],
            "planned_task_names": [
                str(task["normalized_relative_path"]).rsplit("/", 1)[-1] for task in tasks
            ],
        }),
        encoding="utf-8",
    )
    print(f"blocking runner staged {len(completed)} completed trials; sleeping", flush=True)
    time.sleep(BLOCK_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
