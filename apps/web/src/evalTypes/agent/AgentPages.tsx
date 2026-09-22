import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { OutcomeFlap } from "../../board/OutcomeFlap";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ArrowClockwiseIcon,
  CheckCircleIcon,
  FileTextIcon,
  ProhibitIcon,
  RobotIcon,
  WarningCircleIcon,
  XCircleIcon,
} from "@phosphor-icons/react";
import {
  cancelRun,
  createAgentTasksRun,
  dryRunAgentTasks,
  getAgentArtifactContent,
  getAgentCaseDetail,
  getAgentTasksOverview,
  getModels,
  getReport,
  getRun,
  getScoringPasses,
  retryRun,
  type AgentDryRunSummary,
  type AgentTasksOverview,
  type AgentCaseDetail,
  type ModelRecord,
  type RunRecord,
} from "../../api/client";
import { BatchMonitor } from "../../components/BatchMonitor";
import { MetricCards } from "../../components/MetricCards";
import { StatusBadge } from "../../components/StatusBadge";
import { statusLabel } from "../../components/statusMeta";
import { formatTimestamp, shortRunId } from "../../components/runFormat";
import Input from "@douyinfe/semi-ui/lib/es/input";
import Button from "@douyinfe/semi-ui/lib/es/button";
import { Board } from "../../board/Board";
import { StatusFlap } from "../../board/StatusFlap";
import { EmptyBoard } from "../../board/EmptyBoard";
import { FieldGrid, Field, IssueBar } from "../../board/FieldGrid";
import { suiteRoutes } from "../registry";

export const ROUTES = suiteRoutes("agent-tasks");

const MODE_LABELS: Record<string, string> = {
  "native-tool": "native-tool（规范工具调用）",
  "legacy-json": "legacy-json（JSON 动作协议）",
};

const TERMINATION_LABELS: Record<string, string> = {
  final_answer: "模型给出最终回答",
  max_steps: "步数预算耗尽",
  max_tool_calls: "工具次数预算耗尽",
  wall_time: "时长预算耗尽",
  token_limit: "token 预算超限",
  cost_limit: "费用预算超限",
  cancelled: "被取消",
  error: "执行错误",
  invalid_state: "异常状态",
};

const METRIC_STATUS_LABELS: Record<string, { label: string; tone: "success" | "error" | "warning" | "neutral" }> = {
  scored: { label: "已评分", tone: "success" },
  insufficient_evidence: { label: "证据不足", tone: "warning" },
  evaluator_error: { label: "评分器异常", tone: "error" },
  not_applicable: { label: "不适用", tone: "neutral" },
};

function Panel({ title, children, actions }: { title: string; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <section className="panel detail" aria-label={title}>
      <div className="panel-head">
        <h2>{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

function EmptyState({ text }: { text: string }) {
  return <p className="empty-state">{text}</p>;
}

/* ------------------------------------------------------------------ 操作页 */

export function AgentOperate() {
  const [overview, setOverview] = useState<AgentTasksOverview | null>(null);
  const [models, setModels] = useState<ModelRecord[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [scenario, setScenario] = useState("");
  const [modelId, setModelId] = useState("");
  const [mode, setMode] = useState<"native-tool" | "legacy-json">("native-tool");
  const [maxSteps, setMaxSteps] = useState("8");
  const [maxToolCalls, setMaxToolCalls] = useState("16");
  const [wallTimeSec, setWallTimeSec] = useState("120");
  const [dryRun, setDryRun] = useState<AgentDryRunSummary | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [o, m] = await Promise.all([getAgentTasksOverview(), getModels()]);
        if (cancelled) return;
        setOverview(o);
        setModels(m.items);
        if (o.items.length > 0 && !scenario) setScenario(o.items[0].scenario);
        const published = m.items.find((item) => item.lifecycle === "published");
        if (published) setModelId((current) => current || published.id);
      } catch (error) {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : String(error));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectedModel = useMemo(
    () => models?.find((item) => item.id === modelId) ?? null,
    [models, modelId],
  );
  // native-tool 需要模型声明 supports_tools；不满足时禁用并给出原因（不静默降级）
  const nativeUnsupported =
    mode === "native-tool" && selectedModel != null && selectedModel.supports_tools === false;
  const budgetInvalid = useMemo(() => {
    const steps = Number(maxSteps);
    const calls = Number(maxToolCalls);
    const wall = Number(wallTimeSec);
    if (!Number.isInteger(steps) || steps <= 0 || steps > 64) return "步数预算须为 1–64 的整数";
    if (!Number.isInteger(calls) || calls <= 0 || calls > 256) return "工具次数预算须为 1–256 的整数";
    if (!Number.isFinite(wall) || wall <= 0 || wall > 3600) return "时长预算须为 0–3600 秒";
    return null;
  }, [maxSteps, maxToolCalls, wallTimeSec]);
  const submitDisabled = loading || !scenario || !modelId || nativeUnsupported || budgetInvalid != null || submitting;

  const requestBody = useCallback(() => ({
    scenario,
    model: modelId,
    mode,
    budget: {
      max_steps: Number(maxSteps),
      max_tool_calls: Number(maxToolCalls),
      wall_time_sec: Number(wallTimeSec),
    },
  }), [scenario, modelId, mode, maxSteps, maxToolCalls, wallTimeSec]);

  const runPreflight = async () => {
    setFormError(null);
    setDryRun(null);
    try {
      setDryRun(await dryRunAgentTasks(requestBody()));
    } catch (error) {
      // 预检失败保留全部表单输入，就地显示结构化原因
      setFormError(error instanceof Error ? error.message : String(error));
    }
  };

  const submit = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      const run = await createAgentTasksRun(requestBody());
      window.location.assign(ROUTES.monitor([run.id]));
    } catch (error) {
      setFormError(error instanceof Error ? error.message : String(error));
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div className="page">
        <EmptyBoard reason="正在读取 Agent 任务数据集…" />
      </div>
    );
  }

  return (
    <div className="page operate">
      {/* 页面身份由顶栏（面包屑 + 激活页签）承担，页内不再重复标题 */}
      {loadError && (
        <div role="alert" className="error">
          <WarningCircleIcon size={16} weight="bold" aria-hidden />
          加载失败：{loadError}
        </div>
      )}
      <section className="panel" aria-label="任务与模型">
        <div className="panel-head">
          <h2>任务与模型</h2>
        </div>
        {overview?.items.length === 0 && (
          <EmptyBoard
            reason="还没有 Agent 任务数据集"
            next="先用 CLI 或 API 导入：agent-tasks import"
          />
        )}
        <FieldGrid>
          <Field label="任务数据集">
            <select value={scenario} onChange={(event) => setScenario(event.target.value)} aria-label="任务数据集">
              {(overview?.items ?? []).map((item) => (
                <option key={item.scenario} value={item.scenario}>
                  {item.scenario}（{item.cases} 题）
                </option>
              ))}
            </select>
          </Field>
          <Field label="已发布模型">
            <select value={modelId} onChange={(event) => setModelId(event.target.value)} aria-label="已发布模型">
              <option value="">选择模型…</option>
              {(models ?? []).map((item) => (
                <option key={item.id} value={item.id} disabled={item.lifecycle !== "published"}>
                  {item.id}
                  {item.lifecycle !== "published" ? "（未发布）" : ""}
                  {item.supports_tools === false ? "（不支持工具）" : ""}
                </option>
              ))}
            </select>
          </Field>
          <Field label="执行模式" hint="native-tool 走规范工具调用；legacy-json 走 JSON 动作协议">
            <select
              value={mode}
              onChange={(event) => setMode(event.target.value as "native-tool" | "legacy-json")}
              aria-label="执行模式"
            >
              <option value="native-tool">{MODE_LABELS["native-tool"]}</option>
              <option value="legacy-json">{MODE_LABELS["legacy-json"]}</option>
            </select>
          </Field>
        </FieldGrid>
        {nativeUnsupported && (
          <p className="hint warning" role="note">
            <ProhibitIcon size={14} weight="bold" aria-hidden />
            模型 {modelId} 声明不支持工具（supports_tools=false），native-tool 模式不可用；不会自动切换到 legacy-json。
          </p>
        )}
        <div className="section-head">
          <h3 className="embed-title">预算</h3>
        </div>
        <FieldGrid columns={3}>
          <Field label="最大步数">
            <Input value={maxSteps} onChange={(v) => setMaxSteps(v)} inputMode="numeric" aria-label="最大步数" />
          </Field>
          <Field label="最大工具次数">
            <Input value={maxToolCalls} onChange={(v) => setMaxToolCalls(v)} inputMode="numeric" aria-label="最大工具次数" />
          </Field>
          <Field label="时长（秒）">
            <Input value={wallTimeSec} onChange={(v) => setWallTimeSec(v)} inputMode="decimal" aria-label="时长（秒）" />
          </Field>
        </FieldGrid>
        {budgetInvalid && <p className="hint warning" role="alert">{budgetInvalid}</p>}
        {formError && (
          <div role="alert" className="error">
            <XCircleIcon size={16} weight="bold" aria-hidden />
            {formError}
          </div>
        )}
      </section>
      {dryRun && (
        <section className="panel" aria-label="预检摘要">
          <div className="panel-head">
            <h2>预检摘要</h2>
          </div>
          <dl className="kv">
            <div><dt>后端</dt><dd className="mono">{dryRun.backend}</dd></div>
            <div><dt>模式</dt><dd className="mono">{dryRun.mode}（{dryRun.prompt_version}）</dd></div>
            <div><dt>选中任务数</dt><dd className="mono">{dryRun.selected_cases}</dd></div>
            <div><dt>数据集</dt><dd className="mono">{dryRun.dataset}</dd></div>
          </dl>
        </section>
      )}
      {(overview?.items.length ?? 0) > 0 && (
        <section className="panel" aria-label="最近运行">
          <div className="panel-head">
            <h2>最近运行</h2>
          </div>
          <Board
            label="最近运行板面"
            head={
              <>
                <th className="w-[132px]">Run</th>
                <th className="w-[110px]">状态</th>
                <th className="w-[124px]">模式</th>
                <th>创建时间</th>
                <th className="board-actions w-[104px]">操作</th>
              </>
            }
          >
            {overview!.items.flatMap((item) => item.runs.slice(0, 5).map((run) => (
              <tr key={run.id}>
                <td className="data board-id" title={run.id}>{shortRunId(run.id)}</td>
                <td><StatusFlap status={run.status} /></td>
                <td className="data">{run.mode ?? "—"}</td>
                <td className="data">{formatTimestamp(run.created_at) ?? "—"}</td>
                <td className="board-actions">
                  <Link className="board-link" to={ROUTES.result(run.id)}>查看结果</Link>
                </td>
              </tr>
            )))}
            {overview!.items.every((item) => item.runs.length === 0) && (
              <tr>
                <td colSpan={5} style={{ height: "auto", padding: "16px" }}>
                  <EmptyBoard reason="这个数据集还没有运行记录" next="选好模型后点右下角「创建运行」" />
                </td>
              </tr>
            )}
          </Board>
        </section>
      )}
      <IssueBar note="预检只做静态检查（零模型调用）；创建运行会真实调用模型并产生费用">
        <Button onClick={runPreflight} disabled={submitDisabled}>预检</Button>
        <Button theme="solid" type="primary" onClick={submit} disabled={submitDisabled}>
          {submitting ? "提交中…" : "创建运行"}
        </Button>
      </IssueBar>
    </div>
  );
}

/* ------------------------------------------------------------------ 监控页 */

export function AgentMonitor() {
  const [params] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  return (
    <div className="page">
      <h1 className="page-title">Agent 运行监控</h1>
      {runIds.length === 0
        ? <EmptyState text="没有待监控的运行。" />
        : <BatchMonitor runIds={runIds} resultPath={ROUTES.result} />}
    </div>
  );
}

/* ------------------------------------------------------------------ 结果页 */

const MAX_EVENT_ROWS = 200;
const MAX_ARTIFACT_CHARS = 4000;

interface ScoreRow {
  case_id: string;
  metric_id: string;
  metric_status: string;
  passed: boolean | null;
  reason: string | null;
  value: number | null;
}

function MetricStatusBadge({ status }: { status: string }) {
  const meta = METRIC_STATUS_LABELS[status] ?? { label: status, tone: "neutral" as const };
  return <span className={`badge tone-${meta.tone}`}>{meta.label}</span>;
}

function ArtifactViewer({ runId, caseId, path }: { runId: string; caseId: string; path: string }) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open || content != null) return;
    let cancelled = false;
    getAgentArtifactContent(runId, caseId, path)
      .then((payload) => { if (!cancelled) setContent(payload.content); })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : String(err)); });
    return () => { cancelled = true; };
  }, [open, content, runId, caseId, path]);
  const truncated = (content?.length ?? 0) > MAX_ARTIFACT_CHARS;
  return (
    <div className="artifact-view">
      <button type="button" className="link" onClick={() => setOpen(!open)} aria-expanded={open}>
        <FileTextIcon size={14} weight="bold" aria-hidden /> {path}
      </button>
      {open && error && <p className="hint warning" role="alert">读取失败：{error}</p>}
      {open && content != null && (
        <pre className="code-block">
          {content.slice(0, MAX_ARTIFACT_CHARS)}
          {truncated && `\n… [已截断，完整内容共 ${content.length} 字符]`}
        </pre>
      )}
    </div>
  );
}

export function AgentResult() {
  const { runId = "" } = useParams();
  const navigate = useNavigate();
  const [run, setRun] = useState<RunRecord | null>(null);
  const [passes, setPasses] = useState<Array<{ id: string; summary: Record<string, any> }>>([]);
  const [selectedPass, setSelectedPass] = useState<string>("");
  // 历史 pass 的只读分数（null = 展示 current pass 的 run.scores）
  const [historicalScores, setHistoricalScores] = useState<ScoreRow[] | null>(null);
  const [caseDetail, setCaseDetail] = useState<AgentCaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // #18：迟到响应不得覆盖新选择——按请求序号丢弃过期结果
  const passRequestSeq = useRef(0);

  const onPassChange = async (passId: string) => {
    const requestSeq = ++passRequestSeq.current;
    setSelectedPass(passId);
    setHistoricalScores(null);
    if (!passId || !run || passId === run.current_scoring_pass_id) return;
    try {
      const report = await getReport(runId, passId);
      if (passRequestSeq.current !== requestSeq) return; // 已切换到其它批次
      setHistoricalScores((report.scores as any[] ?? [])
        .filter((score) => typeof score.metric_id === "string")
        .map((score) => ({
          case_id: score.case_id, metric_id: score.metric_id,
          metric_status: score.metric_status ?? "scored",
          passed: score.passed ?? null, reason: score.reason ?? null, value: score.value ?? null,
        })));
    } catch (err) {
      if (passRequestSeq.current !== requestSeq) return; // 已切换到其它批次：迟到的失败也不得覆盖
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const reload = useCallback(async () => {
    try {
      const [nextRun, passList] = await Promise.all([
        getRun(runId),
        getScoringPasses(runId),
      ]);
      setRun(nextRun);
      setPasses(passList.items);
      setSelectedPass((current) => current || nextRun.current_scoring_pass_id || passList.items.at(-1)?.id || "");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [runId]);

  useEffect(() => { void reload(); }, [reload]);

  // R3 #8：路由切换到另一个 Run（如重试进入子 Run）时重置样本下钻，
  // 避免 stale 的 caseDetail 与产物缓存跨 Run 复用。
  useEffect(() => { setCaseDetail(null); }, [runId]);

  const scores: ScoreRow[] = useMemo(() => {
    if (historicalScores != null) return historicalScores;
    const current = run?.scores ?? [];
    return current
      .filter((score: any) => typeof score.metric_id === "string")
      .map((score: any) => ({
        case_id: score.case_id, metric_id: score.metric_id,
        metric_status: score.metric_status ?? "scored",
        passed: score.passed ?? null, reason: score.reason ?? null, value: score.value ?? null,
      }));
  }, [run, historicalScores]);

  const active = run != null && !["completed", "failed", "cancelled", "unsupported", "profile_stale", "needs_review"].includes(run.status);
  const retryable = run != null && ["failed", "cancelled", "unsupported", "profile_stale", "needs_review"].includes(run.status);

  if (error) {
    return (
      <div className="page">
        <div role="alert" className="error">读取失败：{error}</div>
        <button type="button" className="link" onClick={() => void reload()}>重试读取</button>
      </div>
    );
  }
  if (!run) return <div className="page"><EmptyState text="加载中…" /></div>;

  const caseIds: string[] = run.case_ids ?? [];
  const onCaseSelect = async (caseId: string) => {
    if (!caseId) { setCaseDetail(null); return; }
    try { setCaseDetail(await getAgentCaseDetail(runId, caseId)); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  };

  return (
    <div className="page">
      <Panel
        title="运行概览"
        actions={
          <div className="actions">
            {active && (
              <button type="button" disabled={busy} onClick={async () => {
                setBusy(true);
                try { await cancelRun(runId, "操作员在结果页取消"); await reload(); }
                finally { setBusy(false); }
              }}>取消运行</button>
            )}
            {retryable && (
              <button type="button" disabled={busy} onClick={async () => {
                setBusy(true);
                try {
                  const child = await retryRun(runId);
                  navigate(ROUTES.result(child.id));
                } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
                finally { setBusy(false); }
              }}>
                <ArrowClockwiseIcon size={14} weight="bold" aria-hidden /> 重试（子 Run）
              </button>
            )}
          </div>
        }
      >
        <dl className="kv">
          <div><dt>Run</dt><dd className="mono">{run.id}</dd></div>
          <div><dt>状态</dt><dd><StatusBadge status={run.status} /></dd></div>
          <div><dt>场景</dt><dd>{run.scenario_version}</dd></div>
          <div><dt>模式</dt><dd>{(run.manifest?.agent_config?.mode as string) ?? "—"}</dd></div>
          <div><dt>终止</dt><dd>{TERMINATION_LABELS[run.status] ?? statusLabel(run.status)}</dd></div>
        </dl>
        {run.error?.message && (
          <div role="alert" className="error">运行错误：{run.error.message}</div>
        )}
      </Panel>

      {passes.length > 0 && (
        <Panel title="评分批次（历史不可变）">
          <label className="field">
            <span>查看批次</span>
            <select value={selectedPass} onChange={(event) => void onPassChange(event.target.value)}>
              {passes.map((passItem, index) => (
                <option key={passItem.id} value={passItem.id}>
                  #{index + 1} {passItem.id.slice(0, 12)}…
                  {passItem.id === run.current_scoring_pass_id ? "（当前）" : ""}
                </option>
              ))}
            </select>
          </label>
          <p className="hint">
            切换只读历史批次（当前显示：{historicalScores != null ? "历史批次" : "当前批次"}）；
            读取与切换不会重新评分或调用模型。
          </p>
        </Panel>
      )}

      <Panel title="多指标结果">
        {scores.length === 0
          ? <EmptyState text={active ? "运行进行中，还没有评分。" : "本运行没有多指标分数（可能是未评分的终态）。"} />
          : (
            <Board
              label="多指标结果板面"
              head={
                <>
                  <th className="w-[220px]">任务</th>
                  <th className="w-[180px]">指标</th>
                  <th className="w-[130px]">状态</th>
                  <th className="w-[120px]">判定</th>
                  <th>原因</th>
                </>
              }
            >
              {scores.map((score) => (
                <tr key={`${score.case_id}:${score.metric_id}`}>
                  <td className="data board-id" title={score.case_id}>{score.case_id}</td>
                  <td className="data" title={score.metric_id}>{score.metric_id}</td>
                  <td><MetricStatusBadge status={score.metric_status} /></td>
                  <td>
                    {score.passed === true && <OutcomeFlap label="通过" tone="success" />}
                    {score.passed === false && <OutcomeFlap label="未通过" tone="error" />}
                    {score.passed == null && <OutcomeFlap label="未判定" tone="neutral" />}
                  </td>
                  <td className="truncate" title={score.reason ?? undefined}>{score.reason ?? "—"}</td>
                </tr>
              ))}
            </Board>
          )}
      </Panel>

      <Panel title="样本下钻">
        <label className="field">
          <span>任务</span>
          <select defaultValue="" onChange={(event) => void onCaseSelect(event.target.value)}>
            <option value="">选择任务…</option>
            {caseIds.map((caseId) => <option key={caseId} value={caseId}>{caseId}</option>)}
          </select>
        </label>
        {!caseDetail && <EmptyState text="选择任务查看终止原因、事件轨迹与文件产物。" />}
        {caseDetail && caseDetail.pending && (
          <p className="hint">该任务尚未产生结果（运行进行中或未执行到该任务）。</p>
        )}
        {caseDetail && !caseDetail.pending && caseDetail.agent == null && (
          <p className="hint warning">该任务没有 Agent 执行记录（可能是未尝试或调用失败）。</p>
        )}
        {caseDetail && (
          <>
            <dl className="kv">
              <div><dt>终止原因</dt>
                <dd>{TERMINATION_LABELS[caseDetail.agent?.termination_reason ?? ""] ?? caseDetail.agent?.termination_reason ?? "—"}</dd></div>
              {caseDetail.agent?.termination_detail && (
                <div><dt>细节</dt><dd>{caseDetail.agent.termination_detail}</dd></div>
              )}
              <div><dt>步数 / 工具调用</dt>
                <dd>{caseDetail.agent?.steps ?? 0} / {caseDetail.agent?.tool_calls ?? 0}</dd></div>
              <div><dt>耗时</dt>
                <dd>{caseDetail.agent?.duration_ms != null ? `${Math.round(caseDetail.agent.duration_ms)} ms` : "—"}</dd></div>
              <div><dt>工作区清理</dt>
                <dd>
                  {caseDetail.cleanup?.status === "success"
                    ? "已回收"
                    : `清理失败（残留：${(caseDetail.cleanup?.residual ?? []).join("、") || "未知"}）`}
                </dd></div>
            </dl>
            {caseDetail.capture_errors.length > 0 && (
              <div role="alert" className="banner error">
                证据采集失败：{caseDetail.capture_errors.join("；")}
              </div>
            )}
            <h3>文件产物</h3>
            {caseDetail.artifacts.length === 0
              ? <EmptyState text="没有捕获到文件产物。" />
              : caseDetail.artifacts.map((artifact) =>
                artifact.available
                  // R3 #8：缓存键含 Run——重试进入子 Run 后不复用父 Run 的产物内容
                  ? <ArtifactViewer key={`${runId}:${caseDetail.case_id}:${artifact.path}`} runId={runId} caseId={caseDetail.case_id} path={artifact.path} />
                  : <p key={`${runId}:${caseDetail.case_id}:${artifact.path}`} className="hint warning">产物 {artifact.path} 不可用（采集失败或损坏）</p>,
              )}
            <h3>事件轨迹{caseDetail.events.length >= MAX_EVENT_ROWS ? `（前 ${MAX_EVENT_ROWS} 条）` : ""}</h3>
            <ol className="timeline">
              {caseDetail.events.slice(0, MAX_EVENT_ROWS).map((event, index) => (
                <li key={index} className="timeline-item">
                  <span className="mono">{event.type}</span>
                  {event.tool != null && <span> · {String(event.tool)}</span>}
                  {event.reason != null && <span> · {String(event.reason)}</span>}
                </li>
              ))}
            </ol>
          </>
        )}
      </Panel>
    </div>
  );
}

/* ------------------------------------------------------------------ 并列阅读 */

export function AgentCompare() {
  const [params] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean).slice(0, 2);
  const [runs, setRuns] = useState<Array<RunRecord | null>>([null, null]);

  useEffect(() => {
    let cancelled = false;
    Promise.all(runIds.map((id) => getRun(id).catch(() => null)))
      .then((loaded) => { if (!cancelled) setRuns(loaded); });
    return () => { cancelled = true; };
  }, [runIds.join(",")]);

  return (
    <div className="page">
      <h1 className="page-title">同任务结果并列阅读</h1>
      <p className="hint">
        <RobotIcon size={14} weight="bold" aria-hidden />
        两个运行按任务逐指标并列展示，供人工对照；这不是正式的可比性结论（正式比较由后续阶段的比较策略提供）。
      </p>
      {runIds.length < 2 && <EmptyState text="请从运行列表选择两个同场景的运行进行并列阅读。" />}
      <div className="compare-grid">
        {runs.map((run, index) => (
          <Panel key={runIds[index] ?? index} title={run ? `运行 ${runIds[index].slice(0, 12)}…` : "运行"}>
            {!run
              ? <EmptyState text="运行不存在或不可读。" />
              : (
                <>
                  <dl className="kv">
                    <div><dt>状态</dt><dd><StatusBadge status={run.status} /></dd></div>
                    <div><dt>模型</dt><dd>{(run.manifest?.resource_snapshots?.model_profile as any)?.id ?? "—"}</dd></div>
                  </dl>
                  <Board
                    label="数据板面"
                    head={<>
                      <th>任务</th><th>指标</th><th>判定</th>
                    </>}
                  >

                    

                      {(run.scores ?? [])
                        .filter((score: any) => typeof score.metric_id === "string")
                        .map((score: any, i: number) => (
                          <tr key={i}>
                            <td className="mono">{score.case_id}</td>
                            <td className="mono">{score.metric_id}</td>
                            <td>{score.passed === true ? "通过" : score.passed === false ? "未通过" : "未判定"}</td>
                          </tr>
                        ))}

                  </Board>
                </>
              )}
          </Panel>
        ))}
      </div>
    </div>
  );
}
