import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { getReport, getRun, modelLabel, rescoreRun, type RunRecord } from "../../api/client";
import { MetricCards } from "../../components/MetricCards";
import { CaseDrillTable, type DrillRow } from "../../components/CaseDrillTable";
import { StatusBadge } from "../../components/StatusBadge";
import { RunAuditSummary } from "../../components/RunAuditSummary";
import { runSelectionLabel } from "../selection";
import { scorerShort } from "./presets";

/* 词汇表对齐真实 scorer（packages/evaluators/motte_eval/direct_llm.py）五种 outcome。 */
const OUTCOME_LABELS: Record<string, { label: string; tone: "success" | "error" | "neutral" }> = {
  correct: { label: "通过", tone: "success" },
  wrong_answer: { label: "不通过", tone: "error" },
  no_expectation: { label: "无判定", tone: "neutral" },
  call_failed: { label: "调用失败", tone: "error" },
  not_attempted: { label: "未尝试", tone: "neutral" },
};

function outputText(result: any): string {
  if (result == null) return "（无结果）";
  if (typeof result.content === "string" && result.content) return result.content.slice(0, 120);
  if (result.error?.message) return `错误：${result.error.message}`;
  return "（空输出）";
}

export function DirectLlmResult() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [cost, setCost] = useState<number | null>(null);
  const [priceTable, setPriceTable] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getRun(runId).then(setRun).catch((e) => setError(String(e)));
    getReport(runId)
      .then((report) => {
        setCost(report.cost?.total ?? null);
        setPriceTable(report.cost?.price_table_versions?.[0] ?? null);
      })
      .catch(() => undefined);
  }, [runId]);

  if (error) return <div className="page"><section className="panel detail"><p className="error">{error}</p></section></div>;
  if (!run) return <div className="page"><section className="panel detail"><p className="empty">加载中</p></section></div>;

  const snapshotCases = run.manifest?.benchmark_snapshot?.dataset?.cases as
    | { case_id: string; input: string; expected?: string | null; metadata?: Record<string, any> }[]
    | undefined;
  const datasetScorerName = run.manifest?.benchmark_provenance?.scorer as string | undefined;
  const scores = run.scores ?? [];
  const judged = scores.filter((score) => score.judged ?? score.outcome !== "no_expectation").length;
  const passed = scores.filter((score) => score.passed).length;
  const usage = (run.cases ?? []).reduce(
    (sum, row) => ({
      prompt: sum.prompt + (row.result?.usage?.prompt_tokens ?? 0),
      completion: sum.completion + (row.result?.usage?.completion_tokens ?? 0),
    }),
    { prompt: 0, completion: 0 },
  );
  /* 分母是「有期望答案的题」（judged）：无判定与未尝试都不进分母。 */
  const rate = judged > 0 ? Math.round((passed / judged) * 100) : null;
  const selection = runSelectionLabel(run.manifest?.benchmark_provenance);

  const rows: DrillRow[] = (run.case_ids ?? []).map((caseId) => {
    const score = scores.find((item) => item.case_id === caseId);
    const outcome = OUTCOME_LABELS[score?.outcome ?? ""]
      ?? { label: score?.passed ? "通过" : "不通过", tone: score?.passed ? "success" : "error" };
    const datasetCase = snapshotCases?.find((item) => item.case_id === caseId);
    const result = (run.cases ?? []).find((item) => item.case_id === caseId)?.result;
    const caseScorer = score?.scorer ?? datasetCase?.metadata?.scorer ?? datasetScorerName;
    return {
      caseId,
      outcomeLabel: outcome.label,
      outcomeTone: outcome.tone,
      summary: outputText(result),
      detail: (
        <>
          <p><span className="field-label">题面</span>{datasetCase?.input ?? "（题面缺失）"}</p>
          <p>
            <span className="field-label">模型输出</span>
            <span className="mono">{typeof result?.content === "string" ? result.content : JSON.stringify(result?.content ?? result)}</span>
          </p>
          {result?.error && (
            <p><span className="field-label">失败原因</span><span className="fail">{result.error.class ? `${result.error.class}：` : ""}{result.error.message}</span></p>
          )}
          <p>
            <span className="field-label">期望</span>
            <span className="mono">{datasetCase?.expected ?? "（无判定）"}</span>
          </p>
          <p><span className="field-label">评分器</span><span className="mono">{scorerShort(caseScorer)}</span></p>
        </>
      ),
    };
  });

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
        {datasetScorerName && <p className="hint mono">评分器 {datasetScorerName}（数据集默认，单题可覆盖）</p>}
        <MetricCards items={[
          { label: `通过率 · ${passed}/${judged}`, value: rate == null ? "—" : `${rate}%`, tone: "success" },
          { label: `判定题数（选中 ${scores.length} · 无判定 ${scores.length - judged}）`, value: `${judged}`, tone: "neutral" },
          { label: `tokens（输入 ${usage.prompt} + 输出 ${usage.completion}）`, value: String(usage.prompt + usage.completion), tone: "neutral" },
          { label: priceTable ? `成本 · pt ${priceTable}` : "成本", value: cost == null ? "—" : `¥${cost}`, tone: "neutral" },
          { label: "模型", value: modelLabel(run) ?? "—", tone: "neutral" },
        ]} />
        <RunAuditSummary run={run} />
        <CaseDrillTable rows={rows} />
      </section>
    </div>
  );
}
