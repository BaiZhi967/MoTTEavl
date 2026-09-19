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

from typing import Any

from motte_contracts.comparison import ComparisonPolicy, RunReportRef
from motte_eval.comparison import ComparisonResult, compare_run_reports
from motte_eval.coverage import coverage_summary
from motte_eval.gates import evaluate_gate


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
        digest = hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        # 显式 pass 与 current 同一校验：归属与存在性都由 _resolve_pass 把关
        # （review R2-08：不存在的 pass 不能参与比较）。
        pass_id = str(_resolve_pass(self.store, run_id, scoring_pass_id)["id"])
        return RunReportRef(
            run_id=run_id,
            scoring_pass_id=pass_id,
            report_schema="report-v1",
            evidence_hash="sha256:" + digest,
        )

    def _manifest_view(self, run_id: str) -> dict[str, Any]:
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        manifest = run.get("manifest") or {}
        view = dict(manifest)
        view["case_ids"] = list(run.get("case_ids") or [])
        return view

    def candidate_summary(
        self, run_id: str, *, scoring_pass_id: str | None = None,
    ) -> dict[str, Any]:
        """从固定 ScoringPass 的不可变事实计算覆盖与指标（只读）。

        优先使用 pass summary 冻结的 aggregate（创建 pass 时已算好）；
        缺 aggregate 时按该 pass 的 ScoreSet 重算同样的口径。Run 的
        case_ids 只作分母，不再从 case_runs 现场拼分子（review R12）。
        """
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        record = _resolve_pass(self.store, run_id, scoring_pass_id)
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
            baseline_manifest=self._manifest_view(baseline_run_id),
            candidate_manifest=self._manifest_view(candidate_run_id),
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
