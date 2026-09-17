import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { cancelRun, getRun, retryRun, type RunRecord } from "../api/client";
import { StatusBadge } from "./StatusBadge";
import { RunProgress } from "./RunProgress";
import { countDone, isTerminal, useRunEvents } from "../hooks/useRunEvents";

const RETRYABLE = ["failed", "cancelled", "unsupported", "profile_stale"];

function BatchRow({ runId, resultPath, renderDetail }: {
  runId: string;
  resultPath: (runId: string) => string;
  renderDetail?: (runId: string) => ReactNode;
}) {
  const [run, setRun] = useState<RunRecord | null>(null);
  const { events, status } = useRunEvents(runId);
  const [expanded, setExpanded] = useState(false);
  const current = status ?? run?.status ?? "queued";
  const terminal = isTerminal(current);

  useEffect(() => {
    let alive = true;
    const load = () => getRun(runId).then((record) => alive && setRun(record)).catch(() => undefined);
    void load();
    if (!terminal) {
      const timer = setInterval(load, 3000);
      return () => { alive = false; clearInterval(timer); };
    }
    return () => { alive = false; };
  }, [runId, terminal]);

  const total = run?.case_ids?.length ?? 0;
  const done = terminal ? (run?.cases?.length ?? total) : Math.max(countDone(events), run?.cases?.length ?? 0);

  const refresh = () => getRun(runId).then(setRun).catch(() => undefined);

  return (
    <li className="batch-row">
      <div className="batch-row-head">
        <button type="button" className="link" onClick={() => setExpanded((open) => !open)} aria-expanded={expanded}>
          {runId}
        </button>
        <span className="mono">{run?.model ?? "—"}</span>
        <StatusBadge status={current} />
        <RunProgress done={done} total={total} />
        {terminal ? (
          <>
            <Link className="link" to={resultPath(runId)}>结果</Link>
            {RETRYABLE.includes(current) && (
              <button type="button" onClick={() => void retryRun(runId).then(refresh)}>重试</button>
            )}
          </>
        ) : (
          <button type="button" onClick={() => void cancelRun(runId, "web 控制台取消")}>取消</button>
        )}
      </div>
      {current === "queued" && <p className="hint">等待 Worker 领取；若长期排队，请在服务端启动 Worker（make worker）。</p>}
      {terminal && run?.error && (
        <p className="error">
          {run.error.code ?? run.error.type ?? ""} {run.error.message ?? ""}
        </p>
      )}
      {expanded && renderDetail?.(runId)}
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
      {comparePath && <Link className="link" to={comparePath}>查看对比结果</Link>}
    </section>
  );
}
