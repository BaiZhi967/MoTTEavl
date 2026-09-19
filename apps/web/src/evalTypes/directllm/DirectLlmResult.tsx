import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { getReport, getRun, modelLabel, rescoreRun, type RunRecord, type RunReport } from "../../api/client";
import { MetricCards, type MetricItem } from "../../components/MetricCards";
import { CaseDrillTable, type DrillRow } from "../../components/CaseDrillTable";
import { StatusBadge } from "../../components/StatusBadge";
import { RunAuditSummary } from "../../components/RunAuditSummary";
import { runSelectionLabel } from "../selection";
import { scorerShort } from "./presets";

/* 词汇表同时兼容 Direct LLM v1 与 v2 scorer outcome。 */
const OUTCOME_LABELS: Record<string, { label: string; tone: "success" | "error" | "neutral" }> = {
  correct: { label: "通过", tone: "success" },
  wrong_answer: { label: "不通过", tone: "error" },
  invalid_format: { label: "格式无效", tone: "error" },
  no_expectation: { label: "无判定", tone: "neutral" },
  call_failed: { label: "调用失败", tone: "error" },
  not_attempted: { label: "未尝试", tone: "neutral" },
};

const LEGACY_JUDGED_OUTCOMES = new Set(["correct", "wrong_answer", "invalid_format"]);

/** 新分数信任 judged 事实；旧分数缺字段时只从确定性判定 outcome 回落。 */
export function isDirectLlmJudged(score: { judged?: boolean | null; outcome?: string | null }): boolean {
  if (score.judged === true) return true;
  if (score.judged === false) return false;
  return typeof score.outcome === "string" && LEGACY_JUDGED_OUTCOMES.has(score.outcome);
}

interface ReportRates {
  coverage: number;
  completion: number | null;
  attemptRate: number | null;
}

function reportRate(report: RunReport, key: "coverage" | "completion" | "attempt_rate"): number | null {
  const summary = report.summary as RunReport["summary"] & Record<string, unknown>;
  const aggregate = summary.aggregate;
  const value = (aggregate && typeof aggregate === "object" ? aggregate[key] : undefined) ?? summary[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function v2ReportRates(report: RunReport): ReportRates | null {
  if ((report.schema_version ?? 1) < 2) return null;
  const coverage = reportRate(report, "coverage");
  if (coverage == null) return null;
  return {
    coverage,
    completion: reportRate(report, "completion"),
    attemptRate: reportRate(report, "attempt_rate"),
  };
}

function outputText(result: any): string {
  if (result == null) return "（无结果）";
  if (typeof result.content === "string" && result.content) return result.content.slice(0, 120);
  if (result.error?.message) return `错误：${result.error.message}`;
  return "（空输出）";
}

export interface DirectLlmSnapshotCase {
  case_id: string;
  input?: unknown;
  prompt?: unknown;
  expected?: unknown;
  metadata?: Record<string, unknown>;
  scorer?: unknown;
}

export interface DirectLlmSnapshotView {
  schemaVersion: number;
  cases: DirectLlmSnapshotCase[];
  defaultScorer?: unknown;
}

function snapshotCase(value: unknown): value is DirectLlmSnapshotCase {
  return Boolean(value && typeof value === "object"
    && typeof (value as { case_id?: unknown }).case_id === "string");
}

/** v2 快照只信任 selected_cases；v1 才保留 dataset.cases 的兼容读取。 */
export function directLlmSnapshotView(run: RunRecord | undefined): DirectLlmSnapshotView {
  const snapshot = run?.manifest?.benchmark_snapshot;
  const schemaVersion = typeof snapshot?.schema_version === "number" ? snapshot.schema_version : 1;
  if (schemaVersion >= 2) {
    return {
      schemaVersion,
      cases: Array.isArray(snapshot?.selected_cases) ? snapshot.selected_cases.filter(snapshotCase) : [],
      defaultScorer: snapshot?.dataset?.eval?.scorer,
    };
  }
  return {
    schemaVersion,
    cases: Array.isArray(snapshot?.dataset?.cases) ? snapshot.dataset.cases.filter(snapshotCase) : [],
    defaultScorer: snapshot?.dataset?.eval?.scorer,
  };
}

export function directLlmCasePrompt(item: DirectLlmSnapshotCase | undefined): string {
  if (typeof item?.input === "string") return item.input;
  if (typeof item?.prompt === "string") return item.prompt;
  return "（题面缺失）";
}

export function directLlmCaseExpected(item: DirectLlmSnapshotCase | undefined): string {
  if (item?.expected == null) return "（无判定）";
  return typeof item.expected === "string" ? item.expected : JSON.stringify(item.expected);
}

export function directLlmCaseMetadata(item: DirectLlmSnapshotCase | undefined): string | null {
  if (!item?.metadata) return null;
  const entries = Object.entries(item.metadata).filter(([key, value]) => key !== "scorer" && value != null);
  if (entries.length === 0) return null;
  return entries.map(([key, value]) => {
    if (Array.isArray(value)) return `${key}=${value.join(",")}`;
    if (typeof value === "object") return `${key}=${JSON.stringify(value)}`;
    return `${key}=${String(value)}`;
  }).join(" · ");
}

export function directLlmCaseScorer(
  snapshot: DirectLlmSnapshotView,
  item: DirectLlmSnapshotCase | undefined,
  score: { scorer?: string | null; scorer_version?: string | null } | undefined,
  provenanceScorer: unknown,
): unknown {
  const scoreScorer = score?.scorer
    ? { id: score.scorer, version: score.scorer_version ?? null }
    : undefined;
  const snapshotScorer = item?.scorer ?? item?.metadata?.scorer;
  return snapshot.schemaVersion >= 2
    ? snapshotScorer ?? snapshot.defaultScorer ?? scoreScorer ?? provenanceScorer
    : scoreScorer ?? snapshotScorer ?? snapshot.defaultScorer ?? provenanceScorer;
}

export function DirectLlmResult() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [cost, setCost] = useState<number | null>(null);
  const [priceTable, setPriceTable] = useState<string | null>(null);
  const [reportRates, setReportRates] = useState<ReportRates | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getRun(runId).then(setRun).catch((e) => setError(String(e)));
    getReport(runId)
      .then((report) => {
        setCost(report.cost?.total ?? null);
        setPriceTable(report.cost?.price_table_versions?.[0] ?? null);
        setReportRates(v2ReportRates(report));
      })
      .catch(() => undefined);
  }, [runId]);

  if (error) return <div className="page"><section className="panel detail"><p className="error">{error}</p></section></div>;
  if (!run) return <div className="page"><section className="panel detail"><p className="empty">加载中</p></section></div>;

  const snapshot = directLlmSnapshotView(run);
  const datasetScorerName = run.manifest?.benchmark_provenance?.scorer as unknown;
  const scores = run.scores ?? [];
  const judgedScores = scores.filter(isDirectLlmJudged);
  const judged = judgedScores.length;
  const passed = judgedScores.filter((score) => score.passed).length;
  const usage = (run.cases ?? []).reduce(
    (sum, row) => ({
      prompt: sum.prompt + (row.result?.usage?.prompt_tokens ?? 0),
      completion: sum.completion + (row.result?.usage?.completion_tokens ?? 0),
    }),
    { prompt: 0, completion: 0 },
  );
  /* 分母只取 judged 事实；旧分数仅确定性判定 outcome 回落，调用失败与未尝试不猜测。 */
  const rate = judged > 0 ? Math.round((passed / judged) * 100) : null;
  const selection = runSelectionLabel(run.manifest?.benchmark_provenance);

  const rows: DrillRow[] = (run.case_ids ?? []).map((caseId) => {
    const score = scores.find((item) => item.case_id === caseId);
    const outcome = OUTCOME_LABELS[score?.outcome ?? ""]
      ?? { label: score?.passed ? "通过" : "不通过", tone: score?.passed ? "success" : "error" };
    const datasetCase = snapshot.cases.find((item) => item.case_id === caseId);
    const result = (run.cases ?? []).find((item) => item.case_id === caseId)?.result;
    const caseScorer = directLlmCaseScorer(snapshot, datasetCase, score, datasetScorerName);
    const caseMetadata = directLlmCaseMetadata(datasetCase);
    return {
      caseId,
      outcomeLabel: outcome.label,
      outcomeTone: outcome.tone,
      summary: outputText(result),
      detail: (
        <>
          <p><span className="field-label">题面</span>{directLlmCasePrompt(datasetCase)}</p>
          <p>
            <span className="field-label">模型输出</span>
            <span className="mono">{typeof result?.content === "string" ? result.content : JSON.stringify(result?.content ?? result)}</span>
          </p>
          {result?.error && (
            <p><span className="field-label">失败原因</span><span className="fail">{result.error.class ? `${result.error.class}：` : ""}{result.error.message}</span></p>
          )}
          <p>
            <span className="field-label">期望</span>
            <span className="mono">{directLlmCaseExpected(datasetCase)}</span>
          </p>
          <p><span className="field-label">评分器</span><span className="mono">{scorerShort(caseScorer)}</span></p>
          {caseMetadata && (
            <p><span className="field-label">元数据</span><span className="mono">{caseMetadata}</span></p>
          )}
        </>
      ),
    };
  });

  const reportRateItems: MetricItem[] = reportRates ? [
    { label: "覆盖率", value: `${Math.round(reportRates.coverage * 100)}%`, tone: "neutral" },
    ...(reportRates.completion == null ? [] : [{
      label: "完成率", value: `${Math.round(reportRates.completion * 100)}%`, tone: "neutral" as const,
    }]),
    ...(reportRates.attemptRate == null ? [] : [{
      label: "尝试率", value: `${Math.round(reportRates.attemptRate * 100)}%`, tone: "neutral" as const,
    }]),
  ] : [];
  const metricItems: MetricItem[] = [
    { label: `通过率 · ${passed}/${judged}`, value: rate == null ? "—" : `${rate}%`, tone: "success" },
    { label: `判定题数（选中 ${scores.length} · 无判定 ${scores.length - judged}）`, value: `${judged}`, tone: "neutral" },
    ...reportRateItems,
    { label: `tokens（输入 ${usage.prompt} + 输出 ${usage.completion}）`, value: String(usage.prompt + usage.completion), tone: "neutral" },
    { label: priceTable ? `成本 · pt ${priceTable}` : "成本", value: cost == null ? "—" : `¥${cost}`, tone: "neutral" },
    { label: "模型", value: modelLabel(run) ?? "—", tone: "neutral" },
  ];

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head">
          <h2 className="mono">运行 {runId} · 结果</h2>
          <div className="panel-head-actions">
            <StatusBadge status={run.status} />
            <button type="button" onClick={async () => { try { await rescoreRun(runId); setRun(await getRun(runId)); } catch (e) { setError(String(e)); } }}>
              重新评分
            </button>
          </div>
        </div>
        {run.error && (
          <p className="error">
            {run.error.code ?? run.error.type ?? ""} {run.error.message ?? ""}
          </p>
        )}
        {selection && <p className="hint">本次题目：{selection}</p>}
        {scorerShort(datasetScorerName) !== "—" && (
          <p className="hint mono">评分器 {scorerShort(datasetScorerName)}（数据集默认，单题可覆盖）</p>
        )}
        <MetricCards items={metricItems} />
        <RunAuditSummary run={run} />
        <CaseDrillTable rows={rows} />
      </section>
    </div>
  );
}
