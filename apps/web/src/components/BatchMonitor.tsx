import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { cancelRun, getRun, modelLabel, retryRun, type RunRecord } from "../api/client";
import { StatusBadge } from "./StatusBadge";
import { RunProgress } from "./RunProgress";
import { RunErrorBanner } from "./RunErrorBanner";
import { countDone, isTerminal, useRunEvents } from "../hooks/useRunEvents";

const RETRYABLE = ["failed", "cancelled", "unsupported", "profile_stale", "needs_review"];

function BatchRow({ runId, resultPath, renderDetail }: {
  runId: string;
  resultPath: (runId: string) => string;
  renderDetail?: (runId: string) => ReactNode;
}) {
  const [activeRunId, setActiveRunId] = useState(runId);
  const [run, setRun] = useState<RunRecord | null>(null);
  const { events, status } = useRunEvents(activeRunId);
  const [expanded, setExpanded] = useState(false);
  const current = status ?? run?.status ?? "queued";
  const terminal = isTerminal(current);

  useEffect(() => {
    let alive = true;
    const load = () => getRun(activeRunId).then((record) => alive && setRun(record)).catch(() => undefined);
    void load();
    if (!terminal) {
      const timer = setInterval(load, 3000);
      return () => { alive = false; clearInterval(timer); };
    }
    return () => { alive = false; };
  }, [activeRunId, terminal]);

  const total = run?.case_ids?.length ?? 0;
  const done = terminal ? (run?.cases?.length ?? total) : Math.max(countDone(events), run?.cases?.length ?? 0);

  const followRetry = (child: RunRecord) => {
    setRun(child);
    setActiveRunId(child.id);
    setExpanded(false);
  };

  return (
    <li className="batch-row">
      <div className="batch-row-head">
        <button type="button" className="link" onClick={() => setExpanded((open) => !open)} aria-expanded={expanded}>
          {activeRunId}
        </button>
        <span className="mono">{modelLabel(run) ?? "—"}</span>
        <StatusBadge status={current} />
        <RunProgress done={done} total={total} />
        {terminal ? (
          <>
            <Link className="link" to={resultPath(activeRunId)}>结果</Link>
            {RETRYABLE.includes(current) && (
              <button type="button" onClick={() => void retryRun(activeRunId).then(followRetry)}>重试</button>
            )}
          </>
        ) : (
          <button type="button" onClick={() => void cancelRun(activeRunId, "web 控制台取消")}>取消</button>
        )}
      </div>
      {current === "queued" && <p className="hint">等待 Worker 领取；若长期排队，请在服务端启动 Worker（make worker）。</p>}
      {terminal && run?.error && <RunErrorBanner run={run} monitor />}
      {expanded && renderDetail?.(activeRunId)}
    </li>
  );
}

export function BatchMonitor({ runIds, resultPath, comparePath, renderDetail }: {
  runIds: string[];
  resultPath: (runId: string) => string;
  comparePath?: string;
  renderDetail?: (runId: string) => ReactNode;
}) {
  return (
    <section className="panel detail" aria-label="批次过程">
      <div className="panel-head">
        <h2>运行过程</h2>
      </div>
      <ul className="batch-list">
        {runIds.map((runId) => (
          <BatchRow key={runId} runId={runId} resultPath={resultPath} renderDetail={renderDetail} />
        ))}
      </ul>
      {runIds.length === 0 && (
        <p className="empty">
          未指定运行。从各类型操作页发起跑测后自动进入，或到
          <Link className="link" to="/runs">运行总览</Link>
          查看历史运行。
        </p>
      )}
      {comparePath && <Link className="link" to={comparePath}>查看对比结果</Link>}
    </section>
  );
}
