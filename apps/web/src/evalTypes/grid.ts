import type { RunRecord } from "../api/client";

export interface GridCell {
  caseId: string;
  state: "pass" | "fail" | "pending";
}

/**
 * 由 scores 推导逐题格子状态：灰=未跑（无分数、not_attempted，或本题没有期望答案因此不判定），
 * 仅实际判错的 outcome 标红。GSM8K 与 Direct LLM 共用同一套语义。
 */
export function gridCells(run: RunRecord): GridCell[] {
  const scoreByCase = new Map((run.scores ?? []).map((score) => [score.case_id, score]));
  return (run.case_ids ?? []).map((caseId) => {
    const score = scoreByCase.get(caseId);
    if (!score || score.outcome === "not_attempted" || score.outcome === "no_expectation") {
      return { caseId, state: "pending" };
    }
    return { caseId, state: score.passed ? "pass" : "fail" };
  });
}
