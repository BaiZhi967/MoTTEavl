import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { createRun, getModels, getRun, modelLabel, type ModelRecord, type RunRecord } from "../../api/client";
import { ModelPicker } from "../../components/ModelPicker";
import { BatchMonitor } from "../../components/BatchMonitor";
import { MetricCards } from "../../components/MetricCards";
import { CaseDrillTable, type DrillRow } from "../../components/CaseDrillTable";
import { useRunEvents } from "../../hooks/useRunEvents";
import { suiteRoutes } from "../registry";

const ROUTES = suiteRoutes("direct-llm");
const SCENARIO = "direct-llm@1";

export function DirectLlmOperate() {
  const navigate = useNavigate();
  const [models, setModels] = useState<ModelRecord[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [caseIds, setCaseIds] = useState("");
  const [temperature, setTemperature] = useState("");
  const [maxTokens, setMaxTokens] = useState("");
  const [reasoningLevel, setReasoningLevel] = useState("");
  const [failures, setFailures] = useState<{ model: string; error: string }[]>([]);
  const [launched, setLaunched] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);

  useEffect(() => {
    getModels().then((payload) => setModels(payload.items)).catch(() => undefined);
  }, []);

  const selectedModel = models.find((model) => selected.length === 1 && model.id === selected[0]);
  const ceiling = selectedModel?.max_output_tokens ?? null;

  const doRun = async () => {
    setRunning(true);
    setFailures([]);
    setLaunched([]);
    setError("");
    const ids = caseIds.split(/[,，\s]+/).filter(Boolean);
    const parameters: Record<string, number> = {};
    if (temperature.trim()) parameters.temperature = Number(temperature);
    if (maxTokens.trim()) parameters.max_output_tokens = Number(maxTokens);
    const manifest: Record<string, unknown> = {};
    if (Object.keys(parameters).length > 0) manifest.parameters = parameters;
    if (reasoningLevel) manifest.reasoning_level = reasoningLevel;
    try {
      const created: string[] = [];
      const failed: { model: string; error: string }[] = [];
      for (const model of selected) {
        try {
          const run = await createRun({
            scenario_version: SCENARIO,
            manifest: { ...manifest, model },
            case_ids: ids,
          });
          created.push(run.id);
        } catch (e) {
          failed.push({ model, error: String(e) });
        }
      }
      setFailures(failed);
      if (created.length > 0 && failed.length === 0) {
        navigate(ROUTES.monitor(created));
        return;
      }
      if (created.length > 0) {
        // 混合成败：留在本页让失败就地可见，只给出批次入口。
        setLaunched(created);
        return;
      }
      if (failed.length === 0) setError("请先选择至少一个模型并填写 Case 列表");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="page">
      <section className="panel detail" aria-label="Direct LLM 操作页">
        <div className="panel-head"><h2>Direct LLM 评测 · 操作</h2></div>
        {error && <p className="error">{error}</p>}
        <div className="operate-grid">
          <div className="operate-card">
            <h3 className="embed-title">模型（可多选对比）</h3>
            <ModelPicker models={models} selected={selected} onToggle={(id) =>
              setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id])} />
          </div>
          <div className="operate-card">
            <h3 className="embed-title">场景与样本</h3>
            <label>
              场景
              <input value={SCENARIO} readOnly className="mono" />
            </label>
            <label>
              Case 列表（逗号分隔）
              <input value={caseIds} onChange={(change) => setCaseIds(change.target.value)} placeholder="case-1, case-2" />
            </label>
          </div>
          <div className="operate-card">
            <h3 className="embed-title">参数（可选）</h3>
            <label>
              temperature
              <input type="number" step="0.1" min="0" value={temperature} onChange={(change) => setTemperature(change.target.value)} />
            </label>
            <label>
              max_output_tokens{ceiling != null ? `（≤ ${ceiling}）` : ""}
              <input type="number" min="1" value={maxTokens} onChange={(change) => setMaxTokens(change.target.value)} />
            </label>
            {selectedModel?.reasoning?.supported && (
              <label>
                推理等级（默认 {selectedModel.reasoning.default_level ?? "无"}）
                <select className="mono" value={reasoningLevel} onChange={(change) => setReasoningLevel(change.target.value)}>
                  <option value="">默认</option>
                  {selectedModel.reasoning.levels.map((level) => <option key={level} value={level}>{level}</option>)}
                </select>
              </label>
            )}
          </div>
        </div>
        <div className="actions">
          <button type="button" onClick={() => void doRun()} disabled={running || selected.length === 0 || !caseIds.trim()}>
            {running ? "创建中…" : `发起评测（${selected.length} 个模型 × ${caseIds.split(/[,，\s]+/).filter(Boolean).length} case）`}
          </button>
          <span className="hint">真实调用 · 产生费用</span>
        </div>
        {failures.length > 0 && (
          <ul>{failures.map((f) => <li key={f.model} className="error">{f.model}：{f.error}</li>)}</ul>
        )}
        {launched.length > 0 && (
          <p>
            已创建 {launched.length} 个运行 ·{" "}
            <Link className="link" to={ROUTES.monitor(launched)}>查看批次进度</Link>
          </p>
        )}
      </section>
    </div>
  );
}

function CaseStream({ runId }: { runId: string }) {
  const { events } = useRunEvents(runId);
  const responses = events.filter((event) => event.type === "model_response");
  if (responses.length === 0) return <p className="hint">等待首个 case 响应…</p>;
  return (
    <div className="case-stream">
      {responses.map((event) => (
        <div key={event.seq} className="case-card">
          <p className="field-label mono">{event.case_id}</p>
          <p className="mono">{typeof event.result?.content === "string" ? event.result.content : JSON.stringify(event.result ?? "")}</p>
        </div>
      ))}
    </div>
  );
}

export function DirectLlmMonitor() {
  const [params] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  return (
    <div className="page">
      <BatchMonitor runIds={runIds} resultPath={ROUTES.result} renderDetail={(runId) => <CaseStream runId={runId} />} />
    </div>
  );
}

export function DirectLlmResult() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getRun(runId).then(setRun).catch((e) => setError(String(e)));
  }, [runId]);

  if (error) return <div className="page"><section className="panel detail"><p className="error">{error}</p></section></div>;
  if (!run) return <div className="page"><section className="panel detail"><p className="empty">加载中</p></section></div>;

  const scores = run.scores ?? [];
  const passed = scores.filter((score) => score.passed).length;
  const rows: DrillRow[] = (run.cases ?? []).map((entry) => ({
    caseId: entry.case_id,
    outcomeLabel: scores.find((score) => score.case_id === entry.case_id)?.passed ? "一致" : "不一致",
    outcomeTone: scores.find((score) => score.case_id === entry.case_id)?.passed ? "success" : "error",
    summary: typeof entry.result?.content === "string" ? entry.result.content.slice(0, 120) : "（无输出）",
    detail: (
      <>
        <p><span className="field-label">实际输出</span><span className="mono">{typeof entry.result?.content === "string" ? entry.result.content : JSON.stringify(entry.result?.content ?? "")}</span></p>
        <p><span className="field-label">期望</span><span className="mono">{typeof entry.expected === "string" ? entry.expected : JSON.stringify(entry.expected ?? "（无）")}</span></p>
      </>
    ),
  }));

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head"><h2 className="mono">运行 {runId} · 结果</h2></div>
        <MetricCards items={[
          { label: `通过 · ${passed}/${scores.length}`, value: scores.length > 0 ? `${Math.round((passed / scores.length) * 100)}%` : "—", tone: "success" },
          { label: "模型", value: modelLabel(run) ?? "—", tone: "neutral" },
        ]} />
        <CaseDrillTable rows={rows} />
      </section>
    </div>
  );
}
