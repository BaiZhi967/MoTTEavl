import type { DirectLlmPreset } from "../../api/client";

/** 数据集默认评分器选项（与 packages/contracts/motte_contracts/direct_llm.py 的 SCORERS 对齐）。 */
export const SCORER_OPTIONS = [
  { value: "exact", label: "exact（逐字精确匹配）" },
  { value: "contains", label: "contains（输出包含期望串）" },
  { value: "regex", label: "regex（输出匹配正则）" },
] as const;

export const SCORER_VALUES = SCORER_OPTIONS.map((option) => option.value);

export function scorerLabel(scorer: string | null | undefined): string {
  return SCORER_OPTIONS.find((option) => option.value === scorer)?.label
    ?? (scorer ? scorer : "未知评分器");
}

/** 表格列用的短名：保留契约里的原始取值，不做中文润色。 */
export function scorerShort(scorer: string | null | undefined): string {
  return scorer || "—";
}

/** 显示与默认选中顺序：按数据集名，题数多的在前；同题数按数据集名。 */
export function sortPresets(items: DirectLlmPreset[]): DirectLlmPreset[] {
  return [...items].sort((a, b) => b.cases - a.cases
    || a.dataset.localeCompare(b.dataset) || a.scenario.localeCompare(b.scenario));
}

/** 数据集级评分器（快照/概览里都取自 eval.scorer）。 */
export function datasetScorer(preset: DirectLlmPreset | undefined | null): string | null {
  const scorer = preset?.eval?.scorer;
  return typeof scorer === "string" ? scorer : null;
}
