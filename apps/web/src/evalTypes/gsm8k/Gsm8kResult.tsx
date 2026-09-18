import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { getReport, getRun, rescoreRun, type RunRecord } from "../../api/client";
import { MetricCards } from "../../components/MetricCards";
import { CaseDrillTable, type DrillRow } from "../../components/CaseDrillTable";
import { StatusBadge } from "../../components/StatusBadge";

/* 词汇表对齐真实 scorer（packages/evaluators/motte_eval/gsm8k.py）五种 outcome。 */
const OUTCOME_LABELS: Record<string, { label: string; tone: "success" | "error" | "neutral" }> = {
  correct: { label: "答对", tone: "success" },
  wrong_answer: { label: "答错", tone: "error" },
  parse_failure: { label: "解析失败", tone: "error" },
  call_failed: { label: "调用失败", tone: "error" },
  not_attempted: { label: "未尝试", tone: "neutral" },
};

function outputText(result: any): string {
  if (result == null) return "（无结果）";
  if (typeof result.content === "string" && result.content) return result.content.slice(0, 120);
  if (result.error?.message) return `错误：${result.error.message}`;
  return "（空输出）";
}

export function Gsm8kResult() {
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
    | { case_id: string; input: any; expected: any }[] | undefined;
  const scores = run.scores ?? [];
  const passed = scores.filter((score) => score.passed).length;
  const usage = (run.cases ?? []).reduce(
    (sum, row) => ({
      prompt: sum.prompt + (row.result?.usage?.prompt_tokens ?? 0),
      completion: sum.completion + (row.result?.usage?.completion_tokens ?? 0),
    }),
    { prompt: 0, completion: 0 },
  );
  const accuracy = scores.length > 0 ? Math.round((passed / scores.length) * 100) : null;
  const attempted = scores.filter((score) => (score.outcome ?? "correct") !== "not_attempted").length;

  const rows: DrillRow[] = (run.case_ids ?? []).map((caseId) => {
    const score = scores.find((item) => item.case_id === caseId);
    const outcome = OUTCOME_LABELS[score?.outcome ?? ""] ?? { label: score?.passed ? "通过" : "未通过", tone: score?.passed ? "success" : "error" };
    const datasetCase = snapshotCases?.find((item) => item.case_id === caseId);
    const result = (run.cases ?? []).find((item) => item.case_id === caseId)?.result;
    const question = datasetCase ? (datasetCase.input?.question ?? JSON.stringify(datasetCase.input)) : "（题面缺失）";
    const expected = datasetCase?.expected ?? "（期望缺失）";
    return {
      caseId,
      outcomeLabel: outcome.label,
      outcomeTone: outcome.tone,
      summary: outputText(result),
      detail: (
        <>
          <p><span className="field-label">题目</span>{question}</p>
          <p><span className="field-label">模型输出</span><span className="mono">{typeof result?.content === "string" ? result.content : JSON.stringify(result?.content ?? result)}</span></p>
          {result?.error && (
            <p><span className="field-label">失败原因</span><span className="fail">{result.error.class ? `${result.error.class}：` : ""}{result.error.message}</span></p>
          )}
          <p><span className="field-label">期望</span><span className="mono">{typeof expected === "string" ? expected : JSON.stringify(expected)}</span></p>
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
        <MetricCards items={[
          { label: `accuracy · ${passed}/${scores.length}`, value: accuracy == null ? "—" : `${accuracy}%`, tone: "success" },
          { label: `tokens（输入 ${usage.prompt} + 输出 ${usage.completion}）`, value: String(usage.prompt + usage.completion), tone: "neutral" },
          { label: priceTable ? `成本 · pt ${priceTable}` : "成本", value: cost == null ? "—" : `¥${cost}`, tone: "neutral" },
          { label: `口径（选中 ${run.case_ids?.length ?? 0} · 应答 ${attempted}）`, value: `${passed}/${scores.length}`, tone: "neutral" },
        ]} />
        <CaseDrillTable rows={rows} />
      </section>
    </div>
  );
}
