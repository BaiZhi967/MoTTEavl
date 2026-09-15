import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as Select from "@radix-ui/react-select";
import { CaretDownIcon, CheckIcon, DotsThreeIcon, XIcon } from "@phosphor-icons/react";
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
import { StatusBadge } from "../components/StatusBadge";
import { STATUS_ORDER, statusLabel } from "../components/statusMeta";
import { RunTimeline } from "../components/RunTimeline";
import { ScoreTable } from "../components/ScoreTable";

const TERMINAL_STATUSES = ["completed", "failed", "cancelled", "unsupported", "profile_stale"];
const RETRYABLE_STATUSES = ["failed", "cancelled", "unsupported", "profile_stale"];

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

  const terminal = useMemo(() => run && TERMINAL_STATUSES.includes(run.status), [run]);

  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content" aria-label={`运行 ${runId} 详情`}>
          <div className="dialog-header">
            <Dialog.Title className="dialog-title mono">运行 {runId}</Dialog.Title>
            <Dialog.Close asChild>
              <button className="icon-btn" aria-label="关闭详情">
                <XIcon size={16} weight="bold" aria-hidden />
              </button>
            </Dialog.Close>
          </div>
          {error && <p className="error">{error}</p>}
          {run && (
            <dl className="kv">
              <dt>场景</dt>
              <dd className="mono">{run.scenario_version}</dd>
              <dt>状态</dt>
              <dd><StatusBadge status={run.status} /></dd>
              {run.parent_run_id && (
                <>
                  <dt>父运行</dt>
                  <dd className="mono">{run.parent_run_id}</dd>
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
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function StatusFilter({ value, onChange }: { value: string; onChange: (next: string) => void }) {
  return (
    <Select.Root value={value} onValueChange={onChange}>
      <Select.Trigger className="select-trigger" aria-label="状态过滤">
        <Select.Value />
        <CaretDownIcon size={14} weight="bold" aria-hidden />
      </Select.Trigger>
      <Select.Portal>
        <Select.Content className="select-content" position="popper" sideOffset={4}>
          <Select.Viewport>
            <Select.Item value="all" className="select-item">
              <Select.ItemText>全部</Select.ItemText>
              <Select.ItemIndicator className="select-item-indicator">
                <CheckIcon size={14} weight="bold" aria-hidden />
              </Select.ItemIndicator>
            </Select.Item>
            {STATUS_ORDER.map((status) => (
              <Select.Item key={status} value={status} className="select-item">
                <Select.ItemText>{statusLabel(status)}</Select.ItemText>
                <Select.ItemIndicator className="select-item-indicator">
                  <CheckIcon size={14} weight="bold" aria-hidden />
                </Select.ItemIndicator>
              </Select.Item>
            ))}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}

export function RunsPage() {
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [statusFilter, setStatusFilter] = useState("all");
  const [selected, setSelected] = useState<string | null>(null);
  const [scenario, setScenario] = useState("replay@1");
  const [caseIds, setCaseIds] = useState("case-1");
  const [manifest, setManifest] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const payload = await getRuns(statusFilter === "all" ? undefined : statusFilter);
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

  const submit = async (form: FormEvent<HTMLFormElement>) => {
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
          <StatusFilter value={statusFilter} onChange={setStatusFilter} />
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
            {runs.map((run) => {
              const cancellable = !TERMINAL_STATUSES.includes(run.status);
              const retryable = RETRYABLE_STATUSES.includes(run.status);
              return (
                <tr key={run.id}>
                  <td>
                    <button className="link" onClick={() => setSelected(run.id)}>
                      {run.id}
                    </button>
                  </td>
                  <td className="mono">{run.scenario_version}</td>
                  <td>
                    <StatusBadge status={run.status} />
                  </td>
                  <td className="actions">
                    {cancellable || retryable ? (
                      <DropdownMenu.Root>
                        <DropdownMenu.Trigger asChild>
                          <button className="icon-btn" aria-label={`运行 ${run.id} 操作`}>
                            <DotsThreeIcon size={16} weight="bold" aria-hidden />
                          </button>
                        </DropdownMenu.Trigger>
                        <DropdownMenu.Portal>
                          <DropdownMenu.Content className="menu-content" align="end" sideOffset={4}>
                            {cancellable && (
                              <DropdownMenu.Item
                                className="menu-item danger"
                                onSelect={() => act(() => cancelRun(run.id, "web 控制台取消"))}
                              >
                                取消
                              </DropdownMenu.Item>
                            )}
                            {retryable && (
                              <DropdownMenu.Item className="menu-item" onSelect={() => act(() => retryRun(run.id))}>
                                重试
                              </DropdownMenu.Item>
                            )}
                          </DropdownMenu.Content>
                        </DropdownMenu.Portal>
                      </DropdownMenu.Root>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              );
            })}
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
