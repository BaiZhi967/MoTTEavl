import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getRun, type RunRecord } from "../../api/client";
import { BatchMonitor } from "../../components/BatchMonitor";
import { RunTimeline } from "../../components/RunTimeline";
import { isTerminal, useRunEvents } from "../../hooks/useRunEvents";
import { gridCells } from "./grid";
import { suiteRoutes } from "../registry";

const ROUTES = suiteRoutes("gsm8k");

function GridDetail({ run }: { run: RunRecord }) {
  return (
    <div className="progress-grid" role="list" aria-label="逐题进度">
      {gridCells(run).map((cell) => (
        <span
          key={cell.caseId}
          className={`grid-cell grid-cell-${cell.state}`}
          role="listitem"
          title={`${cell.caseId}：${cell.state === "pass" ? "答对" : cell.state === "fail" ? "答错" : "未跑"}`}
        >
          {cell.caseId.replace(/^.*?(\d+)$/, "$1")}
        </span>
      ))}
    </div>
  );
}

function Gsm8kRowDetail({ runId }: { runId: string }) {
  const [run, setRun] = useState<RunRecord | null>(null);
  const { events } = useRunEvents(runId);
  useEffect(() => {
    let alive = true;
    const load = () => getRun(runId).then((record) => alive && setRun(record)).catch(() => undefined);
    void load();
    const timer = setInterval(load, 3000);
    return () => { alive = false; clearInterval(timer); };
  }, [runId]);
  if (!run) return <p className="hint">加载逐题进度…</p>;
  return (
    <>
      <GridDetail run={run} />
      <RunTimeline events={events} embedded />
    </>
  );
}

export function Gsm8kMonitor() {
  const [params] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  const [allTerminal, setAllTerminal] = useState(false);

  useEffect(() => {
    if (runIds.length === 0) return;
    const timer = setInterval(async () => {
      const runs = await Promise.all(runIds.map((id) => getRun(id).catch(() => null)));
      setAllTerminal(runs.every((run) => run != null && isTerminal(run.status)));
    }, 3000);
    return () => clearInterval(timer);
  }, [runIds.join(",")]);

  return (
    <div className="page">
      <BatchMonitor
        runIds={runIds}
        resultPath={ROUTES.result}
        comparePath={allTerminal ? ROUTES.compare(runIds) : undefined}
        renderDetail={(runId) => <Gsm8kRowDetail runId={runId} />}
      />
    </div>
  );
}
