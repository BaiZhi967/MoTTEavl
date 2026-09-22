import type { components } from "./schema";

type GeneratedScore = components["schemas"]["Score"];
type GeneratedCaseRun = components["schemas"]["CaseRun"];
type GeneratedRun = components["schemas"]["Run"];

export type Score = GeneratedScore;

export type CaseRun = Omit<GeneratedCaseRun, "result" | "expected"> & {
  result: any;
  expected?: any;
};

/** Core fields come from generated OpenAPI; JSON payloads stay open for suite plugins. */
export type RunRecord = Omit<
  GeneratedRun,
  "status" | "manifest" | "requested_manifest" | "cases" | "scores" | "cancellation" | "error"
> & {
  status: string;
  manifest?: any;
  requested_manifest?: any;
  cases?: CaseRun[];
  scores?: Score[];
  cancellation?: { reason?: string };
  error?: any;
};

export interface ProviderRecord {
  name: string;
  kind: string;
  base_url?: string;
  credentials?: string;
  api_key_env?: string;
  enabled?: boolean;
  [key: string]: any;
}

export interface ModelRecord {
  id: string;
  provider: string;
  model?: string | null;
  enabled?: boolean;
  generation?: number;
  lifecycle?: "draft" | "published" | "deprecated";
  profile_hash?: string | null;
  published_at?: string | null;
  deprecated_at?: string | null;
  capabilities: Record<string, any>;
  context_window?: number | null;
  max_output_tokens?: number | null;
  input_modalities?: string[];
  supports_tools?: boolean;
  reasoning?: {
    supported: boolean;
    levels: string[];
    control: string | null;
    default_level?: string | null;
  };
  parameters?: Record<string, number | null>;
}

export interface HarnessReport {
  name: string;
  binary: string;
  installed: boolean;
  version: string | null;
  source: string | null;
  path: string | null;
  runnable: boolean;
  protocol_ready?: boolean;
  execution_ready?: boolean;
  error?: string | null;
}

export interface AgentReport {
  id: string;
  kind: string;
  description: string;
  protocol_ready: boolean;
  execution_ready: boolean;
}

export type RunReport = components["schemas"]["RunReport"];

/** 模型摘要：档案引用 > 展开快照 > inline provider 字段（镜像后端 _run_model_label）。
 * 详情端点 GET /runs/{id} 不回顶层 model（仅列表端点回），须从 manifest 摘要。 */
export function modelLabel(run: Pick<RunRecord, "manifest"> | null | undefined): string | null {
  const manifest = run?.manifest;
  if (typeof manifest?.model === "string" && manifest.model) return manifest.model;
  const providerModel = manifest?.provider?.model;
  if (typeof providerModel === "string" && providerModel) return providerModel;
  return null;
}

/** 公共 API 的结构化拒绝：保留 code / status / details，页面按服务端语义显示。 */
export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly details: Record<string, any>;

  constructor(
    status: number,
    payload: { code?: string | null; message?: string | null; details?: Record<string, any> | null },
    fallbackMessage?: string,
  ) {
    const code = payload.code ?? null;
    const message = payload.message || fallbackMessage || `HTTP ${status}`;
    super(code ? `${code}：${message}` : message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
    this.details = payload.details ?? {};
  }
}

/** 请求失败时的展示信息：错误码 + 消息 + 服务端登记的允许取值。 */
export function describeApiError(error: unknown): {
  code: string | null;
  message: string;
  allowed: string[];
  status: number | null;
} {
  if (error instanceof ApiRequestError) {
    const allowed = error.details?.allowed;
    return {
      code: error.code,
      message: error.message,
      allowed: Array.isArray(allowed) ? allowed.map((item) => String(item)) : [],
      status: error.status,
    };
  }
  const message = error instanceof Error ? error.message : String(error);
  return { code: null, message, allowed: [], status: null };
}

const API_TOKEN_KEY = "motte-api-token";

export function getApiToken(): string {
  try {
    return sessionStorage.getItem(API_TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setApiToken(token: string): void {
  try {
    const value = token.trim();
    if (value) sessionStorage.setItem(API_TOKEN_KEY, value);
    else sessionStorage.removeItem(API_TOKEN_KEY);
  } catch {
    // Disabled storage still supports unauthenticated local deployments.
  }
}

function authenticatedHeaders(initial?: HeadersInit): Headers {
  const headers = new Headers(initial);
  const token = getApiToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return headers;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = authenticatedHeaders(init?.headers);
  if (init?.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...init,
    headers,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    // 结构化错误原样上抛（code/message/details）：页面不猜含义、也不吞掉身份。
    const error = payload?.error;
    if (error && typeof error === "object") {
      // 结构化拒绝原样上抛：details 之外，服务端还会把逐字段错误放在
      // error.fields（field/code/message）、允许取值放在 error.allowed —— 一并折叠进
      // details，页面才能逐字段显示，而不是只剩一句 message。
      throw new ApiRequestError(response.status, {
        code: typeof error.code === "string" ? error.code : null,
        message: typeof error.message === "string" ? error.message : null,
        details: {
          ...(error.details && typeof error.details === "object" ? error.details : {}),
          ...(Array.isArray(error.fields) ? { fields: error.fields } : {}),
          ...(Array.isArray(error.allowed) ? { allowed: error.allowed } : {}),
        },
      });
    }
    throw new ApiRequestError(response.status, {
      message: typeof payload?.detail === "string" ? payload.detail : null,
    });
  }
  return payload as T;
}

/** 写请求统一入口：注册类端点用 POST，读-改-写更新端点用 PUT（服务端只注册 PUT，错发 POST 会 405）。 */
const jsonRequest = (method: "POST" | "PUT", body: unknown): RequestInit => ({
  method,
  body: JSON.stringify(body),
});

const jsonBody = (body: unknown): RequestInit => jsonRequest("POST", body);

// ---------------------------------------------------------------- runs

export const getRuns = (status?: string) =>
  request<{ items: RunRecord[]; total: number }>(`/api/v1/runs${status ? `?status=${status}` : ""}`);

// ---------------------------------------------------------------- M4 runtimes

export interface RuntimeReadiness {
  backend: string;
  pinned_version: string;
  installed: boolean;
  installed_version: string | null;
  protocol_ready: boolean;
  execution_ready: boolean;
  reasons: Record<string, string>;
}

export interface RuntimeCatalogItem {
  name: string;
  version: string;
  kind: string | null;
  transport: string | null;
  upstream_version: string | null;
  model_control: string | null;
  interactive: boolean;
  tool_enforcement?: string | null;
  published: boolean;
  readiness: RuntimeReadiness;
}

export const getRuntimes = () =>
  request<{ items: RuntimeCatalogItem[]; total: number }>("/api/v1/runtimes");

export interface RuntimeApproval {
  approval_id: string;
  method: string;
  item_id: string;
  summary: unknown;
  request_hash: string;
  expires_at: string;
  state: string;
}

export interface RuntimeSession {
  session_id: string;
  run_id: string;
  case_id: string;
  attempt_id: string;
  state: string;
  revision: number;
  control_revision: number;
  native_thread_id?: string;
  active_turn_id?: string;
  pending_approvals: RuntimeApproval[];
}

export interface RuntimeCommand {
  id: string;
  type: string;
  status: string;
  case_id?: string;
  session_id?: string;
  dedupe_key?: string;
  content?: string;
  error?: unknown;
  ack_evidence?: { kind?: string };
}

export interface RuntimeCommandRequest {
  kind: "user_message" | "approve" | "reject" | "interrupt";
  case_id: string;
  session_id: string;
  expected_session_revision: number;
  dedupe_key: string;
  content?: string;
  payload?: { approval_id: string; request_hash: string };
}

export const getRuntimeSessions = (runId: string) =>
  request<{ items: RuntimeSession[]; total: number }>(`/api/v1/runs/${encodeURIComponent(runId)}/sessions`);
export const getRuntimeCommands = (runId: string) =>
  request<{ items: RuntimeCommand[]; total: number }>(`/api/v1/runs/${encodeURIComponent(runId)}/commands`);
export async function sendRuntimeCommand(runId: string, body: RuntimeCommandRequest) {
  const result = await request<{ command_id?: unknown; status?: unknown }>(
    `/api/v1/runs/${encodeURIComponent(runId)}/messages`, jsonBody(body),
  );
  if (!result || typeof result.command_id !== "string" || !result.command_id
    || typeof result.status !== "string"
    || !["queued", "delivered", "acknowledged", "rejected", "expired", "delivery_unknown", "failed"].includes(result.status)) {
    throw new TypeError("命令接收响应不完整，提交结果未知");
  }
  return { command_id: result.command_id, status: result.status };
}

export const publishRuntimes = () =>
  request<{ published: string[]; total: number }>("/api/v1/runtimes/publish", jsonBody({}));

export interface RuntimeProfileRecord {
  name: string;
  version: string;
  runtime: string;
  native_settings: Record<string, unknown>;
  workspace: { source: string; root?: string };
  budgets: Record<string, unknown>;
  credential_refs: string[];
  published_at: string;
}

export const getRuntimeProfiles = () =>
  request<{ items: RuntimeProfileRecord[]; total: number }>("/api/v1/runtime_profiles");

export const publishRuntimeProfile = (body: Record<string, unknown>) =>
  request<RuntimeProfileRecord>("/api/v1/runtime_profiles", jsonBody(body));

export const createRun = (body: { scenario_version: string; manifest?: any; case_ids?: string[] }) =>
  request<RunRecord>("/api/v1/runs", jsonBody(body));

export const getRun = (id: string) => request<RunRecord>(`/api/v1/runs/${id}`);

export const cancelRun = (id: string, reason?: string) =>
  request<RunRecord>(`/api/v1/runs/${id}/cancel`, jsonBody({ reason }));

export const retryRun = (id: string) => request<RunRecord>(`/api/v1/runs/${id}/retry`, jsonBody({}));

export const rescoreRun = (id: string) => request<RunRecord>(`/api/v1/runs/${id}/rescore`, jsonBody({}));

export const replayRun = (id: string, cases: Record<string, any>) =>
  request<RunRecord>(`/api/v1/runs/${id}/replay`, jsonBody({ cases }));

export const getReport = (id: string, scoringPassId?: string) =>
  request<RunReport>(`/api/v1/runs/${id}/report${scoringPassId ? `?scoring_pass_id=${encodeURIComponent(scoringPassId)}` : ""}`);

export const getScoringPasses = (id: string) =>
  request<{ items: Array<Record<string, any>>; total: number }>(`/api/v1/runs/${id}/scoring-passes`);

// ---------------------------------------------------------------- resources

export const getProviders = () => request<{ items: ProviderRecord[] }>(`/api/v1/providers`);
export const createProvider = (body: ProviderRecord) => request<ProviderRecord>("/api/v1/providers", jsonBody(body));
/** 读-改-写更新连接（kind / base_url / credentials / api_key_env / enabled）；name 不可变。 */
export const updateProvider = (name: string, body: Partial<ProviderRecord>) =>
  request<ProviderRecord>(`/api/v1/providers/${name}`, jsonRequest("PUT", body));
export const deleteProvider = (name: string) =>
  request<{ deleted: string }>(`/api/v1/providers/${name}`, { method: "DELETE" });

export const getModels = () => request<{ items: ModelRecord[] }>(`/api/v1/models`);
export const createModel = (body: any) => request<ModelRecord>("/api/v1/models", jsonBody(body));
/** 读-改-写更新模型档案（合并式，保留未提及字段）；id / provider 不可变。 */
export const updateModel = (id: string, body: Partial<ModelRecord>) =>
  request<ModelRecord>(`/api/v1/models/${id}`, jsonRequest("PUT", body));
export const publishModel = (id: string) =>
  request<ModelRecord>(`/api/v1/models/${id}/publish`, jsonBody({}));

// ---------------------------------------------------------------- model test

export interface ModelTestResult {
  ok: boolean;
  provider: string;
  model: string;
  base_url?: string | null;
  tested_at: string;
  latency_ms?: number | null;
  attempts?: number | null;
  retry_count?: number | null;
  usage?: Record<string, any> | null;
  error?: { class?: string; message?: string } | null;
}

/** 单次最小真实调用（max_output_tokens=16），验证连接、密钥与模型名；报告已脱敏。 */
export const testModel = (id: string) =>
  request<ModelTestResult>(`/api/v1/models/${id}/test`, jsonBody({}));
export const deleteModel = (id: string) => request<{ deprecated: string }>(`/api/v1/models/${id}`, { method: "DELETE" });

export const getScenarios = () => request<{ items: any[] }>(`/api/v1/scenarios`);

// ---------------------------------------------------------------- benchmarks (GSM8K)

export interface BenchmarkRunProgress {
  id: string;
  status: string;
  created_at?: string | null;
  accuracy?: number | null;
}

export interface BenchmarkPreset {
  scenario: string;
  dataset: string;
  /** smoke=前 20 题 / full=整个 test split；未知预设为 null。 */
  scope?: string | null;
  benchmark?: Record<string, any>;
  provenance?: Record<string, any>;
  cases: number;
  runs: BenchmarkRunProgress[];
}

export interface BenchmarkOverview {
  items: BenchmarkPreset[];
  total: number;
}

export const getBenchmarkOverview = () =>
  request<BenchmarkOverview>(`/api/v1/benchmarks/gsm8k`);

export interface BenchmarkCase {
  case_id: string;
  input: string;
  expected: string;
  source_line?: number | null;
}

export interface BenchmarkCasePage {
  dataset: string;
  /** 当前过滤条件下匹配到的题数（分页分母）。 */
  total: number;
  /** 数据集总题数（不过滤时等于 total）。 */
  dataset_total: number;
  offset: number;
  limit: number;
  query: string;
  items: BenchmarkCase[];
}

/** 分页浏览数据集题目（只读；运行级子集由 createBenchmarkRun 的 case_selection 决定）。 */
export const getBenchmarkCases = (params: {
  dataset: string; offset?: number; limit?: number; query?: string;
}) => {
  const search = new URLSearchParams({ dataset: params.dataset });
  if (params.offset) search.set("offset", String(params.offset));
  if (params.limit) search.set("limit", String(params.limit));
  if (params.query) search.set("query", params.query);
  return request<BenchmarkCasePage>(`/api/v1/benchmarks/gsm8k/cases?${search.toString()}`);
};

/** 下载官方 test split 并导入（默认：解析最新 commit + 全量；同内容复用版本，冲突 409）。 */
export const importBenchmark = (body: {
  revision?: string; license?: string; version?: string; name?: string; scope?: "smoke" | "full";
}) =>
  request<{
    imported: string; scenario: string; scope: string; benchmark: string; cases: number;
    /** 本次真正使用的 commit：省略 revision 时由服务端解析官方最新后固定在此。 */
    revision: string; source_sha256: string; cases_sha256: string;
  }>("/api/v1/benchmarks/gsm8k/import", jsonBody(body));

export interface CaseSelectionRequest {
  /** all=整份数据集；ids=指定题目；random=随机 N 题（seed 记录进运行快照，可复现）。 */
  mode: "all" | "ids" | "random";
  case_ids?: string[];
  count?: number;
  seed?: string;
}

export const createBenchmarkRun = (body: {
  model: string; scenario?: string; parameters?: Record<string, number>;
  reasoning_level?: string; case_selection?: CaseSelectionRequest;
}) => request<RunRecord>("/api/v1/benchmarks/gsm8k/runs", jsonBody(body));

// ---------------------------------------------------------------- direct llm

export type DirectLlmProfileSummary = components["schemas"]["DatasetProfileSummary"];
export type DirectLlmCaseSelection = NonNullable<components["schemas"]["DirectLlmRunRequest"]["case_selection"]>;
export type DirectLlmRunRequest = components["schemas"]["DirectLlmRunRequest"];
export type DirectLlmDryRunResponse = components["schemas"]["DirectLlmDryRunResponse"];

export interface DirectLlmPreset {
  scenario: string;
  dataset: string;
  suite: string;
  contract_version?: number | null;
  dataset_fingerprint?: string | null;
  profiles?: DirectLlmProfileSummary[];
  /** 数据集级 eval 描述：scorer / scorer_version / prompt_version / selected_count 等。 */
  eval?: Record<string, any>;
  provenance?: Record<string, any>;
  cases: number;
  runs: BenchmarkRunProgress[];
}

export interface DirectLlmOverview {
  items: DirectLlmPreset[];
  total: number;
}

export type DirectLlmSourceSummary = components["schemas"]["DatasetSourceSummary"];
export type DirectLlmSourceDetail = components["schemas"]["SourceSpec"];
export type DirectLlmSourceList = components["schemas"]["DatasetSourceListResponse"];

export interface DirectLlmBuiltin {
  id: string;
  label: string;
  description: string;
  scorer: string;
  source: string;
  /** false 表示样例文件缺失或损坏，错误原因见 error。 */
  importable: boolean;
  cases?: number | null;
  error?: string;
}

export interface DirectLlmCase {
  case_id: string;
  input: string;
  /** null = 本题没有期望答案，评分记为「无判定」。 */
  expected: string | null;
  scorer: string;
  source_line?: number | null;
}

export interface DirectLlmCasePage {
  dataset: string;
  total: number;
  dataset_total: number;
  offset: number;
  limit: number;
  query: string;
  items: DirectLlmCase[];
}

export interface DirectLlmImportReceipt {
  imported: string; scenario: string; suite: string; scorer: string; cases: number;
  source: string; source_sha256: string; cases_sha256: string;
}

export const getDirectLlmOverview = () =>
  request<DirectLlmOverview>(`/api/v1/benchmarks/direct-llm`);

/** 只读受管来源目录；仅返回治理摘要，不触发上游网络访问。 */
export const getDirectLlmSources = () =>
  request<DirectLlmSourceList>(`/api/v1/benchmarks/direct-llm/sources`);

/** 读取一个受管来源的本地治理登记详情，不执行 fetch / prepare。 */
export const getSourceDetail = (sourceId: string) =>
  request<DirectLlmSourceDetail>(
    `/api/v1/benchmarks/direct-llm/sources/${encodeURIComponent(sourceId)}`,
  );

/** 仓库内置样例清单（含题数与可导入性），供操作页一键导入。 */
export const getDirectLlmBuiltins = () =>
  request<{ items: DirectLlmBuiltin[]; total: number }>(`/api/v1/benchmarks/direct-llm/builtins`);

/** 分页浏览数据集题目（只读；运行级子集由 createDirectLlmRun 的 case_selection 决定）。 */
export const getDirectLlmCases = (params: {
  dataset: string; offset?: number; limit?: number; query?: string;
}) => {
  const search = new URLSearchParams({ dataset: params.dataset });
  if (params.offset) search.set("offset", String(params.offset));
  if (params.limit) search.set("limit", String(params.limit));
  if (params.query) search.set("query", params.query);
  return request<DirectLlmCasePage>(`/api/v1/benchmarks/direct-llm/cases?${search.toString()}`);
};

/** 导入一份 JSONL：`content`（本地粘贴/上传正文）与 `builtin`（内置样例 id）二选一。 */
export const importDirectLlm = (body: {
  content?: string; builtin?: string; name?: string; version?: string;
  license?: string; scorer?: string; source?: string;
}) => request<DirectLlmImportReceipt>("/api/v1/benchmarks/direct-llm/import", jsonBody(body));

export const dryRunDirectLlm = (body: DirectLlmRunRequest) =>
  request<DirectLlmDryRunResponse>("/api/v1/benchmarks/direct-llm/dry-run", jsonBody(body));

export const createDirectLlmRun = (body: DirectLlmRunRequest) =>
  request<RunRecord>("/api/v1/benchmarks/direct-llm/runs", jsonBody(body));

// ---------------------------------------------------------------- provider kinds

export interface ProviderKindMeta {
  kind: string;
  label: string;
  description: string;
  default_base_url: string | null;
  default_key_env: string | null;
}

// ---------------------------------------------------------------- agent-tasks

export interface AgentTasksOverview {
  items: Array<{
    scenario: string;
    dataset: string;
    suite: string;
    cases: number;
    dataset_fingerprint?: string | null;
    runs: Array<{ id: string; status: string; created_at?: string | null; mode?: string | null }>;
  }>;
  total: number;
}

export interface AgentCaseRecord {
  case_id: string;
  input: string;
  fixture: string[];
  expected: Record<string, any> | null;
  forbidden_paths: string[];
  limits: Record<string, any>;
}

export interface AgentTasksRunRequest {
  scenario: string;
  model: string;
  mode: "native-tool" | "legacy-json";
  budget?: Record<string, number>;
  case_selection?: { mode: string; case_ids?: string[] };
}

export interface AgentDryRunSummary {
  scenario: string;
  dataset: string;
  mode: string;
  prompt_version: string;
  selected_cases: number;
  backend: string;
  budget: Record<string, unknown>;
}

export interface AgentCaseDetail {
  run_id: string;
  case_id: string;
  outcome: string | null;
  pending?: boolean;
  agent: {
    final_output?: unknown;
    termination_reason: string;
    termination_detail?: string | null;
    steps: number;
    tool_calls: number;
    prompt_version?: string | null;
    mode?: string | null;
    duration_ms?: number;
    budget?: Record<string, any>;
  } | null;
  events: Array<Record<string, any>>;
  capture_errors: string[];
  cleanup?: { status?: string; residual?: string[]; error?: string } | null;
  observation: Record<string, any> | null;
  artifacts: Array<{
    artifact_id: string;
    path: string;
    media_type?: string | null;
    size_bytes?: number | null;
    sha256?: string | null;
    available: boolean;
  }>;
}

export const getAgentTasksOverview = () =>
  request<AgentTasksOverview>("/api/v1/agent-tasks");

export const importAgentTasks = (body: { content: string; name: string; version?: string }) =>
  request<Record<string, unknown>>("/api/v1/agent-tasks/import", jsonBody(body));

export const getAgentTasksCases = (params: { dataset: string; query?: string }) =>
  request<{ items: AgentCaseRecord[]; total: number }>(
    `/api/v1/agent-tasks/cases?dataset=${encodeURIComponent(params.dataset)}`
    + (params.query ? `&query=${encodeURIComponent(params.query)}` : ""),
  );

export const dryRunAgentTasks = (body: AgentTasksRunRequest) =>
  request<AgentDryRunSummary>("/api/v1/agent-tasks/runs/dry-run", jsonBody(body));

export const createAgentTasksRun = (body: AgentTasksRunRequest) =>
  request<RunRecord>("/api/v1/agent-tasks/runs", jsonBody(body));

export const getAgentCaseDetail = (runId: string, caseId: string) =>
  request<AgentCaseDetail>(`/api/v1/runs/${runId}/cases/${encodeURIComponent(caseId)}/agent`);

export const getAgentArtifactContent = (runId: string, caseId: string, path: string) =>
  request<{
    path: string;
    media_type?: string | null;
    size_bytes?: number | null;
    sha256?: string | null;
    sha256_matches: boolean;
    content: string;
  }>(
    `/api/v1/runs/${runId}/cases/${encodeURIComponent(caseId)}`
    + `/artifacts/content?path=${encodeURIComponent(path)}`,
  );

export const getRunInvocations = (runId: string, caseId?: string) =>
  request<{ items: Array<Record<string, any>>; total: number }>(
    `/api/v1/runs/${runId}/invocations${caseId ? `?case_id=${encodeURIComponent(caseId)}` : ""}`,
  );

export const getProviderKinds = () =>
  request<{ items: ProviderKindMeta[] }>(`/api/v1/provider_kinds`);

// ---------------------------------------------------------------- credentials

export interface CredentialSummary {
  profile: string;
  key_hint: string;
}

/** 写入服务器凭据文件（与 CLI credentials set 等价）；响应只回掩码，密钥不回显。 */
export const setCredential = (profile: string, apiKey: string) =>
  request<{ profile: string; key_hint: string }>(`/api/v1/credentials/${encodeURIComponent(profile)}`, {
    method: "PUT",
    body: JSON.stringify({ api_key: apiKey }),
  });

export const getCredentials = () => request<{ items: CredentialSummary[] }>(`/api/v1/credentials`);
export const createScenario = (body: any) => request<any>("/api/v1/scenarios", jsonBody(body));

export const getAgents = () =>
  request<{ items: AgentReport[] }>(`/api/v1/agents`);
export const getHarnesses = () => request<{ items: HarnessReport[] }>(`/api/v1/harnesses`);

// ---------------------------------------------------------------- SSE

export interface TraceEvent {
  protocol?: "motte.trace";
  schema_version?: number;
  run_id: string;
  seq: number;
  type: string;
  payload?: Record<string, any>;
  span_id?: string | null;
  parent_span_id?: string | null;
  recorded_at?: string | null;
}

/** 订阅运行事件流；fetch 允许携带 Bearer header，并保留游标/gap 语义。 */
export function subscribeRunEvents(
  runId: string,
  handlers: {
    onEvent: (event: TraceEvent) => void;
    onError?: () => void;
    onGap?: (gap: { type: "gap"; after: number; next_seq: number; partial: boolean }) => void;
  },
  options?: { after?: number },
): () => void {
  const controller = new AbortController();
  let cursor = Math.max(0, options?.after ?? 0);
  let reconnectTimer: number | null = null;
  const terminal = new Set(["completed", "failed", "cancelled", "unsupported", "profile_stale", "needs_review"]);

  const dispatch = (frame: string) => {
    let eventType = "message";
    const data: string[] = [];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith("event:")) eventType = line.slice(6).trim();
      else if (line.startsWith("id:")) {
        const parsed = Number(line.slice(3).trim());
        if (Number.isFinite(parsed)) cursor = Math.max(cursor, parsed);
      } else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (data.length === 0) return false;
    try {
      const payload = JSON.parse(data.join("\n"));
      if (eventType === "motte-gap") {
        handlers.onGap?.(payload);
        return false;
      }
      const event = payload as TraceEvent;
      if (typeof event.seq === "number") cursor = Math.max(cursor, event.seq);
      handlers.onEvent(event);
      const eventStatus = event.payload?.status;
      return typeof eventStatus === "string" && terminal.has(eventStatus);
    } catch {
      return false;
    }
  };

  const connect = async () => {
    try {
      const query = cursor > 0 ? "?after=" + cursor : "";
      const response = await fetch(
        "/api/v1/runs/" + encodeURIComponent(runId) + "/events" + query,
        {
          headers: authenticatedHeaders({ Accept: "text/event-stream" }),
          signal: controller.signal,
        },
      );
      if (!response.ok || !response.body) throw new Error("SSE HTTP " + response.status);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let reachedTerminal = false;
      while (!controller.signal.aborted && !reachedTerminal) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          reachedTerminal = dispatch(buffer.slice(0, boundary)) || reachedTerminal;
          buffer = buffer.slice(boundary + 2);
          boundary = buffer.indexOf("\n\n");
        }
        if (done) break;
      }
      if (reachedTerminal) controller.abort();
    } catch {
      if (!controller.signal.aborted) handlers.onError?.();
    }
    if (!controller.signal.aborted) {
      reconnectTimer = window.setTimeout(() => void connect(), 1_000);
    }
  };

  void connect();
  return () => {
    controller.abort();
    if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
  };
}

export interface EventsSnapshot {
  events: TraceEvent[];
  last_seq: number | null;
  run_status: string;
  partial: boolean;
  next_after?: number | null;
  has_more?: boolean;
}

/** SSE 断线/缺口的持久查询：一次性拉取 seq > after 的事件（协议 §2）。 */
export async function fetchRunEventsSnapshot(
  runId: string,
  after: number,
  limit = 500,
): Promise<EventsSnapshot> {
  const events: TraceEvent[] = [];
  let cursor = Math.max(0, after);
  let latest: EventsSnapshot | null = null;
  while (latest === null || latest.has_more === true) {
    latest = await request<EventsSnapshot>(
      `/api/v1/runs/${encodeURIComponent(runId)}/events/snapshot?after=${cursor}&limit=${limit}`,
    );
    events.push(...latest.events);
    if (!latest.has_more) break;
    const next = latest.next_after ?? latest.last_seq;
    if (next == null || next <= cursor) throw new Error("事件快照游标没有前进");
    cursor = next;
  }
  return { ...latest, events };
}

// ---------------------------------------------------------------------------
// 外部 Benchmark（job-based C-Eval，M2-T07/T08）
// ---------------------------------------------------------------------------

export interface ExternalCatalogBenchmark {
  benchmark_id: string;
  benchmark_version: string;
  status: "registered" | "prepared" | "runnable" | "verified";
  blockers: string[];
  dataset: {
    state: string;
    provenance: string;
    revision: string;
    rows: number;
    gold_rows: number;
    unscored: boolean;
  } | null;
}

export interface ExternalPreflightReport {
  ok: boolean;
  reasons: string[];
  checks: Record<string, boolean | null>;
}

export interface ExternalJobRecord {
  job_id: string;
  run_id: string;
  status: string;
  launch_token: string;
  metrics?: {
    parser_version?: string | null;
    ceval_native?: Record<string, unknown>;
    ceval_diagnostic?: {
      per_subject?: Record<string, number>;
      aggregate?: Record<string, number>;
    };
  };
  evidence?: Record<string, unknown>;
}

export interface ComparisonReportView {
  eligible: boolean;
  reasons: string[];
  metric_eligibility: Record<string, boolean>;
  case_diff: { added: string[]; removed: string[]; changed: string[] };
  /** 政策显式允许的差异（典型是 model / skill）；记录以便「允许」本身可审计。 */
  allowed_differences?: string[];
}

/** 门禁结论（gate-lite@2）：页面照实渲染 rules / metric_id，不自己下结论。 */
export interface GateResultView {
  schema?: string;
  passed: boolean;
  metric_id?: string;
  policy?: Record<string, any>;
  rules: { id: string; passed: boolean; reason: string }[];
  conclusion_hash?: string;
  evaluated_at?: string | null;
}

export const getExternalCatalog = () =>
  request<{ benchmarks: ExternalCatalogBenchmark[] }>("/api/v1/benchmarks/external/catalog");

// 通用外部基准入口（review R16：ceval/cmmlu 同一 API 面）。
export const prepareExternalDataset = (benchmarkId: string, body: Record<string, unknown>) =>
  request<{ state: string; provenance: string; revision: string; rows: number; unscored: boolean }>(
    `/api/v1/benchmarks/external/${encodeURIComponent(benchmarkId)}/prepare`,
    jsonBody(body),
  );

export const getExternalPreflight = (
  benchmarkId: string, params: { model?: string; scope?: string; split?: string },
) => {
  const query = new URLSearchParams();
  if (params.model) query.set("model", params.model);
  if (params.scope) query.set("scope", params.scope);
  if (params.split) query.set("split", params.split);
  return request<ExternalPreflightReport>(
    `/api/v1/benchmarks/external/${encodeURIComponent(benchmarkId)}/preflight?${query.toString()}`,
  );
};

export const getExternalCases = (benchmarkId: string, query = "", offset = 0, limit = 50) =>
  request<{ cases: { case_id: string; subject: string; has_gold: boolean }[]; total: number }>(
    `/api/v1/benchmarks/external/${encodeURIComponent(benchmarkId)}/cases?offset=${offset}&limit=${limit}&query=${encodeURIComponent(query)}`,
  );

export const createExternalRun = (benchmarkId: string, body: Record<string, unknown>) =>
  request<RunRecord>(
    `/api/v1/benchmarks/external/${encodeURIComponent(benchmarkId)}/runs`,
    jsonBody(body),
  );

// C-Eval 兼容封装（既有页面/测试引用）。
export const prepareCevalDataset = (body: Record<string, unknown>) =>
  prepareExternalDataset("ceval", body);

export const getCevalPreflight = (model: string) =>
  getExternalPreflight("ceval", { model });

export const getCevalCases = (query = "", offset = 0, limit = 50) =>
  getExternalCases("ceval", query, offset, limit);

export const createCevalRun = (body: Record<string, unknown>) =>
  createExternalRun("ceval", body);

export const getExternalJobs = (runId: string) =>
  request<{ jobs: ExternalJobRecord[] }>(`/api/v1/runs/${runId}/external-jobs`);

export const compareRuns = (baseline: string, candidate: string, factors = "model") =>
  request<ComparisonReportView>(
    `/api/v1/comparisons?baseline=${encodeURIComponent(baseline)}&candidate=${encodeURIComponent(candidate)}&factors=${factors}`,
  );

export const evaluateRunGate = (body: Record<string, unknown>) =>
  request<GateResultView>("/api/v1/gates", jsonBody(body));

// ---------------------------------------------------------------------------
// Terminal-Bench（Harbor，M3-T09）
// ---------------------------------------------------------------------------

export interface TerminalBenchRunProgress {
  id: string;
  status: string;
  created_at?: string | null;
  /** 有效 Trial 通过率（分母是有效 Trial）；没有有效 Trial 时为 null，不填 0。 */
  valid_trial_pass_rate: number | null;
}

export interface TerminalBenchPreset {
  scenario: string;
  dataset_revision: string;
  tasks: number;
  runner_connected: boolean;
  runs: TerminalBenchRunProgress[];
}

export interface TerminalBenchOverview {
  items: TerminalBenchPreset[];
  total: number;
  runner: { adapter_id: string; harbor_version: string; connected: boolean };
  benchmark: string;
}

export interface TerminalBenchTask {
  task_key: string;
  normalized_relative_path: string;
  display_name?: string | null;
  source_id: string;
  dataset_revision: string;
  file_count: number | null;
  total_bytes: number | null;
  has_tests: boolean | null;
  has_solution: boolean | null;
  declared_license?: string | null;
}

export interface TerminalBenchPreflightReport {
  ok: boolean;
  /** 阻塞原因码；ok=false 时创建运行必须被禁用（fail-closed）。 */
  reasons: string[];
  /** 原因码 → 操作员可执行说明（服务端登记，前端不猜含义）。 */
  messages: Record<string, string>;
  checks: Record<string, unknown>;
  profile_fingerprint: string;
  platform_custom_profile: boolean;
}

/** 公共请求 DTO（review R16）：字段名与 API 一一对应，未知字段会被 422 拒绝。 */
export interface TerminalBenchTimeouts {
  /** Agent 执行期限 → 原生 agents[].override_timeout_sec。 */
  agent_sec?: number;
  /** Verifier 期限 → 原生 verifier.override_timeout_sec。 */
  verifier_sec?: number;
  /** Agent 准备期限 → 原生 agents[].override_setup_timeout_sec。 */
  agent_setup_sec?: number;
  /** Job 总期限 → Supervisor 的 max_wall_seconds。 */
  job_sec?: number;
}

export interface TerminalBenchResources {
  cpus?: number;
  memory_mb?: number;
  storage_mb?: number;
  gpus?: number;
}

/**
 * 凭据引用（review R2-06）：只有 `env:NAME` 形态，平台从不接收凭据值。
 * 元素形状就是 API 收口的 `{"ref": "env:VAR"}`，没有第二个字段。
 */
export interface TerminalBenchCredentialRef {
  ref: string;
}

export interface TerminalBenchRunRequest {
  /** 已发布的 ModelProfile id；oracle 可省略（真实 Agent 必填）。 */
  model?: string;
  agent_id: string;
  agent_version: string;
  n_trials: number;
  task_keys?: string[];
  dataset_revision?: string;
  aggregation?: "first-trial" | "mean-success";
  timeouts?: TerminalBenchTimeouts;
  resources?: TerminalBenchResources;
  /** 凭据引用：名称 → `{"ref": "env:VAR"}`；没有引用时整个字段省略（不下发明文字段）。 */
  credentials?: Record<string, TerminalBenchCredentialRef>;
}

/** Run 级成本视图（review R15）：per_success_usd 只在成本完整时有值。 */
export interface TerminalBenchCostView {
  known_cost_usd: number | null;
  known_trials: number;
  unknown_trials: number;
  unknown_cost: boolean;
  currency: string | null;
  per_success_usd: number | null;
  /** complete / unknown_cost / no_success / no_known_cost。 */
  per_success_usd_basis: string;
  /** 只覆盖「报道了成本」的 Trial 的成功成本小计，不是整个 Run 的单位成本。 */
  known_cost_subtotal_per_success_usd: number | null;
  successes: number;
  includes_failed_trials_in_numerator: boolean;
  note?: string | null;
}

/** GET /runs/{id}/tasks 的每行都带同一份全 Run 聚合（含 cost）。 */
export interface TerminalBenchRunAggregate {
  [key: string]: any;
  cost?: TerminalBenchCostView;
}

export interface TerminalBenchTaskRow {
  task_key: string;
  normalized_relative_path?: string | null;
  planned_trials: number;
  observed_trials: number;
  valid_trials: number;
  invalid_trials: number;
  valid_trial_pass_rate: number | null;
  valid_trial_coverage?: number | null;
  task_pass: boolean | null;
  task_pass_reason: string;
  /** run 级评分聚合（与服务端 scoring aggregate 同键，含 cost）。 */
  aggregate?: TerminalBenchRunAggregate;
}

export interface TerminalBenchTrialRow {
  trial_id: string;
  repeat_index: number | null;
  disposition: string;
  verifier_status: string;
  /** canonical reward 维度；缺失为 null（未知），不是 0。 */
  reward: number | null;
  valid: boolean;
  coverage?: Record<string, any>;
  source_trial_id: string | null;
}

/** 证据引用（artifact_refs 的元素）：身份 + hash + 完整度。 */
export interface TerminalBenchArtifact {
  artifact_id: string;
  kind: string;
  sha256?: string | null;
  size_bytes?: number | null;
  media_type?: string | null;
  source_path?: string | null;
  complete: boolean;
  truncated: boolean;
  note?: string | null;
}

/**
 * 单个工件/终端内容（GET .../trials/{id} 的 terminal 与 .../artifacts/{id}）。
 * ``verified=false`` 时 ``text`` 为 null 且必须显示 ``note``；``encoding="binary"``
 * 表示没有可读文本（不能伪造一段日志）。
 */
export interface TerminalBenchArtifactContent {
  artifact_id: string;
  kind?: string | null;
  sha256?: string | null;
  size_bytes?: number | null;
  media_type?: string | null;
  source_path?: string | null;
  encoding?: string | null;
  text: string | null;
  truncated: boolean;
  verified: boolean | null;
  note?: string | null;
}

export interface TerminalBenchTrialDetail {
  run_id: string;
  trial_id: string;
  task_key: string;
  repeat_index: number | null;
  disposition: string;
  termination: Record<string, any>;
  verifier_observation: {
    status: string;
    rewards?: Record<string, number>;
    error?: Record<string, any> | null;
    evidence_refs?: Array<Record<string, any>>;
  };
  usage: { cost_usd?: number | null; tokens?: Record<string, any> | null; coverage?: string };
  coverage: { items?: Record<string, string>; missing?: string[]; partial?: string[] };
  artifacts: TerminalBenchArtifact[];
  /** 终端/日志引用（没有可读内容时仍保留身份，便于人工核对冻结证据）。 */
  terminal_ref?: TerminalBenchArtifact | null;
  /** 终端文本（有界 + 脱敏）；没有引用或不可读时为 null。 */
  terminal?: TerminalBenchArtifactContent | null;
  evidence_complete: boolean;
  source_hash?: string | null;
  parser_version?: string | null;
}

export const getTerminalBenchOverview = () =>
  request<TerminalBenchOverview>("/api/v1/benchmarks/terminal-bench");

export const getTerminalBenchTasks = () =>
  request<{ items: TerminalBenchTask[]; total: number }>("/api/v1/benchmarks/terminal-bench/tasks");

export const getTerminalBenchPreflight = (params: {
  model?: string;
  n_trials?: number;
  task_keys?: string[];
  agent_id?: string;
  agent_version?: string;
  dataset_revision?: string;
  /** 凭据引用（`name=env:VAR`，逗号分隔）：与创建请求必须同一组，否则预检结论不作数。 */
  credential_refs?: string;
}) => {
  const query = new URLSearchParams();
  if (params.model !== undefined) query.set("model", params.model);
  if (params.n_trials !== undefined) query.set("n_trials", String(params.n_trials));
  if (params.task_keys?.length) query.set("task_keys", params.task_keys.join(","));
  if (params.agent_id !== undefined) query.set("agent_id", params.agent_id);
  if (params.agent_version !== undefined) query.set("agent_version", params.agent_version);
  if (params.dataset_revision) query.set("dataset_revision", params.dataset_revision);
  if (params.credential_refs) query.set("credential_refs", params.credential_refs);
  return request<TerminalBenchPreflightReport>(
    `/api/v1/benchmarks/terminal-bench/preflight?${query.toString()}`,
  );
};

/** 202：一次 Run 对应一个 Harbor Job，多 Task × 多 Trial 都在该 Job 内。 */
export const createTerminalBenchRun = (body: TerminalBenchRunRequest) =>
  request<RunRecord>("/api/v1/benchmarks/terminal-bench/runs", jsonBody(body));

export const getRunTasks = (runId: string) =>
  request<{ run_id: string; items: TerminalBenchTaskRow[]; total: number; status?: string }>(
    `/api/v1/runs/${runId}/tasks`,
  );

export const getRunTaskTrials = (runId: string, taskKey: string) =>
  request<{ run_id: string; task_key: string; items: TerminalBenchTrialRow[]; total: number }>(
    `/api/v1/runs/${runId}/tasks/${encodeURIComponent(taskKey)}/trials`,
  );

export const getRunTrial = (runId: string, trialId: string) =>
  request<TerminalBenchTrialDetail>(
    `/api/v1/runs/${runId}/trials/${encodeURIComponent(trialId)}`,
  );

/**
 * artifact_id 自带斜杠（冻结 bundle 内的相对路径）：逐段编码后拼进路径，
 * 既保留路径结构又不会让特殊字符跑到路径之外。
 */
export function encodeArtifactPath(artifactId: string): string {
  return artifactId.split("/").map((segment) => encodeURIComponent(segment)).join("/");
}

/** 按 Trial 归属读取一个 Artifact 的冻结内容（有界 + 脱敏；不属于该 Trial 时 404）。 */
export const getRunTrialArtifact = (runId: string, trialId: string, artifactId: string) =>
  request<TerminalBenchArtifactContent>(
    `/api/v1/runs/${runId}/trials/${encodeURIComponent(trialId)}`
    + `/artifacts/${encodeArtifactPath(artifactId)}`,
  );

// ---------------------------------------------------------------------------
// M5：场景 Workflow / Skill / Judge 资源（M5-T11）
//
// 采用 M5 计划第 10 节的拟议路由组（/api/v1/workflows、/api/v1/skills 的
// validate/test 子路径、/api/v1/judges）。服务端尚未注册的端点会返回 404，
// 页面统一按「能力不可用」如实渲染（src/components/capability.tsx）：入口保留、
// 明确禁用并给出原因，不伪造结果、也不静默隐藏。响应缺字段一律按「未知」处理。
// ---------------------------------------------------------------------------

export interface WorkflowStepRecord {
  step_id: string;
  kind: string;
  timeout_sec?: number | null;
  failure_policy?: string | null;
  /** invoke_fixture_tool 步骤的受控工具模式（real / mock / replay / deny）。 */
  tool_mode?: string | null;
  label?: string | null;
  [key: string]: unknown;
}

export interface WorkflowVersionRecord {
  workflow_id: string;
  version: string;
  schema_version?: number | null;
  description?: string | null;
  lifecycle?: string | null;
  published_at?: string | null;
  content_hash?: string | null;
  fixture_refs?: Array<Record<string, unknown>>;
  target_requirements?: Record<string, unknown> | null;
  steps?: WorkflowStepRecord[];
  completion_assertions?: Array<Record<string, unknown>>;
  limits?: Record<string, unknown> | null;
  failure_policy?: string | null;
}

/** 校验问题：locator 是字段路径（如 limits.max_turns、steps[0].kind）。 */
export interface ValidationIssue {
  locator: string;
  code: string;
  message: string;
}

/** 只读校验报告：不发布、不建 Run、不产生模型调用与费用。 */
export interface WorkflowValidationReport {
  ok: boolean;
  /** 逐字段问题；被服务端 4xx 拒绝时来自 error.fields，通过时为空数组。 */
  errors: ValidationIssue[];
  warnings?: ValidationIssue[];
  workflow_id?: string | null;
  version?: string | null;
  content_hash?: string | null;
  /** 服务端预检字段：publishable / executed 恒为可发布的预检语义。 */
  publishable?: boolean | null;
  executed?: boolean | null;
  ref?: string | null;
  step_count?: number | null;
  condition_count?: number | null;
  defaulted_fields?: string[];
}

/** 服务端成功响应形状（POST /workflows/validate）。 */
export interface WorkflowPreflightResponse {
  ok: boolean;
  publishable?: boolean | null;
  executed?: boolean | null;
  workflow_id?: string | null;
  version?: string | null;
  ref?: string | null;
  content_hash?: string | null;
  step_count?: number | null;
  condition_count?: number | null;
  defaulted_fields?: string[];
}

/**
 * 结构化校验拒绝（HTTP 422 等）→ 逐字段问题。
 * 服务端用 `error.fields`（field/code/message）表达契约错误；没有 fields 时
 * 退化为一份文档级问题。非校验类错误返回 null，页面照常显示真实错误。
 */
export function validationIssuesFromError(error: unknown, fallbackLocator = "workflow"): ValidationIssue[] | null {
  if (!(error instanceof ApiRequestError)) return null;
  if (error.status !== 422 && error.status !== 400) return null;
  const raw = (error.details as Record<string, any> | undefined)?.fields;
  const fields = Array.isArray(raw) ? raw : [];
  if (fields.length === 0) {
    return [{
      locator: fallbackLocator,
      code: error.code ?? "INVALID",
      message: error.message,
    }];
  }
  return fields.map((field: any) => ({
    locator: String(field?.field ?? field?.locator ?? fallbackLocator),
    code: String(field?.code ?? "INVALID"),
    message: String(field?.message ?? "服务端未给出字段说明"),
  }));
}

export const getWorkflows = () =>
  request<{ items: WorkflowVersionRecord[]; total: number }>("/api/v1/workflows");

export const getWorkflow = (workflowId: string, version: string) =>
  request<WorkflowVersionRecord>(
    `/api/v1/workflows/${encodeURIComponent(workflowId)}/${encodeURIComponent(version)}`,
  );

/** 只读校验：草案 JSON 原样送出；成功响应规范化为报告，4xx 由页面用 validationIssuesFromError 展开。 */
export const validateWorkflow = async (draft: Record<string, unknown>): Promise<WorkflowValidationReport> => {
  const response = await request<WorkflowPreflightResponse>("/api/v1/workflows/validate", jsonBody(draft));
  return {
    ok: response.ok === true,
    errors: [],
    warnings: [],
    workflow_id: response.workflow_id ?? null,
    version: response.version ?? null,
    content_hash: response.content_hash ?? null,
    publishable: response.publishable ?? null,
    executed: response.executed ?? null,
    ref: response.ref ?? null,
    step_count: response.step_count ?? null,
    condition_count: response.condition_count ?? null,
    defaulted_fields: response.defaulted_fields ?? [],
  };
};

/** 发布一个 Workflow 版本；内容相同时服务端按幂等策略返回原记录或冲突。 */
export const publishWorkflow = (draft: Record<string, unknown>) =>
  request<WorkflowVersionRecord>("/api/v1/workflows", jsonBody(draft));

/** 单步断言结果：locator / op 由服务端登记，页面只照实显示。 */
export interface ScenarioAssertionState {
  locator?: string | null;
  path?: string | null;
  op?: string | null;
  passed?: boolean | null;
  value?: unknown;
  reason?: string | null;
}

export interface ScenarioStepState {
  step_id: string;
  kind?: string | null;
  index?: number | null;
  seq?: number | null;
  /** 步骤状态；服务端未给出时页面显示「未知」，不推断成功。 */
  status?: string | null;
  tool_mode?: string | null;
  timeout_sec?: number | null;
  duration_ms?: number | null;
  attempts?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  checkpoint?: { label?: string | null; frozen?: boolean | null; state_hash?: string | null } | null;
  assertions?: ScenarioAssertionState[];
  error?: { code?: string | null; message?: string | null } | null;
  detail?: string | null;
  result?: unknown;
  /** true = 服务端明确标记该步结果未知；不得当作成功。 */
  unknown?: boolean | null;
}

export interface ScenarioFixtureState {
  fixture_id: string;
  version?: number | null;
  kind?: string | null;
  owner?: string | null;
  isolated?: boolean | null;
  prepare_error?: { code?: string | null; message?: string | null } | null;
  snapshot?: { complete?: boolean | null; ref?: string | null } | null;
  cleanup?: { status?: string | null; residual?: string[]; error?: string | null } | null;
}

export interface ScenarioRunStepsView {
  run_id: string;
  status?: string | null;
  workflow?: { workflow_id?: string | null; version?: string | null; content_hash?: string | null } | null;
  steps: ScenarioStepState[];
  fixtures?: ScenarioFixtureState[];
  unknown?: boolean | null;
}

/** 场景 Run 的逐步证据（步骤 / checkpoint / fixture 隔离与清理）。 */
export const getScenarioRunSteps = (runId: string) =>
  request<ScenarioRunStepsView>(`/api/v1/runs/${encodeURIComponent(runId)}/steps`);

export type SkillKind = "instruction" | "instruction_with_resources" | "executable" | string;

export interface SkillResourceRecord {
  path: string;
  sha256?: string | null;
  size_bytes?: number | null;
  media_type?: string | null;
}

export interface SkillCheckView {
  id?: string | null;
  locator?: string | null;
  passed?: boolean | null;
  message?: string | null;
}

/** 单个验证范围的结果：status 只认服务端登记值，未知字符串按原样显示。 */
export interface SkillVerificationScopeView {
  status?: string | null;
  scope?: string | null;
  ran_at?: string | null;
  checks?: SkillCheckView[];
  /** 能力不可用 / 未运行的原因；禁用入口必须显示它。 */
  reason?: string | null;
  /** fixture / 行为测试返回的既有 Run 与评分批次引用。 */
  run_id?: string | null;
  pass_id?: string | null;
  conditions?: Record<string, unknown> | null;
}

export interface SkillVerificationView {
  /** 静态校验：manifest / 资源哈希 / schema / 依赖固定 / 权限声明。 */
  static?: SkillVerificationScopeView | null;
  /** executable fixture：受控入口的输入输出与副作用。 */
  executable_fixture?: SkillVerificationScopeView | null;
  /** 固定 Agent / 模型 / 任务下的行为测试。 */
  behaviour?: SkillVerificationScopeView | null;
}

export interface SkillVersionRecord {
  /** 服务端可能用 skill_id 或 name 作为资源标识；两者都缺时页面显示未知。 */
  skill_id?: string | null;
  name?: string | null;
  version: string;
  kind: SkillKind;
  description?: string | null;
  lifecycle?: string | null;
  content_hash?: string | null;
  instruction_ref?: string | null;
  resource_manifest?: SkillResourceRecord[];
  dependency_refs?: Array<{ name?: string | null; version?: string | null; digest?: string | null }>;
  requested_permissions?: Record<string, unknown> | string[];
  input_schema?: unknown;
  output_schema?: unknown;
  fixture_refs?: Array<Record<string, unknown>>;
  injection_mode?: string | null;
  entrypoint?: { interpreter?: string | null; argv?: string[]; [key: string]: unknown } | null;
  verification?: SkillVerificationView | null;
}

/** 已发布 SkillVersion 目录（M5）：/api/v1/skills 是 v0 内存注册表，版本资源走 versions 子资源。 */
export const getSkills = () =>
  request<{ items: SkillVersionRecord[]; total: number }>("/api/v1/skills/versions");

export const getSkill = (skillId: string, version: string) =>
  request<SkillVersionRecord>(
    `/api/v1/skills/${encodeURIComponent(skillId)}/versions/${encodeURIComponent(version)}`,
  );

/**
 * 静态校验（只读）：请求体是 Skill 文档本身，不运行入口、不执行 fixture、
 * 零模型调用。服务端明确 validation_scope="static" 且 resource_bytes_verified=false
 * —— 资源字节核验属于 executable fixture 作用域，不能把它说成已执行（M5-A10）。
 */
export interface SkillValidationResult {
  ok: boolean;
  executed?: boolean | null;
  validation_scope?: string | null;
  resource_bytes_verified?: boolean | null;
  skill_id?: string | null;
  version?: string | null;
  ref?: string | null;
  kind?: string | null;
  lifecycle?: string | null;
  content_hash?: string | null;
  executable?: boolean | null;
  dependency_refs?: Array<Record<string, unknown>>;
  resource_paths?: string[];
  fixture_refs?: Array<Record<string, unknown>>;
  requested_permissions?: Record<string, unknown>;
  defaulted_fields?: string[];
}

export const validateSkill = (document: Record<string, unknown>) =>
  request<SkillValidationResult>("/api/v1/skills/validate", jsonBody(document));

/** executable fixture：返回既有 Run 引用，不新建第二套执行对象。 */
export const testSkillFixture = (body: { skill_id: string; version: string; fixture_id?: string }) =>
  request<SkillVerificationScopeView>("/api/v1/skills/fixture-tests", jsonBody(body));

/** 固定 Agent / 模型 / 任务的行为测试：条件必须显式给出。 */
export const testSkillBehaviour = (body: {
  skill_id: string; version: string; agent_id: string; model: string; case_ids?: string[];
}) => request<SkillVerificationScopeView>("/api/v1/skills/behaviour-tests", jsonBody(body));

export interface JudgeCostView {
  /** 已报道成本（USD）；未知时必须是 null，不得填 0。 */
  reported_usd?: number | null;
  estimated_usd?: number | null;
  currency?: string | null;
  known_calls?: number | null;
  unknown_calls?: number | null;
  unknown_cost?: boolean | null;
  price_table_version?: string | null;
}

export interface JudgeCriterionView {
  id?: string;
  criterion_id?: string;
  description?: string | null;
  scale?: string | null;
  weight?: number | null;
}

export interface JudgeRubricView {
  rubric_id: string;
  version: string;
  content_sha256?: string | null;
  scale?: string | null;
  criteria?: JudgeCriterionView[];
  missing_evidence_policy?: string | null;
}

export interface JudgeCalibrationView {
  /** calibrated / experimental / not_run / unavailable；未知字符串按原样显示。 */
  status?: string | null;
  calibration_version?: string | null;
  samples?: number | null;
  human_reviewed?: number | null;
  required_samples?: number | null;
  policy?: Record<string, unknown> | null;
  disagreements?: Array<{ criterion?: string | null; metric?: string | null; value?: number | null; basis?: string | null }>;
  position_swap?: Record<string, unknown> | null;
  repeat_stability?: Record<string, unknown> | null;
  reasons?: string[];
  /** true = 只有合成 fixture 标签，不构成人类质量真值。 */
  synthetic_only?: boolean | null;
  calibrated_at?: string | null;
}

export interface JudgeSpecView {
  judge_id: string;
  version: string;
  lifecycle?: string | null;
  /** Judge 固定 profile 的模型（独立于 subject 模型）。 */
  model?: string | null;
  provider?: string | null;
  spec_sha256?: string | null;
  /** 允许的用途（mode）清单，付费提交的用途选项来自这里。 */
  modes?: string[];
  budget?: {
    max_calls?: number | null;
    max_prompt_tokens?: number | null;
    max_completion_tokens?: number | null;
    max_total_tokens?: number | null;
    hard_cost_cap_usd?: number | null;
  } | null;
  evidence?: {
    selector?: Record<string, unknown> | null;
    case_ids?: string[];
    observation_refs?: string[];
    missing_policy?: string | null;
  } | null;
  rubric?: JudgeRubricView | null;
  calibration?: JudgeCalibrationView | null;
  cost?: JudgeCostView | null;
  spec?: JudgeSpecRequest["spec"] | null;
}

/** Judge 预检（只读）：返回将调用的 profile、最大次数与费用是否可估计。 */
export interface JudgePreflightView {
  mode?: string | null;
  model?: string | null;
  spec_sha256?: string | null;
  sample_count?: number | null;
  repeats?: number | null;
  orderings?: number | null;
  max_calls?: number | null;
  token_ceiling?: {
    prompt?: number | null; completion?: number | null; total?: number | null;
    budget_prompt?: number | null; budget_completion?: number | null;
  } | null;
  price_coverage?: {
    known?: boolean | null;
    price_table_version?: string | null;
    input_per_million?: number | null;
    output_per_million?: number | null;
    estimated_cost_usd?: number | null;
  } | null;
  authorised?: boolean | null;
  budget_executable?: boolean | null;
  hard_monetary_cap?: boolean | null;
  reasons?: string[];
  authorisation?: Record<string, unknown> | null;
}

export interface JudgeSpecRequest {
  judge_profile_id: string;
  model: string;
  rubric_id: string;
  rubric_version: string;
  criteria?: string[];
  parameters?: Record<string, unknown>;
  input_selector?: Record<string, unknown> | null;
  missing_evidence_policy?: "insufficient_evidence" | "not_applicable" | "fail" | null;
  calibration_version?: string | null;
  budget: {
    max_calls: number;
    max_prompt_tokens?: number;
    max_completion_tokens?: number;
    hard_cost_cap_usd?: number | null;
  };
}

export interface JudgePreflightRequest {
  run_id: string;
  mode: "single" | "pairwise";
  spec: JudgeSpecRequest;
  source_pass_id?: string;
  repeats?: number;
  case_ids?: string[];
  authorisation?: JudgeAuthorisationRequest | null;
  publish_policy?: "all_scored" | "allow_non_scored";
}

export interface JudgeAuthorisationRequest {
  authorised: true;
  actor: string;
  max_calls: number;
  max_total_tokens?: number | null;
  hard_cost_cap_usd?: number | null;
}

export interface JudgeSubmitRequest extends JudgePreflightRequest {
  request_key: string;
  /** 显式授权：没有它服务端必须零调用。 */
  authorisation: JudgeAuthorisationRequest;
}

export interface JudgeJobRecord {
  job_id: string;
  status: string;
  judge_id?: string | null;
  version?: string | null;
  purpose?: string | null;
  model?: string | null;
  run_id?: string | null;
  scoring_pass_id?: string | null;
  created_at?: string | null;
  max_calls?: number | null;
  calls_made?: number | null;
  cost?: JudgeCostView | null;
  cancellation?: { requested_at?: string | null; state?: string | null } | null;
  reasons?: string[];
  error?: { code?: string | null; message?: string | null } | null;
}

export const getJudges = () =>
  request<{ items: JudgeSpecView[]; total: number }>("/api/v1/judge-specs");

export const getJudge = (judgeId: string, version?: string) =>
  request<JudgeSpecView>(
    `/api/v1/judge-specs/${encodeURIComponent(judgeId)}${version ? `?version=${encodeURIComponent(version)}` : ""}`,
  );

/** 校准报告（只读，零模型调用）。 */
export const getJudgeCalibration = (judgeId: string, version?: string) =>
  request<JudgeCalibrationView>(
    `/api/v1/judge-specs/${encodeURIComponent(judgeId)}/calibration${version ? `?version=${encodeURIComponent(version)}` : ""}`,
  );

/** 只读预检：只算预算与费用覆盖，不发送任何 Judge 调用。 */
export const preflightJudge = (body: JudgePreflightRequest) =>
  request<JudgePreflightView>("/api/v1/judges/preflight", jsonBody(body));

/** 付费提交：仅在操作员显式确认后调用；成功后返回同一 ScoringJob / pass 引用。 */
export const submitJudgeJob = (body: JudgeSubmitRequest) =>
  request<JudgeJobRecord>("/api/v1/judges", jsonBody(body));

export const getJudgeJob = (jobId: string) =>
  request<JudgeJobRecord>(`/api/v1/judges/${encodeURIComponent(jobId)}`);

/** 幂等取消：重复取消返回同一状态，不影响已完成的评分批次。 */
export const cancelJudgeJob = async (jobId: string) => {
  const result = await request<{ job: JudgeJobRecord }>(
    `/api/v1/judges/${encodeURIComponent(jobId)}/cancel`,
    jsonBody({}),
  );
  return result.job;
};

// ---------------------------------------------------------------------------
// M6：实验 / 比较 / Baseline / 门禁（experiments-and-comparison 协议）。
//
// compare / snapshot / gate / export 一律只读，零模型 / Judge / Runner 调用；
// Baseline 与 GatePolicy 发布后不可变（同 id 异内容 409），默认 baseline 指针
// 用 expected_current 做 CAS。服务端尚未注册的端点（404/405/501）由页面按
// 「能力不可用」渲染（src/components/capability.tsx）；实体 404（*_NOT_FOUND）
// 是普通错误，不按能力不可用处理。缺字段一律显示「未知」，不填 0。
// ---------------------------------------------------------------------------

/** 比较政策允许显式放行的因子（motte_contracts.comparison 冻结集合）。 */
export const COMPARISON_FACTORS: readonly string[] = [
  "model", "agent_id", "agent_version", "n_trials", "timeouts", "resources",
  "tools", "retries", "environment", "credentials", "runtime", "intervention",
  "workflow", "fixture", "skill", "judge", "rubric", "calibration", "budget_policy",
];

/** 三级比较结论（GET /comparisons）：结构性原因与指标原因分开。 */
export interface ComparabilityView {
  /** 兼容字段：质量指标是否可比（level ∈ comparable/partially 且质量 metric eligible）。 */
  eligible: boolean;
  /** comparable | partially_comparable | not_comparable；未知字符串按原样显示。 */
  level: string;
  reasons?: string[];
  structural_reasons: string[];
  metric_reasons: string[];
  metric_eligibility: Record<string, boolean>;
  case_diff: { added: string[]; removed: string[]; changed: string[] };
  /** 政策显式允许的差异（本身可审计）。 */
  allowed_differences?: string[];
}

export const compareRunReports = (params: {
  baseline: string;
  candidate: string;
  factors?: string[];
  baseline_pass?: string;
  candidate_pass?: string;
}) => {
  const query = new URLSearchParams({
    baseline: params.baseline,
    candidate: params.candidate,
  });
  query.set("factors", (params.factors ?? ["model"]).join(","));
  if (params.baseline_pass) query.set("baseline_pass", params.baseline_pass);
  if (params.candidate_pass) query.set("candidate_pass", params.candidate_pass);
  return request<ComparabilityView>(`/api/v1/comparisons?${query.toString()}`);
};

/** 回归分类（GET /regressions）：结论照实渲染，页面不自己下结论。 */
export const getRegressionClassification = (params: {
  baseline: string;
  candidate: string;
  baseline_pass?: string;
  candidate_pass?: string;
}) => {
  const query = new URLSearchParams({
    baseline: params.baseline,
    candidate: params.candidate,
  });
  if (params.baseline_pass) query.set("baseline_pass", params.baseline_pass);
  if (params.candidate_pass) query.set("candidate_pass", params.candidate_pass);
  return request<Record<string, any>>(`/api/v1/regressions?${query.toString()}`);
};

/** ReportSnapshot（GET /runs/{id}/report-snapshot）：固定报告的冻结视图。 */
export interface ReportSnapshotView {
  snapshot_id: string;
  ref: { run_id: string; scoring_pass_id: string; report_schema?: string; evidence_hash?: string };
  created_at?: string | null;
  suite?: string | null;
  denominator?: string | null;
  /** 9 个 disposition 计数键（协议 §2 不变量）。 */
  counts: Record<string, number>;
  coverage?: number | null;
  /** null = 该指标资格不足（缺样本 / 未评分），不得显示成 0。 */
  metric_values: Record<string, number | null>;
  metric_registry_version?: string | null;
  cost?: {
    entries?: Array<{ scope?: string; currency?: string; amount?: number }>;
    unknown_usage_count?: number;
    source?: string | null;
    usage_coverage?: number | null;
  } | null;
  evidence_pins?: Array<{ artifact_id: string; sha256: string }>;
  case_dispositions?: Array<{ case_id: string; disposition: string; detail?: string | null }>;
}

export const getReportSnapshot = (runId: string, scoringPassId?: string) =>
  request<ReportSnapshotView>(
    `/api/v1/runs/${encodeURIComponent(runId)}/report-snapshot`
    + (scoringPassId ? `?scoring_pass_id=${encodeURIComponent(scoringPassId)}` : ""),
  );

// -- Baseline -------------------------------------------------------------

export interface BaselineEntryView {
  /** null = 单 Run baseline；非空 = 按 cell 条件映射。 */
  cell_key: string | null;
  ref: { run_id: string; scoring_pass_id: string } & Record<string, any>;
}

/** BaselineSnapshot（协议 §4）：固定 entries + 政策 hash + 资格，写入后不可变。 */
export interface BaselineSnapshotView {
  baseline_id: string;
  entries: BaselineEntryView[];
  comparison_policy_hash: string;
  /** formal | diagnostic；ineligible 在创建时即被拒绝。 */
  eligibility: string;
  created_by: string;
  reason: string;
  source_note?: string | null;
  created_at?: string | null;
  metrics: Record<string, number | null>;
}

/** 默认 baseline 指针（scope → snapshot）：CAS 更新 + 审计，不覆盖历史。 */
export interface DefaultBaselinePointerView {
  scope: string;
  baseline_id: string;
  updated_by: string;
  reason: string;
  comparison_policy_hash: string;
  updated_at?: string | null;
  position?: number | null;
}

export const getBaselines = (limit = 100) =>
  request<{ items: BaselineSnapshotView[]; total: number }>(
    `/api/v1/baselines?limit=${limit}`,
  );

export const createBaseline = (body: {
  baseline_id: string;
  entries: Array<{ cell_key?: string | null; run_id: string; scoring_pass_id: string }>;
  policy?: Record<string, unknown>;
  created_by?: string;
  reason?: string;
  source_note?: string | null;
}) => request<BaselineSnapshotView>("/api/v1/baselines", jsonBody(body));

export const getBaseline = (baselineId: string) =>
  request<BaselineSnapshotView>(`/api/v1/baselines/${encodeURIComponent(baselineId)}`);

export const getDefaultBaseline = (scope: string) =>
  request<{ scope: string; pointer: DefaultBaselinePointerView | null }>(
    `/api/v1/baselines/default?scope=${encodeURIComponent(scope)}`,
  );

/** CAS 切换默认指针：expected_current 缺省且已有指针时 409 CAS_CONFLICT。 */
export const setDefaultBaseline = (body: {
  scope: string;
  baseline_id: string;
  expected_current?: string | null;
  updated_by: string;
  reason: string;
}) => request<DefaultBaselinePointerView>("/api/v1/baselines/default", jsonBody(body));

// -- GatePolicy / 版本化求值 / 导出 ----------------------------------------

/** GatePolicyVersion（协议 §9）：published 后不可变；弃用只改 lifecycle。 */
export interface GatePolicyView {
  policy_id: string;
  version: string;
  /** draft | published | deprecated。 */
  lifecycle?: string;
  rules: Array<Record<string, any>>;
  diagnostic?: boolean;
  created_by?: string;
  reason?: string;
  created_at?: string | null;
  [key: string]: any;
}

export const getGatePolicies = () =>
  request<{ items: GatePolicyView[]; total: number }>("/api/v1/gate-policies");

export const publishGatePolicy = (draft: Record<string, unknown>) =>
  request<GatePolicyView>("/api/v1/gate-policies", jsonBody(draft));

export const deprecateGatePolicy = (policyId: string, version: string) =>
  request<GatePolicyView>(
    `/api/v1/gate-policies/${encodeURIComponent(policyId)}/${encodeURIComponent(version)}/deprecate`,
    jsonBody({}),
  );

/** 逐规则求值结果：status ∈ pass/fail/insufficient/not_applicable/skipped_diagnostic。 */
export interface GateRuleResultView {
  rule_id: string;
  kind: string;
  status: string;
  severity?: string;
  decision?: string | null;
  reason: string;
  diagnostic_skipped?: boolean;
}

/** GateResult（POST /gates/versioned 与 GET /gates/results/{id}）：追加只读。 */
export interface GateResultFullView {
  gate_result_id: string;
  policy_id: string;
  policy_version: string;
  policy_content_hash?: string;
  baseline?: { baseline_id?: string; run_id?: string; scoring_pass_id?: string } | null;
  candidates?: Array<Record<string, any>>;
  /** 六类决策（协议 §6）。 */
  decision: string;
  rule_results: GateRuleResultView[];
  evaluation_input_hash?: string;
  result_semantics_hash?: string;
  conclusion_hash?: string;
  evaluated_at?: string | null;
  suggested_actions?: string[];
  /** 决策 → 退出码摘要（协议 §7）；服务端未附带时为 null（页面显示未知）。 */
  exit_code?: number | null;
}

export const evaluateVersionedGate = (body: {
  run_id: string;
  policy_id: string;
  policy_version: string;
  scoring_pass_id?: string;
  baseline_id?: string;
  allowed_factors?: string[];
}) => request<GateResultFullView>("/api/v1/gates/versioned", jsonBody(body));

export const getGateResult = (gateResultId: string) =>
  request<GateResultFullView>(`/api/v1/gates/results/${encodeURIComponent(gateResultId)}`);

/** 导出端点直接交给浏览器下载（JSON canonical dump / 最小 JUnit XML）。 */
export function gateResultExportUrl(gateResultId: string, format: "json" | "junit"): string {
  return `/api/v1/gates/results/${encodeURIComponent(gateResultId)}/export?format=${format}`;
}

// -- Experiment -----------------------------------------------------------

/** 预览违规（MATRIX_TOO_LARGE / BUDGET_EXCEEDED 等）：非空时创建必须禁用。 */
export interface ExperimentPreviewViolation {
  code: string;
  message: string;
  [key: string]: unknown;
}

/** 零创建预览（POST /experiments/preview）：矩阵展开 + 护栏，不触碰存储。 */
export interface ExperimentPreviewView {
  experiment_id: string;
  version: string;
  cells: Array<{ cell_id: string; factor_assignment: Record<string, any>; repeat_index: number }>;
  cell_count: number;
  max_potential_calls: number;
  budget?: Record<string, any>;
  violations: ExperimentPreviewViolation[];
}

export interface ExperimentCellView {
  cell_id: string;
  experiment_id?: string;
  experiment_version?: string | number;
  repeat_index: number;
  factor_assignment: Record<string, any>;
  /** pending | allocating | allocated | failed | cancelled。 */
  allocation_status: string;
  run_id?: string | null;
  superseding_run_ids?: string[];
  failure_reason?: string | null;
}

export interface ExperimentStatusView {
  experiment_id: string;
  version: string;
  spec: Record<string, any>;
  cell_count: number;
  progress: Record<string, number>;
  cells: ExperimentCellView[];
}

/** POST /experiments（202）：发布 spec（幂等）+ 铺 cell + 分配的结果。 */
export interface ExperimentCreateOutcome extends ExperimentStatusView {
  created?: boolean;
  allocated?: number;
  skipped_existing?: number;
  failed?: Array<{ cell_id: string; reason: string }>;
}

export const previewExperiment = (specPayload: Record<string, unknown>) =>
  request<ExperimentPreviewView>("/api/v1/experiments/preview", jsonBody(specPayload));

export const createExperiment = (
  specPayload: Record<string, unknown>,
  requestKey?: string,
) =>
  request<ExperimentCreateOutcome>(
    "/api/v1/experiments",
    jsonBody(requestKey ? { ...specPayload, _request_key: requestKey } : specPayload),
  );

export const allocateExperiment = (experimentId: string, version?: string) =>
  request<ExperimentCreateOutcome>(
    `/api/v1/experiments/${encodeURIComponent(experimentId)}/allocate`,
    jsonBody(version ? { version } : {}),
  );

export const getExperiment = (experimentId: string, version?: string) =>
  request<ExperimentStatusView>(
    `/api/v1/experiments/${encodeURIComponent(experimentId)}`
    + (version ? `?version=${encodeURIComponent(version)}` : ""),
  );

/** 只作用于本实验：pending/allocating cell 落 cancelled，自有 Run 走取消。 */
export const cancelExperiment = (
  experimentId: string,
  body?: { version?: string; reason?: string },
) =>
  request<ExperimentStatusView & {
    reason?: string;
    cancelled_at?: string;
    cancelled_cells?: string[];
    cancelled_runs?: string[];
  }>(
    `/api/v1/experiments/${encodeURIComponent(experimentId)}/cancel`,
    jsonBody(body ?? {}),
  );

/** 显式重试：superseding 子 Run（parent 指向原 Run），原结果不消失。 */
export const retryExperimentCell = (cellId: string, reason?: string) =>
  request<{
    cell_id: string;
    run_id: string;
    parent_run_id?: string | null;
    superseding_run_ids?: string[];
    allocation_status?: string;
    reason?: string;
    retried_at?: string;
  }>(
    `/api/v1/experiments/cells/${encodeURIComponent(cellId)}/retry`,
    jsonBody(reason ? { reason } : {}),
  );
