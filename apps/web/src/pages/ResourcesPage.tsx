import { Fragment, useCallback, useEffect, useState, type FormEvent } from "react";
import * as Switch from "@radix-ui/react-switch";
import { KeyIcon, PencilSimpleIcon, PlayIcon, PlusIcon, TrashIcon } from "@phosphor-icons/react";
import {
  createModel,
  createProvider,
  deleteModel,
  deleteProvider,
  getAgents,
  getCredentials,
  getHarnesses,
  getModels,
  getProviderKinds,
  getProviders,
  setCredential,
  testModel,
  type CredentialSummary,
  type HarnessReport,
  type ModelRecord,
  type ModelTestResult,
  type ProviderKindMeta,
  type ProviderRecord,
} from "../api/client";

/** 目录接口不可用时的兜底：至少能创建本地兼容端点。 */
const FALLBACK_KINDS: ProviderKindMeta[] = [
  {
    kind: "openai_compatible",
    label: "OpenAI 兼容",
    description: "任意兼容 Chat Completions 的端点（vLLM、Ollama、网关等）",
    default_base_url: null,
    default_key_env: "OPENAI_API_KEY",
  },
];

/** Provider 与其模型合并管理：连接（协议 / Base URL / 凭据）在 Provider 上，模型挂在 Provider 下。 */
export function ProvidersPage() {
  const [kinds, setKinds] = useState<ProviderKindMeta[]>(FALLBACK_KINDS);
  const [providers, setProviders] = useState<ProviderRecord[]>([]);
  const [models, setModels] = useState<ModelRecord[]>([]);
  const [credentialSummaries, setCredentialSummaries] = useState<CredentialSummary[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState(FALLBACK_KINDS[0].kind);
  const [baseUrl, setBaseUrl] = useState("");
  const [credentials, setCredentials] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState("");

  const kindMeta = kinds.find((entry) => entry.kind === kind);
  const defaultUrls = kinds
    .map((entry) => entry.default_base_url)
    .filter((url): url is string => Boolean(url));

  const refresh = useCallback(async () => {
    try {
      const [providerPayload, modelPayload, credentialPayload] = await Promise.all([
        getProviders(),
        getModels(),
        getCredentials().catch(() => ({ items: [] as CredentialSummary[] })),
      ]);
      setProviders(providerPayload.items);
      setModels(modelPayload.items);
      setCredentialSummaries(credentialPayload.items);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
    getProviderKinds()
      .then((payload) => setKinds(payload.items))
      .catch(() => undefined);
  }, [refresh]);

  /** 切换协议时同步默认端点：只在输入为空或仍是另一个 kind 的默认值时替换，不覆盖手输内容。 */
  const changeKind = (nextKind: string) => {
    setKind(nextKind);
    const nextDefault = kinds.find((entry) => entry.kind === nextKind)?.default_base_url ?? "";
    if (!baseUrl || defaultUrls.includes(baseUrl)) {
      setBaseUrl(nextDefault);
    }
  };

  const submit = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setError("");
    try {
      const profile = credentials || name;
      if (apiKey) {
        await setCredential(profile, apiKey);
      }
      await createProvider({
        name,
        kind,
        base_url: baseUrl,
        ...(credentials ? { credentials } : {}),
      });
      setName("");
      setCredentials("");
      setApiKey("");
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <form className="panel form-panel" onSubmit={submit} aria-label="创建 Provider">
        <h2>创建 Provider</h2>
        <label>
          协议类型
          <select value={kind} onChange={(change) => changeKind(change.target.value)}>
            {kinds.map((entry) => (
              <option key={entry.kind} value={entry.kind}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>
        {kindMeta?.description && <p className="hint">{kindMeta.description}</p>}
        <label>
          名称
          <input value={name} onChange={(change) => setName(change.target.value)} placeholder="my-provider" required />
        </label>
        <label>
          Base URL
          <input
            value={baseUrl}
            onChange={(change) => setBaseUrl(change.target.value)}
            placeholder={kindMeta?.default_base_url ?? "http://localhost:8001/v1"}
            required
          />
        </label>
        <label>
          凭据 profile（留空则与名称相同）
          <input value={credentials} onChange={(change) => setCredentials(change.target.value)} placeholder="my-provider" />
        </label>
        <label>
          API Key（可选；写入服务器凭据文件，不回显、不入库）
          <input
            type="password"
            autoComplete="new-password"
            value={apiKey}
            onChange={(change) => setApiKey(change.target.value)}
            placeholder="sk-…"
          />
        </label>
        <p className="hint">
          API Key 会写入服务器凭据文件（0600），与 <code>python -m motte_cli credentials set {"<profile>"}</code> 等价
          {kindMeta?.default_key_env && <>；未填写时回退环境变量 <code>{kindMeta.default_key_env}</code></>}
        </p>
        <button type="submit">创建</button>
        {error && <p className="error">{error}</p>}
      </form>

      <section className="panel">
        <h2>Provider 与模型</h2>
        {!loaded && providers.length === 0 ? (
          <p className="empty">加载中</p>
        ) : providers.length === 0 ? (
          <p className="empty">暂无 Provider，在左侧创建第一个连接</p>
        ) : (
          providers.map((provider) => (
            <ProviderSection
              key={provider.name}
              provider={provider}
              models={models.filter((model) => model.provider === provider.name)}
              credentialHint={
                credentialSummaries.find((entry) => entry.profile === (provider.credentials ?? provider.name))
                  ?.key_hint
              }
              onChanged={refresh}
            />
          ))
        )}
      </section>
    </div>
  );
}

/** 模型表单的参数列摘要：temp 0.2 top_p 0.9 max 4096；空则返回 null。 */
function parameterSummary(parameters?: ModelRecord["parameters"]) {
  if (!parameters) {
    return null;
  }
  const parts: string[] = [];
  if (parameters.temperature != null) parts.push(`temp ${parameters.temperature}`);
  if (parameters.top_p != null) parts.push(`top_p ${parameters.top_p}`);
  if (parameters.max_output_tokens != null) parts.push(`max ${parameters.max_output_tokens}`);
  return parts.length ? parts.join("  ") : null;
}

function ProviderSection({
  provider,
  models,
  credentialHint,
  onChanged,
}: {
  provider: ProviderRecord;
  models: ModelRecord[];
  credentialHint?: string;
  onChanged: () => Promise<void>;
}) {
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [modelId, setModelId] = useState("");
  const [modelName, setModelName] = useState("");
  const [contextWindow, setContextWindow] = useState("");
  const [temperature, setTemperature] = useState("");
  const [topP, setTopP] = useState("");
  const [maxOutputTokens, setMaxOutputTokens] = useState("");
  const [supportsTools, setSupportsTools] = useState(false);
  const [keyEditing, setKeyEditing] = useState(false);
  const [keyInput, setKeyInput] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [tests, setTests] = useState<Record<string, ModelTestResult | "running">>({});
  const [error, setError] = useState("");

  const resetModelForm = () => {
    setModelId("");
    setModelName("");
    setContextWindow("");
    setTemperature("");
    setTopP("");
    setMaxOutputTokens("");
    setSupportsTools(false);
    setEditing(null);
  };

  const startAdd = () => {
    resetModelForm();
    setAdding(true);
  };

  const startEdit = (model: ModelRecord) => {
    setModelId(model.id);
    setModelName(model.model ?? "");
    setContextWindow(model.context_window != null ? String(model.context_window) : "");
    setTemperature(model.parameters?.temperature != null ? String(model.parameters.temperature) : "");
    setTopP(model.parameters?.top_p != null ? String(model.parameters.top_p) : "");
    setMaxOutputTokens(
      model.parameters?.max_output_tokens != null ? String(model.parameters.max_output_tokens) : "",
    );
    setSupportsTools(Boolean(model.supports_tools));
    setEditing(model.id);
    setAdding(true);
  };

  const submitModel = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setError("");
    const parameters: Record<string, number> = {};
    if (temperature) parameters.temperature = Number(temperature);
    if (topP) parameters.top_p = Number(topP);
    if (maxOutputTokens) parameters.max_output_tokens = Number(maxOutputTokens);
    try {
      await createModel({
        id: modelId,
        provider: provider.name,
        ...(modelName ? { model: modelName } : {}),
        capabilities: { text: true },
        ...(contextWindow ? { context_window: Number(contextWindow) } : {}),
        supports_tools: supportsTools,
        ...(Object.keys(parameters).length ? { parameters } : {}),
      });
      resetModelForm();
      setAdding(false);
      await onChanged();
    } catch (e) {
      setError(String(e));
    }
  };

  const runTest = async (id: string) => {
    setError("");
    setTests((current) => ({ ...current, [id]: "running" }));
    try {
      const report = await testModel(id);
      setTests((current) => ({ ...current, [id]: report }));
    } catch (e) {
      setTests((current) => ({
        ...current,
        [id]: { ok: false, provider: provider.kind, model: id, tested_at: "", error: { message: String(e) } },
      }));
    }
  };

  const removeModel = async (id: string) => {
    setError("");
    try {
      await deleteModel(id);
      await onChanged();
    } catch (e) {
      setError(String(e));
    }
  };

  const saveKey = async () => {
    setError("");
    try {
      await setCredential(provider.credentials ?? provider.name, keyInput);
      setKeyEditing(false);
      setKeyInput("");
      await onChanged();
    } catch (e) {
      setError(String(e));
    }
  };

  const removeProviderWithModels = async () => {
    setError("");
    try {
      await Promise.all(models.map((model) => deleteModel(model.id)));
      await deleteProvider(provider.name);
      await onChanged();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="provider-block">
      <header className="provider-head">
        <div>
          <strong className="mono">{provider.name}</strong>
          <span className="status-badge status-tone-neutral kind-badge">{provider.kind}</span>
        </div>
        <div className="provider-meta">
          <span className="mono">{provider.base_url ?? "—"}</span>
          <span>凭据 {provider.credentials ?? provider.name}</span>
          {credentialHint && (
            <span>
              密钥 <span className="mono">{credentialHint}</span>
            </span>
          )}
        </div>
        <div className="provider-actions">
          <button className="link" onClick={startAdd}>
            <PlusIcon size={14} weight="bold" aria-hidden />
            添加模型
          </button>
          <button className="link" onClick={() => setKeyEditing((open) => !open)}>
            <KeyIcon size={14} weight="bold" aria-hidden />
            更新密钥
          </button>
          {confirmDelete ? (
            <>
              <button
                className="link danger"
                title={`删除 ${provider.name} 及其 ${models.length} 个模型`}
                onClick={() => void removeProviderWithModels()}
              >
                确认删除
              </button>
              <button className="link" onClick={() => setConfirmDelete(false)}>
                取消
              </button>
            </>
          ) : (
            <button className="link danger" onClick={() => setConfirmDelete(true)}>
              <TrashIcon size={14} weight="bold" aria-hidden />
              删除
            </button>
          )}
        </div>
      </header>

      {keyEditing && (
        <div className="key-edit">
          <span className="field-label">新 API Key（{provider.credentials ?? provider.name}）</span>
          <input
            className="control"
            type="password"
            autoComplete="new-password"
            aria-label="新 API Key"
            value={keyInput}
            onChange={(change) => setKeyInput(change.target.value)}
            placeholder="sk-…"
          />
          <button type="button" onClick={() => void saveKey()} disabled={!keyInput}>
            保存
          </button>
          <button type="button" onClick={() => { setKeyEditing(false); setKeyInput(""); }}>
            取消
          </button>
        </div>
      )}

      <table>
        <thead>
          <tr>
            <th>模型 ID</th>
            <th>API 模型名</th>
            <th>上下文</th>
            <th>参数</th>
            <th>工具</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {models.map((model) => {
            const summary = parameterSummary(model.parameters);
            const test = tests[model.id];
            return (
              <Fragment key={model.id}>
                <tr>
                  <td className="mono">{model.id}</td>
                  <td className="mono">{model.model ?? model.id}</td>
                  <td>{model.context_window ?? "未知"}</td>
                  <td className={summary ? "mono params-cell" : undefined}>{summary ?? "—"}</td>
                  <td>{model.supports_tools ? "是" : "否"}</td>
                  <td className="row-actions">
                    <button className="link" onClick={() => startEdit(model)}>
                      <PencilSimpleIcon size={14} weight="bold" aria-hidden />
                      编辑
                    </button>
                    <button className="link" onClick={() => void runTest(model.id)}>
                      <PlayIcon size={14} weight="bold" aria-hidden />
                      测试
                    </button>
                    <button className="link danger" onClick={() => void removeModel(model.id)}>
                      删除
                    </button>
                  </td>
                </tr>
                {test && (
                  <tr className="test-result-row">
                    <td colSpan={6}>
                      {test === "running" ? (
                        <span className="muted">测试中，真实调用进行中</span>
                      ) : test.ok ? (
                        <span className="pass">
                          通过 <span className="mono">{Math.round(test.latency_ms ?? 0)}ms</span>
                          {test.usage && (
                            <span className="muted">
                              {" "}
                              tokens {test.usage.prompt_tokens ?? "?"} + {test.usage.completion_tokens ?? "?"}
                            </span>
                          )}
                        </span>
                      ) : (
                        <span className="fail">
                          失败 {test.error?.class && <span className="mono">{test.error.class}</span>}{" "}
                          {test.error?.message ?? "未知错误"}
                        </span>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
          {models.length === 0 && (
            <tr>
              <td colSpan={6} className="empty">
                暂无模型
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {adding && (
        <form
          className="model-add"
          onSubmit={submitModel}
          aria-label={editing ? `编辑 ${provider.name} 的模型 ${editing}` : `为 ${provider.name} 添加模型`}
        >
          <label>
            模型 ID{editing ? "（编辑中不可改）" : ""}
            <input
              value={modelId}
              onChange={(change) => setModelId(change.target.value)}
              placeholder="qwen2.5-7b"
              required
              disabled={Boolean(editing)}
            />
          </label>
          <label>
            API 模型名（留空则与 ID 相同）
            <input value={modelName} onChange={(change) => setModelName(change.target.value)} placeholder="Qwen/Qwen2.5-7B-Instruct" />
          </label>
          <label>
            上下文窗口
            <input value={contextWindow} onChange={(change) => setContextWindow(change.target.value)} placeholder="32768" />
          </label>
          <label>
            temperature（0-1，可选）
            <input
              type="number"
              min={0}
              max={1}
              step="any"
              value={temperature}
              onChange={(change) => setTemperature(change.target.value)}
              placeholder="0.7"
            />
          </label>
          <label>
            top_p（0-1，可选）
            <input
              type="number"
              min={0}
              max={1}
              step="any"
              value={topP}
              onChange={(change) => setTopP(change.target.value)}
              placeholder="0.9"
            />
          </label>
          <label>
            max_output_tokens（可选）
            <input
              type="number"
              min={1}
              step={1}
              value={maxOutputTokens}
              onChange={(change) => setMaxOutputTokens(change.target.value)}
              placeholder="4096"
            />
          </label>
          <div className="switch-row">
            <span>支持工具调用</span>
            <Switch.Root
              className="switch"
              checked={supportsTools}
              onCheckedChange={setSupportsTools}
              aria-label="支持工具调用"
            >
              <Switch.Thumb className="switch-thumb" />
            </Switch.Root>
          </div>
          <button type="submit">{editing ? "保存修改" : "注册"}</button>
          <button
            type="button"
            onClick={() => {
              resetModelForm();
              setAdding(false);
            }}
          >
            取消
          </button>
        </form>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}

export function HarnessesPage() {
  const [harnesses, setHarnesses] = useState<HarnessReport[]>([]);
  const [agents, setAgents] = useState<{ id: string; kind: string; description: string }[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    getHarnesses().then((payload) => setHarnesses(payload.items)).catch((e) => setError(String(e)));
    getAgents().then((payload) => setAgents(payload.items)).catch(() => undefined);
  }, []);

  return (
    <div className="page">
      {error && <p className="error">{error}</p>}
      <section className="panel">
        <h2>Harness 安装情况</h2>
        <table>
          <thead>
            <tr>
              <th>Harness</th>
              <th>已安装</th>
              <th>版本</th>
              <th>安装来源</th>
              <th>可执行</th>
            </tr>
          </thead>
          <tbody>
            {harnesses.map((harness) => (
              <tr key={harness.name}>
                <td>{harness.name}</td>
                <td className={harness.installed ? "pass" : "fail"}>{harness.installed ? "是" : "否"}</td>
                <td>{harness.version ?? "—"}</td>
                <td>{harness.source ?? "—"}</td>
                <td className={harness.runnable ? "pass" : "fail"}>{harness.runnable ? "是" : "否"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="panel">
        <h2>Agent 运行时</h2>
        <ul>
          {agents.map((agent) => (
            <li key={agent.id}>
              <strong>{agent.id}</strong>（{agent.kind}）：{agent.description}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
