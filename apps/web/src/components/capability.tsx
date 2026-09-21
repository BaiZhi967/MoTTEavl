import type { ReactNode } from "react";
import { ApiRequestError, describeApiError } from "../api/client";

/** 「未知」的唯一文案：缺字段 / 未报道不得填 0 或空字符串。 */
export const UNKNOWN_TEXT = "未知";

/**
 * 接口未注册或能力被关闭时的判定。
 * 404 / 405 / 501 一律视为「能力不可用」：入口保留、明确禁用并给出原因，
 * 而不是让页面崩溃或静默隐藏。其余错误按真实错误显示。
 */
export function unavailableReason(error: unknown): string | null {
  if (error instanceof ApiRequestError) {
    if (error.status === 404) return "服务端未注册该端点（HTTP 404）";
    if (error.status === 405) return "服务端不允许该调用（HTTP 405）";
    if (error.status === 501) return "服务端声明该能力尚未实现（HTTP 501）";
    return null;
  }
  return null;
}

/** 能力不可用提示：固定 3px 左语气条 + --bg-subtle 底（DESIGN.md 第 4 节）。 */
export function CapabilityNotice({
  children,
  tone = "warning",
  testId,
}: {
  children: ReactNode;
  /** warning = 能力不可用；info = 只读 / 零费用说明。 */
  tone?: "warning" | "info";
  testId?: string;
}) {
  return (
    <p className={tone === "info" ? "notice notice-info" : "notice"} role="status" data-testid={testId}>
      {children}
    </p>
  );
}

/**
 * 读取失败渲染：404/405/501 → 能力不可用（保留入口 + 原因）；
 * 其它错误 → 内联 alert（不用 window.alert）。
 */
export function LoadFailure({ error, what, testId }: { error: unknown; what: string; testId?: string }) {
  const reason = unavailableReason(error);
  if (reason) {
    return (
      <CapabilityNotice testId={testId}>
        {what}能力不可用：{reason}。入口保留但不可操作，不伪造结果。
      </CapabilityNotice>
    );
  }
  return (
    <p className="error" role="alert" data-testid={testId}>
      {what}读取失败：{describeApiError(error).message}
    </p>
  );
}

/**
 * 动作失败文案：404/405/501 一律说「能力不可用」，其余按服务端结构化错误显示。
 * 页面只在表单旁显示它，绝不因此清空用户已填内容。
 */
export function failureText(error: unknown, what: string): string {
  const reason = unavailableReason(error);
  if (reason) return what + "能力不可用：" + reason;
  return what + "失败：" + describeApiError(error).message;
}

/** 未知值：带原因说明的「未知」，用于表格与键值行。 */
export function UnknownValue({ reason }: { reason?: string | null }) {
  return (
    <span className="hint" title={reason ?? undefined}>
      {UNKNOWN_TEXT}
    </span>
  );
}

/** 费用视图：报道 / 估算 / 未知三态分开，缺证据一律「未知」。 */
export interface CostLike {
  reported_usd?: number | null;
  estimated_usd?: number | null;
  currency?: string | null;
  known_calls?: number | null;
  unknown_calls?: number | null;
  unknown_cost?: boolean | null;
  price_table_version?: string | null;
}

/** 货币格式化：去掉尾随 0，小额不四舍五入成 0。 */
export function formatMoney(value: number | null | undefined, currency?: string | null): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const trimmed = value.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  const symbol = !currency || currency === "USD" ? "$" : `${currency} `;
  return `${symbol}${trimmed === "" ? "0" : trimmed}`;
}

/** 费用完整性判定：没有任何已报道或估算成本，或服务端标记未知 → 未知。 */
export function costIsUnknown(cost: CostLike | null | undefined): boolean {
  if (!cost) return true;
  if (cost.unknown_cost === true) return true;
  return cost.reported_usd == null && cost.estimated_usd == null;
}

export function CostView({ cost, testId }: { cost: CostLike | null | undefined; testId?: string }) {
  const currency = cost?.currency ?? "USD";
  const reported = formatMoney(cost?.reported_usd, currency);
  const estimated = formatMoney(cost?.estimated_usd, currency);
  const unknown = costIsUnknown(cost);
  return (
    <dl className="kv" data-testid={testId}>
      <dt>报道成本</dt>
      <dd className="mono">{reported ?? UNKNOWN_TEXT}</dd>
      <dt>估算成本</dt>
      <dd className="mono">{estimated ?? UNKNOWN_TEXT}</dd>
      <dt>成本完整性</dt>
      <dd>
        {unknown
          ? `未知（${cost?.unknown_calls != null ? `${cost.unknown_calls} 次调用未报道成本` : "服务端没有给出成本证据"}）`
          : "已知"}
      </dd>
      {cost?.price_table_version && (
        <>
          <dt>价格表</dt>
          <dd className="mono">{cost.price_table_version}</dd>
        </>
      )}
    </dl>
  );
}
