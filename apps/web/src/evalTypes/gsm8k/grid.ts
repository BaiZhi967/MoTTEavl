import type { RunRecord } from "../../api/client";

export interface GridCell {
  caseId: string;
  state: "pass" | "fail" | "pending";
}

/** 由 scores 推导逐题格子状态：有 outcome 按 passed，无分数未跑。 */
export function gridCells(run: RunRecord): GridCell[] {
  const scoreByCase = new Map((run.scores ?? []).map((score) => [score.case_id, score]));
  return (run.case_ids ?? []).map((caseId) => {
    const score = scoreByCase.get(caseId);
    if (!score) return { caseId, state: "pending" };
    return { caseId, state: score.passed ? "pass" : "fail" };
  });
}
