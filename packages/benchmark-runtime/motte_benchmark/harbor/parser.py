"""Harbor/Terminal-Bench 结果解析（M3-T04，需求第 6 节）。

**纯函数**：只消费已经冻结的字节（``dict[str, bytes]``），不再访问任何
运行目录、不启动任务、不调用模型。冻结发生在解析之前（见
``motte_sdk.external_jobs.ExternalJobSupervisor._collect_quiet``），因此
重复导入同一份字节必然得到同一结果。

解析规则（对照真实 Harbor 0.23.0 产物，fixture 见
``tests/fixtures/benchmarks/harbor/samples/``）：

- 每个计划 Trial 都产出一条 ``TrialResult``，包括没跑成的；"未知 ID /
  重复 ID / 同名任务 / 缺文件 / 半写 JSON / 超大文件 / 截断轨迹" 都显式
  表达，绝不挑最后一个或最高分；
- reward：``verifier/reward.json`` 优先，其次 ``verifier/reward.txt``；
  合法有限数字 = ``scored``（**0 是有效失败**），两个来源同时存在且不一致
  = 协议错误；值类型/范围不符（bool、字符串、null、NaN/Inf）= 协议错误；
  文件不存在 = 缺证据；Verifier 自身异常（超时/解析失败）= verifier_error；
- 证据完整度诚实标注：轨迹/终端/工件缺失或截断时降级为 partial/unavailable，
  不假装完整、不伪造隐藏思考；config 里的秘密只记引用（解析时按需脱敏）。
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any, Iterable, Mapping

PARSER_VERSION = "harbor-terminal-bench-parser@1"
#: 与 ``motte_benchmark.harbor.config.HARBOR_VERSION`` 对应的上游版本。
HARBOR_VERSION = "0.23.0"
MIGRATED_FROM = "native Harbor trial directories (no legacy adapter code reused)"

#: 单个冻结文件的解析上限（超过只记 hash 与大小，不解析）。
MAX_PARSE_BYTES = 8 * 1024 * 1024
#: 单一证据文件进入 TrialResult 的原始文本上限（完整字节另有 Artifact）。
MAX_INLINE_TEXT = 16 * 1024

VERIFIER_ERROR_EXCEPTIONS = frozenset({
    "VerifierTimeoutError",
    "VerifierOutputParseError",
    "RewardFileNotFoundError",
    "RewardFileEmptyError",
})
#: 已知的 Harbor 异常类型 → 中止阶段（用于 termination 分类）。
_EXCEPTION_PHASES = {
    "AgentTimeoutError": "agent",
    "AgentSetupTimeoutError": "agent",
    "AgentSafetyRefusalError": "agent",
    "AgentAuthenticationError": "agent",
    "AgentError": "agent",
    "VerifierTimeoutError": "verifier",
    "VerifierOutputParseError": "verifier",
    "EnvironmentStartTimeoutError": "environment",
    "EnvironmentBuildTimeoutError": "environment",
    "EnvironmentError": "environment",
}


class HarborParseError(ValueError):
    """输入本身不可解析（不是任务/Verifier 失败）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _decode(data: bytes) -> str | None:
    """UTF-8 解码；非 UTF-8 返回 None（调用方标记 unavailable，不做有损转换）。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _load_json(data: bytes, label: str) -> tuple[Any, dict[str, Any] | None]:
    """解析 JSON；半写/畸形/非 UTF-8 都返回带原因的 None，不抛异常。"""
    text = _decode(data)
    if text is None:
        return None, {"code": "EVIDENCE_NOT_UTF8", "message": f"{label} is not valid UTF-8"}
    try:
        return json.loads(text), None
    except json.JSONDecodeError as error:
        return None, {
            "code": "EVIDENCE_MALFORMED_JSON",
            "message": f"{label} is not valid JSON: {error.msg} at offset {error.pos}",
        }


def _duration_seconds(start: Any, end: Any) -> float | None:
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        begin = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finish = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max((finish - begin).total_seconds(), 0.0)


def _timings(payload: Mapping[str, Any]) -> dict[str, float | None]:
    phases = ("environment_setup", "agent_setup", "agent_execution", "verifier")
    out: dict[str, float | None] = {}
    for phase in phases:
        entry = payload.get(phase)
        if isinstance(entry, Mapping):
            out[phase + "_sec"] = _duration_seconds(entry.get("started_at"), entry.get("finished_at"))
        else:
            out[phase + "_sec"] = None
    total = _duration_seconds(payload.get("started_at"), payload.get("finished_at"))
    out["total_sec"] = total
    return out


def trial_directories(files: Mapping[str, bytes], *, prefix: str = "harbor/trials/") -> list[str]:
    """冻结字节里的一级 Trial 目录名（排序，稳定）。"""
    names = set()
    for rel in files:
        if not rel.startswith(prefix):
            continue
        rest = rel[len(prefix):]
        if "/" in rest:
            names.add(rest.split("/", 1)[0])
    return sorted(names)


def _trial_file(files: Mapping[str, bytes], trial: str, relative: str) -> bytes | None:
    return files.get(f"harbor/trials/{trial}/{relative}")


def _reward_from_json(
    data: bytes, label: str,
) -> tuple[dict[str, float] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """返回 ``(rewards, protocol_error, raw_summary)``。

    ``rewards`` 为 None 表示"这个来源没有给出可用奖励"；``protocol_error``
    非空表示值类型/范围不符（需求第 6 节：保留原始 hash，不自动修正）。
    """
    payload, error = _load_json(data, label)
    if error is not None:
        return None, error, {"source": label, "parsed": False}
    if payload is None:
        return None, {
            "code": "VERIFIER_REWARD_EMPTY",
            "message": f"{label} is null rather than a reward mapping",
        }, {"source": label, "parsed": True}
    if not isinstance(payload, dict):
        return None, {
            "code": "VERIFIER_REWARD_NOT_OBJECT",
            "message": f"{label} must be a key/value object, got {type(payload).__name__}",
        }, {"source": label, "parsed": True}
    if not payload:
        return None, None, {"source": label, "parsed": True, "empty": True}
    rewards: dict[str, float] = {}
    for key, value in payload.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, {
                "code": "VERIFIER_REWARD_TYPE",
                "message": f"{label} reward {key!r} must be a number, "
                           f"got {type(value).__name__}",
            }, {"source": label, "parsed": True}
        if not math.isfinite(float(value)):
            return None, {
                "code": "VERIFIER_REWARD_RANGE",
                "message": f"{label} reward {key!r} must be finite, got {value!r}",
            }, {"source": label, "parsed": True}
        rewards[str(key)] = float(value)
    return rewards, None, {"source": label, "parsed": True, "keys": sorted(rewards)}


def _reward_from_text(data: bytes, label: str) -> tuple[float | None, dict[str, Any] | None]:
    text = _decode(data)
    if text is None:
        return None, {"code": "EVIDENCE_NOT_UTF8", "message": f"{label} is not valid UTF-8"}
    stripped = text.strip()
    if not stripped:
        return None, {"code": "VERIFIER_REWARD_EMPTY", "message": f"{label} is empty"}
    try:
        value = float(stripped)
    except ValueError:
        return None, {
            "code": "VERIFIER_REWARD_TYPE",
            "message": f"{label} must contain a single number, got {stripped[:64]!r}",
        }
    if not math.isfinite(value):
        return None, {
            "code": "VERIFIER_REWARD_RANGE",
            "message": f"{label} must be finite, got {stripped[:64]!r}",
        }
    return value, None


def read_verifier_observation(
    files: Mapping[str, bytes], trial: str,
) -> dict[str, Any]:
    """从冻结字节判定 Verifier 观测状态（scored / missing / protocol / error）。"""
    json_bytes = _trial_file(files, trial, "verifier/reward.json")
    text_bytes = _trial_file(files, trial, "verifier/reward.txt")
    result_bytes = _trial_file(files, trial, "result.json")
    result_payload: Mapping[str, Any] = {}
    result_error: dict[str, Any] | None = None
    if result_bytes is not None:
        parsed, result_error = _load_json(result_bytes, "result.json")
        if isinstance(parsed, Mapping):
            result_payload = parsed

    exception = result_payload.get("exception_info")
    exception_type = (
        str(exception.get("exception_type")) if isinstance(exception, Mapping) else None
    )
    verifier_result = result_payload.get("verifier_result")
    rewards_from_result: dict[str, float] | None = None
    protocol_error: dict[str, Any] | None = None
    if isinstance(verifier_result, Mapping) and "rewards" in verifier_result:
        raw = verifier_result.get("rewards")
        if raw is None:
            rewards_from_result = None
        elif isinstance(raw, Mapping):
            rewards_from_result, protocol_error, _ = _reward_from_json(
                json.dumps(raw).encode("utf-8"), "result.json#verifier_result.rewards",
            )
        else:
            protocol_error = {
                "code": "VERIFIER_REWARD_NOT_OBJECT",
                "message": "result.json verifier_result.rewards must be an object or null",
            }

    json_rewards, json_error, json_summary = (
        (None, None, {"source": "verifier/reward.json", "present": False})
        if json_bytes is None
        else _reward_from_json(json_bytes, "verifier/reward.json")
    ) if json_bytes is not None else (None, None, {"source": "verifier/reward.json", "present": False})
    if json_bytes is not None:
        json_rewards, json_error, json_summary = _reward_from_json(
            json_bytes, "verifier/reward.json",
        )
        json_summary["present"] = True

    text_reward: float | None = None
    text_error: dict[str, Any] | None = None
    if text_bytes is not None:
        text_reward, text_error = _reward_from_text(text_bytes, "verifier/reward.txt")

    evidence_refs = [
        entry for entry in (
            _evidence_ref(files, f"harbor/trials/{trial}/verifier/reward.json",
                          kind="harbor-reward-json"),
            _evidence_ref(files, f"harbor/trials/{trial}/verifier/reward.txt",
                          kind="harbor-reward-text"),
            _evidence_ref(files, f"harbor/trials/{trial}/verifier/test-stdout.txt",
                          kind="harbor-verifier-stdout"),
            _evidence_ref(files, f"harbor/trials/{trial}/verifier/test-stderr.txt",
                          kind="harbor-verifier-stderr"),
            _evidence_ref(files, f"harbor/trials/{trial}/exception.txt",
                          kind="harbor-exception"),
        ) if entry is not None
    ]

    observation: dict[str, Any] = {
        "status": "missing_verifier_evidence",
        "rewards": {},
        "evidence_refs": evidence_refs,
        "error": None,
        "source_path": None,
        "source_hash": None,
        "detail": {
            "reward_json": json_summary,
            "reward_text_present": text_bytes is not None,
            "result_json": "parsed" if result_payload else ("missing" if result_bytes is None else "unusable"),
            "result_error": result_error,
        },
    }

    if protocol_error is not None:
        observation["status"] = "verifier_protocol_error"
        observation["error"] = protocol_error
        return observation

    if json_bytes is not None and json_error is not None:
        observation["status"] = "verifier_protocol_error"
        observation["error"] = json_error
        return observation
    if text_bytes is not None and text_error is not None:
        # 原始 reward 文件畸形就是 Verifier 协议错误：不因为 result.json 里
        # 恰好有个汇总值就把它当成有效分数（需求第 6 节）。
        observation["status"] = "verifier_protocol_error"
        observation["error"] = text_error
        return observation

    # 奖励**必须**来自 Verifier 的原始 reward 文件（reward.json 优先，
    # 否则 reward.txt）。``result.json`` 里的结构化奖励只用于交叉核对：
    # 只有汇总值而原始文件缺失时按"证据不足"处理，绝不用汇总值冒充已验证
    # 的分数（M3-A04：缺 reward 文件不得默认 0 或 1）。
    rewards = json_rewards
    if rewards is None and text_reward is not None:
        rewards = {"reward": text_reward}
    raw_reward_present = json_bytes is not None or text_bytes is not None

    if rewards is not None and text_reward is not None and set(rewards) == {"reward"}:
        if not math.isclose(rewards["reward"], text_reward, rel_tol=0.0, abs_tol=1e-9):
            observation["status"] = "verifier_protocol_error"
            observation["error"] = {
                "code": "VERIFIER_REWARD_SOURCE_CONFLICT",
                "message": (
                    "reward.txt and the structured reward disagree; refusing to pick "
                    "one silently (both raw files are preserved as evidence)"
                ),
                "details": {
                    "structured": rewards,
                    "structured_source": (
                        "verifier/reward.json" if json_rewards is not None
                        else "result.json#verifier_result.rewards"
                    ),
                    "reward_text": text_reward,
                },
            }
            return observation
    if not raw_reward_present and (rewards is not None or rewards_from_result is not None):
        observation["status"] = "missing_verifier_evidence"
        observation["error"] = {
            "code": "VERIFIER_RAW_REWARD_MISSING",
            "message": (
                "no verifier reward file is present; the structured summary in "
                "result.json is recorded for audit only and is not treated as a "
                "verified score"
            ),
            "details": {"result_json_rewards": rewards_from_result or rewards},
        }
        observation["detail"]["structured_rewards"] = rewards_from_result or rewards
        return observation

    if rewards_from_result is not None and rewards is not None and (
        set(rewards) != set(rewards_from_result)
        or any(
            not math.isclose(rewards[key], rewards_from_result[key], rel_tol=0.0, abs_tol=1e-9)
            for key in rewards if key in rewards_from_result
        )
    ):
        # 同一份证据里的两个奖励来源互相矛盾：不挑一个，保留两份值报协议错误。
        observation["status"] = "verifier_protocol_error"
        observation["error"] = {
            "code": "VERIFIER_REWARD_SOURCE_CONFLICT",
            "message": (
                "the raw reward file and Harbor's structured summary disagree; "
                "refusing to choose one (both values are preserved as evidence)"
            ),
            "details": {"raw_reward_file": rewards, "result_json": rewards_from_result},
        }
        return observation

    if rewards:
        observation["status"] = "scored"
        observation["rewards"] = rewards
        observation["source_path"] = (
            "verifier/reward.json" if json_rewards is not None else "verifier/reward.txt"
        )
        source_rel = {
            "verifier/reward.json": f"harbor/trials/{trial}/verifier/reward.json",
            "verifier/reward.txt": f"harbor/trials/{trial}/verifier/reward.txt",
        }[observation["source_path"]]
        data = files.get(source_rel)
        if data is not None:
            import hashlib  # noqa: PLC0415 - 只在需要 hash 时导入

            observation["source_hash"] = "sha256:" + hashlib.sha256(data).hexdigest()
        return observation

    if exception_type in VERIFIER_ERROR_EXCEPTIONS:
        observation["status"] = "verifier_error"
        observation["error"] = {
            "code": exception_type,
            "message": str(exception.get("exception_message") or exception_type),
            "details": {"phase": "verifier", "exception_type": exception_type},
        }
        return observation

    if exception_type is not None:
        # 非 Verifier 侧的异常：Verifier 没有产出证据，但不冒充"Verifier 出错"。
        observation["error"] = {
            "code": "VERIFIER_EVIDENCE_MISSING",
            "message": "verifier produced no reward evidence",
            "details": {
                "exception_type": exception_type,
                "phase": _EXCEPTION_PHASES.get(exception_type, "unknown"),
            },
        }
    return observation


def _evidence_ref(
    files: Mapping[str, bytes], rel: str, *, kind: str, complete: bool = True,
    truncated: bool = False, note: str | None = None,
) -> dict[str, Any] | None:
    data = files.get(rel)
    if data is None:
        return None
    import hashlib  # noqa: PLC0415 - 只在需要 hash 时导入

    return {
        "artifact_id": rel,
        "kind": kind,
        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "source_path": rel,
        "complete": complete,
        "truncated": truncated,
        "note": note,
    }


def _terminal_evidence(
    files: Mapping[str, bytes], trial: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """终端/轨迹证据：完整、截断或不可用，逐项标注。"""
    log = _trial_file(files, trial, "trial.log")
    agent_logs = sorted(
        rel for rel in files
        if rel.startswith(f"harbor/trials/{trial}/agent/")
    )
    refs: list[dict[str, Any]] = []
    terminal_state = "unavailable"
    if log is not None:
        truncated = len(log) > MAX_PARSE_BYTES
        text = _decode(log)
        refs.append(_evidence_ref(
            files, f"harbor/trials/{trial}/trial.log", kind="harbor-trial-log",
            complete=not truncated and text is not None,
            truncated=truncated,
            note=None if text is not None else "not valid UTF-8",
        ))
        terminal_state = "truncated" if truncated else ("complete" if text is not None else "unavailable")
    trajectory_state = "unavailable"
    for rel in agent_logs:
        data = files[rel]
        text = _decode(data)
        truncated = len(data) > MAX_PARSE_BYTES
        refs.append(_evidence_ref(
            files, rel, kind="harbor-agent-log",
            complete=not truncated and text is not None,
            truncated=truncated,
            note=None if text is not None else "not valid UTF-8",
        ))
    if agent_logs:
        trajectory_state = "complete"
        if any(
            len(files[rel]) > MAX_PARSE_BYTES or _decode(files[rel]) is None
            for rel in agent_logs
        ):
            trajectory_state = "partial"
    # 轨迹为空（真实 oracle 运行会产生 0 字节 agent 日志）时如实降级。
    if trajectory_state == "complete" and all(len(files[rel]) == 0 for rel in agent_logs):
        trajectory_state = "unavailable"
    return (
        {"terminal": terminal_state, "trajectory": trajectory_state},
        {"terminal_refs": [ref for ref in refs if ref is not None]},
    )


def _workspace_diff(
    files: Mapping[str, bytes], trial: str,
) -> dict[str, Any]:
    """workspace 工件（Harbor artifacts/）：存在即引用，缺失即标注 missing。"""
    manifest_rel = f"harbor/trials/{trial}/artifacts/manifest.json"
    refs = [
        ref for ref in (
            _evidence_ref(files, rel, kind="harbor-artifact")
            for rel in sorted(files)
            if rel.startswith(f"harbor/trials/{trial}/artifacts/")
        ) if ref is not None
    ]
    manifest = files.get(manifest_rel)
    state = "missing"
    entries: list[dict[str, Any]] = []
    if manifest is not None:
        payload, error = _load_json(manifest, "artifacts/manifest.json")
        if error is None and isinstance(payload, list):
            entries = [item for item in payload if isinstance(item, Mapping)]
            collected = [item for item in entries if item.get("status") not in ("empty", "missing")]
            state = "complete" if collected else "empty"
        else:
            state = "partial"
    return {"state": state, "manifest": entries, "refs": refs}


def _disposition_for(status: str, rewards: Mapping[str, float], cancelled: bool) -> str:
    if status == "scored":
        # 取消不覆盖已经确定的 Trial：已取得的 reward 保持其质量结论，
        # 取消只体现在 termination（需求第 6 节"保留已确定 Trial"）。
        return "succeeded" if any(value > 0 for value in rewards.values()) else "failed"
    if cancelled:
        return "cancelled"
    # 未评分的单元：任务确实跑过但没有可信的通过判断 → 不确定，不是"通过"。
    return "indeterminate"


def _coverage_for(
    files: Mapping[str, bytes], trial: str, observation: Mapping[str, Any],
    terminal: Mapping[str, Any], workspace: Mapping[str, Any], usage_known: bool,
) -> dict[str, Any]:
    reward_present = observation["status"] == "scored"
    complete = {
        "instruction": "complete" if files.get(f"harbor/trials/{trial}/instruction.md") else "unavailable",
        "config": "complete" if files.get(f"harbor/trials/{trial}/config.json") else "unavailable",
        "trajectory": terminal["trajectory"],
        "terminal": terminal["terminal"],
        "workspace": workspace["state"],
        "verifier_output": (
            "complete" if observation["evidence_refs"] else "unavailable"
        ),
        "reward": "complete" if reward_present else "unavailable",
        "usage": "complete" if usage_known else "unavailable",
    }
    missing = sorted(key for key, value in complete.items() if value == "unavailable")
    partial = sorted(
        key for key, value in complete.items() if value in ("partial", "truncated", "empty")
    )
    return {"items": complete, "missing": missing, "partial": partial}


def parse_harbor_files(
    files: Mapping[str, bytes],
    plans: Iterable[Mapping[str, Any]],
    *,
    parser_version: str = PARSER_VERSION,
    source_trial_prefix: str = "harbor/trials/",
    cancelled: bool = False,
) -> dict[str, Any]:
    """把已冻结的 Harbor 产物解析成全部计划 Trial 的结果。

    ``plans`` 是冻结的 TrialPlan（含 ``task_key``/``repeat_index``）；
    映射规则：Harbor 的 trial 目录名是 ``<task_name>__<suffix>``，
    ``task_name`` 取任务目录的 basename。平台**先按路径**（``task_id.path``
    的末段与计划里的相对路径匹配），路径不可用时才按 basename 映射，且
    同一 basename 出现多次时拒绝猜测（记录 unmapped，不合并身份）。
    """
    plans = [dict(plan) for plan in plans]
    plan_by_task: dict[str, list[dict[str, Any]]] = {}
    for plan in plans:
        plan_by_task.setdefault(str(plan["task_key"]), []).append(plan)

    frozen_plan_payload: Mapping[str, Any] = {}
    plan_bytes = files.get("harbor/plan.json")
    if plan_bytes is not None:
        parsed, _error = _load_json(plan_bytes, "plan.json")
        if isinstance(parsed, Mapping):
            frozen_plan_payload = parsed
    display_by_key = {
        str(task["task_key"]): str(task["normalized_relative_path"])
        for task in (frozen_plan_payload.get("tasks") or [])
        if isinstance(task, Mapping) and task.get("task_key")
    }
    basenames: dict[str, list[str]] = {}
    for task_key, relative in display_by_key.items():
        basenames.setdefault(relative.rstrip("/").split("/")[-1], []).append(task_key)

    trial_dirs = trial_directories(files, prefix=source_trial_prefix)
    assigned: dict[str, dict[str, Any]] = {}
    unmapped: list[dict[str, Any]] = []
    suffix_of: dict[str, str] = {}

    for trial_dir in trial_dirs:
        result_bytes = files.get(f"{source_trial_prefix}{trial_dir}/result.json")
        payload: Mapping[str, Any] = {}
        if result_bytes is not None:
            parsed, _error = _load_json(result_bytes, "result.json")
            if isinstance(parsed, Mapping):
                payload = parsed
        task_key = None
        task_id = payload.get("task_id")
        if isinstance(task_id, Mapping) and task_id.get("path"):
            tail = str(task_id["path"]).rstrip("/").split("/")[-1]
            candidates = basenames.get(tail, [])
            if len(candidates) == 1:
                task_key = candidates[0]
        if task_key is None:
            head = trial_dir.split("__", 1)[0]
            candidates = basenames.get(head, [])
            if len(candidates) == 1:
                task_key = candidates[0]
            elif len(candidates) > 1:
                unmapped.append({
                    "source_trial_id": str(payload.get("id") or trial_dir),
                    "directory": trial_dir,
                    "code": "HARBOR_TASK_NAME_AMBIGUOUS",
                    "message": (
                        f"task basename {head!r} matches {len(candidates)} planned tasks; "
                        "refusing to merge identities by name"
                    ),
                    "candidates": sorted(candidates),
                })
                continue
        if task_key is None:
            unmapped.append({
                "source_trial_id": str(payload.get("id") or trial_dir),
                "directory": trial_dir,
                "code": "HARBOR_TRIAL_UNMAPPED",
                "message": "trial directory does not match any planned task",
            })
            continue
        suffix_of[trial_dir] = str(payload.get("id") or trial_dir)
        assigned.setdefault(task_key, []).append({
            "directory": trial_dir,
            "payload": payload,
            "started_at": payload.get("started_at"),
        })

    results: list[dict[str, Any]] = []
    for task_key, task_plans in plan_by_task.items():
        ordered_plans = sorted(task_plans, key=lambda plan: int(plan["repeat_index"]))
        observed = sorted(
            assigned.get(task_key, []),
            key=lambda item: (str(item["started_at"] or "~"), item["directory"]),
        )
        for index, plan in enumerate(ordered_plans):
            result = _trial_result(
                files, plan, observed[index] if index < len(observed) else None,
                parser_version=parser_version, cancelled=cancelled,
                source_trial_prefix=source_trial_prefix,
            )
            results.append(result)
        for extra in observed[len(ordered_plans):]:
            unmapped.append({
                "source_trial_id": str(extra["payload"].get("id") or extra["directory"]),
                "directory": extra["directory"],
                "code": "HARBOR_TRIAL_UNPLANNED",
                "message": (
                    "harbor produced more trials for this task than the frozen plan; "
                    "the extra trial is kept as audit evidence and not scored"
                ),
                "task_key": task_key,
            })

    return {
        "parser_version": parser_version,
        "harbor_version": HARBOR_VERSION,
        "migrated_from": MIGRATED_FROM,
        "trial_count": len(results),
        "planned_trial_count": len(plans),
        "source_trial_count": len(trial_dirs),
        "results": results,
        "unmapped": unmapped,
        "suffixes": suffix_of,
    }


def _trial_result(
    files: Mapping[str, bytes],
    plan: Mapping[str, Any],
    observed: Mapping[str, Any] | None,
    *,
    parser_version: str,
    cancelled: bool,
    source_trial_prefix: str,
) -> dict[str, Any]:
    """单个计划 Trial 的结果；没有观测到就是 not_attempted，绝不补造证据。"""
    trial_key = str(plan["trial_id"])
    if observed is None:
        return {
            "trial_id": trial_key,
            "source_trial_id": None,
            "disposition": "cancelled" if cancelled else "not_attempted",
            "termination": {
                "reason": "cancelled_before_collection" if cancelled else "no_result_recorded",
                "agent_started": False,
            },
            "verifier_observation": {
                "status": "missing_verifier_evidence",
                "rewards": {},
                "evidence_refs": [],
                "error": None,
                "detail": {"observed": False},
            },
            "artifact_refs": [],
            "usage": {"cost_usd": None, "tokens": None, "coverage": "unavailable"},
            "coverage": {
                "items": {"trajectory": "unavailable", "terminal": "unavailable",
                          "workspace": "missing", "reward": "unavailable"},
                "missing": ["reward", "terminal", "trajectory"],
                "partial": [],
            },
            "parser_version": parser_version,
            "task_key": str(plan["task_key"]),
            "repeat_index": int(plan["repeat_index"]),
        }

    directory = str(observed["directory"])
    prefix = f"{source_trial_prefix}{directory}/"
    payload = observed["payload"]
    observation = read_verifier_observation(files, directory)
    terminal, terminal_evidence = _terminal_evidence(files, directory)
    workspace = _workspace_diff(files, directory)
    timings = _timings(payload)
    agent_result = payload.get("agent_result")
    usage: dict[str, Any] = {"cost_usd": None, "tokens": None, "coverage": "unavailable"}
    usage_known = False
    if isinstance(agent_result, Mapping):
        cost = agent_result.get("cost_usd")
        tokens = {
            "input": agent_result.get("n_input_tokens"),
            "cache": agent_result.get("n_cache_tokens"),
            "output": agent_result.get("n_output_tokens"),
        }
        tokens_seen = any(value is not None for value in tokens.values())
        coverage = "observed" if cost is not None else ("tokens_only" if tokens_seen else "unavailable")
        usage = {
            "cost_usd": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            "tokens": tokens if tokens_seen else None,
            "coverage": coverage,
            "source": "harbor-agent-result",
        }
        usage_known = coverage != "unavailable"
    exception = payload.get("exception_info")
    exception_type = None
    if isinstance(exception, Mapping):
        exception_type = str(exception.get("exception_type") or "")
    exit_code = payload.get("exit_code")
    if exit_code is None and isinstance(payload.get("agent_result"), Mapping):
        exit_code = payload["agent_result"].get("exit_code")
    termination = {
        "reason": _termination_reason(observation["status"], exception_type, cancelled),
        "exception_type": exception_type or None,
        "exception_message": (
            str(exception.get("exception_message")) if isinstance(exception, Mapping) else None
        ),
        "failure_phase": _EXCEPTION_PHASES.get(exception_type or "", None),
        "timings": timings,
        "harbor_trial_id": payload.get("id"),
        "trial_name": payload.get("trial_name"),
        "task_checksum": payload.get("task_checksum"),
        "verifier_environment_mode": payload.get("verifier_environment_mode"),
        "cancelled": cancelled,
    }
    coverage = _coverage_for(files, directory, observation, terminal, workspace, usage_known)
    disposition = _disposition_for(
        str(observation["status"]), observation["rewards"], cancelled,
    )
    # 最后一条防线：契约要求 succeeded/failed 必须来自 scored，且非 scored 不得
    # 冒充质量结论；这里再断言一次，避免未来改动悄悄破坏语义。
    if disposition in ("succeeded", "failed") and observation["status"] != "scored":
        raise HarborParseError(
            "HARBOR_DISPOSITION_INCONSISTENT",
            f"trial {trial_key} has disposition {disposition} without a scored reward",
        )
    if disposition not in ("succeeded", "failed") and observation["status"] == "scored":
        raise HarborParseError(
            "HARBOR_DISPOSITION_INCONSISTENT",
            f"trial {trial_key} has a scored reward but disposition {disposition}",
        )

    artifact_refs = [
        ref for ref in (
            _evidence_ref(files, prefix + "result.json", kind="harbor-trial-result"),
            _evidence_ref(files, prefix + "config.json", kind="harbor-trial-config"),
            _evidence_ref(files, prefix + "lock.json", kind="harbor-trial-lock"),
            _evidence_ref(files, prefix + "instruction.md", kind="harbor-instruction"),
            *terminal_evidence["terminal_refs"],
            *workspace["refs"],
        ) if ref is not None
    ]
    termination["job_finished"] = isinstance(payload.get("finished_at"), str)
    return {
        "trial_id": trial_key,
        "source_trial_id": str(payload.get("id") or directory),
        "disposition": disposition,
        "termination": termination,
        "verifier_observation": observation,
        "artifact_refs": artifact_refs,
        "usage": usage,
        "coverage": coverage,
        "parser_version": parser_version,
        "task_key": str(plan["task_key"]),
        "repeat_index": int(plan["repeat_index"]),
        "source_directory": directory,
    }


def _termination_reason(status: str, exception_type: str | None, cancelled: bool) -> str:
    if cancelled:
        return "cancelled"
    if exception_type:
        phase = _EXCEPTION_PHASES.get(exception_type)
        return f"{phase}_error" if phase else "runner_error"
    if status == "scored":
        return "completed"
    if status == "missing_verifier_evidence":
        return "verifier_evidence_missing"
    return "completed_without_reward"
