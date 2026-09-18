import type { BenchmarkPreset } from "../../api/client";

export const DATASET_NAME = "gsm8k-test";

export interface ScopeOption {
  value: "full" | "smoke";
  label: string;
}

/** 下载范围：全量=整个 test split，冒烟=前 20 题（默认全量，冒烟在高级设置里选）。 */
export const SCOPE_OPTIONS: ScopeOption[] = [
  { value: "full", label: "全量（整个 test split）" },
  { value: "smoke", label: "冒烟（前 20 题）" },
];

export function scopeLabel(scope: string | null | undefined): string {
  return scope === "full" ? "全量" : scope === "smoke" ? "冒烟" : "未知范围";
}

/** 显示与默认选中顺序：题数最多的数据集在前（全量优先），同题数时冒烟在前，其余按名称。 */
export function sortPresets(items: BenchmarkPreset[]): BenchmarkPreset[] {
  return [...items].sort((a, b) => b.cases - a.cases
    || (a.scope === "smoke" ? 0 : 1) - (b.scope === "smoke" ? 0 : 1)
    || a.scenario.localeCompare(b.scenario) || a.dataset.localeCompare(b.dataset));
}
