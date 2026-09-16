import { useCallback, useEffect, useState, type FormEvent } from "react";
import * as Switch from "@radix-ui/react-switch";
import { PlusIcon, TrashIcon } from "@phosphor-icons/react";
import {
  createModel,
  createProvider,
  deleteModel,
  deleteProvider,
  getAgents,
  getHarnesses,
  getModels,
  getProviders,
  type HarnessReport,
  type ModelRecord,
  type ProviderRecord,
} from "../api/client";

/** Provider 与其模型合并管理：连接（Base URL / 凭据）在 Provider 上，模型挂在 Provider 下。 */
export function ProvidersPage() {
  const [providers, setProviders] = useState<ProviderRecord[]>([]);
  const [models, setModels] = useState<ModelRecord[]>([]);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [credentials, setCredentials] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [providerPayload, modelPayload] = await Promise.all([getProviders(), getModels()]);
      setProviders(providerPayload.items);
      setModels(modelPayload.items);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const submit = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setError("");
    try {
      await createProvider({
        name,
        kind: "openai_compatible",
        base_url: baseUrl,
        ...(credentials ? { credentials } : {}),
      });
      setName("");
      setBaseUrl("");
      setCredentials("");
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <form className="panel form-panel" onSubmit={submit} aria-label="创建 Provider">
        <h2>创建 Provider（openai_compatible）</h2>
        <label>
          名称
          <input value={name} onChange={(change) => setName(change.target.value)} placeholder="local-vllm" required />
        </label>
        <label>
          Base URL
          <input value={baseUrl} onChange={(change) => setBaseUrl(change.target.value)} placeholder="http://localhost:8001/v1" required />
        </label>
        <label>
          凭据 profile（留空则与名称相同；密钥本体用 CLI 配置，绝不入库）
          <input value={credentials} onChange={(change) => setCredentials(change.target.value)} placeholder="local-vllm" />
        </label>
        <p className="hint">
          密钥配置：<code>python -m motte_cli credentials set {"<profile>"}</code>（写入 ~/.motte/credentials.toml，0600）
        </p>
        <button type="submit">创建</button>
        {error && <p className="error">{error}</p>}
      </form>

      <section className="panel">
        <h2>Provider 与模型</h2>
        {providers.length === 0 ? (
          <p className="empty">暂无 Provider</p>
        ) : (
          providers.map((provider) => (
            <ProviderSection
              key={provider.name}
              provider={provider}
              models={models.filter((model) => model.provider === provider.name)}
              onChanged={refresh}
            />
          ))
        )}
      </section>
    </div>
  );
}

function ProviderSection({
  provider,
  models,
  onChanged,
}: {
  provider: ProviderRecord;
  models: ModelRecord[];
  onChanged: () => Promise<void>;
}) {
  const [adding, setAdding] = useState(false);
  const [modelId, setModelId] = useState("");
  const [modelName, setModelName] = useState("");
  const [contextWindow, setContextWindow] = useState("");
  const [supportsTools, setSupportsTools] = useState(false);
  const [error, setError] = useState("");

  const submitModel = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setError("");
    try {
      await createModel({
        id: modelId,
        provider: provider.name,
        ...(modelName ? { model: modelName } : {}),
        capabilities: { text: true },
        ...(contextWindow ? { context_window: Number(contextWindow) } : {}),
        supports_tools: supportsTools,
      });
      setModelId("");
      setModelName("");
      setContextWindow("");
      setSupportsTools(false);
      setAdding(false);
      await onChanged();
    } catch (e) {
      setError(String(e));
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
          <span className="provider-kind">{provider.kind}</span>
        </div>
        <div className="provider-meta">
          <span className="mono">{provider.base_url ?? "—"}</span>
          <span>凭据 {provider.credentials ?? provider.name}</span>
        </div>
        <div className="provider-actions">
          <button className="link" onClick={() => setAdding((open) => !open)}>
            <PlusIcon size={14} weight="bold" aria-hidden />
            添加模型
          </button>
          <button
            className="link danger"
            title={`删除 ${provider.name} 及其 ${models.length} 个模型`}
            onClick={() => void removeProviderWithModels()}
          >
            <TrashIcon size={14} weight="bold" aria-hidden />
            删除
          </button>
        </div>
      </header>

      <table>
        <thead>
          <tr>
            <th>模型 ID</th>
            <th>API 模型名</th>
            <th>上下文</th>
            <th>工具</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {models.map((model) => (
            <tr key={model.id}>
              <td className="mono">{model.id}</td>
              <td className="mono">{model.model ?? model.id}</td>
              <td>{model.context_window ?? "未知"}</td>
              <td>{model.supports_tools ? "是" : "否"}</td>
              <td className="row-actions">
                <button className="link danger" onClick={() => void removeModel(model.id)}>
                  删除
                </button>
              </td>
            </tr>
          ))}
          {models.length === 0 && (
            <tr>
              <td colSpan={5} className="empty">
                暂无模型
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {adding && (
        <form className="model-add" onSubmit={submitModel} aria-label={`为 ${provider.name} 添加模型`}>
          <label>
            模型 ID
            <input value={modelId} onChange={(change) => setModelId(change.target.value)} placeholder="qwen2.5-7b" required />
          </label>
          <label>
            API 模型名（留空则与 ID 相同）
            <input value={modelName} onChange={(change) => setModelName(change.target.value)} placeholder="Qwen/Qwen2.5-7B-Instruct" />
          </label>
          <label>
            上下文窗口
            <input value={contextWindow} onChange={(change) => setContextWindow(change.target.value)} placeholder="32768" />
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
          <button type="submit">注册</button>
          <button type="button" onClick={() => setAdding(false)}>
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
