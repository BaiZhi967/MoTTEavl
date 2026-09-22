import { Fragment, useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { cancelRun, getRun, modelLabel, retryRun, type RunRecord } from "../api/client";
import { RunProgress } from "./RunProgress";
import { RunErrorBanner } from "./RunErrorBanner";
import { Board } from "../board/Board";
import { StatusFlap } from "../board/StatusFlap";
import { EmptyBoard } from "../board/EmptyBoard";
import { countDone, isTerminal, useRunEvents } from "../hooks/useRunEvents";

const RETRYABLE = ["failed", "cancelled", "unsupported", "profile_stale", "needs_review"];
const COLUMNS = 6;

function BatchRow({ runId, resultPath, renderDetail }: {
  runId: string;
  resultPath: (runId: string) => string;
  renderDetail?: (runId: string) => ReactNode;
}) {
  const [activeRunId, setActiveRunId] = useState(runId);
  const [run, setRun] = useState<RunRecord | null>(null);
  const { events, status } = useRunEvents(activeRunId);
  const [expanded, setExpanded] = useState(false);
  const [action, setAction] = useState<"cancel" | "retry" | null>(null);
  const [actionError, setActionError] = useState("");
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

  const retry = async () => {
    setAction("retry");
    setActionError("");
    try {
      const child = await retryRun(activeRunId);
      setRun(child);
      setActiveRunId(child.id);
      setExpanded(false);
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setAction(null);
    }
  };

  const cancel = async () => {
    setAction("cancel");
    setActionError("");
    try {
      setRun(await cancelRun(activeRunId, "web 控制台取消"));
    } catch (caught) {
      setActionError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setAction(null);
    }
  };

  /* 一个运行 = 板面上的一行；展开的内容就地插在它下面（球鞋档案墙那条 raise）。 */
  const detailRow = (content: ReactNode) => (
    <tr className="board-detail">
      <td colSpan={COLUMNS}>{content}</td>
    </tr>
  );

  return (
    <Fragment>
      <tr>
        <td className="board-caret">
          <button
            type="button"
            className="board-open"
            aria-expanded={expanded}
            aria-label={expanded ? "收起 " + activeRunId + " 的详情" : "展开 " + activeRunId + " 的详情"}
            onClick={() => setExpanded((open) => !open)}
          >
            <span aria-hidden>{expanded ? "▾" : "▸"}</span>
          </button>
        </td>
        <td className="data board-id">
          <button
            type="button"
            className="link run-id"
            title={activeRunId}
            aria-expanded={expanded}
            onClick={() => setExpanded((open) => !open)}
          >
            {activeRunId}
          </button>
        </td>
        <td className="data truncate" title={modelLabel(run) ?? undefined}>
          <span className="mono">{modelLabel(run) ?? "—"}</span>
        </td>
        <td><StatusFlap status={current} /></td>
        <td className="num">
          <RunProgress done={done} total={total} />
        </td>
        <td className="board-actions">
          {terminal ? (
            <>
              <Link className="board-link" to={resultPath(activeRunId)}>结果</Link>
              {RETRYABLE.includes(current) && (
                <button type="button" className="link row-action" disabled={action !== null} onClick={() => void retry()}>
                  {action === "retry" ? "重试中…" : "重试"}
                </button>
              )}
            </>
          ) : (
            <button type="button" className="link row-action" disabled={action !== null} onClick={() => void cancel()}>
              {action === "cancel" ? "取消中…" : "取消"}
            </button>
          )}
        </td>
      </tr>
      {actionError && detailRow(<p className="error" role="alert">{actionError}</p>)}
      {current === "queued" && detailRow(
        <p className="hint">等待 Worker 领取；若长期排队，请在服务端启动 Worker（make worker）。</p>,
      )}
      {terminal && run?.error && detailRow(<RunErrorBanner run={run} monitor />)}
      {expanded && renderDetail ? detailRow(renderDetail(activeRunId)) : null}
    </Fragment>
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
        <p className="panel-summary">
          本批次 <b className="mono">{runIds.length}</b> 个运行
          {comparePath ? <Link className="board-link" to={comparePath}>查看对比结果</Link> : null}
        </p>
      </div>
      <Board
        label="批次板面"
        head={
          <>
            <th className="w-[28px]"><span className="sr-only">展开</span></th>
            <th className="w-[260px]">Run</th>
            <th className="w-[160px]">模型</th>
            <th className="w-[104px]">状态</th>
            <th className="num w-[140px]">进度</th>
            <th className="board-actions w-[150px]">操作</th>
          </>
        }
      >
        {runIds.map((runId) => (
          <BatchRow key={runId} runId={runId} resultPath={resultPath} renderDetail={renderDetail} />
        ))}
        {runIds.length === 0 && (
          <tr>
            <td colSpan={COLUMNS} style={{ height: "auto", padding: "16px" }}>
              <EmptyBoard
                reason="未指定运行"
                next={
                  <>
                    从各类型操作页发起跑测后自动进入，或到
                    <Link className="link" to="/runs">运行总览</Link>
                    查看历史运行。
                  </>
                }
              />
            </td>
          </tr>
        )}
      </Board>
    </section>
  );
}
