import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ArrowClockwiseIcon, DownloadSimpleIcon } from "@phosphor-icons/react";
import {
  createBenchmarkRun,
  getBenchmarkOverview,
  importBenchmark,
  type BenchmarkOverview,
} from "../api/client";
import { StatusBadge } from "../components/StatusBadge";

/** GSM8K 冒烟跑测页：测试集受控下载导入、跑测参数配置与最近运行进度。 */
export function BenchmarksPage() {
  const [overview, setOverview] = useState<BenchmarkOverview | null>(null);
  const [revision, setRevision] = useState("");
  const [license, setLicense] = useState("MIT");
  const [version, setVersion] = useState("1");
  const [model, setModel] = useState("");
  const [temperature, setTemperature] = useState("");
  const [maxTokens, setMaxTokens] = useState("");
  const [importing, setImporting] = useState(false);
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setOverview(await getBenchmarkOverview());
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    // 有进行中的运行时 3s 轮询进度；全部终态时降低为不轮询
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  const doImport = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setImporting(true);
    setError("");
    setMessage("");
    try {
      const payload = await importBenchmark({ revision: revision.trim(), license: license.trim(), version: version.trim() });
      setMessage(`已导入 ${payload.imported}，场景 ${payload.scenario}（${payload.cases} 题）`);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setImporting(false);
    }
  };

  const doRun = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setRunning(true);
    setError("");
    setMessage("");
    try {
      const parameters: Record<string, number> = {};
      if (temperature.trim()) parameters.temperature = Number(temperature);
      if (maxTokens.trim()) parameters.max_tokens = Number(maxTokens);
      const created = await createBenchmarkRun({
        model: model.trim(),
        ...(Object.keys(parameters).length ? { parameters } : {}),
      });
      setMessage(`已创建运行 ${created.id}，等待 Worker 执行`);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="page">
      <form className="panel form-panel" onSubmit={doImport} aria-label="下载并导入测试集">
        <h2>下载测试集</h2>
        <p className="hint">从官方 openai/grade-school-math 仓库按 pinned commit 下载 test.jsonl，取前 20 题生成冒烟数据集。源文件保存在 var/datasets/（已 gitignore）。</p>
        <label>
          官方仓库 commit（40 位）
          <input
            value={revision}
            onChange={(change) => setRevision(change.target.value)}
            placeholder="完整 40 位 commit hash"
            required
          />
        </label>
        <label>
          License
          <input value={license} onChange={(change) => setLicense(change.target.value)} />
        </label>
        <label>
          数据集版本
          <input value={version} onChange={(change) => setVersion(change.target.value)} />
        </label>
        <button type="submit" disabled={importing}>
          <DownloadSimpleIcon size={14} weight="bold" aria-hidden />
          {importing ? "下载中…" : "下载并导入"}
        </button>
        {message && <p className="import-feedback pass">{message}</p>}
      </form>

      <form className="panel form-panel" onSubmit={doRun} aria-label="创建跑测">
        <h2>创建跑测</h2>
        <label>
          模型档案 ID
          <input value={model} onChange={(change) => setModel(change.target.value)} placeholder="如 qwen-max" required />
        </label>
        <label>
          temperature（可选）
          <input type="number" step="0.1" min="0" max="2" value={temperature} onChange={(change) => setTemperature(change.target.value)} />
        </label>
        <label>
          max_tokens（可选）
          <input type="number" value={maxTokens} onChange={(change) => setMaxTokens(change.target.value)} />
        </label>
        <button type="submit" disabled={running || !overview || overview.items.length === 0}>
          {running ? "创建中…" : "开始跑测（20 题）"}
        </button>
        <p className="hint">预设：max_output_tokens=1024、无重试；此处参数覆盖默认值，不改变 1024 上限校验。创建后由 Worker 执行，会产生真实模型调用费用。</p>
        {message && <p className="import-feedback pass">{message}</p>}
      </form>

      <section className="panel" aria-label="测试集与跑测进度">
        <div className="panel-head">
          <h2>测试集与进度</h2>
          <button className="icon-btn" onClick={() => void refresh()} aria-label="刷新">
            <ArrowClockwiseIcon size={16} weight="bold" aria-hidden />
          </button>
        </div>
        {error && <p className="error">{error}</p>}
        <table>
          <thead>
            <tr>
              <th>场景</th>
              <th>数据集</th>
              <th>来源 revision</th>
              <th>题数</th>
              <th>最近运行</th>
            </tr>
          </thead>
          <tbody>
            {(overview?.items ?? []).map((preset) => (
              <tr key={preset.scenario}>
                <td className="mono">{preset.scenario}</td>
                <td className="mono">{preset.dataset}</td>
                <td className="mono">{preset.provenance?.revision ?? "—"}</td>
                <td className="mono">{preset.cases}</td>
                <td>
                  {preset.runs.length === 0 ? (
                    <span className="muted">未运行</span>
                  ) : (
                    <table className="run-progress">
                      <tbody>
                        {preset.runs.map((run) => (
                          <tr key={run.id}>
                            <td className="mono">{run.id}</td>
                            <td>
                              <StatusBadge status={run.status} />
                            </td>
                            <td className="mono">
                              {run.accuracy != null ? `${Math.round(run.accuracy * 100)}%` : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </td>
              </tr>
            ))}
            {(!overview || overview.items.length === 0) && (
              <tr>
                <td colSpan={5} className="empty">
                  暂无测试集，先在左侧下载并导入
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
