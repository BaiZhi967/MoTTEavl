import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getReport, getRun, modelLabel, type RunRecord } from "../../api/client";
import { suiteRoutes } from "../registry";
import {
  directLlmCaseExpected, directLlmCaseMetadata, directLlmCasePrompt, directLlmCaseScorer,
  directLlmSnapshotView, isDirectLlmJudged,
} from "./DirectLlmResult";
import { scorerShort } from "./presets";

const ROUTES = suiteRoutes("direct-llm");

interface Column {
  runId: string;
  model: string;
  rate: number | null;
  judged: number;
  tokens: number;
  cost: number | null;
  failedCases: string[];
  run: RunRecord;
}

function collect(run: RunRecord, report: { cost?: { total?: number | null } } | null): Column {
  const scores = run.scores ?? [];
  const judged = scores.filter(isDirectLlmJudged);
  const passed = judged.filter((score) => score.passed);
  const tokens = (run.cases ?? []).reduce((sum, row) => sum + (row.result?.usage?.total_tokens ?? 0), 0);
  return {
    runId: run.id,
    model: modelLabel(run) ?? run.id,
    rate: judged.length > 0 ? Math.round((passed.length / judged.length) * 100) : null,
    judged: judged.length,
    tokens,
    cost: report?.cost?.total ?? null,
    failedCases: judged
      .filter((score) => !score.passed && typeof score.case_id === "string")
      .map((score) => score.case_id as string),
    run,
  };
}

interface DatasetIdentity {
  key: string;
  label: string;
}

interface ComparisonGate {
  comparable: boolean;
  reasons: string[];
  datasets: Array<DatasetIdentity | null>;
}

function datasetIdentity(run: RunRecord): DatasetIdentity | null {
  const manifest = run.manifest ?? {};
  const provenanceDataset = manifest.benchmark_provenance?.dataset;
  const scenarioDataset = manifest.benchmark_snapshot?.scenario?.dataset;
  for (const value of [provenanceDataset, scenarioDataset]) {
    if (typeof value === "string" && value.trim()) {
      return { key: `dataset:${value.trim()}`, label: value.trim() };
    }
  }
  const snapshotDataset = manifest.benchmark_snapshot?.dataset;
  if (typeof snapshotDataset?.name === "string" && typeof snapshotDataset?.version === "string") {
    const label = `${snapshotDataset.name}@${snapshotDataset.version}`;
    return { key: `dataset:${label}`, label };
  }
  if (typeof run.scenario_version === "string" && run.scenario_version) {
    return { key: `scenario:${run.scenario_version}`, label: `场景 ${run.scenario_version}` };
  }
  return null;
}

function caseSetIdentity(run: RunRecord): string {
  return JSON.stringify([...new Set(run.case_ids ?? [])].sort());
}

function comparisonGate(columns: Column[]): ComparisonGate {
  const datasets = columns.map((column) => datasetIdentity(column.run));
  if (columns.length < 2) return { comparable: true, reasons: [], datasets };

  const reasons: string[] = [];
  if (datasets.some((identity) => identity == null)) {
    reasons.push("缺少一致的数据集版本证据");
  } else if (new Set(datasets.map((identity) => identity?.key)).size > 1) {
    reasons.push(`数据集/版本不同（${datasets.map((identity) => identity?.label).join(" / ")}）`);
  }

  const caseSets = columns.map((column) => caseSetIdentity(column.run));
  if (new Set(caseSets).size > 1) {
    reasons.push(`题目集合不同（${columns.map((column) => column.run.case_ids?.length ?? 0).join(" / ")} 题）`);
  }

  return { comparable: reasons.length === 0, reasons, datasets };
}

export function DirectLlmCompare() {
  const [params] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  const [columns, setColumns] = useState<Column[] | null>(null);
  const [error, setError] = useState("");
  const [openCase, setOpenCase] = useState<string | null>(null);

  useEffect(() => {
    Promise.all(runIds.map((id) => Promise.all([getRun(id), getReport(id).catch(() => null)])))
      .then((entries) => setColumns(entries.map(([run, report]) => collect(run, report))))
      .catch((e) => setError(String(e)));
  }, [runIds.join(",")]);

  if (error) return <div className="page"><section className="panel detail"><p className="error">{error}</p></section></div>;
  if (!columns) return <div className="page"><section className="panel detail"><p className="empty">加载中</p></section></div>;

  const gate = comparisonGate(columns);
  /* 空数组 reduce 无初值会抛错（/direct-llm/compare 不带 runs 参数可直接到达）。 */
  const sharedFailed = gate.comparable && columns.length > 0
    ? columns
        .map((column) => new Set(column.failedCases))
        .reduce((acc, set) => new Set([...acc].filter((id) => set.has(id))))
    : new Set<string>();

  const snapshot = directLlmSnapshotView(columns[0]?.run);
  const datasetScorerName = gate.comparable
    ? columns[0]?.run.manifest?.benchmark_provenance?.scorer as unknown
    : undefined;

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head">
          <h2>Direct LLM · 多模型对比</h2>
        </div>
        {!gate.comparable ? (
          <>
            <p className="error" role="alert">
              所选运行不可比：{gate.reasons.join("；")}。为避免误导，未生成共同不通过与逐题对比矩阵。
            </p>
            <ul className="compare-case-list" aria-label="不可比运行">
              {columns.map((column, index) => (
                <li key={column.runId}>
                  <span>{column.model}</span>
                  <span className="muted mono"> {column.runId}</span>
                  <span className="hint mono">
                    {` · ${gate.datasets[index]?.label ?? "数据集版本未知"} · ${column.run.case_ids?.length ?? 0} 题`}
                  </span>
                  <Link className="link" to={ROUTES.result(column.runId)}>详情</Link>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            <p className="hint">
              分母是各运行里「有期望答案」的题数（无判定的题不参与比较）
              {scorerShort(datasetScorerName) !== "—"
                ? ` · 数据集默认评分器 ${scorerShort(datasetScorerName)}` : ""}
            </p>
            <table>
              <thead>
                <tr>
                  <th>指标</th>
                  {columns.map((column) => (
                    <th key={column.runId}>
                      {column.model}
                      <span className="muted mono"> {column.runId}</span>
                      <Link className="link" to={ROUTES.result(column.runId)}>详情</Link>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                <tr><td>通过率</td>{columns.map((c) => <td key={c.runId} className="mono">{c.rate == null ? "—" : `${c.rate}%`}</td>)}</tr>
                <tr><td>判定题数</td>{columns.map((c) => <td key={c.runId} className="mono">{c.judged}</td>)}</tr>
                <tr><td>tokens</td>{columns.map((c) => <td key={c.runId} className="mono">{c.tokens}</td>)}</tr>
                <tr><td>成本</td>{columns.map((c) => <td key={c.runId} className="mono">{c.cost == null ? "—" : `¥${c.cost}`}</td>)}</tr>
                <tr><td>共同不通过</td><td colSpan={Math.max(1, columns.length)} className="mono">{[...sharedFailed].join(", ") || "无"}</td></tr>
              </tbody>
            </table>

            <h3 className="embed-title">逐题下钻</h3>
            <ul className="compare-case-list">
              {(columns[0]?.run.case_ids ?? []).map((caseId) => (
                <li key={caseId}>
                  <button type="button" className="link" onClick={() => setOpenCase(openCase === caseId ? null : caseId)}>
                    {caseId}
                  </button>
                  {openCase === caseId && (
                    <div className="drill-detail">
                      <p>
                        <span className="field-label">题面</span>
                        {directLlmCasePrompt(snapshot.cases.find((item) => item.case_id === caseId))}
                      </p>
                      <p>
                        <span className="field-label">期望</span>
                        <span className="mono">
                          {directLlmCaseExpected(snapshot.cases.find((item) => item.case_id === caseId))}
                        </span>
                      </p>
                      <p>
                        <span className="field-label">评分器</span>
                        <span className="mono">
                          {scorerShort(directLlmCaseScorer(
                            snapshot,
                            snapshot.cases.find((item) => item.case_id === caseId),
                            columns[0]?.run.scores?.find((score) => score.case_id === caseId),
                            datasetScorerName,
                          ))}
                        </span>
                      </p>
                      {directLlmCaseMetadata(snapshot.cases.find((item) => item.case_id === caseId)) && (
                        <p>
                          <span className="field-label">元数据</span>
                          <span className="mono">
                            {directLlmCaseMetadata(snapshot.cases.find((item) => item.case_id === caseId))}
                          </span>
                        </p>
                      )}
                      {columns.map((column) => {
                        const result = column.run.cases?.find((item) => item.case_id === caseId)?.result;
                        return (
                          <p key={column.runId}>
                            <span className="field-label">{column.model}</span>
                            <span className="mono">{typeof result?.content === "string" ? result.content : result?.content != null ? JSON.stringify(result.content) : "（无结果）"}</span>
                          </p>
                        );
                      })}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </div>
  );
}
