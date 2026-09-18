import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { getRun, cancelRun, retryRun, rescoreRun, getReport, type RunRecord } from "../../api/client";
import { isTerminal, useRunEvents } from "../../hooks/useRunEvents";
import { StatusBadge } from "../../components/StatusBadge";
import { RunTimeline } from "../../components/RunTimeline";
import { ScoreTable } from "../../components/ScoreTable";
import { RunAuditSummary } from "../../components/RunAuditSummary";

/** 未匹配类型 run 的通用过程页：状态 + 时间线 + 取消/重试。 */
export function FallbackMonitorPage() {
  const { runId = "" } = useParams();
  const navigate = useNavigate();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [error, setError] = useState("");
  const { events, status } = useRunEvents(runId);
  const current = status ?? run?.status ?? null;
  const terminal = isTerminal(current);

  useEffect(() => {
    let alive = true;
    const load = () => getRun(runId).then((record) => alive && setRun(record)).catch((e) => alive && setError(String(e)));
    void load();
    if (!terminal) {
      const timer = setInterval(load, 3000);
      return () => { alive = false; clearInterval(timer); };
    }
    return () => { alive = false; };
  }, [runId, terminal]);

  const retry = async () => {
    try {
      const child = await retryRun(runId);
      navigate(`/runs/${child.id}/monitor`);
    } catch (e) {
      setError(String(e));
    }
  };

  const act = async (action: () => Promise<unknown>) => {
    try {
      await action();
      setRun(await getRun(runId));
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head">
          <h2 className="mono">运行 {runId}</h2>
          <div className="panel-head-actions">
            {terminal ? (
              <Link className="link" to={`/runs/${runId}/result`}>查看结果</Link>
            ) : (
              <button type="button" onClick={() => act(() => cancelRun(runId, "web 控制台取消"))}>取消</button>
            )}
            {terminal && run && ["failed", "cancelled", "unsupported", "profile_stale", "needs_review"].includes(current ?? "") && (
              <button type="button" onClick={() => void retry()}>重试</button>
            )}
          </div>
        </div>
        {error && <p className="error">{error}</p>}
        {run && (
          <dl className="kv">
            <dt>场景</dt><dd className="mono">{run.scenario_version}</dd>
            <dt>状态</dt><dd><StatusBadge status={current ?? "queued"} /></dd>
          </dl>
        )}
        {current === "queued" && (
          <p className="hint">等待 Worker 领取；若长期排队，请在服务端启动 Worker（make worker）。</p>
        )}
        <RunTimeline events={events} embedded />
      </section>
    </div>
  );
}

function downloadJson(name: string, payload: unknown) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}

/** 未匹配类型 run 的通用结果页：概要 + 评分表 + 重评分/导出。 */
export function FallbackResultPage() {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getRun(runId).then(setRun).catch((e) => setError(String(e)));
  }, [runId]);

  const act = async (action: () => Promise<unknown>) => {
    try {
      await action();
      setRun(await getRun(runId));
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <section className="panel detail">
        <div className="panel-head">
          <h2 className="mono">运行 {runId}</h2>
          <div className="panel-head-actions">
            <Link className="link" to="/runs">返回总览</Link>
            <button type="button" onClick={() => act(() => rescoreRun(runId))} disabled={run?.status !== "completed"}>
              重新评分
            </button>
            <button
              type="button"
              onClick={async () => {
                try {
                  downloadJson(`${runId}-report.json`, await getReport(runId));
                } catch (e) {
                  setError(String(e));
                }
              }}
              disabled={run?.status !== "completed" && !isTerminal(run?.status ?? null)}
            >
              导出报告
            </button>
          </div>
        </div>
        {error && <p className="error">{error}</p>}
        {run && (
          <dl className="kv">
            <dt>场景</dt><dd className="mono">{run.scenario_version}</dd>
            <dt>状态</dt><dd><StatusBadge status={run.status} /></dd>
            {run.parent_run_id && (<><dt>父运行</dt><dd className="mono">{run.parent_run_id}</dd></>)}
            {run.cancellation?.reason && (<><dt>取消原因</dt><dd>{run.cancellation.reason}</dd></>)}
          </dl>
        )}
        {run && <RunAuditSummary run={run} />}
        {run?.scores && <ScoreTable scores={run.scores} embedded />}
      </section>
    </div>
  );
}
