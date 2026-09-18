import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getReport, getRun, modelLabel, type RunRecord } from "../../api/client";
import { suiteRoutes } from "../registry";

const ROUTES = suiteRoutes("gsm8k");

interface Column {
  runId: string;
  model: string;
  accuracy: number | null;
  tokens: number;
  cost: number | null;
  failedCases: string[];
  run: RunRecord;
}

function collect(run: RunRecord, report: { cost?: { total?: number | null } } | null): Column {
  const scores = run.scores ?? [];
  const passed = scores.filter((score) => score.passed);
  const tokens = (run.cases ?? []).reduce((sum, row) => sum + (row.result?.usage?.total_tokens ?? 0), 0);
  return {
    runId: run.id,
    model: modelLabel(run) ?? run.id,
    accuracy: scores.length > 0 ? Math.round((passed.length / scores.length) * 100) : null,
    tokens,
    cost: report?.cost?.total ?? null,
    failedCases: scores.filter((score) => !score.passed).map((score) => score.case_id),
    run,
  };
}

export function Gsm8kCompare() {
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

  /* 空数组 reduce 无初值会抛错（/gsm8k/compare 不带 runs 参数可直接到达），单列时语义保持为该 run 自己的答错集 */
  const sharedFailed = columns.length > 0
    ? columns
        .map((column) => new Set(column.failedCases))
        .reduce((acc, set) => new Set([...acc].filter((id) => set.has(id))))
    : new Set<string>();

  const snapshotCases = (columns[0]?.run.manifest?.benchmark_snapshot?.dataset?.cases ?? []) as
    { case_id: string; input: any; expected: any }[];
  /* 子集运行可以各选不同题目：题数不一致时 accuracy 不可直接横向比较。 */
  const sizes = [...new Set(columns.map((column) => column.run.case_ids?.length ?? 0))];

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head">
          <h2>GSM8K · 多模型对比</h2>
        </div>
        {sizes.length > 1 && (
          <p className="hint">
            各列选中的题目数不同（{sizes.join(" / ")} 题），accuracy 不是同一题集上的比较。
          </p>
        )}
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
            <tr><td>accuracy</td>{columns.map((c) => <td key={c.runId} className="mono">{c.accuracy == null ? "—" : `${c.accuracy}%`}</td>)}</tr>
            <tr><td>tokens</td>{columns.map((c) => <td key={c.runId} className="mono">{c.tokens}</td>)}</tr>
            <tr><td>成本</td>{columns.map((c) => <td key={c.runId} className="mono">{c.cost == null ? "—" : `¥${c.cost}`}</td>)}</tr>
            <tr><td>答错题重合</td><td colSpan={Math.max(1, columns.length)} className="mono">{[...sharedFailed].join(", ") || "无"}</td></tr>
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
                  <p><span className="field-label">题目</span>{snapshotCases.find((c) => c.case_id === caseId)?.input?.question ?? "（题面缺失）"}</p>
                  <p><span className="field-label">期望</span><span className="mono">{String(snapshotCases.find((c) => c.case_id === caseId)?.expected ?? "")}</span></p>
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
      </section>
    </div>
  );
}
