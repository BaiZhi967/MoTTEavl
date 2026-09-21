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

review R9 修复：

- 无冻结 aggregate 时，``candidate_summary`` 的覆盖与质量按 **Case** 计，
  并与 ``motte_eval`` 的 ``denominator`` 口径一致：多指标 pass 的同一 Case
  只算一个 attempted case，非分母行不占分母，全部入分母行都通过才算通过；
  不再把 3 条指标行当成 3 个 attempted case 而抛
  ``ValueError: attempted N exceeds selected M``。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping

from pydantic import ValidationError

from motte_contracts.comparison import (
    DISPOSITION_COUNT_KEYS,
    BaselineEntry,
    BaselineSnapshot,
    CaseDispositionRecord,
    ComparisonPolicy,
    CostEntry,
    CostSummary,
    DefaultBaselinePointer,
    ReportSnapshot,
    RunReportRef,
)
from motte_contracts.gates import GatePolicyVersion
from motte_contracts.metrics import METRIC_REGISTRY_VERSION, lookup_metric
from motte_eval.comparison import ComparisonResult, _model_identity, compare_run_reports
from motte_eval.coverage import coverage_summary
from motte_eval.gates import (
    _rule_direction_reason,
    evaluate_gate,
    evaluate_gate_policy,
    gate_exit_code,
)
from motte_eval.regression import regression_report

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


def _case_row_counts(scores: list[dict[str, Any]]) -> tuple[int, int]:
    """ScoreSet 行 → (attempted cases, passed cases)：与 motte_eval 分母口径一致。

    覆盖和质量都按 **Case** 计，而不是按指标行计（review R9）：

    - ``denominator=False`` 的非分母行（insufficient_evidence /
      evaluator_error / not_applicable）不占分母，也不参与 Case 判定；
    - 一个 Case 的多条指标行只算**一个** attempted case（按 case_id 去重）；
    - 一个 Case 只有在**它的全部入分母行都通过**时才算通过。这正是
      ``motte_eval.workflow.goal-achieved`` 的合取口径（任一分量确认失败
      即整题失败），也保证注册指标 ``accuracy``（unit=ratio、分母
      selected_cases）不会超过 1；
    - 缺 ``denominator`` 的历史单指标行按"有 passed 即入分母"读取：该字段
      是后加的，旧行不能因此掉出分母。
    """
    attempted: set[str] = set()
    failed: set[str] = set()
    for score in scores:
        if score.get("denominator") is False:
            continue
        case_id = score.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            continue
        if score.get("passed") is None:
            continue
        attempted.add(case_id)
        if score.get("passed") is not True:
            failed.add(case_id)
    return len(attempted), len(attempted - failed)


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


# ---------------------------------------------------------------------------
# M6-Full（协议 §2/§4–§7）：ReportSnapshot 装配、baseline / 版本化 Gate 服务。
# ---------------------------------------------------------------------------

#: Direct LLM 套件：质量口径的合法分母是 judged_cases（无期望题除外）。
DIRECT_LLM_SUITE = "direct-llm"

#: created_at 缺失时的确定性回退：snapshot/baseline 的内容 hash 必须可重现。
_FALLBACK_TIMESTAMP = "1970-01-01T00:00:00+00:00"

#: 正式报告 / Gate 只消费终态 Run（与既有 evaluate_gate 的集合一致）。
_TERMINAL_RUN_STATUSES = frozenset({
    "completed", "failed", "cancelled", "unsupported", "needs_review",
})


def _suite_identity(manifest: Mapping[str, Any]) -> str | None:
    """manifest 冻结的套件身份：``benchmark_provenance.suite`` 优先。"""
    for key in ("benchmark_provenance", "provenance"):
        provenance = manifest.get(key)
        if isinstance(provenance, dict):
            suite = provenance.get("suite")
            if isinstance(suite, str) and suite:
                return suite
    suite = manifest.get("suite")
    return suite if isinstance(suite, str) and suite else None


def _call_failed_case_ids(store: Any, run_id: str) -> set[str]:
    """case_run 结果里 ``outcome=call_failed`` 的样本（评分行之外的第二证据源）。

    旧装配可能没有 case_runs 仓库：缺失时返回空集，判定退回评分行的
    ``outcome`` 字段，绝不臆造调用失败。
    """
    case_runs = getattr(store, "case_runs", None)
    list_for_run = getattr(case_runs, "list_for_run", None)
    if list_for_run is None:
        return set()
    failed: set[str] = set()
    for record in list_for_run(run_id) or []:
        if isinstance(record, dict) and record.get("outcome") == "call_failed":
            case_id = record.get("case_id")
            if isinstance(case_id, str) and case_id:
                failed.add(case_id)
    return failed


def _classify_case_rows(
    selected_ids: list[str],
    rows: list[dict[str, Any]],
    *,
    failed_calls: set[str],
    needs_review_run: bool,
) -> tuple[dict[str, str], dict[str, bool | None]]:
    """逐 case 互斥 disposition（协议 §2 真值表）→ (dispositions, outcomes)。

    判定顺序即优先级：``call_failed`` / ``no_expectation`` 是评分行携带的
    **明确**终态（v1 评分器给这些行也写了 passed），必须先于 judged 判定，
    不能让它们冒充 judged 的失败；有明确 passed 的入分母行按合取口径判过
    （与 ``_case_row_counts`` 一致：任一分量失败即整题失败）；其余在
    needs_review Run 里保持 needs_review，否则 unknown；缺行 = not_attempted。
    """
    rows_by_case: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        case_id = row.get("case_id")
        if isinstance(case_id, str) and case_id:
            rows_by_case.setdefault(case_id, []).append(row)
    dispositions: dict[str, str] = {}
    outcomes: dict[str, bool | None] = {}
    for case_id in selected_ids:
        case_rows = rows_by_case.get(case_id) or []
        if not case_rows:
            disposition = "not_attempted"
        elif (
            any(row.get("outcome") == "call_failed" for row in case_rows)
            or case_id in failed_calls
        ):
            disposition = "call_failed"
        elif any(row.get("outcome") == "no_expectation" for row in case_rows):
            disposition = "no_expectation"
        else:
            judged_rows = [
                row for row in case_rows
                if row.get("denominator") is not False
                and row.get("passed") is not None
            ]
            if judged_rows:
                disposition = "judged"
                outcomes[case_id] = all(
                    row.get("passed") is True for row in judged_rows
                )
            elif needs_review_run:
                disposition = "needs_review"
            else:
                disposition = "unknown"
        dispositions[case_id] = disposition
        outcomes.setdefault(case_id, None)
    return dispositions, outcomes


def _disposition_counts(
    dispositions: Mapping[str, str], outcomes: Mapping[str, bool | None],
) -> dict[str, int]:
    """dispositions → 9 键 counts（协议 §2 不变量：attempted + not_attempted =
    selected；judged + call_failed + unknown + needs_review + no_expectation =
    attempted）。

    ``no_expectation`` 的 case **已执行**（模型被调用、费用已发生），计入
    attempted、绝不落入 not_attempted；它只是不进质量分母（"judged 外"指
    分母资格，不是未尝试）。逐 case 的真实终分类始终保留在
    ``case_dispositions`` 记录里，缺失/未知/失败永不静默删除。
    """
    counts = {key: 0 for key in DISPOSITION_COUNT_KEYS}
    counts["selected"] = len(dispositions)
    for case_id, disposition in dispositions.items():
        if disposition == "judged":
            counts["judged"] += 1
            if outcomes.get(case_id) is True:
                counts["scored"] += 1
        elif disposition in counts:
            counts[disposition] += 1
    counts["attempted"] = (
        counts["judged"] + counts["call_failed"]
        + counts["unknown"] + counts["needs_review"]
        + counts["no_expectation"]
    )
    counts["not_attempted"] = counts["selected"] - counts["attempted"]
    return counts


def _cost_summary_from(block: Any) -> tuple[CostSummary, float | None]:
    """manifest / pass 冻结的成本块 → (CostSummary, USD 总额)。

    未知成本保持 None（不当 0）：entries 为空且 unknown_usage_count=1，
    已知成本只记 subject/USD 单口径，来源标 manifest。
    """
    known = bool(block.get("known")) if isinstance(block, dict) else False
    total = _number(block.get("total_usd")) if known and isinstance(block, dict) else None
    if total is None or total < 0:
        return CostSummary(entries=(), unknown_usage_count=1, source="manifest"), None
    return (
        CostSummary(
            entries=(CostEntry(scope="subject", currency="USD", amount=total),),
            unknown_usage_count=0,
            source="manifest",
        ),
        total,
    )


def _trial_disposition_records(rows: list[dict[str, Any]]) -> list[CaseDispositionRecord]:
    """Terminal-Bench 的 Trial 行 → 逐 Trial disposition 记录。

    有效 Trial（denominator=True 且有明确 passed）记 judged（detail 写
    passed/failed）；无效 Trial 归 unknown（结果未知保持可见，§2）；计划内
    未观测的 Trial 不在评分行里，由 counts 的 not_attempted 保持可见。
    """
    records: list[CaseDispositionRecord] = []
    for row in rows:
        if row.get("unit") != "trial":
            continue
        identity = row.get("case_id") or row.get("trial_id")
        if not isinstance(identity, str) or not identity:
            continue
        if row.get("denominator") is True and row.get("passed") is not None:
            records.append(CaseDispositionRecord(
                case_id=identity, disposition="judged",
                detail="passed" if row.get("passed") is True else "failed",
            ))
        else:
            records.append(CaseDispositionRecord(
                case_id=identity, disposition="unknown",
            ))
    return records


def _trial_case_outcomes(rows: list[dict[str, Any]]) -> dict[str, bool | None]:
    """Terminal-Bench 的有效 Trial 行 → case 级结论（合取：任一 Trial 失败
    即该 case 判失败；多 Trial 混合结果的精细归类归统计政策，不在快照层做）。"""
    judged: dict[str, list[bool]] = {}
    for row in rows:
        if row.get("unit") != "trial" or row.get("denominator") is not True:
            continue
        identity = row.get("case_id") or row.get("trial_id")
        if isinstance(identity, str) and identity and isinstance(row.get("passed"), bool):
            judged.setdefault(identity, []).append(row["passed"])
    return {
        identity: (all(values) if values else None)
        for identity, values in judged.items()
    }


def _outcome_strings(outcomes: Mapping[str, bool | None]) -> dict[str, str]:
    """bool|None → 回归分类的样本结论；None（结果未知）绝不是成功。"""
    return {
        case_id: (
            "pass" if value is True else "fail" if value is False else "unknown"
        )
        for case_id, value in outcomes.items()
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        # 证据删除防护（A19/G21）：该 pass 的 score sets 内容进入 hash——
        # 证据变化 ⇒ 不同 evidence hash ⇒ 不同 evaluation_input_hash，不会
        # 与既有结论撞 id（append-only 冲突），也不从当前配置补历史事实。
        evidence["score_sets_digest"] = hashlib.sha256(json.dumps(
            self._score_rows(dict(scoring_pass)), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
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
            attempted, correct = _case_row_counts(scores)
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
        # 成本资格从**所选 pass 的冻结汇总**取（成本在评分期聚合，不在 run
        # manifest 顶层）；与 candidate_summary/report 同一事实源。
        return compare_run_reports(
            self.report_ref(baseline_run_id, scoring_pass_id=baseline_pass_id),
            self.report_ref(candidate_run_id, scoring_pass_id=candidate_pass_id),
            baseline_manifest=self._manifest_view(baseline_run_id, baseline_pass_id),
            candidate_manifest=self._manifest_view(candidate_run_id, candidate_pass_id),
            policy=ComparisonPolicy(allowed_factors=tuple(allowed_factors)),
            baseline_cost=self._pass_cost_view(baseline_run_id, baseline_pass_id),
            candidate_cost=self._pass_cost_view(candidate_run_id, candidate_pass_id),
        )

    def _pass_cost_view(
        self, run_id: str, scoring_pass_id: str | None,
    ) -> dict[str, Any] | None:
        """所选 pass 的成本视图：known/total_usd（unknown 保持 None，不当 0）。

        pass 汇总冻结块优先；direct-llm 等在评分期不冻结成本块的套件回退到
        case 结果的逐题成本块（与 report 同一事实源）。
        """
        try:
            record = _resolve_pass(self.store, run_id, scoring_pass_id)
        except ComparisonError:
            return None
        summary = record.get("summary") or {}
        aggregate = summary.get("aggregate") or {}
        cost = aggregate.get("cost") if isinstance(aggregate, dict) else None
        if isinstance(cost, dict) and cost.get("unknown_cost") is False:
            total = _number(cost.get("known_cost_usd"))
            if total is not None:
                return {"known": True, "total_usd": total, "currency": "USD"}
        summary_cost, total_usd = self._case_cost_summary(run_id)
        return {
            "known": summary_cost.known, "total_usd": total_usd, "currency": "USD",
        }

    def _case_cost_summary(self, run_id: str) -> tuple[CostSummary, float | None]:
        """从 case 结果的逐题成本块汇总（与 API report 同口径）。

        每个有结果的 case 都带定价成本才算 known；无任何成本证据时
        unknown（null 不是 0）。
        """
        case_runs = getattr(self.store, "case_runs", None)
        if case_runs is None or not hasattr(case_runs, "list_for_run"):
            return CostSummary(entries=(), unknown_usage_count=1, source="cases"), None
        cases = case_runs.list_for_run(run_id)
        blocks: list[dict[str, Any]] = []
        for case in cases:
            result = case.get("result")
            if isinstance(result, dict) and isinstance(result.get("cost"), dict):
                blocks.append(result["cost"])
        attempted = [
            case for case in cases
            if isinstance(case.get("result"), dict)
        ]
        totals = [
            float(block["total"]) for block in blocks
            if block.get("total") is not None
        ]
        if attempted and len(totals) == len(attempted) and totals:
            total = round(sum(totals), 8)
            versions = sorted({
                str(block["price_table_version"]) for block in blocks
                if block.get("price_table_version")
            })
            return (
                CostSummary(
                    entries=(
                        CostEntry(
                            scope="subject", currency="USD", amount=total,
                            price_table_versions=tuple(versions),
                        ),
                    ),
                    unknown_usage_count=0, source="case_results",
                ),
                total,
            )
        # 无成本证据本身就是"未知"（null 不是 0），不用 0 冒充确认无未知。
        return (
            CostSummary(entries=(), unknown_usage_count=1, source="case_results"),
            None,
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

    # --------------------------------------------- M6：ReportSnapshot（§5）

    def report_snapshot(
        self, run_id: str, *, scoring_pass_id: str | None = None,
    ) -> ReportSnapshot:
        """固定 Run + ScoringPass 的冻结报告视图（只读，协议 §2/§5）。

        - 逐 case disposition 按 §2 真值表互斥归类，counts 全 9 键且不变量
          成立；缺失/未知/失败永不从分母静默删除，空分母不折算成 0；
        - Direct LLM 套件的质量口径分母是 judged_cases（coverage 仍按
          selected），其余 selected_cases；Terminal-Bench 走 Trial 口径；
        - snapshot_id 是内容 canonical hash：同输入两次构造同 id（可重算
          验证），先以占位 id 构造再回填。
        """
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        manifest = run.get("manifest") or {}
        record = _resolve_pass(self.store, run_id, scoring_pass_id)
        ref = self.report_ref(run_id, scoring_pass_id=str(record["id"]))
        created_at = str(
            record.get("created_at") or run.get("updated_at")
            or run.get("created_at") or _FALLBACK_TIMESTAMP
        )
        suite = _suite_identity(manifest)
        rows = self._score_rows(record)
        if _is_terminal_bench_run(manifest):
            view = _terminal_bench_summary(manifest, record, rows)
            selected = _count(view.get("selected"))
            attempted = _count(view.get("attempted"))
            counts = {
                "selected": selected,
                "attempted": attempted,
                "judged": _count(view.get("judged")),
                "scored": _count(view.get("scored")),
                "call_failed": 0,
                "unknown": _count(view.get("unknown")),
                "not_attempted": max(selected - attempted, 0),
                "needs_review": 0,
                "no_expectation": 0,
            }
            coverage = _number(view.get("coverage"))
            denominator = "planned_trials"
            cost, total_usd = _cost_summary_from(view.get("cost"))
            metric_values = {
                "valid_trial_pass_rate": _number(
                    (view.get("metric_values") or {}).get("valid_trial_pass_rate")
                ),
                "valid_trial_coverage": coverage,
                "cost.total_usd": total_usd,
                "cost.per_success_usd": (
                    _number((view.get("cost") or {}).get("per_success_usd"))
                    if total_usd is not None else None
                ),
            }
            records = _trial_disposition_records(rows)
        else:
            dispositions, outcomes = _classify_case_rows(
                list(run.get("case_ids") or []), rows,
                failed_calls=_call_failed_case_ids(self.store, run_id),
                needs_review_run=run.get("status") == "needs_review",
            )
            counts = _disposition_counts(dispositions, outcomes)
            selected = counts["selected"]
            judged = counts["judged"]
            scored = counts["scored"]
            # 平台重算口径的 accuracy 只在**没有未决样本**时给出：unknown /
            # needs_review / not_attempted 让它保持 None，绝不折算成 0；
            # no_expectation 与 call_failed 是评分行携带的明确终态，只稀释
            # 分子、不阻断取值（质量口径的严格版本是 judged_accuracy）。
            unscored = sum(
                1 for disposition in dispositions.values()
                if disposition in ("unknown", "needs_review", "not_attempted")
            )
            # 成本口径与 report 相同：manifest 冻结块优先，缺失时从 case 结果
            # 的逐题成本块汇总（与 _build_report 同一事实源）。
            cost, total_usd = _cost_summary_from(manifest.get("cost"))
            if not cost.known:
                cost, total_usd = self._case_cost_summary(run_id)
            metric_values = {
                "accuracy": (
                    round(scored / selected, 6) if selected and not unscored else None
                ),
                "judged_accuracy": round(scored / judged, 6) if judged else None,
                "cost.total_usd": total_usd,
                "cost.per_success_usd": (
                    round(total_usd / scored, 6)
                    if total_usd is not None and scored > 0 else None
                ),
            }
            coverage = round(judged / selected, 6) if selected else None
            denominator = (
                "judged_cases" if suite == DIRECT_LLM_SUITE else "selected_cases"
            )
            records = [
                CaseDispositionRecord(
                    case_id=case_id,
                    disposition=disposition,
                    detail=(
                        "passed" if outcomes.get(case_id) else "failed"
                        if disposition == "judged" else None
                    ),
                )
                for case_id, disposition in dispositions.items()
            ]
        snapshot = ReportSnapshot(
            snapshot_id="pending",
            ref=ref,
            created_at=created_at,
            suite=suite,
            denominator=denominator,
            counts=counts,
            coverage=coverage,
            metric_values=metric_values,
            metric_registry_version=METRIC_REGISTRY_VERSION,
            cost=cost,
            evidence_pins=(),
            case_dispositions=tuple(records),
        )
        return snapshot.model_copy(
            update={"snapshot_id": snapshot.compute_snapshot_id()}
        )

    def case_outcomes(
        self, run_id: str, scoring_pass_id: str | None = None,
    ) -> dict[str, bool | None]:
        """固定报告的逐 case 结论（judged→passed 合取值，其余 None=未知）。

        ``ReportSnapshot`` 契约不携带逐 case 布尔结论，本伴生方法把它投影给
        critical_case 规则与回归分类使用；None 是"结果未知"，绝不是成功。
        """
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        manifest = run.get("manifest") or {}
        record = _resolve_pass(self.store, run_id, scoring_pass_id)
        rows = self._score_rows(record)
        if _is_terminal_bench_run(manifest):
            return _trial_case_outcomes(rows)
        _dispositions, outcomes = _classify_case_rows(
            list(run.get("case_ids") or []), rows,
            failed_calls=_call_failed_case_ids(self.store, run_id),
            needs_review_run=run.get("status") == "needs_review",
        )
        return outcomes

    # --------------------------------------------- M6：Baseline（§4）

    def _baseline_store(self) -> Any:
        store = getattr(self.store, "baseline_store", None)
        if store is None:
            raise ComparisonError(
                "BASELINE_STORE_MISSING",
                "baseline operations requested but this store has no "
                "baseline store wired",
            )
        return store

    def create_baseline(
        self,
        baseline_id: str,
        entries: list[dict[str, Any]],
        *,
        policy: ComparisonPolicy | dict[str, Any],
        created_by: str,
        reason: str,
        source_note: str | None = None,
        scoring_pass_ids: list[str | None] | None = None,
    ) -> dict[str, Any]:
        """固定 RunReportRef 集合 → 不可变 BaselineSnapshot（协议 §4）。

        entries 是 ``[{"cell_key": str|None, "run_id": str, "scoring_pass_id": str}]``
        （``scoring_pass_ids`` 可按序补齐缺省 pass）；run/pass 不存在 →
        BASELINE_NOT_FOUND（引用不完整即拒绝创建）。资格：全部 run 终态
        completed 才 formal，needs_review/failed 只能 diagnostic。写入同 id
        同内容幂等、异内容冲突（BaselineConflict 透传）。
        """
        store = self._baseline_store()
        if isinstance(policy, ComparisonPolicy):
            comparison = policy
        else:
            try:
                comparison = ComparisonPolicy.model_validate(policy)
            except ValidationError as error:
                raise ComparisonError(
                    "COMPARISON_POLICY_INVALID", str(error)
                ) from error
        pass_ids = list(scoring_pass_ids or [])
        resolved: list[BaselineEntry] = []
        all_completed = True
        metrics: dict[str, float | int | None] = {}
        timestamps: list[str] = []
        for index, entry in enumerate(entries):
            run_id = entry.get("run_id")
            run = (
                self.store.runs.get(run_id)
                if isinstance(run_id, str) and run_id else None
            )
            if run is None:
                raise ComparisonError(
                    "BASELINE_NOT_FOUND",
                    f"baseline entry {index} references unknown run {run_id!r}",
                )
            pass_id = entry.get("scoring_pass_id")
            if not pass_id and index < len(pass_ids):
                pass_id = pass_ids[index]
            try:
                record = _resolve_pass(self.store, run_id, pass_id)
            except ComparisonError as error:
                raise ComparisonError(
                    "BASELINE_NOT_FOUND",
                    f"baseline entry {index} (run {run_id}) has no fixed "
                    f"scoring pass: {error.code}",
                ) from error
            fixed_pass = str(record["id"])
            if run.get("status") != "completed":
                all_completed = False
            summary = self.candidate_summary(run_id, scoring_pass_id=fixed_pass)
            for key, value in (summary.get("metric_values") or {}).items():
                metrics.setdefault(key, value)
            metrics.setdefault("coverage", summary.get("coverage"))
            timestamps.append(str(
                record.get("created_at") or run.get("updated_at") or ""
            ))
            resolved.append(BaselineEntry(
                cell_key=entry.get("cell_key"),
                ref=self.report_ref(run_id, scoring_pass_id=fixed_pass),
            ))
        # created_at 取所固定 pass 的最大创建时间：同参数重放得到同内容，
        # 幂等写回不因时间漂移变成 BASELINE_IMMUTABLE 冲突。
        created_at = max(timestamps) if any(timestamps) else _FALLBACK_TIMESTAMP
        snapshot = BaselineSnapshot(
            baseline_id=baseline_id,
            entries=tuple(resolved),
            comparison_policy_hash=comparison.policy_hash(),
            eligibility="formal" if all_completed else "diagnostic",
            created_by=created_by,
            reason=reason,
            source_note=source_note,
            created_at=created_at,
            metrics=metrics,
        )
        return store.put(snapshot.model_dump())

    def list_baselines(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._baseline_store().list(limit)

    def get_baseline(self, baseline_id: str) -> dict[str, Any] | None:
        return self._baseline_store().get(baseline_id)

    def set_default_baseline(
        self,
        scope: str,
        baseline_id: str,
        *,
        updated_by: str,
        reason: str,
        expected_current: str | None = None,
    ) -> dict[str, Any]:
        """默认 baseline 是指针操作（scope → snapshot，CAS + 审计，§4）。

        policy hash 取该 baseline 冻结的 comparison_policy_hash；首次设置
        expected_current=None，移动指针必须给出当前值；CAS 期望不符
        （BaselineConflict）透传。
        """
        store = self._baseline_store()
        snapshot = store.get(baseline_id)
        if snapshot is None:
            raise ComparisonError(
                "BASELINE_NOT_FOUND",
                f"baseline snapshot {baseline_id!r} not found",
            )
        pointer = DefaultBaselinePointer(
            scope=scope,
            baseline_id=baseline_id,
            updated_by=updated_by,
            reason=reason,
            comparison_policy_hash=str(snapshot["comparison_policy_hash"]),
            updated_at=_now_iso(),
            position=0,
        )
        return store.set_default(
            pointer.model_dump(), expected_current=expected_current,
        )

    def get_default_baseline(self, scope: str) -> dict[str, Any] | None:
        return self._baseline_store().get_default(scope)

    def default_baseline_history(self, scope: str) -> list[dict[str, Any]]:
        return self._baseline_store().default_history(scope)

    # --------------------------------------------- M6：版本化 Gate（§6/§9）

    def _gate_store(self) -> Any:
        store = getattr(self.store, "gate_store", None)
        if store is None:
            raise ComparisonError(
                "GATE_STORE_MISSING",
                "gate policy operations requested but this store has no "
                "gate store wired",
            )
        return store

    def publish_gate_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """版本化 Gate 政策落库（协议 §9：同 id@version 不可变，幂等）。

        契约校验失败 → GATE_POLICY_INVALID；逐规则 metric 必须在注册表中
        （GATE_METRIC_UNKNOWN），operator 与 metric direction 冲突同样在
        发布期拒绝（求值期拒绝会冻结一条永远不可求值的政策）。lifecycle
        保留入参：draft 允许保存，但 ``evaluate_gate_versioned`` 只接受
        published / deprecated（deprecated 显式求值仍允许，§9）。
        """
        gate_store = self._gate_store()
        try:
            model = GatePolicyVersion.model_validate(payload)
        except ValidationError as error:
            raise ComparisonError("GATE_POLICY_INVALID", str(error)) from error
        for rule in model.rules:
            if not rule.metric_id:
                continue
            definition = lookup_metric(rule.metric_id)
            if definition is None:
                raise ComparisonError(
                    "GATE_METRIC_UNKNOWN",
                    f"rule {rule.rule_id!r} references unknown metric "
                    f"{rule.metric_id!r} (registry {METRIC_REGISTRY_VERSION})",
                )
            direction_reason = _rule_direction_reason(rule, definition)
            if direction_reason is not None:
                raise ComparisonError(
                    "GATE_POLICY_INVALID",
                    f"rule {rule.rule_id!r}: {direction_reason}",
                )
        # JSON 形状落库：tuple → list、enum → str，与存储层（SQLite JSON
        # round-trip）的字节形状一致，幂等重放不因形状差异误报不可变冲突。
        return gate_store.put_policy(json.loads(model.model_dump_json()))

    def evaluate_gate_versioned(
        self,
        *,
        policy_id: str,
        policy_version: str,
        run_id: str,
        scoring_pass_id: str | None = None,
        baseline_id: str | None = None,
        evaluated_at: str | None = None,
        allowed_factors: tuple[str, ...] | list[str] = ("model",),
    ) -> dict[str, Any]:
        """版本化政策 × 固定报告 → GateResult（协议 §6–§8，纯求值）。

        只消费固定引用与冻结快照：政策按 id@version 读取（draft 拒绝），
        Run 必须终态；baseline 走 BaselineSnapshot 的固定 entry（多 entry
        选 cell_key=None 或第一个），可比性判断与普通路径同一比较算法。
        重复求值产生等价结论（同 gate_result_id 幂等落盘，不改写原结果）。
        返回存储结果 + ``exit_code``（决策 → CLI 退出码，§7）。
        """
        gate_store = self._gate_store()
        stored_policy = gate_store.get_policy(policy_id, policy_version)
        if stored_policy is None:
            raise ComparisonError(
                "GATE_POLICY_NOT_FOUND",
                f"gate policy {policy_id}@{policy_version} not found",
            )
        try:
            policy = GatePolicyVersion.model_validate(stored_policy)
        except ValidationError as error:
            raise ComparisonError(
                "GATE_POLICY_INVALID",
                f"stored policy {policy_id}@{policy_version} is invalid: {error}",
            ) from error
        if policy.lifecycle == "draft":
            raise ComparisonError(
                "GATE_POLICY_DRAFT",
                f"gate policy {policy_id}@{policy_version} is a draft; only "
                "published (or deprecated) policies are evaluated",
            )
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.get("status") not in _TERMINAL_RUN_STATUSES:
            raise ComparisonError(
                "RUN_NOT_TERMINAL",
                f"run {run_id} is {run.get('status')!r}; gates evaluate fixed, "
                "terminal reports only",
            )
        candidate_ref = self.report_ref(run_id, scoring_pass_id=scoring_pass_id)
        candidate_snapshot = self.report_snapshot(
            run_id, scoring_pass_id=scoring_pass_id,
        )
        # ReportSnapshot 契约不携带逐 case 布尔结论；作为 Mapping 视图传给
        # 求值器时补上 case_results（critical_case 规则的取值来源）。
        candidate_view = {
            **candidate_snapshot.model_dump(),
            "case_results": self.case_outcomes(run_id, scoring_pass_id),
        }
        baseline_ref: RunReportRef | None = None
        baseline_view: dict[str, Any] | None = None
        comparison_level: Any = None
        metric_eligibility: dict[str, bool] | None = None
        comparison_reasons: tuple[str, ...] = ()
        if baseline_id is not None:
            baseline_store = self._baseline_store()
            stored_baseline = baseline_store.get(baseline_id)
            if stored_baseline is None:
                raise ComparisonError(
                    "BASELINE_NOT_FOUND",
                    f"baseline snapshot {baseline_id!r} not found",
                )
            entries = stored_baseline.get("entries") or []
            chosen = next(
                (item for item in entries if item.get("cell_key") is None),
                entries[0] if entries else None,
            )
            if not isinstance(chosen, dict):
                raise ComparisonError(
                    "BASELINE_INCOMPLETE",
                    f"baseline {baseline_id!r} has no entries to compare",
                )
            try:
                entry = BaselineEntry.model_validate(chosen)
            except ValidationError as error:
                raise ComparisonError(
                    "BASELINE_INCOMPLETE",
                    f"baseline {baseline_id!r} entry is invalid: {error}",
                ) from error
            baseline_ref = entry.ref
            baseline_snapshot = self.report_snapshot(
                entry.ref.run_id, scoring_pass_id=entry.ref.scoring_pass_id,
            )
            baseline_view = baseline_snapshot.model_dump()
            result = self.compare(
                entry.ref.run_id, run_id,
                allowed_factors=tuple(allowed_factors),
                baseline_pass_id=entry.ref.scoring_pass_id,
                candidate_pass_id=scoring_pass_id,
            )
            comparison_level = result.level
            comparison_reasons = tuple(result.reasons)
            metric_eligibility = dict(result.metric_eligibility)
        manifest = run.get("manifest") or {}
        gate_result = evaluate_gate_policy(
            policy,
            candidate_ref=candidate_ref,
            candidate_snapshot=candidate_view,
            candidate_run_status=str(run.get("status") or ""),
            baseline_ref=baseline_ref,
            baseline_snapshot=baseline_view,
            comparison_level=comparison_level,
            comparison_reasons=comparison_reasons,
            metric_eligibility=metric_eligibility,
            candidate_evidence={
                # 实际模型身份按套件冻结口径投影（请求值不替代回报值）；
                # 副作用/安全标记在固定报告里不可观测 → None（证据不足，
                # fail-closed），绝不当成"无违规"。
                "model_identity": _model_identity(dict(manifest)),
                "side_effect_violations": None,
                "safety_markers": None,
            },
            evaluated_at=evaluated_at,
        )
        stored_result = gate_store.put_result(json.loads(gate_result.model_dump_json()))
        return {**stored_result, "exit_code": gate_exit_code(gate_result.decision)}

    # --------------------------------------------- M6：回归分类（T07/A10）

    def classify_regression(
        self,
        baseline_run_id: str,
        candidate_run_id: str,
        *,
        baseline_pass_id: str | None = None,
        candidate_pass_id: str | None = None,
    ) -> dict[str, Any]:
        """两侧固定报告的逐 case 回归分类（A10/G14：added/removed 独立列出，
        绝不冒充 fixed/new_failure；None 结论按 unknown 处理）。"""
        baseline_outcomes = _outcome_strings(
            self.case_outcomes(baseline_run_id, baseline_pass_id)
        )
        candidate_outcomes = _outcome_strings(
            self.case_outcomes(candidate_run_id, candidate_pass_id)
        )
        report = regression_report(baseline_outcomes, candidate_outcomes)
        report["baseline_ref"] = self.report_ref(
            baseline_run_id, scoring_pass_id=baseline_pass_id,
        ).model_dump()
        report["candidate_ref"] = self.report_ref(
            candidate_run_id, scoring_pass_id=candidate_pass_id,
        ).model_dump()
        return report
