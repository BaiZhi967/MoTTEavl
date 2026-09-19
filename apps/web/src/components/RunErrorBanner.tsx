import type { RunRecord } from "../api/client";

const MAX_IDS = 3;

function idList(ids: string[]): string {
  const shown = ids.slice(0, MAX_IDS).join(" · ");
  return ids.length > MAX_IDS ? `${shown} 等 ${ids.length} 题` : shown;
}

/**
 * run 级错误横幅：先说清失败范围（几题中的哪几题），再给原始错误与出路，
 * 避免裸 provider 消息让人以为整页结果都不可用。
 * monitor 场景（批次行）自带「结果 / 重试」操作，只展示范围行，不重复指引。
 */
export function RunErrorBanner({ run, monitor = false }: { run: RunRecord; monitor?: boolean }) {
  const error = run.error;
  if (!error) return null;
  const tag = String(error.code ?? error.class ?? error.type ?? "") || null;
  const message = typeof error.message === "string" ? error.message : "";
  const total = run.case_ids?.length ?? run.cases?.length ?? 0;
  const failedIds = (run.cases ?? [])
    .filter((row) => row.result?.error)
    .map((row) => row.case_id);
  const partial = failedIds.length > 0 && total > failedIds.length;
  const guidance = monitor || failedIds.length === 0 ? null : partial
    ? `其余 ${total - failedIds.length} 题结果不受影响，可在下方查看；如需完整结果，请在「运行」总览重试本次运行。`
    : "可在「运行」总览重试本次运行。";
  return (
    <p className="error">
      {tag && <><span className="mono">{tag}</span>{" "}</>}
      {failedIds.length > 0
        ? `${total > 0 ? `${total} 题中 ` : ""}${failedIds.length} 题调用失败（${idList(failedIds)}）：${message}`
        : message}
      {guidance && <><br />{guidance}</>}
    </p>
  );
}
