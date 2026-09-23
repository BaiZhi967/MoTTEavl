import {
  compareRunReports, getReportSnapshot, getRun, modelLabel,
  type ComparabilityView, type ReportSnapshotView, type RunRecord,
} from "../api/client";

export interface SuiteColumn {
  run: RunRecord;
  snapshot: ReportSnapshotView;
  model: string;
}

export interface SuiteComparison {
  columns: SuiteColumn[];
  comparisons: ComparabilityView[];
  comparable: boolean;
  reasons: string[];
  sharedFailed: string[];
}

/** Every quality value and case outcome comes from the selected, fixed ScoringPass. */
export async function loadSuiteComparison(runIds: string[], passIds: (string | undefined)[]): Promise<SuiteComparison> {
  const columns = await Promise.all(runIds.map(async (id, index) => {
    const [run, snapshot] = await Promise.all([getRun(id), getReportSnapshot(id, passIds[index])]);
    return { run, snapshot, model: modelLabel(run) ?? id };
  }));
  const comparisons = await Promise.all(columns.slice(1).map((column) => compareRunReports({
    baseline: columns[0].run.id,
    candidate: column.run.id,
    baseline_pass: columns[0].snapshot.ref.scoring_pass_id,
    candidate_pass: column.snapshot.ref.scoring_pass_id,
    factors: ["model"],
  })));
  const comparable = comparisons.every((result) => result.metric_eligibility?.quality === true);
  const reasons = comparisons.flatMap((result) => result.structural_reasons ?? result.reasons ?? []);
  const failures = columns.map((column) => new Set(
    (column.snapshot.case_dispositions ?? [])
      .filter((record) => record.disposition === "judged" && record.detail === "failed")
      .map((record) => record.case_id),
  ));
  const sharedFailed = comparable && failures.length
    ? [...failures[0]].filter((id) => failures.every((set) => set.has(id))) : [];
  return { columns, comparisons, comparable, reasons, sharedFailed };
}

export function snapshotRate(snapshot: ReportSnapshotView, key: string): string {
  const value = snapshot.metric_values?.[key];
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value * 100)}%` : "未知";
}

export function usageTotal(run: RunRecord): string {
  const cases = run.cases ?? [];
  if (!cases.length || cases.some((row) => typeof row.result?.usage?.total_tokens !== "number")) return "未知";
  return String(cases.reduce((sum, row) => sum + (row.result?.usage?.total_tokens ?? 0), 0));
}

export function snapshotCost(snapshot: ReportSnapshotView): string {
  const cost = snapshot.cost;
  if (!cost?.entries?.length) return "未知";
  const totals = new Map<string, number>();
  for (const entry of cost.entries) {
    if (!entry.currency || typeof entry.amount !== "number") return "未知";
    totals.set(entry.currency, (totals.get(entry.currency) ?? 0) + entry.amount);
  }
  const values = [...totals].sort(([a], [b]) => a.localeCompare(b)).map(([currency, amount]) =>
    `${currency} ${Number(amount.toFixed(6))}`);
  return `${values.join(" · ")}${cost.unknown_usage_count ? ` · ${cost.unknown_usage_count} 笔未知` : ""}`;
}
