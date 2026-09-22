import { useRef, useState, type FormEvent } from "react";
import { Board } from "../../board/Board";
import { EmptyBoard } from "../../board/EmptyBoard";
import { StatusFlap } from "../../board/StatusFlap";
import { ArrowsLeftRightIcon } from "@phosphor-icons/react";
import {
  COMPARISON_FACTORS,
  compareRunReports,
  describeApiError,
  type ComparabilityView,
} from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";

/**
 * 通用比较页（M6）：选两个固定 Run（可选各自 ScoringPass）+ 政策允许的因子，
 * 只读比较（零模型 / Judge / Runner 调用）。三级结论徽章来自 STATUS_META
 * （scope comparison）；结构性原因与指标原因分开；case diff 三组 mono 清单；
 * 缺失 / 部分资格不渲染成 0。
 */

function CaseDiffList({ title, ids, testId }: { title: string; ids: string[]; testId: string }) {
  return (
    <div data-testid={testId}>
      <p className="field-label">
        {title}
        <span className="mono">（{ids.length}）</span>
      </p>
      {ids.length === 0 ? (
        <p className="hint">无</p>
      ) : (
        <ul className="compare-case-list">
          {ids.map((caseId) => (
            <li key={caseId}><span className="mono hint">{caseId}</span></li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CompareResult({ result }: { result: ComparabilityView }) {
  const structural = result.structural_reasons ?? [];
  const metricReasons = result.metric_reasons ?? [];
  const metricEligibility = Object.entries(result.metric_eligibility ?? {});
  const diff = result.case_diff ?? { added: [], removed: [], changed: [] };
  return (
    <section className="panel detail" aria-label="比较结果" data-testid="compare-result">
      <div className="panel-head">
        <h2>比较结果</h2>
        <div className="panel-head-actions">
          <StatusBadge status={result.level} />
          <span className="hint">
            {result.eligible ? "质量指标可比（eligible）" : "质量指标不可比（不给任何质量结论）"}
          </span>
        </div>
      </div>

      {(result.allowed_differences ?? []).length > 0 && (
        <p className="hint">
          政策显式允许的差异：<span className="mono">{(result.allowed_differences ?? []).join(", ")}</span>
          （「允许」本身可审计）
        </p>
      )}

      <h3 className="embed-title">结构性原因</h3>
      {structural.length === 0 ? (
        <p className="hint pass">无结构性阻断。</p>
      ) : (
        <ul className="failure-list" data-testid="compare-structural-reasons">
          {structural.map((reason) => <li key={reason} className="hint fail">{reason}</li>)}
        </ul>
      )}

      <h3 className="embed-title">指标原因</h3>
      {metricReasons.length === 0 ? (
        <p className="hint pass">无指标级阻断。</p>
      ) : (
        <ul className="failure-list" data-testid="compare-metric-reasons">
          {metricReasons.map((reason) => <li key={reason} className="hint fail">{reason}</li>)}
        </ul>
      )}

      <h3 className="embed-title">指标资格</h3>
      {metricEligibility.length === 0 ? (
        <p className="empty">暂无指标资格数据</p>
      ) : (
        <Board
          testId="compare-metric-eligibility"
          label="指标资格板面"
          head={
            <>
              <th className="w-[280px]">metric</th>
              <th>资格</th>
            </>
          }
        >
            {metricEligibility.map(([metric, eligible]) => (
              <tr key={metric}>
                <td className="mono nowrap">{metric}</td>
                <td>
                  {eligible === true
                    ? <span className="pass">可比</span>
                    : eligible === false
                      ? <span className="fail">资格不足（部分可比，缺样本 / 未知成本不折算成 0）</span>
                      : <span className="hint">{String(eligible)}</span>}
                </td>
              </tr>
            ))}
        </Board>
      )}

      <h3 className="embed-title">Case 差异（按内容 hash 对齐）</h3>
      <CaseDiffList title="新增（仅候选）" ids={diff.added ?? []} testId="compare-diff-added" />
      <CaseDiffList title="移除（仅基线）" ids={diff.removed ?? []} testId="compare-diff-removed" />
      <CaseDiffList title="内容变化（同名不同内容）" ids={diff.changed ?? []} testId="compare-diff-changed" />
    </section>
  );
}

export function ComparePage() {
  const [baselineRun, setBaselineRun] = useState("");
  const [candidateRun, setCandidateRun] = useState("");
  const [baselinePass, setBaselinePass] = useState("");
  const [candidatePass, setCandidatePass] = useState("");
  const [factors, setFactors] = useState<string[]>(["model"]);
  const [result, setResult] = useState<ComparabilityView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  /** 迟到响应丢弃：新的比较发起后，旧结论不得覆盖。 */
  const compareSeq = useRef(0);

  const toggleFactor = (factor: string, checked: boolean) => {
    setFactors((items) => checked ? [...new Set([...items, factor])] : items.filter((item) => item !== factor));
  };

  const canCompare = baselineRun.trim() !== "" && candidateRun.trim() !== "" && factors.length > 0 && !busy;

  const onCompare = async (event: FormEvent) => {
    event.preventDefault();
    if (!canCompare) return;
    const seq = ++compareSeq.current;
    setBusy(true);
    setError("");
    try {
      const payload = await compareRunReports({
        baseline: baselineRun.trim(),
        candidate: candidateRun.trim(),
        factors,
        baseline_pass: baselinePass.trim() === "" ? undefined : baselinePass.trim(),
        candidate_pass: candidatePass.trim() === "" ? undefined : candidatePass.trim(),
      });
      if (compareSeq.current !== seq) return; // 已发起新的比较：迟到结论丢弃
      setResult(payload);
    } catch (caught) {
      if (compareSeq.current !== seq) return;
      setResult(null);
      const described = describeApiError(caught);
      setError(`比较失败${described.code ? "（" + described.code + "）" : ""}：${described.message}`);
    } finally {
      if (compareSeq.current === seq) setBusy(false);
    }
  };

  return (
    <div className="page">
      <section className="panel form-panel" aria-label="比较输入">
        <form onSubmit={onCompare}>
          <h2><ArrowsLeftRightIcon size={16} weight="bold" aria-hidden /> 比较两个 Run</h2>
          <p className="hint">只读固定报告（Run + ScoringPass），零模型 / Judge / Runner 调用；一切正式结果引用具体 pass，不追随 current。</p>
          <label htmlFor="compare-baseline-run">
            基线 Run（baseline）
            <input
              id="compare-baseline-run"
              className="mono"
              value={baselineRun}
              placeholder="run id"
              onChange={(change) => setBaselineRun(change.target.value)}
            />
          </label>
          <label htmlFor="compare-baseline-pass">
            基线 ScoringPass（可选）
            <input
              id="compare-baseline-pass"
              className="mono"
              value={baselinePass}
              placeholder="缺省 current pass"
              onChange={(change) => setBaselinePass(change.target.value)}
            />
          </label>
          <label htmlFor="compare-candidate-run">
            候选 Run（candidate）
            <input
              id="compare-candidate-run"
              className="mono"
              value={candidateRun}
              placeholder="run id"
              onChange={(change) => setCandidateRun(change.target.value)}
            />
          </label>
          <label htmlFor="compare-candidate-pass">
            候选 ScoringPass（可选）
            <input
              id="compare-candidate-pass"
              className="mono"
              value={candidatePass}
              placeholder="缺省 current pass"
              onChange={(change) => setCandidatePass(change.target.value)}
            />
          </label>

          <div className="model-picker-group" data-testid="compare-factors">
            <p className="field-label">政策允许变化的因子（至少一个；未允许的条件必须一致）</p>
            {COMPARISON_FACTORS.map((factor) => (
              <label key={factor} className="model-picker-item">
                <input
                  type="checkbox"
                  checked={factors.includes(factor)}
                  onChange={(change) => toggleFactor(factor, change.target.checked)}
                />
                <span className="mono">{factor}</span>
              </label>
            ))}
          </div>

          <div className="actions">
            <button
              type="submit"
              className="primary"
              disabled={!canCompare}
              title={factors.length === 0 ? "至少选择一个允许因子" : undefined}
              data-testid="compare-submit"
            >
              {busy ? "比较中…" : "比较（只读）"}
            </button>
          </div>
          {error && <p className="error" role="alert" data-testid="compare-error">{error}</p>}
        </form>
      </section>

      {result
        ? <CompareResult result={result} />
        : <section className="panel detail" aria-label="比较结果">
            <h2>比较结果</h2>
            <p className="empty">输入两个 Run 后显示三级结论、原因分组与 case 差异。</p>
          </section>}
    </div>
  );
}
