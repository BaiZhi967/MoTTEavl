import { useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as Select from "@radix-ui/react-select";
import { CaretDownIcon, CheckIcon, DotsThreeIcon } from "@phosphor-icons/react";
import { cancelRun, getRuns, retryRun, type RunRecord } from "../api/client";
import { StatusBadge } from "../components/StatusBadge";
import { STATUS_ORDER, statusLabel } from "../components/statusMeta";
import { formatDuration, formatTimestamp, shortRunId } from "../components/runFormat";
import { isTerminal } from "../hooks/useRunEvents";
import { EVAL_SUITES, suiteForRun, suiteRoutes } from "../evalTypes/registry";

const RETRYABLE_STATUSES = ["failed", "cancelled", "unsupported", "profile_stale", "needs_review"];

function typeLabel(run: RunRecord): string {
  return suiteForRun(run)?.label ?? "通用";
}

export function RunsOverviewPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [statusFilter, setStatusFilter] = useState("all");
  // 套件顶栏「结果」页签以 ?type=<套件标签> 落入本页，合法的标签作为初始过滤值
  const [typeFilter, setTypeFilter] = useState(() => {
    const wanted = searchParams.get("type");
    const known = [...EVAL_SUITES.map((suite) => suite.label), "通用"];
    return wanted && known.includes(wanted) ? wanted : "all";
  });
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const payload = await getRuns(statusFilter === "all" ? undefined : statusFilter);
      setRuns(payload.items);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, [statusFilter]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 3000);
    return () => clearInterval(timer);
  }, [refresh]);

  const visible = runs.filter((run) => typeFilter === "all" || typeLabel(run) === typeFilter);

  const monitorPath = (run: RunRecord) => {
    const suite = suiteForRun(run);
    return suite ? suiteRoutes(suite.id).monitor([run.id]) : `/runs/${run.id}/monitor`;
  };

  /* 非终态优先看过程，终态直达结果 */
  const openRun = (run: RunRecord) => {
    if (!isTerminal(run.status)) {
      navigate(monitorPath(run));
      return;
    }
    const suite = suiteForRun(run);
    navigate(suite ? suiteRoutes(suite.id).result(run.id) : `/runs/${run.id}/result`);
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
      <section className="panel detail">
        <div className="panel-head">
          <h2>运行总览</h2>
          <div className="inline-field">
            <span className="field-label">类型</span>
            <Select.Root value={typeFilter} onValueChange={setTypeFilter}>
              <Select.Trigger className="select-trigger" aria-label="类型过滤">
                <Select.Value />
                <CaretDownIcon size={14} weight="bold" aria-hidden />
              </Select.Trigger>
              <Select.Portal>
                <Select.Content className="select-content" position="popper" sideOffset={4}>
                  <Select.Viewport>
                    <Select.Item value="all" className="select-item">
                      <Select.ItemText>全部类型</Select.ItemText>
                      <Select.ItemIndicator className="select-item-indicator">
                        <CheckIcon size={14} weight="bold" aria-hidden />
                      </Select.ItemIndicator>
                    </Select.Item>
                    {[...EVAL_SUITES.map((suite) => suite.label), "通用"].map((label) => (
                      <Select.Item key={label} value={label} className="select-item">
                        <Select.ItemText>{label}</Select.ItemText>
                        <Select.ItemIndicator className="select-item-indicator">
                          <CheckIcon size={14} weight="bold" aria-hidden />
                        </Select.ItemIndicator>
                      </Select.Item>
                    ))}
                  </Select.Viewport>
                </Select.Content>
              </Select.Portal>
            </Select.Root>
          </div>
          <div className="inline-field">
            <span className="field-label">状态</span>
            <Select.Root value={statusFilter} onValueChange={setStatusFilter}>
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
          </div>
        </div>
        {error && <p className="error">{error}</p>}
        <table>
          <thead>
            <tr>
              <th>ID</th><th>类型</th><th>场景</th><th>模型</th><th>状态</th><th>进度</th><th>开始</th><th>耗时</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((run) => {
              const cancellable = !isTerminal(run.status);
              const retryable = RETRYABLE_STATUSES.includes(run.status);
              const total = run.case_ids?.length ?? 0;
              const done = run.cases?.length ?? 0;
              return (
                <tr key={run.id}>
                  <td className="mono" title={run.id}>
                    <button className="link" onClick={() => openRun(run)}>{shortRunId(run.id)}</button>
                  </td>
                  <td>{typeLabel(run)}</td>
                  <td className="mono">{run.scenario_version}</td>
                  <td className="mono">{run.model ?? "—"}</td>
                  <td><StatusBadge status={run.status} /></td>
                  <td className="mono">{total ? `${done}/${total}` : "—"}</td>
                  <td className="mono">{formatTimestamp(run.created_at) ?? "—"}</td>
                  <td className="mono">{formatDuration(run.created_at, run.finished_at) ?? "—"}</td>
                  <td className="row-actions">
                    <button type="button" className="link" onClick={() => navigate(monitorPath(run))}>过程</button>
                    {(cancellable || retryable) ? (
                      <DropdownMenu.Root>
                        <DropdownMenu.Trigger asChild>
                          <button className="icon-btn" aria-label={`运行 ${run.id} 操作`}>
                            <DotsThreeIcon size={16} weight="bold" aria-hidden />
                          </button>
                        </DropdownMenu.Trigger>
                        <DropdownMenu.Portal>
                          <DropdownMenu.Content className="menu-content" align="end" sideOffset={4}>
                            {cancellable && (
                              <DropdownMenu.Item className="menu-item danger" onSelect={() => act(() => cancelRun(run.id, "web 控制台取消"))}>
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
                    ) : null}
                  </td>
                </tr>
              );
            })}
            {visible.length === 0 && (
              <tr><td colSpan={9} className="empty">暂无运行</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
