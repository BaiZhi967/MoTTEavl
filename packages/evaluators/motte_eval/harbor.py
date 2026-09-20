"""Harbor/Terminal-Bench 评分（M3-T08，需求第 6/7 节）。

三条硬规则：

1. **质量与覆盖分开**。``valid_trial_pass_rate`` 的分母是**有效 Trial**
   （Verifier 给出可判定 reward 的 Trial），``valid_trial_coverage`` 的分母
   是**计划 Trial**。三 Trial 为 1 / 0 / Verifier 超时时：通过率 1/2、覆盖率
   2/3，完整覆盖 Gate 不放行——不把出错的 Trial 悄悄删掉后只报 50%。
2. **reward=0 是有效失败**，不是缺失；Agent 退出 0 不覆盖它。Verifier 异常/
   证据缺失既不通过也不当成 0 分，而是降低覆盖。
3. **聚合规则事前固定**（``first-trial`` / ``mean-success``），不默认择优；
   没有可用值时返回不适用，而不是扩分母或自动补跑。
4. pass@k 等完整统计属于 M6：这里只提供 Trial 原始值与事前固定的 Task 通过
   规则，并把 Trial 资格（有效/无效、覆盖）交给下游。

重评分只读冻结证据：本模块不启动任务、不调用模型。
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

from motte_contracts.trial import TrialDisposition, VerifierStatus

TRIAL_METRIC_ID = "reward"
TRIAL_EVALUATOR_ID = "harbor-verifier"
#: 评分器版本：Harbor 侧 Verifier 协议 + 平台解析口径。
TRIAL_EVALUATOR_VERSION = "harbor-terminal-bench@1"

#: Verifier 给出可判定 reward 的状态。
VALID_VERIFIER_STATUSES = frozenset({VerifierStatus.scored.value})
#: 任务层聚合规则（事前固定，不默认择优）。
AGGREGATION_POLICIES = ("first-trial", "mean-success")
#: 覆盖门禁的最小完整度（计划覆盖不足时不放行）。
FULL_COVERAGE_THRESHOLD = 1.0


def trial_row(trial: Mapping[str, Any]) -> dict[str, Any]:
    """把一条 TrialResult 投影成 Trial 层 Score 行（含 trial_id 维度）。"""
    observation = trial.get("verifier_observation") or {}
    status = str(observation.get("status") or VerifierStatus.missing_verifier_evidence.value)
    rewards = observation.get("rewards") or {}
    reward = rewards.get(TRIAL_METRIC_ID)
    if not isinstance(reward, (int, float)) or isinstance(reward, bool):
        reward = None
    valid = status in VALID_VERIFIER_STATUSES and reward is not None
    passed = (reward > 0) if valid else None
    coverage = trial.get("coverage") or {}
    return {
        "case_id": str(trial.get("task_key") or ""),
        "trial_id": str(trial.get("trial_id") or ""),
        "metric_id": TRIAL_METRIC_ID,
        "evaluator_id": TRIAL_EVALUATOR_ID,
        "evaluator_version": TRIAL_EVALUATOR_VERSION,
        "value": reward,
        "passed": passed,
        "attempted": str(trial.get("disposition")) != TrialDisposition.not_attempted.value,
        "judged": valid,
        "outcome": status,
        "metric_status": status,
        "unit": "trial",
        "denominator": valid,
        "reason": _trial_reason(trial, status, valid),
        "scorer": "harbor-terminal-bench",
        "scorer_version": TRIAL_EVALUATOR_VERSION,
        "details": {
            "disposition": trial.get("disposition"),
            "rewards": rewards,
            "termination": trial.get("termination") or {},
            "coverage": coverage,
            "repeat_index": trial.get("repeat_index"),
            "source_trial_id": trial.get("source_trial_id"),
            "parser_version": trial.get("parser_version"),
        },
    }


def _trial_reason(trial: Mapping[str, Any], status: str, valid: bool) -> str:
    if valid:
        return "scored"
    if status == VerifierStatus.verifier_error.value:
        return "verifier_error"
    if status == VerifierStatus.verifier_protocol_error.value:
        return "verifier_protocol_error"
    if str(trial.get("disposition")) == TrialDisposition.not_attempted.value:
        return "not_attempted"
    if str(trial.get("disposition")) == TrialDisposition.cancelled.value:
        return "cancelled"
    return "missing_verifier_evidence"


def trial_scores(
    trials: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """全部计划 Trial 的 Trial 层分数行（含无效行，供覆盖计算）。"""
    return [trial_row(trial) for trial in trials]


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def task_aggregate(
    *,
    trials: Sequence[Mapping[str, Any]],
    aggregation: str = "first-trial",
    planned_per_task: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Task 与 Trial 两层聚合：分母/单位明确，覆盖门禁单独判断。"""
    if aggregation not in AGGREGATION_POLICIES:
        raise ValueError(
            f"unknown aggregation policy {aggregation!r} "
            f"(registered: {sorted(AGGREGATION_POLICIES)})",
        )
    rows = trial_scores(trials)
    by_task: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_task.setdefault(row["case_id"], []).append(row)

    valid_rows = [row for row in rows if row["judged"]]
    valid_pass = sum(1 for row in valid_rows if row["passed"])
    valid_fail = sum(1 for row in valid_rows if row["passed"] is False)
    invalid = len(rows) - len(valid_rows)

    per_task: dict[str, Any] = {}
    for task_key, task_rows in list(by_task.items()):
        if not task_rows:
            continue
        ordered = sorted(task_rows, key=lambda row: row["details"].get("repeat_index") or 0)
        judged = [row for row in ordered if row["judged"]]
        planned = int((planned_per_task or {}).get(task_key, len(ordered)))
        task_pass = None
        if judged:
            if aggregation == "first-trial":
                task_pass = bool(judged[0]["passed"])
            else:
                task_pass = (sum(1 for row in judged if row["passed"]) / len(judged)) >= 0.5
        per_task[task_key] = {
            "planned_trials": planned,
            "observed_trials": len(ordered),
            "valid_trials": len(judged),
            "passed_trials": sum(1 for row in judged if row["passed"]),
            "failed_trials": sum(1 for row in judged if row["passed"] is False),
            "invalid_trials": len(ordered) - len(judged),
            "task_pass": task_pass,
            "task_pass_reason": (
                "no_valid_trial" if task_pass is None else f"{aggregation}"
            ),
        }

    # 计划里有、但完全没有产出的 Task 也必须有行：否则"没跑"会从视图里消失，
    # 覆盖统计看起来反而完美（M3-G10）。
    for task_key, planned in (planned_per_task or {}).items():
        if task_key in by_task:
            continue
        by_task[task_key] = []
        per_task[task_key] = {
            "planned_trials": int(planned),
            "observed_trials": 0,
            "valid_trials": 0,
            "passed_trials": 0,
            "failed_trials": 0,
            "invalid_trials": int(planned),
            "task_pass": None,
            "task_pass_reason": "no_valid_trial",
        }

    selected_tasks = len(by_task) if by_task else 0
    scored_tasks = sum(1 for item in per_task.values() if item["task_pass"] is not None)
    selected_trials = sum(item["planned_trials"] for item in per_task.values())
    observed_trials = len(rows)
    valid_trials = len(valid_rows)
    passed_tasks = sum(1 for item in per_task.values() if item["task_pass"] is True)

    valid_pass_rate = (
        round(valid_pass / valid_trials, 6) if valid_trials else None
    )
    valid_coverage = (
        round(valid_trials / selected_trials, 6) if selected_trials else None
    )
    observed_coverage = (
        round(observed_trials / selected_trials, 6) if selected_trials else None
    )
    return {
        "aggregation": aggregation,
        "unit": "trial",
        "denominator": "planned_trials",
        "selected_tasks": selected_tasks,
        "scored_tasks": scored_tasks,
        "passed_tasks": passed_tasks,
        "selected_trials": selected_trials,
        "observed_trials": observed_trials,
        "valid_trials": valid_trials,
        "valid_pass_trials": valid_pass,
        "valid_fail_trials": valid_fail,
        "invalid_trials": invalid,
        # 任务层通过率：分母是"有有效 Trial 的 Task"，与覆盖率分开。
        "task_pass_rate": (
            round(passed_tasks / scored_tasks, 6) if scored_tasks else None
        ),
        # 质量：分母是有效 Trial。覆盖：分母是计划 Trial。
        "valid_trial_pass_rate": valid_pass_rate,
        "valid_trial_coverage": valid_coverage,
        "observed_trial_coverage": observed_coverage,
        "trial_completeness": valid_coverage,
        "task_completeness": (
            round(scored_tasks / selected_tasks, 6) if selected_tasks else None
        ),
        "per_task": per_task,
    }


def coverage_gate(
    aggregate: Mapping[str, Any], *, required_coverage: float = FULL_COVERAGE_THRESHOLD,
    required_pass_rate: float | None = None,
) -> dict[str, Any]:
    """完整覆盖 Gate：覆盖不足时明确不放行，并给出原因。

    覆盖门禁只回答"证据是否足以支持一个结论"，不替用户决定质量阈值。
    """
    coverage = _as_float(aggregate.get("valid_trial_coverage"))
    pass_rate = _as_float(aggregate.get("valid_trial_pass_rate"))
    reasons: list[str] = []
    if coverage is None:
        reasons.append("INSUFFICIENT_EVIDENCE:no_valid_trial_coverage")
    elif coverage + 1e-9 < required_coverage:
        reasons.append(
            f"INSUFFICIENT_EVIDENCE:valid_trial_coverage={coverage}<{required_coverage}",
        )
    if required_pass_rate is not None:
        if pass_rate is None:
            reasons.append("INSUFFICIENT_EVIDENCE:no_valid_trial_pass_rate")
        elif pass_rate + 1e-9 < required_pass_rate:
            reasons.append(f"QUALITY_BELOW_THRESHOLD:{pass_rate}<{required_pass_rate}")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "required_coverage": required_coverage,
        "required_pass_rate": required_pass_rate,
        "valid_trial_coverage": coverage,
        "valid_trial_pass_rate": pass_rate,
        "invalid_trials": aggregate.get("invalid_trials"),
        "selected_trials": aggregate.get("selected_trials"),
        "valid_trials": aggregate.get("valid_trials"),
    }


def cost_summary(
    trials: Iterable[Mapping[str, Any]],
    *,
    price_table_version: str | None = None,
    currency: str = "USD",
) -> dict[str, Any]:
    """成本视图：已知小计、未知 Trial 数、计量来源与单位成本。

    - 未知成本保持 ``null`` 并计数，不填 0；
    - 成功单位成本分母为零时返回 ``None``（不适用）；
    - 明确说明失败 Trial 的费用是否计入分子（这里：计入全部已观测 Trial）。
    """
    known_total = 0.0
    known_trials = 0
    unknown_trials = 0
    for trial in trials:
        usage = trial.get("usage") or {}
        cost = _as_float(usage.get("cost_usd"))
        if cost is None:
            unknown_trials += 1
            continue
        known_total += cost
        known_trials += 1
    rows = trial_scores(trials)
    valid = [row for row in rows if row["judged"]]
    successes = sum(1 for row in valid if row["passed"])
    return {
        "known_cost_usd": round(known_total, 8) if known_trials else None,
        "known_trials": known_trials,
        "unknown_trials": unknown_trials,
        "unknown_cost": unknown_trials > 0,
        "currency": currency if known_trials else None,
        "price_table_version": price_table_version,
        "metering_source": "harbor-agent-result",
        "per_success_usd": (
            round(known_total / successes, 8) if successes else None
        ),
        "successes": successes,
        "includes_failed_trials_in_numerator": True,
        "note": (
            "total covers every trial that reported cost, including failed trials; "
            "per-success divides that total by successful trials"
        ),
    }


def duration_summary(trials: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """时长视图：Agent / Verifier / 环境各自保留，不混成单一延迟。"""
    buckets: dict[str, list[float]] = {
        "environment_setup_sec": [], "agent_setup_sec": [], "agent_execution_sec": [],
        "verifier_sec": [], "total_sec": [],
    }
    for trial in trials:
        timings = (trial.get("termination") or {}).get("timings") or {}
        for key in buckets:
            value = _as_float(timings.get(key))
            if value is not None:
                buckets[key].append(value)
    return {
        key: {
            "observed_trials": len(values),
            "total_sec": round(sum(values), 6) if values else None,
            "mean_sec": round(sum(values) / len(values), 6) if values else None,
        }
        for key, values in buckets.items()
    }


def rescore_identity(
    *, aggregate: Mapping[str, Any], trials: Sequence[Mapping[str, Any]],
    parser_version: str,
) -> dict[str, Any]:
    """重评分的比较条件：任务内容集合、Agent/环境配置、重复数与聚合规则。

    重评分只读冻结证据，因此这些条件不变时新旧 pass 的差异只可能来自评分
    规则版本（``evaluator_version``），而不是任务被重跑。
    """
    from motte_contracts.trial import canonical_hash

    task_keys = sorted({str(trial.get("task_key")) for trial in trials})
    return {
        "task_keys": task_keys,
        "task_set_hash": canonical_hash(task_keys),
        "planned_trials": aggregate.get("selected_trials"),
        "aggregation": aggregate.get("aggregation"),
        "parser_version": parser_version,
        "evaluator_version": TRIAL_EVALUATOR_VERSION,
    }
