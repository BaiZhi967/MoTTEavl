import { Fragment, useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import * as Select from "@radix-ui/react-select";
import { CaretDownIcon, CaretRightIcon, CheckIcon, DotsThreeIcon } from "@phosphor-icons/react";
import { cancelRun, getRuns, retryRun, modelLabel, type RunRecord } from "../api/client";
import { STATUS_ORDER, statusLabel } from "../components/statusMeta";
import { formatClock, formatDuration, formatTimestamp, shortRunId } from "../components/runFormat";
import { RunProgress } from "../components/RunProgress";
import { isTerminal } from "../hooks/useRunEvents";
import { Board } from "../board/Board";
import { StatusFlap } from "../board/StatusFlap";
import { IdentityPlate } from "../board/IdentityPlate";
import { EmptyBoard } from "../board/EmptyBoard";
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
  /** 就地展开的证据行（球鞋档案墙的 raise）：把这一行抽出来看铭牌，不跳走。 */
  const [opened, setOpened] = useState<string[]>([]);

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
  const liveCount = visible.filter((run) => !isTerminal(run.status)).length;
  const toggleOpen = (id: string) =>
    setOpened((current) => (current.includes(id) ? current.filter((x) => x !== id) : [...current, id]));

  const monitorPath = (run: RunRecord) => {
    const suite = suiteForRun(run);
    return suite ? suiteRoutes(suite.id).monitor([run.id]) : "/runs/" + run.id + "/monitor";
  };

  /* 非终态优先看过程，终态直达结果 */
  const openRun = (run: RunRecord) => {
    if (!isTerminal(run.status)) {
      navigate(monitorPath(run));
      return;
    }
    const suite = suiteForRun(run);
    navigate(suite ? suiteRoutes(suite.id).result(run.id) : "/runs/" + run.id + "/result");
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
    <div className="page fill">
      <section className="panel detail" aria-label="运行总览">
        <div className="panel-head">
          <p className="panel-summary">
            共 <b className="mono">{visible.length}</b> 条
            {liveCount > 0 && (
              <span className="panel-summary-live" title="当前筛选结果中尚未进入终态的 Run">
                <span className="led led-live" aria-hidden />
                进行中 <b className="mono">{liveCount}</b>
              </span>
            )}
          </p>
          <div className="panel-head-actions">
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
        </div>
        {error && <p className="error">{error}</p>}
        <Board
          label="运行板面"
          head={
            <>
              <th className="w-[28px]"><span className="sr-only">展开</span></th>
              <th className="w-[160px]">ID</th>
              <th className="w-[124px]">类型</th>
              <th>场景</th>
              <th>模型</th>
              <th className="w-[96px]">状态</th>
              <th className="num w-[110px]">进度</th>
              <th className="w-[96px]">开始</th>
              <th className="num w-[64px]">耗时</th>
              <th className="board-actions w-[88px]">操作</th>
            </>
          }
        >
          {visible.map((run) => {
            const cancellable = !isTerminal(run.status);
            const retryable = RETRYABLE_STATUSES.includes(run.status);
            const total = run.case_ids?.length ?? 0;
            const done = run.cases?.length ?? 0;
            const expanded = opened.includes(run.id);
            return (
              <Fragment key={run.id}>
                <tr>
                  <td>
                    <button
                      type="button"
                      className="board-open"
                      aria-label={expanded ? "收起 " + run.id + " 的证据" : "展开 " + run.id + " 的证据"}
                      onClick={() => toggleOpen(run.id)}
                    >
                      <CaretRightIcon
                        size={12}
                        weight="bold"
                        aria-hidden
                        style={{ transform: expanded ? "rotate(90deg)" : undefined }}
                      />
                    </button>
                  </td>
                  <td className="data board-id">
                    <button className="link run-id" title={run.id} onClick={() => openRun(run)}>
                      {shortRunId(run.id)}
                    </button>
                  </td>
                  <td className="truncate" title={typeLabel(run)}>{typeLabel(run)}</td>
                  <td className="data truncate" title={run.scenario_version}>{run.scenario_version}</td>
                  <td className="data truncate" title={run.model ?? modelLabel(run) ?? undefined}>
                    {run.model ?? modelLabel(run) ?? "—"}
                  </td>
                  <td><StatusFlap status={run.status} /></td>
                  <td className="num">
                    {total ? <RunProgress done={done} total={total} /> : "—"}
                  </td>
                  <td className="data">{formatTimestamp(run.created_at) ?? "—"}</td>
                  <td className="num">{formatDuration(run.created_at, run.finished_at) ?? (isTerminal(run.status) ? "—" : "进行中")}</td>
                  <td className="board-actions">
                    <button type="button" className="link row-action" onClick={() => navigate(monitorPath(run))}>过程</button>
                    {(cancellable || retryable) ? (
                      <DropdownMenu.Root>
                        <DropdownMenu.Trigger asChild>
                          <button className="icon-btn" aria-label={"运行 " + run.id + " 操作"}>
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
                {expanded && (
                  <tr className="board-detail">
                    <td colSpan={10}>
                      <IdentityPlate
                        id={run.id}
                        copyValue={run.id}
                        rows={[
                          { label: "套件", value: typeLabel(run) },
                          { label: "场景", value: run.scenario_version },
                          { label: "模型", value: run.model ?? modelLabel(run) ?? "未知" },
                          { label: "创建", value: formatClock(run.created_at) ?? "—" },
                          { label: "完成", value: formatClock(run.finished_at) ?? "未结束" },
                          { label: "题数", value: total ? done + " / " + total : "未知" },
                          { label: "评分", value: run.scores?.length ? run.scores.length + " 项" : "缺工件" },
                        ]}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
          {visible.length === 0 && (
            <tr>
              <td colSpan={10} style={{ height: "auto", padding: "16px" }}>
                <EmptyBoard
                  reason={runs.length === 0 ? "还没有任何运行记录" : "当前筛选条件下没有运行"}
                  next={runs.length === 0 ? "去某个套件的「操作」页发起第一次跑测" : "把类型或状态过滤改回「全部」"}
                />
              </td>
            </tr>
          )}
        </Board>
      </section>
    </div>
  );
}
