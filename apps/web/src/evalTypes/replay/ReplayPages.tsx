import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { createRun, getRun, type RunRecord } from "../../api/client";
import { BatchMonitor } from "../../components/BatchMonitor";
import { CaseDrillTable, type DrillRow } from "../../components/CaseDrillTable";
import { RunTimeline } from "../../components/RunTimeline";
import { useRunEvents } from "../../hooks/useRunEvents";
import { suiteRoutes } from "../registry";

const ROUTES = suiteRoutes("replay");
const SCENARIOS = ["replay@1", "json_extract@1"];

export function ReplayOperate() {
  const navigate = useNavigate();
  const [scenario, setScenario] = useState(SCENARIOS[0]);
  const [caseIds, setCaseIds] = useState("case-1");
  const [manifest, setManifest] = useState("");
  const [error, setError] = useState("");

  const submit = () => {
    setError("");
    let parsed: any = {};
    if (manifest.trim()) {
      try {
        parsed = JSON.parse(manifest);
      } catch (e) {
        setError(`Manifest JSON 无法解析：${e}`);
        return;
      }
    }
    createRun({
      scenario_version: scenario,
      manifest: parsed,
      case_ids: caseIds.split(/[,，\s]+/).filter(Boolean),
    })
      .then((run) => navigate(ROUTES.monitor([run.id])))
      .catch((e) => setError(String(e)));
  };

  return (
    <div className="page">
      <section className="panel form-panel">
        <h2>Replay 回放 · 操作</h2>
        {error && <p className="error">{error}</p>}
        <label>
          场景
          <select className="mono" value={scenario} onChange={(change) => setScenario(change.target.value)}>
            {SCENARIOS.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>
          Case 列表（逗号分隔）
          <input value={caseIds} onChange={(change) => setCaseIds(change.target.value)} />
        </label>
        <label>
          Manifest JSON（inline provider 或 replay fixture）
          <textarea className="mono" rows={6} value={manifest} onChange={(change) => setManifest(change.target.value)}
            placeholder='{"provider":{"kind":"replay","fixture":{…}}}' />
        </label>
        <button type="button" onClick={submit}>创建回放</button>
        <p className="hint">回放不产生模型费用；fixture 与期望在 Manifest 或 replay 接口中提供。</p>
      </section>
    </div>
  );
}

function ReplayRowDetail({ runId }: { runId: string }) {
  const { events } = useRunEvents(runId);
  return <RunTimeline events={events} embedded />;
}

export function ReplayMonitor() {
  const [params] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  return (
    <div className="page">
      <BatchMonitor runIds={runIds} resultPath={ROUTES.result} renderDetail={(runId) => <ReplayRowDetail runId={runId} />} />
    </div>
  );
}

export function ReplayResult() {
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
    summary: typeof entry.result?.content === "string" ? entry.result.content.slice(0, 120) : JSON.stringify(entry.result ?? ""),
    detail: (
      <>
        <p><span className="field-label">实际</span><span className="mono">{typeof entry.result?.content === "string" ? entry.result.content : JSON.stringify(entry.result ?? "")}</span></p>
        <p><span className="field-label">期望</span><span className="mono">{typeof entry.expected === "string" ? entry.expected : JSON.stringify(entry.expected ?? "（无）")}</span></p>
      </>
    ),
  }));

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head"><h2 className="mono">运行 {runId} · 结果</h2></div>
        <p className="summary">共 {scores.length} 项，一致 {passed}，不一致 {scores.length - passed}</p>
        <CaseDrillTable rows={rows} />
      </section>
    </div>
  );
}
