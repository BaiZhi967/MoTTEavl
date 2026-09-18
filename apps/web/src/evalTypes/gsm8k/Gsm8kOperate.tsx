import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { DownloadSimpleIcon } from "@phosphor-icons/react";
import {
  createBenchmarkRun, getBenchmarkOverview, getModels, importBenchmark,
  type BenchmarkOverview, type BenchmarkPreset, type ModelRecord,
} from "../../api/client";
import { ModelPicker } from "../../components/ModelPicker";
import { suiteRoutes } from "../registry";
import { DATASET_NAME, SCOPE_OPTIONS, scopeLabel, sortPresets } from "./presets";
import {
  clearCaseSelection, loadCaseSelection, randomSeed, selectionRequest, type StoredSelection,
} from "./selection";

const ROUTES = suiteRoutes("gsm8k");
type CaseMode = "all" | "ids" | "random";

export function Gsm8kOperate() {
  const navigate = useNavigate();
  const [overview, setOverview] = useState<BenchmarkOverview | null>(null);
  const [chosenScenario, setChosenScenario] = useState("");
  const [models, setModels] = useState<ModelRecord[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [revision, setRevision] = useState("");
  const [license, setLicense] = useState("MIT");
  const [scope, setScope] = useState<"full" | "smoke">("full");
  const [name, setName] = useState(DATASET_NAME);
  const [version, setVersion] = useState("");
  const [importing, setImporting] = useState(false);
  const [running, setRunning] = useState(false);
  const [failures, setFailures] = useState<{ model: string; error: string }[]>([]);
  const [launched, setLaunched] = useState<string[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [caseMode, setCaseMode] = useState<CaseMode>("all");
  const [randomCount, setRandomCount] = useState("100");
  const [seed, setSeed] = useState(randomSeed);
  const [picked, setPicked] = useState<StoredSelection | null>(null);
  const [levels, setLevels] = useState<Record<string, string>>({});

  const presets = useMemo(() => sortPresets(overview?.items ?? []), [overview]);
  /* 默认选中题数最多的数据集（全量优先）；用户的选择一旦落进 state 就不再被覆盖。 */
  const preset: BenchmarkPreset | undefined =
    presets.find((item) => item.scenario === chosenScenario) ?? presets[0];
  /* 指定题目来自「题目」页：数据集对不上时不能直接发起，避免把别的题集 id 发上去。 */
  const pickedMismatch = Boolean(picked && preset && picked.dataset !== preset.dataset);
  const usablePicked = picked && !pickedMismatch ? picked : null;
  const reasoningModels = useMemo(
    () => models.filter((model) => selected.includes(model.id) && model.reasoning?.supported),
    [models, selected]);
  /* 本次每个模型真正会跑的题数（按钮与参数卡共用，避免显示数据集总题数误导成本）。 */
  const runSize = caseMode === "random" ? Math.max(0, Math.floor(Number(randomCount) || 0))
    : caseMode === "ids" ? usablePicked?.caseIds.length ?? 0
      : preset?.cases ?? 0;

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
    setPicked(loadCaseSelection());
  }, [refresh]);

  /* 数据集切换后旧勾选不再适用；换数据集时清空，避免「指定题目」带着别的题集 id 发起。 */
  useEffect(() => {
    if (picked && preset && picked.dataset !== preset.dataset) setCaseMode((mode) => (mode === "ids" ? "all" : mode));
  }, [picked, preset]);

  const doImport = async (form: FormEvent<HTMLFormElement>) => {
    form.preventDefault();
    setImporting(true);
    setError("");
    setMessage("");
    try {
      // 默认（高级设置留空）= 官方最新 commit + 全量 + 自动版本号，服务端解析后落库固定 revision。
      const payload = await importBenchmark({
        revision: revision.trim(),
        license: license.trim(),
        name: name.trim(),
        version: version.trim(),
        scope,
      });
      setMessage(`已导入 ${payload.imported} · ${scopeLabel(payload.scope)} · ${payload.cases} 题`
        + ` · revision ${payload.revision.slice(0, 7)}`);
      setChosenScenario(payload.scenario);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setImporting(false);
    }
  };

  const doRun = async () => {
    if (!preset) return;
    const selection = selectionRequest(caseMode, {
      caseIds: usablePicked?.caseIds,
      count: Number(randomCount),
      seed,
    });
    if (!selection) {
      setError(caseMode === "ids" ? "请先在「题目」页勾选要跑的题目" : "请填写随机题数（1 到数据集题数之间）");
      return;
    }
    setRunning(true);
    setFailures([]);
    setLaunched([]);
    setError("");
    try {
      const created: string[] = [];
      const failed: { model: string; error: string }[] = [];
      for (const model of selected) {
        try {
          const run = await createBenchmarkRun({
            model, scenario: preset.scenario, case_selection: selection,
            ...(levels[model] ? { reasoning_level: levels[model] } : {}),
          });
          created.push(run.id);
        } catch (e) {
          failed.push({ model, error: String(e) });
        }
      }
      setFailures(failed);
      if (created.length > 0 && failed.length === 0) {
        navigate(ROUTES.monitor(created));
        return;
      }
      if (created.length > 0) {
        // 混合成败：留在本页让失败就地可见，只给出批次入口。
        setLaunched(created);
        return;
      }
      if (failed.length === 0) {
        setError("请先选择至少一个模型");
      }
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="page">
      <section className="panel detail" aria-label="GSM8K 操作页">
        <div className="panel-head">
          <h2>GSM8K 数学评测 · 操作</h2>
        </div>
        {error && <p className="error">{error}</p>}

        <div className="operate-grid">
          <div className="operate-card">
            <h3 className="embed-title">数据集（版本固定）</h3>
            {preset ? (
              <>
                {presets.length > 1 && (
                  <div className="inline-field">
                    <span className="field-label">运行数据集</span>
                    <select
                      className="control"
                      aria-label="运行数据集"
                      value={preset.scenario}
                      onChange={(change) => setChosenScenario(change.target.value)}
                    >
                      {presets.map((item) => (
                        <option key={item.scenario} value={item.scenario}>
                          {item.scenario} · {scopeLabel(item.scope)} · {item.cases} 题
                        </option>
                      ))}
                    </select>
                  </div>
                )}
                <p className="mono">{preset.scenario}</p>
                <p className="hint mono">
                  {preset.dataset} · {scopeLabel(preset.scope)} · {preset.cases} 题
                  {preset.provenance?.revision ? ` · revision ${String(preset.provenance.revision).slice(0, 7)}` : ""}
                </p>
                {preset.provenance?.synthetic === true && (
                  <p className="hint">合成夹具（synthetic）：题面为占位数据，不是官方题目。</p>
                )}
              </>
            ) : (
              <p className="hint">尚未导入数据集。用「下载并导入数据集」卡从官方仓库按 pinned commit 取题。</p>
            )}
          </div>

          <div className="operate-card">
            <h3 className="embed-title">题目（本次运行跑哪些题）</h3>
            <label>
              本次运行题目
              <select
                className="control"
                value={caseMode}
                onChange={(change) => setCaseMode(change.target.value === "ids" ? "ids"
                  : change.target.value === "random" ? "random" : "all")}
              >
                <option value="all">全部（{preset?.cases ?? 0} 题）</option>
                <option value="random">随机 N 题</option>
                <option value="ids">指定题目（题目页勾选）</option>
              </select>
            </label>
            {caseMode === "random" && (
              <>
                <label>
                  随机题数
                  <input
                    type="number" min={1} max={preset?.cases ?? 1} value={randomCount}
                    onChange={(change) => setRandomCount(change.target.value)}
                  />
                </label>
                <label>
                  随机种子（写进运行快照，可复现）
                  <input className="mono" value={seed} onChange={(change) => setSeed(change.target.value)} />
                </label>
                <button type="button" className="link" onClick={() => setSeed(randomSeed())}>重新生成种子</button>
              </>
            )}
            {caseMode === "ids" && (
              <p className="hint">
                {usablePicked
                  ? `已选 ${usablePicked.caseIds.length} 题（${usablePicked.dataset}）`
                  : pickedMismatch
                    ? `已选题目来自 ${picked?.dataset}，当前数据集是 ${preset?.dataset}；请回题目页重新选择。`
                    : "尚未选择题目。"}
                {" "}
                <Link className="link" to={ROUTES.cases}>去题目页选择</Link>
                {usablePicked && (
                  <button
                    type="button" className="link"
                    onClick={() => { clearCaseSelection(); setPicked(null); setCaseMode("all"); }}
                  >
                    清除
                  </button>
                )}
              </p>
            )}
            {caseMode === "all" && (
              <p className="hint">
                整份数据集（{preset?.cases ?? 0} 题）。
                <Link className="link" to={ROUTES.cases}>浏览 / 勾选题目</Link>
              </p>
            )}
          </div>

          <div className="operate-card">
            <h3 className="embed-title">模型（可多选对比）</h3>
            <ModelPicker
              models={models}
              selected={selected}
              onToggle={(id) => setSelected((current) =>
                current.includes(id) ? current.filter((item) => item !== id) : [...current, id])}
            />
            {reasoningModels.map((model) => (
              <label key={model.id}>
                {model.id} 的思考强度（默认 {model.reasoning?.default_level ?? "无"}）
                <select
                  className="control mono"
                  value={levels[model.id] ?? ""}
                  onChange={(change) => setLevels((current) => ({ ...current, [model.id]: change.target.value }))}
                >
                  <option value="">默认</option>
                  {(model.reasoning?.levels ?? []).map((level) => (
                    <option key={level} value={level}>{level}</option>
                  ))}
                </select>
              </label>
            ))}
            {reasoningModels.length > 0 && (
              <p className="hint">
                输出上限固定 1024：高思考强度会把预算耗在思考上，正文可能为空并记为解析失败。
              </p>
            )}
          </div>

          <div className="operate-card">
            <h3 className="embed-title">跑测参数（preset 固定）</h3>
            <dl className="kv">
              <dt>题数</dt>
              <dd className="mono">
                {caseMode === "random" ? `${randomCount || 0} / ${preset?.cases ?? 0}`
                  : caseMode === "ids" ? `${usablePicked?.caseIds.length ?? 0} / ${preset?.cases ?? 0}`
                    : preset?.cases ?? "—"}
              </dd>
              <dt>输出上限</dt><dd className="mono">1024</dd>
              <dt>重试</dt><dd className="mono">0</dd>
            </dl>
          </div>

          <div className="operate-card">
            <h3 className="embed-title">下载并导入数据集</h3>
            <p className="hint">
              默认解析官方仓库数据文件的最新 commit，下载整个 test split（全量）并校验全文件；源文件存到
              var/datasets/gsm8k/，题数写进不可变数据集，评分分母随之为该题数。需要冒烟子集、指定 commit
              或指定版本时展开高级设置。
            </p>
            <form onSubmit={doImport} aria-label="下载并导入数据集">
              <button type="submit" disabled={importing}>
                <DownloadSimpleIcon size={14} weight="bold" aria-hidden />
                {importing ? "下载中…"
                  : revision.trim() ? `按指定 commit 下载${scopeLabel(scope)}数据集`
                    : `下载最新${scopeLabel(scope)}数据集`}
              </button>
              <details className="disclosure">
                <summary>高级设置</summary>
                <label>
                  题目范围
                  <select
                    className="control"
                    value={scope}
                    onChange={(change) => setScope(change.target.value === "smoke" ? "smoke" : "full")}
                  >
                    {SCOPE_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </label>
                <label>
                  官方仓库 commit（40 位）
                  <input
                    value={revision}
                    onChange={(change) => setRevision(change.target.value)}
                    placeholder="留空 = 自动解析官方最新"
                  />
                </label>
                <label>
                  数据集名
                  <input value={name} onChange={(change) => setName(change.target.value)} />
                </label>
                <label>
                  数据集版本
                  <input
                    value={version}
                    onChange={(change) => setVersion(change.target.value)}
                    placeholder="留空 = 自动（同内容复用，否则下一个空号）"
                  />
                </label>
                <label>
                  License
                  <input value={license} onChange={(change) => setLicense(change.target.value)} />
                </label>
              </details>
              {message && <p className="import-feedback pass">{message}</p>}
            </form>
          </div>
        </div>

        <div className="actions">
          <button
            type="button"
            className="primary"
            onClick={() => void doRun()}
            disabled={running || selected.length === 0 || !preset || runSize <= 0}
          >
            {running ? "创建中…" : `发起跑测（${selected.length} 个模型 × ${runSize} 题）`}
          </button>
          <span className="hint">真实调用 · 产生费用 · 发起后自动进入过程页</span>
        </div>
        {failures.length > 0 && (
          <ul className="failure-list">
            {failures.map((failure) => (
              <li key={failure.model} className="error">{failure.model}：{failure.error}</li>
            ))}
          </ul>
        )}
        {launched.length > 0 && (
          <p>
            已创建 {launched.length} 个运行 ·{" "}
            <Link className="link" to={ROUTES.monitor(launched)}>查看批次进度</Link>
          </p>
        )}
      </section>
    </div>
  );
}
