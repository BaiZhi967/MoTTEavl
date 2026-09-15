import { useCallback, useEffect, useState, type FormEvent } from "react";
import * as Switch from "@radix-ui/react-switch";
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

export function ProvidersPage() {
  const [providers, setProviders] = useState<ProviderRecord[]>([]);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKeyEnv, setApiKeyEnv] = useState("OPENAI_API_KEY");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setProviders((await getProviders()).items);
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
      await createProvider({ name, kind: "openai_compatible", base_url: baseUrl, api_key_env: apiKeyEnv });
      setName("");
      setBaseUrl("");
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <form className="panel" onSubmit={submit} aria-label="创建 Provider">
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
          API Key 环境变量名（只存变量名，绝不存密钥）
          <input value={apiKeyEnv} onChange={(change) => setApiKeyEnv(change.target.value)} />
        </label>
        <button type="submit">创建</button>
        {error && <p className="error">{error}</p>}
      </form>

      <section className="panel">
        <h2>Provider 列表</h2>
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>Base URL</th>
              <th>密钥环境变量</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {providers.map((provider) => (
              <tr key={provider.name}>
                <td>{provider.name}</td>
                <td>{provider.kind}</td>
                <td>{provider.base_url}</td>
                <td>{provider.api_key_env ?? "—"}</td>
                <td>
                  <button
                    onClick={async () => {
                      await deleteProvider(provider.name);
                      await refresh();
                    }}
                  >
                    删除
                  </button>
                </td>
              </tr>
            ))}
            {providers.length === 0 && (
              <tr>
                <td colSpan={5} className="empty">
                  暂无 Provider
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}

export function ModelsPage() {
  const [models, setModels] = useState<ModelRecord[]>([]);
  const [id, setId] = useState("");
  const [provider, setProvider] = useState("");
  const [contextWindow, setContextWindow] = useState("");
  const [supportsTools, setSupportsTools] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setModels((await getModels()).items);
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
      await createModel({
        id,
        provider,
        capabilities: { text: true },
        ...(contextWindow ? { context_window: Number(contextWindow) } : {}),
        supports_tools: supportsTools,
      });
      setId("");
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <form className="panel" onSubmit={submit} aria-label="创建模型">
        <h2>注册模型</h2>
        <label>
          模型 ID
          <input value={id} onChange={(change) => setId(change.target.value)} placeholder="qwen2.5-7b" required />
        </label>
        <label>
          Provider
          <input value={provider} onChange={(change) => setProvider(change.target.value)} placeholder="local-vllm" required />
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
        {error && <p className="error">{error}</p>}
      </form>

      <section className="panel">
        <h2>模型列表</h2>
        <table>
          <thead>
            <tr>
              <th>模型</th>
              <th>Provider</th>
              <th>上下文</th>
              <th>工具</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {models.map((model) => (
              <tr key={model.id}>
                <td>{model.id}</td>
                <td>{model.provider}</td>
                <td>{model.context_window ?? "未知"}</td>
                <td>{model.supports_tools ? "是" : "否"}</td>
                <td>
                  <button
                    onClick={async () => {
                      await deleteModel(model.id);
                      await refresh();
                    }}
                  >
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
      </section>
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
