import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { DownloadSimpleIcon } from "@phosphor-icons/react";
import {
  createBenchmarkRun, getBenchmarkOverview, getModels, importBenchmark,
  type BenchmarkOverview, type ModelRecord,
} from "../../api/client";
import { ModelPicker } from "../../components/ModelPicker";
import { suiteRoutes } from "../registry";

const ROUTES = suiteRoutes("gsm8k");

export function Gsm8kOperate() {
  const navigate = useNavigate();
  const [overview, setOverview] = useState<BenchmarkOverview | null>(null);
  const [models, setModels] = useState<ModelRecord[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [revision, setRevision] = useState("");
  const [license, setLicense] = useState("MIT");
  const [importing, setImporting] = useState(false);
  const [running, setRunning] = useState(false);
  const [failures, setFailures] = useState<{ model: string; error: string }[]>([]);
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
    getModels().then((payload) => setModels(payload.items)).catch(() => undefined);
  }, [refresh]);

  const doImport = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setImporting(true);
    setError("");
    setMessage("");
    try {
      const payload = await importBenchmark({ revision: revision.trim(), license: license.trim() });
      setMessage(`已导入 ${payload.imported}（${payload.cases} 题）`);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setImporting(false);
    }
  };

  const doRun = async () => {
    setRunning(true);
    setFailures([]);
    setError("");
    try {
      const created: string[] = [];
      const failed: { model: string; error: string }[] = [];
      for (const model of selected) {
        try {
          const run = await createBenchmarkRun({ model });
          created.push(run.id);
        } catch (e) {
          failed.push({ model, error: String(e) });
        }
      }
      setFailures(failed);
      if (created.length > 0) {
        navigate(ROUTES.monitor(created));
        return;
      }
      if (failed.length === 0) {
        setError("请先选择至少一个模型");
      }
    } finally {
      setRunning(false);
    }
  };

  const preset = overview?.items[0];

  return (
    <div className="page">
      <section className="panel detail" aria-label="GSM8K 操作页">
        <div className="panel-head">
          <h2>GSM8K 数学评测 · 操作</h2>
        </div>
        {error && <p className="error">{error}</p>}

        {!preset ? (
          <form onSubmit={doImport} aria-label="下载并导入测试集">
            <h3 className="embed-title">导入测试集</h3>
            <p className="hint">从官方 openai/grade-school-math 仓库按 pinned commit 下载 test.jsonl，取前 20 题生成冒烟数据集。</p>
            <label>
              官方仓库 commit（40 位）
              <input value={revision} onChange={(change) => setRevision(change.target.value)} placeholder="完整 40 位 commit hash" required />
            </label>
            <label>
              License
              <input value={license} onChange={(change) => setLicense(change.target.value)} />
            </label>
            <button type="submit" disabled={importing}>
              <DownloadSimpleIcon size={14} weight="bold" aria-hidden />
              {importing ? "下载中…" : "下载并导入"}
            </button>
            {message && <p className="import-feedback pass">{message}</p>}
          </form>
        ) : (
          <div className="operate-grid">
            <div className="operate-card">
              <h3 className="embed-title">数据集（pinned）</h3>
              <p className="mono">{preset.scenario}</p>
              <p className="hint mono">
                {preset.dataset} · {preset.cases} 题
                {preset.provenance?.revision ? ` · revision ${String(preset.provenance.revision).slice(0, 7)}` : ""}
              </p>
            </div>
            <div className="operate-card">
              <h3 className="embed-title">模型（可多选对比）</h3>
              <ModelPicker
                models={models}
                selected={selected}
                onToggle={(id) => setSelected((current) =>
                  current.includes(id) ? current.filter((item) => item !== id) : [...current, id])}
              />
            </div>
            <div className="operate-card">
              <h3 className="embed-title">跑测参数（preset 固定）</h3>
              <dl className="kv">
                <dt>题数</dt><dd className="mono">{preset.cases}</dd>
                <dt>输出上限</dt><dd className="mono">1024</dd>
                <dt>重试</dt><dd className="mono">0</dd>
              </dl>
            </div>
          </div>
        )}

        {preset && (
          <div className="actions">
            <button type="button" onClick={() => void doRun()} disabled={running || selected.length === 0}>
              {running ? "创建中…" : `发起跑测（${selected.length} 个模型 × ${preset.cases} 题）`}
            </button>
            <span className="hint">真实调用 · 产生费用 · 发起后自动进入过程页</span>
          </div>
        )}
        {failures.length > 0 && (
          <ul>
            {failures.map((failure) => (
              <li key={failure.model} className="error">{failure.model}：{failure.error}</li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
