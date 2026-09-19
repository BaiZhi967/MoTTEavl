"""比较/门禁装配服务（M6-T03 Lite + M2-T09）。

以固定 Run（+当前 ScoringPass）为输入做比较与 Gate 求值；全程只读，
0 次 Runner/Judge/模型调用。C-Eval 直接消费本服务——不存在 C-Eval 专用
Gate 引擎。
"""
from __future__ import annotations

from typing import Any

from motte_contracts.comparison import ComparisonPolicy, RunReportRef
from motte_eval.comparison import ComparisonResult, compare_run_reports
from motte_eval.coverage import coverage_summary
from motte_eval.gates import evaluate_gate


def _current_pass_id(store: Any, run_id: str) -> str:
    passes = getattr(store, "scoring_passes", None)
    if passes is not None:
        current = passes.current(run_id)
        if current is not None:
            return str(current.get("id"))
    return f"cases:{run_id}"


class ComparisonService:
    """读取固定 Run 事实 → 比较/覆盖/Gate（同一 M6-Lite 服务）。"""

    def __init__(self, store: Any, baselines: Any = None) -> None:
        self.store = store
        self.baselines = baselines

    # ------------------------------------------------------------------ 视图

    def report_ref(self, run_id: str) -> RunReportRef:
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        import hashlib
        import json

        manifest = run.get("manifest") or {}
        digest = hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return RunReportRef(
            run_id=run_id,
            scoring_pass_id=_current_pass_id(self.store, run_id),
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

    def candidate_summary(self, run_id: str) -> dict[str, Any]:
        """从固定 Run 事实计算覆盖与指标（只读，不触发任何执行）。"""
        run = self.store.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        rows = self.store.case_runs.list_for_run(run_id)
        selected_ids = list(run.get("case_ids") or [])
        judged_rows = [row for row in rows if row.get("outcome") == "responded"]
        correct = 0
        unscored = False
        for row in judged_rows:
            payload = row.get("result") if isinstance(row.get("result"), dict) else {}
            prediction = str(payload.get("prediction") or "").strip()
            gold = payload.get("gold")
            if gold is None or str(gold).strip() == "":
                unscored = True
                continue
            if prediction and prediction.upper() == str(gold).strip().upper():
                correct += 1
        judged = len(judged_rows)
        metric_value = (
            round(correct / len(selected_ids), 6)
            if selected_ids and not unscored else None
        )
        cost_known = bool((run.get("manifest") or {}).get("cost", {}).get("known"))
        return coverage_summary(
            denominator="selected_cases",
            selected=len(selected_ids),
            judged=judged,
            scored=correct,
            metric_value=metric_value,
            cost={"known": cost_known},
        )

    # ------------------------------------------------------------------ 比较

    def compare(
        self, baseline_run_id: str, candidate_run_id: str, *,
        allowed_factors: list[str] | tuple[str, ...],
    ) -> ComparisonResult:
        return compare_run_reports(
            self.report_ref(baseline_run_id),
            self.report_ref(candidate_run_id),
            baseline_manifest=self._manifest_view(baseline_run_id),
            candidate_manifest=self._manifest_view(candidate_run_id),
            policy=ComparisonPolicy(allowed_factors=tuple(allowed_factors)),
        )

    # ------------------------------------------------------------------ 门禁

    def evaluate_gate(
        self, run_id: str, *, policy: dict[str, Any], baseline_run_id: str | None = None,
    ) -> dict[str, Any]:
        comparison_view: dict[str, Any] | None = None
        if baseline_run_id is not None and policy.get("require_comparable"):
            result = self.compare(
                baseline_run_id, run_id,
                allowed_factors=policy.get("allowed_factors") or ["model"],
            )
            comparison_view = {
                "eligible": result.eligible,
                "reasons": list(result.reasons),
            }
        candidate = self.candidate_summary(run_id)
        return evaluate_gate(policy, candidate, comparison=comparison_view)
