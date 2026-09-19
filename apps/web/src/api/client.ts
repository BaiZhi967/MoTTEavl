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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = payload?.error?.message || payload?.detail || `HTTP ${response.status}`;
    throw new Error(message);
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

export const createRun = (body: { scenario_version: string; manifest?: any; case_ids?: string[] }) =>
  request<RunRecord>("/api/v1/runs", jsonBody(body));

export const getRun = (id: string) => request<RunRecord>(`/api/v1/runs/${id}`);

export const cancelRun = (id: string, reason?: string) =>
  request<RunRecord>(`/api/v1/runs/${id}/cancel`, jsonBody({ reason }));

export const retryRun = (id: string) => request<RunRecord>(`/api/v1/runs/${id}/retry`, jsonBody({}));

export const rescoreRun = (id: string) => request<RunRecord>(`/api/v1/runs/${id}/rescore`, jsonBody({}));

export const replayRun = (id: string, cases: Record<string, any>) =>
  request<RunRecord>(`/api/v1/runs/${id}/replay`, jsonBody({ cases }));

export const getReport = (id: string) => request<RunReport>(`/api/v1/runs/${id}/report`);

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

/** 订阅运行事件流；浏览器重连时自动携带 Last-Event-ID，服务端按 seq 续传。 */
export function subscribeRunEvents(
  runId: string,
  handlers: { onEvent: (event: TraceEvent) => void; onError?: () => void },
): () => void {
  const source = new EventSource(`/api/v1/runs/${runId}/events`);
  source.onmessage = (message) => {
    try {
      handlers.onEvent(JSON.parse(message.data));
    } catch {
      // 忽略无法解析的帧
    }
  };
  source.onerror = () => handlers.onError?.();
  return () => source.close();
}
