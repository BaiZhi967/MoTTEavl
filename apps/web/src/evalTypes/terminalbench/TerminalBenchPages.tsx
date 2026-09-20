/**
 * Terminal-Bench（Harbor）五页 + Trial 钻取（M3-T09）。
 *
 * 语义约束（需求第 6/7 节，前端只呈现、不替服务端猜）：
 * - reward=0 是**有效失败**；缺失 reward / Verifier 协议错误 / Verifier 异常都与 0 分不同，
 *   分别呈现为「未知」与对应 Verifier 状态，绝不填 0、也不当成通过。
 * - 质量分母是有效 Trial，覆盖分母是计划 Trial；两者分开显示。
 * - 成本与模型身份不可观察时显示「未知」；未知量不参与求值，每成功成本在无成功 Trial 时「不适用」。
 * - 切 Task/Trial 一律重置下钻视图（按 run+Trial+工件身份 key），晚到的旧响应被丢弃。
 */
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ArrowLeftIcon,
  ArrowsClockwiseIcon,
  CaretDownIcon,
  CaretRightIcon,
  MagnifyingGlassIcon,
} from "@phosphor-icons/react";
import {
  compareRuns,
  createTerminalBenchRun,
  evaluateRunGate,
  getRun,
  getRuns,
  getRunTaskTrials,
  getRunTasks,
  getRunTrial,
  getTerminalBenchOverview,
  getTerminalBenchPreflight,
  getTerminalBenchTasks,
  modelLabel,
  type ComparisonReportView,
  type GateResultView,
  type RunRecord,
  type TerminalBenchArtifact,
  type TerminalBenchOverview,
  type TerminalBenchPreflightReport,
  type TerminalBenchTask,
  type TerminalBenchTaskRow,
  type TerminalBenchTrialDetail,
  type TerminalBenchTrialRow,
} from "../../api/client";
import { MetricCards } from "../../components/MetricCards";
import { StatusBadge } from "../../components/StatusBadge";
import { suiteRoutes } from "../registry";

export const ROUTES = suiteRoutes("terminal-bench");
/** 任务清单页的路径段是 tasks（suiteRoutes 的 cases 段不适用于本套件）。 */
export const TASKS_ROUTE = "/terminal-bench/tasks";

type Tone = "success" | "error" | "neutral";

/**
 * 判定词汇与语气（DESIGN.md 第 4 节）：disposition 与 Verifier 状态各自成词，
 * reward 缺失 ≠ reward=0。语气只用 success / error / neutral 三档。
 */
export const OUTCOME_LABELS: Record<string, { label: string; tone: Tone }> = {
  // disposition
  succeeded: { label: "成功", tone: "success" },
  failed: { label: "失败", tone: "error" },
  not_attempted: { label: "未尝试", tone: "neutral" },
  cancelled: { label: "已取消", tone: "neutral" },
  indeterminate: { label: "不确定", tone: "neutral" },
  pending: { label: "待产出", tone: "neutral" },
  // Verifier 观测
  scored: { label: "已评分", tone: "success" },
  missing_verifier_evidence: { label: "缺评分证据", tone: "neutral" },
  verifier_protocol_error: { label: "Verifier 协议错误", tone: "error" },
  verifier_error: { label: "Verifier 异常", tone: "error" },
};

/** 覆盖项与工件完整度词汇：只描述证据可得性，不表达质量结论。 */
export const COVERAGE_STATE_LABELS: Record<string, { label: string; tone: Tone }> = {
  complete: { label: "完整", tone: "success" },
  truncated: { label: "已截断", tone: "neutral" },
  partial: { label: "部分", tone: "neutral" },
  unavailable: { label: "不可用", tone: "neutral" },
  missing: { label: "缺工件", tone: "neutral" },
  empty: { label: "空", tone: "neutral" },
};

const COVERAGE_ITEM_LABELS: Record<string, string> = {
  instruction: "任务指令",
  config: "实际配置",
  terminal: "终端文本",
  trajectory: "Agent 轨迹",
  workspace: "Workspace 工件",
  verifier_output: "Verifier 输出",
  reward: "Reward",
  usage: "用量与成本",
};

/** Harbor Agent Profile：服务端只登记已验证组合，这里不发明新组合。 */
export const AGENT_PROFILES = [
  { agent_id: "oracle", agent_version: "1.0.0", label: "oracle@1.0.0（参考解，已登记）" },
] as const;

const AGGREGATION_LABELS: Record<string, string> = {
  "first-trial": "first-trial（首个有效 Trial，事前固定）",
  "mean-success": "mean-success（有效 Trial 成功率 ≥ 0.5）",
};

const TERMINATION_REASON_LABELS: Record<string, string> = {
  completed: "正常完成",
  completed_without_reward: "完成但没有 reward",
  verifier_evidence_missing: "缺少 Verifier 证据",
  cancelled: "已取消",
  cancelled_before_collection: "收集前已取消",
  no_result_recorded: "没有结果记录",
  environment_error: "环境阶段错误",
  agent_error: "Agent 阶段错误",
  verifier_error: "Verifier 阶段错误",
  runner_error: "Runner 错误",
};

const PHASE_LABELS: Record<string, string> = {
  environment: "环境阶段",
  agent: "Agent 阶段",
  verifier: "Verifier 阶段",
  setup: "准备阶段",
};

/** Agent / Verifier / 环境时长分别保留（不合成单一延迟）。 */
const TIMING_FIELDS: Array<{ key: string; label: string }> = [
  { key: "environment_setup_sec", label: "环境准备（environment_setup）" },
  { key: "agent_setup_sec", label: "Agent 准备（agent_setup）" },
  { key: "agent_execution_sec", label: "Agent 执行（agent_execution）" },
  { key: "verifier_sec", label: "Verifier（verifier）" },
  { key: "total_sec", label: "合计（total）" },
];

const USAGE_COVERAGE_LABELS: Record<string, string> = {
  observed: "已观测（成本与 token 都有）",
  tokens_only: "仅 token（成本未知）",
  unavailable: "未观测（成本与 token 都未知）",
};

const COMPARE_POLICY = {
  metric: "valid_trial_pass_rate",
  op: "gte",
  threshold: 0.5,
  required_coverage: 1.0,
  require_cost_known: false,
  require_comparable: true,
} as const;

// ------------------------------------------------------------------ 取值助手

/** 身份戳状态：值只在自己声明的身份下可读，切换身份后旧值立即失效（不串证据）。 */
interface Identified<T> {
  identity: string;
  value: T;
}

function valueFor<T>(state: Identified<T> | null, identity: string): T | null {
  return state !== null && state.identity === identity ? state.value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** 未知一律「未知」，绝不填 0。 */
function formatRate(value: number | null | undefined): string {
  const number = asNumber(value);
  return number === null ? "未知" : `${(number * 100).toFixed(1)}%`;
}

function formatReward(value: number | null | undefined): string {
  const number = asNumber(value);
  return number === null ? "未知" : String(number);
}

function formatCost(value: number | null | undefined): string {
  const number = asNumber(value);
  return number === null ? "未知" : `$${number.toFixed(6)}`;
}

function formatSeconds(value: unknown): string {
  const number = asNumber(value);
  return number === null ? "未知" : `${number.toFixed(3)} s`;
}

function formatCount(value: unknown): string {
  const number = asNumber(value);
  return number === null ? "未知" : String(number);
}

function formatBytes(value: number | null | undefined): string {
  const number = asNumber(value);
  if (number === null) return "未知";
  if (number < 1024) return `${number} B`;
  if (number < 1024 * 1024) return `${(number / 1024).toFixed(1)} KiB`;
  return `${(number / (1024 * 1024)).toFixed(1)} MiB`;
}

function describeCheckValue(value: unknown): string {
  if (value === null || value === undefined) return "未知";
  if (typeof value === "boolean") return value ? "通过" : "未通过";
  if (typeof value === "number" || typeof value === "string") return String(value);
  const items = Array.isArray(value) ? value.map((item) => describeCheckValue(item)) : [];
  const text = Array.isArray(value) ? (items.length === 0 ? "无" : items.join("、")) : JSON.stringify(value);
  return text.length > 200 ? `${text.slice(0, 200)}…` : text;
}

function describeError(error: unknown): string | null {
  if (error === null || error === undefined) return null;
  if (typeof error !== "object") return String(error);
  const record = error as Record<string, unknown>;
  const code = typeof record.code === "string" ? record.code : null;
  const message = typeof record.message === "string" ? record.message : null;
  if (code || message) return [code, message].filter(Boolean).join("：");
  const text = JSON.stringify(error);
  return text === undefined ? String(error) : text;
}

function matchesTask(task: TerminalBenchTask, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [task.task_key, task.normalized_relative_path, task.display_name ?? ""]
    .some((field) => field.toLowerCase().includes(needle));
}

/** 覆盖率摘要：没有任何登记时是「未知」，不是「缺失 0 项」。 */
function coverageSummary(coverage: Record<string, any> | undefined): string {
  if (!coverage) return "未知";
  const missing = Array.isArray(coverage.missing) ? coverage.missing.length : null;
  const partial = Array.isArray(coverage.partial) ? coverage.partial.length : null;
  if (missing === null && partial === null) return "未知";
  return `${missing ?? 0}${partial ? ` / 部分 ${partial}` : ""}`;
}

export function isTerminalBenchRun(run: RunRecord): boolean {
  return run.scenario_version?.startsWith("terminal-bench") === true
    || run.manifest?.execution?.backend_id === "harbor-external";
}

/** Trial 层展示判定：scored 但 reward 缺失仍是「未知」，不冒充 0 分。 */
export function rewardVerdict(detail: TerminalBenchTrialDetail): string {
  const status = detail.verifier_observation?.status ?? "";
  const reward = asNumber(detail.verifier_observation?.rewards?.reward);
  if (status === "scored" && reward !== null) {
    return reward > 0 ? "通过（有效成功）" : "有效失败（reward=0，不是缺证据）";
  }
  if (status === "scored") return "已评分但没有 canonical reward 维度（未知，不填 0）";
  const meta = OUTCOME_LABELS[status];
  return `无质量判定（${meta?.label ?? (status || "未知")}），不计入通过率分母`;
}

function StateBadge({
  value,
  table = OUTCOME_LABELS,
}: {
  value: string | null | undefined;
  table?: Record<string, { label: string; tone: Tone }>;
}) {
  const key = value ?? "";
  const meta = table[key] ?? { label: key || "未知", tone: "neutral" as Tone };
  return <span className={`status-badge status-tone-${meta.tone}`}>{meta.label}</span>;
}

function artifactState(artifact: TerminalBenchArtifact): string {
  if (artifact.truncated) return "truncated";
  if (artifact.complete) return "complete";
  return "unavailable";
}

function Panel({ title, actions, children }: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
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

function BackLink() {
  const navigate = useNavigate();
  return (
    <button type="button" className="link" onClick={() => navigate(ROUTES.operate)}>
      <ArrowLeftIcon size={14} weight="bold" aria-hidden /> 返回操作页
    </button>
  );
}

// ------------------------------------------------------------------ 操作页

export function TerminalBenchOperate() {
  const navigate = useNavigate();
  const [overview, setOverview] = useState<TerminalBenchOverview | null>(null);
  const [tasks, setTasks] = useState<TerminalBenchTask[]>([]);
  const [loadError, setLoadError] = useState("");
  const [query, setQuery] = useState("");
  const [profileIndex, setProfileIndex] = useState(0);
  const [model, setModel] = useState("");
  const [nTrials, setNTrials] = useState("1");
  const [timeoutSec, setTimeoutSec] = useState("");
  const [jobTimeoutSec, setJobTimeoutSec] = useState("");
  const [aggregation, setAggregation] = useState<"first-trial" | "mean-success">("first-trial");
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [preflight, setPreflight] = useState<TerminalBenchPreflightReport | null>(null);
  const [preflightError, setPreflightError] = useState("");
  const [runError, setRunError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [overviewPayload, taskPayload] = await Promise.all([
          getTerminalBenchOverview(),
          getTerminalBenchTasks(),
        ]);
        if (!alive) return;
        setOverview(overviewPayload);
        setTasks(taskPayload.items);
      } catch (error) {
        if (alive) setLoadError(error instanceof Error ? error.message : String(error));
      }
    })();
    return () => { alive = false; };
  }, []);

  const profile = AGENT_PROFILES[profileIndex];
  const filteredTasks = useMemo(() => tasks.filter((task) => matchesTask(task, query)), [tasks, query]);
  const preset = overview?.items[0] ?? null;
  const nTrialsNumber = Number(nTrials);
  const nTrialsInvalid = !Number.isInteger(nTrialsNumber) || nTrialsNumber < 1 || nTrialsNumber > 10
    ? "重复次数（n_trials）须为 1–10 的整数"
    : null;
  const timeoutsInvalid = [timeoutSec, jobTimeoutSec].some((value) => (
    value !== "" && (!Number.isInteger(Number(value)) || Number(value) <= 0)
  ))
    ? "超时须为正整数秒（留空用服务端默认值）"
    : null;
  const inputInvalid = nTrialsInvalid ?? timeoutsInvalid;
  const preflightOk = preflight?.ok === true;
  const createDisabled = submitting || inputInvalid !== null || !preflightOk;

  const submitPreflight = (event: FormEvent) => {
    event.preventDefault();
    setPreflightError("");
    setPreflight(null);
    if (inputInvalid !== null) {
      setPreflightError(inputInvalid);
      return;
    }
    getTerminalBenchPreflight({
      model,
      n_trials: nTrialsNumber,
      task_keys: selectedKeys.length > 0 ? selectedKeys : undefined,
    })
      .then(setPreflight)
      .catch((error) => setPreflightError(error instanceof Error ? error.message : String(error)));
  };

  const submitRun = (event: FormEvent) => {
    event.preventDefault();
    setRunError("");
    if (!preflightOk || inputInvalid !== null) return;
    setSubmitting(true);
    createTerminalBenchRun({
      model,
      agent_id: profile.agent_id,
      agent_version: profile.agent_version,
      n_trials: nTrialsNumber,
      ...(selectedKeys.length > 0 ? { task_keys: selectedKeys } : {}),
      ...(timeoutSec !== "" ? { timeout_sec: Number(timeoutSec) } : {}),
      ...(jobTimeoutSec !== "" ? { job_timeout_sec: Number(jobTimeoutSec) } : {}),
      aggregation,
    })
      .then((run) => navigate(ROUTES.monitor([run.id])))
      .catch((error) => {
        setRunError(error instanceof Error ? error.message : String(error));
        setSubmitting(false);
      });
  };

  const toggleTask = (taskKey: string) => {
    setSelectedKeys((current) => (
      current.includes(taskKey) ? current.filter((key) => key !== taskKey) : [...current, taskKey]
    ));
    // 选择变了，旧预检结论作废（不拿旧 Profile 的结论放行新请求）。
    setPreflight(null);
  };

  return (
    <div className="page">
      <Panel title="Terminal-Bench（Harbor）" actions={<Link className="link" to={TASKS_ROUTE}>任务清单</Link>}>
        {loadError && <p className="error">{loadError}</p>}
        <dl className="kv">
          <div>
            <dt>数据集版本</dt>
            <dd className="mono" data-testid="tb-dataset-revision">
              {preset?.dataset_revision ?? "未准备（先用 CLI/API 准备任务集）"}
            </dd>
          </div>
          <div><dt>任务数</dt><dd className="mono">{preset ? formatCount(preset.tasks) : "未知"}</dd></div>
          <div>
            <dt>Runner</dt>
            <dd className="mono" data-testid="tb-runner">
              {overview
                ? `${overview.runner.adapter_id} / harbor ${overview.runner.harbor_version}；${overview.runner.connected ? "已连接" : "未连接"}`
                : "未知"}
            </dd>
          </div>
        </dl>
        <p className="hint">
          一个 Run = 一个 Harbor Job，Job 内有多个 Task × n_trials 个计划 Trial；
          一次预检只做静态检查（零模型调用、零任务启动）。
        </p>
      </Panel>

      <div className="operate-grid">
        <div className="operate-card">
          <p className="embed-title">任务筛选</p>
          <div className="inline-field">
            <span className="field-label">
              <MagnifyingGlassIcon size={14} weight="bold" aria-hidden /> 搜索
            </span>
            <input
              id="tb-task-search"
              className="control"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="按 task_key / 相对路径 / 显示名过滤"
              aria-label="任务筛选"
            />
          </div>
          <p className="hint" data-testid="tb-task-count">
            {filteredTasks.length} / {tasks.length} 个 Task；已选 {selectedKeys.length} 个
            {selectedKeys.length === 0 ? "（不选=整份数据集）" : ""}
          </p>
          <ul className="tb-tree" aria-label="任务清单" data-testid="tb-task-list">
            {filteredTasks.map((task) => (
              <li key={task.task_key} className="model-picker-item" data-enabled={selectedKeys.includes(task.task_key)}>
                <input
                  type="checkbox"
                  checked={selectedKeys.includes(task.task_key)}
                  onChange={() => toggleTask(task.task_key)}
                  aria-label={`选择任务 ${task.normalized_relative_path}`}
                />
                <span className="mono nowrap">{task.normalized_relative_path}</span>
                <span className="hint mono">{task.task_key.slice(0, 12)}…</span>
              </li>
            ))}
            {tasks.length === 0 && <li className="empty">暂无已准备的任务集。</li>}
            {tasks.length > 0 && filteredTasks.length === 0 && <li className="empty">没有匹配的任务。</li>}
          </ul>
        </div>

        <div className="operate-card">
          <p className="embed-title">Agent Profile</p>
          <label>
            Profile（Agent 身份）
            <select
              className="control"
              value={profileIndex}
              onChange={(event) => { setProfileIndex(Number(event.target.value)); setPreflight(null); }}
              aria-label="Agent Profile"
            >
              {AGENT_PROFILES.map((item, index) => (
                <option key={`${item.agent_id}@${item.agent_version}`} value={index}>{item.label}</option>
              ))}
            </select>
          </label>
          <label>
            模型档案 id
            <input
              className="control"
              value={model}
              onChange={(event) => { setModel(event.target.value); setPreflight(null); }}
              placeholder="provider/model（oracle 可留空）"
              aria-label="模型档案"
            />
          </label>
          <label>
            重复次数 n_trials
            <input
              className="control"
              value={nTrials}
              onChange={(event) => { setNTrials(event.target.value); setPreflight(null); }}
              inputMode="numeric"
              aria-label="重复次数"
            />
          </label>
          {nTrialsInvalid && <p className="error">{nTrialsInvalid}</p>}
        </div>

        <div className="operate-card">
          <p className="embed-title">预算与限制</p>
          <label>
            单 Trial 超时（秒）
            <input
              className="control"
              value={timeoutSec}
              onChange={(event) => { setTimeoutSec(event.target.value); setPreflight(null); }}
              inputMode="numeric"
              placeholder="留空用服务端默认值"
              aria-label="单 Trial 超时"
            />
          </label>
          <label>
            Job 超时（秒）
            <input
              className="control"
              value={jobTimeoutSec}
              onChange={(event) => { setJobTimeoutSec(event.target.value); setPreflight(null); }}
              inputMode="numeric"
              placeholder="留空用服务端默认值"
              aria-label="Job 超时"
            />
          </label>
          <label>
            Task 聚合规则
            <select
              className="control"
              value={aggregation}
              onChange={(event) => {
                setAggregation(event.target.value as "first-trial" | "mean-success");
                setPreflight(null);
              }}
              aria-label="聚合规则"
            >
              {Object.entries(AGGREGATION_LABELS).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
          {timeoutsInvalid && <p className="error">{timeoutsInvalid}</p>}
          <p className="hint">超时与聚合规则在创建时冻结；运行中不叠加自动重试（重试只改变 Trial 处置，不新增实验样本）。</p>
        </div>

        <div className="operate-card">
          <p className="embed-title">预检与创建</p>
          <form onSubmit={submitPreflight} aria-label="Terminal-Bench 预检">
            <div className="actions">
              <button type="submit" disabled={inputInvalid !== null}>预检（零模型调用）</button>
            </div>
          </form>
          {preflightError && <p className="error" data-testid="tb-preflight-error">{preflightError}</p>}
          {preflight && (
            <div data-testid="tb-preflight">
              <p className="hint" data-testid="tb-preflight-verdict">
                {preflight.ok
                  ? "预检通过：可以创建运行。"
                  : `预检未通过（${preflight.reasons.length} 项阻塞）：创建运行被禁用，先按原因修复。`}
              </p>
              {!preflight.ok && (
                <ul className="failure-list" data-testid="tb-preflight-reasons">
                  {preflight.reasons.map((code) => (
                    <li key={code} className="fail">
                      <span className="mono">{code}</span>
                      {"： "}
                      {preflight.messages?.[code] ?? "原因码未登记（按 fail-closed 处理，不猜含义）"}
                    </li>
                  ))}
                </ul>
              )}
              <table aria-label="预检检查项">
                <thead><tr><th>检查项</th><th>取值</th></tr></thead>
                <tbody>
                  {Object.entries(preflight.checks ?? {}).map(([key, value]) => (
                    <tr key={key}><td className="mono">{key}</td><td className="mono">{describeCheckValue(value)}</td></tr>
                  ))}
                </tbody>
              </table>
              <dl className="kv">
                <div>
                  <dt>Profile 指纹</dt>
                  <dd className="mono" data-testid="tb-profile-fingerprint">{preflight.profile_fingerprint || "未知"}</dd>
                </div>
                <div>
                  <dt>平台自定义 Profile</dt>
                  <dd>{preflight.platform_custom_profile ? "是（比登记 Profile 更严，不能冒充完全同口径）" : "否"}</dd>
                </div>
              </dl>
            </div>
          )}
          <form onSubmit={submitRun} aria-label="创建 Terminal-Bench 运行">
            <div className="actions">
              <button type="submit" className="primary" disabled={createDisabled} data-testid="tb-create-run">
                {submitting ? "提交中…" : `创建运行（${selectedKeys.length === 0 ? "全部 Task" : `${selectedKeys.length} 个 Task`} × ${nTrialsInvalid ? "?" : nTrialsNumber} 次）`}
              </button>
              {!preflightOk && <span className="hint">创建前必须先通过预检。</span>}
            </div>
          </form>
          {runError && <p className="error" data-testid="tb-run-error">{runError}</p>}
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ 任务清单页

export function TerminalBenchTasks() {
  const [tasks, setTasks] = useState<TerminalBenchTask[] | null>(null);
  const [revision, setRevision] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [taskPayload, overviewPayload] = await Promise.all([
          getTerminalBenchTasks(),
          getTerminalBenchOverview().catch(() => null),
        ]);
        if (!alive) return;
        setTasks(taskPayload.items);
        setRevision(
          overviewPayload?.items[0]?.dataset_revision ?? taskPayload.items[0]?.dataset_revision ?? null,
        );
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => { alive = false; };
  }, []);

  const filtered = useMemo(() => (tasks ?? []).filter((task) => matchesTask(task, query)), [tasks, query]);
  const undeclared = (tasks ?? []).filter((task) => !task.declared_license).length;

  return (
    <div className="page">
      <Panel title="Terminal-Bench 任务清单" actions={<Link className="link" to={ROUTES.operate}>操作页</Link>}>
        {error && <p className="error">{error}</p>}
        <div className="inline-field">
          <label className="field-label" htmlFor="tb-tasks-search">搜索</label>
          <input
            id="tb-tasks-search"
            className="control"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="按 task_key / 相对路径 / 显示名过滤"
            aria-label="任务搜索"
          />
        </div>
        <p className="hint" data-testid="tb-tasks-meta">
          数据集版本：<span className="mono">{revision ?? "未准备"}</span>；
          共 {tasks?.length ?? "未知"} 个 Task（当前显示 {filtered.length} 个）。
        </p>
        <p className="hint" data-testid="tb-license-note">
          许可证以任务集自身声明（declared_license）为准：未声明 {undeclared} 个，
          列表按「未声明」原样显示，不用默认许可代替人工确认。
        </p>
        {tasks === null && <p className="hint">读取中…</p>}
        {tasks?.length === 0 && <p className="empty">暂无已准备的任务集，请先用 CLI/API 准备 Terminal-Bench 任务集。</p>}
        {tasks !== null && tasks.length > 0 && (
          <table aria-label="任务清单">
            <thead>
              <tr>
                <th>任务</th><th>相对路径</th><th>文件数</th><th>大小</th>
                <th>tests（Verifier）</th><th>solution</th><th>许可证</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((task) => (
                <tr key={task.task_key} data-testid={`tb-task-${task.task_key}`}>
                  <td className="mono nowrap" title={task.task_key}>{task.task_key.slice(0, 16)}…</td>
                  <td className="mono">{task.normalized_relative_path}</td>
                  <td className="mono">{formatCount(task.file_count)}</td>
                  <td className="mono">{formatBytes(task.total_bytes)}</td>
                  <td className="mono">{task.has_tests === null || task.has_tests === undefined ? "未知" : task.has_tests ? "有" : "无"}</td>
                  <td className="mono">{task.has_solution === null || task.has_solution === undefined ? "未知" : task.has_solution ? "有" : "无"}</td>
                  <td className="mono">{task.declared_license ?? "未声明"}</td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr><td colSpan={7} className="empty">没有匹配的任务。</td></tr>
              )}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

// ------------------------------------------------------------------ 监控页

function MonitorRunNode({ run, keyPrefix }: { run: RunRecord; keyPrefix: number }) {
  const [identity, setIdentity] = useState<string | null>(null);
  const [tasksState, setTasksState] = useState<Identified<TerminalBenchTaskRow[]> | null>(null);
  const [trialsByTask, setTrialsByTask] = useState<Record<string, Identified<TerminalBenchTrialRow[]>>>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState("");
  const currentIdentity = `${run.id}#${keyPrefix}`;
  const tasks = valueFor(tasksState, currentIdentity) ?? [];
  const loading = identity !== currentIdentity;

  useEffect(() => {
    let alive = true;
    setError("");
    setExpanded(null);
    setTrialsByTask({});
    setTasksState(null);
    getRunTasks(run.id)
      .then((payload) => {
        if (!alive) return;
        setTasksState({ identity: currentIdentity, value: payload.items });
        setIdentity(currentIdentity);
      })
      .catch((err) => {
        if (!alive) return;
        setError(err instanceof Error ? err.message : String(err));
        setIdentity(currentIdentity);
      });
    return () => { alive = false; };
    // currentIdentity 由 run.id 与刷新序号决定：刷新会换发请求并让旧响应失效。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentIdentity]);

  const toggleTask = (taskKey: string) => {
    const next = expanded === taskKey ? null : taskKey;
    setExpanded(next);
    if (next === null) return;
    const cached = trialsByTask[taskKey];
    if (cached !== undefined && cached.identity === currentIdentity) return;
    getRunTaskTrials(run.id, taskKey)
      .then((payload) => setTrialsByTask((current) => ({
        ...current,
        [taskKey]: { identity: currentIdentity, value: payload.items },
      })))
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  };

  const queuedWithoutTrials = run.status === "queued"
    && !loading
    && tasks.every((task) => (task.observed_trials ?? 0) === 0);

  return (
    <li className="tb-tree-item">
      <div className="batch-row-head">
        <span className="mono nowrap">{run.id}</span>
        <StatusBadge status={run.status} />
        <span className="mono">{modelLabel(run) ?? "未知"}</span>
        <span className="hint">
          {tasks.length > 0
            ? `Task ${tasks.length}；计划 Trial ${tasks.reduce((sum, task) => sum + (task.planned_trials ?? 0), 0)}`
            : "Task 未知"}
        </span>
        <Link className="link" to={ROUTES.result(run.id)}>结果页</Link>
      </div>
      {error && <p className="error">{error}</p>}
      {queuedWithoutTrials && (
        <p className="empty" data-testid="tb-queued-empty">
          运行已排队，还没有 Trial：等待 Worker 领取并启动 Harbor Job。Trial 一旦产出会立即出现在这里。
        </p>
      )}
      {!loading && tasks.length === 0 && !queuedWithoutTrials && (
        <p className="empty">该运行还没有 Task 记录（未产出任何 Trial）。</p>
      )}
      {loading && <p className="hint">读取 Task 中…</p>}
      {tasks.length > 0 && (
        <ul className="tb-tree" aria-label={`${run.id} 的 Task → Trial 树`}>
          {tasks.map((task) => {
            const open = expanded === task.task_key;
            const trials = valueFor(trialsByTask[task.task_key] ?? null, currentIdentity) ?? [];
            return (
              <li key={task.task_key} className="tb-tree-item">
                <div className="tb-node">
                  <button
                    type="button"
                    className="icon-btn"
                    onClick={() => toggleTask(task.task_key)}
                    aria-expanded={open}
                    aria-label={`展开任务 ${task.task_key}`}
                  >
                    {open
                      ? <CaretDownIcon size={14} weight="bold" aria-hidden />
                      : <CaretRightIcon size={14} weight="bold" aria-hidden />}
                  </button>
                  <span className="mono nowrap">{task.task_key.slice(0, 16)}…</span>
                  <span className="mono">{task.normalized_relative_path ?? "—"}</span>
                  <span className="hint" data-testid={`tb-task-progress-${task.task_key}`}>
                    计划 {formatCount(task.planned_trials)} / 观测 {formatCount(task.observed_trials)}
                    {" "}/ 有效 {formatCount(task.valid_trials)} / 无效 {formatCount(task.invalid_trials)}
                  </span>
                  <span className="mono">{formatRate(task.valid_trial_pass_rate)}</span>
                  {task.task_pass === true && <StateBadge value="succeeded" />}
                  {task.task_pass === false && <StateBadge value="failed" />}
                  {task.task_pass === null && <span className="hint">{task.task_pass_reason || "无有效 Trial"}</span>}
                </div>
                {open && (
                  <ul className="tb-tree" aria-label={`${task.task_key} 的 Trial`}>
                    {trials.map((trial) => (
                      <li key={trial.trial_id} className="tb-node" data-testid={`tb-trial-node-${trial.trial_id}`}>
                        <span className="mono nowrap">{trial.trial_id}</span>
                        <span className="mono">#{trial.repeat_index ?? "?"}</span>
                        <StateBadge value={trial.disposition} />
                        <StateBadge value={trial.verifier_status} />
                        <span className="hint">reward {formatReward(trial.reward)}</span>
                        <span className="hint">{trial.valid ? "有效 Trial" : "无效 Trial"}</span>
                        <Link
                          className="link"
                          to={`${ROUTES.result(run.id)}?task=${encodeURIComponent(task.task_key)}&trial=${encodeURIComponent(trial.trial_id)}`}
                        >
                          钻取
                        </Link>
                      </li>
                    ))}
                    {trials.length === 0 && <li className="empty">暂无 Trial 记录。</li>}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </li>
  );
}

export function TerminalBenchMonitor() {
  const [params] = useSearchParams();
  const requestedIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  const requestedKey = requestedIds.join(",");
  const [runs, setRuns] = useState<RunRecord[] | null>(null);
  const [runnerNote, setRunnerNote] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let alive = true;
    setError("");
    setRuns(null);
    (async () => {
      try {
        const overview = await getTerminalBenchOverview().catch(() => null);
        if (!alive) return;
        if (overview) {
          setRunnerNote(
            `${overview.runner.adapter_id} / harbor ${overview.runner.harbor_version}：`
            + (overview.runner.connected ? "Runner 已连接" : "Runner 未连接，Job 不会启动"),
          );
        }
        if (requestedKey.length > 0) {
          const records = await Promise.all(
            requestedKey.split(",").map((id) => getRun(id).catch(() => null)),
          );
          if (!alive) return;
          setRuns(records.filter((record): record is RunRecord => record !== null));
        } else {
          const payload = await getRuns();
          if (!alive) return;
          setRuns(payload.items.filter(isTerminalBenchRun).slice(0, 5));
        }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => { alive = false; };
  }, [requestedKey, reloadKey]);

  return (
    <div className="page">
      <Panel
        title="Job → Task → Trial 监控"
        actions={(
          <div className="panel-head-actions">
            <button
              type="button"
              className="icon-btn"
              aria-label="刷新"
              onClick={() => setReloadKey((key) => key + 1)}
            >
              <ArrowsClockwiseIcon size={16} weight="bold" aria-hidden />
            </button>
            <BackLink />
          </div>
        )}
      >
        {error && <p className="error">{error}</p>}
        {runnerNote && <p className="hint" data-testid="tb-runner-status">{runnerNote}</p>}
        <p className="hint">
          一个 Run 对应一个 Harbor Job；Job 内的 Task 与计划 Trial 全部列出，未产出结果的 Trial 不隐藏。
        </p>
        {runs === null && <p className="hint">读取中…</p>}
        {runs?.length === 0 && (
          <p className="empty" data-testid="tb-monitor-empty">暂无 Terminal-Bench 运行。</p>
        )}
        {runs !== null && runs.length > 0 && (
          <ul className="tb-tree" aria-label="Job → Task → Trial 树">
            {runs.map((run) => <MonitorRunNode key={run.id} run={run} keyPrefix={reloadKey} />)}
          </ul>
        )}
      </Panel>
    </div>
  );
}

// ------------------------------------------------------------------ 结果页与 Trial 钻取

interface CostSummary {
  knownCostUsd: number | null;
  knownTrials: number;
  unknownTrials: number;
  successes: number;
  perSuccessUsd: number | null;
}

function summarizeCost(
  trials: TerminalBenchTrialRow[],
  details: Record<string, TerminalBenchTrialDetail>,
): CostSummary {
  let total = 0;
  let knownTrials = 0;
  let unknownTrials = 0;
  for (const row of trials) {
    const cost = asNumber(details[row.trial_id]?.usage?.cost_usd);
    if (cost === null) {
      unknownTrials += 1;
    } else {
      total += cost;
      knownTrials += 1;
    }
  }
  const successes = trials.filter((row) => row.valid && asNumber(row.reward) !== null && Number(row.reward) > 0).length;
  return {
    knownCostUsd: knownTrials > 0 ? total : null,
    knownTrials,
    unknownTrials,
    successes,
    perSuccessUsd: knownTrials > 0 && successes > 0 ? total / successes : null,
  };
}

function perSuccessText(cost: CostSummary): string {
  if (cost.perSuccessUsd !== null) return formatCost(cost.perSuccessUsd);
  if (cost.successes === 0) return "不适用";
  return "未知";
}

/** 工件视图：身份 = run + trial + artifact，切 Trial 立即清空展开项。 */
function ArtifactViewer({
  runId,
  trialId,
  artifacts,
}: {
  runId: string;
  trialId: string;
  artifacts: TerminalBenchArtifact[];
}) {
  const [open, setOpen] = useState<string | null>(null);
  const selected = artifacts.find((artifact) => artifact.artifact_id === open) ?? null;

  return (
    <div data-testid="tb-artifact-viewer">
      {artifacts.length === 0 ? (
        <p className="empty" data-testid="tb-artifacts-missing">
          缺工件：该 Trial 没有可读工件（workspace / 轨迹可能未被采集）。
        </p>
      ) : (
        <table aria-label="Trial 工件">
          <thead>
            <tr><th>工件</th><th>类型</th><th>完整度</th><th>大小</th><th>sha256</th><th /></tr>
          </thead>
          <tbody>
            {artifacts.map((artifact) => (
              <tr
                key={`${runId}:${trialId}:${artifact.artifact_id}`}
                data-testid={`tb-artifact-${artifact.artifact_id}`}
              >
                <td className="mono nowrap">{artifact.artifact_id}</td>
                <td className="mono">{artifact.kind}</td>
                <td><StateBadge value={artifactState(artifact)} table={COVERAGE_STATE_LABELS} /></td>
                <td className="mono">{formatBytes(artifact.size_bytes)}</td>
                <td className="mono">{artifact.sha256 ?? "未知"}</td>
                <td className="row-actions">
                  <button
                    type="button"
                    className="link"
                    aria-expanded={open === artifact.artifact_id}
                    onClick={() => setOpen(open === artifact.artifact_id ? null : artifact.artifact_id)}
                  >
                    查看
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {selected && (
        <div className="drill-detail" data-testid="tb-artifact-detail">
          <p><span className="field-label">工件</span><span className="mono">{selected.artifact_id}</span></p>
          <p><span className="field-label">类型</span><span className="mono">{selected.kind}</span></p>
          <p><span className="field-label">sha256</span><span className="mono">{selected.sha256 ?? "未知"}</span></p>
          <p><span className="field-label">大小</span><span className="mono">{formatBytes(selected.size_bytes)}</span></p>
          <p>
            <span className="field-label">完整度</span>
            <StateBadge value={artifactState(selected)} table={COVERAGE_STATE_LABELS} />
          </p>
          {selected.note && <p><span className="field-label">备注</span>{selected.note}</p>}
        </div>
      )}
    </div>
  );
}

function TrialDrillDown({
  runId,
  trialId,
  sourceTrialId,
  detail,
  loading,
  error,
}: {
  runId: string;
  trialId: string;
  /** 上游 Runner 自己的 Trial 身份（来自 Trial 清单行，不在详情契约里）。 */
  sourceTrialId: string | null;
  detail: TerminalBenchTrialDetail | null;
  loading: boolean;
  error: string;
}) {
  const coverageItems = detail?.coverage?.items ?? {};
  const missing = detail?.coverage?.missing ?? [];
  const partial = detail?.coverage?.partial ?? [];
  const rewards = detail?.verifier_observation?.rewards ?? {};
  const rewardEntries = Object.entries(rewards);
  const verifierError = describeError(detail?.verifier_observation?.error);
  const termination = detail?.termination ?? {};
  const timings = (termination.timings ?? {}) as Record<string, unknown>;
  const failurePhase = typeof termination.failure_phase === "string" ? termination.failure_phase : null;

  return (
    <section className="panel detail" aria-label="Trial 钻取" data-testid="tb-trial-drill">
      <div className="panel-head">
        <h2>Trial 钻取</h2>
        <span className="hint mono nowrap" data-testid="tb-drill-trial-id">{trialId}</span>
      </div>
      {loading && <p className="hint">读取 Trial 中…</p>}
      {error && <p className="error" data-testid="tb-drill-error">{error}</p>}
      {detail && (
        <>
          <dl className="kv">
            <div><dt>Task</dt><dd className="mono">{detail.task_key}</dd></div>
            <div><dt>重复序号</dt><dd className="mono">{detail.repeat_index ?? "未知"}</dd></div>
            <div><dt>来源 Trial</dt><dd className="mono">{sourceTrialId ?? "未知"}</dd></div>
            <div><dt>处置</dt><dd><StateBadge value={detail.disposition} /></dd></div>
            <div>
              <dt>判定</dt>
              <dd data-testid="tb-drill-verdict">{rewardVerdict(detail)}</dd>
            </div>
            <div><dt>证据完整</dt><dd>{detail.evidence_complete ? "是" : "否（缺工件或截断，见覆盖表）"}</dd></div>
          </dl>

          <h3 className="embed-title">终止（Agent / Verifier / 环境分开统计）</h3>
          <dl className="kv">
            <div>
              <dt>原因</dt>
              <dd data-testid="tb-drill-termination">
                {TERMINATION_REASON_LABELS[String(termination.reason ?? "")] ?? (termination.reason ? String(termination.reason) : "未知")}
              </dd>
            </div>
            <div><dt>Agent 是否启动</dt><dd>{termination.agent_started === true ? "是" : termination.agent_started === false ? "否" : "未知"}</dd></div>
            <div><dt>失败阶段</dt><dd>{failurePhase ? (PHASE_LABELS[failurePhase] ?? failurePhase) : "无"}</dd></div>
            <div><dt>异常类型</dt><dd className="mono">{termination.exception_type ? String(termination.exception_type) : "无"}</dd></div>
            <div><dt>异常信息</dt><dd>{termination.exception_message ? String(termination.exception_message) : "无"}</dd></div>
          </dl>
          <table aria-label="Trial 分阶段耗时" data-testid="tb-termination-timings">
            <thead><tr><th>阶段</th><th>耗时</th></tr></thead>
            <tbody>
              {TIMING_FIELDS.map((field) => (
                <tr key={field.key}>
                  <td>{field.label}</td>
                  <td className="mono">{formatSeconds(timings[field.key])}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 className="embed-title">Verifier 观测</h3>
          <dl className="kv">
            <div><dt>状态</dt><dd><StateBadge value={detail.verifier_observation?.status} /></dd></div>
            <div>
              <dt>Reward</dt>
              <dd className="mono" data-testid="tb-drill-reward">
                {rewardEntries.length === 0
                  ? "未知（没有 reward 维度，不等于 0）"
                  : rewardEntries.map(([key, value]) => `${key}=${formatReward(value)}`).join("、")}
              </dd>
            </div>
            <div>
              <dt>错误</dt>
              <dd className="mono" data-testid="tb-drill-verifier-error">{verifierError ?? "无"}</dd>
            </div>
            <div><dt>证据引用</dt><dd className="mono">{detail.verifier_observation?.evidence_refs?.length ?? 0} 条</dd></div>
          </dl>

          <h3 className="embed-title">终端文本</h3>
          {detail.terminal_text === null || detail.terminal_text === undefined ? (
            <p className="hint" data-testid="tb-terminal-missing">终端文本不可用（未捕获或不可读）。</p>
          ) : (
            <>
              {detail.terminal_truncated && (
                <p className="hint fail" data-testid="tb-terminal-truncated">
                  终端文本已截断：以下内容不是完整日志，平台不据此推断被截去的部分。
                </p>
              )}
              <pre className="terminal-log" data-testid="tb-terminal-log">{detail.terminal_text}</pre>
            </>
          )}

          <h3 className="embed-title">覆盖与工件完整度</h3>
          <table aria-label="Trial 覆盖项" data-testid="tb-coverage">
            <thead><tr><th>覆盖项</th><th>状态</th></tr></thead>
            <tbody>
              {Object.entries(coverageItems).map(([key, value]) => (
                <tr key={key}>
                  <td>{COVERAGE_ITEM_LABELS[key] ?? key}<span className="hint mono"> {key}</span></td>
                  <td><StateBadge value={value} table={COVERAGE_STATE_LABELS} /></td>
                </tr>
              ))}
              {Object.keys(coverageItems).length === 0 && (
                <tr><td colSpan={2} className="empty">没有覆盖率登记（未知）。</td></tr>
              )}
            </tbody>
          </table>
          <dl className="kv">
            <div>
              <dt>缺失</dt>
              <dd data-testid="tb-coverage-missing">{missing.length === 0 ? "无" : missing.map((key) => COVERAGE_ITEM_LABELS[key] ?? key).join("、")}</dd>
            </div>
            <div>
              <dt>部分 / 截断</dt>
              <dd>{partial.length === 0 ? "无" : partial.map((key) => COVERAGE_ITEM_LABELS[key] ?? key).join("、")}</dd>
            </div>
            <div><dt>轨迹可用性</dt><dd data-testid="tb-trajectory">{(COVERAGE_STATE_LABELS[coverageItems.trajectory ?? ""] ?? { label: coverageItems.trajectory ?? "未知" }).label}</dd></div>
            <div>
              <dt>用量口径</dt>
              <dd data-testid="tb-usage-coverage">
                {USAGE_COVERAGE_LABELS[detail.usage?.coverage ?? ""] ?? "未知（未登记口径）"}
                {"；成本 "}
                {formatCost(detail.usage?.cost_usd)}
              </dd>
            </div>
          </dl>
          <ArtifactViewer
            key={`${runId}:${trialId}`}
            runId={runId}
            trialId={trialId}
            artifacts={detail.artifacts ?? []}
          />
          <p className="hint">
            工件按身份引用（artifact_id / sha256 / 完整度），本页只读清单；
            「缺工件」表示证据未被采集或被截断，不代表任务失败。
          </p>
        </>
      )}
    </section>
  );
}

export function TerminalBenchResult() {
  const { runId = "" } = useParams();
  const [params] = useSearchParams();
  const query = params.toString();
  const [runState, setRunState] = useState<Identified<RunRecord> | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [tasksState, setTasksState] = useState<Identified<TerminalBenchTaskRow[]> | null>(null);
  const [tasksError, setTasksError] = useState("");
  const [taskKey, setTaskKey] = useState("");
  const [trialsState, setTrialsState] = useState<Identified<TerminalBenchTrialRow[]> | null>(null);
  const [trialsError, setTrialsError] = useState("");
  const [detailsState, setDetailsState] = useState<Identified<Record<string, TerminalBenchTrialDetail>> | null>(null);
  const [selectedTrial, setSelectedTrial] = useState("");
  const [drillState, setDrillState] = useState<Identified<TerminalBenchTrialDetail> | null>(null);
  const [drillError, setDrillError] = useState("");
  const [drillLoading, setDrillLoading] = useState(false);
  const drillToken = useRef(0);

  const run = valueFor(runState, runId);
  const tasks = valueFor(tasksState, runId) ?? [];
  const trialIdentity = `${runId}:${taskKey}`;
  const trials = valueFor(trialsState, trialIdentity) ?? [];
  const details = valueFor(detailsState, trialIdentity) ?? {};
  const drillIdentity = `${runId}:${selectedTrial}`;
  const drillDetail = valueFor(drillState, drillIdentity);

  useEffect(() => {
    let alive = true;
    setNotFound(false);
    setRunState(null);
    setTasksState(null);
    setTasksError("");
    setTrialsState(null);
    setTrialsError("");
    setDetailsState(null);
    setTaskKey(params.get("task") ?? "");
    setSelectedTrial(params.get("trial") ?? "");
    setDrillState(null);
    setDrillError("");
    drillToken.current += 1;
    getRun(runId)
      .then((payload) => { if (alive) setRunState({ identity: runId, value: payload }); })
      .catch(() => { if (alive) setNotFound(true); });
    getRunTasks(runId)
      .then((payload) => { if (alive) setTasksState({ identity: runId, value: payload.items }); })
      .catch((err) => {
        if (alive) setTasksError(err instanceof Error ? err.message : String(err));
      });
    return () => { alive = false; };
    // query 参与重置：深链换 Run/Task/Trial 时旧证据立即失效。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, query]);

  // Task 选择：深链无效或未指定时回落到第一个 Task。
  useEffect(() => {
    if (tasks.length === 0) return;
    if (taskKey && tasks.some((task) => task.task_key === taskKey)) return;
    setTaskKey(tasks[0].task_key);
  }, [tasks, taskKey]);

  // Trial 清单：按 run + task 身份装载，切 Task 立即清空。
  useEffect(() => {
    let alive = true;
    setTrialsError("");
    if (!taskKey) return () => { alive = false; };
    getRunTaskTrials(runId, taskKey)
      .then((payload) => {
        if (alive) setTrialsState({ identity: `${runId}:${taskKey}`, value: payload.items });
      })
      .catch((err) => {
        if (!alive) return;
        setTrialsError(err instanceof Error ? err.message : String(err));
        setTrialsState({ identity: `${runId}:${taskKey}`, value: [] });
      });
    return () => { alive = false; };
  }, [runId, taskKey]);

  // 未指定（或指定了不在清单里的）Trial 时回落到第一个计划 Trial。
  useEffect(() => {
    if (trials.length === 0) return;
    if (selectedTrial && trials.some((row) => row.trial_id === selectedTrial)) return;
    setSelectedTrial(trials[0].trial_id);
  }, [trials, selectedTrial]);

  // 成本视图需要每个 Trial 的 usage：只读拉取该 Task 的全部 Trial 详情（数量 = n_trials，规模可控）。
  useEffect(() => {
    if (trials.length === 0) return;
    let alive = true;
    const identity = `${runId}:${taskKey}`;
    Promise.all(trials.map((row) => getRunTrial(runId, row.trial_id)
      .then((detail) => [row.trial_id, detail] as const)
      .catch(() => null)))
      .then((entries) => {
        if (!alive) return;
        const byId: Record<string, TerminalBenchTrialDetail> = {};
        for (const entry of entries) {
          if (entry !== null) byId[entry[0]] = entry[1];
        }
        setDetailsState({ identity, value: byId });
      });
    return () => { alive = false; };
  }, [runId, taskKey, trials]);

  // 钻取：每次选择换发请求代号；晚到的旧 Trial 响应不得覆盖更新的选择。
  useEffect(() => {
    const token = ++drillToken.current;
    setDrillError("");
    if (!selectedTrial) {
      setDrillLoading(false);
      return;
    }
    setDrillLoading(true);
    getRunTrial(runId, selectedTrial)
      .then((payload) => {
        if (token !== drillToken.current) return;
        setDrillState({ identity: `${runId}:${selectedTrial}`, value: payload });
        setDrillLoading(false);
      })
      .catch((err) => {
        if (token !== drillToken.current) return;
        setDrillError(err instanceof Error ? err.message : String(err));
        setDrillLoading(false);
      });
  }, [runId, selectedTrial]);

  const aggregate = useMemo(() => {
    const embedded = tasks.find((task) => task.aggregate && typeof task.aggregate === "object")?.aggregate;
    if (embedded) return embedded as Record<string, unknown>;
    if (tasks.length === 0) return null;
    return {
      selected_tasks: tasks.length,
      scored_tasks: tasks.filter((task) => task.task_pass !== null).length,
      selected_trials: tasks.reduce((sum, task) => sum + (task.planned_trials ?? 0), 0),
      observed_trials: tasks.reduce((sum, task) => sum + (task.observed_trials ?? 0), 0),
      valid_trials: tasks.reduce((sum, task) => sum + (task.valid_trials ?? 0), 0),
      invalid_trials: tasks.reduce((sum, task) => sum + (task.invalid_trials ?? 0), 0),
      valid_trial_pass_rate: null,
    };
  }, [tasks]);

  const cost = useMemo(() => summarizeCost(trials, details), [trials, details]);
  const passRate = asNumber(aggregate?.valid_trial_pass_rate);
  const invalidTrials = asNumber(aggregate?.invalid_trials);
  const completed = run?.status === "completed";
  const passRateTone: Tone = !completed ? "neutral" : (passRate !== null && passRate > 0 ? "success" : "error");
  const selectedTask = tasks.find((task) => task.task_key === taskKey) ?? null;

  return (
    <div className="page">
      <Panel title="Terminal-Bench 结果" actions={<BackLink />}>
        {notFound && <p className="hint">运行不存在或已删除。</p>}
        {!run && !notFound && <p className="hint">读取中…</p>}
        {run && (
          <>
            <dl className="kv">
              <div><dt>Run</dt><dd className="mono nowrap">{run.id}</dd></div>
              <div><dt>状态</dt><dd><StatusBadge status={run.status} /></dd></div>
              <div><dt>场景</dt><dd className="mono">{run.scenario_version}</dd></div>
              <div>
                <dt>模型</dt>
                <dd className="mono" data-testid="tb-model">{modelLabel(run) ?? "未知（运行快照没有模型身份）"}</dd>
              </div>
              <div>
                <dt>数据集版本</dt>
                <dd className="mono">
                  {run.manifest?.benchmark_provenance?.dataset_revision
                    ?? run.manifest?.external_benchmark?.dataset_revision
                    ?? "未知"}
                </dd>
              </div>
              <div>
                <dt>聚合规则</dt>
                <dd className="mono">{run.manifest?.benchmark_provenance?.aggregation ?? "未知"}</dd>
              </div>
            </dl>

            <MetricCards items={[
              { label: "有效 Trial 通过率", value: formatRate(passRate), tone: passRateTone },
              {
                label: "有效 Trial / 计划 Trial",
                value: `${formatCount(aggregate?.valid_trials)}/${formatCount(aggregate?.selected_trials)}`,
              },
              {
                label: "有效 Task / 计划 Task",
                value: `${formatCount(aggregate?.scored_tasks)}/${formatCount(aggregate?.selected_tasks)}`,
              },
              {
                label: "无效 Trial",
                value: formatCount(invalidTrials),
                tone: invalidTrials !== null && invalidTrials > 0 ? "error" : "neutral",
              },
              { label: "已知成本（USD）", value: formatCost(cost.knownCostUsd) },
              { label: "每成功成本（USD）", value: perSuccessText(cost) },
            ]} />
            <p className="hint" data-testid="tb-cost-note">
              质量分母是有效 Trial，覆盖分母是计划 Trial；无效 Trial 既不算通过也不算 0 分。
              成本：成本已知的 Trial {cost.knownTrials} 个、成本未知 {cost.unknownTrials} 个，
              未知量一律显示为「未知」，不参与求值（含失败 Trial 的费用计入分子）。
            </p>
            {tasksError && <p className="error">{tasksError}</p>}

            <div className="inline-field">
              <label className="field-label" htmlFor="tb-result-task">Task</label>
              <select
                id="tb-result-task"
                className="control"
                value={taskKey}
                onChange={(event) => {
                  setTaskKey(event.target.value);
                  setSelectedTrial("");
                }}
                aria-label="Task"
              >
                {tasks.map((task) => (
                  <option key={task.task_key} value={task.task_key}>
                    {task.normalized_relative_path ?? task.task_key}（{task.task_key.slice(0, 12)}…）
                  </option>
                ))}
              </select>
              {selectedTask && (
                <span className="hint" data-testid="tb-task-summary">
                  计划 {formatCount(selectedTask.planned_trials)} / 观测 {formatCount(selectedTask.observed_trials)}
                  {" "}/ 有效 {formatCount(selectedTask.valid_trials)} / 无效 {formatCount(selectedTask.invalid_trials)}
                  {" "}/ 通过率 {formatRate(selectedTask.valid_trial_pass_rate)}
                  {" "}/ Task 判定 {selectedTask.task_pass === null ? `未知（${selectedTask.task_pass_reason || "无有效 Trial"}）` : selectedTask.task_pass ? "通过" : "未通过"}
                </span>
              )}
            </div>
            {tasks.length === 0 && !tasksError && <p className="empty">暂无 Task：该运行还没有产出 Trial。</p>}

            <table aria-label="Trial 清单">
              <thead>
                <tr>
                  <th>Trial</th><th>重复</th><th>处置</th><th>Verifier</th>
                  <th>reward</th><th>有效</th><th>覆盖缺失</th><th />
                </tr>
              </thead>
              <tbody>
                {trials.map((row) => (
                  <tr key={row.trial_id} data-testid={`tb-trial-${row.trial_id}`}>
                    <td className="mono nowrap">{row.trial_id}</td>
                    <td className="mono">{row.repeat_index ?? "未知"}</td>
                    <td><StateBadge value={row.disposition} /></td>
                    <td><StateBadge value={row.verifier_status} /></td>
                    <td className="mono" data-testid={`tb-reward-${row.trial_id}`}>{formatReward(row.reward)}</td>
                    <td>{row.valid ? "有效" : "无效"}</td>
                    <td className="mono" data-testid={`tb-coverage-${row.trial_id}`}>
                      {coverageSummary(row.coverage)}
                    </td>
                    <td className="row-actions">
                      <button
                        type="button"
                        className="link"
                        onClick={() => setSelectedTrial(row.trial_id)}
                        aria-pressed={selectedTrial === row.trial_id}
                      >
                        钻取
                      </button>
                    </td>
                  </tr>
                ))}
                {trials.length === 0 && (
                  <tr>
                    <td colSpan={8} className="empty">
                      {taskKey ? "暂无 Trial：该 Task 的计划尚未产出结果。" : "暂无 Trial。"}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
            {trialsError && <p className="error">{trialsError}</p>}
          </>
        )}
      </Panel>

      {run && selectedTrial && (
        <TrialDrillDown
          key={drillIdentity}
          runId={runId}
          trialId={selectedTrial}
          sourceTrialId={trials.find((row) => row.trial_id === selectedTrial)?.source_trial_id ?? null}
          detail={drillDetail}
          loading={drillLoading}
          error={drillError}
        />
      )}
    </div>
  );
}

// ------------------------------------------------------------------ 比较页

export function TerminalBenchCompare() {
  const [params] = useSearchParams();
  const initial = (params.get("runs") ?? "").split(",").filter(Boolean);
  const [baseline, setBaseline] = useState(initial[0] ?? "");
  const [candidate, setCandidate] = useState(initial[1] ?? "");
  const [comparison, setComparison] = useState<ComparisonReportView | null>(null);
  const [gate, setGate] = useState<GateResultView | null>(null);
  const [error, setError] = useState("");
  const submitRef = useRef(0);

  // 输入改动即作废旧结论，并使在途请求失效（晚到的旧响应不覆盖新选择）。
  useEffect(() => {
    submitRef.current += 1;
    setComparison(null);
    setGate(null);
    setError("");
  }, [baseline, candidate]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const token = ++submitRef.current;
    setError("");
    setComparison(null);
    setGate(null);
    compareRuns(baseline, candidate)
      .then((payload) => {
        if (token !== submitRef.current) return;
        setComparison(payload);
        // 数据不足（不可比）时不给结论：只显示 API 返回的原因。
        if (!payload.eligible) return;
        return evaluateRunGate({
          run_id: candidate,
          baseline_run_id: baseline,
          policy: COMPARE_POLICY,
        }).then((gatePayload) => {
          if (token !== submitRef.current) return;
          setGate(gatePayload);
        });
      })
      .catch((err) => {
        if (token !== submitRef.current) return;
        setError(err instanceof Error ? err.message : String(err));
      });
  };

  return (
    <div className="page">
      <Panel title="Terminal-Bench 比较" actions={<BackLink />}>
        <form className="inline-field" onSubmit={submit} aria-label="选择运行">
          <label className="field-label" htmlFor="tb-baseline">Baseline</label>
          <input
            id="tb-baseline"
            className="control"
            value={baseline}
            onChange={(event) => setBaseline(event.target.value)}
            aria-label="baseline"
          />
          <label className="field-label" htmlFor="tb-candidate">候选</label>
          <input
            id="tb-candidate"
            className="control"
            value={candidate}
            onChange={(event) => setCandidate(event.target.value)}
            aria-label="candidate"
          />
          <button type="submit">比较</button>
        </form>
        <p className="hint">
          固定比较条件（本页发送给 Gate）：指标 {COMPARE_POLICY.metric}，方向 {COMPARE_POLICY.op}
          {" "}{COMPARE_POLICY.threshold}，覆盖要求 {COMPARE_POLICY.required_coverage}，
          可比重 {String(COMPARE_POLICY.require_comparable)}，成本已知要求 {String(COMPARE_POLICY.require_cost_known)}
          （成本未知只标注，不冒充可比）。
        </p>
        {error && <p className="error">{error}</p>}
        {comparison && (
          <div data-testid="tb-comparison">
            {comparison.eligible ? (
              <p className="hint">两份报告可比（指标可用性见下表）。</p>
            ) : (
              <p className="error" data-testid="tb-compare-insufficient">
                数据不足，不给出结论：{comparison.reasons.join("；") || "原因未登记"}
              </p>
            )}
            <p className="hint" data-testid="tb-case-diff">
              任务集合差异：+{comparison.case_diff.added.length} / -{comparison.case_diff.removed.length}
              {" "}/ 改 {comparison.case_diff.changed.length}
            </p>
            <table aria-label="指标可用性">
              <thead><tr><th>条件</th><th>可用</th></tr></thead>
              <tbody>
                {Object.entries(comparison.metric_eligibility ?? {}).map(([metric, eligible]) => (
                  <tr key={metric}>
                    <td className="mono">{metric}</td>
                    <td>{eligible ? "可用" : "不可用"}</td>
                  </tr>
                ))}
                {Object.keys(comparison.metric_eligibility ?? {}).length === 0 && (
                  <tr><td colSpan={2} className="empty">没有指标可用性登记。</td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}
        {gate && (
          <div data-testid="tb-gate">
            {gate.passed ? (
              <p className="hint" data-testid="tb-gate-pass">门禁通过（同任务集、覆盖达标、可比重成立）。</p>
            ) : (
              <p className="error" data-testid="tb-gate-blocked">门禁未通过，不显示放行。</p>
            )}
            <table aria-label="固定比较条件">
              <thead><tr><th>条件</th><th>结论</th><th>原因</th></tr></thead>
              <tbody>
                {gate.rules.map((rule) => (
                  <tr key={rule.id}>
                    <td className="mono">{rule.id}</td>
                    <td>{rule.passed ? "满足" : "不满足"}</td>
                    <td className="hint">{rule.reason}</td>
                  </tr>
                ))}
                {gate.rules.length === 0 && (
                  <tr><td colSpan={3} className="empty">门禁没有登记条件。</td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
