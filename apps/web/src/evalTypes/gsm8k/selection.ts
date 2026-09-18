import type { CaseSelectionRequest } from "../../api/client";

/** 题目页选中的集合：跨页保留，用 sessionStorage 传给操作页（避免把上千个 id 塞进 URL）。 */
const STORAGE_KEY = "motte.gsm8k.case-selection";
/** 一次跑测最多勾选的题目数由数据集大小决定；这里只挡住明显失控的粘贴输入。 */
export const MAX_CASE_IDS = 20000;

export interface StoredSelection {
  dataset: string;
  caseIds: string[];
}

export function loadCaseSelection(): StoredSelection | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredSelection;
    if (typeof parsed?.dataset !== "string" || !Array.isArray(parsed.caseIds)) return null;
    const caseIds = parsed.caseIds.filter((item): item is string => typeof item === "string");
    return caseIds.length > 0 ? { dataset: parsed.dataset, caseIds } : null;
  } catch {
    return null;
  }
}

export function saveCaseSelection(selection: StoredSelection): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(selection));
  } catch {
    // 隐私模式等无法写入存储时，操作页仍可手动选择「全部 / 随机」
  }
}

export function clearCaseSelection(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // 同上：清理失败不影响页面
  }
}

/** 8 字节十六进制种子，与服务端 select_cases 的 seed 格式一致（8-64 位 hex）。 */
export function randomSeed(): string {
  try {
    const bytes = new Uint8Array(8);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  } catch {
    return Math.floor(Math.random() * 2 ** 48).toString(16).padStart(12, "0");
  }
}

/** 把勾选结果拼成运行级选择请求；空选择返回 null（调用方据此禁用发起）。 */
export function selectionRequest(
  mode: "all" | "ids" | "random",
  options: { caseIds?: string[]; count?: number; seed?: string } = {},
): CaseSelectionRequest | null {
  if (mode === "ids") {
    const caseIds = options.caseIds ?? [];
    return caseIds.length > 0 ? { mode: "ids", case_ids: caseIds } : null;
  }
  if (mode === "random") {
    const count = options.count ?? 0;
    return count > 0 ? { mode: "random", count, seed: options.seed || undefined } : null;
  }
  return { mode: "all" };
}

/** 运行快照里的选择描述，用于结果页/对比页展示证据。 */
export function runSelectionLabel(provenance: Record<string, any> | undefined): string | null {
  const selection = provenance?.run_selection;
  if (!selection || typeof selection !== "object") {
    const count = provenance?.selected_count;
    return typeof count === "number" ? `全部 ${count} 题` : null;
  }
  const count = typeof selection.count === "number" ? selection.count : "?";
  if (selection.mode === "random") {
    const seed = typeof selection.seed === "string" ? selection.seed.slice(0, 8) : "";
    return `随机 ${count} 题（seed ${seed}…，可复现）`;
  }
  if (selection.mode === "ids") return `指定 ${count} 题`;
  return `全部 ${count} 题`;
}
