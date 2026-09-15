export interface Score {
  case_id: string;
  passed: boolean;
}

export interface CaseRun {
  case_id: string;
  result: any;
  expected?: any;
}

export interface RunRecord {
  id: string;
  scenario_version: string;
  status: string;
  manifest?: any;
  case_ids?: string[];
  cases?: CaseRun[];
  scores?: Score[];
  parent_run_id?: string;
  cancellation?: { reason?: string };
  error?: any;
}

export interface ProviderRecord {
  name: string;
  kind: string;
  base_url?: string;
  credentials?: string;
  api_key_env?: string;
  [key: string]: any;
}

export interface ModelRecord {
  id: string;
  provider: string;
  model?: string | null;
  capabilities: Record<string, any>;
  context_window?: number | null;
  supports_tools?: boolean;
}

export interface HarnessReport {
  name: string;
  binary: string;
  installed: boolean;
  version: string | null;
  source: string | null;
  path: string | null;
  runnable: boolean;
  error?: string | null;
}

export interface RunReport {
  run_id: string;
  scenario_version: string;
  status: string;
  generated_at: string;
  summary: { cases: number; scored: number; passed: number; failed: number; pass_rate: number | null };
  cost: { total: number | null; price_table_versions: string[] };
  scores: Score[];
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

const jsonBody = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

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
export const deleteProvider = (name: string) =>
  request<{ deleted: string }>(`/api/v1/providers/${name}`, { method: "DELETE" });

export const getModels = () => request<{ items: ModelRecord[] }>(`/api/v1/models`);
export const createModel = (body: any) => request<ModelRecord>("/api/v1/models", jsonBody(body));
export const deleteModel = (id: string) => request<{ deleted: string }>(`/api/v1/models/${id}`, { method: "DELETE" });

export const getScenarios = () => request<{ items: any[] }>(`/api/v1/scenarios`);
export const createScenario = (body: any) => request<any>("/api/v1/scenarios", jsonBody(body));

export const getAgents = () =>
  request<{ items: { id: string; kind: string; description: string }[] }>(`/api/v1/agents`);
export const getHarnesses = () => request<{ items: HarnessReport[] }>(`/api/v1/harnesses`);

// ---------------------------------------------------------------- SSE

export interface TraceEvent {
  run_id: string;
  seq: number;
  type: string;
  [key: string]: any;
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
