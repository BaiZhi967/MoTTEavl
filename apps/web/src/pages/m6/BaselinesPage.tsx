import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Board } from "../../board/Board";
import { EmptyBoard } from "../../board/EmptyBoard";
import { StatusFlap } from "../../board/StatusFlap";
import { Link } from "react-router-dom";
import { ArrowClockwiseIcon, PushPinIcon } from "@phosphor-icons/react";
import {
  createBaseline,
  describeApiError,
  getBaselines,
  getBaseline,
  getDefaultBaseline,
  setDefaultBaseline,
  type BaselineSnapshotView,
  type DefaultBaselinePointerView,
} from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import {
  LoadFailure,
  UNKNOWN_TEXT,
} from "../../components/capability";

/**
 * Baseline 页（M6）：不可变快照清单 + 详情 + 创建 + 默认指针（scope → snapshot，
 * CAS expected_current）。写入后不可变：同 id 异内容 409；指针切换 409 CAS_CONFLICT
 * 时就地显示错误、保留表单。服务端未提供指针历史端点：时间线只记录本会话的操作。
 */

const DEFAULT_POLICY = '{\n  "allowed_factors": ["model"]\n}';

const DEFAULT_ENTRIES = '[\n  { "run_id": "run-xxxx", "scoring_pass_id": "pass-yyyy" }\n]';

function parseJson(text: string, what: string): { ok: true; value: unknown } | { ok: false; message: string } {
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (error) {
    return { ok: false, message: what + " JSON 无法解析：" + (error instanceof Error ? error.message : String(error)) };
  }
}

function BaselineDetail({
  baseline,
  detailError,
}: {
  baseline: BaselineSnapshotView;
  detailError?: string | null;
}) {
  const metrics = Object.entries(baseline.metrics ?? {});
  return (
    <section className="panel wide-panel" aria-label="Baseline 详情">
      <div className="panel-head">
        <h2>Baseline 详情</h2>
        <div className="panel-head-actions">
          <StatusBadge status={baseline.eligibility || "formal"} />
        </div>
      </div>
      {detailError && <p className="error" role="alert" data-testid="baseline-detail-error">{detailError}</p>}
      <dl className="kv" data-testid="baseline-detail-kv">
        <dt>baseline_id</dt>
        <dd className="mono">{baseline.baseline_id}</dd>
        <dt>资格</dt>
        <dd>
          <StatusBadge status={baseline.eligibility || "formal"} />
          {baseline.eligibility === "diagnostic" ? "（证据不完整：只能诊断比较，不能作正式 Gate baseline）" : "（可作正式 Gate baseline）"}
        </dd>
        <dt>比较政策 hash</dt>
        <dd className="mono">{baseline.comparison_policy_hash ?? UNKNOWN_TEXT}</dd>
        <dt>创建者 / 理由</dt>
        <dd>{(baseline.created_by ?? UNKNOWN_TEXT) + " · " + (baseline.reason ?? UNKNOWN_TEXT)}</dd>
        <dt>创建时间</dt>
        <dd className="mono">{baseline.created_at ?? UNKNOWN_TEXT}</dd>
        <dt>来源说明</dt>
        <dd>{baseline.source_note ?? "—"}</dd>
      </dl>

      <h3 className="embed-title">固定 entries</h3>
      <Board
        testId="baseline-entries-table"
        label="基线固定条目板面"
        head={
          <>
            <th className="w-[220px]">cell_key</th>
            <th className="w-[300px]">Run</th>
            <th>ScoringPass</th>
          </>
        }
      >
          {(baseline.entries ?? []).map((entry, index) => (
            <tr key={index}>
              <td className="mono">{entry.cell_key ?? "（单 Run）"}</td>
              <td className="mono nowrap">
                {entry.ref?.run_id
                  ? <Link className="link" to={`/runs/${entry.ref.run_id}/monitor`}>{entry.ref.run_id}</Link>
                  : UNKNOWN_TEXT}
              </td>
              <td className="mono nowrap">{entry.ref?.scoring_pass_id ?? UNKNOWN_TEXT}</td>
            </tr>
          ))}
          {(baseline.entries ?? []).length === 0 && (
            <tr><td colSpan={3} className="empty">服务端未返回 entries</td></tr>
          )}
      </Board>

      <h3 className="embed-title">指标</h3>
      {metrics.length === 0 ? (
        <p className="empty">暂无指标</p>
      ) : (
        <dl className="kv" data-testid="baseline-metrics">
          {metrics.map(([metric, value]) => (
            <Fragment key={metric}>
              <dt className="mono">{metric}</dt>
              <dd className="mono">{value ?? UNKNOWN_TEXT}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      <p className="hint">null = 资格不足（缺样本 / 未评分），保持未知，不折算成 0。</p>
    </section>
  );
}

function DefaultPointerCard({ baselines }: { baselines: BaselineSnapshotView[] }) {
  const [scope, setScope] = useState("");
  const [pointer, setPointer] = useState<DefaultBaselinePointerView | null>(null);
  const [pointerLoadedScope, setPointerLoadedScope] = useState<string | null>(null);
  const [baselineId, setBaselineId] = useState("");
  const [expectedCurrent, setExpectedCurrent] = useState("");
  const [updatedBy, setUpdatedBy] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  /** 本会话的指针操作记录（服务端未提供历史端点，不伪造全量历史）。 */
  const [history, setHistory] = useState<DefaultBaselinePointerView[]>([]);
  const seq = useRef(0);

  const onLoadPointer = useCallback(async (scopeValue: string) => {
    const target = scopeValue.trim();
    if (target === "") return;
    const current = ++seq.current;
    setBusy(true);
    setError("");
    try {
      const payload = await getDefaultBaseline(target);
      if (seq.current !== current) return; // 迟到响应不得覆盖新 scope
      setPointer(payload.pointer ?? null);
      setPointerLoadedScope(target);
    } catch (caught) {
      if (seq.current !== current) return;
      setPointer(null);
      setError("读取默认指针失败：" + describeApiError(caught).message);
    } finally {
      if (seq.current === current) setBusy(false);
    }
  }, []);

  const formValid = scope.trim() !== "" && baselineId.trim() !== "" && updatedBy.trim() !== "" && reason.trim() !== "";
  const blockedReason = !formValid
    ? "scope、baseline_id、操作者与理由均必填"
    : pointerLoadedScope === scope.trim() && pointer !== null && expectedCurrent.trim() === "" && pointer.baseline_id !== baselineId.trim()
      ? "该 scope 已有指针：填写 expected_current（CAS）才能切换"
      : null;

  const onSwitch = async (event: FormEvent) => {
    event.preventDefault();
    if (blockedReason !== null || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const next = await setDefaultBaseline({
        scope: scope.trim(),
        baseline_id: baselineId.trim(),
        expected_current: expectedCurrent.trim() === "" ? null : expectedCurrent.trim(),
        updated_by: updatedBy.trim(),
        reason: reason.trim(),
      });
      setPointer(next);
      setPointerLoadedScope(scope.trim());
      setHistory((items) => [...items, next]);
      setNotice(`指针已切换（position ${next.position ?? UNKNOWN_TEXT}）：审计记录操作者与理由，历史指针不被覆盖。`);
      setExpectedCurrent(next.baseline_id);
    } catch (caught) {
      // 409 CAS_CONFLICT 等失败：就地显示错误码 + 消息，表单内容原样保留。
      const described = describeApiError(caught);
      setError(`切换默认指针失败${described.code ? "（" + described.code + "）" : ""}：${described.message}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="panel form-panel" aria-label="默认 Baseline 指针">
      <h2>默认指针</h2>
      <div className="inline-field">
        <label className="field-label" htmlFor="baseline-pointer-scope">scope</label>
        <input
          id="baseline-pointer-scope"
          className="control mono"
          value={scope}
          placeholder="如 release"
          onChange={(change) => setScope(change.target.value)}
        />
        <button type="button" disabled={busy || scope.trim() === ""} onClick={() => void onLoadPointer(scope.trim())}>
          {busy ? "读取中…" : "读取指针"}
        </button>
      </div>

      {pointerLoadedScope !== null && (
        pointer
          ? (
            <dl className="kv" data-testid="baseline-pointer-current">
              <dt>当前 baseline</dt>
              <dd className="mono">{pointer.baseline_id}</dd>
              <dt>更新者 / 理由</dt>
              <dd>{pointer.updated_by + " · " + pointer.reason}</dd>
              <dt>position</dt>
              <dd className="mono">{pointer.position ?? UNKNOWN_TEXT}</dd>
              <dt>比较政策 hash</dt>
              <dd className="mono">{pointer.comparison_policy_hash ?? UNKNOWN_TEXT}</dd>
              <dt>更新时间</dt>
              <dd className="mono">{pointer.updated_at ?? UNKNOWN_TEXT}</dd>
            </dl>
          )
          : <p className="hint" data-testid="baseline-pointer-empty">该 scope 尚未设置默认 baseline（首次设置不需要 expected_current）。</p>
      )}

      <form onSubmit={onSwitch}>
        <h3 className="embed-title">切换指针</h3>
        <label htmlFor="baseline-pointer-target">
          目标 baseline_id
          <input
            id="baseline-pointer-target"
            className="mono"
            list="baseline-id-options"
            value={baselineId}
            placeholder="目标 baseline"
            onChange={(change) => setBaselineId(change.target.value)}
          />
        </label>
        <datalist id="baseline-id-options">
          {baselines.map((item) => <option key={item.baseline_id} value={item.baseline_id} />)}
        </datalist>
        <label htmlFor="baseline-pointer-expected">
          expected_current（可选，CAS）
          <input
            id="baseline-pointer-expected"
            className="mono"
            value={expectedCurrent}
            placeholder="期望的当前 baseline_id"
            onChange={(change) => setExpectedCurrent(change.target.value)}
          />
        </label>
        <label htmlFor="baseline-pointer-operator">
          操作者（必填）
          <input
            id="baseline-pointer-operator"
            value={updatedBy}
            placeholder="操作者身份"
            onChange={(change) => setUpdatedBy(change.target.value)}
          />
        </label>
        <label htmlFor="baseline-pointer-reason">
          切换理由（必填）
          <input
            id="baseline-pointer-reason"
            value={reason}
            placeholder="切换原因（进入审计）"
            onChange={(change) => setReason(change.target.value)}
          />
        </label>
        <div className="actions">
          <button type="submit" className="primary" disabled={blockedReason !== null || busy} title={blockedReason ?? undefined}
            data-testid="baseline-pointer-submit">
            {busy ? "切换中…" : "切换默认指针"}
          </button>
        </div>
        {blockedReason && <p className="hint fail" data-testid="baseline-pointer-reason">{blockedReason}</p>}
        {error && <p className="error" role="alert" data-testid="baseline-pointer-error">{error}</p>}
        {notice && <p className="hint pass" data-testid="baseline-pointer-notice">{notice}</p>}
      </form>

      {history.length > 0 && (
        <>
          <h3 className="embed-title">本会话指针操作</h3>
          <ul className="timeline">
            {history.map((item, index) => (
              <li key={index} className="event event-tone-info">
                <span className="seq">#{item.position ?? index + 1}</span>
                <span className="time">{item.updated_at ?? ""}</span>
                <span className="type">{item.baseline_id}</span>
                <span className="detail">{item.updated_by} · {item.reason}</span>
              </li>
            ))}
          </ul>
          <p className="hint">服务端未提供指针历史端点：上表只记录本会话的切换操作，不是全量历史。</p>
        </>
      )}
    </section>
  );
}

function CreateBaselineCard({ onCreated }: { onCreated: (baseline: BaselineSnapshotView) => void }) {
  const [baselineId, setBaselineId] = useState("");
  const [entriesText, setEntriesText] = useState(DEFAULT_ENTRIES);
  const [policyText, setPolicyText] = useState(DEFAULT_POLICY);
  const [createdBy, setCreatedBy] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const entries = parseJson(entriesText, "entries");
    if (!entries.ok) { setError(entries.message); return; }
    if (!Array.isArray(entries.value) || entries.value.length === 0) {
      setError("entries 必须是非空数组：[{ cell_key?, run_id, scoring_pass_id }]");
      return;
    }
    const policy = parseJson(policyText, "policy");
    if (!policy.ok) { setError(policy.message); return; }
    if (policy.value === null || typeof policy.value !== "object" || Array.isArray(policy.value)) {
      setError("policy 必须是 JSON 对象");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const stored = await createBaseline({
        baseline_id: baselineId.trim(),
        entries: entries.value as Array<{ cell_key?: string | null; run_id: string; scoring_pass_id: string }>,
        policy: policy.value as Record<string, unknown>,
        created_by: createdBy.trim() || "web",
        reason: reason.trim(),
      });
      setNotice(`已创建 ${stored.baseline_id}（${stored.eligibility ?? UNKNOWN_TEXT}）。Baseline 写入后不可变：同 id 异内容会被 409 拒绝。`);
      onCreated(stored);
    } catch (caught) {
      const described = describeApiError(caught);
      setError(`创建失败${described.code ? "（" + described.code + "）" : ""}：${described.message}`);
    } finally {
      setBusy(false);
    }
  };

  const formValid = baselineId.trim() !== "" && reason.trim() !== "";

  return (
    <section className="panel form-panel" aria-label="创建 Baseline">
      <form onSubmit={onSubmit}>
        <h2>创建 Baseline</h2>
        <label htmlFor="baseline-create-id">
          baseline_id
          <input
            id="baseline-create-id"
            className="mono"
            value={baselineId}
            placeholder="如 blt-2026-09"
            onChange={(change) => setBaselineId(change.target.value)}
          />
        </label>
        <label htmlFor="baseline-create-entries">
          entries（JSON 数组：固定 RunReportRef 集合）
          <textarea
            id="baseline-create-entries"
            className="mono"
            rows={6}
            value={entriesText}
            onChange={(change) => setEntriesText(change.target.value)}
          />
        </label>
        <label htmlFor="baseline-create-policy">
          比较政策（默认只允许 model 变化）
          <textarea
            id="baseline-create-policy"
            className="mono"
            rows={4}
            value={policyText}
            onChange={(change) => setPolicyText(change.target.value)}
          />
        </label>
        <label htmlFor="baseline-create-by">
          创建者
          <input
            id="baseline-create-by"
            value={createdBy}
            placeholder="默认 web"
            onChange={(change) => setCreatedBy(change.target.value)}
          />
        </label>
        <label htmlFor="baseline-create-reason">
          理由（必填）
          <input
            id="baseline-create-reason"
            value={reason}
            placeholder="为什么固定这组报告"
            onChange={(change) => setReason(change.target.value)}
          />
        </label>
        <div className="actions">
          <button type="submit" className="primary" disabled={!formValid || busy} data-testid="baseline-create-submit">
            {busy ? "创建中…" : "创建 Baseline"}
          </button>
        </div>
        {error && <p className="error" role="alert" data-testid="baseline-create-error">{error}</p>}
        {notice && <p className="hint pass" data-testid="baseline-create-notice">{notice}</p>}
      </form>
    </section>
  );
}

export function BaselinesPage() {
  const [baselines, setBaselines] = useState<BaselineSnapshotView[]>([]);
  const [listError, setListError] = useState<unknown>(null);
  const [loaded, setLoaded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  /** 选中详情：先显示列表行数据，点击后按 id 拉取完整快照。 */
  const [detail, setDetail] = useState<BaselineSnapshotView | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const detailSeq = useRef(0);

  const refresh = useCallback(async () => {
    setLoaded(false);
    try {
      const payload = await getBaselines();
      setBaselines(payload.items);
      setListError(null);
    } catch (caught) {
      setBaselines([]);
      setListError(caught);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const selectedRow = useMemo(
    () => baselines.find((item) => item.baseline_id === selected) ?? null,
    [baselines, selected],
  );

  const selectBaseline = useCallback(async (baselineId: string) => {
    setSelected(baselineId);
    const seq = ++detailSeq.current;
    setDetail(null);
    setDetailError(null);
    try {
      const snapshot = await getBaseline(baselineId);
      if (detailSeq.current !== seq) return; // 已切换到其它 baseline：迟到快照丢弃
      setDetail(snapshot);
    } catch (caught) {
      if (detailSeq.current !== seq) return;
      setDetailError("读取 Baseline 详情失败：" + describeApiError(caught).message);
    }
  }, []);

  const shown = detail ?? selectedRow;

  return (
    <div className="page">
      <aside className="panel list-panel" aria-label="Baseline 清单">
        <div className="panel-head">
          <h2>Baseline</h2>
          <div className="panel-head-actions">
            <button type="button" className="icon-btn" aria-label="刷新清单" onClick={() => void refresh()}>
              <ArrowClockwiseIcon size={14} weight="bold" aria-hidden />
            </button>
          </div>
        </div>
        {listError !== null && <LoadFailure error={listError} what="Baseline 清单" testId="baseline-list-unavailable" />}
        {!loaded && baselines.length === 0 ? (
          <p className="empty">加载中</p>
        ) : baselines.length === 0 ? (
          <p className="empty" data-testid="baseline-list-empty">暂无 Baseline</p>
        ) : (
          <ul className="provider-list">
            {baselines.map((item) => (
              <li key={item.baseline_id}>
                <button
                  type="button"
                  className="provider-item"
                  data-state={selected === item.baseline_id ? "active" : undefined}
                  onClick={() => void selectBaseline(item.baseline_id)}
                >
                  <PushPinIcon size={16} weight="bold" aria-hidden />
                  <span className="provider-item-name mono">{item.baseline_id}</span>
                  <StatusBadge status={item.eligibility || "formal"} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      {shown
        ? <BaselineDetail baseline={shown} detailError={detailError} />
        : <section className="panel wide-panel" aria-label="Baseline 详情">
            <h2>Baseline 详情</h2>
            <p className="empty">选择左侧（或先创建）一个 Baseline。</p>
          </section>}

      <CreateBaselineCard
        onCreated={(baseline) => {
          setBaselines((items) => {
            const index = items.findIndex((item) => item.baseline_id === baseline.baseline_id);
            if (index === -1) return [...items, baseline];
            const next = [...items];
            next[index] = baseline;
            return next;
          });
          setSelected(baseline.baseline_id);
          setDetail(baseline);
        }}
      />

      <DefaultPointerCard baselines={baselines} />
    </div>
  );
}
