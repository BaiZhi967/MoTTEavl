"""比较/门禁装配服务（M6-T03 Lite + M2-T09 + review R12）。

以固定 Run **+ 指定 ScoringPass**（缺省 current pass）为输入做比较与 Gate
求值；全程只读，0 次 Runner/Judge/模型调用。C-Eval/CMMLU 直接消费本
服务——不存在专用 Gate 引擎。

review R12 修复：

- ``candidate_summary`` 从**该 pass 的不可变 ScoreSet**（或 pass summary 的
  冻结 aggregate）求值，不再从 case_runs 现场重算——rescore 后旧 pass 引用
  及其结论不漂移；
- 无 ScoringPass 的输入（未完成/queued 的 Run）抛 ``NO_SCORING_EVIDENCE``，
  不能凭一条原始正确预测冒充已固定报告；
- Gate 需要终态 Run；baseline 可引用不可变快照（baselines store）而非
  只能是活 Run；
- 输出记录所用 RunReportRef（run + scoring_pass + 证据 hash）。
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from motte_contracts.comparison import ComparisonPolicy, RunReportRef
from motte_eval.comparison import ComparisonResult, compare_run_reports
from motte_eval.coverage import coverage_summary
from motte_eval.gates import evaluate_gate

#: Terminal-Bench（Harbor）的 suite 身份：指标与覆盖按 **Trial 口径** 装配。
TERMINAL_BENCH_SUITE = "terminal-bench-harbor"
#: ``motte_eval.harbor.task_aggregate`` 冻结在 pass summary 里的 Trial 指标。
_TRIAL_AGGREGATE_KEYS = (
    "valid_trial_pass_rate", "valid_trial_coverage", "selected_trials",
    "valid_trials", "valid_pass_trials", "invalid_trials",
)


def _is_terminal_bench_run(manifest: Mapping[str, Any]) -> bool:
    provenance = manifest.get("benchmark_provenance")
    return isinstance(provenance, dict) and provenance.get("suite") == TERMINAL_BENCH_SUITE


def _planned_trials(manifest: Mapping[str, Any]) -> list[Any]:
    """冻结计划里的全部 Trial；两个冻结位置互为缺省（review R18）。"""
    task_manifest = manifest.get("task_manifest")
    plan = task_manifest.get("trials") if isinstance(task_manifest, dict) else None
    if not isinstance(plan, list):
        external = manifest.get("external_benchmark")
        runner_config = external.get("runner_config") if isinstance(external, dict) else None
        plan_config = runner_config.get("plan") if isinstance(runner_config, dict) else None
        plan = plan_config.get("trials") if isinstance(plan_config, dict) else None
    return plan if isinstance(plan, list) else []


def _is_trial_aggregate(aggregate: Mapping[str, Any]) -> bool:
    return all(key in aggregate for key in _TRIAL_AGGREGATE_KEYS)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _trial_row_counts(scores: list[dict[str, Any]]) -> tuple[int, int, int, int]:
    """ScoreSet 的 Trial 行 → (观测, 有效, 通过, 无效)：``denominator`` 决定资格。"""
    rows = [row for row in scores if row.get("unit") == "trial"]
    valid = [row for row in rows if row.get("denominator") is True]
    passed = [row for row in valid if row.get("passed") is True]
    return len(rows), len(valid), len(passed), len(rows) - len(valid)


def _terminal_bench_summary(
    manifest: Mapping[str, Any],
    record: Mapping[str, Any],
    scores: list[dict[str, Any]],
) -> dict[str, Any]:
    """Terminal-Bench 的覆盖与指标（review R18）：分母是**计划 Trial**。

    优先用该 ScoringPass 冻结的 ``summary["aggregate"]``（``motte_eval.harbor.
    task_aggregate`` 的口径）；缺 aggregate 时按该 pass 的 ScoreSet + 冻结计划
    重算同一口径。质量与覆盖分开：``valid_trial_pass_rate`` 的分母是有效
    Trial，``valid_trial_coverage`` 的分母是计划 Trial；成本未知时保持 None。
    """
    summary = record.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    aggregate = summary.get("aggregate")
    planned = len(_planned_trials(manifest))
    cost_known = False
    cost_total: float | None = None
    frozen_rate: float | None = None
    if isinstance(aggregate, dict) and _is_trial_aggregate(aggregate):
        observed = _count(aggregate.get("observed_trials"))
        valid = _count(aggregate.get("valid_trials"))
        passed = _count(aggregate.get("valid_pass_trials"))
        invalid = _count(aggregate.get("invalid_trials"))
        planned = planned or _count(aggregate.get("selected_trials"))
        frozen_rate = _number(aggregate.get("valid_trial_pass_rate"))
        cost = aggregate.get("cost") if isinstance(aggregate.get("cost"), dict) else {}
        # 只有成本完整（有已知成本且明确没有未知 Trial）时总额才可用，否则保持 None。
        cost_known = (
            _count(cost.get("known_trials")) > 0 and cost.get("unknown_cost") is False
        )
        cost_total = _number(cost.get("known_cost_usd")) if cost_known else None
    else:
        observed, valid, passed, invalid = _trial_row_counts(scores)
    # 计划缺失或短于观测时退回观测数：分母不小于已经观测到的事实。
    selected = max(planned, observed)
    pass_rate = frozen_rate if frozen_rate is not None else (
        round(passed / valid, 6) if valid else None
    )
    view = coverage_summary(
        denominator="planned_trials",
        selected=selected,
        judged=valid,
        scored=passed,
        attempted=observed,
        unknown=invalid,
        cost={"known": cost_known, "total_usd": cost_total, "currency": "USD"},
        metric_values={
            "valid_trial_pass_rate": pass_rate,
            "cost.total_usd": cost_total,
        },
    )
    # 覆盖值以 coverage_summary 的计算为准（judged / planned）。
    view["metric_values"]["valid_trial_coverage"] = view["coverage"]
    return view


class ComparisonError(ValueError):
    """比较/门禁输入不满足固定报告前置；``code`` 供 API 映射 422。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _current_pass_id(store: Any, run_id: str) -> str | None:
    passes = getattr(store, "scoring_passes", None)
    if passes is not None:
        current = passes.current(run_id)
        if current is not None:
            return str(current.get("id"))
    return None


def _resolve_pass(store: Any, run_id: str, scoring_pass_id: str | None) -> dict[str, Any]:
    """解析指定 pass（缺省 current）；不存在即无评分证据。"""
    passes = getattr(store, "scoring_passes", None)
    if passes is None:
        raise ComparisonError("NO_SCORING_EVIDENCE", "store has no scoring passes")
    resolved_id = scoring_pass_id or _current_pass_id(store, run_id)
    if resolved_id is None:
        raise ComparisonError(
            "NO_SCORING_EVIDENCE",
            f"run {run_id} has no scoring pass; an unfixed report cannot be compared or gated",
        )
    for record in passes.list_for_run(run_id) if hasattr(passes, "list_for_run") else []:
        if str(record.get("id")) == str(resolved_id):
            return record
    current = passes.current(run_id)
    if current is not None and str(current.get("id")) == str(resolved_id):
        return current
    raise ComparisonError(
        "SCORING_PASS_NOT_FOUND",
        f"scoring pass {resolved_id} not found for run {run_id}",
    )


#: pass.judge 里属于评分身份的字段（Judge 写入的真实形状，见
#: motte_eval.judge.JudgeSpec.as_summary）。历史 pass 缺这些字段时保持
#: unknown，绝不从 subject 的原 evaluator 或当前设置补齐。
_PROVENANCE_JUDGE_FIELDS: tuple[str, ...] = (
    "judge_profile_id", "mode", "profile_sha256", "spec_sha256", "model",
    "prompt", "prompt_sha256", "rubric_id", "rubric_version", "rubric_sha256",
    "calibration_version", "input_selector", "missing_evidence_policy",
)

#: 人工修订沿血缘回溯的层数上限：只走已保存的 pass 引用，容忍损坏的血缘环。
_MANUAL_REVISION_LINEAGE_LIMIT = 8


def _manual_revision_summary(record: Mapping[str, Any]) -> dict[str, Any] | None:
    value = record.get("manual_revision")
    return dict(value) if isinstance(value, dict) else None


def _instrument_pass(store: Any, record: Mapping[str, Any]) -> Mapping[str, Any]:
    """评分工具的出处：人工修订沿**已保存的血缘**回退到被修订的 pass。

    只读 pass 之间记录在案的引用（manual_revision.source_pass_id /
    previous_pass_id）：不读 current 指针，也不读任何当前资源设置；血缘缺失
    就停在该 pass 上，身份保持 unknown（review F09）。
    """
    current: Mapping[str, Any] = record
    run_id = record.get("run_id")
    seen: set[str] = set()
    for _ in range(_MANUAL_REVISION_LINEAGE_LIMIT):
        judge = current.get("judge")
        if isinstance(judge, dict) and judge:
            return current
        if current.get("source") != "manual_revision":
            return current
        manual = _manual_revision_summary(current) or {}
        source_id = manual.get("source_pass_id") or current.get("previous_pass_id")
        if not isinstance(source_id, str) or not source_id or source_id in seen:
            return current
        seen.add(source_id)
        passes = getattr(store, "scoring_passes", None)
        parent = passes.get(source_id) if passes is not None else None
        if not isinstance(parent, dict):
            return current
        # 血缘只在同一个 Run 内成立：跨 Run 引用保持 unknown，不继承外来身份。
        if run_id is not None and parent.get("run_id") != run_id:
            return current
        current = parent
    return current


def _scoring_provenance(store: Any, record: Mapping[str, Any]) -> dict[str, Any]:
    """所选 pass 的真实评分身份（review F09）。

    profile / spec / prompt / rubric / input-selector / calibration / 人工修订
    全部只从该 pass（及其记录在案的血缘）读取；历史缺字段保持 unknown。
    """
    instrument = _instrument_pass(store, record)
    judge = instrument.get("judge")
    judge = judge if isinstance(judge, dict) else {}
    provenance: dict[str, Any] = {
        "scorer_id": instrument.get("scorer_id"),
        "scorer_version": instrument.get("scorer_version"),
    }
    for field_name in _PROVENANCE_JUDGE_FIELDS:
        provenance[field_name] = judge.get(field_name)
    manual = _manual_revision_summary(record)
    provenance["manual_revision"] = (
        {
            "revision_id": manual.get("revision_id") or record.get("id"),
            "source_pass_id": (
                manual.get("source_pass_id") or record.get("previous_pass_id")
            ),
            "actor": manual.get("actor"),
            "reason": manual.get("reason"),
            "changed_metrics": manual.get("changed_metrics"),
            # 人工修订自身的 scorer 记在修订块；评分工具身份继承来源 pass。
            "scorer_id": record.get("scorer_id"),
            "scorer_version": record.get("scorer_version"),
        }
        if manual is not None
        else None
    )
    return provenance


class ComparisonService:
    """读取固定 Run（+ScoringPass）事实 → 比较/覆盖/Gate（同一 M6-Lite 服务）。"""

    def __init__(self, store: Any, baselines: Any = None) -> None:
        self.store = store
        self.baselines = baselines

    # ------------------------------------------------------------------ 视图

    def report_ref(
        self, run_id: str, *, scoring_pass_id: str | None = None,
    ) -> RunReportRef:
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        import hashlib
        import json

        manifest = run.get("manifest") or {}
        scoring_pass = _resolve_pass(self.store, run_id, scoring_pass_id)
        interventions = (scoring_pass.get('summary') or {}).get('interventions')
        # 报告引用绑定**所选 pass 的评分身份**（F09）：不同的评分工具不能得到
        # 同一个 evidence hash。人工修订的身份继承其记录在案的来源 pass。
        evidence: dict[str, Any] = {
            "manifest": manifest,
            "scoring_provenance": _scoring_provenance(self.store, scoring_pass),
        }
        if interventions:
            evidence["interventions"] = interventions
        digest = hashlib.sha256(json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        # 显式 pass 与 current 同一校验：归属与存在性都由 _resolve_pass 把关
        # （review R2-08：不存在的 pass 不能参与比较）。
        pass_id = str(scoring_pass['id'])
        return RunReportRef(
            run_id=run_id,
            scoring_pass_id=pass_id,
            report_schema="report-v1",
            evidence_hash="sha256:" + digest,
        )

    def _manifest_view(self, run_id: str, scoring_pass_id: str | None = None) -> dict[str, Any]:
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        manifest = run.get("manifest") or {}
        view = dict(manifest)
        view["case_ids"] = list(run.get("case_ids") or [])
        scoring_pass = _resolve_pass(self.store, run_id, scoring_pass_id)
        view['interventions'] = (scoring_pass.get('summary') or {}).get('interventions') or {}
        # F09：比较消费所选 pass 的真实评分身份，而不是 manifest 里 subject 的
        # 原 evaluator，也不是被比较时的当前 Judge/资源设置。历史缺字段保持
        # unknown，绝不补齐。
        view["scoring_provenance"] = _scoring_provenance(self.store, scoring_pass)
        return view

    def _score_rows(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        if self.store.score_sets is None:
            return []
        return list(self.store.score_sets.list_for_pass(str(record.get("id"))))

    def candidate_summary(
        self, run_id: str, *, scoring_pass_id: str | None = None,
    ) -> dict[str, Any]:
        """从固定 ScoringPass 的不可变事实计算覆盖与指标（只读）。

        优先使用 pass summary 冻结的 aggregate（创建 pass 时已算好）；
        缺 aggregate 时按该 pass 的 ScoreSet 重算同样的口径。Run 的
        case_ids 只作分母，不再从 case_runs 现场拼分子（review R12）。
        Terminal-Bench 的 Run 走 Trial 口径（review R18）：分母是计划 Trial，
        指标是 ``valid_trial_pass_rate`` / ``valid_trial_coverage``。
        """
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        record = _resolve_pass(self.store, run_id, scoring_pass_id)
        manifest = run.get("manifest") or {}
        if _is_terminal_bench_run(manifest):
            return _terminal_bench_summary(manifest, record, self._score_rows(record))
        selected_ids = list(run.get("case_ids") or [])
        selected = len(selected_ids)
        summary = record.get("summary") or {}
        aggregate = summary.get("aggregate") or {}
        if isinstance(aggregate, dict) and aggregate.get("accuracy") is not None:
            accuracy = aggregate.get("accuracy")
            attempted = int(aggregate.get("attempted") or 0)
            correct = int(aggregate.get("correct") or 0)
        else:
            scores = self.store.score_sets.list_for_pass(
                str(record.get("id")),
            ) if self.store.score_sets is not None else []
            correct = sum(1 for score in scores if score.get("passed") is True)
            attempted = sum(
                1 for score in scores if score.get("passed") is not None
            )
            expectations = (run.get("manifest") or {}).get("case_expectations")
            if isinstance(expectations, dict) and expectations:
                unscored = any(
                    expectations.get(case_id) in (None, "")
                    for case_id in selected_ids
                )
            else:
                # 无冻结期望的旧报告：按该 pass 的 ScoreSet 判定 unscored。
                unscored = any(score.get("passed") is None for score in scores)
            accuracy = (
                round(correct / selected, 6)
                if selected and not unscored else None
            )
        cost_known = bool((run.get("manifest") or {}).get("cost", {}).get("known"))
        total_usd = (
            (run.get("manifest") or {}).get("cost", {}).get("total_usd")
            if cost_known else None
        )
        return coverage_summary(
            denominator="selected_cases",
            selected=selected,
            judged=attempted,
            scored=correct,
            metric_value=accuracy,
            cost={"known": cost_known, "total_usd": total_usd, "currency": "USD"},
            metric_values={
                "accuracy": accuracy,
                "cost.total_usd": total_usd,
            },
        )

    # ------------------------------------------------------------------ 比较

    def compare(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
        *,
        allowed_factors: list[str] | tuple[str, ...],
        baseline_pass_id: str | None = None,
        candidate_pass_id: str | None = None,
    ) -> ComparisonResult:
        return compare_run_reports(
            self.report_ref(baseline_run_id, scoring_pass_id=baseline_pass_id),
            self.report_ref(candidate_run_id, scoring_pass_id=candidate_pass_id),
            baseline_manifest=self._manifest_view(baseline_run_id, baseline_pass_id),
            candidate_manifest=self._manifest_view(candidate_run_id, candidate_pass_id),
            policy=ComparisonPolicy(allowed_factors=tuple(allowed_factors)),
        )

    # ------------------------------------------------------------------ 门禁

    def evaluate_gate(
        self,
        run_id: str,
        *,
        policy: dict[str, Any],
        baseline_run_id: str | None = None,
        scoring_pass_id: str | None = None,
        baseline_snapshot_id: str | None = None,
        require_terminal: bool = True,
    ) -> dict[str, Any]:
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        terminal = {"completed", "failed", "cancelled", "unsupported", "needs_review"}
        if require_terminal and run.get("status") not in terminal:
            raise ComparisonError(
                "RUN_NOT_TERMINAL",
                f"run {run_id} is {run.get('status')!r}; gates evaluate fixed, "
                "terminal reports only",
            )
        candidate = self.candidate_summary(run_id, scoring_pass_id=scoring_pass_id)
        candidate_ref = self.report_ref(run_id, scoring_pass_id=scoring_pass_id)
        comparison_view: dict[str, Any] | None = None
        baseline_view: dict[str, Any] | None = None
        if baseline_snapshot_id is not None:
            # Baseline 快照只提供固定报告的引用（run+pass）；可比性判断
            # 与普通路径走**同一比较算法**（review R2-04）：快照自带的
            # comparable 布尔不再被信任。
            if self.baselines is None:
                raise ComparisonError(
                    "BASELINE_STORE_MISSING",
                    "BASELINE_STORE_MISSING: baseline snapshots requested but no "
                    "baseline store is wired",
                )
            snapshot = self.baselines.get(baseline_snapshot_id)
            if snapshot is None:
                raise ComparisonError(
                    "BASELINE_NOT_FOUND",
                    f"BASELINE_NOT_FOUND: baseline snapshot {baseline_snapshot_id} not found",
                )
            base_run_id = snapshot.get("run_id")
            base_pass_id = snapshot.get("scoring_pass_id")
            if not isinstance(base_run_id, str) or not base_run_id or not isinstance(base_pass_id, str) or not base_pass_id:
                raise ComparisonError(
                    "BASELINE_INCOMPLETE",
                    "BASELINE_INCOMPLETE: baseline snapshot "
                    f"{baseline_snapshot_id} has no fixed "
                    "run_id/scoring_pass_id; cannot resolve a report to compare",
                )
            result = self.compare(
                base_run_id, run_id,
                allowed_factors=policy.get("allowed_factors") or ["model"],
                baseline_pass_id=base_pass_id,
                candidate_pass_id=scoring_pass_id,
            )
            comparison_view = {
                "eligible": result.eligible,
                "reasons": list(result.reasons),
            }
            baseline_view = {
                "baseline_snapshot_id": baseline_snapshot_id,
                "run_id": base_run_id,
                "scoring_pass_id": base_pass_id,
            }
        elif baseline_run_id is not None and policy.get("require_comparable"):
            result = self.compare(
                baseline_run_id, run_id,
                allowed_factors=policy.get("allowed_factors") or ["model"],
                # 固定的候选 pass 不能被 current 指针替换（F09）：可比性判断与
                # candidate_summary/report_ref 必须消费同一个 pass。
                candidate_pass_id=scoring_pass_id,
            )
            comparison_view = {
                "eligible": result.eligible,
                "reasons": list(result.reasons),
            }
            baseline_view = {
                "run_id": baseline_run_id,
                "scoring_pass_id": _current_pass_id(self.store, baseline_run_id),
            }
        conclusion = evaluate_gate(policy, candidate, comparison=comparison_view)
        conclusion["report_refs"] = {
            "candidate": {
                "run_id": candidate_ref.run_id,
                "scoring_pass_id": candidate_ref.scoring_pass_id,
                "evidence_hash": candidate_ref.evidence_hash,
            },
            **({"baseline": baseline_view} if baseline_view else {}),
        }
        return conclusion
