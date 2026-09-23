import { useEffect, useState } from "react";
import { Board } from "../../board/Board";
import { Link, useSearchParams } from "react-router-dom";
import { suiteRoutes } from "../registry";
import {
  directLlmCaseExpected, directLlmCaseMetadata, directLlmCasePrompt, directLlmCaseScorer,
  directLlmSnapshotView,
} from "./DirectLlmResult";
import { scorerShort } from "./presets";
import { loadSuiteComparison, snapshotCost, snapshotRate, usageTotal, type SuiteComparison } from "../suiteComparison";

const ROUTES = suiteRoutes("direct-llm");

export function DirectLlmCompare() {
  const [params] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  const [comparison, setComparison] = useState<SuiteComparison | null>(null);
  const [error, setError] = useState("");
  const [openCase, setOpenCase] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setComparison(null);
    setError("");
    if (!runIds.length) return () => { alive = false; };
    loadSuiteComparison(runIds, runIds.map((_, index) => params.get(`pass${index}`) ?? undefined))
      .then((result) => { if (alive) setComparison(result); })
      .catch((caught) => { if (alive) setError(String(caught)); });
    return () => { alive = false; };
  }, [params.toString()]);

  if (runIds.length === 0) {
    return (
      <div className="page">
        <section className="panel detail">
          <div className="panel-head"><h2>Direct LLM · 多模型对比</h2></div>
          <p className="empty">
            暂无可对比运行。对比入口在批次过程页（全部运行到终态后出现「查看对比结果」），
            也可从 <Link className="link" to="/runs">运行总览</Link> 回看单次结果。
          </p>
        </section>
      </div>
    );
  }

  if (error) return <div className="page"><section className="panel detail"><p className="error">{error}</p></section></div>;
  if (!comparison) return <div className="page"><section className="panel detail"><p className="empty">加载中</p></section></div>;

  const { columns, comparable, reasons, sharedFailed } = comparison;

  const snapshot = directLlmSnapshotView(columns[0]?.run);
  const datasetScorerName = comparable
    ? columns[0]?.run.manifest?.benchmark_provenance?.scorer as unknown
    : undefined;

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head">
          <h2>Direct LLM · 多模型对比</h2>
        </div>
        {!comparable ? (
          <>
            <p className="error" role="alert">
              所选运行不可比：{reasons.join("；")}。未生成共同不通过与逐题对比矩阵。
            </p>
            <ul className="compare-case-list" aria-label="不可比运行">
              {columns.map((column) => (
                <li key={column.run.id}>
                  <span>{column.model}</span>
                  <span className="muted mono"> {column.run.id}</span>
                  <span className="hint mono"> · {column.snapshot.ref.scoring_pass_id} · {column.run.case_ids?.length ?? 0} 题</span>
                  <Link className="link" to={ROUTES.result(column.run.id)}>详情</Link>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <>
            {comparison.comparisons.some((item) => item.metric_eligibility?.cost === false) && (
              <p className="hint" role="status">成本指标不可比：{comparison.comparisons.flatMap((item) => item.metric_reasons ?? []).join("；") || "证据不足"}</p>
            )}
            <p className="hint">
              分母是各运行里「有期望答案」的题数（无判定的题不参与比较）
              {scorerShort(datasetScorerName) !== "—"
                ? ` · 数据集默认评分器 ${scorerShort(datasetScorerName)}` : ""}
            </p>
            <Board
              label="数据板面"
              head={<>
                <th>指标</th>
                {columns.map((column) => (
                <th key={column.run.id}>
                {column.model}
                <span className="muted mono"> {column.run.id} · {column.snapshot.ref.scoring_pass_id}</span>
                <Link className="link" to={ROUTES.result(column.run.id)}>详情</Link>
                </th>
                ))}
              </>}
            >

              

                <tr><td>通过率</td>{columns.map((c) => <td key={c.run.id} className="mono">{snapshotRate(c.snapshot, "judged_accuracy")}</td>)}</tr>
                <tr><td>判定题数</td>{columns.map((c) => <td key={c.run.id} className="mono">{c.snapshot.counts.judged ?? "未知"}</td>)}</tr>
                <tr><td>tokens</td>{columns.map((c) => <td key={c.run.id} className="mono">{usageTotal(c.run)}</td>)}</tr>
                <tr><td>成本</td>{columns.map((c) => <td key={c.run.id} className="mono">{snapshotCost(c.snapshot)}</td>)}</tr>
                <tr><td>共同不通过</td><td colSpan={Math.max(1, columns.length)} className="mono">{sharedFailed.join(", ") || "无"}</td></tr>

            </Board>

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
                            undefined,
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
                          <p key={column.run.id}>
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
