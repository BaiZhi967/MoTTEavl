import { useCallback, useEffect, useMemo, useState } from "react";
import {
  cancelRun,
  createRun,
  getReport,
  getRun,
  getRuns,
  rescoreRun,
  retryRun,
  subscribeRunEvents,
  type RunRecord,
  type TraceEvent,
} from "../api/client";
import { StatusBadge, statusLabel } from "../components/StatusBadge";
import { RunTimeline } from "../components/RunTimeline";
import { ScoreTable } from "../components/ScoreTable";

function downloadJson(name: string, payload: unknown) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}

function RunDetail({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [run, setRun] = useState<RunRecord | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    getRun(runId).then((record) => alive && setRun(record)).catch((e) => alive && setError(String(e)));
    const unsubscribe = subscribeRunEvents(runId, {
      onEvent: (event) =>
        setEvents((current) => (current.some((item) => item.seq === event.seq) ? current : [...current, event])),
    });
    return () => {
      alive = false;
      unsubscribe();
    };
  }, [runId]);

  const terminal = useMemo(() => run && ["completed", "failed", "cancelled", "unsupported", "profile_stale"].includes(run.status), [run]);

  return (
    <section className="panel detail">
      <header>
        <h2>运行 {runId}</h2>
        <button onClick={onClose}>关闭</button>
      </header>
      {error && <p className="error">{error}</p>}
      {run && (
        <dl className="kv">
          <dt>场景</dt>
          <dd>{run.scenario_version}</dd>
          <dt>状态</dt>
          <dd><StatusBadge status={run.status} /></dd>
          {run.parent_run_id && (
            <>
              <dt>父运行</dt>
              <dd>{run.parent_run_id}</dd>
            </>
          )}
          {run.cancellation?.reason && (
            <>
              <dt>取消原因</dt>
              <dd>{run.cancellation.reason}</dd>
            </>
          )}
        </dl>
      )}
      <div className="actions">
        <button
          onClick={async () => {
            try {
              await rescoreRun(runId);
              setRun(await getRun(runId));
            } catch (e) {
              setError(String(e));
            }
          }}
          disabled={!terminal || run?.status !== "completed"}
        >
          重新评分
        </button>
        <button
          onClick={async () => {
            try {
              downloadJson(`${runId}-report.json`, await getReport(runId));
            } catch (e) {
              setError(String(e));
            }
          }}
          disabled={!terminal}
        >
          导出报告
        </button>
      </div>
      <RunTimeline events={events} />
      {run?.scores && <ScoreTable scores={run.scores} />}
    </section>
  );
}

export function RunsPage() {
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [scenario, setScenario] = useState("replay@1");
  const [caseIds, setCaseIds] = useState("case-1");
  const [manifest, setManifest] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const payload = await getRuns(statusFilter || undefined);
      setRuns(payload.items);
    } catch (e) {
      setError(String(e));
    }
  }, [statusFilter]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 3000);
    return () => clearInterval(timer);
  }, [refresh]);

  const submit = async (form: Event) => {
    form.preventDefault();
    setError("");
    try {
      const body: { scenario_version: string; case_ids?: string[]; manifest?: any } = {
        scenario_version: scenario,
        case_ids: caseIds.split(/[,，\s]+/).filter(Boolean),
      };
      if (manifest.trim()) body.manifest = JSON.parse(manifest);
      const created = await createRun(body);
      setSelected(created.id);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const act = async (action: () => Promise<unknown>) => {
    try {
      await action();
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <form className="panel" onSubmit={submit} aria-label="创建运行">
        <h2>创建运行</h2>
        <label>
          场景版本
          <input value={scenario} onChange={(change) => setScenario(change.target.value)} placeholder="scenario@version" />
        </label>
        <label>
          Case 列表（逗号分隔）
          <input value={caseIds} onChange={(change) => setCaseIds(change.target.value)} />
        </label>
        <label>
          Manifest JSON（可选）
          <textarea rows={3} value={manifest} onChange={(change) => setManifest(change.target.value)} placeholder='{"provider":{"kind":"replay","fixture":{...}}}' />
        </label>
        <button type="submit">创建</button>
        {error && <p className="error">{error}</p>}
      </form>

      <section className="panel">
        <h2>运行列表</h2>
        <label>
          状态过滤：
          <select value={statusFilter} onChange={(change) => setStatusFilter(change.target.value)} aria-label="状态过滤">
            <option value="">全部</option>
            {["queued", "preparing", "running", "collecting", "scoring", "completed", "failed", "cancelled", "unsupported", "profile_stale"].map((status) => (
              <option key={status} value={status}>
                {statusLabel(status)}
              </option>
            ))}
          </select>
        </label>
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>场景</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id}>
                <td>
                  <button className="link" onClick={() => setSelected(run.id)}>
                    {run.id}
                  </button>
                </td>
                <td>{run.scenario_version}</td>
                <td>
                  <StatusBadge status={run.status} />
                </td>
                <td className="actions">
                  {!["completed", "failed", "cancelled", "unsupported", "profile_stale"].includes(run.status) && (
                    <button onClick={() => act(() => cancelRun(run.id, "web 控制台取消"))}>取消</button>
                  )}
                  {["failed", "cancelled", "unsupported", "profile_stale"].includes(run.status) && (
                    <button onClick={() => act(() => retryRun(run.id))}>重试</button>
                  )}
                </td>
              </tr>
            ))}
            {runs.length === 0 && (
              <tr>
                <td colSpan={4} className="empty">
                  暂无运行
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {selected && <RunDetail runId={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}
