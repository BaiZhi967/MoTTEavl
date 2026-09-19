import type { DirectLlmPreset } from "../../api/client";

/** 数据集默认评分器选项（与 packages/contracts/motte_contracts/direct_llm.py 的 SCORERS 对齐）。 */
export const SCORER_OPTIONS = [
  { value: "exact", label: "exact（逐字精确匹配）" },
  { value: "contains", label: "contains（输出包含期望串）" },
  { value: "regex", label: "regex（输出匹配正则）" },
] as const;

export const SCORER_VALUES = SCORER_OPTIONS.map((option) => option.value);

export type ScorerReference = string | { id: string; version?: string | null };

function scorerIdentity(scorer: unknown): string | null {
  if (typeof scorer === "string") return scorer.trim() || null;
  if (!scorer || typeof scorer !== "object") return null;
  const candidate = scorer as { id?: unknown; version?: unknown };
  const id = typeof candidate.id === "string" ? candidate.id.trim() : "";
  if (!id) return null;
  const version = typeof candidate.version === "string" ? candidate.version.trim() : "";
  return version ? `${id}@${version}` : id;
}

export function scorerLabel(scorer: unknown): string {
  if (typeof scorer === "string") {
    return SCORER_OPTIONS.find((option) => option.value === scorer)?.label
      ?? (scorer.trim() || "未知评分器");
  }
  return scorerIdentity(scorer) ?? "未知评分器";
}

/** 表格列用的短名：v1 保留字符串，v2 scorer spec 展示不可歧义的 id@version。 */
export function scorerShort(scorer: unknown): string {
  return scorerIdentity(scorer) ?? "—";
}

/** 显示与默认选中顺序：按数据集名，题数多的在前；同题数按数据集名。 */
export function sortPresets(items: DirectLlmPreset[]): DirectLlmPreset[] {
  return [...items].sort((a, b) => b.cases - a.cases
    || a.dataset.localeCompare(b.dataset) || a.scenario.localeCompare(b.scenario));
}

/** 数据集级评分器（快照/概览里都取自 eval.scorer），兼容 v1 字符串与 v2 spec。 */
export function datasetScorer(preset: DirectLlmPreset | undefined | null): ScorerReference | null {
  const scorer = preset?.eval?.scorer;
  if (!scorerIdentity(scorer)) return null;
  return scorer as ScorerReference;
}
