import { useCallback, useEffect, useState, type FormEvent } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import * as Switch from "@radix-ui/react-switch";
import {
  ArrowClockwiseIcon,
  KeyIcon,
  PencilSimpleIcon,
  PlayIcon,
  PlugIcon,
  PlusIcon,
  TrashIcon,
  XIcon,
} from "@phosphor-icons/react";
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
  publishModel,
  setCredential,
  testModel,
  updateModel,
  updateProvider,
  type AgentReport,
  type CredentialSummary,
  type HarnessReport,
  type ModelRecord,
  type ModelTestResult,
  type ProviderKindMeta,
  type ProviderRecord,
} from "../api/client";
import { formatContext } from "../components/ModelPicker";

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

/** Provider 管理采用 master-detail：左列清单选中，右列编辑选中 Provider 的连接与模型。 */
export function ProvidersPage() {
  const [kinds, setKinds] = useState<ProviderKindMeta[]>(FALLBACK_KINDS);
  const [providers, setProviders] = useState<ProviderRecord[]>([]);
  const [models, setModels] = useState<ModelRecord[]>([]);
  const [credentialSummaries, setCredentialSummaries] = useState<CredentialSummary[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [loadError, setLoadError] = useState("");
  /** 本会话内各 Provider 最近一次测试结果（true 通过 / false 失败）；只喂给清单状态点，未测不显示。 */
  const [providerTests, setProviderTests] = useState<Record<string, boolean>>({});

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
      setLoadError("");
    } catch (e) {
      setLoadError(String(e));
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

  // 选中项被删除时自动回落到第一个，保证右侧始终有内容
  const selectedProvider = providers.find((provider) => provider.name === selected) ?? providers[0] ?? null;

  return (
    <div className="page">
      <aside className="panel list-panel" aria-label="Provider 清单">
        <div className="panel-head">
          <h2>Provider</h2>
          <div className="panel-head-actions">
            <button type="button" className="icon-btn" aria-label="刷新清单" onClick={() => void refresh()}>
              <ArrowClockwiseIcon size={14} weight="bold" aria-hidden />
            </button>
            <button type="button" className="link" onClick={() => setCreating(true)}>
              <PlusIcon size={14} weight="bold" aria-hidden />
              添加
            </button>
          </div>
        </div>
        {loadError && <p className="error">{loadError}</p>}
        {!loaded && providers.length === 0 ? (
          <p className="empty">加载中</p>
        ) : providers.length === 0 ? (
          <p className="empty">暂无 Provider</p>
        ) : (
          <ul className="provider-list">
            {providers.map((provider) => (
              <li key={provider.name}>
                <button
                  type="button"
                  className="provider-item"
                  data-state={selectedProvider?.name === provider.name ? "active" : undefined}
                  data-enabled={provider.enabled === false ? "false" : undefined}
                  onClick={() => setSelected(provider.name)}
                >
                  <PlugIcon size={16} weight="bold" aria-hidden />
                  <span className="provider-item-name mono">{provider.name}</span>
                  {providerTests[provider.name] === true && (
                    <span className="state-dot pass" aria-label="最近测试通过" />
                  )}
                  {providerTests[provider.name] === false && (
                    <span className="state-dot fail" aria-label="最近测试失败" />
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      {selectedProvider ? (
        <ProviderDetail
          key={selectedProvider.name}
          provider={selectedProvider}
          models={models.filter((model) => model.provider === selectedProvider.name)}
          kinds={kinds}
          credentialHint={
            credentialSummaries.find((entry) => entry.profile === (selectedProvider.credentials ?? selectedProvider.name))
              ?.key_hint
          }
          onTestResult={(ok) =>
            setProviderTests((current) => ({ ...current, [selectedProvider.name]: ok }))
          }
          onChanged={refresh}
        />
      ) : (
        loaded && (
          <section className="panel provider-detail">
            <p className="empty">暂无 Provider，点击左侧「添加」创建第一个连接</p>
          </section>
        )
      )}

      <CreateProviderDialog
        open={creating}
        kinds={kinds}
        onClose={() => setCreating(false)}
        onCreated={async (name) => {
          await refresh();
          setSelected(name);
        }}
      />
    </div>
  );
}

/** 创建 Provider 的滑出面板：连接信息一次性填写，密钥写凭据文件、不回显、不入库。 */
function CreateProviderDialog({
  open,
  kinds,
  onClose,
  onCreated,
}: {
  open: boolean;
  kinds: ProviderKindMeta[];
  onClose: () => void;
  onCreated: (name: string) => Promise<void>;
}) {
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
      setBaseUrl("");
      setCredentials("");
      setApiKey("");
      onClose();
      await onCreated(name);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content" aria-label="创建 Provider">
          <div className="dialog-header">
            <Dialog.Title className="dialog-title">创建 Provider</Dialog.Title>
            <Dialog.Close asChild>
              <button type="button" className="icon-btn" aria-label="关闭创建面板">
                <XIcon size={16} weight="bold" aria-hidden />
              </button>
            </Dialog.Close>
          </div>
          <form onSubmit={submit}>
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
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** 模型行的参数摘要：temp 0.2 top_p 0.9 max 4096；空则返回 null。 */
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

function ProviderDetail({
  provider,
  models,
  kinds,
  credentialHint,
  onTestResult,
  onChanged,
}: {
  provider: ProviderRecord;
  models: ModelRecord[];
  kinds: ProviderKindMeta[];
  credentialHint?: string;
  onTestResult: (ok: boolean) => void;
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
  const [inputModalities, setInputModalities] = useState<string[]>(["text"]);
  const [capabilities, setCapabilities] = useState<ModelRecord["capabilities"]>({ text: true });
  const [reasoningEnabled, setReasoningEnabled] = useState(false);
  const [reasoningLevels, setReasoningLevels] = useState("");
  const [reasoningDefault, setReasoningDefault] = useState("");
  const [reasoningControl, setReasoningControl] = useState("");
  const [savingModel, setSavingModel] = useState(false);
  const [keyEditing, setKeyEditing] = useState(false);
  const [keyInput, setKeyInput] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [tests, setTests] = useState<Record<string, ModelTestResult | "running">>({});
  const [error, setError] = useState("");
  // 连接编辑：初值取自 provider 记录，脏状态才显示保存按钮
  const [connectionKind, setConnectionKind] = useState(provider.kind);
  const [connectionUrl, setConnectionUrl] = useState(provider.base_url ?? "");
  const [savingConnection, setSavingConnection] = useState(false);
  const connectionDirty = connectionKind !== provider.kind || connectionUrl !== (provider.base_url ?? "");

  const resetModelForm = () => {
    setModelId("");
    setModelName("");
    setContextWindow("");
    setTemperature("");
    setTopP("");
    setMaxOutputTokens("");
    setSupportsTools(false);
    setInputModalities(["text"]);
    setCapabilities({ text: true });
    setReasoningEnabled(false);
    setReasoningLevels("");
    setReasoningDefault("");
    setReasoningControl("");
    setSavingModel(false);
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
    const outputLimit = model.max_output_tokens ?? model.parameters?.max_output_tokens;
    setMaxOutputTokens(outputLimit != null ? String(outputLimit) : "");
    setSupportsTools(Boolean(model.supports_tools));
    setInputModalities(Array.from(new Set(["text", ...(model.input_modalities ?? [])])));
    setCapabilities({ ...model.capabilities });
    setReasoningEnabled(Boolean(model.reasoning?.supported));
    setReasoningLevels(model.reasoning?.levels.join(", ") ?? "");
    setReasoningDefault(model.reasoning?.default_level ?? "");
    setReasoningControl(model.reasoning?.control ?? "");
    setError("");
    setEditing(model.id);
    setAdding(true);
  };

  const submitModel = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setError("");
    if (savingModel) return;
    const levels = reasoningLevels.split(/[,，\s]+/).filter(Boolean);
    if (reasoningEnabled && (!levels.length || !reasoningControl.trim() || !levels.includes(reasoningDefault))) {
      setError("请填写推理等级、CEL 表达式，并选择其中一个等级作为默认值。");
      return;
    }
    const parameters = { ...(models.find((model) => model.id === editing)?.parameters ?? {}) };
    delete parameters.temperature;
    delete parameters.top_p;
    delete parameters.max_output_tokens;
    if (temperature) parameters.temperature = Number(temperature);
    if (topP) parameters.top_p = Number(topP);
    setSavingModel(true);
    try {
      const payload = {
        model: modelName.trim() || null,
        capabilities,
        input_modalities: inputModalities,
        context_window: contextWindow ? Number(contextWindow) : null,
        max_output_tokens: maxOutputTokens ? Number(maxOutputTokens) : null,
        supports_tools: supportsTools,
        parameters,
        reasoning: {
          supported: reasoningEnabled,
          levels: Array.from(new Set(levels)),
          default_level: reasoningEnabled ? reasoningDefault || null : null,
          control: reasoningControl.trim() || null,
        },
      };
      // 编辑走合并式 PUT，保留未提及字段（enabled、provenance 等）；新增仍是 POST 注册
      if (editing) {
        await updateModel(editing, payload);
      } else {
        await createModel({ id: modelId, provider: provider.name, ...payload });
      }
      resetModelForm();
      setAdding(false);
      await onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setSavingModel(false);
    }
  };

  const runTest = async (id: string) => {
    setError("");
    setTests((current) => ({ ...current, [id]: "running" }));
    try {
      const report = await testModel(id);
      setTests((current) => ({ ...current, [id]: report }));
      onTestResult(report.ok);
    } catch (e) {
      setTests((current) => ({
        ...current,
        [id]: { ok: false, provider: provider.kind, model: id, tested_at: "", error: { message: String(e) } },
      }));
      onTestResult(false);
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

  const publishModelProfile = async (id: string) => {
    setError("");
    try {
      await publishModel(id);
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

  const saveConnection = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setError("");
    setSavingConnection(true);
    try {
      await updateProvider(provider.name, { kind: connectionKind, base_url: connectionUrl });
      await onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setSavingConnection(false);
    }
  };

  const toggleProvider = async (next: boolean) => {
    setError("");
    try {
      await updateProvider(provider.name, { enabled: next });
      await onChanged();
    } catch (e) {
      setError(String(e));
    }
  };

  const toggleModel = async (model: ModelRecord, next: boolean) => {
    setError("");
    try {
      await updateModel(model.id, { enabled: next });
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
    <section className="panel provider-detail" aria-label={`Provider ${provider.name} 详情`}>
      <header className="detail-head">
        <h2 className="mono">{provider.name}</h2>
        <span className="status-badge status-tone-neutral kind-badge">{provider.kind}</span>
        <div className="detail-actions">
          <span className="inline-field">
            <span className="field-label">启用</span>
            <Switch.Root
              className="switch"
              checked={provider.enabled !== false}
              onCheckedChange={(next) => void toggleProvider(next)}
              aria-label={`启用 ${provider.name}`}
            >
              <Switch.Thumb className="switch-thumb" />
            </Switch.Root>
          </span>
          <button type="button" className="link" onClick={() => setKeyEditing((open) => !open)}>
            <KeyIcon size={14} weight="bold" aria-hidden />
            更新密钥
          </button>
          {confirmDelete ? (
            <>
              <button
                type="button"
                className="link danger"
                title={`删除 ${provider.name} 及其 ${models.length} 个模型`}
                onClick={() => void removeProviderWithModels()}
              >
                确认删除
              </button>
              <button type="button" className="link" onClick={() => setConfirmDelete(false)}>
                取消
              </button>
            </>
          ) : (
            <button type="button" className="link danger" onClick={() => setConfirmDelete(true)}>
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

      <form className="connection-form" onSubmit={saveConnection} aria-label="连接配置">
        <div className="section-head">
          <h3 className="embed-title">连接</h3>
        </div>
        <div className="connection-grid">
          <label>
            协议类型
            <select value={connectionKind} onChange={(change) => setConnectionKind(change.target.value)}>
              {kinds.map((entry) => (
                <option key={entry.kind} value={entry.kind}>
                  {entry.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Base URL
            <input
              value={connectionUrl}
              onChange={(change) => setConnectionUrl(change.target.value)}
              placeholder="http://localhost:8001/v1"
            />
          </label>
        </div>
        <dl className="kv">
          <dt>凭据 profile</dt>
          <dd className="mono">{provider.credentials ?? provider.name}</dd>
          <dt>密钥</dt>
          <dd className="mono">{credentialHint ?? "未配置"}</dd>
        </dl>
        {connectionDirty && (
          <button type="submit" disabled={savingConnection}>
            保存连接
          </button>
        )}
      </form>

      <section aria-label="模型列表">
        <div className="section-head">
          <h3 className="embed-title">模型列表</h3>
          <button type="button" className="link" onClick={startAdd}>
            <PlusIcon size={14} weight="bold" aria-hidden />
            添加模型
          </button>
        </div>
        <ul className="model-list">
          {models.map((model) => {
            const summary = parameterSummary({ ...model.parameters, max_output_tokens: model.max_output_tokens ?? model.parameters?.max_output_tokens ?? null });
            const context = formatContext(model.context_window);
            const test = tests[model.id];
            const lifecycle = model.lifecycle ?? "draft";
            const lifecycleLabel = lifecycle === "draft" ? "草稿" : lifecycle === "published" ? "已发布" : "已弃用";
            return (
              <li key={model.id} className="model-item">
                <div className="model-row">
                  <div className="model-main">
                    <span className="mono model-id">{model.id}</span>
                    <span className={`status-badge model-badge ${lifecycle === "published" ? "status-tone-success" : lifecycle === "deprecated" ? "status-tone-warning" : "status-tone-neutral"}`}>
                      {lifecycleLabel} · g{model.generation ?? 1}
                    </span>
                    {context && <span className="status-badge status-tone-neutral model-badge">{context}</span>}
                    {model.supports_tools && (
                      <span className="status-badge status-tone-neutral model-badge">工具</span>
                    )}
                    {summary && <span className="model-params mono">{summary}</span>}
                  </div>
                  <div className="model-actions">
                    <Switch.Root
                      className="switch"
                      checked={model.enabled !== false}
                      onCheckedChange={(next) => void toggleModel(model, next)}
                      aria-label={`启用 ${model.id}`}
                      disabled={lifecycle !== "draft"}
                    >
                      <Switch.Thumb className="switch-thumb" />
                    </Switch.Root>
                    <button type="button" className="link" onClick={() => void runTest(model.id)} disabled={lifecycle === "deprecated"}>
                      <PlayIcon size={14} weight="bold" aria-hidden />
                      测试
                    </button>
                    {lifecycle === "draft" && (
                      <>
                        <button type="button" className="link" onClick={() => startEdit(model)}>
                          <PencilSimpleIcon size={14} weight="bold" aria-hidden />
                          编辑
                        </button>
                        <button type="button" className="link" onClick={() => void publishModelProfile(model.id)}>
                          发布
                        </button>
                      </>
                    )}
                    {lifecycle !== "deprecated" && (
                      <button type="button" className="link danger" onClick={() => void removeModel(model.id)}>
                        弃用
                      </button>
                    )}
                  </div>
                </div>
                {test && (
                  <div className="model-test-result">
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
                  </div>
                )}
              </li>
            );
          })}
          {models.length === 0 && <li className="empty">暂无模型</li>}
        </ul>
      </section>

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
          <fieldset className="model-config-group">
            <legend>基础限制</legend>
            <div className="connection-grid">
              <label>
                上下文窗口
                <input type="number" min={1} step={1} value={contextWindow} onChange={(change) => setContextWindow(change.target.value)} placeholder="32768" />
              </label>
              <label>
                最大输出 Token
                <input type="number" min={1} step={1} value={maxOutputTokens} onChange={(change) => setMaxOutputTokens(change.target.value)} placeholder="4096" aria-describedby="output-token-help" />
              </label>
            </div>
            <p className="hint" id="output-token-help">单次模型请求允许生成的最大 Token 数。留空使用服务端默认值。</p>
          </fieldset>
          <fieldset className="model-config-group">
            <legend>输入类型</legend>
            <p className="hint">设置模型能够接收的内容类型。文本为必选项；能力声明不会自动转换输入内容。</p>
            {[
              ["text", "文本", "接收文本内容（必选）"],
              ["image", "图片", "接收图片内容"],
              ["video", "视频", "接收视频内容"],
              ["pdf", "PDF", "直接接收 PDF 文档"],
            ].map(([value, label, description]) => (
              <div className="model-option" key={value}>
                <div><span>{label}</span><p className="hint">{description}</p></div>
                <Switch.Root className="switch" aria-label={`输入类型：${label}`} disabled={value === "text"} checked={inputModalities.includes(value)} onCheckedChange={(checked) => setInputModalities((current) => checked ? [...current, value] : current.filter((item) => item !== value))}>
                  <Switch.Thumb className="switch-thumb" />
                </Switch.Root>
              </div>
            ))}
          </fieldset>
          <fieldset className="model-config-group">
            <legend>模型能力</legend>
            <p className="hint">请勿勾选模型不支持的能力。声明支持不等于在每次请求中启用。</p>
            {[
              ["structured_output", "结构化输出", "支持通过 JSON Schema 约束模型输出的字段、类型和结构。"],
              ["native_search", "原生联网搜索", "支持使用模型接口内置的联网搜索能力。"],
              ["system_messages", "对话中系统消息", "支持在对话中途插入系统指令。"],
            ].map(([key, label, description]) => (
              <div className="model-option" key={key}>
                <div><span>{label}</span><p className="hint">{description}</p></div>
                <Switch.Root className="switch" aria-label={label} checked={capabilities[key] === true} onCheckedChange={(checked) => setCapabilities((current) => ({ ...current, [key]: checked }))}>
                  <Switch.Thumb className="switch-thumb" />
                </Switch.Root>
              </div>
            ))}
            <div className="model-option">
              <span>支持工具调用</span>
              <Switch.Root className="switch" checked={supportsTools} onCheckedChange={setSupportsTools} aria-label="支持工具调用">
                <Switch.Thumb className="switch-thumb" />
              </Switch.Root>
            </div>
          </fieldset>
          <fieldset className="model-config-group">
            <legend>推理等级</legend>
            <div className="model-option">
              <span>启用推理等级映射</span>
              <Switch.Root className="switch" checked={reasoningEnabled} onCheckedChange={setReasoningEnabled} aria-label="启用推理等级映射">
                <Switch.Thumb className="switch-thumb" />
              </Switch.Root>
            </div>
            {reasoningEnabled && <>
              <div className="connection-grid">
                <label>可用推理等级
                  <input value={reasoningLevels} onChange={(change) => setReasoningLevels(change.target.value)} placeholder="low, medium, high" required />
                </label>
                <label>默认推理等级
                  <select value={reasoningDefault} onChange={(change) => setReasoningDefault(change.target.value)} required>
                    <option value="">请选择</option>
                    {Array.from(new Set(reasoningLevels.split(/[,，\s]+/).filter(Boolean))).map((level) => <option key={level} value={level}>{level}</option>)}
                  </select>
                </label>
              </div>
              <label>CEL 表达式
                <textarea className="mono" rows={5} value={reasoningControl} onChange={(change) => setReasoningControl(change.target.value)} placeholder={'{"reasoning_effort": reasoningLevel}'} required aria-describedby="reasoning-help" />
              </label>
              <p className="hint" id="reasoning-help">使用 CEL 将当前推理等级 <code>reasoningLevel</code> 映射为 JSON 对象，按顶层字段合并到请求体。保存时校验所有可用等级；运行可用 <code>manifest.reasoning_level</code> 覆盖默认等级。</p>
              <p className="hint">示例仅适用于支持该字段的接口：Chat Completions 使用 <code>{'{"reasoning_effort": reasoningLevel}'}</code>；Responses 使用 <code>{'{"reasoning": {"effort": reasoningLevel}}'}</code>。不能覆盖模型、消息、工具或凭据字段。</p>
            </>}
          </fieldset>
          <details className="model-config-group">
            <summary>高级采样参数（可选）</summary>
            <p className="hint">仅为支持采样参数的模型设置；推理模型通常应留空。不修改时保留已有配置。</p>
            <div className="connection-grid">
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
            </div>
          </details>
          <button type="submit" disabled={savingModel}>{savingModel ? "保存中…" : editing ? "保存修改" : "注册"}</button>
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
    </section>
  );
}

export function HarnessesPage() {
  const [harnesses, setHarnesses] = useState<HarnessReport[]>([]);
  const [agents, setAgents] = useState<AgentReport[]>([]);
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
              <th>本机可运行</th>
              <th>协议就绪</th>
              <th>评测执行就绪</th>
            </tr>
          </thead>
          <tbody>
            {harnesses.map((harness) => (
              <tr key={harness.name}>
                <td>{harness.name}</td>
                <td className={harness.installed ? "pass" : "fail"}>{harness.installed ? "是" : "否"}</td>
                <td className="mono">{harness.version ?? "—"}</td>
                <td className={harness.runnable ? "pass" : "fail"}>{harness.runnable ? "是" : "否"}</td>
                <td className={harness.protocol_ready ? "pass" : "fail"}>{harness.protocol_ready ? "是" : "否"}</td>
                <td className={harness.execution_ready ? "pass" : "fail"}>{harness.execution_ready ? "是" : "否"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="panel">
        <h2>Agent 运行时</h2>
        <table>
          <thead>
            <tr><th>Agent</th><th>类型</th><th>协议就绪</th><th>评测执行就绪</th><th>说明</th></tr>
          </thead>
          <tbody>
            {agents.map((agent) => (
              <tr key={agent.id}>
                <td className="mono">{agent.id}</td>
                <td className="mono">{agent.kind}</td>
                <td className={agent.protocol_ready ? "pass" : "fail"}>{agent.protocol_ready ? "是" : "否"}</td>
                <td className={agent.execution_ready ? "pass" : "fail"}>{agent.execution_ready ? "是" : "否"}</td>
                <td>{agent.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
