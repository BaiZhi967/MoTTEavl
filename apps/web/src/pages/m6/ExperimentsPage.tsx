import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Board } from "../../board/Board";
import { EmptyBoard } from "../../board/EmptyBoard";
import { StatusFlap } from "../../board/StatusFlap";
import { Link } from "react-router-dom";
import { ArrowClockwiseIcon, FlaskIcon } from "@phosphor-icons/react";
import {
  cancelExperiment,
  createExperiment,
  describeApiError,
  getExperiment,
  previewExperiment,
  retryExperimentCell,
  type ExperimentCellView,
  type ExperimentPreviewView,
  type ExperimentStatusView,
} from "../../api/client";
import { StatusBadge, statusLabel } from "../../components/StatusBadge";
import { UNKNOWN_TEXT } from "../../components/capability";

/**
 * 实验页（M6）：spec 预览（零创建）→ 创建（202）→ 状态视图（cells 表）→ 取消 / cell 重试。
 * 服务端只提供按 id 读取实验（无全量列表端点）：清单是本会话载入 / 创建过的实验，
 * 不伪造「全部实验」视图。分配状态徽章来自 STATUS_META（scope experiment）。
 */

/** cell_id 截短展示（sha256 前缀 + 12 位 hex），完整值放 title。 */
function shortCellId(cellId: string): string {
  if (cellId.length <= "sha256:".length + 12) return cellId;
  return cellId.slice(0, "sha256:".length + 12) + "…";
}

/** 因素分配的紧凑展示：factor=value 以 · 连接；空表是全默认 cell。 */
function factorAssignmentText(cell: ExperimentCellView): string {
  const entries = Object.entries(cell.factor_assignment ?? {}).filter(([, value]) => value != null);
  if (entries.length === 0) return "（全默认）";
  return entries.map(([factor, value]) => `${factor}=${String(value)}`).join(" · ");
}

/**
 * 实验清单徽章：从 cells 统计派生（用既有运行状态，不新增运行状态）。
 * 只反映分配口径：allocated 不代表 Run 已终态，failed 的 cell 使整体标失败。
 */
export function experimentBadgeStatus(status: ExperimentStatusView): string {
  const progress = status.progress ?? {};
  if ((progress.failed ?? 0) > 0) return "failed";
  if ((progress.pending ?? 0) > 0 || (progress.allocating ?? 0) > 0) return "running";
  return "completed";
}

export function experimentKey(status: Pick<ExperimentStatusView, "experiment_id" | "version">): string {
  return `${status.experiment_id}@${status.version}`;
}

function parseSpec(text: string): { ok: true; value: Record<string, unknown> } | { ok: false; message: string } {
  try {
    const parsed = JSON.parse(text);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { ok: false, message: "spec 必须是 JSON 对象" };
    }
    return { ok: true, value: parsed as Record<string, unknown> };
  } catch (error) {
    return { ok: false, message: "spec JSON 无法解析：" + (error instanceof Error ? error.message : String(error)) };
  }
}

const DEFAULT_SPEC = `{
  "experiment_id": "exp-demo",
  "version": "1",
  "task_ref": { "suite": "direct-llm", "scenario_version": "direct-llm@1" },
  "factors": { "model_profile": ["model-a", "model-b"] },
  "repeats": 1,
  "budget_policy": { "max_total_calls": 200, "max_total_tokens": 200000 }
}`;

function PreviewResult({ preview }: { preview: ExperimentPreviewView }) {
  return (
    <div data-testid="experiment-preview-result">
      <dl className="kv">
        <dt>cell 数</dt>
        <dd className="mono">{preview.cell_count}</dd>
        <dt>最大潜在调用</dt>
        <dd className="mono">{preview.max_potential_calls ?? UNKNOWN_TEXT}</dd>
        <dt>调用上限（预算）</dt>
        <dd className="mono">
          {preview.budget?.max_total_calls ?? UNKNOWN_TEXT}
          {" 次调用 / "}
          {preview.budget?.max_total_tokens ?? UNKNOWN_TEXT}
          {" tokens"}
        </dd>
        <dt>已知费用</dt>
        <dd className="mono">{preview.budget?.cost_known ?? "unknown_until_run"}（运行前保持未知，不填 0）</dd>
      </dl>
      {preview.violations.length > 0 ? (
        <ul className="failure-list" data-testid="experiment-preview-violations">
          {preview.violations.map((violation, index) => (
            <li key={violation.code + "-" + index} className="hint fail">
              <span className="mono">{violation.code}</span>
              {"："}{violation.message}
            </li>
          ))}
        </ul>
      ) : (
        <p className="hint pass" data-testid="experiment-preview-clean">护栏检查通过：无违规。</p>
      )}
    </div>
  );
}

function ExperimentStatusPanel({
  status,
  onRefresh,
  onCancelled,
}: {
  status: ExperimentStatusView;
  onRefresh: () => void;
  onCancelled: (next: ExperimentStatusView) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState<"cancel" | string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const progress = status.progress ?? {};

  const onCancel = async () => {
    setBusy("cancel");
    setError("");
    try {
      const result = await cancelExperiment(status.experiment_id, {
        version: status.version,
        reason: "experiment cancelled",
      });
      setNotice(
        `已取消：${(result.cancelled_cells ?? []).length} 个 cell、`
        + `${(result.cancelled_runs ?? []).length} 个自有 Run（其它实验 / 游离 Run 不受影响）。`,
      );
      onCancelled({ ...result });
      setConfirming(false);
    } catch (caught) {
      setError("取消实验失败：" + describeApiError(caught).message);
    } finally {
      setBusy(null);
    }
  };

  const onRetryCell = async (cell: ExperimentCellView) => {
    setBusy(cell.cell_id);
    setError("");
    setNotice("");
    try {
      const result = await retryExperimentCell(cell.cell_id, "explicit retry");
      setNotice(
        `已创建 superseding 子 Run ${result.run_id}（parent ${result.parent_run_id ?? UNKNOWN_TEXT}）；`
        + "原 Run 结果保留，不挑最好结果。",
      );
      onRefresh();
    } catch (caught) {
      setError("重试 cell 失败：" + describeApiError(caught).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="panel wide-panel" aria-label="实验状态">
      <div className="panel-head">
        <h2>实验状态</h2>
        <div className="panel-head-actions">
          <span className="hint"><StatusBadge status={experimentBadgeStatus(status)} /></span>
          <button type="button" className="icon-btn" aria-label="刷新实验状态" onClick={onRefresh}>
            <ArrowClockwiseIcon size={14} weight="bold" aria-hidden />
          </button>
        </div>
      </div>
      <dl className="kv">
        <dt>实验</dt>
        <dd className="mono">{experimentKey(status)}</dd>
        <dt>cell 数</dt>
        <dd className="mono">{status.cell_count}</dd>
        <dt>分配进度</dt>
        <dd className="mono">
          {["pending", "allocating", "allocated", "failed", "cancelled"]
            .map((key) => `${statusLabel(key)} ${progress[key] ?? 0}`)
            .join(" / ")}
        </dd>
      </dl>
      <p className="hint">徽章与进度是分配口径：已分配不代表 Run 已到终态，逐 Run 状态以链接跳转为准。</p>
      {notice && <p className="hint" data-testid="experiment-action-notice">{notice}</p>}
      {error && <p className="error" role="alert" data-testid="experiment-action-error">{error}</p>}

      <div className="actions">
        {confirming ? (
          <>
            <button
              type="button"
              className="link danger"
              disabled={busy !== null}
              onClick={() => void onCancel()}
              data-testid="experiment-cancel-confirm"
            >
              {busy === "cancel" ? "取消中…" : "确认取消"}
            </button>
            <button type="button" className="link" disabled={busy !== null} onClick={() => setConfirming(false)}>
              放弃
            </button>
            <span className="hint fail">取消只作用于本实验：pending / allocating cell 落已取消，自有 Run 走取消；已完成的评分批次保持不变。</span>
          </>
        ) : (
          <button type="button" className="link danger" disabled={busy !== null} onClick={() => setConfirming(true)}>
            取消实验
          </button>
        )}
      </div>

      <Board
        testId="experiment-cells-table"
        label="实验单元板面"
        head={
          <>
            <th className="w-[150px]">cell</th>
            <th>因素分配</th>
            <th className="num w-[80px]">repeat</th>
            <th className="w-[280px]">Run</th>
            <th className="w-[130px]">分配状态</th>
            <th className="board-actions w-[110px]">操作</th>
          </>
        }
      >
          {status.cells.map((cell) => (
            <tr key={cell.cell_id}>
              <td className="mono nowrap" title={cell.cell_id}>{shortCellId(cell.cell_id)}</td>
              <td className="mono" title={cell.failure_reason ?? undefined}>{factorAssignmentText(cell)}</td>
              <td className="mono">{cell.repeat_index}</td>
              <td className="mono nowrap">
                {cell.run_id
                  ? <Link className="link" to={`/runs/${cell.run_id}/monitor`}>{cell.run_id}</Link>
                  : <span className="hint">{UNKNOWN_TEXT}</span>}
              </td>
              <td><StatusFlap status={cell.allocation_status} /></td>
              <td className="row-actions">
                {cell.allocation_status === "allocated" && cell.run_id && (
                  <button
                    type="button"
                    className="link"
                    disabled={busy !== null}
                    title="重试会创建 superseding 子 Run（原结果保留），并产生真实调用与费用"
                    onClick={() => void onRetryCell(cell)}
                  >
                    {busy === cell.cell_id ? "重试中…" : "重试"}
                  </button>
                )}
                {(cell.superseding_run_ids ?? []).length > 0 && (
                  <span className="hint mono" title={(cell.superseding_run_ids ?? []).join(", ")}>
                    {" "}+{(cell.superseding_run_ids ?? []).length} 重试
                  </span>
                )}
              </td>
            </tr>
          ))}
          {status.cells.length === 0 && <tr><td colSpan={6} className="empty">暂无 cell</td></tr>}
      </Board>
    </section>
  );
}

export function ExperimentsPage() {
  const [known, setKnown] = useState<ExperimentStatusView[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [loadId, setLoadId] = useState("");
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(false);

  const [specText, setSpecText] = useState(DEFAULT_SPEC);
  const [preview, setPreview] = useState<ExperimentPreviewView | null>(null);
  const [previewFor, setPreviewFor] = useState<string | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [createBusy, setCreateBusy] = useState(false);
  const [createError, setCreateError] = useState("");
  const [createNotice, setCreateNotice] = useState("");

  /** 迟到响应丢弃：载入 / 预览各自带请求序号。 */
  const loadSeq = useRef(0);
  const previewSeq = useRef(0);

  const upsert = useCallback((status: ExperimentStatusView) => {
    const key = experimentKey(status);
    setKnown((items) => {
      const index = items.findIndex((item) => experimentKey(item) === key);
      if (index === -1) return [...items, status];
      const next = [...items];
      next[index] = status;
      return next;
    });
    setSelected(key);
  }, []);

  const current = useMemo(
    () => known.find((item) => experimentKey(item) === selected) ?? null,
    [known, selected],
  );

  const refreshCurrent = useCallback(async () => {
    if (!current) return;
    const seq = ++loadSeq.current;
    try {
      const status = await getExperiment(current.experiment_id, current.version);
      if (loadSeq.current !== seq) return;
      upsert(status);
    } catch (caught) {
      if (loadSeq.current !== seq) return;
      setLoadError("刷新实验失败：" + describeApiError(caught).message);
    }
  }, [current, upsert]);

  const onLoad = async (event: FormEvent) => {
    event.preventDefault();
    const raw = loadId.trim();
    if (raw === "") return;
    const atIndex = raw.lastIndexOf("@");
    const experimentId = atIndex > 0 ? raw.slice(0, atIndex) : raw;
    const version = atIndex > 0 ? raw.slice(atIndex + 1) : undefined;
    const seq = ++loadSeq.current;
    setLoading(true);
    setLoadError("");
    try {
      const status = await getExperiment(experimentId, version || undefined);
      if (loadSeq.current !== seq) return;
      upsert(status);
      setLoadId("");
    } catch (caught) {
      if (loadSeq.current !== seq) return;
      setLoadError("载入实验失败：" + describeApiError(caught).message);
    } finally {
      if (loadSeq.current === seq) setLoading(false);
    }
  };

  const parsed = useMemo(() => parseSpec(specText), [specText]);
  const previewFresh = preview !== null && previewFor === specText;
  const violations = previewFresh ? preview.violations : [];

  const createBlockedReason = !parsed.ok
    ? parsed.message
    : !previewFresh
      ? "必须先预览（只读、零创建、零调用）"
      : violations.length > 0
        ? `预览存在 ${violations.length} 项违规：超限整体拒绝，不会先排队一部分`
        : null;
  const canCreate = createBlockedReason === null && !createBusy && !previewBusy;

  const onPreview = async () => {
    if (!parsed.ok) {
      setPreviewError(parsed.message);
      return;
    }
    const seq = ++previewSeq.current;
    setPreviewBusy(true);
    setPreviewError("");
    try {
      const result = await previewExperiment(parsed.value);
      if (previewSeq.current !== seq) return;
      setPreview(result);
      setPreviewFor(specText);
    } catch (caught) {
      if (previewSeq.current !== seq) return;
      setPreview(null);
      setPreviewFor(null);
      setPreviewError("预览失败：" + describeApiError(caught).message);
    } finally {
      if (previewSeq.current === seq) setPreviewBusy(false);
    }
  };

  const onCreate = async (event: FormEvent) => {
    event.preventDefault();
    if (!canCreate || !parsed.ok) return;
    setCreateBusy(true);
    setCreateError("");
    setCreateNotice("");
    try {
      const outcome = await createExperiment(parsed.value);
      upsert(outcome);
      setCreateNotice(
        `创建已受理（202）：${outcome.created === false ? "复用既有 spec" : "发布新 spec"}，`
        + `分配 ${outcome.allocated ?? 0} 个 cell、跳过 ${outcome.skipped_existing ?? 0} 个既有 cell。`,
      );
    } catch (caught) {
      // 创建失败保留 spec 与预览内容，照实显示服务端错误码 + 消息。
      setCreateError("创建失败：" + describeApiError(caught).message);
    } finally {
      setCreateBusy(false);
    }
  };

  useEffect(() => {
    if (previewFor !== null && previewFor !== specText) {
      // spec 已改动：旧预览结论不再可信，创建入口回到「必须先预览」。
      setCreateNotice("");
    }
  }, [specText, previewFor]);

  return (
    <div className="page">
      <aside className="panel list-panel" aria-label="实验清单">
        <div className="panel-head">
          <h2>实验</h2>
        </div>
        <form onSubmit={onLoad}>
          <label htmlFor="experiment-load-id">
            按 ID 载入
            <input
              id="experiment-load-id"
              className="mono"
              value={loadId}
              placeholder="experiment_id 或 experiment_id@version"
              onChange={(change) => setLoadId(change.target.value)}
            />
          </label>
          <div className="actions">
            <button type="submit" className="primary" disabled={loading || loadId.trim() === ""}>
              {loading ? "载入中…" : "载入"}
            </button>
          </div>
        </form>
        {loadError && <p className="error" role="alert" data-testid="experiment-load-error">{loadError}</p>}
        {known.length === 0 ? (
          <>
            <p className="empty">暂无实验（本会话）</p>
            <p className="hint">服务端未提供实验全量列表端点：载入或创建后在此显示。</p>
          </>
        ) : (
          <ul className="provider-list">
            {known.map((item) => {
              const key = experimentKey(item);
              return (
                <li key={key}>
                  <button
                    type="button"
                    className="provider-item"
                    data-state={selected === key ? "active" : undefined}
                    onClick={() => setSelected(key)}
                  >
                    <FlaskIcon size={16} weight="bold" aria-hidden />
                    <span className="provider-item-name mono">{key}</span>
                    <StatusBadge status={experimentBadgeStatus(item)} />
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </aside>

      <section className="panel form-panel" aria-label="实验预览与创建">
        <form onSubmit={onCreate}>
          <h2>预览与创建</h2>
          <label htmlFor="experiment-spec">
            ExperimentSpec（JSON）
            <textarea
              id="experiment-spec"
              className="mono"
              rows={12}
              value={specText}
              onChange={(change) => setSpecText(change.target.value)}
            />
          </label>
          <div className="actions">
            <button
              type="button"
              disabled={previewBusy || createBusy || !parsed.ok}
              onClick={() => void onPreview()}
              data-testid="experiment-preview-button"
            >
              {previewBusy ? "预览中…" : "预览（零创建）"}
            </button>
            <button
              type="submit"
              className="primary"
              disabled={!canCreate}
              title={createBlockedReason ?? undefined}
              data-testid="experiment-create-button"
            >
              {createBusy ? "创建中…" : "创建实验"}
            </button>
          </div>
          {createBlockedReason && (
            <p className="hint fail" data-testid="experiment-create-reason">{createBlockedReason}</p>
          )}
          {previewError && <p className="error" role="alert" data-testid="experiment-preview-error">{previewError}</p>}
          {createError && <p className="error" role="alert" data-testid="experiment-create-error">{createError}</p>}
          {createNotice && <p className="hint pass" data-testid="experiment-create-notice">{createNotice}</p>}
          {previewFresh && preview && <PreviewResult preview={preview} />}
        </form>
      </section>

      {current
        ? <ExperimentStatusPanel
            key={experimentKey(current)}
            status={current}
            onRefresh={() => void refreshCurrent()}
            onCancelled={(next) => upsert(next)}
          />
        : <section className="panel wide-panel" aria-label="实验状态">
            <h2>实验状态</h2>
            <p className="empty">载入或创建一个实验后显示 cell 明细。</p>
          </section>}
    </div>
  );
}
