"""Harbor Job 适配器（M3-T06/T07，复用 M2 的 ExternalJobAdapter 协议）。

职责划分（需求第 5 节"资源所有权"）：

- ``ProcessJobAdapter`` 持有 Runner 进程生命周期（PID 归属、token 核验、
  进程组中断、残留清单）——这部分是 M2 已测代码，不重写；
- 本适配器持有 Harbor 语义：把冻结配置写进工作目录（``prepare``，零执行）、
  从 Harbor 自己的 Job 目录受控读取产物（``read_output_files``）、在边界把
  文本 UTF-8 encode 成 bytes 后交给纯 Parser（``collect_from_files``）。

平台主权：adapter 不修改 Run 表、不决定评分。任务容器由 Harbor 管理，
平台只记录容器/进程/工作目录的 owner token，删除前核验所有权。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from motte_benchmark.harbor.parser import PARSER_VERSION, parse_harbor_files
from motte_benchmark.harbor.tasks import HarborTaskError
from motte_benchmark.process import ProcessJobAdapter
from motte_benchmark.protocol import BenchmarkRuntimeError
from motte_benchmark.trusted import TrustedDir, TrustedPathError
from motte_contracts.external_job import ExternalJobHandle, ExternalJobStatus, ExternalJobSpec

ADAPTER_ID = "terminal-bench-harbor"
ADAPTER_VERSION = "1"
PROFILE_NAME = "job-profile.json"
CONFIG_NAME = "harbor/config.json"
PLAN_NAME = "harbor/plan.json"
LOCATION_NAME = "harbor/job-location.json"
#: 单次采集的原始证据预算（与 M2 一致：超预算只留 hash，解析前拒绝最终化）。
MAX_RESULT_BYTES = 10 * 1024 * 1024


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


class HarborJobAdapter:
    """Harbor（Terminal-Bench）外部 Job 适配器。"""

    #: 与锁定 Harbor 版本对应的 Runner 版本串（进入 profile/证据）。
    RUNNER_VERSION = "harbor-0.23.0"

    def __init__(
        self, *, argv: list[str] | None = None, module: str | None = None,
        extra_env: dict[str, str] | None = None, default_limits: dict[str, Any] | None = None,
        data_root: str | None = None,
    ) -> None:
        self.adapter_id = ADAPTER_ID
        self.adapter_version = ADAPTER_VERSION
        if argv is None and module is None:
            argv = ["/opt/motte-runner/bin/harbor-entry"]
        #: 任务数据的受控根目录：Runner 从它把冻结的相对任务路径解析成绝对路径，
        #: 因此必须随启动环境一起传下去（否则 Runner 找不到任务目录）。
        self.data_root = data_root
        env = {str(key): str(value) for key, value in (extra_env or {}).items()}
        if data_root:
            env.setdefault("MOTTE_TASK_ROOT", str(data_root))
        self._process = ProcessJobAdapter(
            argv=argv, module=module, extra_env=env or None, default_limits=default_limits,
        )
        self.last_parsed: dict[str, Any] | None = None

    # ---------------------------------------------------------- 身份与限额

    @property
    def parser_version(self) -> str:
        return PARSER_VERSION

    @property
    def default_limits(self) -> dict[str, Any]:
        return self._process.default_limits

    @property
    def start_calls(self) -> list[dict[str, Any]]:
        return self._process.start_calls

    def spawned_processes(self) -> list[dict[str, Any]]:
        return self._process.spawned_processes()

    # ---------------------------------------------------------- 生命周期

    def prepare(self, spec: ExternalJobSpec) -> ExternalJobHandle:
        """写冻结配置与计划；零执行、零模型调用。"""
        handle = self._process.prepare(spec)
        work_dir = Path(handle.work_dir)
        (work_dir / "harbor").mkdir(parents=True, exist_ok=True)
        config = spec.runner_config.get("harbor")
        if not isinstance(config, dict):
            raise BenchmarkRuntimeError(
                "HARBOR_CONFIG_MISSING",
                "runner_config.harbor is required for the terminal-bench adapter",
            )
        plan = spec.runner_config.get("plan")
        if not isinstance(plan, dict):
            raise BenchmarkRuntimeError(
                "HARBOR_PLAN_MISSING", "runner_config.plan is required (frozen TrialPlan set)",
            )
        tasks = plan.get("tasks")
        if not isinstance(tasks, list):
            raise BenchmarkRuntimeError(
                "HARBOR_PLAN_MISSING", "runner_config.plan.tasks is required",
            )
        (work_dir / PLAN_NAME).write_bytes(_encode(plan))
        (work_dir / CONFIG_NAME).write_bytes(_encode(config))
        (work_dir / PROFILE_NAME).write_bytes(_encode(spec.profile))
        return handle.model_copy(update={
            "launch_identity": {
                **(handle.launch_identity or {}),
                "adapter": ADAPTER_ID,
                "adapter_version": ADAPTER_VERSION,
                "harbor_version": config.get("harbor_version"),
                "planned_trials": len(plan.get("trials") or []),
                "config_hash": config.get("config_hash"),
                "data_root": self.data_root,
            },
        })

    def start(self, spec: ExternalJobSpec, handle: ExternalJobHandle) -> ExternalJobHandle:
        """启动 Runner；launch_token 已在持久层落库后由 supervisor 注入。"""
        started = self._process.start(spec, handle)
        owned = dict(started.owned_resources or {})
        # 资源账本：本 Job 拥有的容器/工作目录前缀（清理前据此核验所有权）。
        owned.update({
            "owner_token": started.launch_token,
            "job_id": started.job_id,
            "run_id": started.run_id,
            "container_label": f"motte.job={started.job_id}",
        })
        resources = list(owned.get("resources") or [])
        resources.append({
            "kind": "job_dir",
            "path": str(started.work_dir),
            "owner_token": started.launch_token,
        })
        owned["resources"] = resources
        return started.model_copy(update={"owned_resources": owned})

    def poll(self, handle: ExternalJobHandle) -> ExternalJobHandle:
        return self._process.poll(handle)

    def interrupt(self, handle: ExternalJobHandle) -> ExternalJobHandle:
        """中断本 Job 拥有的进程树；只凭 owner token 核验，不做全局清理。"""
        return self._process.interrupt(handle)

    def cleanup(self, handle: ExternalJobHandle) -> dict[str, Any]:
        """清理本 Job 资源并列出残留（不做全局 prune，也不删未知目录）。"""
        outcome = self._process.cleanup(handle)
        leftovers = list(outcome.get("leftovers") or [])
        ledger = list((handle.owned_resources or {}).get("resources") or [])
        job_dir = next(
            (item["path"] for item in ledger if item.get("kind") == "job_dir"), None,
        )
        outcome = dict(outcome)
        outcome.update({
            "owner_token": handle.launch_token,
            "job_id": handle.job_id,
            "known_resources": ledger,
            "job_dir": job_dir,
            "job_dir_present": bool(job_dir) and Path(job_dir).is_dir(),
            "leftovers": leftovers,
            "state": "clean" if not leftovers else "residual",
        })
        return outcome

    def collect(
        self, handle: ExternalJobHandle, cursor: dict[str, Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        """兼容路径：先读受控字节，再走共享的纯 Parser。"""
        files = self.read_output_files(handle)
        return self.collect_from_files(handle, cursor, files)

    # ---------------------------------------------------------- 证据读取

    def _job_dir(self, handle: ExternalJobHandle) -> Path | None:
        """Harbor Job 目录（来自 Runner 写的定位文件；越界即拒绝）。"""
        location = Path(handle.work_dir) / LOCATION_NAME
        if not location.is_file():
            return None
        try:
            payload = json.loads(location.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        candidate = payload.get("job_dir")
        if not isinstance(candidate, str) or not candidate:
            return None
        job_dir = Path(candidate)
        work_root = Path(handle.work_dir).resolve()
        resolved = job_dir.resolve()
        # Job 目录必须就落在本 Job 的工作目录内（Runner 侧 jobs_dir 受控）。
        if resolved != work_root and work_root not in resolved.parents:
            return None
        return resolved

    def read_output_files(self, handle: ExternalJobHandle) -> dict[str, str]:
        """受控读取 Harbor 产物文本：``{相对路径: 文本}``，解析前先冻结。

        只读取 Parser 需要的文件（结果/配置/锁/奖励/终端/轨迹/工件清单），
        非 UTF-8 或不存在的文件不进入文本 bundle（其 hash 由 artifact 引用
        与 `evidence-index.json` 表达），绝不有损解码。
        """
        files: dict[str, str] = {}
        for name in ("config.json", "lock.json", "result.json"):
            source = Path(handle.work_dir) / "harbor" / name
            if source.is_file():
                files[f"harbor/{name}"] = source.read_text(encoding="utf-8", errors="strict")
        plan_path = Path(handle.work_dir) / PLAN_NAME
        if plan_path.is_file():
            files[PLAN_NAME] = plan_path.read_text(encoding="utf-8", errors="strict")
        index: dict[str, Any] = {"schema_version": 1, "files": {}}
        job_dir = self._job_dir(handle)
        if job_dir is None:
            # Job 目录缺失/越界：仍然返回冻结的计划与 harness 文件，让 Parser
            # 把全部计划单元标成 not_attempted（有 disposition），而不是空结果。
            files["harbor/evidence-index.json"] = json.dumps(
                {**index, "job_dir_unavailable": True}, ensure_ascii=False, sort_keys=True,
            )
            return files
        with TrustedDir(job_dir, error_factory=_read_error) as trusted:
            rels, symlinks = trusted.list_files()
            if symlinks:
                # 输出边界内的 symlink 显式拒绝：不静默丢结果、不跟随到宿主。
                raise BenchmarkRuntimeError(
                    "HARBOR_OUTPUT_SYMLINK",
                    f"harbor job dir contains symlinks: {symlinks[:5]}",
                )
            for rel in rels:
                if rel.endswith("instruction.md"):
                    continue
                if not self._wanted(rel):
                    continue
                try:
                    data = trusted.read_bytes(rel, max_bytes=MAX_RESULT_BYTES)
                except TrustedPathError:
                    continue
                entry: dict[str, Any] = {
                    "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    # 二进制/非 UTF-8：留在原位，只登记 hash 与编码，不做有损解码。
                    entry["encoding"] = "binary"
                    index["files"][
                        f"harbor/job/{rel}" if "/" not in rel else f"harbor/trials/{rel}"
                    ] = entry
                    continue
                entry["encoding"] = "utf-8"
                key = (
                    f"harbor/job/{rel}" if "/" not in rel else f"harbor/trials/{rel}"
                )
                index["files"][key] = entry
                files[key] = text
        files["harbor/evidence-index.json"] = json.dumps(
            index, ensure_ascii=False, sort_keys=True,
        )
        return files

    @staticmethod
    def _wanted(rel: str) -> bool:
        """Parser 需要的产物（大文件仍只登记 hash，二进制只登记编码与 hash）。

        路径形状是 ``<trial 目录>/...`` 或 job 层的 ``result.json`` 等文件，
        因此判定要跳过第一段 Trial 目录名看真正的种类。
        """
        parts = rel.split("/")
        if len(parts) == 1:
            # job 根层的文件（Harbor 的 job result.json/config.json/lock.json）。
            return parts[0] in ("result.json", "config.json", "lock.json", "trial.log",
                                "exception.txt")
        kinds = parts[1:]
        if len(kinds) == 1:
            # Trial 根层的文件（result.json / config.json / lock.json / trial.log …）。
            return True
        if kinds[0] in ("agent", "verifier", "artifacts", "user-agent"):
            return True
        if kinds[0] == "steps":
            return bool(kinds) and kinds[-1] in (
                "result.json", "trial.log", "exception.txt", "reward.json", "reward.txt",
                "test-stdout.txt", "test-stderr.txt", "manifest.json",
            )
        return False

    def collect_from_files(
        self, handle: ExternalJobHandle, cursor: dict[str, Any], files: dict[str, str],
    ) -> tuple[list[Any], dict[str, Any]]:
        """把冻结文本 encode 成 bytes 后调用纯 Parser（恢复路径同样走这里）。"""
        from motte_contracts.external_job import NormalizedCaseResult

        plan_payload = files.get(PLAN_NAME)
        if plan_payload is None:
            raise BenchmarkRuntimeError(
                "HARBOR_FROZEN_PLAN_MISSING",
                "frozen plan is unavailable; case mapping cannot be rebuilt",
            )
        try:
            plan = json.loads(plan_payload)
        except json.JSONDecodeError as error:
            raise BenchmarkRuntimeError(
                "HARBOR_FROZEN_PLAN_INVALID", f"frozen plan is not valid JSON: {error}",
            ) from error
        plans = plan.get("trials") if isinstance(plan, dict) else None
        if not isinstance(plans, list):
            raise BenchmarkRuntimeError(
                "HARBOR_FROZEN_PLAN_INVALID", "frozen plan must contain the trial list",
            )
        encoded = {rel: text.encode("utf-8") for rel, text in files.items()}
        parsed = parse_harbor_files(encoded, plans, parser_version=PARSER_VERSION)
        self.last_parsed = parsed

        results: list[NormalizedCaseResult] = []
        for trial in parsed["results"]:
            observation = trial["verifier_observation"]
            case_key = f"{trial['task_key']}#{trial['repeat_index']}"
            results.append(NormalizedCaseResult(
                # 幂等导入键必须是**计划 Trial**身份：一个 Task 有多个 Trial，
                # 用 task_key 当键会让第二个 Trial 与第一个"同键异内容"冲突。
                # 逻辑 Case 身份由 payload 的 task_key 表达（一 Task 一条 CaseRun）。
                stable_case_key=case_key,
                source_case_id=str(trial.get("source_trial_id") or case_key),
                output=trial,
                error=(
                    observation.get("error") if observation.get("error") else None
                ),
                status=_case_status(trial),
                error_category=(
                    str(observation["status"]) if observation.get("error") else None
                ),
                usage=trial.get("usage") or {},
                evidence_coverage=trial.get("coverage") or {},
            ))
        cursor = dict(cursor or {})
        cursor.update({
            "parser_version": parsed["parser_version"],
            "harbor_version": parsed["harbor_version"],
            "planned_trial_count": parsed["planned_trial_count"],
            "trial_count": parsed["trial_count"],
            "unmapped": parsed["unmapped"][:20],
            "coverage": _aggregate_coverage(parsed["results"]),
        })
        return results, cursor


def _read_error(message: str) -> Exception:
    return BenchmarkRuntimeError("HARBOR_OUTPUT_UNREADABLE", message)


def _case_status(trial: dict[str, Any]) -> Any:
    """``NormalizedCaseResult`` 的处置状态：与 Trial disposition 对齐。"""
    from motte_contracts.external_job import CaseResultStatus

    disposition = str(trial["disposition"])
    if disposition in ("succeeded", "failed"):
        return CaseResultStatus.succeeded
    if disposition == "not_attempted":
        return CaseResultStatus.not_attempted
    return CaseResultStatus.failed


def _aggregate_coverage(results: list[dict[str, Any]]) -> dict[str, Any]:
    """采集覆盖范围汇总（供报告显示，不参与评分分母）。"""
    counted: dict[str, dict[str, int]] = {}
    for trial in results:
        items = (trial.get("coverage") or {}).get("items") or {}
        for key, value in items.items():
            bucket = counted.setdefault(key, {})
            bucket[str(value)] = bucket.get(str(value), 0) + 1
    return counted
